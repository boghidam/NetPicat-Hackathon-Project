"""Runtime traffic controls: the amount of cars and buses (0-1000%) and rush hour at a key point."""

import math
import sys

import pytest

from smartcity.config import Config
from smartcity.main import apply_start_options, main
from smartcity.simulation import Simulation
from smartcity.traffic_model import TrafficModel


@pytest.fixture
def sim(iasi_net):
    return Simulation(Config(seed=2), net=iasi_net)


def cars_near(sim, node, radius=400.0):
    n = sim.net.nodes[node]
    count = 0
    for v in sim.model.iter_vehicles():
        if v.kind == "car":
            x, y, _, _ = sim.net.position(v.seg, max(0.0, v.pos))
            count += math.hypot(x - n.x, y - n.y) <= radius
    return count


def no_overlaps(sim):
    for lane in sim.model.lanes:
        for ahead, behind in zip(lane, lane[1:], strict=False):
            assert behind.pos <= ahead.pos - ahead.length + 1e-6


# -- the amount of traffic ---------------------------------------------------------


def test_the_ladder_runs_from_zero_through_normal_to_ten_times_normal():
    steps = Simulation.TRAFFIC_STEPS
    assert steps[0] == 0 and 100 in steps and steps[-1] == 1000
    assert list(steps) == sorted(set(steps))


def test_stepping_walks_the_ladder_and_clamps_at_both_ends(sim):
    seen = [sim.traffic_percent]
    for _ in range(30):
        sim.step_traffic(+1)
        seen.append(sim.traffic_percent)
    assert seen[0] == 100 and seen[-1] == 1000 and seen == sorted(seen)
    for _ in range(40):
        sim.step_traffic(-1)
    assert sim.traffic_percent == 0
    sim.set_traffic_percent(5000)
    assert sim.traffic_percent == 1000
    sim.set_traffic_percent(-20)
    assert sim.traffic_percent == 0


def test_normal_traffic_is_100_percent_and_changes_nothing(iasi_net):
    a = Simulation(Config(seed=5), net=iasi_net)
    b = Simulation(Config(seed=5), net=iasi_net)
    assert a.traffic_percent == 100 and a.model.demand_scale == 1.0 and a.rush_point is None
    b.set_traffic_percent(100)  # a no-op
    a.run(60)
    b.run(60)
    snap = lambda s: [(v.id, v.seg, round(v.pos, 6)) for v in s.model.iter_vehicles()]  # noqa: E731
    assert snap(a) == snap(b)
    assert len(a.fleet.buses) == sum(a.fleet._base.values()) == 100


def test_the_global_traffic_amount_in_the_config_sets_the_starting_traffic(iasi_net):
    from smartcity import config

    assert config.TRAFFIC_AMOUNT == 1.0 and Config().traffic_amount == config.TRAFFIC_AMOUNT  # 1.0 is 100%
    normal = Simulation(Config(seed=4), net=iasi_net)
    double = Simulation(Config(seed=4, traffic_amount=2.0), net=iasi_net)
    assert normal.traffic_percent == 100 and double.traffic_percent == 200
    assert len(double.fleet.buses) == 2 * len(normal.fleet.buses)
    assert 1.7 * normal.model.n_cars <= double.model.n_cars <= 2.3 * normal.model.n_cars
    none = Simulation(Config(seed=4, traffic_amount=0.0), net=iasi_net)
    assert len(none.fleet.buses) == 0 and none.model.n_cars == 0


def test_zero_percent_means_no_cars_and_no_buses(sim):
    sim.run(30)
    sim.set_traffic_percent(0)
    assert sim.model.n_cars == 0 and len(sim.fleet.buses) == 0
    assert not any(v.kind in ("car", "bus") for v in sim.model.iter_vehicles())
    published = sim.api.published
    sim.run(60)  # nothing spawns, nothing breaks
    assert sim.model.n_cars == 0 and len(sim.fleet.buses) == 0 and sim.api.published == published
    assert sim.model.queued_outside == 0
    assert sim.metrics()["cars"] == 0


