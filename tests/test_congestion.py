import pytest

from smartcity.config import Config
from smartcity.congestion import CongestionEstimator
from smartcity.mock_api import BusTelemetry
from smartcity.network import CityGrid


def tel(seg, d, speed, t=0.0):
    return BusTelemetry("B00", t, 0, 0, seg, d, speed, 12.0, 0.0)


@pytest.fixture
def est():
    cfg = Config()
    return CongestionEstimator(CityGrid(cfg), cfg)


def test_prior_until_a_bus_reports(est):
    assert est.load(3, now=0.0) == pytest.approx(est.cfg.est_prior_load)


def test_slow_bus_raises_load_and_fast_bus_lowers_it(est):
    est.on_telemetry(tel(3, 100.0, 3.0))  # 25% of free speed => load 0.75
    assert est.raw_load(3) == pytest.approx(0.75)
    for _ in range(30):
        est.on_telemetry(tel(3, 100.0, 12.0))
    assert est.raw_load(3) < 0.05


def test_ewma_update_uses_alpha(est):
    est.on_telemetry(tel(3, 100.0, 12.0))  # seeds ratio 1.0
    est.on_telemetry(tel(3, 100.0, 0.0))
    assert est.ratio[3] == pytest.approx(1.0 - est.cfg.est_alpha)


def test_samples_near_the_stop_line_or_segment_start_are_ignored(est):
    est.on_telemetry(tel(3, 10.0, 0.0))  # braking zone
    est.on_telemetry(tel(3, 190.0, 0.0))  # just left the previous signal
    assert est.samples[3] == 0


def test_confidence_decays_with_staleness(est):
    for _ in range(4):
        est.on_telemetry(tel(3, 100.0, 2.0, t=0.0))
    assert est.load(3, now=0.0) > 0.7
    assert est.load(3, now=600.0) == pytest.approx(est.cfg.est_prior_load, abs=0.02)
