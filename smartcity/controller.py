"""Signal control: per-intersection phase machines and the network-wide coordinator.

Each signalised intersection serves one *phase* at a time: approaches whose headings are roughly
opposite are green together (and red together), an unpaired street has a phase of its own.
SUMO-style safety rules apply: a green never lasts less than `min_green` (7 s), then comes a 3 s
yellow and a 2 s all-red clearance before the conflicting phase starts.

Only the buses are observed, never the cars, so a green is **never ended early**. It can only be
*extended*, and only on evidence from buses:

* at the start of every green (once per red-green cycle of that light) the controller asks how far
  each bus heading for it covered during the cycle that just ended; if even the worst-held bus got
  less than a small share of the free-flow distance, this green is lengthened;
* a forecast that a bus about to arrive will make low progress (feed-forward from the AI module)
  can lengthen the green that is starting before the bus has even been seen to stall.

The coordinator also holds the network-wide pieces: edge and junction loads (walking the road graph
to the junction at the end of each release), a throttle on how fast each approach releases cars so a
bottleneck is not simply moved downstream, and a budget on extensions.
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
from .progress import RouteProgress

JUNCTION_WEIGHT = 0.85  # how much of a downstream junction's congestion counts against releasing into it


@dataclass(frozen=True)
class Evidence:
    """The worst-held bus heading for a junction, and how little it moved during the last cycle."""

    bus: BusTelemetry
    approach: int
    travelled: float  # m along its route during the cycle
    expected: float  # m it would have covered at the speed limit
    share: float  # travelled / expected


class NetworkCoordinator:
    def __init__(
        self,
        net: RoadNetwork,
        cfg: Config,
        estimator: CongestionEstimator,
        predictor: PostIntersectionPredictor,
        loops: DetectionLoops,
        progress: RouteProgress,
        events: EventLog,
        adaptive: bool = True,
    ):
        self.net, self.cfg = net, cfg
        self.estimator, self.predictor, self.loops, self.progress = estimator, predictor, loops, progress
        self.events = events
        self.adaptive = adaptive
        self.controllers: dict[int, IntersectionController] = {}
        n = len(net.segments)
        self.est_load = [cfg.est_prior_load] * n  # bus telemetry: per-edge EWMA and detection loops
        self.fc_load = [0.0] * n  # from AI forecasts only
        self.load = [cfg.est_prior_load] * n  # what the throttle acts on
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

    # -- evidence from buses ------------------------------------------------------
    def cycle_evidence(self, group: list[int], span: float, now: float) -> Evidence | None:
        """The bus heading for this phase that covered the smallest share of free-flow distance in `span` s.

        Only buses within `cycle_window` metres of a stop line of the phase, whose own route continues
        onto it, are considered, and only if they are held to under `cycle_low_progress`.
        """
        cfg = self.cfg
        worst: Evidence | None = None
        for bus_id, (tel, approach) in self.loops.buses_near(group, cfg.cycle_window).items():
            done = self.progress.travelled(bus_id, now, span)
            if done is None:
                continue  # not observed for a whole cycle yet
            expected = span * tel.free_speed
            share = done / expected if expected > 0 else 1.0
            if share < cfg.cycle_low_progress and (worst is None or share < worst.share):
                worst = Evidence(tel, approach, done, expected, share)
        return worst

    def forecasts_for(self, seg_id: int, now: float) -> list[Forecast]:
        return sorted((f for f in self.predictor.incoming(seg_id, now) if f.eta >= now), key=lambda f: f.eta)

    def forecasts_for_group(self, group: list[int], now: float) -> list[Forecast]:
        return sorted((f for a in group for f in self.forecasts_for(a, now)), key=lambda f: f.eta)

    def downstream_openness(self, approach: int) -> float:
        """1 when the edges an approach releases into are clear, 0 when they are blocked (no point extending)."""
        cfg = self.cfg
        return 1.0 - max(0.0, min(1.0, (self.dload[approach] - cfg.rho_gate) / (cfg.rho_block - cfg.rho_gate)))

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
    """Phase signal: opposite streets share a green; the standard timer, extended on bus evidence only."""

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
        self._last_green = {self.idx: -self.elapsed}  # when each phase last turned green
        self._used_forecasts: set[int] = set()

    # -- queries -----------------------------------------------------------
    @property
    def current(self) -> list[int]:
        """The approaches that currently have (or are finishing) the green."""
        return self.groups[self.idx]

    def state_for(self, seg_id: int) -> str:
        if self._group_of.get(seg_id) != self.idx:
            return "R"
        return {"GREEN": "G", "YELLOW": "Y", "ALLRED": "R"}[self.phase]

    # -- simulation step ---------------------------------------------------
    def step(self, now: float, dt: float) -> None:
        self.elapsed += dt
        if self.phase == "YELLOW":
            if self.elapsed >= self.cfg.yellow:
                self.phase, self.elapsed = "ALLRED", 0.0
        elif self.phase == "ALLRED":
            if self.elapsed >= self.cfg.all_red:
                self._start_green(now)
        elif self.elapsed >= self.planned:  # a green only ever ends when its (possibly extended) time is up
            self.phase, self.elapsed = "YELLOW", 0.0

    def _start_green(self, now: float) -> None:
        self.idx = (self.idx + 1) % len(self.groups)
        self.phase, self.elapsed, self.extended_by = "GREEN", 0.0, 0.0
        self.planned = self.cfg.base_green  # the standard timer; never shorter
        previous = self._last_green.get(self.idx)
        self._last_green[self.idx] = now
        if self.coord.adaptive:
            self._review_buses(now, None if previous is None else now - previous)

    # -- the per-cycle bus check ---------------------------------------------------
    def _review_buses(self, now: float, cycle: float | None) -> None:
        """Decide, once per red-green cycle of this light, whether to lengthen the green that is starting."""
        cfg, coord, group = self.cfg, self.coord, self.current
        if cycle is not None and cycle >= cfg.min_cycle:
            ev = coord.cycle_evidence(group, cycle, now)
            if ev is not None:
                want = cfg.cycle_ext_max * (1.0 - ev.share / cfg.cycle_low_progress)
                want *= coord.downstream_openness(ev.approach)  # no point extending into a jammed street
                self._extend(
                    now,
                    want,
                    "LOCAL_EXTEND",
                    f"bus {ev.bus.bus_id} L{ev.bus.line} covered {ev.travelled:.0f} m in the last {cycle:.0f} s cycle "
                    f"({ev.share:.0%} of free flow)",
                )
        # Feed-forward: a bus due soon that is forecast to make low progress.
        for f in coord.forecasts_for_group(group, now):
            if f.id in self._used_forecasts or f.predicted_density < cfg.pred_density_min or f.confidence < 0.3:
                continue
            if f.eta - now <= cfg.pred_horizon:
                self._used_forecasts.add(f.id)
                self._extend(
                    now,
                    cfg.pred_ext,
                    "PREDICTIVE_EXTEND",
                    f"forecast for L{f.line} {f.source_bus}: +{f.predicted_delay:.0f}s delay, ETA {f.eta - now:.0f}s, "
                    f"conf {f.confidence:.2f}",
                )
                break

    def _extend(self, now: float, seconds: float, kind: str, detail: str) -> None:
        room = self.cfg.max_green - self.planned
        want = min(seconds, room)
        if want < 1.0:
            return
        granted = self.coord.request_extension(self.node, want, now)
        if granted <= 0.0:
            return
        self.planned += granted
        self.extended_by += granted
        self.events.add(now, kind, f"{self.coord.label_group(self.current)} +{granted:.1f}s ({detail})", node=self.node)
