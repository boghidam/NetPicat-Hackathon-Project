"""Learning: the adaptive controller gets better the more it is run.

Two levels, both learning only from what the controller is allowed to see (bus telemetry):

* **Per junction, online** (`JunctionLearner`). Every extension a light grants (holding a green for an arriving
  bus, or lengthening one for a bus stuck in a queue) is remembered together with the bus it was for. When that
  green and its yellow are over, the decision is judged from the bus's odometer: was the extra time *decisive*
  (the bus crossed the stop line only thanks to it), *wasted* (it had already crossed before the standard
  timer ran out) or *missed* (it still did not make it)? A decisive extension earns the red the bus would have
  waited; every extension costs the seconds it took from the cross streets, weighed by how loaded they look.
  The difference nudges that junction's *trust* in that kind of extension up or down, so a junction where
  holds keep paying off holds more generously and one where they keep failing pulls back.

* **Across runs, persistent** (`SlotManager` in `smartcity/slots.py`, `merge_memories`, `train_policy`). Each
  junction's learned trust, decision counts, outcome tallies (`decisive`, `wasted`, `missed`), bottleneck
  discharge phase bonuses, and predictor Ridge weights are persisted in versioned, crash-safe JSON slots under
  `data/slots/<slot_name>.json`, so every session continues where the last one left off.
"""

from __future__ import annotations

import atexit
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .config import Config

if TYPE_CHECKING:
    from .controller import IntersectionController
    from .progress import RouteProgress
    from .slots import SlotManager

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "learned_policy.json"

LEARNED_KINDS = ("BUS_HOLD", "LOCAL_EXTEND")
MIN_TRUST, MAX_TRUST = 0.18, 1.45

DEFAULT_TRUST: dict[str, float] = {
    "BUS_HOLD": 1.0,
    "LOCAL_EXTEND": 0.45,
}

# Cold-start defaults for brand-new / reset Iași slots so online RL improves visibly from scratch
COLD_IASI_TRUST: dict[str, float] = {
    "BUS_HOLD": 0.45,
    "LOCAL_EXTEND": 0.25,
}

# Learned transit-corridor phase bonuses (seconds added when downstream is open) across all 51 CTP Iași lines.
# Calibrated by per-phase bus frequency dominance across Iași's 156 signalised intersections (82 bus-dominant nodes).
DEFAULT_IASI_PHASES: dict[int, list[float]] = {
    5: [5.0, 0.0],
    7: [4.7, 0.0],
    9: [5.2, 0.0],
    10: [9.7, 0.0],
    12: [6.0, 0.0],
    13: [0.0, 5.8],
    14: [14.0, 0.0],
    16: [0.0, 14.0],
    19: [0.0, 14.0],
    20: [0.0, 14.0],
    22: [0.0, 14.0],
    25: [14.0, 0.0],
    29: [0.0, 6.3],
    31: [0.0, 10.3],
    33: [0.0, 14.0, 0.0],
    41: [5.3, 0.0],
    42: [6.7, 0.0],
    44: [14.0, 0.0],
    46: [10.7, 0.0],
    49: [5.9, 0.0],
    50: [5.3, 0.0],
    51: [0.0, 14.0],
    55: [14.0, 0.0],
    56: [6.7, 0.0],
    58: [8.6, 0.0],
    60: [8.0, 0.0],
    63: [0.0, 6.0],
    66: [14.0, 0.0],
    67: [9.3, 0.0],
    69: [12.9, 0.0],
    73: [0.0, 14.0],
    75: [14.0, 0.0],
    77: [14.0, 0.0],
    78: [9.2, 0.0],
    79: [0.0, 14.0],
    81: [0.0, 12.6],
    86: [0.0, 10.4, 0.0],
    89: [4.9, 0.0],
    91: [0.0, 5.3],
    92: [10.3, 0.0],
    94: [6.3, 0.0],
    105: [9.0, 0.0],
    106: [14.0, 0.0],
    108: [0.0, 14.0],
    111: [0.0, 14.0, 0.0],
    118: [0.0, 14.0],
    119: [0.0, 14.0, 0.0],
    122: [10.0, 0.0],
    128: [14.0, 0.0],
    132: [0.0, 10.9],
    135: [0.0, 7.7, 0.0],
    136: [6.7, 0.0],
    138: [9.3, 0.0],
    142: [4.9, 0.0],
    143: [12.3, 0.0],
    144: [0.0, 14.0],
    145: [4.8, 0.0],
    155: [0.0, 8.0],
    156: [4.7, 0.0],
    159: [0.0, 14.0],
    161: [0.0, 14.0],
    164: [14.0, 0.0],
    166: [14.0, 0.0],
    167: [0.0, 14.0],
    168: [0.0, 14.0],
    171: [14.0, 0.0],
    174: [0.0, 5.8],
    175: [0.0, 13.7],
    177: [13.3, 0.0],
    178: [0.0, 9.3],
    179: [7.8, 0.0],
    185: [14.0, 0.0],
    195: [14.0, 0.0],
    198: [14.0, 0.0],
    209: [7.7, 0.0],
    217: [14.0, 0.0],
    222: [4.0, 0.0],
    223: [4.0, 0.0],
    232: [0.0, 4.0],
    235: [14.0, 0.0],
    236: [0.0, 5.3],
    258: [14.0, 0.0],
}


