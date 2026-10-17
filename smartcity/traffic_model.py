"""Hidden ground-truth traffic: macroscopic vehicle counts per segment.

Controllers never read this directly. They only see what buses report, so the buses
really are the sensors. The model decides how fast a bus can travel (Greenshields)
and moves background vehicles through signals, respecting downstream storage.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from .config import Config
from .network import CityGrid


class TrafficModel:
    def __init__(self, grid: CityGrid, cfg: Config, rng: random.Random):
        self.grid, self.cfg = grid, cfg
        n = len(grid.segments)
        self.cap = [s.capacity for s in grid.segments]
        self.count = [cfg.initial_fill * c for c in self.cap]
        jitter = cfg.demand_jitter
        self.demand = [cfg.base_demand * (1 + rng.uniform(-jitter, jitter)) for _ in range(n)]
        self.out_mult = [1.0] * n  # incident: reduces discharge at the stop line
        self.demand_mult = [1.0] * n
        self.incidents: dict[int, float] = {}  # segment id -> expiry time
        self.exited = 0.0  # vehicles that left the network (throughput)
        self.outflow = [0.0] * n  # last tick's discharge, veh/s (for display)

    # -- state queries ------------------------------------------------------
    def density(self, seg_id: int) -> float:
        """Fraction of jam density, 0..1."""
        return min(1.0, self.count[seg_id] / self.cap[seg_id])

    def speed(self, seg_id: int) -> float:
        """Greenshields: v = v_free * (1 - rho / rho_jam), floored."""
        cfg = self.cfg
        return max(cfg.min_speed, cfg.free_speed * (1.0 - self.density(seg_id)))

    def mean_density(self) -> float:
        return sum(self.density(i) for i in range(len(self.cap))) / len(self.cap)

    # -- incidents ----------------------------------------------------------
    def start_incident(self, seg_id: int, now: float) -> None:
        self.out_mult[seg_id] = self.cfg.incident_outflow_mult
        self.demand_mult[seg_id] = self.cfg.incident_demand_mult
        self.incidents[seg_id] = now + self.cfg.incident_duration

    def clear_incident(self, seg_id: int) -> None:
        self.out_mult[seg_id] = 1.0
        self.demand_mult[seg_id] = 1.0
        self.incidents.pop(seg_id, None)

    # -- dynamics -----------------------------------------------------------
    def step(
        self,
        now: float,
        dt: float,
        is_green: Callable[[int], bool],
        release_factor: Callable[[int], float],
    ) -> None:
        """Advance one tick. `release_factor` is the controller's throttle in [0, 1]."""
        for seg_id, expiry in list(self.incidents.items()):
            if now >= expiry:
                self.clear_incident(seg_id)

        count, cap, grid = self.count, self.cap, self.grid
        flow = [0.0] * len(count)
        for seg in grid.segments:
            sid = seg.id
            if not is_green(sid) or count[sid] <= 1e-9:
                continue
            rate = self.cfg.sat_flow * release_factor(sid) * self.out_mult[sid]
            rate = min(rate, count[sid] / dt)
            # A movement cannot discharge into a link with no room (physical spillback).
            for target, share in grid.turn_targets(sid):
                if target is not None:
                    space = max(0.0, cap[target] - count[target])
                    rate = min(rate, space / (share * dt))
            flow[sid] = rate

        # Apply simultaneously so segment ordering does not bias the result.
        for seg in grid.segments:
            sid = seg.id
            rate = flow[sid]
            if rate > 0.0:
                count[sid] -= rate * dt
                for target, share in grid.turn_targets(sid):
                    if target is None:
                        self.exited += rate * share * dt
                    else:
                        count[target] += rate * share * dt
        for sid in range(len(count)):
            count[sid] = min(cap[sid], max(0.0, count[sid] + self.demand[sid] * self.demand_mult[sid] * dt))
        self.outflow = flow
