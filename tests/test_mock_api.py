import random

from smartcity.config import Config
from smartcity.mock_api import BusFleet, TelemetryBus, default_routes
from smartcity.network import CityGrid
from smartcity.traffic_model import TrafficModel


def make(seed=3, **kw):
    cfg = Config(initial_cars=0, gate_demand=0.0, **kw)
    grid = CityGrid(cfg)
    rng = random.Random(seed)
    world = TrafficModel(grid, cfg, rng)
    api = TelemetryBus()
    return cfg, grid, world, api, BusFleet(grid, cfg, rng, world, api)


def run(world, fleet, cfg, seconds, state="G"):
    for i in range(int(seconds / cfg.dt)):
        world.step(i * cfg.dt, cfg.dt, lambda s: state, lambda s: 1.0)
        fleet.emit(i * cfg.dt)


def test_routes_are_closed_loops_over_real_segments():
    cfg, grid, *_ = make()
    for route in default_routes(grid):
        assert len(route) >= 4
        for a, b in zip(route, route[1:] + route[:1], strict=True):
            assert grid.segments[a].to_node == grid.segments[b].from_node


def test_every_bus_publishes_each_tick_with_all_fields():
    cfg, grid, world, api, fleet = make()
    got = []
    api.subscribe(got.append)
    out = fleet.emit(0.0)
    assert len(out) == len(fleet.buses) == len(got) == 18
    rec = got[0]
    seg = grid.segments[rec.segment_id]
    assert rec.bus_id.startswith("B") and 0 <= rec.dist_to_next_node <= seg.stop_pos
    assert rec.free_speed == cfg.free_speed and rec.delay == 0.0


def test_distance_reported_is_to_the_stop_line_not_the_junction_centre():
    cfg, grid, world, api, fleet = make()
    bus = world.buses[0]
    seg = grid.segments[bus.seg]
    rec = fleet.emit(0.0)[0]
    assert abs(rec.dist_to_next_node - max(0.0, seg.stop_pos - bus.pos)) < 1e-9


def test_free_flow_buses_accumulate_little_delay():
    cfg, grid, world, api, fleet = make()
    run(world, fleet, cfg, 60.0)
    assert max(b.delay for b in world.buses) < 12.0  # only the initial acceleration and short queues


def test_red_light_stops_buses_before_the_junction_and_builds_delay():
    cfg, grid, world, api, fleet = make()
    run(world, fleet, cfg, 120.0, state="R")
    for b in world.buses:
        seg = grid.segments[b.seg]
        assert b.v == 0.0 and b.pos <= seg.stop_pos
    assert max(b.delay for b in world.buses) > 60.0


def test_fleet_is_deterministic_for_a_seed():
    def snapshot():
        cfg, grid, world, api, fleet = make(seed=11)
        run(world, fleet, cfg, 30.0)
        return [(b.seg, round(b.pos, 6)) for b in world.buses]

    assert snapshot() == snapshot()
