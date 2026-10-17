"""Headless fixed-step engine wiring the whole closed loop together."""

from __future__ import annotations

import random

from .config import Config
from .congestion import CongestionEstimator
from .controller import IntersectionController, NetworkCoordinator
from .events import EventLog
from .mock_api import BusFleet, TelemetryBus
from .network import CityGrid
from .predictor import PostIntersectionPredictor
from .traffic_model import TrafficModel


class Simulation:
    def __init__(self, cfg: Config | None = None, adaptive: bool = True, incident_at: float | None = None):
        self.cfg = cfg = cfg or Config()
        rng = random.Random(cfg.seed)
        self.t = 0.0
        self.grid = CityGrid(cfg)
        self.events = EventLog()
        self.model = TrafficModel(self.grid, cfg, rng)
        self.api = TelemetryBus()
        self.fleet = BusFleet(self.grid, cfg, rng, self.api)
        self.estimator = CongestionEstimator(self.grid, cfg)
        self.predictor = PostIntersectionPredictor(self.grid, cfg, self.events)
        self.coord = NetworkCoordinator(self.grid, cfg, self.estimator, self.predictor, self.events, adaptive)
        self.controllers = {
            n.id: IntersectionController(n.id, self.grid, cfg, self.coord, self.events) for n in self.grid.nodes
        }
        self.coord.controllers = self.controllers
        # Consumers see only the API stream, exactly as a real deployment would.
        self.api.subscribe(self.estimator.on_telemetry)
        self.api.subscribe(self.predictor.on_telemetry)
        self.api.subscribe(self.coord.observe)

        self.incident_at = incident_at
        self.incident_segment = self.grid.segment_between(5, 6)  # central eastbound link
        self.incident_started = False
        self._since_refresh = cfg.decision_period  # force an initial refresh
        # metrics
        self.slow_ema = 0.0  # recent mean fraction of time buses lose to delay
        self.peak_density = 0.0
        self.coord.refresh(0.0)

    # -- control surface -----------------------------------------------------
    @property
    def adaptive(self) -> bool:
        return self.coord.adaptive

    def set_adaptive(self, on: bool) -> None:
        self.coord.adaptive = on
        self.events.add(self.t, "INFO", f"control mode: {'ADAPTIVE' if on else 'FIXED TIMER'}")

    def trigger_incident(self, segment_id: int | None = None) -> None:
        sid = self.incident_segment if segment_id is None else segment_id
        self.model.start_incident(sid, self.t)
        self.incident_started = True
        seg = self.grid.segments[sid]
        self.events.add(
            self.t,
            "INCIDENT",
            f"lane blocked on seg{sid} (n{seg.from_node}->n{seg.to_node}) for {self.cfg.incident_duration:.0f}s",
            node=seg.to_node,
        )

    # -- stepping ------------------------------------------------------------
    def step(self) -> None:
        cfg, dt = self.cfg, self.cfg.dt
        if self.incident_at is not None and not self.incident_started and self.t >= self.incident_at:
            self.trigger_incident()
        for c in self.controllers.values():
            c.step(self.t, dt)
        self.model.step(self.t, dt, lambda s: self.coord.signal_state(s) == "G", self.coord.release_factor)
        self.fleet.step(self.t, dt, self.coord.signal_state, self.model.speed)
        self._since_refresh += dt
        if self._since_refresh >= cfg.decision_period:
            self._since_refresh = 0.0
            self.coord.refresh(self.t)
        self.t += dt
        mean_slow = sum(1.0 - min(1.0, b.v / cfg.free_speed) for b in self.fleet.buses) / len(self.fleet.buses)
        self.slow_ema += (mean_slow - self.slow_ema) * dt / 60.0
        self.peak_density = max(self.peak_density, max(self.model.density(i) for i in range(len(self.grid.segments))))

    def run(self, seconds: float) -> None:
        for _ in range(int(round(seconds / self.cfg.dt))):
            self.step()

    # -- metrics -----------------------------------------------------------------
    def metrics(self) -> dict[str, float]:
        buses = self.fleet.buses
        elapsed = sum(b.elapsed for b in buses) or 1.0
        return {
            "t": self.t,
            "bus_delay_ratio": sum(b.delay for b in buses) / elapsed,  # share of travel time lost
            "bus_delay_recent": self.slow_ema,
            "mean_density": self.model.mean_density(),
            "peak_density": self.peak_density,
            "throughput": self.model.exited,
            "overrides": float(self.events.overrides),
        }
