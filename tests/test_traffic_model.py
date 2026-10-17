import random

import pytest

from smartcity.config import Config
from smartcity.network import CityGrid
from smartcity.traffic_model import TrafficModel


def make(**kw):
    cfg = Config(**kw)
    grid = CityGrid(cfg)
    return cfg, grid, TrafficModel(grid, cfg, random.Random(1))


def test_greenshields_speed_density():
    cfg, grid, m = make()
    m.count[0] = 0.0
    assert m.speed(0) == pytest.approx(cfg.free_speed)
    m.count[0] = 0.5 * m.cap[0]
    assert m.speed(0) == pytest.approx(0.5 * cfg.free_speed)
    m.count[0] = m.cap[0]
    assert m.speed(0) == pytest.approx(cfg.min_speed)


def test_red_signal_accumulates_traffic_and_conserves_nothing_leaves():
    cfg, grid, m = make(base_demand=0.1, demand_jitter=0.0)
    before = m.count[5]
    for i in range(100):
        m.step(i * cfg.dt, cfg.dt, lambda s: False, lambda s: 1.0)
    assert m.count[5] == pytest.approx(before + 0.1 * 10.0)


def test_green_discharges_into_targets_and_respects_receiving_space():
    cfg, grid, m = make(base_demand=0.0, demand_jitter=0.0)
    src = grid.segment_between(4, 5)
    for sid in range(len(m.count)):
        m.count[sid] = 0.0
    m.count[src] = 10.0
    straight = grid.straight_next(src)
    m.count[straight] = m.cap[straight]  # full: movement must be blocked
    m.step(0.0, cfg.dt, lambda s: s == src, lambda s: 1.0)
    assert m.count[src] == pytest.approx(10.0)
    m.count[straight] = 0.0
    m.step(0.1, cfg.dt, lambda s: s == src, lambda s: 1.0)
    assert m.count[src] < 10.0 and m.count[straight] > 0.0


def test_release_factor_throttles_discharge():
    cfg, grid, m = make(base_demand=0.0, demand_jitter=0.0)
    src = grid.segment_between(4, 5)
    m.count[src] = 10.0
    full = TrafficModel(grid, cfg, random.Random(1))
    full.count[src] = 10.0
    m.step(0.0, 0.1, lambda s: s == src, lambda s: 0.25)
    full.step(0.0, 0.1, lambda s: s == src, lambda s: 1.0)
    assert (10.0 - m.count[src]) == pytest.approx(0.25 * (10.0 - full.count[src]))


def test_incident_expires():
    cfg, grid, m = make()
    m.start_incident(3, now=0.0)
    assert m.out_mult[3] < 1.0
    m.step(cfg.incident_duration + 1, cfg.dt, lambda s: False, lambda s: 1.0)
    assert m.out_mult[3] == 1.0
