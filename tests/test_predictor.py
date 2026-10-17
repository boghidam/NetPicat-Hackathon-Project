import pytest

from smartcity.config import Config
from smartcity.events import EventLog
from smartcity.mock_api import BusTelemetry
from smartcity.network import CityGrid
from smartcity.predictor import PostIntersectionPredictor

ROW = [(4, 5), (5, 6), (6, 7)]  # eastbound along row 1


def make(**kw):
    cfg = Config(**kw)
    grid = CityGrid(cfg)
    return cfg, grid, PostIntersectionPredictor(grid, cfg, EventLog())


def drive(pred, grid, cfg, bus, seg_ratios, t0=0.0):
    """Feed telemetry for a bus that crosses segments at the given constant speed ratios."""
    t, delay = t0, 0.0
    for seg, ratio in seg_ratios:
        for d in (190.0, 150.0, 110.0, 70.0, 30.0, 4.0):
            t += 200.0 / (ratio * cfg.free_speed) / 6
            delay += (1 - ratio) * 2.0
            pred.on_telemetry(BusTelemetry(bus, t, 0, 0, seg, d, ratio * cfg.free_speed, cfg.free_speed, delay))
    return t


def seg(grid, a, b):
    return grid.segment_between(a, b)


def test_forecast_issued_only_after_a_crossing():
    cfg, grid, pred = make()
    drive(pred, grid, cfg, "B00", [(seg(grid, 4, 5), 0.5)])
    assert pred.active(now=0.0) == []  # no history yet: first segment only
    drive(pred, grid, cfg, "B00", [(seg(grid, 5, 6), 0.5)], t0=100.0)
    targets = {f.target_segment for f in pred.active(now=100.0)}
    assert seg(grid, 5, 6) in targets


def test_slow_history_predicts_denser_traffic_than_fast_history():
    cfg, grid, pred = make()
    drive(pred, grid, cfg, "SLOW", [(seg(grid, 4, 5), 0.2), (seg(grid, 5, 6), 0.2)])
    drive(pred, grid, cfg, "FAST", [(seg(grid, 4, 5), 0.95), (seg(grid, 5, 6), 0.95)])
    slow = next(f for f in pred.active(0) if f.source_bus == "SLOW" and f.hop == 1)
    fast = next(f for f in pred.active(0) if f.source_bus == "FAST" and f.hop == 1)
    assert slow.predicted_density > fast.predicted_density + 0.4
    assert slow.eta - 0 > 0 and slow.predicted_speed < fast.predicted_speed


def test_second_hop_has_lower_confidence_and_later_eta():
    cfg, grid, pred = make()
    drive(pred, grid, cfg, "B00", [(seg(grid, 4, 5), 0.4), (seg(grid, 5, 6), 0.4)])
    hop1 = next(f for f in pred.active(0) if f.hop == 1)
    hop2 = next(f for f in pred.active(0) if f.hop == 2)
    assert hop2.target_segment == seg(grid, 6, 7)
    assert hop2.confidence == pytest.approx(hop1.confidence * cfg.pred_hop2_decay)
    assert hop2.eta > hop1.eta


def test_forecast_resolves_when_bus_leaves_segment_and_records_error():
    cfg, grid, pred = make()
    drive(pred, grid, cfg, "B00", [(seg(grid, 4, 5), 0.5), (seg(grid, 5, 6), 0.5), (seg(grid, 6, 7), 0.5)])
    assert pred.resolved == 1
    assert pred.recent_resolved
    assert all(f.hop == 2 or f.target_segment == seg(grid, 6, 7) for f in pred.active(0))


def test_forecasts_expire():
    cfg, grid, pred = make()
    drive(pred, grid, cfg, "B00", [(seg(grid, 4, 5), 0.5), (seg(grid, 5, 6), 0.5)])
    assert pred.active(now=1e6) == []


def test_ridge_replaces_ewma_and_learns_a_persistent_pattern():
    cfg, grid, pred = make(pred_min_samples=20, pred_refit_every=5)
    assert pred.model_name == "EWMA"
    legs = [seg(grid, 4, 5), seg(grid, 5, 6), seg(grid, 6, 7)]
    for i in range(60):  # buses whose next-segment speed tracks their recent speed
        r = 0.15 + 0.8 * ((i * 37) % 100) / 100
        drive(pred, grid, cfg, f"T{i}", [(leg, r) for leg in legs])
    assert pred.model_name == "RIDGE"
    drive(pred, grid, cfg, "TEST", [(legs[0], 0.3), (legs[1], 0.3)])
    f = next(f for f in pred.active(0) if f.source_bus == "TEST" and f.hop == 1)
    assert f.predicted_density == pytest.approx(0.7, abs=0.15)
