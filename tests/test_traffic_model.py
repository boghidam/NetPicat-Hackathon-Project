import random

import pytest

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.traffic_model import TrafficModel, Vehicle


def make(**kw):
    cfg = Config(initial_cars=0, gate_demand=0.0, **kw)
    net = build_city("grid", cfg)
    return cfg, net, TrafficModel(net, cfg, random.Random(1))


def turn(net, seg_id, kind):
    return next(t for t, _ in net.turn_targets(seg_id) if net.turn_kind(seg_id, t) == kind)


def put(world, seg_id, pos, v=0.0, kind="car", next_seg=None):
    cfg = world.cfg
    route = [seg_id] + ([next_seg] if next_seg is not None else [])
    veh = Vehicle(
        f"T{len(list(world.iter_vehicles()))}", kind, seg_id, pos, v,
        cfg.car_length if kind == "car" else cfg.bus_length, 1.0, route=route, next_seg=next_seg,
    )  # fmt: skip
    world._insert(world.lanes[seg_id], veh)
    return veh


def run(world, seconds, state="G", factor=1.0):
    dt = world.cfg.dt
    for i in range(int(seconds / dt)):
        world.step(i * dt, dt, lambda s: state, lambda s: factor)


def road(net, a, b):
    return net.segment_between(a, b)


def test_vehicle_stops_at_the_stop_line_on_red_not_in_the_junction():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    car = put(w, seg.id, 20.0, v=12.0, next_seg=turn(net, seg.id, "straight"))
    run(w, 40, state="R")
    assert car.pos <= seg.stop_pos
    assert car.pos > seg.stop_pos - 1.0 and car.v == 0.0


def test_vehicle_proceeds_through_the_junction_on_green():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    nxt = turn(net, seg.id, "straight")
    car = put(w, seg.id, 20.0, v=12.0, next_seg=nxt)
    run(w, 30, state="G")
    assert car.seg == nxt


def test_car_follows_without_overlap_and_queues_with_a_gap():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    nxt = turn(net, seg.id, "straight")
    cars = [put(w, seg.id, 20.0 + 25 * i, v=12.0, next_seg=nxt) for i in range(5)]
    run(w, 60, state="R")
    lane = w.lanes[seg.id]
    for ahead, behind in zip(lane, lane[1:], strict=False):
        assert behind.pos <= ahead.pos - ahead.length - cfg.min_gap + 0.05
    assert len(lane) == len(cars)


def test_speed_follows_each_edges_own_limit():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    car = put(w, seg.id, 5.0, v=0.0, next_seg=turn(net, seg.id, "straight"))
    run(w, 12, state="G")
    assert car.v == pytest.approx(seg.speed_limit * car.speed_factor, rel=0.02)


def test_vehicle_waits_at_the_line_when_the_exit_is_full():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    nxt = turn(net, seg.id, "straight")
    blocker = put(w, nxt, 3.0, kind="blocker")  # a stalled vehicle right at the start of the next edge
    car = put(w, seg.id, 100.0, v=10.0, next_seg=nxt)
    run(w, 30, state="G")
    assert car.seg == seg.id and car.pos <= seg.stop_pos  # green, but it will not enter the box
    w.lanes[nxt].remove(blocker)
    run(w, 20, state="G")
    assert car.seg == nxt


def test_yellow_stops_those_who_can_and_lets_a_too_fast_vehicle_through():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    nxt = turn(net, seg.id, "straight")
    far = put(w, seg.id, 60.0, v=12.0, next_seg=nxt)
    near = put(w, seg.id, seg.stop_pos - 6.0, v=12.0, next_seg=nxt)  # cannot stop in 6 m at 12 m/s
    run(w, 25, state="Y")
    assert near.seg == nxt
    assert far.seg == seg.id and far.pos <= seg.stop_pos


def test_throttle_spaces_releases_by_the_reduced_rate():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    nxt = turn(net, seg.id, "straight")
    for i in range(6):
        put(w, seg.id, seg.stop_pos - 2 - i * 7.0, next_seg=nxt)
    dt, crossed_at, seen = cfg.dt, [], set()
    for i in range(int(60 / dt)):
        w.step(i * dt, dt, lambda s: "G", lambda s: 0.25)
        for v in w.lanes[nxt]:
            if v.id not in seen:
                seen.add(v.id)
                crossed_at.append(i * dt)
    gaps = [b - a for a, b in zip(crossed_at, crossed_at[1:], strict=False)]
    assert gaps and min(gaps) >= 1 / (cfg.sat_flow * 0.25) - 0.3  # >= 8 s between cars at g = 0.25


def test_buses_are_not_throttled():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    put(w, seg.id, seg.stop_pos - 2, kind="bus", next_seg=turn(net, seg.id, "straight"))
    w.next_release[seg.id] = 1e9  # a car would be held for a very long time
    run(w, 10, state="G", factor=0.1)
    assert not w.lanes[seg.id]


