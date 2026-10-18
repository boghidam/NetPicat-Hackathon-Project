"""Custom mock transit API: a fleet of buses broadcasting high-frequency telemetry.

No network is involved; `TelemetryBus` is an in-process pub/sub with a bounded ring
buffer. It stands in for a cheap, high-rate AVL (automatic vehicle location) feed.
Every bus runs strictly along the edge sequence of its public line.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .network import RoadNetwork
from .traffic_model import TrafficModel, Vehicle


@dataclass(frozen=True)
class BusTelemetry:
    bus_id: str
    t: float
    x: float  # metres east of the map centre
    y: float  # metres north of the map centre
    segment_id: int
    dist_to_next_node: float  # m to the stop line of the next intersection
    speed: float  # m/s
    free_speed: float  # m/s: the speed limit of the current edge
    delay: float  # accumulated seconds beyond the free-flow schedule
    line: str = ""  # public line number, e.g. "28"
    leg: int = 0  # position along the line's edge list

    def format(self) -> str:
        return (
            f"{self.bus_id} L{self.line:<3s} seg{self.segment_id:<4d} d={self.dist_to_next_node:5.0f}m "
            f"v={self.speed * 3.6:4.0f}km/h delay={self.delay:5.0f}s"
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


class BusFleet:
    """The transit fleet. Buses live in the traffic model; this class publishes their telemetry."""

    def __init__(self, net: RoadNetwork, cfg: Config, rng: random.Random, world: TrafficModel, bus: TelemetryBus):
        self.net, self.cfg, self.world, self.api = net, cfg, world, bus
        self.rng = rng
        self.on_remove: list[Callable[[str], None]] = []  # called with the id of every bus taken off the map
        self._base: dict[str, int] = {}  # buses per line at 100% traffic
        n = 0
        for line in net.routes:
            lengths = [net.segments[s].length for s in line.edges]
            total = sum(lengths)
            count = max(2, min(cfg.max_buses_per_line, round(total / cfg.bus_headway_m)))
            self._base[line.ref] = count
            for k in range(count):
                # Spread the buses of one line evenly along its loop, with a little jitter.
                offset = (k / count) * total + rng.uniform(0, 40)
                leg, pos = 0, offset
                while pos >= lengths[leg]:
                    pos -= lengths[leg]
                    leg = (leg + 1) % len(lengths)
                world.add_bus(f"B{n:02d}", line.ref, line.edges, leg, pos)
                n += 1
        self._next_id = n

    @property
    def buses(self) -> list[Vehicle]:
        return self.world.buses

    def targets(self, percent: float) -> dict[str, int]:
        """Buses per line at this traffic amount: 100% is the normal fleet, 0% is none, 1000% is ten times as many."""
        scale = percent / 100.0
        raw = {ref: n * scale for ref, n in self._base.items()}
        counts = {ref: int(v) for ref, v in raw.items()}
        extra = round(sum(self._base.values()) * scale) - sum(counts.values())
        for ref in sorted(raw, key=lambda r: raw[r] - counts[r], reverse=True)[: max(0, extra)]:
            counts[ref] += 1  # largest remainder, so the total is exact
        return counts

    def set_percent(self, percent: float) -> None:
        """Add buses along their lines or take the most recently added ones off the map."""
        for ref, want in self.targets(percent).items():
            route = self.net.route_by_ref[ref]
            mine = sorted((b for b in self.world.buses if b.line == ref), key=lambda b: b.id)
            for bus in reversed(mine[want:]):
                self.world.remove_vehicle(bus)
                for forget in self.on_remove:
                    forget(bus.id)
            lengths = [self.net.segments[e].length for e in route.edges]
            total = sum(lengths)
            for _ in range(max(0, want - len(mine))):
                for _attempt in range(60):  # a spot along the line that is free right now
                    leg, pos = 0, self.rng.uniform(0.0, total)
                    while pos >= lengths[leg]:
                        pos -= lengths[leg]
                        leg += 1
                    if self.world.try_place_bus(f"B{self._next_id:02d}", ref, route.edges, leg, pos):
                        self._next_id += 1
                        break

    def emit(self, now: float) -> list[BusTelemetry]:
        """Publish one telemetry record per bus (a high-rate AVL feed)."""
        out: list[BusTelemetry] = []
        for b in self.world.buses:
            seg = self.net.segments[b.seg]
            x, y, _, _ = self.net.position(seg.id, b.pos)
            rec = BusTelemetry(
                b.id, now, x, y, seg.id, min(seg.stop_pos, max(0.0, seg.stop_pos - b.pos)), b.v, seg.speed_limit, b.delay,
                line=b.line or "", leg=b.leg,
            )  # fmt: skip
            self.api.publish(rec)
            out.append(rec)
        return out


def route_length(net: RoadNetwork, edges: list[int]) -> float:
    return math.fsum(net.segments[s].length for s in edges)