def test_buses_scale_with_the_percentage_and_cover_every_line(sim):
    lines = {r.ref for r in sim.net.routes}
    for percent, total in ((10, 10), (50, 50), (150, 150), (300, 300), (1000, 1000)):
        sim.set_traffic_percent(percent)
        assert len(sim.fleet.buses) == total, percent
        assert sim.fleet.targets(percent).keys() == lines
    sim.set_traffic_percent(100)
    assert len(sim.fleet.buses) == 100
    assert {b.line: 0 for b in sim.fleet.buses}.keys() == lines  # every line is back
    assert sorted(sim.fleet.targets(100).values()) == sorted(sim.fleet._base.values())


def test_boosting_adds_cars_straight_away_in_proportion(sim):
    sim.run(30)
    before = sim.model.n_cars
    sim.set_traffic_percent(300)
    assert 2.5 * before <= sim.model.n_cars <= 3.5 * before
    sim.set_traffic_percent(1000)
    assert sim.model.n_cars >= 6 * before
    sim.run(30)
    no_overlaps(sim)


def test_lowering_thins_the_cars_and_buses_straight_away(sim):
    sim.run(30)
    before = sim.model.n_cars
    sim.set_traffic_percent(50)
    assert 0.35 * before <= sim.model.n_cars <= 0.65 * before
    assert len(sim.fleet.buses) == 50
    sim.run(30)
    no_overlaps(sim)


def test_the_spawn_rate_follows_the_percentage():
    def spawned(scale):
        cfg = Config(initial_cars=0, seed=3)
        from smartcity.data import build_city

        world = TrafficModel(build_city("grid", cfg), cfg, __import__("random").Random(3))
        world.demand_scale = scale
        for i in range(int(120 / cfg.dt)):
            world.step(i * cfg.dt, cfg.dt, lambda s: "G", lambda s: 1.0)
        return world._ids

    assert spawned(0.0) == 0
    assert 2.2 <= spawned(3.0) / spawned(1.0) <= 3.8


def test_buses_taken_off_the_map_are_forgotten_by_every_consumer(sim):
    sim.run(60)
    ids = {b.id for b in sim.fleet.buses}
    sim.set_traffic_percent(30)
    gone = ids - {b.id for b in sim.fleet.buses}
    assert gone
    for bus_id in gone:
        assert bus_id not in sim.loops.latest
        assert sim.progress.odometer(bus_id) is None
        assert bus_id not in sim.predictor._tracks
        assert all(f.source_bus != bus_id for f in sim.predictor.active(sim.t))
    sim.run(30)  # and nothing resurrects them
    assert not (gone & set(sim.loops.latest))


def test_going_back_up_from_zero_restores_a_normal_fleet_and_some_cars(sim):
    sim.set_traffic_percent(0)
    sim.set_traffic_percent(100)
    assert len(sim.fleet.buses) == 100
    assert sim.model.n_cars >= 0.7 * sim.cfg.initial_cars
    sim.run(60)
    no_overlaps(sim)
    assert sim.api.published > 0


def test_a_full_boosted_minute_keeps_every_lane_ordered(sim):
    sim.set_traffic_percent(1000)
    for _ in range(int(60 / sim.cfg.dt)):
        sim.step()
    no_overlaps(sim)
    assert len(sim.fleet.buses) == 1000 and sim.model.n_cars > 1000


# -- rush hour at a key point -----------------------------------------------------------


def test_the_eight_marked_key_points_can_be_chosen(sim):
    assert sim.key_points == [
        "Copou",
        "Păcurari",
        "Gara Iași",
        "Piața Unirii",
        "Bd. Independenței",
        "Palas",
        "Podu Roș",
        "Tudor Vladimirescu",
    ]


def test_every_key_point_maps_to_a_real_signalised_junction_close_by(sim):
    for name, lat, lon in sim.net.landmarks_ll:
        node = sim.net.nodes[sim._junction_near(name)]
        x, y = sim.net.proj.to_xy(lat, lon)
        assert node.signalised and not node.gate
        assert math.hypot(node.x - x, node.y - y) < 300


