"""Headless closed-loop scenario: an incident hits the central link; compare control modes."""

from smartcity.config import Config
from smartcity.simulation import Simulation

SEEDS = (1, 2, 3, 4)
SECONDS = 600


def run(seed: int, adaptive: bool):
    sim = Simulation(Config(seed=seed), adaptive=adaptive, incident_at=120.0)
    jam_seconds = 0.0  # link-seconds spent above the spillback threshold
    for _ in range(SECONDS // 5):
        sim.run(5)
        jam_seconds += 5 * sum(sim.model.density(i) > sim.cfg.rho_block for i in range(len(sim.grid.segments)))
    return sim, jam_seconds


def test_adaptive_control_contains_spillback_better_than_fixed_timer():
    fixed = [run(s, False) for s in SEEDS]
    adaptive = [run(s, True) for s in SEEDS]
    fixed_jam = sum(j for _, j in fixed)
    adaptive_jam = sum(j for _, j in adaptive)
    assert adaptive_jam < 0.8 * fixed_jam
    # and it does not buy that by hurting the buses or throughput
    fixed_delay = sum(s.metrics()["bus_delay_ratio"] for s, _ in fixed) / len(SEEDS)
    adaptive_delay = sum(s.metrics()["bus_delay_ratio"] for s, _ in adaptive) / len(SEEDS)
    assert adaptive_delay < fixed_delay * 1.03
    fixed_thr = sum(s.metrics()["throughput"] for s, _ in fixed)
    adaptive_thr = sum(s.metrics()["throughput"] for s, _ in adaptive)
    assert adaptive_thr > 0.97 * fixed_thr


def test_adaptive_run_exercises_every_decision_path_and_the_ai_feed_forward():
    sim, _ = run(1, True)
    counts = sim.events.counts
    assert counts["INCIDENT"] == 1
    assert counts["THROTTLE"] > 0 and counts["SPLIT_ADJUST"] > 0 and counts["EARLY_SWITCH"] > 0
    assert counts["FORECAST"] > 0 and counts["FORECAST_RESOLVED"] > 0
    assert sim.predictor.model_name == "RIDGE"
    assert sim.api.published == len(sim.fleet.buses) * int(SECONDS / sim.cfg.dt)


def test_fixed_mode_never_overrides_the_timer():
    sim, _ = run(1, False)
    assert sim.events.overrides == 0


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
