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

from .buildings import IASI_CENTER_LAT, IASI_CENTER_LON, Building
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
    turnaround_leg: int = 0  # leg index reaching the far terminus (0 = loop with single terminus)
    metropolitan: bool = False


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
    kind: str = "standard"  # "standard" | "roundabout" | "yield"


@dataclass(frozen=True)
class Roundabout:
    node: int
    name: str
    x: float
    y: float
    radius_m: float
    ring_lanes: int  # 1 or 2 concentric circulating lanes (HG 130/2023)
    signalized: bool  # True for mega-roundabouts (Podu Roș, Podu de Piatră); False for pure yield-to-left
    arm_angles: dict[int, float] = field(default_factory=dict)  # seg_id -> perimeter angle (radians)


@dataclass(frozen=True)
class BusStop:
    id: int
    seg_id: int
    pos_m: float
    x: float
    y: float
    name: str
    has_bay: bool
    demand: float = 1.0


@dataclass(frozen=True)
class Crosswalk:
    id: int
    seg_id: int
    pos_m: float
    x: float
    y: float
    hub: str
    rate: float = 0.02


@dataclass(frozen=True)
class RailCrossing:
    name: str
    seg_id: int
    pos_m: float
    x: float
    y: float
    period_s: float
    closed_s: float
    offset_s: float


