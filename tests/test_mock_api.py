import random

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.mock_api import BusFleet, TelemetryBus
from smartcity.traffic_model import TrafficModel


def make(map_name="grid", seed=3, **kw):
    cfg = Config(initial_cars=0, gate_demand=0.0, **kw)
    net = build_city(map_name, cfg)
    rng = random.Random(seed)
    world = TrafficModel(net, cfg, rng)
    api = TelemetryBus()
    return cfg, net, world, api, BusFleet(net, cfg, rng, world, api)


def run(world, fleet, cfg, seconds, state="G"):
    for i in range(int(seconds / cfg.dt)):
        world.step(i * cfg.dt, cfg.dt, lambda s: state, lambda s: 1.0)
        fleet.emit(i * cfg.dt)


def test_every_bus_publishes_each_tick_with_all_fields():
    cfg, net, world, api, fleet = make()
    got = []
    api.subscribe(got.append)
    out = fleet.emit(0.0)
    assert len(out) == len(fleet.buses) == len(got) == 18
    rec = got[0]
    seg = net.segments[rec.segment_id]
    assert rec.bus_id.startswith("B") and 0 <= rec.dist_to_next_node <= seg.stop_pos
    assert rec.free_speed == seg.speed_limit and rec.delay == 0.0
    assert rec.line in net.route_by_ref


def test_distance_reported_is_to_the_stop_line_not_the_junction_centre():
    cfg, net, world, api, fleet = make()
    bus = world.buses[0]
    seg = net.segments[bus.seg]
    rec = fleet.emit(0.0)[0]
    assert abs(rec.dist_to_next_node - max(0.0, seg.stop_pos - bus.pos)) < 1e-9


def test_free_flow_buses_accumulate_little_delay():
    cfg, net, world, api, fleet = make()
    run(world, fleet, cfg, 60.0)
    assert max(b.delay for b in world.buses) < 12.0  # only the initial acceleration and short queues


def test_red_light_stops_buses_before_the_junction_and_builds_delay():
    cfg, net, world, api, fleet = make()
    run(world, fleet, cfg, 120.0, state="R")
    for b in world.buses:
        seg = net.segments[b.seg]
        assert b.v == 0.0 and b.pos <= seg.stop_pos
    assert max(b.delay for b in world.buses) > 60.0


def test_buses_follow_their_line_strictly_and_loop_forever():
    cfg, net, world, api, fleet = make("iasi")
    visited = {b.id: [] for b in world.buses}
    for i in range(int(900 / cfg.dt)):
        world.step(i * cfg.dt, cfg.dt, lambda s: "G", lambda s: 1.0)
        fleet.emit(i * cfg.dt)
        for b in world.buses:
            assert b.route is not None and b.cyclic and b.seg == b.route[b.leg]
            if not visited[b.id] or visited[b.id][-1] != b.seg:
                visited[b.id].append(b.seg)
    for b in world.buses:  # the order of edges it drove is the order of its line
        n = len(b.route)
        seq = visited[b.id]
        for e1, e2 in zip(seq, seq[1:], strict=False):
            assert any(b.route[(i + 1) % n] == e2 for i, e in enumerate(b.route) if e == e1)
    assert max(len(v) for v in visited.values()) >= 6  # they really travel, not just sit at the start


def test_each_ctp_line_gets_buses_proportional_to_its_length():
    cfg, net, world, api, fleet = make("iasi")
    per_line = {}
    for b in world.buses:
        per_line[b.line] = per_line.get(b.line, 0) + 1
    assert set(per_line) == {r.ref for r in net.routes}
    assert all(2 <= n <= cfg.max_buses_per_line for n in per_line.values())
    assert per_line["28"] >= per_line["41"]  # the long line has at least as many as the short one


def test_fleet_is_deterministic_for_a_seed():
    def snapshot():
        cfg, net, world, api, fleet = make(seed=11)
        run(world, fleet, cfg, 30.0)
        return [(b.seg, round(b.pos, 6)) for b in world.buses]

    assert snapshot() == snapshot()
