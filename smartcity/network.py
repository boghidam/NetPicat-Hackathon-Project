"""Road network: a directed graph of intersections and one-lane segments with polyline geometry.

Built from plain graph data (real OpenStreetMap data for Iași, or a synthetic grid for tests).
Every segment runs centre to centre of two intersections along a polyline; vehicles must stop
at the *stop line*, `box_half` metres before the centre of the intersection they approach.

An intersection with three or more approaches is signalised. Its approaches are grouped into
phases: approaches whose headings are roughly opposite share a green, and an unpaired street
(such as the stem of a T-junction) has a phase of its own. Boundary nodes of the cropped map are
gates where cars enter and leave.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field

import networkx as nx

from .config import Config

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
OPPOSITE_TOLERANCE = math.radians(40)
STRAIGHT_LIMIT = math.radians(35)


# ---------------------------------------------------------------------------- plain input data
@dataclass
class EdgeData:
    id: int
    u: int
    v: int
    length: float  # metres
    speed_kph: float
    geometry: list[tuple[float, float]]  # (lon, lat) from u to v
    highway: str = ""
    name: str = ""


@dataclass
class GraphData:
    nodes: list[tuple[int, float, float]]  # id (0..n-1 in order), lat, lon
    edges: list[EdgeData]  # edge ids must be 0..n-1 in order
    landmarks: list[tuple[str, float, float]] = field(default_factory=list)
    source: str = ""


@dataclass
class RouteData:
    ref: str  # public line number, e.g. "28"
    name: str
    edges: list[int]  # cyclic, contiguous edge ids
    stops: list[tuple[float, float]] = field(default_factory=list)  # lat, lon
    backbone: bool = False  # a major line that runs more buses


# ---------------------------------------------------------------------------- projection
class LocalProjection:
    """Equirectangular projection around a centre: (lat, lon) <-> metres east/north."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = M_PER_DEG_LON * math.cos(math.radians(lat0))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG_LAT

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / M_PER_DEG_LAT, self.lon0 + x / self.kx


def wrap(angle: float) -> float:
    """Wrap to (-pi, pi]."""
    return (angle + math.pi) % (2 * math.pi) - math.pi


