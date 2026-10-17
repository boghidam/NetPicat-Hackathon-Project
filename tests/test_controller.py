import pytest

from smartcity.config import Config
from smartcity.mock_api import BusTelemetry
from smartcity.network import Axis, Dir
from smartcity.simulation import Simulation

NODE = 5  # interior intersection


def make(adaptive=True, **kw):
    sim = Simulation(Config(**kw), adaptive=adaptive)
    return sim, sim.controllers[NODE], sim.coord


def bus_on(sim, seg_id, d, speed, t=None, bus="BX"):
    tel = BusTelemetry(bus, sim.t if t is None else t, 0, 0, seg_id, d, speed, sim.cfg.free_speed, 0.0)
    sim.coord.observe(tel)
    return tel


def run_ctl(sim, ctl, seconds):
    dt = sim.cfg.dt
    for i in range(int(seconds / dt)):
        ctl.step(sim.t + i * dt, dt)
        sim.coord.refresh(sim.t + i * dt) if i % 5 == 0 else None


def green_approach(sim, ctl):
    return sim.grid.approaches(NODE, ctl.axis)[0]


def test_fixed_timer_alternates_with_yellow_and_never_overrides():
    sim, ctl, _ = make(adaptive=False)
    start_axis = ctl.axis
    seen = set()
    for i in range(int(130 / sim.cfg.dt)):
        ctl.step(i * sim.cfg.dt, sim.cfg.dt)
        seen.add((ctl.axis, ctl.phase))
    assert {(a, p) for a in Axis for p in ("GREEN", "YELLOW")} <= seen
    assert sim.events.overrides == 0
    assert ctl.axis in Axis and start_axis in Axis


def test_exactly_one_axis_is_ever_green():
    sim, ctl, _ = make()
    for i in range(1200):
        ctl.step(i * sim.cfg.dt, sim.cfg.dt)
        assert [ctl.state_for(a) for a in Axis].count("G") <= 1


def test_slow_bus_near_stop_line_extends_green_and_logs_override():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed = "GREEN", 22.0
    ctl.planned = 26.0  # 4 s left: inside the extension window
    seg = green_approach(sim, ctl)
    bus_on(sim, seg.id, d=40.0, speed=1.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned > 26.0 and ctl.extended_by > 0
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_fast_bus_does_not_trigger_an_extension():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 22.0, 26.0
    bus_on(sim, green_approach(sim, ctl).id, d=40.0, speed=11.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned == 26.0


def test_extension_is_capped_at_max_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed = "GREEN", 40.0
    ctl.planned = sim.cfg.max_green - 1.0
    bus_on(sim, green_approach(sim, ctl).id, d=40.0, speed=0.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned <= sim.cfg.max_green


def test_grid_wide_budget_denies_extensions_when_exhausted():
    sim, ctl, coord = make(budget_seconds=4.5)
    assert coord.request_extension(NODE, 4.0, now=0.0) > 0
    assert coord.request_extension(NODE, 4.0, now=1.0) == 0.0
    assert sim.events.counts["BUDGET_DENIED"] == 1
    # the window rolls forward
    assert coord.request_extension(NODE, 4.0, now=0.0 + sim.cfg.budget_window + 5) > 0


def test_neighbour_extensions_damp_each_other():
    sim, ctl, coord = make()
    first = coord.request_extension(1, 8.0, now=0.0)  # node 1 is adjacent to node 5
    second = coord.request_extension(NODE, 8.0, now=1.0)
    assert first == pytest.approx(8.0)
    assert second < 8.0


def test_throttle_is_unity_when_downstream_is_clear_and_zero_when_far_worse():
    sim, ctl, coord = make()
    seg = green_approach(sim, ctl)
    straight = sim.grid.straight_next(seg.id)
    coord.est_load = [0.1] * len(coord.est_load)
    coord.refresh(0.0)  # refresh recomputes from the estimator; prior is 0.25 everywhere
    assert coord.throttle[seg.id] == 1.0
    # feed the estimator so the next link looks jammed while the approach looks free
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, straight, 100.0, 0.2, 12.0, 0))
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, seg.id, 100.0, 12.0, 12.0, 0))
    coord.refresh(0.0)
    assert coord.throttle[seg.id] == pytest.approx(0.0, abs=0.05)
    assert sim.events.counts["THROTTLE"] >= 1