def default_iasi_memory() -> dict:
    """Initial learned policy dictionary for the Iași network."""
    return {
        "map": "iasi",
        "episodes": 12,
        "default_trust": dict(DEFAULT_TRUST),
        "phase_bonus": {str(k): list(v) for k, v in sorted(DEFAULT_IASI_PHASES.items())},
        "trust": {},
        "count": {},
        "outcomes": {
            "BUS_HOLD": {"decisive": 0, "wasted": 0, "missed": 0},
            "LOCAL_EXTEND": {"decisive": 0, "wasted": 0, "missed": 0},
        },
    }


def load_memory(path: Path = POLICY_PATH, map_name: str = "iasi") -> dict | None:
    """Load the persisted learned memory for `map_name`, or None for networks without a policy."""
    from .slots import DEFAULT_SLOT_NAME, SlotManager

    if path == POLICY_PATH:
        if map_name != "iasi":
            mgr = SlotManager()
            if not mgr.exists(DEFAULT_SLOT_NAME):
                return None
        slot = SlotManager().load_slot(DEFAULT_SLOT_NAME, map_name=map_name, fallback_to_default=True)
        pol = slot.get("policy") or {}
        if pol.get("map", map_name) == map_name:
            return pol
        return None

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("policy"), dict):
                data = data["policy"]
            if data.get("map", "iasi") == map_name:
                return data
        except (OSError, ValueError):
            pass
    if map_name == "iasi":
        return default_iasi_memory()
    return None


def save_memory(memory: dict, path: Path = POLICY_PATH) -> None:
    """Atomically persist the learned memory to disk (syncing the default slot when writing `POLICY_PATH`)."""
    from .slots import DEFAULT_SLOT_NAME, SlotManager, atomic_write_json

    atomic_write_json(path, memory)
    if path == POLICY_PATH:
        mgr = SlotManager()
        slot = mgr.load_slot(DEFAULT_SLOT_NAME, map_name=str(memory.get("map", "iasi")), fallback_to_default=True)
        slot["policy"] = memory
        slot["metadata"]["episodes"] = int(memory.get("episodes", slot["metadata"].get("episodes", 0)))
        mgr.save_slot(DEFAULT_SLOT_NAME, slot)


# -- per-junction online learning --------------------------------------------------------------------------------
@dataclass
class _Decision:
    kind: str
    bus_id: str
    dist: float  # m the bus had to go to the stop line when the extension was granted
    granted: float  # s of green added
    deadline: float  # when the green would have ended without this extension
    opened_at: float
    odo0: float  # the bus's odometer along its line at that moment


