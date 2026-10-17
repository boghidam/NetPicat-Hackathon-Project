"""Signal control: per-intersection phase machines and the network-wide coordinator.

The coordinator is where the global optimisation lives. It turns bus-derived congestion
estimates and AI forecasts into per-segment loads, and from those into (a) a throttle on
how fast each movement may release traffic, (b) a hard spillback guard, (c) dynamic
phase scores, and (d) a grid-wide budget on green extensions.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .config import Config
from .congestion import CongestionEstimator
from .events import EventLog
from .mock_api import BusTelemetry
from .network import Axis, CityGrid
from .predictor import Forecast, PostIntersectionPredictor

AXIS_NAME = {Axis.NS: "NS", Axis.EW: "EW"}


@dataclass(frozen=True)
class Score:
    axis: Axis
    pressure: float
    bus_delay: float
    forecast: float
    downstream_penalty: float
    age_bonus: float
    total: float


class NetworkCoordinator:
    def __init__(
        self,
        grid: CityGrid,
        cfg: Config,
        estimator: CongestionEstimator,
        predictor: PostIntersectionPredictor,
        events: EventLog,
        adaptive: bool = True,
    ):
        self.grid, self.cfg = grid, cfg
        self.estimator, self.predictor, self.events = estimator, predictor, events
        self.adaptive = adaptive
        self.controllers: dict[int, IntersectionController] = {}
        n = len(grid.segments)
        self.latest: dict[str, BusTelemetry] = {}
        self._by_seg: dict[int, list[BusTelemetry]] = {}
        self.est_load = [cfg.est_prior_load] * n  # from bus telemetry only
        self.fc_load = [0.0] * n  # from AI forecasts only
        self.load = [cfg.est_prior_load] * n  # what the controllers act on
        self.dload = [0.0] * n  # worst load among the links an approach releases into
        self._worst = [-1] * n
        self.throttle = [1.0] * n
        self._throttled: set[int] = set()
        self._ext_log: deque[tuple[float, int, float]] = deque()
        self._last_log: dict[tuple[str, int], float] = {}
        self._neighbours = {
            node.id: {grid.segments[sid].to_node for sid in grid.out_seg[node.id] if sid is not None}
            for node in grid.nodes
        }

    # -- intake / periodic refresh -----------------------------------------
    def observe(self, tel: BusTelemetry) -> None:
        self.latest[tel.bus_id] = tel

    def refresh(self, now: float) -> None:
        cfg, grid = self.cfg, self.grid
        self._by_seg = {}
        for tel in self.latest.values():
            self._by_seg.setdefault(tel.segment_id, []).append(tel)
        for seg in grid.segments:
            sid = seg.id
            self.est_load[sid] = self.estimator.load(sid, now)
            self.fc_load[sid] = max(
                (
                    f.predicted_density * f.confidence * math.exp(-max(0.0, f.eta - now) / cfg.pred_decay_tau)
                    for f in self.predictor.incoming(sid, now)
                ),
                default=0.0,
            )
            self.load[sid] = max(self.est_load[sid], self.fc_load[sid])
        for seg in grid.segments:
            worst, worst_id = 0.0, -1
            for target, _share in grid.turn_targets(seg.id):
                if target is not None and self.load[target] > worst:
                    worst, worst_id = self.load[target], target
            self.dload[seg.id], self._worst[seg.id] = worst, worst_id
            if not self.adaptive:
                self.throttle[seg.id] = 1.0
                continue
            # Backpressure: meter only when this release would push traffic onto a link
            # that is clearly worse off than the one it leaves (shifting the bottleneck).
            # When everything is uniformly busy, metering would just waste green.
            excess = worst - max(cfg.rho_gate, self.load[seg.id])
            g = 1.0 - excess / (cfg.rho_block - cfg.rho_gate) if excess > 0.0 else 1.0
            g = max(0.0, min(1.0, g))
            self.throttle[seg.id] = g
            self._log_throttle(now, seg.id, g)

    def _log_throttle(self, now: float, sid: int, g: float) -> None:
        seg = self.grid.segments[sid]
        if g < 0.8 and sid not in self._throttled:
            self._throttled.add(sid)
            w = self._worst[sid]
            self.events.add(
                now,
                "THROTTLE",
                f"n{seg.to_node} {AXIS_NAME[seg.axis]} seg{sid}: release metered to {g:.0%} "
                f"(next link seg{w} load {self.dload[sid]:.2f})",
                node=seg.to_node,
            )
        elif g >= 0.95:
            self._throttled.discard(sid)

    def log_once(self, now: float, kind: str, node: int, text: str, cooldown: float = 6.0) -> None:
        key = (kind, node)
        if now - self._last_log.get(key, -math.inf) >= cooldown:
            self._last_log[key] = now
            self.events.add(now, kind, text, node=node)

    # -- what the signals / traffic model read ------------------------------
    def signal_state(self, seg_id: int) -> str:
        seg = self.grid.segments[seg_id]
        return self.controllers[seg.to_node].state_for(seg.axis)

    def release_factor(self, seg_id: int) -> float:
        return self.throttle[seg_id] if self.adaptive else 1.0

    # -- scoring -------------------------------------------------------------
    def axis_blocked(self, node: int, axis: Axis) -> bool:
        """True when every approach on the axis would release into a (nearly) full link."""
        approaches = self.grid.approaches(node, axis)
        return bool(approaches) and all(self.dload[s.id] >= self.cfg.rho_block for s in approaches)

    def slow_bus(self, node: int, axis: Axis) -> BusTelemetry | None:
        cfg = self.cfg
        worst: BusTelemetry | None = None
        for seg in self.grid.approaches(node, axis):
            for tel in self._by_seg.get(seg.id, ()):
                if tel.dist_to_next_node <= cfg.d_trigger and tel.speed < cfg.v_slow_frac * tel.free_speed:
                    if worst is None or tel.speed < worst.speed:
                        worst = tel
        return worst

    def forecasts_for(self, node: int, axis: Axis, now: float) -> list[Forecast]:
        out: list[Forecast] = []
        for seg in self.grid.approaches(node, axis):
            out.extend(f for f in self.predictor.incoming(seg.id, now) if f.eta >= now)
        return sorted(out, key=lambda f: f.eta)

    def score(self, node: int, axis: Axis, now: float) -> Score:
        cfg = self.cfg
        approaches = self.grid.approaches(node, axis)
        if not approaches:
            return Score(axis, 0, 0, 0, 0, 0, 0)
        pressure = max(max(0.0, self.est_load[s.id] - 0.5 * self.dload[s.id]) for s in approaches)
        bus_delay = 0.0
        for s in approaches:
            for tel in self._by_seg.get(s.id, ()):
                if tel.dist_to_next_node <= 150.0:
                    slowness = 1.0 - min(1.0, tel.speed / tel.free_speed)
                    bus_delay = max(bus_delay, slowness * (1.0 - 0.5 * tel.dist_to_next_node / 150.0))
        forecast = max(self.fc_load[s.id] for s in approaches)
        downstream = sum(self.dload[s.id] for s in approaches) / len(approaches)
        red_for = self.controllers[node].red_time(axis, now) if node in self.controllers else 0.0
        age = min(1.0, red_for / cfg.age_full)
        total = (
            cfg.w_pressure * pressure
            + cfg.w_bus_delay * bus_delay
            + cfg.w_forecast * forecast
            + cfg.w_age * age
            - cfg.w_downstream * downstream
        )
        return Score(axis, pressure, bus_delay, forecast, cfg.w_downstream * downstream, cfg.w_age * age, total)

    def plan_green(self, node: int, axis: Axis, now: float) -> tuple[float, str]:
        """Green duration for the phase that is about to start, and a note on why it differs."""
        cfg = self.cfg
        if not self.adaptive:
            return cfg.base_green, ""
        if self.axis_blocked(node, axis) and not self.axis_blocked(node, axis.other):
            return cfg.min_green, "downstream full"
        a = max(0.0, self.score(node, axis, now).total)
        b = max(0.0, self.score(node, axis.other, now).total)
        share = (a + cfg.split_eps) / (a + b + 2 * cfg.split_eps)
        planned = max(cfg.min_green, min(cfg.max_green, 2 * cfg.base_green * share))
        return planned, f"score {a:.2f} vs {b:.2f}"

    # -- extension budget ------------------------------------------------------
    def request_extension(self, node: int, seconds: float, now: float) -> float:
        cfg = self.cfg
        while self._ext_log and self._ext_log[0][0] < now - cfg.budget_window:
            self._ext_log.popleft()
        used = sum(sec for _, _, sec in self._ext_log)
        neighbours = self._neighbours[node]
        recent_nb = {n for t, n, _ in self._ext_log if n in neighbours and t >= now - cfg.neighbour_window}
        damped = seconds / (1.0 + cfg.neighbour_damping * len(recent_nb))
        granted = min(damped, cfg.budget_seconds - used)
        if granted < 1.0:
            self.log_once(
                now,
                "BUDGET_DENIED",
                node,
                f"n{node}: extension of {seconds:.1f}s denied (grid budget {used:.0f}/{cfg.budget_seconds:.0f}s)",
            )
            return 0.0
        self._ext_log.append((now, node, granted))
        return granted


class IntersectionController:
    """Two-phase signal (NS / EW) with a standard timer and adaptive overrides."""

    def __init__(self, node: int, grid: CityGrid, cfg: Config, coord: NetworkCoordinator, events: EventLog):
        self.node, self.grid, self.cfg, self.coord, self.events = node, grid, cfg, coord, events
        n = grid.nodes[node]
        self.axis = Axis.NS if (n.row + n.col) % 2 == 0 else Axis.EW
        self.phase = "GREEN"  # or "YELLOW"
        self.elapsed = float((n.row * 3 + n.col * 5) % int(cfg.base_green))
        self.planned = cfg.base_green
        self.extended_by = 0.0
        self.reason = "BASE"
        self._became_red = {self.axis.other: 0.0, self.axis: math.inf}
        self._cooldown_until = 0.0
        self._acc = 0.0
        self._used_forecasts: set[int] = set()

    # -- queries -----------------------------------------------------------
    def state_for(self, axis: Axis) -> str:
        if axis is not self.axis:
            return "R"
        return "G" if self.phase == "GREEN" else "Y"

    def red_time(self, axis: Axis, now: float) -> float:
        return 0.0 if axis is self.axis else max(0.0, now - self._became_red.get(axis, now))

    @property
    def remaining(self) -> float:
        return self.planned - self.elapsed

    # -- simulation step ---------------------------------------------------
    def step(self, now: float, dt: float) -> None:
        self.elapsed += dt
        if self.phase == "YELLOW":
            if self.elapsed >= self.cfg.yellow:
                self._switch(now)
            return
        if self.coord.adaptive:
            self._acc += dt
            if self._acc >= self.cfg.decision_period:
                self._acc = 0.0
                self._decide(now)
        if self.phase == "GREEN" and self.elapsed >= self.planned:
            self._begin_yellow(now)

    def _begin_yellow(self, now: float) -> None:
        self.phase, self.elapsed = "YELLOW", 0.0
        self._became_red[self.axis] = now + self.cfg.yellow

    def _switch(self, now: float) -> None:
        self.axis = self.axis.other
        self.phase, self.elapsed, self.extended_by, self.reason = "GREEN", 0.0, 0.0, "BASE"
        self._became_red[self.axis.other] = now
        self.planned, why = self.coord.plan_green(self.node, self.axis, now)
        if self.coord.adaptive and abs(self.planned - self.cfg.base_green) >= 3.0:
            self.reason = "SPLIT_ADJUST"
            if abs(self.planned - self.cfg.base_green) >= 10.0:  # keep the log readable
                self.events.add(
                    now,
                    "SPLIT_ADJUST",
                    f"n{self.node} {AXIS_NAME[self.axis]} green {self.planned:.0f}s instead of {self.cfg.base_green:.0f}s ({why})",
                    node=self.node,
                )

    # -- adaptive decisions ----------------------------------------------------
    def _decide(self, now: float) -> None:
        cfg, coord, cur = self.cfg, self.coord, self.axis
        other = cur.other
        blocked = coord.axis_blocked(self.node, cur)
        slow = coord.slow_bus(self.node, cur)

        # 1. Spillback guard: never keep serving a movement into a full link.
        if blocked and self.elapsed >= cfg.min_green and not coord.axis_blocked(self.node, other):
            coord.log_once(
                now,
                "SPILLBACK_HOLD",
                self.node,
                f"n{self.node} {AXIS_NAME[cur]} green cut short: next links full (load >= {cfg.rho_block:.2f}), green passed to {AXIS_NAME[other]}",
            )
            self.planned = self.elapsed
            return

        # 2. Local optimisation: a slow bus near the stop line earns more green.
        if slow is not None and not blocked and now >= self._cooldown_until and self.remaining < cfg.ext_remaining:
            want = cfg.ext_gain * (1.0 - slow.speed / (cfg.v_slow_frac * slow.free_speed))
            self._extend(
                now,
                want,
                "LOCAL_EXTEND",
                f"{slow.bus_id} at {slow.speed:.1f} m/s, {slow.dist_to_next_node:.0f} m from line",
            )

        # 3. Feed-forward: forecasts for buses that have not arrived yet.
        for f in coord.forecasts_for(self.node, cur, now):
            if f.id in self._used_forecasts or f.predicted_density < cfg.pred_density_min or f.confidence < 0.3:
                continue
            until = f.eta - now
            if until <= cfg.lead_window and self.remaining < until + 3.0 and not blocked:
                self._used_forecasts.add(f.id)
                self._extend(
                    now,
                    cfg.pred_ext,
                    "PREDICTIVE_EXTEND",
                    f"forecast for {f.source_bus}: density {f.predicted_density:.2f}, ETA {until:.0f}s, conf {f.confidence:.2f}",
                )
                break

        for f in coord.forecasts_for(self.node, other, now):
            if f.id in self._used_forecasts or f.predicted_density < cfg.pred_density_min or f.confidence < 0.3:
                continue
            until = f.eta - now
            if (
                self.elapsed >= cfg.min_green
                and until <= cfg.lead_window
                and slow is None
                and not coord.axis_blocked(self.node, other)
                and self.remaining > cfg.yellow
            ):
                self._used_forecasts.add(f.id)
                self.events.add(
                    now,
                    "PREDICTIVE_SWITCH",
                    f"n{self.node} {AXIS_NAME[cur]} green cut short: forecast dense traffic ({f.predicted_density:.2f}) "
                    f"for {f.source_bus} on {AXIS_NAME[other]} in {until:.0f}s",
                    node=self.node,
                )
                self.planned = self.elapsed
                return

        # 4. Dynamic weighting: end a green early if the cross axis clearly needs it more.
        if self.elapsed >= cfg.min_green and slow is None and self.remaining > cfg.yellow + 1.0:
            mine, theirs = coord.score(self.node, cur, now), coord.score(self.node, other, now)
            if theirs.total - mine.total >= cfg.early_switch_margin and not coord.axis_blocked(self.node, other):
                self.events.add(
                    now,
                    "EARLY_SWITCH",
                    f"n{self.node} {AXIS_NAME[cur]} green ended early: {AXIS_NAME[other]} score {theirs.total:.2f} vs {mine.total:.2f}",
                    node=self.node,
                )
                self.planned = self.elapsed

    def _extend(self, now: float, seconds: float, kind: str, detail: str) -> None:
        room = self.cfg.max_green - self.planned
        if room <= 0.5:
            return
        granted = self.coord.request_extension(self.node, min(seconds, room), now)
        self._cooldown_until = now + self.cfg.ext_cooldown
        if granted <= 0.0:
            return
        self.planned += granted
        self.extended_by += granted
        self.reason = kind
        self.events.add(
            now,
            kind,
            f"n{self.node} {AXIS_NAME[self.axis]} +{granted:.1f}s ({detail})",
            node=self.node,
        )
