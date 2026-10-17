"""Headless closed-loop scenarios: physical rules hold, and an incident is handled better when adaptive."""

import pytest

from smartcity.config import Config
from smartcity.simulation import Simulation

SEEDS = (1, 2, 3, 4)
SECONDS = 600


def run(seed: int, adaptive: bool, demand: float | None = None):
    cfg = Config(seed=seed) if demand is None else Config(seed=seed, gate_demand=demand)
    sim = Simulation(cfg, adaptive=adaptive, incident_at=120.0)
    roads = [s.id for s in sim.grid.segments if s.kind == "road"]
    jam = 0.0  # link-seconds with at least half the link occupied
    for _ in range(SECONDS // 5):
        sim.run(5)
        jam += 5 * sum(sim.model.density(i) > 0.5 for i in roads)
    return sim, jam


@pytest.mark.parametrize("adaptive", [False, True])
def test_vehicles_never_pass_a_red_stop_line_or_overlap(adaptive):
    sim = Simulation(Config(seed=2), adaptive=adaptive, incident_at=100.0)
    grid = sim.grid
    before: dict[str, tuple[int, float]] = {}
    for _ in range(int(400 / sim.cfg.dt)):
        sim.step()
        for seg in grid.segments:
            lane = sim.model.lanes[seg.id]
            for ahead, behind in zip(lane, lane[1:], strict=False):
                assert behind.pos <= ahead.pos - ahead.length + 1e-6, "vehicles overlap"
            for v in lane:
                prev = before.get(v.id)
                if prev and prev[0] == seg.id and prev[1] <= seg.stop_pos < v.pos and seg.signalised:
                    assert sim.coord.signal_state(seg.id) in ("G", "Y"), f"{v.id} ran a red on seg{seg.id}"
                before[v.id] = (seg.id, v.pos)


def test_junction_box_is_clear_when_a_new_street_turns_green():
    """Yellow + all-red must empty the box of anything that could conflict with the next street."""
    sim = Simulation(Config(seed=4), adaptive=False)
    grid, cfg = sim.grid, sim.cfg
    last = {n: (c.current, c.phase) for n, c in sim.controllers.items()}
    checked = still_entering = slow_leavers = 0
    for _ in range(int(600 / cfg.dt)):
        sim.step()
        for n, c in sim.controllers.items():
            now = (c.current, c.phase)
            if now[1] == "GREEN" and last[n][1] == "ALLRED":
                checked += 1
                for a in c.approaches:  # vehicles between a stop line and the centre
                    seg = grid.segments[a]
                    still_entering += sum(1 for v in sim.model.lanes[a] if v.pos > seg.stop_pos + 0.5)
                for sid in grid.out_seg[n]:  # vehicles in the far half of the box, on their way out
                    if sid is not None:
                        slow_leavers += sum(
                            1
                            for v in sim.model.lanes[sid]
                            if v.pos < cfg.box_half - 0.5 and v.v < 5.0 and v.kind != "blocker"
                        )
            last[n] = now
    assert checked > 100
    assert still_entering == 0 and slow_leavers == 0


def test_adaptive_control_contains_jams_without_hurting_buses_or_throughput():
    """Default demand plus an incident, 8 seeds: far fewer jammed links, buses and throughput about the same."""
    seeds = range(1, 9)
    fixed = [run(s, False) for s in seeds]
    adaptive = [run(s, True) for s in seeds]

    def mean(runs, key):
        return sum(key(sim) for sim, _ in runs) / len(runs)

    assert sum(j for _, j in adaptive) < 0.7 * sum(j for _, j in fixed)
    lost = lambda sim: sim.metrics()["bus_delay_ratio"]  # noqa: E731
    assert mean(adaptive, lost) < mean(fixed, lost) + 0.03  # buses lose at most a few points more
    exited = lambda sim: sim.metrics()["throughput"]  # noqa: E731
    assert mean(adaptive, exited) > 0.95 * mean(fixed, exited)


def test_adaptive_run_exercises_the_decision_paths_and_the_ai_feed_forward():
    sim, _ = run(1, True)
    counts = sim.events.counts
    assert counts["INCIDENT"] == 1
    assert counts["THROTTLE"] > 0 and counts["LOCAL_EXTEND"] > 0
    assert counts["SPLIT_ADJUST"] + counts["EARLY_SWITCH"] > 0
    assert counts["FORECAST"] > 0 and counts["FORECAST_RESOLVED"] > 0
    assert sim.predictor.model_name == "RIDGE"
    assert sim.api.published == len(sim.fleet.buses) * int(SECONDS / sim.cfg.dt)


def test_fixed_mode_never_overrides_the_timer():
    sim, _ = run(1, False)
    assert sim.events.overrides == 0


def test_cars_and_buses_both_circulate():
    sim = Simulation(Config(), adaptive=True)
    sim.run(300)
    kinds = {v.kind for v in sim.model.iter_vehicles()}
    assert {"car", "bus"} <= kinds
    assert sim.model.exited > 50 and all(b.laps >= 0 for b in sim.fleet.buses)


def test_toggling_modes_at_runtime_is_safe():
    sim = Simulation(Config(), adaptive=True)
    sim.run(60)
    sim.set_adaptive(False)
    before = sim.events.overrides
    sim.run(60)
    assert sim.events.overrides == before
    sim.set_adaptive(True)
    sim.run(60)
    assert sim.t > 179