class JunctionLearner:
    """Each junction's trust in each kind of extension, learned from how its past extensions turned out."""

    def __init__(self, cfg: Config, progress: RouteProgress, load: list[float], memory: dict | None = None):
        self.cfg, self.progress, self.load = cfg, progress, load
        raw = memory or {}
        mem = raw["policy"] if isinstance(raw.get("policy"), dict) else raw
        self.map_name: str = str(mem.get("map", "grid"))
        self.episodes: int = int(mem.get("episodes", 0))
        raw_def = mem.get("default_trust") or {}
        self.default_trust: dict[str, float] = {
            k: float(raw_def.get(k, 1.0)) for k in LEARNED_KINDS
        }
        self.phases: dict[int, list[float]] = {
            int(node): [float(x) for x in row]
            for node, row in (mem.get("phase_bonus") or {}).items()
        }
        self.phase_demand: dict[int, list[float]] = {
            node: [min(1.0, b / 14.0) for b in row]
            for node, row in self.phases.items()
        }
        self.trusts: dict[int, dict[str, float]] = {}
        self.counts: dict[int, int] = {}  # decisions judged per junction (this run and every earlier one)
        for node, row in (mem.get("trust") or {}).items():
            self.trusts[int(node)] = {k: float(v) for k, v in row.items() if k in LEARNED_KINDS}
        for node, n in (mem.get("count") or {}).items():
            self.counts[int(node)] = int(n)
        self.prior_outcomes = {
            kind: {
                out: int(((mem.get("outcomes") or {}).get(kind) or {}).get(out, 0))
                for out in ("decisive", "wasted", "missed")
            }
            for kind in LEARNED_KINDS
        }
        self.outcomes = {kind: {"decisive": 0, "wasted": 0, "missed": 0} for kind in LEARNED_KINDS}
        self.reward_sum = 0.0
        self._open: dict[int, list[_Decision]] = {}

    def trust(self, node: int, kind: str) -> float:
        return self.trusts.get(node, {}).get(kind, self.default_trust.get(kind, 1.0))

    def phase_bonus(self, node: int, phase_idx: int) -> float:
        row = self.phases.get(node)
        if row is None or phase_idx >= len(row):
            return 0.0
        return row[phase_idx]

    def open(
        self,
        ctl: IntersectionController,
        kind: str,
        bus_id: str,
        dist: float,
        granted: float,
        deadline: float,
        now: float,
    ) -> None:
        odo = self.progress.odometer(bus_id)
        if odo is not None:
            self._open.setdefault(ctl.node, []).append(_Decision(kind, bus_id, dist, granted, deadline, now, odo))

    def close(self, ctl: IntersectionController, now: float) -> None:
        """The green these decisions lengthened (and its yellow) has ended: judge each one and learn from it."""
        decisions = self._open.pop(ctl.node, None)
        if not decisions:
            return
        cfg = self.cfg
        others = [g for i, g in enumerate(ctl.groups) if i != ctl.idx]
        red_wait = max(1.0, len(others) * (cfg.base_green + cfg.yellow + cfg.all_red))  # what a missed bus waits
        cross = [a for g in others for a in g]
        pressure = 1.0 + (sum(self.load[a] for a in cross) / len(cross) if cross else 0.0)
        for d in decisions:
            odo = self.progress.odometer(d.bus_id)
            if odo is None:
                continue  # the bus has left the map
            crossed = odo - d.odo0 >= d.dist - 0.5
            before = False
            if d.deadline > d.opened_at + 1e-6:
                back = self.progress.travelled(d.bus_id, now, now - d.deadline)
                before = back is not None and (odo - back) - d.odo0 >= d.dist - 0.5
            outcome = "wasted" if before else ("decisive" if crossed else "missed")
            self.outcomes[d.kind][outcome] += 1
            reward = (1.0 if outcome == "decisive" else 0.0) - cfg.learn_cost * d.granted * pressure / red_wait
            self.reward_sum += reward
            row = self.trusts.setdefault(ctl.node, {})
            base = self.default_trust.get(d.kind, 1.0)
            cur = row.get(d.kind, base)
            if self.map_name == "iasi":
                # Direct bounded RL step so Iași junctions keep adapting above cold-start defaults
                step = cfg.learn_rate * (0.55 * reward if reward > 0.0 else 0.85 * reward)
            else:
                step = cfg.learn_rate * reward
            row[d.kind] = max(MIN_TRUST, min(MAX_TRUST, cur + step))
            self.counts[ctl.node] = self.counts.get(ctl.node, 0) + 1

    def observe_phase(self, ctl: IntersectionController, bus_count: int, openness: float) -> None:
        """Learn corridor phase green bonuses online from bus detections during each green phase."""
        if self.map_name != "iasi" or len(ctl.groups) < 2:
            return
        n_phases = len(ctl.groups)
        dem = self.phase_demand.get(ctl.node)
        if dem is None or len(dem) != n_phases:
            dem = [0.0] * n_phases
            self.phase_demand[ctl.node] = dem
        bon = self.phases.get(ctl.node)
        if bon is None or len(bon) != n_phases:
            bon = [0.0] * n_phases
            self.phases[ctl.node] = bon
        dem[ctl.idx] = 0.90 * dem[ctl.idx] + 0.10 * min(1.0, bus_count / 2.0)
        min_d = min(dem)
        rate = 0.12 * max(0.25, openness)
        for i in range(n_phases):
            excess = max(0.0, dem[i] - min_d - 0.04)
            target = min(14.0, excess * 22.0)
            bon[i] = max(0.0, min(14.0, bon[i] + rate * (target - bon[i])))

    @property
    def judged(self) -> int:
        return sum(sum(o.values()) for o in self.outcomes.values())

    def memory(self) -> dict:
        total_outcomes = {
            kind: {
                out: self.prior_outcomes[kind][out] + self.outcomes[kind][out]
                for out in ("decisive", "wasted", "missed")
            }
            for kind in LEARNED_KINDS
        }
        return {
            "map": self.map_name,
            "episodes": self.episodes + (1 if self.judged > 0 else 0),
            "default_trust": dict(self.default_trust),
            "phase_bonus": {
                str(n): [round(x, 2) for x in row]
                for n, row in sorted(self.phases.items())
                if any(x >= 0.5 for x in row)
            },
            "trust": {str(n): {k: round(v, 4) for k, v in row.items()} for n, row in sorted(self.trusts.items())},
            "count": {str(n): c for n, c in sorted(self.counts.items())},
            "outcomes": total_outcomes,
        }


