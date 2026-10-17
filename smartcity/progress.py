"""How far each bus has actually travelled along its line, from telemetry alone.

The controllers know nothing about the cars, only about the buses. The best evidence of a
junction holding traffic up is a bus that covered very little distance during a whole signal
cycle, so this module keeps a per-bus odometer along the public route (unwrapped over laps) and a
short history of it, which lets a controller ask "how far did this bus get in the last N seconds?".
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field

from .mock_api import BusTelemetry
from .network import RoadNetwork


@dataclass
class _Track:
    last_leg: int
    laps: int
    odo: float
    times: deque[float] = field(default_factory=lambda: deque(maxlen=300))
    odos: deque[float] = field(default_factory=lambda: deque(maxlen=300))


class RouteProgress:
    SAMPLE_EVERY = 1.0  # s between history samples

    def __init__(self, net: RoadNetwork):
        self.net = net
        self._cum: dict[str, tuple[list[float], float]] = {}
        for route in net.routes:
            starts, total = [], 0.0
            for e in route.edges:
                starts.append(total)
                total += net.segments[e].length
            self._cum[route.ref] = (starts, total)
        self._tracks: dict[str, _Track] = {}

    def on_telemetry(self, tel: BusTelemetry) -> None:
        cum = self._cum.get(tel.line)
        if cum is None:
            return
        starts, total = cum
        seg = self.net.segments[tel.segment_id]
        along = max(0.0, min(seg.length, seg.stop_pos - tel.dist_to_next_node))
        track = self._tracks.get(tel.bus_id)
        laps = track.laps if track else 0
        if track and tel.leg < track.last_leg - len(starts) // 2:  # wrapped around the loop
            laps += 1
        odo = laps * total + starts[tel.leg] + along
        if track is None:
            track = self._tracks[tel.bus_id] = _Track(tel.leg, laps, odo)
        track.last_leg, track.laps, track.odo = tel.leg, laps, max(odo, track.odo)
        if not track.times or tel.t - track.times[-1] >= self.SAMPLE_EVERY:
            track.times.append(tel.t)
            track.odos.append(track.odo)

    def odometer(self, bus_id: str) -> float | None:
        track = self._tracks.get(bus_id)
        return track.odo if track else None

    def travelled(self, bus_id: str, now: float, span: float) -> float | None:
        """Metres the bus covered along its line during the last `span` seconds.

        None when its history does not reach back that far (it cannot be judged yet).
        """
        track = self._tracks.get(bus_id)
        if track is None or not track.times or track.times[0] > now - span:
            return None
        i = bisect_right(track.times, now - span) - 1
        return max(0.0, track.odo - track.odos[i])
