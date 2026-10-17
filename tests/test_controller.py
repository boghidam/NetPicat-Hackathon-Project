import pytest

from smartcity.config import Config
from smartcity.mock_api import BusTelemetry
from smartcity.network import Dir
from smartcity.predictor import Forecast
from smartcity.simulation import Simulation

FOUR_WAY, THREE_WAY = 5, 1  # interior node, top-edge node


def make(adaptive=True, node=FOUR_WAY, **kw):
    sim = Simulation(Config(**kw), adaptive=adaptive)
    return sim, sim.controllers[node], sim.coord


def bus_on(sim, seg_id, d, speed, bus="BX"):
    tel = BusTelemetry(bus, sim.t, 0, 0, seg_id, d, speed, sim.cfg.free_speed, 0.0)
    sim.coord.observe(tel)
    return tel


def trace(ctl, seconds, dt=0.1, start=0.0):
    """Step a controller on its own and record (time, green approach or None, phase)."""
    out = []
    for i in range(int(seconds / dt)):
        ctl.step(start + i * dt, dt)
        out.append((start + i * dt, ctl.current, ctl.phase))
    return out


def jam_link(sim, seg_id):
    """Make the estimator believe a link is jammed."""
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, seg_id, 100.0, 0.1, 12.0, 0))


def jam_exit(sim, ctl, approach):
    """Make every link that `approach` releases into look jammed to the estimator."""
    for target, _ in sim.grid.turn_targets(approach):
        if sim.grid.segments[target].signalised:
            jam_link(sim, target)


def jam_north_exit(sim):
    """Jam the link leaving node 5 northwards. It feeds the E, S and W approaches but not the N one."""
    jam_link(sim, sim.grid.out_seg[FOUR_WAY][Dir.N])


def approach_from(sim, ctl, side):
    return next(a for a in ctl.approaches if sim.grid.segments[a].from_side == side)


def test_four_way_runs_four_phases_and_three_way_runs_three():
    for node, phases in ((FOUR_WAY, 4), (THREE_WAY, 3)):
        sim, ctl, _ = make(adaptive=False, node=node)
        assert len(ctl.approaches) == phases
        greens = {cur for _, cur, ph in trace(ctl, 5 * phases * 20) if ph == "GREEN"}
        assert len(greens) == phases


def test_only_one_street_is_ever_green_and_the_rest_stay_red():
    sim, ctl, coord = make(adaptive=False)
    for _t, _cur, _ph in trace(ctl, 400):
        states = [ctl.state_for(a) for a in ctl.approaches]
        assert sum(s != "R" for s in states) <= 1


def test_phases_follow_in_sequence_with_yellow_then_all_red_between():
    sim, ctl, _ = make(adaptive=False)
    log = trace(ctl, 300)
    order, last = [], None
    for _t, cur, ph in log:
        if ph == "GREEN" and cur != last:
            order.append(ctl.approaches.index(cur))
            last = cur
    n = len(ctl.approaches)
    assert all((b - a) % n == 1 for a, b in zip(order, order[1:], strict=False))
    # every green is followed by yellow, then an all-red, before the next green
    phases = [ph for _, _, ph in log]
    collapsed = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
    for a, b in zip(collapsed, collapsed[1:], strict=False):
        assert (a, b) in {("GREEN", "YELLOW"), ("YELLOW", "ALLRED"), ("ALLRED", "GREEN")}
    allred = [sum(1 for _, _, p in log if p == "ALLRED")]
    assert allred[0] * sim.cfg.dt > 3 * sim.cfg.all_red * 0.8


def test_fixed_timer_never_overrides():
    sim = Simulation(Config(), adaptive=False)
    sim.run(300)
    assert sim.events.overrides == 0


