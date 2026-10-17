import pytest

from smartcity.config import Config
from smartcity.controller import JUNCTION_WEIGHT
from smartcity.mock_api import BusTelemetry
from smartcity.predictor import Forecast
from smartcity.simulation import Simulation

from .helpers import star_net

FOUR_WAY = 5  # interior crossroads of the synthetic grid
FREE = 13.9


def make(adaptive=True, node=FOUR_WAY, net=None, **kw):
    sim = Simulation(Config(**kw), adaptive=adaptive, map_name="grid", net=net)
    return sim, sim.controllers[node], sim.coord


def t_junction(adaptive=True):
    net = star_net([0, 180, 270])  # east and west through road, stem to the south
    return Simulation(Config(), adaptive=adaptive, net=net)


def bus_on(sim, seg_id, d, speed, bus="BX"):
    tel = BusTelemetry(bus, sim.t, 0, 0, seg_id, d, speed, FREE, 0.0, line="G1")
    sim.coord.observe(tel)
    return tel


def jam_link(sim, seg_id):
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, seg_id, 100.0, 0.1, FREE, 0))


def road(sim, a, b):
    return sim.net.segment_between(a, b)


def step_ctl(ctl, seconds, dt=0.1):
    for i in range(int(seconds / dt)):
        ctl.step(i * dt, dt)
        yield i * dt


# -- phases ---------------------------------------------------------------------------


def test_controllers_exist_only_at_signalised_junctions_and_use_the_networks_groups():
    sim, ctl, _ = make()
    assert set(sim.controllers) == {n.id for n in sim.net.nodes if n.signalised}
    assert ctl.groups == sim.net.phase_groups(FOUR_WAY) and len(ctl.groups) == 2


def test_t_junction_runs_two_phases_the_through_street_and_the_stem():
    sim = t_junction()
    ctl = sim.controllers[0]
    assert sorted(len(g) for g in ctl.groups) == [1, 2]


def test_opposite_lights_always_match_at_every_junction_all_the_time():
    sim = Simulation(Config(), adaptive=True, map_name="iasi", incident_at=30.0)
    checked = 0
    for _ in range(int(300 / sim.cfg.dt)):
        sim.step()
        for ctl in sim.controllers.values():
            for a, b in ((a, sim.net.partner[a]) for a in ctl.approaches if a in sim.net.partner):
                assert ctl.state_for(a) == ctl.state_for(b)
                checked += 1
    assert checked > 100_000


def test_only_one_phase_is_ever_green_and_every_other_street_stays_red():
    for sim in (make(adaptive=False)[0], t_junction(adaptive=False)):
        ctl = sim.controllers[FOUR_WAY if len(sim.controllers) > 1 else 0]
        for _t in step_ctl(ctl, 400):
            lit = {gi for gi, g in enumerate(ctl.groups) if any(ctl.state_for(a) != "R" for a in g)}
            assert lit <= {ctl.idx}


def test_every_street_gets_green_in_a_cycle_at_both_junction_types():
    for sim in (make(adaptive=False)[0], t_junction(adaptive=False)):
        ctl = next(iter(sim.controllers.values())) if len(sim.controllers) == 1 else sim.controllers[FOUR_WAY]
        served = set()
        for _ in step_ctl(ctl, 200):
            served.update(a for a in ctl.approaches if ctl.state_for(a) == "G")
        assert served == set(ctl.approaches)


def test_sumo_style_phase_timing_min_green_yellow_and_all_red():
    sim, ctl, _ = make(adaptive=False)
    cfg = sim.cfg
    durations: dict[str, list[float]] = {"GREEN": [], "YELLOW": [], "ALLRED": []}
    last, started = (ctl.idx, ctl.phase), 0.0
    for t in step_ctl(ctl, 400):
        now = (ctl.idx, ctl.phase)
        if now != last:
            if t > 30:  # skip the partial first phase
                durations[last[1]].append(t - started)
            started, last = t, now
    assert min(durations["GREEN"]) >= cfg.min_green - 0.2 and cfg.min_green == 7.0
    assert all(d == pytest.approx(3.0, abs=0.2) for d in durations["YELLOW"]) and cfg.yellow == 3.0
    assert all(d == pytest.approx(2.0, abs=0.2) for d in durations["ALLRED"])


def test_phases_alternate_green_yellow_all_red_then_the_next_phase():
    sim, ctl, _ = make(adaptive=False)
    seen, last = [], None
    for _ in step_ctl(ctl, 300):
        if (ctl.idx, ctl.phase) != last:
            seen.append((ctl.idx, ctl.phase))
            last = (ctl.idx, ctl.phase)
    for (ia, pa), (ib, pb) in zip(seen, seen[1:], strict=False):
        if pa == "GREEN":
            assert (ib, pb) == (ia, "YELLOW")
        elif pa == "YELLOW":
            assert (ib, pb) == (ia, "ALLRED")
        else:
            assert pb == "GREEN" and ib == (ia + 1) % len(ctl.groups)


def test_adaptive_overrides_never_break_the_minimum_green():
    sim = Simulation(Config(), adaptive=True, map_name="iasi", incident_at=30.0)
    started = {n: 0.0 for n in sim.controllers}
    last = {n: (c.idx, c.phase) for n, c in sim.controllers.items()}
    greens = []
    for _ in range(int(400 / sim.cfg.dt)):
        sim.step()
        for n, c in sim.controllers.items():
            now = (c.idx, c.phase)
            if now != last[n]:
                if last[n][1] == "GREEN" and now[1] == "YELLOW" and started[n] > 0.0:
                    greens.append(sim.t - started[n])
                if now[1] == "GREEN":
                    started[n] = sim.t
                last[n] = now
    assert len(greens) > 200
    assert min(greens) >= sim.cfg.min_green - 0.2 and max(greens) <= sim.cfg.max_green + 0.2


