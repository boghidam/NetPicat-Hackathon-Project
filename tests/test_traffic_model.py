import random

import pytest

from smartcity.config import Config
from smartcity.network import CityGrid
from smartcity.traffic_model import TrafficModel, Vehicle


def make(**kw):
    cfg = Config(initial_cars=0, gate_demand=0.0, **kw)
    grid = CityGrid(cfg)
    return cfg, grid, TrafficModel(grid, cfg, random.Random(1))


def put(world, seg_id, pos, v=0.0, kind="car", next_seg=None):
    cfg = world.cfg
    veh = Vehicle(f"T{len(list(world.iter_vehicles()))}", kind, seg_id, pos, v,
                  cfg.car_length if kind == "car" else cfg.bus_length, cfg.free_speed, next_seg=next_seg, goal=world.grid.exits[-1])  # fmt: skip
    world._insert(world.lanes[seg_id], veh)
    return veh


def run(world, seconds, state="G", factor=1.0):
    dt = world.cfg.dt
    for i in range(int(seconds / dt)):
        world.step(i * dt, dt, lambda s: state, lambda s: factor)


def road(grid, a, b):
    return grid.segment_between(a, b)


def test_vehicle_stops_at_the_stop_line_on_red_not_in_the_junction():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    car = put(w, seg.id, 20.0, v=12.0, next_seg=grid.straight_next(seg.id))
    run(w, 40, state="R")
    assert car.pos <= seg.stop_pos
    assert car.pos > seg.stop_pos - 1.0 and car.v == 0.0


def test_vehicle_proceeds_through_the_junction_on_green():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    nxt = grid.straight_next(seg.id)
    car = put(w, seg.id, 20.0, v=12.0, next_seg=nxt)
    run(w, 30, state="G")
    assert car.seg == nxt


def test_car_follows_without_overlap_and_queues_with_a_gap():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    cars = [put(w, seg.id, 20.0 + 25 * i, v=12.0, next_seg=grid.straight_next(seg.id)) for i in range(5)]
    run(w, 60, state="R")
    lane = w.lanes[seg.id]
    for ahead, behind in zip(lane, lane[1:], strict=False):
        assert behind.pos <= ahead.pos - ahead.length - cfg.min_gap + 0.05
    assert len(lane) == len(cars)


def test_vehicle_waits_at_the_line_when_the_exit_is_full():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    nxt = grid.straight_next(seg.id)
    blocker = put(w, nxt, 3.0, kind="blocker")  # a stalled vehicle right at the start of the next link
    car = put(w, seg.id, 100.0, v=10.0, next_seg=nxt)
    run(w, 30, state="G")
    assert car.seg == seg.id and car.pos <= seg.stop_pos  # green, but it will not enter the box
    w.lanes[nxt].remove(blocker)
    run(w, 20, state="G")
    assert car.seg == nxt


def test_yellow_stops_those_who_can_and_lets_a_too_fast_vehicle_through():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    nxt = grid.straight_next(seg.id)
    far = put(w, seg.id, 60.0, v=12.0, next_seg=nxt)  # 126 m from the line: can stop
    near = put(w, seg.id, seg.stop_pos - 6.0, v=12.0, next_seg=nxt)  # 6 m away at 12 m/s: cannot
    run(w, 25, state="Y")
    assert near.seg == nxt
    assert far.seg == seg.id and far.pos <= seg.stop_pos


def test_throttle_spaces_releases_by_the_reduced_rate():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    nxt = grid.straight_next(seg.id)
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
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    nxt = grid.straight_next(seg.id)
    put(w, seg.id, seg.stop_pos - 2, kind="bus", next_seg=nxt)
    w.next_release[seg.id] = 1e9  # a car would be held for a very long time
    run(w, 10, state="G", factor=0.1)
    assert not w.lanes[seg.id]


def test_stalled_vehicle_blocks_the_lane_then_clears():
    cfg, grid, w = make()
    seg = grid.segments[road(grid, 4, 5)]
    car = put(w, seg.id, 10.0, v=12.0, next_seg=grid.straight_next(seg.id))
    w.start_incident(seg.id, now=0.0)
    blocker, _ = w.incidents[seg.id]
    run(w, 30, state="G")
    assert car.seg == seg.id and car.pos < blocker.pos  # cannot overtake
    dt = cfg.dt
    for i in range(int((cfg.incident_duration + 5) / dt)):
        w.step(30 + i * dt, dt, lambda s: "G", lambda s: 1.0)
    assert seg.id not in w.incidents and car.seg != seg.id


def test_cars_enter_at_gates_and_leave_at_exits():
    cfg = Config(initial_cars=0, gate_demand=0.3)
    grid = CityGrid(cfg)
    w = TrafficModel(grid, cfg, random.Random(5))
    for i in range(int(400 / cfg.dt)):
        w.step(i * cfg.dt, cfg.dt, lambda s: "G", lambda s: 1.0)
    assert w.exited > 20
    assert w.n_cars > 0


def test_initial_cars_are_placed_without_overlap():
    cfg = Config(initial_cars=90)
    grid = CityGrid(cfg)
    w = TrafficModel(grid, cfg, random.Random(3))
    assert w.n_cars >= 80
    for lane in w.lanes:
        for ahead, behind in zip(lane, lane[1:], strict=False):
            assert behind.pos <= ahead.pos - ahead.length


def test_density_is_occupied_fraction_of_link():
    cfg, grid, w = make()
    seg = road(grid, 4, 5)
    assert w.density(seg) == 0.0
    put(w, seg, 50.0)
    assert w.density(seg) == pytest.approx((cfg.car_length + cfg.min_gap) / 200.0)
