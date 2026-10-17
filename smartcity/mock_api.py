"""Custom mock transit API: a fleet of buses broadcasting high-frequency telemetry.

No network is involved; `TelemetryBus` is an in-process pub/sub with a bounded ring
buffer. It stands in for a cheap, high-rate AVL (automatic vehicle location) feed.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .network import CityGrid
from .traffic_model import TrafficModel, Vehicle


@dataclass(frozen=True)
class BusTelemetry:
    bus_id: str
    t: float
    x: float
    y: float
    segment_id: int
    dist_to_next_node: float  # m to the stop line of the next intersection
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
    """The transit fleet. Buses live in the traffic model; this class publishes their telemetry."""

    def __init__(self, grid: CityGrid, cfg: Config, rng: random.Random, world: TrafficModel, bus: TelemetryBus):
        self.grid, self.cfg, self.world, self.api = grid, cfg, world, bus
        routes = default_routes(grid)
        n = 0
        for route in routes:
            length = len(route) * cfg.block_length
            for k in range(cfg.buses_per_route):
                # Spread the buses of one route evenly around the loop, with a little jitter.
                offset = (k / cfg.buses_per_route) * length + rng.uniform(0, 40)
                leg, pos = int(offset // cfg.block_length) % len(route), offset % cfg.block_length
                world.add_bus(f"B{n:02d}", route, leg, pos)
                n += 1

    @property
    def buses(self) -> list[Vehicle]:
        return self.world.buses

    def emit(self, now: float) -> list[BusTelemetry]:
        """Publish one telemetry record per bus (a high-rate AVL feed)."""
        out: list[BusTelemetry] = []
        for b in self.world.buses:
            seg = self.grid.segments[b.seg]
            x, y = self.grid.position(seg.id, b.pos)
            rec = BusTelemetry(
                b.id, now, x, y, seg.id, max(0.0, seg.stop_pos - b.pos), b.v, self.cfg.free_speed, b.delay
            )
            self.api.publish(rec)
            out.append(rec)
        return out
