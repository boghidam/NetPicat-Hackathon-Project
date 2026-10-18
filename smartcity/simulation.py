"""Headless fixed-step engine wiring the whole closed loop together."""

from __future__ import annotations

import math
import random

from .config import Config
from .congestion import CongestionEstimator
from .controller import IntersectionController, NetworkCoordinator
from .data import build_city
from .detection import DetectionLoops
from .events import EventLog
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
        # Consumers see only the API stream, exactly as a real deployment would.
        self.api.subscribe(self.estimator.on_telemetry)
        self.api.subscribe(self.predictor.on_telemetry)
        self.api.subscribe(self.coord.observe)
        self.api.subscribe(self.progress.on_telemetry)
        for consumer in (self.loops, self.progress, self.predictor):  # a bus taken off the map must be forgotten
            self.fleet.on_remove.append(consumer.forget)

        self.incident_at = incident_at
        self.incident_segment = self._pick_incident_segment()
        self.incident_started = False
        self.traffic_percent = 100.0  # cars and buses relative to normal, 0 to 1000
        self.rush_point: str | None = None  # name of the key point under rush-hour pressure
        self._since_refresh = cfg.decision_period  # force an initial refresh
        # metrics
        self.slow_ema = 0.0  # recent mean fraction of time buses lose to delay
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
        """Set the amount of cars and buses: 100 is normal traffic, 0 is none, up to 1000 (ten times as much)."""
        percent = float(max(0.0, min(1000.0, percent)))
        old = self.traffic_percent
        if percent == old:
            return
        self.traffic_percent = percent
        self.model.set_car_percent(percent, old)
        self.fleet.set_percent(percent)
        self.events.add(
            self.t,
            "INFO",
            f"traffic {percent:g}% of normal: {self.model.n_cars} cars, {len(self.fleet.buses)} buses",
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

    # -- stepping ------------------------------------------------------------
    def step(self) -> None:
        cfg, dt = self.cfg, self.cfg.dt
        if self.incident_at is not None and not self.incident_started and self.t >= self.incident_at:
            self.trigger_incident()
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
            segs = self.net.segments
            mean_slow = sum(1.0 - min(1.0, b.v / segs[b.seg].speed_limit) for b in buses) / len(buses)
            self.slow_ema += (mean_slow - self.slow_ema) * dt / 60.0

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
            "throughput": float(self.model.exited),
            "cars": float(self.model.n_cars),
            "car_speed": self.model.car_speed_ratio,
            "queued_outside": float(self.model.queued_outside),
            "overrides": float(self.events.overrides),
        }
