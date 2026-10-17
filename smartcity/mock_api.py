"""Custom mock transit API: a fleet of buses broadcasting high-frequency telemetry.

No network is involved; `TelemetryBus` is an in-process pub/sub with a bounded ring
buffer. It stands in for a cheap, high-rate AVL (automatic vehicle location) feed.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .network import CityGrid


@dataclass(frozen=True)
class BusTelemetry:
    bus_id: str
    t: float
    x: float
    y: float
    segment_id: int
    dist_to_next_node: float  # m to the next stop line
    speed: float  # m/s
    free_speed: float  # m/s
    delay: float  # accumulated seconds beyond the free-flow schedule

    def format(self) -> str:
        return (
            f"{self.bus_id} ({self.x:5.0f},{self.y:5.0f}) seg{self.segment_id:<2d} "
            f"d={self.dist_to_next_node:5.1f}m v={self.speed:4.1f}m/s delay={self.delay:5.1f}s"
        )


class TelemetryBus:
    """Pub/sub for telemetry records, with a bounded history for the HUD."""

    def __init__(self, history: int = 512):
        self._subscribers: list[Callable[[BusTelemetry], None]] = []
        self.history: deque[BusTelemetry] = deque(maxlen=history)
        self.published = 0

    def subscribe(self, callback: Callable[[BusTelemetry], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, record: BusTelemetry) -> None:
        self.history.append(record)
        self.published += 1
        for callback in self._subscribers:
            callback(record)

    def recent(self, n: int) -> list[BusTelemetry]:
        return list(self.history)[-n:]


@dataclass
class Bus:
    bus_id: str
    route: list[int]  # cyclic list of segment ids
    leg: int  # index into route
    pos: float  # m along the current segment
    v: float = 0.0
    delay: float = 0.0
    elapsed: float = 0.0
    laps: int = 0

    @property
    def segment_id(self) -> int:
        return self.route[self.leg]


def _rect_path(grid: CityGrid, r0: int, c0: int, r1: int, c1: int, clockwise: bool) -> list[int]:
    n = grid.cfg.cols
    nodes = (
        [r0 * n + c for c in range(c0, c1)]
        + [r * n + c1 for r in range(r0, r1)]
        + [r1 * n + c for c in range(c1, c0, -1)]
        + [r * n + c0 for r in range(r1, r0, -1)]
    )
    return nodes if clockwise else [nodes[0]] + nodes[:0:-1]


def _line_path(grid: CityGrid, a: tuple[int, int], b: tuple[int, int]) -> list[int]:
    """Out-and-back along a row or column; the turnaround uses the opposite carriageway."""
    n = grid.cfg.cols
    (r0, c0), (r1, c1) = a, b
    steps = max(abs(r1 - r0), abs(c1 - c0))
    dr = (r1 > r0) - (r1 < r0)
    dc = (c1 > c0) - (c1 < c0)
    out = [(r0 + dr * i) * n + (c0 + dc * i) for i in range(steps + 1)]
    return out + out[-2:0:-1]


def default_routes(grid: CityGrid) -> list[list[int]]:
    """Node cycles covering the perimeter, inner ring, rows, columns and diagonals-ish loops."""
    R, C = grid.rows - 1, grid.cols - 1
    node_cycles = [
        _rect_path(grid, 0, 0, R, C, True),
        _rect_path(grid, 0, 0, R, C, False),
        _rect_path(grid, 1, 1, R - 1, C - 1, True),
        _rect_path(grid, 1, 1, R - 1, C - 1, False),
        _line_path(grid, (1, 0), (1, C)),
        _line_path(grid, (R - 1, 0), (R - 1, C)),
        _line_path(grid, (0, 1), (R, 1)),
        _line_path(grid, (0, C - 1), (R, C - 1)),
        _rect_path(grid, 0, 1, R, C - 1, True),
    ]
    routes = []
    for cycle in node_cycles:
        routes.append([grid.segment_between(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))])
    return routes


class BusFleet:
    """Simulates the buses and publishes one telemetry record per bus per tick."""

    def __init__(self, grid: CityGrid, cfg: Config, rng: random.Random, bus: TelemetryBus):
        self.grid, self.cfg, self.api = grid, cfg, bus
        self.buses: list[Bus] = []
        routes = default_routes(grid)
        total_len = [len(r) * cfg.block_length for r in routes]
        n = 0
        for route, length in zip(routes, total_len, strict=True):
            for k in range(cfg.buses_per_route):
                # Spread the buses of one route evenly around the loop, with a little jitter.
                offset = (k / cfg.buses_per_route) * length + rng.uniform(0, 40)
                leg, pos = int(offset // cfg.block_length) % len(route), offset % cfg.block_length
                self.buses.append(Bus(f"B{n:02d}", route, leg, pos))
                n += 1

    def step(
        self,
        now: float,
        dt: float,
        signal_state: Callable[[int], str],
        segment_speed: Callable[[int], float],
    ) -> list[BusTelemetry]:
        """Move every bus one tick. `signal_state(seg)` returns 'G', 'Y' or 'R' for that approach."""
        cfg, grid = self.cfg, self.grid
        by_segment: dict[int, list[Bus]] = {}
        for b in self.buses:
            by_segment.setdefault(b.segment_id, []).append(b)

        for seg_id, buses in by_segment.items():
            seg = grid.segments[seg_id]
            state = signal_state(seg_id)
            v_flow = segment_speed(seg_id)
            buses.sort(key=lambda b: -b.pos)  # leader first
            leader_pos: float | None = None
            for b in buses:
                d_stop = seg.length - b.pos
                brake_dist = b.v * b.v / (2 * cfg.bus_brake)
                stop_at_line = state == "R" or (state == "Y" and brake_dist <= d_stop)
                limit = seg.length - 0.5 if stop_at_line else math.inf
                if leader_pos is not None:
                    limit = min(limit, leader_pos - cfg.bus_gap)
                room = max(0.0, limit - b.pos)
                v_brake = math.sqrt(2 * cfg.bus_brake * room) if math.isfinite(room) else math.inf
                v_des = min(v_flow, v_brake)
                b.v += max(-cfg.bus_brake * dt, min(cfg.bus_accel * dt, v_des - b.v))
                b.v = max(0.0, b.v)
                b.pos = min(b.pos + b.v * dt, limit) if math.isfinite(limit) else b.pos + b.v * dt
                if b.pos >= limit and math.isfinite(limit):
                    b.v = 0.0
                leader_pos = b.pos
                b.delay += dt * (1.0 - min(1.0, b.v / cfg.free_speed))
                b.elapsed += dt

        out: list[BusTelemetry] = []
        for b in self.buses:
            seg = grid.segments[b.segment_id]
            if b.pos >= seg.length:  # crossed the stop line on green
                b.pos -= seg.length
                b.leg = (b.leg + 1) % len(b.route)
                if b.leg == 0:
                    b.laps += 1
                seg = grid.segments[b.segment_id]
            x, y = grid.position(seg.id, b.pos)
            rec = BusTelemetry(b.bus_id, now, x, y, seg.id, seg.length - b.pos, b.v, cfg.free_speed, b.delay)
            self.api.publish(rec)
            out.append(rec)
        return out
