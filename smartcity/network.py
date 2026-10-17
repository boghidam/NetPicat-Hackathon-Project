"""City topology: intersections joined by directed road segments."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .config import Config


class Axis(IntEnum):
    NS = 0
    EW = 1

    @property
    def other(self) -> Axis:
        return Axis.EW if self is Axis.NS else Axis.NS


class Dir(IntEnum):
    """Direction of travel. Screen coordinates: y grows downward."""

    N = 0
    E = 1
    S = 2
    W = 3

    @property
    def axis(self) -> Axis:
        return Axis.NS if self in (Dir.N, Dir.S) else Axis.EW

    @property
    def vector(self) -> tuple[int, int]:
        return ((0, -1), (1, 0), (0, 1), (-1, 0))[self]

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
    capacity: float  # vehicles the link holds at jam density
    direction: Dir

    @property
    def axis(self) -> Axis:
        return self.direction.axis


class CityGrid:
    """Rectangular grid; every adjacent node pair is joined by two directed segments."""

    MARGIN = 100.0  # m of empty space around the grid in world coordinates

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rows, self.cols = cfg.rows, cfg.cols
        L = cfg.block_length
        self.nodes: list[Intersection] = [
            Intersection(r * cfg.cols + c, r, c, self.MARGIN + c * L, self.MARGIN + r * L)
            for r in range(cfg.rows)
            for c in range(cfg.cols)
        ]
        self.segments: list[Segment] = []
        # out_seg[node][dir] / in_seg[node][dir of travel] -> segment id or None
        self.out_seg: list[list[int | None]] = [[None] * 4 for _ in self.nodes]
        self.in_seg: list[list[int | None]] = [[None] * 4 for _ in self.nodes]
        for node in self.nodes:
            for d in Dir:
                dx, dy = d.vector
                r, c = node.row + dy, node.col + dx
                if 0 <= r < cfg.rows and 0 <= c < cfg.cols:
                    to = r * cfg.cols + c
                    seg = Segment(len(self.segments), node.id, to, L, cfg.jam_density * L, d)
                    self.segments.append(seg)
                    self.out_seg[node.id][d] = seg.id
                    self.in_seg[to][d] = seg.id
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

    def approaches(self, node: int, axis: Axis) -> list[Segment]:
        """Incoming segments that stop at this node on the given axis."""
        return [self.segments[sid] for d in Dir if d.axis is axis and (sid := self.in_seg[node][d]) is not None]

    def turn_targets(self, seg_id: int) -> list[tuple[int | None, float]]:
        """Where vehicles leaving this segment go: (segment id or None=exit, share)."""
        return self._turns[seg_id]

    def straight_next(self, seg_id: int) -> int | None:
        seg = self.segments[seg_id]
        return self.out_seg[seg.to_node][seg.direction]

    def _build_turns(self, seg: Segment) -> list[tuple[int | None, float]]:
        cfg = self.cfg
        options = (
            (seg.direction, cfg.turn_straight),
            (seg.direction.left(), cfg.turn_left),
            (seg.direction.right(), cfg.turn_right),
        )
        turns: list[tuple[int | None, float]] = []
        exit_share = 0.0
        for d, share in options:
            target = self.out_seg[seg.to_node][d]
            if target is None:
                exit_share += share
            else:
                turns.append((target, share))
        if exit_share > 0:
            turns.append((None, exit_share))
        return turns

    # -- geometry ---------------------------------------------------------
    def position(self, seg_id: int, pos: float) -> tuple[float, float]:
        """World coordinates of a point `pos` metres along a segment (right-hand lane)."""
        seg = self.segments[seg_id]
        a, b = self.nodes[seg.from_node], self.nodes[seg.to_node]
        f = pos / seg.length
        x, y = a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f
        dx, dy = seg.direction.vector
        off = self.cfg.lane_offset
        return x - dy * off, y + dx * off  # perpendicular to the right of travel