def test_fixed_timer_never_overrides():
    sim = Simulation(Config(), adaptive=False, map_name="grid")
    sim.run(300)
    assert sim.events.overrides == 0


# -- local optimisation ---------------------------------------------------------------------


def test_slow_bus_in_the_detection_window_extends_green_and_logs_override():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 17.0, 20.0  # 3 s left: inside the extension window
    bus_on(sim, ctl.current[0], d=60.0, speed=1.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned > 20.0 and ctl.extended_by > 0
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_bus_on_the_opposite_street_of_the_same_phase_also_extends():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 17.0, 20.0
    bus_on(sim, ctl.current[1], d=60.0, speed=1.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert sim.events.counts["LOCAL_EXTEND"] == 1


def test_bus_on_a_red_street_does_not_extend_the_current_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 17.0, 20.0
    bus_on(sim, ctl.groups[(ctl.idx + 1) % 2][0], d=60.0, speed=0.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert sim.events.counts["LOCAL_EXTEND"] == 0 and ctl.planned == 20.0


def test_fast_bus_does_not_trigger_an_extension():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 17.0, 20.0
    bus_on(sim, ctl.current[0], d=60.0, speed=12.0)
    coord.refresh(sim.t)
    ctl._decide(sim.t)
    assert ctl.planned == 20.0


def test_extension_is_capped_at_max_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed = "GREEN", sim.cfg.max_green - 2
    ctl.planned = sim.cfg.max_green - 1.0
    bus_on(sim, ctl.current[0], d=60.0, speed=0.0)
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
    assert first == pytest.approx(8.0) and second < 8.0


# -- graph-aware bottleneck prevention ------------------------------------------------------


def test_throttle_is_unity_when_downstream_is_clear_and_zero_when_far_worse():
    sim, ctl, coord = make()
    seg = ctl.current[0]
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0
    for target, _ in sim.net.turn_targets(seg):
        jam_link(sim, target)
    coord.refresh(0.0)
    assert coord.throttle[seg] == pytest.approx(0.0, abs=0.05)
    assert sim.events.counts["THROTTLE"] >= 1


def test_an_overloaded_downstream_junction_throttles_the_upstream_release():
    """Releasing onto a perfectly free edge is still metered when the junction it leads to is jammed."""
    sim, ctl, coord = make()
    a = road(sim, 4, 5)  # approach into node 5
    onto = road(sim, 5, 6)  # the straight-ahead edge, free of any report
    for feeder in (road(sim, 7, 6), road(sim, 2, 6)):  # the other streets entering node 6 are jammed
        jam_link(sim, feeder)
    coord.refresh(0.0)
    assert coord.load[onto] == pytest.approx(sim.cfg.est_prior_load)  # nothing reported on the edge itself
    assert coord.junction_load[6] > 0.6
    assert coord.dload[a] >= JUNCTION_WEIGHT * coord.junction_load[6] - 1e-9 > coord.load[onto]
    assert coord.throttle[a] < 1.0


def test_throttle_is_waived_while_a_bus_is_queued_behind_the_cars():
    sim, ctl, coord = make()
    seg = ctl.current[0]
    for target, _ in sim.net.turn_targets(seg):
        jam_link(sim, target)
    bus_on(sim, seg, d=100.0, speed=5.0)  # buses cannot overtake cars, so metering would delay it
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0


def test_throttle_never_applied_in_fixed_mode():
    sim, ctl, coord = make(adaptive=False)
    for target, _ in sim.net.turn_targets(ctl.current[0]):
        jam_link(sim, target)
    coord.refresh(0.0)
    assert all(coord.release_factor(s.id) == 1.0 for s in sim.net.segments)


# -- feed-forward and dynamic weighting -------------------------------------------------------


def forecast(fid, seg, eta, density=0.8):
    return Forecast(fid, "B07", seg, 1, 0.0, eta, 2.0, density, 0.7, 60.0, predicted_delay=25.0, line="G1")


def test_forecast_for_current_phase_triggers_predictive_extension_once():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 16.0, 18.0
    sim.predictor._active[99] = forecast(99, ctl.current[0], eta=6.0)
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1 and ctl.planned > 18.0
    ctl._decide(0.5)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1


def test_forecast_for_a_waiting_phase_ends_the_current_green_and_serves_it_next():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 10.0, 20.0
    other = (ctl.idx + 1) % len(ctl.groups)
    sim.predictor._active[7] = forecast(7, ctl.groups[other][0], eta=7.0)
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == ctl.elapsed and sim.events.counts["PREDICTIVE_SWITCH"] == 1
    ctl.phase, ctl.elapsed = "ALLRED", sim.cfg.all_red
    ctl.step(0.0, sim.cfg.dt)
    assert ctl.idx == other


def test_no_early_switch_before_the_minimum_green():
    sim, ctl, coord = make()
    ctl.phase, ctl.elapsed, ctl.planned = "GREEN", 3.0, 20.0
    other = (ctl.idx + 1) % len(ctl.groups)
    sim.predictor._active[7] = forecast(7, ctl.groups[other][0], eta=7.0)
    coord.refresh(0.0)
    ctl._decide(0.0)
    assert ctl.planned == 20.0 and sim.events.counts["PREDICTIVE_SWITCH"] == 0


def test_green_split_follows_scores_within_bounds():
    sim, ctl, coord = make()
    busy = ctl.groups[0][0]
    for _ in range(6):
        sim.estimator.on_telemetry(BusTelemetry("B", 0.0, 0, 0, busy, 100.0, 1.0, FREE, 0))
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
