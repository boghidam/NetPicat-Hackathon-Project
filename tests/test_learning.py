"""Unit and integration tests for online counterfactual junction learning and bus green holds."""

from __future__ import annotations

import pytest

from smartcity.config import Config
from smartcity.learning import (
    LEARNED_KINDS,
    MAX_TRUST,
    MIN_TRUST,
    JunctionLearner,
    default_iasi_memory,
    load_memory,
    merge_memories,
    save_memory,
)
from smartcity.main import compare
from smartcity.mock_api import BusTelemetry
from smartcity.simulation import Simulation

FOUR_WAY = 5
FREE = 13.9


def _setup_grid_ctl(**kw):
    sim = Simulation(Config(**kw), adaptive=True, map_name="grid")
    ctl = sim.controllers[FOUR_WAY]
    route = sim.net.route_by_ref["G5"]
    approach = route.edges[0]
    gi = next(i for i, g in enumerate(ctl.groups) if approach in g)
    ctl.idx = gi
    ctl.phase = "GREEN"
    ctl.elapsed = ctl.planned = sim.cfg.base_green
    return sim, ctl, route, approach


def _emit(sim, route, bus_id, t, dist_to_stop, speed=8.0, leg=0):
    seg = sim.net.segments[route.edges[leg]]
    tel = BusTelemetry(bus_id, t, 0.0, 0.0, seg.id, dist_to_stop, speed, FREE, 0.0, line=route.ref, leg=leg)
    sim.progress.on_telemetry(tel)
    sim.loops.observe(tel)
    return tel


def test_bus_hold_lengthens_green_at_end_of_phase_when_bus_is_approaching():
    sim, ctl, route, approach = _setup_grid_ctl()
    _emit(sim, route, "B01", 20.0, dist_to_stop=35.0, speed=8.0)
    ctl.step(20.0, sim.cfg.dt)
    assert ctl.phase == "GREEN"
    assert ctl.planned > sim.cfg.base_green
    assert sim.events.counts["BUS_HOLD"] == 1


def test_bus_hold_does_not_fire_when_downstream_is_blocked():
    sim, ctl, route, approach = _setup_grid_ctl()
    _emit(sim, route, "B01", 20.0, dist_to_stop=35.0, speed=8.0)
    sim.coord.dload[approach] = 0.95  # downstream is jammed
    ctl.step(20.0, sim.cfg.dt)
    assert ctl.phase == "YELLOW"
    assert sim.events.counts["BUS_HOLD"] == 0


def test_learner_judges_decisive_wasted_and_missed_extensions_from_odometer():
    # 1. Decisive BUS_HOLD: bus crosses the stop line during the hold
    sim, ctl, route, _ = _setup_grid_ctl(learn_rate=0.2)
    lrn = sim.coord.learner
    assert lrn is not None
    _emit(sim, route, "B01", 20.0, dist_to_stop=30.0, speed=8.0)
    lrn.open(ctl, "BUS_HOLD", "B01", dist=30.0, granted=4.0, deadline=20.0, now=20.0)
    _emit(sim, route, "B01", 25.0, dist_to_stop=0.0, speed=8.0)  # advanced 30 m -> crossed!
    lrn.close(ctl, 25.0)
    assert lrn.outcomes["BUS_HOLD"]["decisive"] == 1
    assert lrn.trust(ctl.node, "BUS_HOLD") > 1.0

    # 2. Missed BUS_HOLD: bus stalls and does not reach the stop line even with the hold
    sim2, ctl2, route2, _ = _setup_grid_ctl(learn_rate=0.2)
    lrn2 = sim2.coord.learner
    _emit(sim2, route2, "B02", 20.0, dist_to_stop=30.0, speed=8.0)
    lrn2.open(ctl2, "BUS_HOLD", "B02", dist=30.0, granted=4.0, deadline=20.0, now=20.0)
    _emit(sim2, route2, "B02", 25.0, dist_to_stop=22.0, speed=0.5)  # only moved 8 m -> missed!
    lrn2.close(ctl2, 25.0)
    assert lrn2.outcomes["BUS_HOLD"]["missed"] == 1
    assert lrn2.trust(ctl2.node, "BUS_HOLD") < 1.0

    # 3. Wasted LOCAL_EXTEND: bus crossed well before the unextended deadline (t=20s)
    sim3, ctl3, route3, _ = _setup_grid_ctl(learn_rate=0.2)
    lrn3 = sim3.coord.learner
    _emit(sim3, route3, "B03", 0.0, dist_to_stop=40.0, speed=10.0)
    lrn3.open(ctl3, "LOCAL_EXTEND", "B03", dist=40.0, granted=5.0, deadline=20.0, now=0.0)
    _emit(sim3, route3, "B03", 10.0, dist_to_stop=0.0, speed=10.0)  # already crossed at t=10s < deadline 20s
    _emit(sim3, route3, "B03", 28.0, dist_to_stop=0.0, speed=10.0)
    lrn3.close(ctl3, 28.0)
    assert lrn3.outcomes["LOCAL_EXTEND"]["wasted"] == 1
    assert lrn3.trust(ctl3.node, "LOCAL_EXTEND") < 1.0


def test_merge_and_persist_memories(tmp_path):
    m1 = default_iasi_memory()
    m1["trust"] = {"10": {"BUS_HOLD": 1.1, "LOCAL_EXTEND": 0.4}}
    m1["count"] = {"10": 4}
    m1["outcomes"]["BUS_HOLD"]["decisive"] = 3

    m2 = default_iasi_memory()
    m2["trust"] = {"10": {"BUS_HOLD": 0.9, "LOCAL_EXTEND": 0.6}}
    m2["count"] = {"10": 4}
    m2["outcomes"]["BUS_HOLD"]["decisive"] = 5

    merged = merge_memories([m1, m2])
    assert merged["trust"]["10"]["BUS_HOLD"] == pytest.approx(1.0)
    assert merged["trust"]["10"]["LOCAL_EXTEND"] == pytest.approx(0.5)
    assert merged["outcomes"]["BUS_HOLD"]["decisive"] == 5

    out_file = tmp_path / "policy.json"
    save_memory(merged, out_file)
    loaded = load_memory(out_file, map_name="iasi")
    assert loaded is not None
    assert loaded["trust"]["10"]["BUS_HOLD"] == pytest.approx(1.0)
    for kind in LEARNED_KINDS:
        assert MIN_TRUST <= loaded["trust"]["10"][kind] <= MAX_TRUST


def test_compare_accepts_traffic_and_rush_options(capsys):
    compare(seconds=5.0, seeds=1, map_name="iasi", traffic=150.0, rush="Podu Roș")
    out = capsys.readouterr().out
    assert "traffic 150%" in out and "rush hour at Podu Roș" in out
    assert "fixed" in out and "adaptive" in out