@dataclass(frozen=True)
class Hospital:
    name: str
    node: int
    x: float
    y: float


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
    lanes: int = 1  # mixed-traffic lanes in this direction (1, 2, or 3)
    oneway: bool = False
    grade_pct: float = 0.0  # uphill (+) / downhill (-) road grade in percent

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

        # Urban zoning & optional residential buildings (populated on real city maps such as Iași)
        self.city_center_xy: tuple[float, float] = self.proj.to_xy(IASI_CENTER_LAT, IASI_CENTER_LON)
        self.city_seg_ids: set[int] = {
            s.id for s in self.segments if self.is_city_xy(*self.point_at(s.id, s.length * 0.5)[:2])
        }
        self.buildings: list[Building] = []
        self.building_by_id: dict[int, Building] = {}
        self.city_buildings: list[Building] = []
        self.outer_buildings: list[Building] = []
        self._node_dist_m: dict[int, dict[int, float]] = {}
        self._city_dests_by_node: dict[int, list[Building]] = {}
        self._outer_dests_by_node: dict[int, list[Building]] = {}
        self.through_gate_pairs: list[tuple[int, int, list[int], float]] = []

        # Realism structures (populated on real city maps via `apply_realism`)
        self.roundabouts: list[Roundabout] = []
        self.roundabout_by_node: dict[int, Roundabout] = {}
        self.yield_nodes: set[int] = set()
        self.yield_minor_approaches: dict[int, set[int]] = {}  # node -> side-street approaches that yield
        self.bus_stops: list[BusStop] = []
        self.stops_by_seg: dict[int, list[BusStop]] = {}
        self.crosswalks: list[Crosswalk] = []
        self.crosswalks_by_seg: dict[int, list[Crosswalk]] = {}
        self.rail_crossings: list[RailCrossing] = []
        self.rail_by_seg: dict[int, RailCrossing] = {}
        self.hospitals: list[Hospital] = []

    def apply_realism(self, data: dict) -> None:
        """Apply multi-lane, roundabout, yield-junction, CTP bus-stop, hill-grade, and crosswalk metadata."""
        from dataclasses import replace

        edge_lanes = data.get("edge_lanes") or {}
        edge_grades = data.get("edge_grades") or {}
        rb_raw = data.get("roundabouts") or []
        yield_set = {int(n) for n in (data.get("yield_nodes") or [])}
        rb_by_node_raw = {int(r["node"]): r for r in rb_raw}

        # Identify one-way vs two-way directed pairs
        edge_pairs = {(s.from_node, s.to_node) for s in self.segments}
        new_segs: list[Segment] = []
        for seg in self.segments:
            lanes = max(1, min(3, int(edge_lanes.get(str(seg.id), seg.lanes))))
            oneway = (seg.to_node, seg.from_node) not in edge_pairs
            grade = float(edge_grades.get(str(seg.id), 0.0))
            stop_pos = seg.stop_pos
            rb_info = rb_by_node_raw.get(seg.to_node)
            if rb_info is not None:
                rad = float(rb_info["radius_m"])
                stop_pos = seg.length - min(rad + 4.5, 0.42 * seg.length)
            new_segs.append(replace(seg, lanes=lanes, oneway=oneway, grade_pct=grade, stop_pos=stop_pos))
        self.segments = new_segs

        # Rebuild Intersection list with real roundabout / yield kinds & updated signalised flags
        gate_set = set(self.gates)
        new_nodes: list[Intersection] = []
        for n in self.nodes:
            if n.id in rb_by_node_raw:
                sig = bool(rb_by_node_raw[n.id].get("signalized", False)) and n.id not in gate_set
                new_nodes.append(replace(n, signalised=sig, kind="roundabout"))
            elif n.id in yield_set and n.id not in gate_set:
                new_nodes.append(replace(n, signalised=False, kind="yield"))
            else:
                new_nodes.append(n)
        self.nodes = new_nodes
        self.yield_nodes = {n.id for n in self.nodes if n.kind == "yield"}

        # Rebuild signal phase groups & opposite partners for the updated signalised set
        self._groups = {}
        self.partner = {}
        for node in self.nodes:
            if node.signalised:
                self._groups[node.id] = self._pair_approaches(node.id)

        # Build Roundabout objects with exact right-hand arm perimeter angles (counterclockwise math angles)
        self.roundabouts = []
        self.roundabout_by_node = {}
        for r in rb_raw:
            nid = int(r["node"])
            node = self.nodes[nid]
            rad = float(r["radius_m"])
            arm_angles: dict[int, float] = {}
            for sid in self.in_segs[nid]:
                seg = self.segments[sid]
                p_rim = max(0.0, seg.length - min(rad + 3.0, seg.length * 0.42))
                px, py, _, _ = self.position(sid, p_rim, lane=0.0)
                arm_angles[sid] = math.atan2(py - node.y, px - node.x)
            for sid in self.out_segs[nid]:
                seg = self.segments[sid]
                p_rim = min(seg.length, min(rad + 2.5, seg.length * 0.35))
                px, py, _, _ = self.position(sid, p_rim, lane=0.0)
                arm_angles[sid] = math.atan2(py - node.y, px - node.x)
            rb = Roundabout(
                node=nid,
                name=str(r.get("name", f"Rond n{nid}")),
                x=node.x,
                y=node.y,
                radius_m=rad,
                ring_lanes=max(1, min(2, int(r.get("ring_lanes", 1)))),
                signalized=bool(r.get("signalized", False)),
                arm_angles=arm_angles,
            )
            self.roundabouts.append(rb)
            self.roundabout_by_node[nid] = rb

        # Identify minor yielding arm(s) at each unsignalized Cedează T-junction (the stem of the T yields!)
        self.yield_minor_approaches = {}
        for nid in self.yield_nodes:
            groups = self._pair_approaches(nid)
            # Paired opposite streets form the main priority road; any unpaired stem yields!
            paired = {a for g in groups if len(g) >= 2 for a in g}
            minor = set(self.in_segs[nid]) - paired
            if not minor and len(self.in_segs[nid]) >= 2:
                # Fallback: approach with lowest lane count / speed limit yields
                worst = min(self.in_segs[nid], key=lambda s: (self.segments[s].lanes, self.segments[s].speed_limit))
                minor = {worst}
            self.yield_minor_approaches[nid] = minor

        # Real CTP bus stops
        self.bus_stops = []
        self.stops_by_seg = {}
        n_segs = len(self.segments)
        for st in data.get("bus_stops") or []:
            sid = int(st["seg_id"])
            if 0 <= sid < n_segs:
                seg = self.segments[sid]
                pos_m = max(14.0, min(seg.stop_pos - 12.0, float(st["pos_m"])))
                sx, sy, _, _ = self.position(sid, pos_m, lane=0.0)
                bs = BusStop(
                    id=int(st["id"]),
                    seg_id=sid,
                    pos_m=pos_m,
                    x=sx,
                    y=sy,
                    name=str(st.get("name", f"Stație {sid}")),
                    has_bay=bool(st.get("has_bay", False)),
                    demand=float(st.get("demand", 1.0)),
                )
                self.bus_stops.append(bs)
                self.stops_by_seg.setdefault(sid, []).append(bs)
        for items in self.stops_by_seg.values():
            items.sort(key=lambda s: s.pos_m)

        # Pedestrian crosswalks
        self.crosswalks = []
        self.crosswalks_by_seg = {}
        for cw in data.get("crosswalks") or []:
            sid = int(cw["seg_id"])
            if 0 <= sid < n_segs:
                seg = self.segments[sid]
                pos_m = max(12.0, min(seg.stop_pos - 4.0, float(cw["pos_m"])))
                cx, cy, _, _ = self.point_at(sid, pos_m)
                obj = Crosswalk(
                    id=int(cw["id"]),
                    seg_id=sid,
                    pos_m=pos_m,
                    x=cx,
                    y=cy,
                    hub=str(cw.get("hub", "")),
                    rate=float(cw.get("rate", 0.02)),
                )
                self.crosswalks.append(obj)
                self.crosswalks_by_seg.setdefault(sid, []).append(obj)

        # CFR railway level crossings
        self.rail_crossings = []
        self.rail_by_seg = {}
        for rc in data.get("rail_crossings") or []:
            sid = int(rc["seg_id"])
            if 0 <= sid < n_segs:
                seg = self.segments[sid]
                pos_m = max(20.0, min(seg.stop_pos - 15.0, float(rc["pos_m"])))
                rx, ry, _, _ = self.point_at(sid, pos_m)
                obj = RailCrossing(
                    name=str(rc.get("name", "Pasaj CFR")),
                    seg_id=sid,
                    pos_m=pos_m,
                    x=rx,
                    y=ry,
                    period_s=float(rc.get("period_s", 420.0)),
                    closed_s=float(rc.get("closed_s", 38.0)),
                    offset_s=float(rc.get("offset_s", 140.0)),
                )
                self.rail_crossings.append(obj)
                self.rail_by_seg[sid] = obj

        # Hospital dispatch hubs
        self.hospitals = []
        for h in data.get("hospitals") or []:
            nid = int(h["node"])
            if 0 <= nid < len(self.nodes):
                nd = self.nodes[nid]
                self.hospitals.append(Hospital(str(h.get("name", "Spital")), nid, nd.x, nd.y))

    def is_city_xy(self, x: float, y: float) -> bool:
        """True if local coordinate `(x, y)` lies inside the urban core radius."""
        cx, cy = self.city_center_xy
        return math.hypot(x - cx, y - cy) <= self.cfg.urban_radius_m

    def is_city_segment(self, seg_id: int) -> bool:
        """True if the midpoint of `seg_id` lies inside the urban core."""
        return seg_id in self.city_seg_ids

    def set_buildings(self, buildings: list[Building]) -> None:
        """Attach residential buildings and precompute fast OD distance buckets + cross-city gate pairs."""
        self.buildings = list(buildings)
        self.building_by_id = {b.id: b for b in self.buildings}
        self.city_buildings = [b for b in self.buildings if b.zone == "city"]
        self.outer_buildings = [b for b in self.buildings if b.zone == "outer"]
        if not self.buildings:
            return

        # Precompute node-to-node shortest road distances once (~0.04 s on Iași)
        self._node_dist_m = dict(nx.all_pairs_dijkstra_path_length(self.nx, weight="length"))
        # Build candidate destination buckets per origin node (with comfortable slack for time-vs-length paths)
        c_min = max(0.0, self.cfg.city_trip_min_m - 600.0)
        c_max = self.cfg.city_trip_max_m + 400.0
        o_min = max(0.0, self.cfg.outer_trip_min_m - 800.0)
        o_max = self.cfg.outer_trip_max_m + 600.0

        self._city_dests_by_node = {}
        self._outer_dests_by_node = {}
        for nid in range(len(self.nodes)):
            dmap = self._node_dist_m.get(nid, {})
            if not dmap:
                continue
            self._city_dests_by_node[nid] = [
                b for b in self.city_buildings
                if c_min <= dmap.get(self.segments[b.seg_id].from_node, -1.0) <= c_max
            ]
            self._outer_dests_by_node[nid] = [
                b for b in self.city_buildings
                if o_min <= dmap.get(self.segments[b.seg_id].from_node, -1.0) <= o_max
            ]

        # Precompute valid cross-city through-traffic gate pairs (enter one side, cross city, exit far side)
        cx, cy = self.city_center_xy
        gate_ang = {g: math.atan2(self.nodes[g].y - cy, self.nodes[g].x - cx) for g in self.gates}
        min_rad = math.radians(self.cfg.through_min_angle_deg)
        pairs: list[tuple[int, int, list[int], float]] = []
        for g1 in self.gates:
            for g2 in self.gates:
                if g1 == g2:
                    continue
                if abs(wrap(gate_ang[g2] - gate_ang[g1])) < min_rad:
                    continue
                path = self.path_between(g1, g2)
                if not path:
                    continue
                total_len = sum(self.segments[s].length for s in path)
                if not (self.cfg.outer_trip_min_m <= total_len <= self.cfg.outer_trip_max_m):
                    continue
                city_len = sum(self.segments[s].length for s in path if s in self.city_seg_ids)
                if total_len > 0 and city_len / total_len >= 0.25:
                    pairs.append((g1, g2, path, total_len))
        self.through_gate_pairs = pairs

    def route_length_m(self, route: list[int], start_pos: float = 0.0, dest_pos: float | None = None) -> float:
        """Exact driving distance in metres along `route` from `start_pos` on leg 0 to `dest_pos` (or end)."""
        if not route:
            return 0.0
        if len(route) == 1:
            end = self.segments[route[0]].length if dest_pos is None else dest_pos
            return max(0.0, end - start_pos)
        total = max(0.0, self.segments[route[0]].length - start_pos)
        for sid in route[1:-1]:
            total += self.segments[sid].length
        total += self.segments[route[-1]].length if dest_pos is None else dest_pos
        return total

    def building_to_building_trip(
        self,
        origin: Building,
        rng: random.Random,
        min_m: float,
        max_m: float,
        dest_zone: str = "city",
    ) -> tuple[list[int], Building, float] | None:
        """Build a contiguous route from `origin` building to a random building strictly within `[min_m, max_m]`."""
        orig_seg = self.segments[origin.seg_id]
        bucket = (
            self._city_dests_by_node.get(orig_seg.to_node, [])
            if origin.zone == "city" and max_m <= self.cfg.city_trip_max_m + 1.0
            else self._outer_dests_by_node.get(orig_seg.to_node, [])
        )
        if not bucket:
            bucket = self.city_buildings if dest_zone == "city" else self.buildings
        if not bucket:
            return None
        n_b = len(bucket)
        for _ in range(28):
            dest = bucket[rng.randrange(n_b)]
            if dest.id == origin.id or dest.seg_id == origin.seg_id:
                continue
            dest_seg = self.segments[dest.seg_id]
            if orig_seg.to_node == dest_seg.from_node:
                mid: list[int] = []
            else:
                path = self.path_between(orig_seg.to_node, dest_seg.from_node)
                if not path:
                    continue
                mid = path
            if dest.seg_id in mid or origin.seg_id in mid:
                continue
            route = [origin.seg_id, *mid, dest.seg_id]
            dist_m = self.route_length_m(route, start_pos=origin.pos_m, dest_pos=dest.pos_m)
            if min_m <= dist_m <= max_m:
                return route, dest, dist_m
        return None

    def gate_to_building_trip(
        self,
        gate: int,
        rng: random.Random,
        min_m: float,
        max_m: float,
    ) -> tuple[list[int], Building, float] | None:
        """Build a contiguous route from perimeter `gate` to a city building strictly within `[min_m, max_m]`."""
        bucket = self._outer_dests_by_node.get(gate) or self.city_buildings
        if not bucket:
            return None
        n_b = len(bucket)
        for _ in range(28):
            dest = bucket[rng.randrange(n_b)]
            dest_seg = self.segments[dest.seg_id]
            if gate == dest_seg.from_node:
                mid: list[int] = []
            else:
                path = self.path_between(gate, dest_seg.from_node)
                if not path:
                    continue
                mid = path
            if dest.seg_id in mid:
                continue
            route = [*mid, dest.seg_id]
            dist_m = self.route_length_m(route, start_pos=0.0, dest_pos=dest.pos_m)
            if min_m <= dist_m <= max_m:
                return route, dest, dist_m
        return None

    def through_city_trip(self, rng: random.Random, gate: int | None = None) -> tuple[list[int], float] | None:
        """Pick an outside -> far outside cross-city route (5-20 km, passing through the city)."""
        if not self.through_gate_pairs:
            return None
        if gate is None:
            _, _, path, dist_m = rng.choice(self.through_gate_pairs)
            return list(path), dist_m
        matching = [p for p in self.through_gate_pairs if p[0] == gate]
        if not matching:
            return None
        _, _, path, dist_m = rng.choice(matching)
        return list(path), dist_m

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

    def lane_lateral_offset(self, seg: Segment, lane: float = 0.0) -> float:
        """Lateral offset (m to the right of centerline) for `lane` (0 = outer/curb, n-1 = inner/left)."""
        n = seg.lanes
        if n <= 1:
            return self.cfg.lane_offset
        w = self.cfg.lane_width
        if seg.oneway:
            return ((n - 1) * 0.5 - lane) * w
        # Two-way multi-lane boulevard: inner lane (n-1) hugs the center median, outer lane (0) hugs the right curb
        return 1.9 + (n - 1 - lane) * w

    def position(self, seg_id: int, pos: float, lane: float = 0.0) -> tuple[float, float, float, float]:
        """Like `point_at`, shifted to the right-hand lane `lane` (0 = curb, n-1 = inner): (x, y, dx, dy)."""
        seg = self.segments[seg_id]
        x, y, dx, dy = self.point_at(seg_id, pos)
        off = self.lane_lateral_offset(seg, lane)
        return x + dy * off, y - dx * off, dx, dy
