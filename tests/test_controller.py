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
    ctl = t_junction().controllers[0]
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
    for sim, node in ((make(adaptive=False)[0], FOUR_WAY), (t_junction(adaptive=False), 0)):
        ctl = sim.controllers[node]
        for _ in step_ctl(ctl, 400):
            lit = {gi for gi, g in enumerate(ctl.groups) if any(ctl.state_for(a) != "R" for a in g)}
            assert lit <= {ctl.idx}


def test_every_street_gets_green_in_a_cycle_at_both_junction_types():
    for sim, node in ((make(adaptive=False)[0], FOUR_WAY), (t_junction(adaptive=False), 0)):
        ctl = sim.controllers[node]
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


def test_fixed_timer_never_overrides():
    sim = Simulation(Config(), adaptive=False, map_name="grid")
    sim.run(300)
    assert sim.events.overrides == 0


# -- greens are never cut short -------------------------------------------------------------


def test_a_green_is_never_ended_early_or_shortened_only_extended():
    """Only buses are observed, so nothing may end a green before its standard time."""
    sim = Simulation(Config(), adaptive=True, map_name="iasi", incident_at=30.0)
    cfg = sim.cfg
    started = {n: 0.0 for n in sim.controllers}
    planned = {n: c.planned for n, c in sim.controllers.items()}
    last = {n: (c.idx, c.phase) for n, c in sim.controllers.items()}
    greens = []
    for _ in range(int(500 / cfg.dt)):
        sim.step()
        for n, c in sim.controllers.items():
            if c.phase == "GREEN":
                assert c.planned >= planned[n] - 1e-9 or (c.idx, c.phase) != last[n]  # a green's length only grows
            planned[n] = c.planned
            now = (c.idx, c.phase)
            if now != last[n]:
                if last[n][1] == "GREEN" and now[1] == "YELLOW" and started[n] > 0.0:
                    greens.append(sim.t - started[n])
                if now[1] == "GREEN":
                    started[n] = sim.t
                last[n] = now
    assert len(greens) > 200
    assert min(greens) >= cfg.base_green - 0.2 and max(greens) <= cfg.max_green + 0.2
    assert max(greens) > cfg.base_green + 1.0  # and some really were extended
    assert not {"EARLY_SWITCH", "PREDICTIVE_SWITCH", "SPLIT_ADJUST"} & set(sim.events.counts)


# -- the per-cycle bus check ------------------------------------------------------------------


def cycle_setup(adaptive=True):
    """Node 5, whose E-W phase is about to turn green again, with a bus approaching on edge 4>5."""
    sim, ctl, coord = make(adaptive=adaptive)
    route = sim.net.route_by_ref["G5"]  # row 1 out and back: 4>5, 5>6, ...
    approach = route.edges[0]  # 4>5 is an approach of node 5's E-W phase
    gi = next(i for i, g in enumerate(ctl.groups) if approach in g)
    ctl.idx = (gi - 1) % len(ctl.groups)  # the other phase is finishing
    ctl.phase, ctl.elapsed = "ALLRED", sim.cfg.all_red
    return sim, ctl, coord, route, approach, gi


def feed_bus(sim, route, bus, along_start, along_end, t0, t1, leg=0):
    """A bus on edge `leg` of its line moving from one point to another between two times."""
    seg = sim.net.segments[route.edges[leg]]
    t = t0
    while t <= t1 + 1e-9:
        f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        along = along_start + f * (along_end - along_start)
        rec = BusTelemetry(
            bus, t, 0, 0, seg.id, max(0.0, seg.stop_pos - along), 1.0, FREE, 0.0, line=route.ref, leg=leg
        )
        sim.progress.on_telemetry(rec)
        sim.loops.observe(rec)
        t += 0.5


def start_green(sim, ctl, now, previous_green):
    sim.t = now
    ctl._last_green[ctl.idx] = previous_green  # placeholder; _start_green reads the entry of the phase it moves to
    ctl._last_green[(ctl.idx + 1) % len(ctl.groups)] = previous_green
    ctl.step(now, sim.cfg.dt)


def test_a_bus_that_barely_moved_during_the_cycle_gets_the_green_extended():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    feed_bus(sim, route, "B07", 140.0, 150.0, 0.0, 60.0)  # 10 m in a 60 s cycle, queued 36 m from the line
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.idx == gi and ctl.phase == "GREEN"
    assert ctl.planned > sim.cfg.base_green and ctl.extended_by > 0
    assert sim.events.counts["LOCAL_EXTEND"] == 1
    assert "B07" in sim.events.recent(1, {"LOCAL_EXTEND"})[0].text and "cycle" in sim.events.recent(1)[0].text


