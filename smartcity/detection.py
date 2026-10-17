"""Virtual detection loops: SUMO-style detectors at fixed distances before every stop line.

There are no physical sensors in this city. Each approach gets virtual detection windows
(by default 50 m, 150 m and 300 m before its stop line) and they are fed by bus telemetry
only. A window reaches back across junctions: a bus that is still on the previous street counts
toward the next approach when the road distance to its stop line is within the window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .mock_api import BusTelemetry
from .network import RoadNetwork

WINDOW_WEIGHTS = (0.5, 0.3, 0.2)  # nearer windows say more about conditions at the line
STANDING = 0.3  # a bus slower than this share of the limit is standing in a queue, not just slowed


@dataclass
class LoopReading:
    """What the windows of one approach currently see."""

    count: list[int] = field(default_factory=lambda: [0, 0, 0])  # buses of any speed per window
    min_ratio: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    moving: list[int] = field(default_factory=lambda: [0, 0, 0])  # buses still moving per window
    sum_ratio: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # their speed ratios
    queue_occupancy: float = 0.0  # how much of the approach edge a standing queue covers
    slowest: BusTelemetry | None = None  # slowest bus inside the middle window
    slowest_ratio: float = 1.0
    delay_score: float = 0.0  # slowness of buses in the outer window, weighted by proximity

    def mean_ratio(self, i: int) -> float | None:
        return self.sum_ratio[i] / self.moving[i] if self.moving[i] else None

    @property
    def density(self) -> float | None:
        """Congestion estimate 0..1, or None if no window contains a bus.

        A standing bus proves a queue reaching back at least to where it stands, so it sets a floor
        on how full the approach edge is; buses that are still moving contribute their slowdown.
        """
        if not any(self.count):
            return None
        total = weight = 0.0
        for i, w in enumerate(WINDOW_WEIGHTS):
            mean = self.mean_ratio(i)
            if mean is not None:
                total += w * (1.0 - mean)
                weight += w
        slowed = total / weight if weight else 0.0
        return max(self.queue_occupancy, slowed)


class DetectionLoops:
    def __init__(self, net: RoadNetwork, cfg: Config):
        self.net, self.cfg = net, cfg
        self.windows = cfg.loop_windows
        self.reach = max(self.windows)
        # feeds[e]: approaches that a bus on edge e counts toward, with the extra road distance
        self.feeds: list[list[tuple[int, float]]] = [[] for _ in net.segments]
        for s in net.segments:
            self._index_upstream(s.id)
        self.latest: dict[str, BusTelemetry] = {}
        self.readings: dict[int, LoopReading] = {}

    def _index_upstream(self, target: int) -> None:
        net = self.net
        self.feeds[target].append((target, 0.0))
        frontier = [(target, 0.0)]
        while frontier:
            e_id, base = frontier.pop()
            e = net.segments[e_id]
            for p_id in net.in_segs[e.from_node]:
                p = net.segments[p_id]
                if p.from_node == e.to_node or e_id not in {t for t, _ in net.turn_targets(p_id)}:
                    continue  # a U-turn, or a street that cannot turn into e
                p_base = base + (p.length - p.stop_pos) + e.stop_pos
                if p_base < self.reach:
                    self.feeds[p_id].append((target, p_base))
                    frontier.append((p_id, p_base))

    def observe(self, tel: BusTelemetry) -> None:
        self.latest[tel.bus_id] = tel

    def refresh(self) -> None:
        """Recompute every window from the latest position of each bus.

        A bus standing within `est_skip_end` of its own stop line is either waiting for its signal or
        pulling away from it, not reporting congestion, so (like the estimator) the loops ignore it.
        """
        v_slow = self.cfg.v_slow_frac
        skip = self.cfg.est_skip_end
        w0, w1, w2 = self.windows
        readings: dict[int, LoopReading] = {}
        for tel in self.latest.values():
            ratio = max(0.0, min(1.0, tel.speed / tel.free_speed))
            if ratio < STANDING and tel.dist_to_next_node < skip:
                continue
            for target, base in self.feeds[tel.segment_id]:
                dist = tel.dist_to_next_node + base
                if dist > w2:
                    continue
                r = readings.get(target)
                if r is None:
                    r = readings[target] = LoopReading()
                standing = ratio < STANDING
                for i, limit in enumerate((w0, w1, w2)):
                    if dist <= limit:
                        r.count[i] += 1
                        r.min_ratio[i] = min(r.min_ratio[i], ratio)
                        if not standing:
                            r.moving[i] += 1
                            r.sum_ratio[i] += ratio
                if standing:  # a queue at least this long (it covers the whole edge if the bus is upstream)
                    seg = self.net.segments[target]
                    occ = min(1.0, (dist + self.cfg.bus_length) / max(1.0, seg.stop_pos)) if base == 0.0 else 1.0
                    r.queue_occupancy = max(r.queue_occupancy, occ)
                if dist <= w1 and ratio < v_slow and ratio < r.slowest_ratio:
                    r.slowest, r.slowest_ratio = tel, ratio
                r.delay_score = max(r.delay_score, (1.0 - ratio) * (1.0 - 0.5 * dist / w2))
        self.readings = readings

    def reading(self, seg_id: int) -> LoopReading | None:
        return self.readings.get(seg_id)
