import random

from smartcity.config import Config
from smartcity.mock_api import BusFleet, TelemetryBus, default_routes
from smartcity.network import CityGrid


def make(seed=3, **kw):
    cfg = Config(**kw)
    grid = CityGrid(cfg)
    api = TelemetryBus()
    return cfg, grid, api, BusFleet(grid, cfg, random.Random(seed), api)


def run(fleet, cfg, seconds, state="G", speed=12.0):
    for i in range(int(seconds / cfg.dt)):
        fleet.step(i * cfg.dt, cfg.dt, lambda s: state, lambda s: speed)


def test_routes_are_closed_loops_over_real_segments():
    cfg, grid, *_ = make()
    for route in default_routes(grid):
        assert len(route) >= 4
        for a, b in zip(route, route[1:] + route[:1], strict=True):
            assert grid.segments[a].to_node == grid.segments[b].from_node


def test_every_bus_publishes_each_tick_with_all_fields():
    cfg, grid, api, fleet = make()
    got = []
    api.subscribe(got.append)
    out = fleet.step(0.0, cfg.dt, lambda s: "G", lambda s: 12.0)
    assert len(out) == len(fleet.buses) == len(got)
    rec = got[0]
    assert rec.bus_id.startswith("B") and 0 <= rec.dist_to_next_node <= 200
    assert rec.free_speed == cfg.free_speed


def test_free_flow_buses_accumulate_almost_no_delay():
    cfg, grid, api, fleet = make()
    run(fleet, cfg, 60.0)
    assert max(b.delay for b in fleet.buses) < 15.0  # only the initial acceleration


def test_red_light_stops_bus_and_builds_delay():
    cfg, grid, api, fleet = make()
    run(fleet, cfg, 120.0, state="R")
    assert all(b.v == 0.0 for b in fleet.buses)
    assert max(b.delay for b in fleet.buses) > 60.0


def test_buses_keep_minimum_gap_in_a_queue():
    cfg, grid, api, fleet = make(buses_per_route=4)
    run(fleet, cfg, 120.0, state="R")
    by_seg = {}
    for b in fleet.buses:
        by_seg.setdefault(b.segment_id, []).append(b.pos)
    for poss in by_seg.values():
        poss.sort()
        assert all(b - a >= cfg.bus_gap - 0.5 for a, b in zip(poss, poss[1:], strict=False))


def test_fleet_is_deterministic_for_a_seed():
    def snapshot():
        cfg, grid, api, fleet = make(seed=11)
        run(fleet, cfg, 30.0)
        return [(b.segment_id, round(b.pos, 6)) for b in fleet.buses]

    assert snapshot() == snapshot()