def test_stalled_vehicle_blocks_the_lane_then_clears():
    cfg, net, w = make()
    seg = net.segments[road(net, 4, 5)]
    car = put(w, seg.id, 10.0, v=12.0, next_seg=turn(net, seg.id, "straight"))
    w.start_incident(seg.id, now=0.0)
    blocker, _ = w.incidents[seg.id]
    run(w, 30, state="G")
    assert car.seg == seg.id and car.pos < blocker.pos  # cannot overtake
    dt = cfg.dt
    for i in range(int((cfg.incident_duration + 5) / dt)):
        w.step(30 + i * dt, dt, lambda s: "G", lambda s: 1.0)
    assert seg.id not in w.incidents and car.seg != seg.id


def test_cars_follow_a_gate_to_gate_path_and_leave_at_the_end():
    cfg = Config(initial_cars=0, gate_demand=0.3)
    net = build_city("grid", cfg)
    w = TrafficModel(net, cfg, random.Random(5))
    paths = {}
    for i in range(int(400 / cfg.dt)):
        w.step(i * cfg.dt, cfg.dt, lambda s: "G", lambda s: 1.0)
        for v in w.iter_vehicles():
            assert v.route is not None and v.seg == v.route[v.leg]  # never off its route
            paths.setdefault(v.id, v.route)
    assert w.exited > 20 and w.n_cars > 0
    for route in paths.values():  # each route runs from one gate to another
        assert net.nodes[net.segments[route[0]].from_node].gate and net.nodes[net.segments[route[-1]].to_node].gate


def test_initial_cars_are_placed_without_overlap():
    cfg = Config(initial_cars=90)
    net = build_city("grid", cfg)
    w = TrafficModel(net, cfg, random.Random(3))
    assert w.n_cars >= 80
    for lane in w.lanes:
        for ahead, behind in zip(lane, lane[1:], strict=False):
            assert behind.pos <= ahead.pos - ahead.length


def test_density_is_occupied_fraction_of_the_edge():
    cfg, net, w = make()
    seg = road(net, 4, 5)
    assert w.density(seg) == 0.0
    put(w, seg, 50.0)
    assert w.density(seg) == pytest.approx((cfg.car_length + cfg.min_gap) / 200.0)


def crossing_times(w, cars, state="G", seconds=40):
    """When each car first appears on a different edge from the one it started on."""
    dt, started, when = w.cfg.dt, {c.id: c.seg for c in cars}, {}
    for i in range(int(seconds / dt)):
        w.step(i * dt, dt, lambda s: state, lambda s: 1.0)
        for c in cars:
            if c.id not in when and c.seg != started[c.id]:
                when[c.id] = i * dt
    return when


def test_both_sides_of_a_green_phase_pass_at_the_same_time():
    cfg, net, w = make()
    west = net.segments[road(net, 4, 5)]  # eastbound into node 5
    east = net.segments[road(net, 6, 5)]  # westbound into node 5: the opposite approach, same phase
    assert net.partner[west.id] == east.id
    a = [put(w, west.id, west.stop_pos - 2 - 7 * i, next_seg=turn(net, west.id, "straight")) for i in range(4)]
    b = [put(w, east.id, east.stop_pos - 2 - 7 * i, next_seg=turn(net, east.id, "straight")) for i in range(4)]
    when = crossing_times(w, a + b)
    assert len(when) == 8  # every car of both queues got through
    for ca, cb in zip(a, b, strict=True):
        assert abs(when[ca.id] - when[cb.id]) < 1.5  # side by side, not one side after the other
    assert max(when.values()) < 15


def test_a_left_turner_holds_up_neither_the_cars_behind_it_nor_the_oncoming_side():
    cfg, net, w = make()
    west = net.segments[road(net, 4, 5)]
    east = net.segments[road(net, 6, 5)]
    left = put(w, west.id, west.stop_pos - 2, next_seg=turn(net, west.id, "left"))
    behind = put(w, west.id, west.stop_pos - 9, next_seg=turn(net, west.id, "straight"))
    oncoming = [
        put(w, east.id, east.stop_pos - 2 - 7 * i, v=8.0, next_seg=turn(net, east.id, "straight")) for i in range(5)
    ]
    when = crossing_times(w, [left, behind, *oncoming])
    assert len(when) == 7  # nobody waited for anybody else to clear the box
    assert when[left.id] < 5 and when[behind.id] < 8 and max(when[c.id] for c in oncoming) < 12


def test_streams_merging_into_the_same_edge_both_get_through_one_behind_the_other():
    cfg, net, w = make()
    a = net.segments[road(net, 1, 5)]  # southbound into node 5, turning right (west)
    b = net.segments[road(net, 9, 5)]  # northbound into node 5, turning left (west)
    target = turn(net, a.id, "right")
    assert turn(net, b.id, "left") == target
    va = put(w, a.id, a.stop_pos - 1.0, v=4.0, next_seg=target)
    vb = put(w, b.id, b.stop_pos - 1.0, v=4.0, next_seg=target)
    dt, when = cfg.dt, {}
    for i in range(int(20 / dt)):
        w.step(i * dt, dt, lambda s: "G", lambda s: 1.0)
        lane = w.lanes[target]
        for ahead, behind in zip(lane, lane[1:], strict=False):
            assert behind.pos <= ahead.pos - ahead.length + 1e-6  # they follow, never overlap
        for v, src in ((va, a), (vb, b)):
            if v.id not in when and v.seg != src.id:
                when[v.id] = i * dt
    assert len(when) == 2 and abs(when[va.id] - when[vb.id]) < 3.0  # not made to take turns at the line
