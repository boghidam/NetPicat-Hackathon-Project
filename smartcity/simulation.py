"""Headless fixed-step engine wiring the whole closed loop together."""

from __future__ import annotations

import math
import random

from .config import Config
from .congestion import CongestionEstimator
from .controller import IntersectionController, NetworkCoordinator
from .data import build_city
from .detection import DetectionLoops
import copy
from typing import Any

from .events import EventLog
from .learning import JunctionLearner, load_memory
from .mock_api import BusFleet, TelemetryBus
from .network import RoadNetwork
from .predictor import PostIntersectionPredictor
from .progress import RouteProgress
from .traffic_model import TrafficModel


class Simulation:
    # the traffic amount moves along this ladder with the [ and ] keys: 100% is normal
    TRAFFIC_STEPS = (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 200, 300, 400, 500, 600, 700, 800, 900, 1000)

    def __init__(
        self,
        cfg: Config | None = None,
        adaptive: bool = True,
        incident_at: float | None = None,
        net: RoadNetwork | None = None,
        map_name: str = "iasi",
        memory: dict | None = None,
    ):
        self.cfg = cfg = cfg or Config()
        rng = random.Random(cfg.seed)
        self.t = 0.0
        self.net = net or build_city(map_name, cfg)
        self.events = EventLog()
        self.model = TrafficModel(self.net, cfg, rng)
        self.api = TelemetryBus()
        self.fleet = BusFleet(self.net, cfg, rng, self.model, self.api)
        self.estimator = CongestionEstimator(self.net, cfg)
        self.predictor = PostIntersectionPredictor(self.net, cfg, self.events)
        self.loops = DetectionLoops(self.net, cfg)
        self.progress = RouteProgress(self.net)
        self.coord = NetworkCoordinator(
            self.net, cfg, self.estimator, self.predictor, self.loops, self.progress, self.events, adaptive
        )
        self.controllers = {
            n.id: IntersectionController(n.id, self.net, cfg, self.coord, self.events)
            for n in self.net.nodes
            if n.signalised
        }
        self.coord.controllers = self.controllers
        self._map_name = "grid" if "synthetic" in self.net.source else map_name
        if memory is not None:
            self.apply_slot_state(memory)
        else:
            from .slots import DEFAULT_SLOT_NAME, SlotManager

            if self._map_name == "iasi":
                slot = SlotManager().load_slot(DEFAULT_SLOT_NAME, map_name="iasi", fallback_to_default=True)
                self.apply_slot_state(slot)
            else:
                self.coord.learner = JunctionLearner(cfg, self.progress, self.coord.load, None)
        # Consumers see only the API stream, exactly as a real deployment would.
        self.api.subscribe(self.estimator.on_telemetry)
        self.api.subscribe(self.predictor.on_telemetry)
        self.api.subscribe(self.coord.observe)
        self.api.subscribe(self.progress.on_telemetry)
        for consumer in (self.loops, self.progress, self.predictor):  # a bus taken off the map or resting at terminus
            self.fleet.on_remove.append(consumer.forget)
            self.model.on_bus_rest.append(consumer.forget)

        self.incident_at = incident_at
        self.incident_segment = self._pick_incident_segment()
        self.incident_started = False
        self.traffic_percent = 100.0  # cars relative to normal, 0 to 1000 (bus schedule stays fixed)
        self.rush_point: str | None = None  # name of the key point under rush-hour pressure
        self.day_cycle_enabled: bool = cfg.day_cycle_enabled
        self.day_minutes: float = 7.5 * 60.0  # starts at 07:30 morning commute when enabled
        self._since_refresh = cfg.decision_period  # force an initial refresh
        if cfg.traffic_amount != 1.0:  # the world is built at 100%; scale it to the configured TRAFFIC_AMOUNT
            self.set_traffic_percent(cfg.traffic_amount * 100.0)
        # metrics
        self._metrics_key: tuple | None = None
        self._metrics: dict[str, float] = {}
        self.slow_ema = 0.0  # recent mean fraction of time driving buses lose to delay
        self._slow_ema_init = False
        self.peak_density = 0.0
        self.coord.refresh(0.0)

    def _pick_incident_segment(self) -> int:
        """A long edge on the busiest bus corridor, close to the middle of the map."""
        net = self.net
        lines_on = [0] * len(net.segments)
        for line in net.routes:
            for s in set(line.edges):
                lines_on[s] += 1
        cx, cy = (net.bounds[0] + net.bounds[2]) / 2, (net.bounds[1] + net.bounds[3]) / 2

        def key(s) -> tuple[int, float]:
            x, y, _, _ = net.point_at(s.id, s.length / 2)
            return lines_on[s.id], -math.hypot(x - cx, y - cy)

        return max((s for s in net.segments if s.length >= 120), key=key, default=net.segments[0]).id

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
        seg = self.net.segments[sid]
        where = seg.name or f"edge {sid}"
        self.events.add(
            self.t,
            "INCIDENT",
            f"vehicle stalled on {where} (n{seg.from_node}->n{seg.to_node}) for {self.cfg.incident_duration:.0f}s",
            node=seg.to_node,
        )

    # -- runtime traffic controls ---------------------------------------------
    def set_traffic_percent(self, percent: float) -> None:
        """Set the car traffic load (0 to 1000% of normal); the CTP bus schedule stays fixed to its deployed fleet."""
        percent = float(max(0.0, min(1000.0, percent)))
        old = self.traffic_percent
        if percent == old:
            return
        self.traffic_percent = percent
        self.model.set_car_percent(percent, old)
        self.events.add(
            self.t,
            "INFO",
            f"car traffic {percent:g}% of normal: {self.model.n_cars} cars ({len(self.fleet.buses)} buses unchanged)",
        )

    def step_traffic(self, direction: int) -> None:
        """Move one step up (+1) or down (-1) the traffic ladder."""
        steps = self.TRAFFIC_STEPS
        cur = self.traffic_percent
        if direction > 0:
            self.set_traffic_percent(next((s for s in steps if s > cur), steps[-1]))
        else:
            self.set_traffic_percent(next((s for s in reversed(steps) if s < cur), steps[0]))

    @property
    def key_points(self) -> list[str]:
        """The named key points of the map that can be put under rush-hour pressure."""
        return [name for name, _lat, _lon in self.net.landmarks_ll]

    def set_rush_point(self, name: str | None) -> None:
        """Put a key point under rush-hour pressure (extra commuters routed through it), or lift it with None."""
        if name is None:
            self.model.set_rush_node(None)
            if self.rush_point is not None:
                self.events.add(self.t, "INFO", f"rush hour over at {self.rush_point}")
            self.rush_point = None
            return
        match = next((n for n in self.key_points if n.casefold() == name.casefold()), None)
        if match is None:
            raise ValueError(f"unknown key point {name!r}; choose from {', '.join(self.key_points)}")
        node = self._junction_near(match)
        self.rush_point = match
        self.model.set_rush_node(node)
        self.events.add(self.t, "INFO", f"RUSH HOUR at {match}: extra traffic routed through n{node}", node=node)

    def cycle_rush_point(self, direction: int = 1) -> None:
        """Step through the key points: none, then each in turn (backwards with direction=-1)."""
        options: list[str | None] = [None, *self.key_points]
        i = options.index(self.rush_point) if self.rush_point in options else 0
        self.set_rush_point(options[(i + direction) % len(options)])

    def _junction_near(self, key_point: str) -> int:
        """The signalised junction closest to a named key point (a real intersection, never a gate)."""
        net = self.net
        _, lat, lon = next(k for k in net.landmarks_ll if k[0] == key_point)
        x, y = net.proj.to_xy(lat, lon)
        junctions = [n for n in net.nodes if n.signalised] or [n for n in net.nodes if not n.gate]
        return min(junctions, key=lambda n: math.hypot(n.x - x, n.y - y)).id

    @staticmethod
    def day_demand_multiplier(minutes_of_day: float) -> float:
        """Realistic 24-hour Iași commute demand curve (07:30-09:00 AM & 16:30-18:45 PM peaks)."""
        h = (minutes_of_day % 1440.0) / 60.0
        knots = (
            (0.0, 0.35), (5.0, 0.40), (6.5, 0.78), (8.0, 1.34), (9.5, 1.05),
            (11.5, 0.84), (13.5, 1.08), (15.5, 1.02), (17.5, 1.40), (19.5, 0.96),
            (21.5, 0.58), (24.0, 0.35),
        )
        for i in range(len(knots) - 1):
            h0, v0 = knots[i]
            h1, v1 = knots[i + 1]
            if h0 <= h <= h1:
                f = (h - h0) / max(1e-6, h1 - h0)
                return v0 + (v1 - v0) * f
        return 1.0

    def set_weather(self, weather: str) -> None:
        self.model.set_weather(weather)
        self.events.add(self.t, "INFO", f"weather changed to {self.model.weather}")

    def set_day_cycle(self, enabled: bool) -> None:
        self.day_cycle_enabled = bool(enabled)
        if not self.day_cycle_enabled:
            self.model.day_demand_mult = 1.0
        else:
            self.model.day_demand_mult = self.day_demand_multiplier(self.day_minutes)

    def set_urban_friction(self, enabled: bool) -> None:
        self.model.set_urban_friction(enabled)

    def dispatch_ambulance(self) -> bool:
        amb = self.model.dispatch_ambulance()
        if amb is not None:
            self.events.add(self.t, "INFO", f"EMERGENCY {amb.id} dispatched with green-wave preemption")
            return True
        return False

    # -- stepping ------------------------------------------------------------
    def step(self) -> None:
        cfg, dt = self.cfg, self.cfg.dt
        if self.incident_at is not None and not self.incident_started and self.t >= self.incident_at:
            self.trigger_incident()
        if self.day_cycle_enabled:
            # 1 simulated city-minute every 4 simulation seconds -> full rush waves visible in minutes
            self.day_minutes = (self.day_minutes + dt / 4.0) % 1440.0
            self.model.day_demand_mult = self.day_demand_multiplier(self.day_minutes)
        self.coord.emergency_approaches = self.model.emergency_approaches() if self.model.ambulances else set()
        for c in self.controllers.values():
            c.step(self.t, dt)
        self.model.step(self.t, dt, self.coord.signal_state, self.coord.release_factor)
        self.fleet.emit(self.t)
        self._since_refresh = round(self._since_refresh + dt, 6)
        if self._since_refresh >= cfg.decision_period:
            self._since_refresh = 0.0
            self.coord.refresh(self.t)
            self.peak_density = max(self.peak_density, max(self.model.density(s.id) for s in self.net.segments))
        self.t = round(self.t + dt, 6)
        buses = self.fleet.buses
        if buses:
            driving = [b for b in buses if b.status == "DRIVING" and b.stop_dwell <= 0.0]
            if driving:
                segs = self.net.segments
                mean_slow = sum(1.0 - min(1.0, b.v / segs[b.seg].speed_limit) for b in driving) / len(driving)
                if not self._slow_ema_init:
                    self.slow_ema = mean_slow
                    self._slow_ema_init = True
                else:
                    self.slow_ema += (mean_slow - self.slow_ema) * min(1.0, dt / 30.0)

    def run(self, seconds: float) -> None:
        for _ in range(int(round(seconds / self.cfg.dt))):
            self.step()

    # -- ML slot state hydration / export (strictly in-memory) ---------------
    def apply_slot_state(self, state: dict[str, Any] | None) -> None:
        """Hydrate `JunctionLearner` and `PostIntersectionPredictor` in RAM from a slot or policy dict."""
        if not state:
            self.coord.learner = JunctionLearner(self.cfg, self.progress, self.coord.load, None)
            return
        policy = state["policy"] if isinstance(state.get("policy"), dict) else state
        self.coord.learner = JunctionLearner(self.cfg, self.progress, self.coord.load, policy)
        if isinstance(state.get("predictor"), dict):
            self.predictor.load_state(state["predictor"])

    def export_slot_state(self, base_slot: dict[str, Any] | None = None) -> dict[str, Any]:
        """Snapshot the current in-memory ML state into a schema v2 slot dictionary (no disk I/O)."""
        from .slots import CURRENT_SCHEMA_VERSION, DEFAULT_SLOT_NAME, SlotManager

        out = copy.deepcopy(base_slot) if base_slot is not None else SlotManager.clean_state(
            DEFAULT_SLOT_NAME, map_name=self._map_name
        )
        out["schema_version"] = CURRENT_SCHEMA_VERSION
        meta = out.setdefault("metadata", {})
        meta.setdefault("map", self._map_name)
        if self.coord.learner is not None:
            pol = self.coord.learner.memory()
            out["policy"] = pol
            meta["episodes"] = max(int(meta.get("episodes", 0)), int(pol.get("episodes", 0)))
        out["predictor"] = self.predictor.export_state()
        return out

    # -- metrics -----------------------------------------------------------------
    def metrics(self) -> dict[str, float]:
        """KPIs as of now. The HUD asks every frame but they only change with the clock, so one tick's answer is
        reused until the next tick (or until the fleet or car count changes)."""
        key = (self.t, len(self.fleet.buses), self.model.n_cars)
        if self._metrics_key != key:
            self._metrics_key, self._metrics = key, self._compute_metrics()
        return dict(self._metrics)

    def _compute_metrics(self) -> dict[str, float]:
        buses = self.fleet.buses
        elapsed = sum(b.elapsed for b in buses) or 1.0
        # Honest bus-inferred arterial traffic flow (strictly from bus telemetry, never reading car positions)
        obs = [
            1.0 - self.coord.est_load[s.id]
            for s in self.net.segments
            if self.estimator.confidence(s.id, self.t) >= 0.05
        ]
        predicted_flow = sum(obs) / len(obs) if obs else (1.0 - self.cfg.est_prior_load)
        driving = sum(1 for b in buses if b.status == "DRIVING")
        layover = sum(1 for b in buses if b.status == "LAYOVER")
        on_break = sum(1 for b in buses if b.status == "DRIVER_BREAK")
        return {
            "t": self.t,
            "bus_delay_ratio": sum(b.delay for b in buses) / elapsed,  # share of travel time lost
            "bus_delay_recent": self.slow_ema,
            "predicted_flow": predicted_flow,  # inferred strictly from bus telemetry (0..1)
            "mean_density": self.model.mean_density(),
            "peak_density": self.peak_density,
            "throughput": float(self.model.exited),
            "cars": float(self.model.n_cars),
            "buses_driving": float(driving),
            "buses_layover": float(layover),
            "buses_break": float(on_break),
            "car_speed": self.model.car_speed_ratio,
            # mean car speed / limit over every car-second of the run (steadier than the 30 s EMA above)
            "car_speed_avg": self.model.car_ratio_sum / self.model.car_ratio_n if self.model.car_ratio_n else 1.0,
            "queued_outside": float(self.model.queued_outside),
            "overrides": float(self.events.overrides),
        }