def test_throttle_never_applied_in_fixed_mode():
    sim, ctl, coord = make(adaptive=False)
    straight = sim.grid.straight_next(green_approach(sim, ctl).id)
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, straight, 100.0, 0.2, 12.0, 0))
    coord.refresh(0.0)
    assert all(coord.release_factor(s.id) == 1.0 for s in sim.grid.segments)


def fill_north_exit(sim):
    """Make the link leaving NODE northwards look jammed (it feeds every EW approach)."""
    target = sim.grid.out_seg[NODE][Dir.N]
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, target, 100.0, 0.1, 12.0, 0))


def test_spillback_guard_hands_green_to_cross_axis():
    sim, ctl, coord = make()
    ctl.axis, ctl.phase, ctl.elapsed, ctl.planned = Axis.EW, "GREEN", sim.cfg.min_green + 1, 28.0
    fill_north_exit(sim)
    coord.refresh(0.0)
    assert coord.axis_blocked(NODE, Axis.EW) and not coord.axis_blocked(NODE, Axis.NS)
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed
    assert sim.events.counts["SPILLBACK_HOLD"] == 1


def test_spillback_guard_respects_min_green():
    sim, ctl, coord = make()
    ctl.axis, ctl.phase, ctl.elapsed, ctl.planned = Axis.EW, "GREEN", 2.0, 28.0
    fill_north_exit(sim)
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == 28.0


def test_forecast_for_current_axis_triggers_predictive_extension():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 20.0, 24.0
    seg = green_approach(sim, ctl)
    from smartcity.predictor import Forecast

    f = Forecast(
        99, "B07", seg.id, 1, 0.0, eta=6.0, predicted_speed=2.0, predicted_density=0.8, confidence=0.7, expires_at=60.0
    )
    sim.predictor._active[99] = f
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1 and ctl.planned > 24.0
    ctl._decide(0.5)  # a forecast is only acted on once
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1


def test_forecast_for_waiting_axis_ends_current_green_early():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 15.0, 28.0
    waiting = sim.grid.approaches(NODE, ctl.axis.other)[0]
    from smartcity.predictor import Forecast

    sim.predictor._active[7] = Forecast(
        7,
        "B03",
        waiting.id,
        1,
        0.0,
        eta=7.0,
        predicted_speed=2.0,
        predicted_density=0.8,
        confidence=0.7,
        expires_at=60.0,
    )
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed
    assert sim.events.counts["PREDICTIVE_SWITCH"] == 1


def test_green_split_follows_scores_within_bounds():
    sim, ctl, coord = make()
    axis = Axis.NS
    seg = sim.grid.approaches(NODE, axis)[0]
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, seg.id, 100.0, 3.0, 12.0, 0))  # approach is busy
    coord.refresh(0.0)
    planned, _ = coord.plan_green(NODE, axis, 0.0)
    assert planned > sim.cfg.base_green
    assert sim.cfg.min_green <= planned <= sim.cfg.max_green


def test_equal_scores_keep_the_standard_timer():
    sim, ctl, coord = make()
    planned, _ = coord.plan_green(NODE, Axis.NS, 0.0)
    assert planned == pytest.approx(sim.cfg.base_green)


def test_hysteresis_min_green_is_never_violated_by_overrides():
    sim, *_ = make()
    sim.set_adaptive(True)
    sim.trigger_incident()
    greens = {nid: [] for nid in sim.controllers}
    last = {nid: (c.axis, c.phase) for nid, c in sim.controllers.items()}
    start = {nid: 0.0 for nid in sim.controllers}
    for _ in range(int(400 / sim.cfg.dt)):
        sim.step()
        for nid, c in sim.controllers.items():
            now = (c.axis, c.phase)
            if now != last[nid]:
                if last[nid][1] == "GREEN" and now[1] == "YELLOW" and sim.t > 30:
                    greens[nid].append(sim.t - start[nid])
                if now[1] == "GREEN":
                    start[nid] = sim.t
                last[nid] = now
    observed = [g for gs in greens.values() for g in gs]
    assert observed and min(observed) >= sim.cfg.min_green - 0.3
    assert max(observed) <= sim.cfg.max_green + 0.3