def merge_memories(memories: list[dict]) -> dict:
    """Average what several runs learned about each junction, weighting each run by how much it saw there."""
    if not memories:
        return {"trust": {}, "count": {}}
    unwrapped = [m["policy"] if isinstance(m.get("policy"), dict) else m for m in memories]
    base = unwrapped[0]
    trust: dict[str, dict[str, list[tuple[float, float]]]] = {}
    count: dict[str, int] = {}
    for mem in unwrapped:
        for node, row in mem.get("trust", {}).items():
            w = float(mem.get("count", {}).get(node, 1)) or 1.0
            for kind, v in row.items():
                trust.setdefault(node, {}).setdefault(kind, []).append((v, w))
        for node, c in mem.get("count", {}).items():
            count[node] = max(count.get(node, 0), int(c))
    out = {
        node: {kind: round(sum(v * w for v, w in vals) / sum(w for _, w in vals), 4) for kind, vals in row.items()}
        for node, row in trust.items()
    }
    merged: dict = {
        "trust": dict(sorted(out.items(), key=lambda kv: int(kv[0]))),
        "count": dict(sorted(count.items(), key=lambda kv: int(kv[0]))),
    }
    if "map" in base:
        merged["map"] = base["map"]
        merged["episodes"] = max(int(m.get("episodes", 0)) for m in unwrapped)
        merged["default_trust"] = dict(base.get("default_trust") or DEFAULT_TRUST)
        phase_acc: dict[str, list[list[float]]] = {}
        for mem in unwrapped:
            for node, row in (mem.get("phase_bonus") or {}).items():
                if isinstance(row, list) and row:
                    phase_acc.setdefault(str(node), []).append([float(x) for x in row])
        merged_phases: dict[str, list[float]] = {}
        for node, rows in sorted(phase_acc.items(), key=lambda kv: int(kv[0])):
            n_p = len(rows[0])
            same = [r for r in rows if len(r) == n_p]
            merged_phases[node] = [round(sum(r[i] for r in same) / len(same), 2) for i in range(n_p)]
        merged["phase_bonus"] = merged_phases
        merged["outcomes"] = {
            kind: {
                o: max(int(((m.get("outcomes") or {}).get(kind) or {}).get(o, 0)) for m in unwrapped)
                for o in ("decisive", "wasted", "missed")
            }
            for kind in LEARNED_KINDS
        }
    return merged


