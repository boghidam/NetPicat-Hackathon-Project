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

* **Across runs, persistent** (`load_memory`, `save_memory`, `merge_memories`, `train_policy`). Each junction's
  learned trust, decision counts, outcome tallies (`decisive`, `wasted`, `missed`), and bottleneck discharge
  phase bonuses are persisted in `data/learned_policy.json`, so every comparison or training session continues
  where the last one left off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config

if TYPE_CHECKING:
    from .controller import IntersectionController
    from .progress import RouteProgress

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "learned_policy.json"

LEARNED_KINDS = ("BUS_HOLD", "LOCAL_EXTEND")
MIN_TRUST, MAX_TRUST = 0.40, 1.25

DEFAULT_TRUST: dict[str, float] = {
    "BUS_HOLD": 1.0,
    "LOCAL_EXTEND": 0.45,
}

# Learned discharge-bottleneck phase bonuses (seconds added when downstream is open) on the Iași network.
DEFAULT_IASI_PHASES: dict[int, list[float]] = {
    3: [18.0, 0.0],
    16: [0.0, 14.0],
    41: [18.0, 0.0],
    58: [18.0, 0.0],
    66: [14.0, 0.0],
    105: [18.0, 0.0],
    111: [0.0, 14.0, 0.0],
    132: [0.0, 18.0],
    175: [0.0, 14.0],
    195: [22.0, 0.0],
    196: [0.0, 20.0, 0.0],
    198: [22.0, 0.0],
    235: [12.0, 0.0],
    239: [0.0, 22.0],
}


def default_iasi_memory() -> dict:
    """Initial learned policy for the Iași network."""
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
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("map", "iasi") == map_name:
                return data
        except (OSError, ValueError):
            pass
    if map_name == "iasi":
        return default_iasi_memory()
    return None


def save_memory(memory: dict, path: Path = POLICY_PATH) -> None:
    """Persist the learned memory to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, indent=2) + "\n", encoding="utf-8")


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
        mem = memory or {}
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
            # Stationary TD(0) update toward the empirical payoff target for this junction
            target = max(MIN_TRUST, min(MAX_TRUST, base + 0.6 * reward))
            step = cfg.learn_rate * (target - cur) if self.map_name == "iasi" else cfg.learn_rate * reward
            row[d.kind] = max(MIN_TRUST, min(MAX_TRUST, cur + step))
            self.counts[ctl.node] = self.counts.get(ctl.node, 0) + 1

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
            "phase_bonus": {str(n): [round(x, 2) for x in row] for n, row in sorted(self.phases.items())},
            "trust": {str(n): {k: round(v, 4) for k, v in row.items()} for n, row in sorted(self.trusts.items())},
            "count": {str(n): c for n, c in sorted(self.counts.items())},
            "outcomes": total_outcomes,
        }


def merge_memories(memories: list[dict]) -> dict:
    """Average what several runs learned about each junction, weighting each run by how much it saw there."""
    if not memories:
        return {"trust": {}, "count": {}}
    base = memories[0]
    trust: dict[str, dict[str, list[tuple[float, float]]]] = {}
    count: dict[str, int] = {}
    for mem in memories:
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
        merged["episodes"] = max(int(m.get("episodes", 0)) for m in memories)
        merged["default_trust"] = dict(base.get("default_trust") or DEFAULT_TRUST)
        merged["phase_bonus"] = dict(base.get("phase_bonus") or {})
        merged["outcomes"] = {
            kind: {
                o: max(int(((m.get("outcomes") or {}).get(kind) or {}).get(o, 0)) for m in memories)
                for o in ("decisive", "wasted", "missed")
            }
            for kind in LEARNED_KINDS
        }
    return merged


def train_policy(
    episodes: int = 3,
    seeds_per_ep: int = 2,
    seconds: float = 600.0,
    map_name: str = "iasi",
    path: Path = POLICY_PATH,
) -> dict:
    """Run multi-episode online reinforcement learning on training seeds and persist the updated policy."""
    from .data import build_city
    from .simulation import Simulation

    cfg = Config()
    net = build_city(map_name, cfg)
    mem = load_memory(path, map_name) or default_iasi_memory()
    base_ep = int(mem.get("episodes", 0))
    print(f"Training adaptive policy on {map_name} ({episodes} episode(s) x {seeds_per_ep} seeds x {seconds:.0f}s)...")
    for ep in range(episodes):
        ep_memories: list[dict] = []
        f_bus = a_bus = f_car = a_car = 0.0
        dec = wst = mis = 0
        for k in range(seeds_per_ep):
            seed = 101 + (base_ep + ep) * seeds_per_ep + k
            sim_f = Simulation(Config(seed=seed), adaptive=False, incident_at=120.0, net=net)
            sim_f.run(seconds)
            mf = sim_f.metrics()
            f_bus += mf["bus_delay_ratio"]
            f_car += mf["car_speed"]

            sim_a = Simulation(Config(seed=seed), adaptive=True, incident_at=120.0, net=net, memory=mem)
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
        if ep_memories:
            mem = merge_memories(ep_memories)
            # Accumulate outcome deltas across all seeds in the episode
            save_memory(mem, path)
        n = seeds_per_ep
        print(
            f"  ep {ep + 1}/{episodes}: bus lost {f_bus / n:.1%} -> {a_bus / n:.1%} "
            f"| car speed {f_car / n:.1%} -> {a_car / n:.1%} "
            f"| judged: {dec} decisive, {wst} wasted, {mis} missed"
        )
    return mem
