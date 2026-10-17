"""City topology: intersections with junction boxes, joined by directed road segments.

Every road is a pair of one-lane directed segments running centre to centre. A vehicle
must stop at the *stop line*, `box_half` metres before the centre of the intersection it
is approaching; the square between the four stop lines is the junction box.

Each intersection has one *approach* per street that leads into it, and the signal gives
green to one approach at a time (3 approaches -> 3 phases, 4 -> 4, ...).

The west and east edge nodes also have an external gate road (entry and exit), so the
city has 4-way junctions (interior, west/east edge) and 3-way junctions (top/bottom edge).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import IntEnum

from .config import Config

STRAIGHT_SHARE, TURN_SHARE = 0.6, 0.2


class Dir(IntEnum):
    """Direction of travel. Screen coordinates: y grows downward."""

    N = 0
    E = 1
    S = 2
    W = 3

    @property
    def vector(self) -> tuple[int, int]:
        return ((0, -1), (1, 0), (0, 1), (-1, 0))[self]

    @property
    def opposite(self) -> Dir:
        return Dir((self + 2) % 4)

    def left(self) -> Dir:
        return Dir((self - 1) % 4)

    def right(self) -> Dir:
        return Dir((self + 1) % 4)


@dataclass(frozen=True)
class Intersection:
    id: int
    row: int
    col: int
    x: float
    y: float


@dataclass(frozen=True)
class Segment:
    id: int
    from_node: int
    to_node: int
    length: float
    direction: Dir
    kind: str  # "road" between intersections, "entry" from a gate, "exit" to a gate
    stop_pos: float  # m from the segment start at which vehicles must stop on red

    @property
    def signalised(self) -> bool:
        return self.kind != "exit"  # an exit leads off the map: no stop line

    @property
    def from_side(self) -> Dir:
        """Which side of the downstream intersection this approach arrives from."""
        return self.direction.opposite


class CityGrid:
    MARGIN = 120.0  # m of empty space around the grid in world coordinates

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rows, self.cols = cfg.rows, cfg.cols
        L = cfg.block_length
        self.nodes: list[Intersection] = [
            Intersection(r * cfg.cols + c, r, c, self.MARGIN + c * L, self.MARGIN + r * L)
            for r in range(cfg.rows)
            for c in range(cfg.cols)
        ]
        self.n_internal = len(self.nodes)
        self.coords: list[tuple[float, float]] = [(n.x, n.y) for n in self.nodes]
        self.segments: list[Segment] = []
        # out_seg[node][dir] / in_seg[node][dir of travel] -> segment id or None (internal nodes only)
        self.out_seg: list[list[int | None]] = [[None] * 4 for _ in self.nodes]
        self.in_seg: list[list[int | None]] = [[None] * 4 for _ in self.nodes]

        def add(a: int, b: int, length: float, d: Dir, kind: str) -> None:
            stop = length - cfg.box_half if kind != "exit" else length
            seg = Segment(len(self.segments), a, b, length, d, kind, stop)
            self.segments.append(seg)
            if a < self.n_internal:
                self.out_seg[a][d] = seg.id
            if b < self.n_internal:
                self.in_seg[b][d] = seg.id

        for node in self.nodes:
            for d in Dir:
                dx, dy = d.vector
                r, c = node.row + dy, node.col + dx
                if 0 <= r < cfg.rows and 0 <= c < cfg.cols:
                    add(node.id, r * cfg.cols + c, L, d, "road")

        self.entries: list[int] = []  # segment ids where cars appear
        self.exits: list[int] = []  # segment ids where cars leave
        for r in range(cfg.rows):
            for col, side in ((0, Dir.W), (cfg.cols - 1, Dir.E)):
                node = self.nodes[r * cfg.cols + col]
                dx, _ = side.vector
                gate = len(self.coords)
                self.coords.append((node.x + dx * cfg.gate_length, node.y))
                self.entries.append(len(self.segments))
                add(gate, node.id, cfg.gate_length, side.opposite, "entry")
                self.exits.append(len(self.segments))
                add(node.id, gate, cfg.gate_length, side, "exit")

        self._between = {(s.from_node, s.to_node): s.id for s in self.segments}
        self._turns = {s.id: self._build_turns(s) for s in self.segments}

    # -- topology lookups -------------------------------------------------
    @property
    def width(self) -> float:
        return 2 * self.MARGIN + (self.cols - 1) * self.cfg.block_length

    @property
    def height(self) -> float:
        return 2 * self.MARGIN + (self.rows - 1) * self.cfg.block_length

    def segment_between(self, a: int, b: int) -> int:
        return self._between[(a, b)]

    def approaches(self, node: int) -> list[Segment]:
        """Incoming segments at an intersection, ordered by the side they arrive from (N, E, S, W)."""
        found = [self.segments[sid] for sid in self.in_seg[node] if sid is not None]
        return sorted(found, key=lambda s: s.from_side)

    def turn_targets(self, seg_id: int) -> list[tuple[int, float]]:
        """Where vehicles leaving this segment can go (never a U-turn), with their share."""
        return self._turns[seg_id]

    def straight_next(self, seg_id: int) -> int | None:
        seg = self.segments[seg_id]
        if not seg.signalised:
            return None
        return self.out_seg[seg.to_node][seg.direction]

    def neighbours(self, node: int) -> set[int]:
        return {
            self.segments[s].to_node for s in self.out_seg[node] if s is not None and self.segments[s].kind == "road"
        }

    def _build_turns(self, seg: Segment) -> list[tuple[int, float]]:
        if not seg.signalised:
            return []
        options = (
            (seg.direction, STRAIGHT_SHARE),
            (seg.direction.left(), TURN_SHARE),
            (seg.direction.right(), TURN_SHARE),
        )
        found = [(t, share) for d, share in options if (t := self.out_seg[seg.to_node][d]) is not None]
        total = sum(share for _, share in found)
        return [(t, share / total) for t, share in found]

    # -- routing for cars --------------------------------------------------------
    def choose_next(self, seg_id: int, goal_exit: int, rng: random.Random, follow: float) -> int | None:
        """Pick the segment a car takes after the intersection ahead, steering toward its exit."""
        options = [(t, s) for t, s in self._turns[seg_id] if self.segments[t].kind != "exit" or t == goal_exit]
        if not options:
            options = self._turns[seg_id]
        if not options:
            return None
        if rng.random() < follow:
            g = self.nodes[self.segments[goal_exit].from_node]

            def remaining(t: int) -> int:
                if t == goal_exit:
                    return 0
                n = self.nodes[self.segments[t].to_node]
                return abs(n.row - g.row) + abs(n.col - g.col) + 1

            best = min(remaining(t) for t, _ in options)
            options = [(t, s) for t, s in options if remaining(t) == best]
        return rng.choices([t for t, _ in options], weights=[s for _, s in options])[0]

    # -- geometry ---------------------------------------------------------
    def position(self, seg_id: int, pos: float) -> tuple[float, float]:
        """World coordinates of a point `pos` metres along a segment (right-hand lane)."""
        seg = self.segments[seg_id]
        (ax, ay), (bx, by) = self.coords[seg.from_node], self.coords[seg.to_node]
        f = pos / seg.length
        x, y = ax + (bx - ax) * f, ay + (by - ay) * f
        dx, dy = seg.direction.vector
        off = self.cfg.lane_offset
        return x - dy * off, y + dx * off  # perpendicular to the right of travel
