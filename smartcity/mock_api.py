"""Custom mock transit API: a fleet of buses broadcasting high-frequency telemetry.

No network is involved; `TelemetryBus` is an in-process pub/sub with a bounded ring
buffer. It stands in for a cheap, high-rate AVL (automatic vehicle location) feed.
Every bus runs strictly along the edge sequence of its public line.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
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
        self._cum: dict[str, tuple[list[float], float]] = {}  # per line: where each edge starts along the loop, total
        self._next_id = 0
        for line in net.routes:
            lengths = [net.segments[s].length for s in line.edges]
            total = sum(lengths)
            starts = [sum(lengths[:i]) for i in range(len(lengths))]
            self._cum[line.ref] = (starts, total)
            count = self.base_count(line.backbone, total)
            self._base[line.ref] = count
            for k in range(count):
                # Staggered departures: the buses of one line start evenly spaced around its loop (equal headways)
                # with a little jitter, so none ever starts on top of another (or on a bus of a crossing line).
                self._place(line.ref, (k / count) * total + rng.uniform(0, 40))

    def base_count(self, backbone: bool, loop_length: float) -> int:
        """Buses on a line at 100% traffic: 4 on a standard line, 6 to 8 on a backbone line (more on longer ones)."""
        cfg = self.cfg
        if not backbone:
            return cfg.buses_per_line
        wanted = round(loop_length / cfg.backbone_headway_m)
        return max(cfg.backbone_buses_min, min(cfg.backbone_buses_max, wanted))

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

    # -- keeping headways even ---------------------------------------------------
    _NUDGES = (0.0, 9.0, -9.0, 18.0, -18.0, 27.0, -27.0, 40.0, -40.0, 60.0, -60.0, 90.0, -90.0, 140.0, -140.0)

    def _place(self, ref: str, offset: float) -> bool:
        """Put a new bus of this line at `offset` m along its loop, or the closest free spot if that one is taken."""
        route = self.net.route_by_ref[ref]
        for nudge in self._NUDGES:
            leg, pos = self._locate(ref, offset + nudge)
            if self.world.try_place_bus(f"B{self._next_id:02d}", ref, route.edges, leg, pos):
                self._next_id += 1
                return True
        return False

    def _locate(self, ref: str, offset: float) -> tuple[int, float]:
        """Edge index and position along it for a distance along the line's loop."""
        starts, total = self._cum[ref]
        offset %= total
        leg = bisect_right(starts, offset) - 1
        return leg, offset - starts[leg]

    def _offset(self, bus: Vehicle) -> float:
        return self._cum[bus.line or ""][0][bus.leg] + bus.pos

    def _gap_midpoints(self, ref: str) -> list[float]:
        """Loop positions in the middle of each gap between neighbouring buses, widest gap first."""
        total = self._cum[ref][1]
        offs = sorted(self._offset(b) for b in self.world.buses if b.line == ref)
        if not offs:
            return [0.0]
        gaps = [(offs[(i + 1) % len(offs)] - offs[i]) % total or total for i in range(len(offs))]
        order = sorted(range(len(offs)), key=lambda i: -gaps[i])
        return [(offs[i] + gaps[i] / 2) % total for i in order]

    def _victim(self, ref: str) -> Vehicle:
        """The bus whose removal leaves the most even spacing (the one between the two narrowest gaps)."""
        total = self._cum[ref][1]
        mine = sorted((b for b in self.world.buses if b.line == ref), key=self._offset)
        if len(mine) < 3:
            return mine[-1]
        offs = [self._offset(b) for b in mine]
        merged = [(offs[(i + 1) % len(offs)] - offs[i - 1]) % total for i in range(len(mine))]
        return mine[min(range(len(mine)), key=lambda i: (merged[i], -int(mine[i].id[1:])))]

    def set_percent(self, percent: float) -> None:
        """Add buses into the widest gaps of their lines, or take buses off so the rest stay evenly spaced."""
        for ref, want in self.targets(percent).items():
            mine = [b for b in self.world.buses if b.line == ref]
            for _ in range(max(0, len(mine) - want)):
                bus = self._victim(ref)
                self.world.remove_vehicle(bus)
                for forget in self.on_remove:
                    forget(bus.id)
            for _ in range(max(0, want - len(mine))):
                if not any(self._place(ref, mid) for mid in self._gap_midpoints(ref)):
                    break  # no room anywhere on the line right now

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
