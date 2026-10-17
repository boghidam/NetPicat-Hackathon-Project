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


def sides(sim, group):
    return {sim.grid.segments[a].from_side for a in group}


def bus_on(sim, seg_id, d, speed, bus="BX"):
    tel = BusTelemetry(bus, sim.t, 0, 0, seg_id, d, speed, sim.cfg.free_speed, 0.0)
    sim.coord.observe(tel)
    return tel


def trace(ctl, seconds, dt=0.1):
    """Step a controller on its own and record (time, phase index, phase)."""
    out = []
    for i in range(int(seconds / dt)):
        ctl.step(i * dt, dt)
        out.append((i * dt, ctl.idx, ctl.phase))
    return out


def jam_link(sim, seg_id):
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, seg_id, 100.0, 0.1, 12.0, 0))


def test_four_way_pairs_opposite_streets_into_two_phases():
    sim, ctl, _ = make(node=FOUR_WAY)
    assert [sides(sim, g) for g in ctl.groups] == [{Dir.N, Dir.S}, {Dir.E, Dir.W}]


def test_t_junction_pairs_the_through_streets_and_gives_the_stem_its_own_phase():
    sim, ctl, _ = make(node=THREE_WAY)  # top edge: arrivals from W, E and S
    assert sorted(sides(sim, g) == {Dir.E, Dir.W} for g in ctl.groups) == [False, True]
    assert sorted(len(g) for g in ctl.groups) == [1, 2]
    assert {a for g in ctl.groups for a in g} == set(ctl.approaches) and len(ctl.approaches) == 3


def test_opposite_lights_always_match_everywhere_all_the_time():
    sim = Simulation(Config(), adaptive=True, incident_at=60.0)
    for _ in range(int(400 / sim.cfg.dt)):
        sim.step()
        for ctl in sim.controllers.values():
            for a in ctl.approaches:
                seg = sim.grid.segments[a]
                opp = sim.grid.in_seg[seg.to_node][seg.direction.opposite]
                if opp is not None:
                    assert ctl.state_for(a) == ctl.state_for(opp), f"n{seg.to_node} {seg.from_side.name} vs opposite"


def test_only_one_phase_is_ever_green_and_every_other_street_stays_red():
    sim, ctl, _ = make(adaptive=False)
    for i in range(int(400 / sim.cfg.dt)):
        ctl.step(i * sim.cfg.dt, sim.cfg.dt)
        lit = {gi for gi, g in enumerate(ctl.groups) if any(ctl.state_for(a) != "R" for a in g)}
        assert lit <= {ctl.idx}


def test_every_street_gets_green_in_a_cycle_at_both_junction_types():
    for node in (FOUR_WAY, THREE_WAY):
        sim, ctl, _ = make(adaptive=False, node=node)
        served = set()
        for _i in range(int(200 / sim.cfg.dt)):
            ctl.step(0.0, sim.cfg.dt)
            served.update(a for a in ctl.approaches if ctl.state_for(a) == "G")
        assert served == set(ctl.approaches)


def test_phases_alternate_with_yellow_then_all_red_between_them():
    sim, ctl, _ = make(adaptive=False)
    log = trace(ctl, 300)
    collapsed, last = [], None
    for _t, idx, ph in log:
        if (idx, ph) != last:
            collapsed.append((idx, ph))
            last = (idx, ph)
    for (ia, pa), (ib, pb) in zip(collapsed, collapsed[1:], strict=False):
        if pa == "GREEN":
            assert (ib, pb) == (ia, "YELLOW")
        elif pa == "YELLOW":
            assert (ib, pb) == (ia, "ALLRED")
        else:
            assert pb == "GREEN" and ib == (ia + 1) % len(ctl.groups)
    assert sum(1 for _, _, p in log if p == "ALLRED") * sim.cfg.dt > 3 * sim.cfg.all_red * 0.8


def test_fixed_timer_never_overrides():
    sim = Simulation(Config(), adaptive=False)
    sim.run(300)
    assert sim.events.overrides == 0