def compass(angle_math: float) -> str:
    """8-point compass name for a heading given as a math angle (radians, ccw from east)."""
    bearing = (90.0 - math.degrees(angle_math)) % 360.0
    return COMPASS[int((bearing + 22.5) // 45) % 8]


# ---------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Intersection:
    id: int
    x: float
    y: float
    lat: float
    lon: float
    signalised: bool
    gate: bool


@dataclass(frozen=True)
class Segment:
    id: int
    from_node: int
    to_node: int
    length: float  # m
    speed_limit: float  # m/s
    stop_pos: float  # m from the start where vehicles stop on red
    points: tuple[tuple[float, float], ...]  # polyline in metres, scaled to `length`
    cum: tuple[float, ...]  # cumulative arc length at each point
    heading_in: float  # direction of travel at the end (math angle, radians)
    heading_out: float  # direction of travel at the start
    highway: str = ""
    name: str = ""
    tangents: tuple[tuple[float, float], ...] = ()  # unit direction of each polyline piece (for fast lookups)

    @property
    def arrives_from(self) -> str:
        """Compass side of the junction this approach arrives from."""
        return compass(self.heading_in + math.pi)


class RoadNetwork:
    def __init__(self, data: GraphData, cfg: Config, routes: list[RouteData] | None = None):
        self.cfg = cfg
        self.landmarks_ll = data.landmarks
        self.source = data.source
        lat0 = sum(n[1] for n in data.nodes) / len(data.nodes)
        lon0 = sum(n[2] for n in data.nodes) / len(data.nodes)
        self.proj = LocalProjection(lat0, lon0)
        self.segments: list[Segment] = []
        self.in_segs: list[list[int]] = [[] for _ in data.nodes]
        self.out_segs: list[list[int]] = [[] for _ in data.nodes]
        xy = {nid: self.proj.to_xy(lat, lon) for nid, lat, lon in data.nodes}
        for e in data.edges:
            assert e.id == len(self.segments), "edge ids must be 0..n-1"
            self.segments.append(self._make_segment(e))
            self.out_segs[e.u].append(e.id)
            self.in_segs[e.v].append(e.id)

        self.bounds = (
            min(p[0] for p in xy.values()), min(p[1] for p in xy.values()),
            max(p[0] for p in xy.values()), max(p[1] for p in xy.values()),
        )  # fmt: skip
        self.gates: list[int] = self._choose_gates(xy)
        gate_set = set(self.gates)
        self.nodes: list[Intersection] = [
            Intersection(
                nid, xy[nid][0], xy[nid][1], lat, lon,
                signalised=len(self.in_segs[nid]) >= cfg.min_approaches and nid not in gate_set,
                gate=nid in gate_set,
            )
            for nid, lat, lon in data.nodes
        ]  # fmt: skip
        self._turns = {s.id: self._build_turns(s) for s in self.segments}
        self._groups: dict[int, list[list[int]]] = {}
        self.partner: dict[int, int] = {}  # approach -> the opposite approach sharing its phase
        for node in self.nodes:
            if node.signalised:
                self._groups[node.id] = self._pair_approaches(node.id)

        self.nx = nx.MultiDiGraph()
        self.nx.add_nodes_from(n.id for n in self.nodes)  # isolated nodes too
        for s in self.segments:
            self.nx.add_edge(s.from_node, s.to_node, key=s.id, time=s.length / s.speed_limit, length=s.length)
        self._paths: dict[tuple[int, int], list[int] | None] = {}
        self.routes: list[RouteData] = []
        self.route_by_ref: dict[str, RouteData] = {}
        self.route_index: dict[str, int] = {}
        self.set_routes(routes or [])

    def set_routes(self, routes: list[RouteData]) -> None:
        """Register public bus lines (cyclic, contiguous edge lists)."""
        for r in routes:
            for a, b in zip(r.edges, r.edges[1:] + r.edges[:1], strict=True):
                assert self.segments[a].to_node == self.segments[b].from_node, f"line {r.ref} is not contiguous"
        self.routes = routes
        self.route_by_ref = {r.ref: r for r in routes}
        self.route_index = {r.ref: i for i, r in enumerate(routes)}

    # -- construction helpers ------------------------------------------------
    def _make_segment(self, e: EdgeData) -> Segment:
        pts = [self.proj.to_xy(lat, lon) for lon, lat in e.geometry]
        cum = [0.0]
        for a, b in zip(pts, pts[1:], strict=False):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        scale = e.length / cum[-1] if cum[-1] > 0 else 1.0
        cum = [c * scale for c in cum]
        x0, y0 = pts[0]
        pts = [(x0 + (x - x0) * scale, y0 + (y - y0) * scale) for x, y in pts]

        def heading_at(start: float, end: float) -> float:
            (xa, ya), (xb, yb) = self._interp(pts, cum, start), self._interp(pts, cum, end)
            return math.atan2(yb - ya, xb - xa)

        span = min(20.0, e.length / 2)
        speed = max(20.0, min(70.0, e.speed_kph)) / 3.6
        stop = e.length - min(self.cfg.box_half, 0.4 * e.length)
        return Segment(
            e.id, e.u, e.v, e.length, speed, stop, tuple(pts), tuple(cum),
            heading_in=heading_at(e.length - span, e.length), heading_out=heading_at(0.0, span),
            highway=e.highway, name=e.name,
            tangents=tuple(
                ((b[0] - a[0]) / n, (b[1] - a[1]) / n)
                for a, b in zip(pts, pts[1:], strict=False)
                for n in [math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0]
            ),
        )  # fmt: skip

    @staticmethod
    def _interp(pts, cum, s: float) -> tuple[float, float]:
        s = max(0.0, min(cum[-1], s))
        i = min(bisect.bisect_right(cum, s) - 1, len(pts) - 2)
        seg_len = cum[i + 1] - cum[i]
        f = (s - cum[i]) / seg_len if seg_len > 0 else 0.0
        return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f

    def _choose_gates(self, xy: dict[int, tuple[float, float]]) -> list[int]:
        """Boundary nodes (with traffic both ways) spread around the map edge."""
        x0, y0, x1, y1 = self.bounds
        band = self.cfg.gate_band
        cands = []
        for nid, (x, y) in xy.items():
            on_edge = min(x - x0, x1 - x, y - y0, y1 - y) <= band
            if on_edge and self.in_segs[nid] and self.out_segs[nid]:
                speed = max(self.segments[s].speed_limit for s in self.in_segs[nid] + self.out_segs[nid])
                cands.append((nid, len(self.in_segs[nid]) + speed / 20.0))
        if not cands:
            return []
        cands.sort(key=lambda c: -c[1])
        chosen = [cands[0][0]]
        while len(chosen) < min(self.cfg.max_gates, len(cands)):  # farthest-point sampling
            best = max(
                (c for c in cands if c[0] not in chosen),
                key=lambda c: min(math.dist(xy[c[0]], xy[g]) for g in chosen),
            )
            chosen.append(best[0])
        return chosen

    def _build_turns(self, seg: Segment) -> list[tuple[int, float]]:
        options = []
        for t_id in self.out_segs[seg.to_node]:
            t = self.segments[t_id]
            delta = wrap(t.heading_out - seg.heading_in)
            options.append((t_id, delta, t.to_node == seg.from_node and abs(delta) > math.radians(150)))
        non_u = [o for o in options if not o[2]]
        options = non_u or options  # a dead end allows the U-turn
        weights = [
            1.0 if abs(d) <= STRAIGHT_LIMIT else (0.5 if abs(d) <= math.radians(100) else 0.3) for _, d, _ in options
        ]
        total = sum(weights) or 1.0
        return [(t, w / total) for (t, _, _), w in zip(options, weights, strict=True)]

    def _pair_approaches(self, node: int) -> list[list[int]]:
        ids = sorted(self.in_segs[node], key=lambda s: wrap(self.segments[s].heading_in) % (2 * math.pi))
        pairs = []
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                off = abs(abs(wrap(self.segments[a].heading_in - self.segments[b].heading_in)) - math.pi)
                if off < OPPOSITE_TOLERANCE:
                    pairs.append((off, a, b))
        used: set[int] = set()
        groups = []
        for _, a, b in sorted(pairs):
            if a not in used and b not in used:
                used.update((a, b))
                groups.append([a, b])
                self.partner[a], self.partner[b] = b, a
        groups += [[a] for a in ids if a not in used]
        return sorted(groups, key=lambda g: ids.index(g[0]))

    # -- topology queries ---------------------------------------------------------
    def approaches(self, node: int) -> list[Segment]:
        return [self.segments[s] for s in self.in_segs[node]]

    def phase_groups(self, node: int) -> list[list[int]]:
        """Approach ids that share a green. Empty for an unsignalised node."""
        return self._groups.get(node, [])

    def turn_targets(self, seg_id: int) -> list[tuple[int, float]]:
        """Where vehicles leaving this segment can go (a U-turn only at a dead end), with their share."""
        return self._turns[seg_id]

    def turn_kind(self, seg_id: int, target_id: int) -> str:
        seg, t = self.segments[seg_id], self.segments[target_id]
        delta = wrap(t.heading_out - seg.heading_in)
        if abs(delta) <= STRAIGHT_LIMIT:
            return "straight"
        if abs(delta) > math.radians(150):
            return "u"
        return "left" if delta > 0 else "right"

    def neighbours(self, node: int) -> set[int]:
        return set(self.nx.successors(node)) | set(self.nx.predecessors(node))

    def segment_between(self, a: int, b: int) -> int:
        return min(self.nx[a][b], key=lambda k: self.nx[a][b][k]["length"])

    # -- routing -----------------------------------------------------------------
    def path_between(self, a: int, b: int) -> list[int] | None:
        """Fastest path of segment ids from node a to node b (cached)."""
        key = (a, b)
        if key not in self._paths:
            try:
                nodes = nx.shortest_path(self.nx, a, b, weight="time")
                self._paths[key] = [
                    min(self.nx[u][v], key=lambda k: self.nx[u][v][k]["time"])
                    for u, v in zip(nodes, nodes[1:], strict=False)
                ]
            except nx.NetworkXNoPath:
                self._paths[key] = None
        return self._paths[key]

    def random_trip(self, rng: random.Random) -> list[int]:
        """A gate-to-gate path for a car."""
        while True:
            a, b = rng.sample(self.gates, 2)
            path = self.path_between(a, b)
            if path:
                return path

    # -- geometry ----------------------------------------------------------------
    @property
    def width(self) -> float:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> float:
        return self.bounds[3] - self.bounds[1]

    def point_at(self, seg_id: int, pos: float) -> tuple[float, float, float, float]:
        """Centre-line point `pos` metres along a segment, with the unit tangent: (x, y, dx, dy)."""
        seg = self.segments[seg_id]
        pos = max(0.0, min(seg.length, pos))
        i = min(bisect.bisect_right(seg.cum, pos) - 1, len(seg.points) - 2)
        (x0, y0), (x1, y1) = seg.points[i], seg.points[i + 1]
        span = seg.cum[i + 1] - seg.cum[i]
        f = (pos - seg.cum[i]) / span if span > 0 else 0.0
        tx, ty = seg.tangents[i]
        return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, tx, ty

    def position(self, seg_id: int, pos: float) -> tuple[float, float, float, float]:
        """Like `point_at`, shifted to the right-hand lane: (x, y, dx, dy)."""
        x, y, dx, dy = self.point_at(seg_id, pos)
        off = self.cfg.lane_offset
        return x + dy * off, y - dx * off, dx, dy