def test_slow_bus_near_stop_line_extends_green_and_logs_override():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 11.0, 14.0  # 3 s left: inside the extension window
    bus_on(sim, ctl.current, d=40.0, speed=1.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned > 14.0 and ctl.extended_by > 0
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_bus_on_a_red_street_does_not_extend_the_current_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 11.0, 14.0
    other = next(a for a in ctl.approaches if a != ctl.current)
    bus_on(sim, other, d=10.0, speed=0.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert sim.events.counts["LOCAL_EXTEND"] == 0 and ctl.planned == 14.0


def test_fast_bus_does_not_trigger_an_extension():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 11.0, 14.0
    bus_on(sim, ctl.current, d=40.0, speed=11.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned == 14.0


def test_extension_is_capped_at_max_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed = "GREEN", sim.cfg.max_green - 2
    ctl.planned = sim.cfg.max_green - 1.0
    bus_on(sim, ctl.current, d=40.0, speed=0.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned <= sim.cfg.max_green


def test_grid_wide_budget_denies_extensions_when_exhausted():
    sim, ctl, coord = make(budget_seconds=4.5)
    assert coord.request_extension(FOUR_WAY, 4.0, now=0.0) > 0
    assert coord.request_extension(FOUR_WAY, 4.0, now=1.0) == 0.0
    assert sim.events.counts["BUDGET_DENIED"] == 1
    assert coord.request_extension(FOUR_WAY, 4.0, now=sim.cfg.budget_window + 5) > 0


def test_neighbour_extensions_damp_each_other():
    sim, ctl, coord = make()
    first = coord.request_extension(1, 8.0, now=0.0)  # node 1 is adjacent to node 5
    second = coord.request_extension(FOUR_WAY, 8.0, now=1.0)
    assert first == pytest.approx(8.0)
    assert second < 8.0


def test_throttle_is_unity_when_downstream_is_clear_and_zero_when_far_worse():
    sim, ctl, coord = make()
    seg = ctl.current
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0
    jam_exit(sim, ctl, seg)
    for _ in range(6):
        sim.estimator.on_telemetry(
            BusTelemetry("B", 0.0, 0, 0, seg, 100.0, 12.0, 12.0, 0)
        )  # the approach itself is free
    coord.refresh(0.0)
    assert coord.throttle[seg] == pytest.approx(0.0, abs=0.05)
    assert sim.events.counts["THROTTLE"] >= 1


def test_throttle_never_applied_in_fixed_mode():
    sim, ctl, coord = make(adaptive=False)
    jam_exit(sim, ctl, ctl.current)
    coord.refresh(0.0)
    assert all(coord.release_factor(s.id) == 1.0 for s in sim.grid.segments)


def test_spillback_guard_cuts_a_green_whose_exits_are_full():
    sim, ctl, coord = make()
    ctl.idx = ctl.approaches.index(approach_from(sim, ctl, Dir.E))
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", sim.cfg.min_green + 1, 14.0
    jam_north_exit(sim)
    coord.refresh(0.0)
    assert coord.blocked(ctl.current) and not coord.blocked(approach_from(sim, ctl, Dir.N))
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed
    assert sim.events.counts["SPILLBACK_HOLD"] == 1


def test_spillback_guard_respects_min_green():
    sim, ctl, coord = make()
    ctl.idx = ctl.approaches.index(approach_from(sim, ctl, Dir.E))
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 2.0, 14.0
    jam_north_exit(sim)
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == 14.0


def test_blocked_streets_are_skipped_when_choosing_the_next_phase():
    sim, ctl, coord = make()
    ctl.idx = ctl.approaches.index(approach_from(sim, ctl, Dir.E))  # next in sequence would be S, then W
    jam_north_exit(sim)
    coord.refresh(0.0)
    ctl.phase, ctl.elapsed = "ALLRED", sim.cfg.all_red
    ctl.step(0.0, sim.cfg.dt)
    assert ctl.current == approach_from(sim, ctl, Dir.N)  # the only street that can still discharge
    assert sim.events.counts["SPILLBACK_HOLD"] == 1


def test_forecast_for_current_street_triggers_predictive_extension_once():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 10.0, 12.0
    f = Forecast(
        99,
        "B07",
        ctl.current,
        1,
        0.0,
        eta=6.0,
        predicted_speed=2.0,
        predicted_density=0.8,
        confidence=0.7,
        expires_at=60.0,
    )
    sim.predictor._active[99] = f
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1 and ctl.planned > 12.0
    ctl._decide(0.5)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1


def test_forecast_for_a_waiting_street_serves_it_next():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 8.0, 14.0
    target = ctl.approaches[(ctl.idx + 2) % len(ctl.approaches)]  # not the next one in sequence
    sim.predictor._active[7] = Forecast(
        7, "B03", target, 1, 0.0, eta=7.0, predicted_speed=2.0, predicted_density=0.8, confidence=0.7, expires_at=60.0
    )
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed and sim.events.counts["PREDICTIVE_SWITCH"] == 1
    ctl.phase, ctl.elapsed = "ALLRED", sim.cfg.all_red
    ctl.step(0.0, sim.cfg.dt)
    assert ctl.current == target


def test_green_split_follows_scores_within_bounds():
    sim, ctl, coord = make()
    busy = ctl.approaches[1]
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, busy, 100.0, 3.0, 12.0, 0))
    coord.refresh(0.0)
    planned, _ = coord.plan_green(FOUR_WAY, busy, 0.0)
    assert planned > sim.cfg.base_green
    assert sim.cfg.min_green <= planned <= sim.cfg.max_green


def test_equal_scores_keep_the_standard_timer():
    sim, ctl, coord = make()
    planned, _ = coord.plan_green(FOUR_WAY, ctl.approaches[0], 0.0)
    assert planned == pytest.approx(sim.cfg.base_green)


def test_greens_stay_within_min_and_max_even_with_overrides():
    sim = Simulation(Config(), adaptive=True, incident_at=30.0)
    ctl = sim.controllers[FOUR_WAY]
    greens, started, last = [], 0.0, (ctl.current, ctl.phase)
    for _ in range(int(500 / sim.cfg.dt)):
        sim.step()
        now = (ctl.current, ctl.phase)
        if now != last:
            if last[1] == "GREEN" and now[1] == "YELLOW" and sim.t > 30:
                greens.append(sim.t - started)
            if now[1] == "GREEN":
                started = sim.t
            last = now
    assert greens and min(greens) >= sim.cfg.min_green - 0.3 and max(greens) <= sim.cfg.max_green + 0.3
    assert Dir.N in {sim.grid.segments[a].from_side for a in ctl.approaches}