def test_slow_bus_near_stop_line_extends_green_and_logs_override():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 15.0, 18.0  # 3 s left: inside the extension window
    bus_on(sim, ctl.current[0], d=40.0, speed=1.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned > 18.0 and ctl.extended_by > 0
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_bus_on_the_opposite_street_of_the_same_phase_also_extends():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 15.0, 18.0
    bus_on(sim, ctl.current[1], d=40.0, speed=1.0)  # the other half of the pair
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_bus_on_a_red_street_does_not_extend_the_current_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 15.0, 18.0
    bus_on(sim, ctl.groups[(ctl.idx + 1) % 2][0], d=10.0, speed=0.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert sim.events.counts["LOCAL_EXTEND"] == 0 and ctl.planned == 18.0


def test_fast_bus_does_not_trigger_an_extension():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 15.0, 18.0
    bus_on(sim, ctl.current[0], d=40.0, speed=11.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned == 18.0


def test_extension_is_capped_at_max_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed = "GREEN", sim.cfg.max_green - 2
    ctl.planned = sim.cfg.max_green - 1.0
    bus_on(sim, ctl.current[0], d=40.0, speed=0.0)
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
    seg = ctl.current[0]
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0
    for target, _ in sim.grid.turn_targets(seg):
        jam_link(sim, target)
    for _ in range(6):
        sim.estimator.on_telemetry(
            BusTelemetry("B", 0.0, 0, 0, seg, 100.0, 12.0, 12.0, 0)
        )  # the approach itself is free
    coord.refresh(0.0)
    assert coord.throttle[seg] == pytest.approx(0.0, abs=0.05)
    assert sim.events.counts["THROTTLE"] >= 1


def test_throttle_is_waived_while_a_bus_is_queued_behind_the_cars():
    sim, ctl, coord = make()
    seg = ctl.current[0]
    for target, _ in sim.grid.turn_targets(seg):
        jam_link(sim, target)
    bus_on(sim, seg, d=60.0, speed=0.0)  # buses cannot overtake cars, so metering would delay it
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0


def test_throttle_never_applied_in_fixed_mode():
    sim, ctl, coord = make(adaptive=False)
    for target, _ in sim.grid.turn_targets(ctl.current[0]):
        jam_link(sim, target)
    coord.refresh(0.0)
    assert all(coord.release_factor(s.id) == 1.0 for s in sim.grid.segments)


def test_forecast_for_current_phase_triggers_predictive_extension_once():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 14.0, 16.0
    f = Forecast(
        99,
        "B07",
        ctl.current[0],
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
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1 and ctl.planned > 16.0
    ctl._decide(0.5)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1


def test_forecast_for_a_waiting_phase_ends_the_current_green_and_serves_it_next():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 10.0, 18.0
    other = (ctl.idx + 1) % len(ctl.groups)
    seg = ctl.groups[other][0]
    sim.predictor._active[7] = Forecast(
        7, "B03", seg, 1, 0.0, eta=7.0, predicted_speed=2.0, predicted_density=0.8, confidence=0.7, expires_at=60.0
    )
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed and sim.events.counts["PREDICTIVE_SWITCH"] == 1
    ctl.phase, ctl.elapsed = "ALLRED", sim.cfg.all_red
    ctl.step(0.0, sim.cfg.dt)
    assert ctl.idx == other


def test_green_split_follows_scores_within_bounds():
    sim, ctl, coord = make()
    busy = ctl.groups[0][0]
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, busy, 100.0, 1.0, 12.0, 0))
    coord.refresh(0.0)
    planned, _ = coord.plan_green(FOUR_WAY, 0, 0.0)
    assert planned > sim.cfg.base_green
    assert sim.cfg.min_green <= planned <= sim.cfg.max_green
    shorter, _ = coord.plan_green(FOUR_WAY, 1, 0.0)
    assert shorter < sim.cfg.base_green


def test_equal_scores_keep_the_standard_timer():
    sim, ctl, coord = make()
    planned, _ = coord.plan_green(FOUR_WAY, 0, 0.0)
    assert planned == pytest.approx(sim.cfg.base_green)


def test_greens_stay_within_min_and_max_even_with_overrides():
    sim = Simulation(Config(), adaptive=True, incident_at=30.0)
    ctl = sim.controllers[FOUR_WAY]
    greens, started, last = [], 0.0, (ctl.idx, ctl.phase)
    for _ in range(int(500 / sim.cfg.dt)):
        sim.step()
        now = (ctl.idx, ctl.phase)
        if now != last:
            if last[1] == "GREEN" and now[1] == "YELLOW" and sim.t > 30:
                greens.append(sim.t - started)
            if now[1] == "GREEN":
                started = sim.t
            last = now
    assert greens and min(greens) >= sim.cfg.min_green - 0.3 and max(greens) <= sim.cfg.max_green + 0.3