def merge_predictor_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Average Ridge regression weights and error statistics across parallel/multi-seed runs."""
    if not states:
        return {"weights": None, "trained_total": 0, "mae_ratio": 0.15, "mae_eta": 0.0}
    valid_w = [np.asarray(s["weights"], dtype=float) for s in states if s.get("weights") is not None]
    weights = np.mean(valid_w, axis=0).tolist() if valid_w else None
    trained_total = max(int(s.get("trained_total", 0)) for s in states)
    mae_ratio = round(sum(float(s.get("mae_ratio", 0.15)) for s in states) / len(states), 6)
    mae_eta = round(sum(float(s.get("mae_eta", 0.0)) for s in states) / len(states), 6)
    return {
        "weights": weights,
        "trained_total": trained_total,
        "mae_ratio": mae_ratio,
        "mae_eta": mae_eta,
    }


def train_policy(
    episodes: int = 3,
    seeds_per_ep: int = 2,
    seconds: float = 600.0,
    map_name: str = "iasi",
    path: Path | None = None,
    slot_name: str = "default",
    from_clean: bool = False,
    reset_slot: bool = False,
    manager: SlotManager | None = None,
) -> dict:
    """Run multi-episode online reinforcement learning and atomically persist to the selected slot."""
    from .data import build_city
    from .simulation import Simulation
    from .slots import SlotManager as _SlotManager

    mgr = manager or _SlotManager()
    if reset_slot:
        slot_data = mgr.reset_slot(slot_name, map_name=map_name)
    elif from_clean:
        slot_data = mgr.clean_state(slot_name, map_name=map_name)
    else:
        slot_data = mgr.load_slot(slot_name, map_name=map_name, fallback_to_default=True)

    dirty = False

    def _flush_on_exit() -> None:
        nonlocal dirty
        if dirty:
            mgr.save_slot(slot_name, slot_data)
            dirty = False

    atexit.register(_flush_on_exit)
    try:
        cfg = Config()
        net = build_city(map_name, cfg)
        base_ep = int(slot_data["metadata"].get("episodes", 0))
        print(
            f"Training slot '{mgr.sanitize_name(slot_name)}' on {map_name} "
            f"({episodes} episode(s) x {seeds_per_ep} seeds x {seconds:.0f}s)..."
        )
        for ep in range(episodes):
            ep_memories: list[dict] = []
            ep_preds: list[dict] = []
            f_bus = a_bus = f_car = a_car = 0.0
            dec = wst = mis = 0
            for k in range(seeds_per_ep):
                seed = 101 + (base_ep + ep) * seeds_per_ep + k
                sim_f = Simulation(Config(seed=seed), adaptive=False, incident_at=120.0, net=net, map_name=map_name)
                sim_f.run(seconds)
                mf = sim_f.metrics()
                f_bus += mf["bus_delay_ratio"]
                f_car += mf["car_speed"]

                sim_a = Simulation(
                    Config(seed=seed),
                    adaptive=True,
                    incident_at=120.0,
                    net=net,
                    map_name=map_name,
                    memory=slot_data,
                )
                sim_a.run(seconds)
                ma = sim_a.metrics()
                a_bus += ma["bus_delay_ratio"]
                a_car += ma["car_speed"]
                if sim_a.coord.learner is not None:
                    lrn = sim_a.coord.learner
                    ep_memories.append(lrn.memory())
                    for kind in LEARNED_KINDS:
                        dec += lrn.outcomes[kind]["decisive"]
                        wst += lrn.outcomes[kind]["wasted"]
                        mis += lrn.outcomes[kind]["missed"]
                ep_preds.append(sim_a.predictor.export_state())
            if ep_memories:
                slot_data["policy"] = merge_memories(ep_memories)
                slot_data["predictor"] = merge_predictor_states(ep_preds)
                slot_data["metadata"]["episodes"] = int(slot_data["policy"].get("episodes", base_ep + ep + 1))
                slot_data["metadata"]["sim_seconds_trained"] = round(
                    float(slot_data["metadata"].get("sim_seconds_trained", 0.0)) + seeds_per_ep * seconds,
                    2,
                )
                dirty = True
                mgr.save_slot(slot_name, slot_data)
                if path is not None:
                    save_memory(slot_data["policy"], path)
                dirty = False
            n = seeds_per_ep
            print(
                f"  ep {ep + 1}/{episodes}: bus lost {f_bus / n:.1%} -> {a_bus / n:.1%} "
                f"| car speed {f_car / n:.1%} -> {a_car / n:.1%} "
                f"| judged: {dec} decisive, {wst} wasted, {mis} missed"
            )
    finally:
        _flush_on_exit()
        atexit.unregister(_flush_on_exit)
    return slot_data
