"""Signal control: per-intersection phase machines and the network-wide coordinator.

Each signalised intersection serves one *phase* at a time: approaches whose headings are roughly
opposite are green together (and red together), an unpaired street has a phase of its own.
SUMO-style safety rules apply: a green never lasts less than `min_green` (7 s), then comes a 3 s
yellow and a 2 s all-red clearance before the conflicting phase starts.

The coordinator is where the global optimisation lives. It turns bus-derived congestion
estimates (per-link EWMA plus virtual detection loops) and AI forecasts into per-edge loads, and
from those, walking the road graph to the *junction at the end of each release*, into (a) a
throttle on how fast each approach may release cars, (b) dynamic phase scores that size each green
and can end one early, and (c) a grid-wide budget on green extensions.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .config import Config
from .congestion import CongestionEstimator
from .detection import DetectionLoops
from .events import EventLog
from .mock_api import BusTelemetry
from .network import RoadNetwork
from .predictor import Forecast, PostIntersectionPredictor

JUNCTION_WEIGHT = 0.85  # how much of a downstream junction's congestion counts against releasing into it


@dataclass(frozen=True)
class Score:
    seg_id: int
    pressure: float
    bus_delay: float
    forecast: float
    downstream_penalty: float
    age_bonus: float
    total: float


class NetworkCoordinator:
    def __init__(
        self,
        net: RoadNetwork,
        cfg: Config,
        estimator: CongestionEstimator,
        predictor: PostIntersectionPredictor,
        loops: DetectionLoops,
        events: EventLog,
        adaptive: bool = True,
    ):
        self.net, self.cfg = net, cfg
        self.estimator, self.predictor, self.loops, self.events = estimator, predictor, loops, events
        self.adaptive = adaptive
        self.controllers: dict[int, IntersectionController] = {}
        n = len(net.segments)
        self.est_load = [cfg.est_prior_load] * n  # bus telemetry: per-link EWMA and detection loops
        self.fc_load = [0.0] * n  # from AI forecasts only
        self.load = [cfg.est_prior_load] * n  # what the controllers act on
        self.junction_load = [cfg.est_prior_load] * len(net.nodes)  # congestion at the end of each approach set
        self.dload = [0.0] * n  # worst downstream load an approach would release into
        self._worst = [-1] * n
        self.throttle = [1.0] * n
        self._throttled: set[int] = set()
        self._ext_log: deque[tuple[float, int, float]] = deque()
        self._last_log: dict[tuple[str, int], float] = {}
        self._neighbours = {node.id: net.neighbours(node.id) for node in net.nodes}

    # -- names for logs and the HUD ------------------------------------------
    def label(self, seg_id: int) -> str:
        seg = self.net.segments[seg_id]
        return f"n{seg.to_node} {(seg.name or seg.highway or seg.arrives_from)[:22]}"

    def label_group(self, group: list[int]) -> str:
        node = self.net.segments[group[0]].to_node
        names = []
        for a in group:
            seg = self.net.segments[a]
            name = (seg.name or seg.highway or seg.arrives_from)[:18]
            if name not in names:
                names.append(name)
        return f"n{node} " + "/".join(names)

    # -- intake / periodic refresh -----------------------------------------
    def observe(self, tel: BusTelemetry) -> None:
        self.loops.observe(tel)

    def refresh(self, now: float) -> None:
        cfg, net = self.cfg, self.net
        self.loops.refresh()
        for seg in net.segments:
            sid = seg.id
            load = self.estimator.load(sid, now)
            reading = self.loops.reading(sid)
            if reading is not None and reading.density is not None:
                load = max(load, reading.density)
            self.est_load[sid] = load
            self.fc_load[sid] = max(
                (
                    f.predicted_density * f.confidence * math.exp(-max(0.0, f.eta - now) / cfg.pred_decay_tau)
                    for f in self.predictor.incoming(sid, now)
                ),
                default=0.0,
            )
            self.load[sid] = max(load, self.fc_load[sid])
        for node in net.nodes:  # congestion of a junction: the two worst approaches into it
            loads = sorted((self.load[s] for s in net.in_segs[node.id]), reverse=True)[:2]
            self.junction_load[node.id] = sum(loads) / len(loads) if loads else 0.0
        for seg in net.segments:
            sid = seg.id
            worst, worst_id = 0.0, -1
            for target, _share in net.turn_targets(sid):
                # releasing onto an edge also loads the junction it leads to
                t_load = max(self.load[target], JUNCTION_WEIGHT * self.junction_load[net.segments[target].to_node])
                if t_load > worst:
                    worst, worst_id = t_load, target
            self.dload[sid], self._worst[sid] = worst, worst_id
            if not self.adaptive or seg.to_node not in self.controllers:
                self.throttle[sid] = 1.0
                continue
            # Backpressure: meter only when this release would push traffic onto a link
            # that is clearly worse off than the one it leaves (shifting the bottleneck).
            # When everything is uniformly busy, metering would just waste green.
            excess = worst - max(cfg.rho_gate, self.load[sid])
            g = 1.0 - excess / (cfg.rho_block - cfg.rho_gate) if excess > 0.0 else 1.0
            g = max(0.0, min(1.0, g))
            # Buses share the single lane with the cars and cannot pass them, so metering cars
            # would hold up a bus queued behind them: leave an approach alone while one is close.
            reading = self.loops.reading(sid)
            if reading is not None and reading.count[1] > 0:
                g = 1.0
            self.throttle[sid] = g
            self._log_throttle(now, sid, g)

    def _log_throttle(self, now: float, sid: int, g: float) -> None:
        seg = self.net.segments[sid]
        if g < 0.8 and sid not in self._throttled:
            self._throttled.add(sid)
            target = self.net.segments[self._worst[sid]]
            where = target.name or f"seg{target.id}"
            self.events.add(
                now,
                "THROTTLE",
                f"{self.label(sid)}: car release metered to {g:.0%} (downstream {where[:20]} load {self.dload[sid]:.2f})",
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
        ctl = self.controllers.get(self.net.segments[seg_id].to_node)
        return "G" if ctl is None else ctl.state_for(seg_id)

    def release_factor(self, seg_id: int) -> float:
        return self.throttle[seg_id] if self.adaptive else 1.0

    # -- sensing ---------------------------------------------------------------
    def slow_bus(self, seg_id: int) -> BusTelemetry | None:
        """The slowest bus inside the 150 m detection window of an approach, if it is crawling."""
        reading = self.loops.reading(seg_id)
        return reading.slowest if reading is not None else None

    def forecasts_for(self, seg_id: int, now: float) -> list[Forecast]:
        return sorted((f for f in self.predictor.incoming(seg_id, now) if f.eta >= now), key=lambda f: f.eta)

    def score(self, seg_id: int, now: float) -> Score:
        cfg = self.cfg
        seg = self.net.segments[seg_id]
        pressure = max(0.0, self.est_load[seg_id] - 0.5 * self.dload[seg_id])
        reading = self.loops.reading(seg_id)
        bus_delay = reading.delay_score if reading is not None else 0.0
        forecast = self.fc_load[seg_id]
        ctl = self.controllers.get(seg.to_node)
        age = min(1.0, (ctl.red_time(seg_id, now) if ctl else 0.0) / cfg.age_full)
        total = (
            cfg.w_pressure * pressure
            + cfg.w_bus_delay * bus_delay
            + cfg.w_forecast * forecast
            + cfg.w_age * age
            - cfg.w_downstream * self.dload[seg_id]
        )
        return Score(
            seg_id, pressure, bus_delay, forecast, cfg.w_downstream * self.dload[seg_id], cfg.w_age * age, total
        )

    # -- phases are groups of opposite approaches -----------------------------
    def group_score(self, group: list[int], now: float) -> Score:
        return max((self.score(a, now) for a in group), key=lambda sc: sc.total)

    def slow_bus_group(self, group: list[int]) -> BusTelemetry | None:
        slow = [t for a in group if (t := self.slow_bus(a)) is not None]
        return min(slow, key=lambda t: t.speed, default=None)

    def forecasts_for_group(self, group: list[int], now: float) -> list[Forecast]:
        return sorted((f for a in group for f in self.forecasts_for(a, now)), key=lambda f: f.eta)

    def plan_green(self, node: int, gi: int, now: float) -> tuple[float, str]:
        """Green duration for the phase that is about to start, and a note on why it differs."""
        cfg = self.cfg
        if not self.adaptive:
            return cfg.base_green, ""
        groups = self.controllers[node].groups
        weights = [max(0.0, self.group_score(g, now).total) + cfg.split_eps for g in groups]
        share = weights[gi] / sum(weights)
        even = 1.0 / len(groups)
        if abs(share - even) < cfg.split_deadband:
            return cfg.base_green, ""  # scores too close to call: keep the standard timer
        planned = max(cfg.min_green, min(cfg.max_green, cfg.base_green * len(groups) * share))
        return planned, f"score share {share:.0%}"

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
                f"n{node}: extension of {seconds:.1f}s denied (network budget {used:.0f}/{cfg.budget_seconds:.0f}s, "
                f"{len(recent_nb)} neighbour(s) just extended)",
            )
            return 0.0
        self._ext_log.append((now, node, granted))
        return granted


class IntersectionController:
    """Phase signal: opposite streets share a green; standard timer plus adaptive overrides."""

    def __init__(self, node: int, net: RoadNetwork, cfg: Config, coord: NetworkCoordinator, events: EventLog):
        self.node, self.net, self.cfg, self.coord, self.events = node, net, cfg, coord, events
        self.groups: list[list[int]] = net.phase_groups(node)
        self.approaches: list[int] = [a for g in self.groups for a in g]
        self._group_of = {a: gi for gi, g in enumerate(self.groups) for a in g}
        n = len(self.groups)
        self.idx = node % n  # which phase is being served
        self.phase = "GREEN"  # GREEN, YELLOW or ALLRED
        self.elapsed = float((node * 7) % int(cfg.base_green))  # desynchronise neighbouring junctions
        self.planned = cfg.base_green
        self.extended_by = 0.0
        self.reason = "BASE"
        self._became_red = {gi: (0.0 if gi != self.idx else math.inf) for gi in range(n)}
        self._cooldown_until = 0.0
        self._acc = 0.0
        self._used_forecasts: set[int] = set()
        self._next_override: int | None = None

    # -- queries -----------------------------------------------------------
    @property
    def current(self) -> list[int]:
        """The approaches that currently have (or are finishing) the green."""
        return self.groups[self.idx]

    def state_for(self, seg_id: int) -> str:
        if self._group_of.get(seg_id) != self.idx:
            return "R"
        return {"GREEN": "G", "YELLOW": "Y", "ALLRED": "R"}[self.phase]

    def red_time(self, seg_id: int, now: float) -> float:
        gi = self._group_of[seg_id]
        return 0.0 if gi == self.idx else max(0.0, now - self._became_red[gi])

    @property
    def remaining(self) -> float:
        return self.planned - self.elapsed

    # -- simulation step ---------------------------------------------------
    def step(self, now: float, dt: float) -> None:
        self.elapsed += dt
        if self.phase == "YELLOW":
            if self.elapsed >= self.cfg.yellow:
                self.phase, self.elapsed = "ALLRED", 0.0
                self._became_red[self.idx] = now
            return
        if self.phase == "ALLRED":
            if self.elapsed >= self.cfg.all_red:
                self._advance(now)
            return
        if self.coord.adaptive:
            self._acc += dt
            if self._acc >= self.cfg.decision_period:
                self._acc = 0.0
                self._decide(now)
        if self.elapsed >= max(self.planned, self.cfg.min_green):
            self.phase, self.elapsed = "YELLOW", 0.0

    def _advance(self, now: float) -> None:
        n = len(self.groups)
        nxt = (self.idx + 1) % n
        if self._next_override is not None:
            nxt = self._next_override
        self._next_override = None
        self.idx = nxt
        self.phase, self.elapsed, self.extended_by, self.reason = "GREEN", 0.0, 0.0, "BASE"
        self._became_red[self.idx] = math.inf
        self.planned, why = self.coord.plan_green(self.node, self.idx, now)
        self.planned = max(self.planned, self.cfg.min_green)
        if self.coord.adaptive and abs(self.planned - self.cfg.base_green) >= 3.0:
            self.reason = "SPLIT_ADJUST"
            if abs(self.planned - self.cfg.base_green) >= 7.0:  # keep the log readable
                self.events.add(
                    now,
                    "SPLIT_ADJUST",
                    f"{self.coord.label_group(self.current)} green {self.planned:.0f}s instead of "
                    f"{self.cfg.base_green:.0f}s ({why})",
                    node=self.node,
                )

    # -- adaptive decisions ----------------------------------------------------
    def _decide(self, now: float) -> None:
        cfg, coord, cur = self.cfg, self.coord, self.current
        name = coord.label_group(cur)
        slow = coord.slow_bus_group(cur)
        others = [(gi, g) for gi, g in enumerate(self.groups) if gi != self.idx]

        # 1. Local optimisation: a slow bus in the detection window earns more green.
        if slow is not None and now >= self._cooldown_until and self.remaining < cfg.ext_remaining:
            ratio = slow.speed / slow.free_speed
            want = cfg.ext_gain * (1.0 - ratio / cfg.v_slow_frac)
            self._extend(
                now,
                want,
                "LOCAL_EXTEND",
                f"bus {slow.bus_id} L{slow.line} at {slow.speed * 3.6:.0f} km/h, {slow.dist_to_next_node:.0f} m from line",
            )

        # 2. Feed-forward: forecasts for buses that have not arrived yet.
        for f in coord.forecasts_for_group(cur, now):
            if f.id in self._used_forecasts or f.predicted_density < cfg.pred_density_min or f.confidence < 0.3:
                continue
            until = f.eta - now
            if until <= cfg.lead_window and self.remaining < until + 3.0:
                self._used_forecasts.add(f.id)
                self._extend(
                    now,
                    cfg.pred_ext,
                    "PREDICTIVE_EXTEND",
                    f"forecast for L{f.line} {f.source_bus}: +{f.predicted_delay:.0f}s delay, ETA {until:.0f}s, "
                    f"conf {f.confidence:.2f}",
                )
                break

        if self.elapsed >= cfg.min_green and slow is None and self.remaining > 1.0:
            for gi, g in others:  # a dense forecast on another street: serve it next
                for f in coord.forecasts_for_group(g, now):
                    if f.id in self._used_forecasts or f.predicted_density < cfg.pred_density_min or f.confidence < 0.3:
                        continue
                    until = f.eta - now
                    if until <= cfg.lead_window:
                        self._used_forecasts.add(f.id)
                        self._next_override = gi
                        self.events.add(
                            now,
                            "PREDICTIVE_SWITCH",
                            f"{name} green cut short: forecast +{f.predicted_delay:.0f}s delay for L{f.line} "
                            f"{f.source_bus} on {coord.label_group(g)} in {until:.0f}s",
                            node=self.node,
                        )
                        self.planned = self.elapsed
                        return

            # 3. Dynamic weighting: end this green early if the next phase clearly needs it more.
            nxt = self.groups[(self.idx + 1) % len(self.groups)]
            mine, theirs = coord.group_score(cur, now), coord.group_score(nxt, now)
            if theirs.total - mine.total >= cfg.early_switch_margin:
                self.events.add(
                    now,
                    "EARLY_SWITCH",
                    f"{name} green ended early: {coord.label_group(nxt)} score {theirs.total:.2f} vs {mine.total:.2f}",
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
        self.events.add(now, kind, f"{self.coord.label_group(self.current)} +{granted:.1f}s ({detail})", node=self.node)
