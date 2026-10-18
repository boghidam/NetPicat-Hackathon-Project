import pytest

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.events import EventLog
from smartcity.mock_api import BusTelemetry
from smartcity.predictor import PostIntersectionPredictor


def make(**kw):
    cfg = Config(**kw)
    net = build_city("grid", cfg)
    route = net.route_by_ref["G5"]  # row 1, out and back: 4>5>6>7>6>5
    return cfg, net, route, PostIntersectionPredictor(net, cfg, EventLog())


def drive(pred, net, route, bus, legs_ratios, t0=0.0, line=None):
    """Feed telemetry for a bus that crosses edges of its line at the given constant speed ratios."""
    t, delay = t0, 0.0
    for leg, ratio in legs_ratios:
        seg = net.segments[route.edges[leg % len(route.edges)]]
        speed = ratio * seg.speed_limit
        for d in (190.0, 150.0, 110.0, 70.0, 30.0, 4.0):
            t += seg.length / speed / 6
            delay += (1 - ratio) * 2.0
            pred.on_telemetry(
                BusTelemetry(
                    bus,
                    t,
                    0,
                    0,
                    seg.id,
                    d,
                    speed,
                    seg.speed_limit,
                    delay,
                    line=route.ref if line is None else line,
                    leg=leg % len(route.edges),
                )
            )
    return t


def hops(pred, bus, now=0.0):
    return sorted((f for f in pred.active(now) if f.source_bus == bus), key=lambda f: f.hop)


def test_forecast_issued_only_after_a_crossing():
    cfg, net, route, pred = make()
    drive(pred, net, route, "B00", [(0, 0.5)])
    assert pred.active(now=0.0) == []  # no history yet: still on its first edge
    drive(pred, net, route, "B00", [(1, 0.5)], t0=100.0)
    assert route.edges[1] in {f.target_segment for f in pred.active(now=100.0)}


def test_slow_history_predicts_denser_traffic_than_fast_history():
    cfg, net, route, pred = make()
    drive(pred, net, route, "SLOW", [(0, 0.2), (1, 0.2)])
    drive(pred, net, route, "FAST", [(0, 0.95), (1, 0.95)])
    slow, fast = hops(pred, "SLOW")[0], hops(pred, "FAST")[0]
    assert slow.predicted_density > fast.predicted_density + 0.4
    assert slow.predicted_speed < fast.predicted_speed
    assert slow.predicted_delay > 5.0 > fast.predicted_delay  # seconds lost on that edge


def test_forecasts_follow_the_buses_route_for_several_hops():
    cfg, net, route, pred = make()
    drive(pred, net, route, "B00", [(0, 0.4), (1, 0.4)])  # has just entered leg 1
    fs = hops(pred, "B00")
    assert [f.hop for f in fs] == list(range(1, cfg.pred_hops + 1))
    assert [f.target_segment for f in fs] == [route.edges[1], route.edges[2], route.edges[3]]  # along the line
    assert all(b.eta > a.eta for a, b in zip(fs, fs[1:], strict=False))
    assert fs[1].confidence == pytest.approx(fs[0].confidence * cfg.pred_hop_decay)
    assert fs[2].confidence == pytest.approx(fs[1].confidence * cfg.pred_hop_decay)
    assert all(f.line == "G5" for f in fs)


def test_route_wraps_around_the_loop():
    cfg, net, route, pred = make()
    last = len(route.edges) - 1
    drive(pred, net, route, "B00", [(last - 1, 0.5), (last, 0.5)])
    assert [f.target_segment for f in hops(pred, "B00")] == [route.edges[last], route.edges[0], route.edges[1]]


def test_a_bus_without_a_known_line_only_gets_the_next_edge_forecast():
    cfg, net, route, pred = make()
    drive(pred, net, route, "B00", [(0, 0.5), (1, 0.5)], line="??")
    assert [f.hop for f in hops(pred, "B00")] == [1]


def test_forecast_window_moves_ahead_as_the_bus_clears_each_junction():
    cfg, net, route, pred = make()
    t = drive(pred, net, route, "B00", [(0, 0.5), (1, 0.5)])
    before = [f.target_segment for f in hops(pred, "B00", now=t)]
    t = drive(pred, net, route, "B00", [(2, 0.5)], t0=t)
    after = [f.target_segment for f in hops(pred, "B00", now=t)]
    assert before[0] == route.edges[1] and after == [route.edges[2], route.edges[3], route.edges[4]]


def test_forecast_resolves_when_bus_leaves_the_edge_and_records_error():
    cfg, net, route, pred = make()
    drive(pred, net, route, "B00", [(0, 0.5), (1, 0.5), (2, 0.5)])
    assert pred.resolved == 1 and pred.recent_resolved
    assert all(f.target_segment != route.edges[1] for f in pred.active(0))


def test_forecasts_expire():
    cfg, net, route, pred = make()
    drive(pred, net, route, "B00", [(0, 0.5), (1, 0.5)])
    assert pred.active(now=1e6) == []


def test_ridge_replaces_ewma_and_learns_a_persistent_pattern():
    cfg, net, route, pred = make(pred_min_samples=20, pred_refit_every=5)
    assert pred.model_name == "EWMA"
    for i in range(60):  # buses whose next-edge speed tracks their recent speed
        r = 0.15 + 0.8 * ((i * 37) % 100) / 100
        drive(pred, net, route, f"T{i}", [(0, r), (1, r), (2, r)])
    assert pred.model_name == "RIDGE"
    drive(pred, net, route, "TEST", [(0, 0.3), (1, 0.3)])
    assert hops(pred, "TEST")[0].predicted_density == pytest.approx(0.7, abs=0.15)