def test_a_bus_that_made_good_progress_changes_nothing():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    feed_bus(sim, route, "B07", 0.0, 150.0, 0.0, 60.0)  # only 150 m... on this edge
    feed_bus(sim, route, "B07", 150.0, 150.0, 60.0, 60.0)
    sim.progress._tracks["B07"].odos[0] = sim.progress._tracks["B07"].odo - 650.0  # but 650 m over the cycle
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green and sim.events.counts["LOCAL_EXTEND"] == 0


def test_the_extension_scales_with_how_little_the_bus_moved():
    sim_a, ctl_a, _, route, _, _ = cycle_setup()
    feed_bus(sim_a, route, "B07", 100.0, 100.0, 0.0, 60.0)  # stuck completely
    start_green(sim_a, ctl_a, 60.0, 0.0)
    sim_b, ctl_b, _, route_b, _, _ = cycle_setup()
    feed_bus(sim_b, route_b, "B07", 100.0, 100.0, 0.0, 60.0)
    sim_b.progress._tracks["B07"].odos[0] -= 0.25 * 60 * FREE  # moved 25% of free flow: still "low", but less
    start_green(sim_b, ctl_b, 60.0, 0.0)
    assert ctl_a.extended_by > ctl_b.extended_by > 0


def test_nothing_is_decided_without_a_whole_cycle_of_history():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    feed_bus(sim, route, "B07", 140.0, 150.0, 40.0, 60.0)  # only watched for 20 s of a 60 s cycle
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green


def test_the_first_green_of_a_phase_has_no_previous_cycle_to_judge():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    feed_bus(sim, route, "B07", 140.0, 150.0, 0.0, 60.0)
    ctl._last_green.pop(gi, None)
    sim.t = 60.0
    ctl.step(60.0, sim.cfg.dt)
    assert ctl.planned == sim.cfg.base_green


def test_buses_too_far_or_not_heading_for_the_light_are_ignored():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    seg = sim.net.segments[approach]
    feed_bus(sim, route, "FAR", 5.0, 5.0, 0.0, 60.0)  # waiting at the start of the edge: 181 m out is inside 300 m
    sim.loops.latest.clear()  # forget the observations but keep the history
    far = BusTelemetry("FAR", 60.0, 0, 0, seg.id, 450.0, 1.0, FREE, 0.0, line=route.ref, leg=0)  # 450 m out
    sim.loops.observe(far)
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green
    sim2, ctl2, _, route2, _, gi2 = cycle_setup()
    other_line = BusTelemetry("X", 60.0, 0, 0, seg.id, 60.0, 1.0, FREE, 0.0, line="??", leg=0)  # cannot be judged
    sim2.loops.observe(other_line)
    start_green(sim2, ctl2, 60.0, 0.0)
    assert ctl2.planned == sim2.cfg.base_green


def test_no_extension_when_the_streets_ahead_are_jammed():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    feed_bus(sim, route, "B07", 140.0, 150.0, 0.0, 60.0)
    coord.dload[approach] = 1.0  # everything it would release into is full
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green and sim.events.counts["LOCAL_EXTEND"] == 0


def test_extension_is_capped_at_max_green_and_uses_the_network_budget():
    sim, ctl, coord, route, approach, gi = cycle_setup(adaptive=True)
    feed_bus(sim, route, "B07", 140.0, 140.0, 0.0, 60.0)
    sim.coord._ext_log.extend((59.0, 99, 20.0) for _ in range(5))  # the budget is already spent
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green and sim.events.counts["BUDGET_DENIED"] == 1
    sim2, ctl2, _, route2, _, _ = cycle_setup()
    feed_bus(sim2, route2, "B07", 140.0, 140.0, 0.0, 60.0)
    object.__setattr__(sim2.cfg, "base_green", sim2.cfg.max_green - 3.0)  # almost no room left
    start_green(sim2, ctl2, 60.0, 0.0)
    assert ctl2.planned <= sim2.cfg.max_green


def test_fixed_timers_ignore_stalled_buses():
    sim, ctl, coord, route, approach, gi = cycle_setup(adaptive=False)
    feed_bus(sim, route, "B07", 140.0, 140.0, 0.0, 60.0)
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green and sim.events.counts["LOCAL_EXTEND"] == 0


def test_grid_wide_budget_denies_extensions_when_exhausted():
    sim, ctl, coord = make(budget_seconds=4.5)
    assert coord.request_extension(FOUR_WAY, 4.0, now=0.0) > 0
    assert coord.request_extension(FOUR_WAY, 4.0, now=1.0) == 0.0
    assert sim.events.counts["BUDGET_DENIED"] == 1
    assert coord.request_extension(FOUR_WAY, 4.0, now=sim.cfg.budget_window + 5) > 0


