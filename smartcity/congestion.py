"""Per-segment congestion estimated *only* from bus telemetry."""

from __future__ import annotations

import math

from .config import Config
from .mock_api import BusTelemetry
from .network import CityGrid


class CongestionEstimator:
    """EWMA of the bus speed ratio on each segment.

    Samples taken while accelerating off a stop line or braking for the next one are
    ignored: they reflect the signal, not the traffic on the link. In normalised units
    the Greenshields inverse is `rho_hat = 1 - v_ewma / v_free`, which is also the
    congestion score.
    """

    def __init__(self, grid: CityGrid, cfg: Config):
        self.grid, self.cfg = grid, cfg
        n = len(grid.segments)
        self.ratio = [1.0 - cfg.est_prior_load] * n
        self.samples = [0] * n
        self.last_seen = [-math.inf] * n

    def on_telemetry(self, tel: BusTelemetry) -> None:
        cfg = self.cfg
        seg = self.grid.segments[tel.segment_id]
        pos = seg.length - tel.dist_to_next_node
        if pos < cfg.est_skip_start or tel.dist_to_next_node < cfg.est_skip_end:
            return
        sid = tel.segment_id
        sample = max(0.0, min(1.0, tel.speed / tel.free_speed))
        if self.samples[sid] == 0:
            self.ratio[sid] = sample
        else:
            self.ratio[sid] += cfg.est_alpha * (sample - self.ratio[sid])
        self.samples[sid] += 1
        self.last_seen[sid] = tel.t

    def confidence(self, seg_id: int, now: float) -> float:
        if self.samples[seg_id] == 0:
            return 0.0
        age = max(0.0, now - self.last_seen[seg_id])
        return min(1.0, self.samples[seg_id] / 3.0) * math.exp(-age / self.cfg.est_stale_tau)

    def raw_load(self, seg_id: int) -> float:
        return 1.0 - self.ratio[seg_id]

    def load(self, seg_id: int, now: float) -> float:
        """Congestion 0..1, blended toward the prior as the observation goes stale."""
        c = self.confidence(seg_id, now)
        return c * self.raw_load(seg_id) + (1.0 - c) * self.cfg.est_prior_load