def test_choosing_a_key_point_is_case_insensitive_and_rejects_unknown_names(sim):
    sim.set_rush_point("podu roș")
    assert sim.rush_point == "Podu Roș" and sim.model.rush_node == sim._junction_near("Podu Roș")
    assert any("RUSH HOUR at Podu Roș" in e.text for e in sim.events.recent(5, {"INFO"}))
    with pytest.raises(ValueError, match="unknown key point"):
        sim.set_rush_point("Atlantis")
    assert sim.rush_point == "Podu Roș"  # a bad name changes nothing


def test_cycling_visits_off_then_every_key_point_and_back_to_off(sim):
    visited = [sim.rush_point]
    for _ in range(9):
        sim.cycle_rush_point(+1)
        visited.append(sim.rush_point)
    assert visited == [None, *sim.key_points, None]
    sim.cycle_rush_point(-1)
    assert sim.rush_point == sim.key_points[-1]


def test_rush_hour_starts_with_commuters_routed_through_the_key_point(sim):
    node = sim._junction_near("Copou")

    def through(s):
        net = s.net
        return sum(
            1
            for v in s.model.iter_vehicles()
            if v.kind == "car" and any(node in (net.segments[e].from_node, net.segments[e].to_node) for e in v.route)
        )

    before = through(sim)
    sim.set_rush_point("Copou")
    assert through(sim) - before >= 0.8 * sim.cfg.rush_seed
    cars = sim.model.n_cars
    sim.run(120)
    assert sim.model.rush_node == node and sim.model.n_cars > 0.5 * cars
    no_overlaps(sim)


@pytest.mark.parametrize("name", ["Copou", "Podu Roș"])
def test_rush_hour_really_loads_the_key_point(iasi_net, name):
    results = {}
    for rush in (False, True):
        s = Simulation(Config(seed=1), net=iasi_net)
        s.run(60)
        if rush:
            s.set_rush_point(name)
        node = s._junction_near(name)
        counts = []
        for _ in range(12):
            s.run(10)
            counts.append(cars_near(s, node))
        results[rush] = sum(counts) / len(counts)
    assert results[True] >= 2.0 * max(results[False], 1.0)


def test_lifting_rush_hour_stops_the_extra_stream(sim):
    sim.set_rush_point("Palas")
    sim.run(30)
    sim.set_rush_point(None)
    assert sim.rush_point is None and sim.model.rush_node is None
    assert not any(sim.model.rush_backlog.values())
    assert any("rush hour over" in e.text for e in sim.events.recent(5, {"INFO"}))
    sim.set_rush_point(None)  # lifting twice is harmless


def test_rush_hour_respects_the_traffic_amount(sim):
    sim.set_traffic_percent(0)
    sim.set_rush_point("Palas")
    sim.run(30)
    assert sim.model.n_cars == 0
    sim.set_traffic_percent(300)
    assert sim.model.n_cars > 0


# -- the controls in the window and on the command line ---------------------------------


def test_start_options_set_the_traffic_amount_and_the_key_point(sim):
    apply_start_options(sim, 250, "Tudor Vladimirescu")
    assert sim.traffic_percent == 250 and sim.rush_point == "Tudor Vladimirescu"
    apply_start_options(sim, None, None)  # nothing requested, nothing changed
    assert sim.traffic_percent == 250 and sim.rush_point == "Tudor Vladimirescu"


@pytest.mark.parametrize("args", [["--traffic", "2000"], ["--traffic", "-5"], ["--rush", "Atlantis"]])
def test_bad_command_line_values_are_rejected(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["smartcity", *args])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_clicking_a_key_point_on_the_map_finds_it(sim):
    import pygame

    from smartcity.visualizer import CityView

    pygame.init()
    try:
        city = CityView(sim, pygame.Rect(0, 0, 880, 860))
        for name, lat, lon in sim.net.landmarks_ll:
            sx, sy = city.camera.latlon_to_screen(lat, lon)
            assert city.landmark_at((int(sx), int(sy))) == name
        far = (city.rect.right - 2, city.rect.bottom - 2)
        assert city.landmark_at(far) is None
    finally:
        pygame.quit()