def test_a_jam_two_junctions_down_the_road_also_blocks_the_extension():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    assert coord._hop2[approach]  # there are streets two junctions on, found by walking the networkx graph
    first_hop = {t for t, _ in sim.net.turn_targets(approach)}
    assert not (set(coord._hop2[approach]) & first_hop)
    for edge in coord._hop2[approach]:
        jam_link(sim, edge)  # the street right ahead is clear, but the ones behind the next junction are full
    coord.refresh(0.5)
    assert coord.dload[approach] < sim.cfg.rho_gate <= sim.cfg.rho_block < coord.dload2[approach]
    assert coord.downstream_openness(approach) == pytest.approx(0.0)
    feed_bus(sim, route, "B07", 140.0, 150.0, 0.0, 60.0)
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned == sim.cfg.base_green and sim.events.counts["LOCAL_EXTEND"] == 0


def test_a_clear_road_two_junctions_on_leaves_the_extension_alone():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    coord.refresh(0.5)
    assert coord.downstream_openness(approach) == pytest.approx(1.0)
    feed_bus(sim, route, "B07", 140.0, 150.0, 0.0, 60.0)
    start_green(sim, ctl, 60.0, 0.0)
    assert ctl.planned > sim.cfg.base_green and sim.events.counts["LOCAL_EXTEND"] == 1


def test_a_forecast_into_a_jammed_street_is_not_pre_extended():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    sim.predictor._active[99] = forecast(99, approach, eta_in=15.0)
    coord.dload2[approach] = 1.0  # the junction after the next one is gridlocked
    start_green(sim, ctl, 60.0, 0.0)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 0 and ctl.planned == sim.cfg.base_green


def test_the_extension_budget_grows_with_the_number_of_signals(iasi_net):
    cfg = Config()
    small = Simulation(cfg, map_name="grid")
    big = Simulation(cfg, net=iasi_net)
    assert small.coord.budget_cap() == cfg.budget_min  # a tiny network still gets a workable floor
    assert big.coord.budget_cap() == pytest.approx(cfg.budget_per_junction * len(big.controllers))
    assert big.coord.budget_cap() > 4 * small.coord.budget_cap()
    fixed = Simulation(Config(budget_seconds=12.0), net=iasi_net)
    assert fixed.coord.budget_cap() == 12.0  # an explicit cap still overrides


def test_neighbour_extensions_damp_each_other():
    sim, ctl, coord = make()
    first = coord.request_extension(1, 8.0, now=0.0)  # node 1 is adjacent to node 5
    second = coord.request_extension(FOUR_WAY, 8.0, now=1.0)
    assert first == pytest.approx(8.0) and second < 8.0


# -- feed-forward ----------------------------------------------------------------------------


def forecast(fid, seg, eta_in, density=0.8, conf=0.7, now=60.0):
    """A forecast issued just before `now` for a bus due `eta_in` seconds later."""
    return Forecast(
        fid, "B07", seg, 1, now - 5.0, now + eta_in, 2.0, density, conf, now + 90.0, predicted_delay=25.0, line="G1"
    )


def test_a_forecast_of_low_progress_extends_the_green_that_is_starting_once():
    sim, ctl, coord, route, approach, gi = cycle_setup()
    sim.predictor._active[99] = forecast(99, approach, eta_in=15.0)
    start_green(sim, ctl, 60.0, 0.0)
    assert sim.events.counts["PREDICTIVE_EXTEND"] == 1 and ctl.planned > sim.cfg.base_green
    before = ctl.planned
    ctl._review_buses(60.0, None)  # the same forecast is not used twice
    assert ctl.planned == before


def test_forecasts_that_are_far_off_dense_or_unreliable_are_ignored():
    for fc in (
        forecast(1, 0, eta_in=140.0),
        forecast(2, 0, eta_in=10.0, density=0.05),
        forecast(3, 0, eta_in=10.0, conf=0.1),
    ):
        sim, ctl, coord, route, approach, gi = cycle_setup()
        object.__setattr__(fc, "target_segment", approach)
        sim.predictor._active[fc.id] = fc
        start_green(sim, ctl, 60.0, 0.0)
        assert ctl.planned == sim.cfg.base_green
        assert sim.events.counts["PREDICTIVE_EXTEND"] == 0


# -- graph-aware bottleneck prevention ---------------------------------------------------------


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
    sim.loops.observe(BusTelemetry("BX", 0.0, 0, 0, seg, 100.0, 5.0, FREE, 0.0, line="G1"))  # inside the 150 m window
    coord.refresh(0.0)
    assert coord.throttle[seg] == 1.0


def test_throttle_never_applied_in_fixed_mode():
    sim, ctl, coord = make(adaptive=False)
    for target, _ in sim.net.turn_targets(ctl.current[0]):
        jam_link(sim, target)
    coord.refresh(0.0)
    assert all(coord.release_factor(s.id) == 1.0 for s in sim.net.segments)
