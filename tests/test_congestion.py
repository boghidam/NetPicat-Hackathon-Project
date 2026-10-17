import pytest

from smartcity.config import Config
from smartcity.congestion import CongestionEstimator
from smartcity.data import build_city
from smartcity.mock_api import BusTelemetry

FREE = 13.9  # m/s: the grid's 50 km/h limit


@pytest.fixture
def est_and_seg():
    cfg = Config()
    net = build_city("grid", cfg)
    return CongestionEstimator(net, cfg), net.segment_between(4, 5)


def tel(seg, d, speed, t=0.0):
    return BusTelemetry("B00", t, 0, 0, seg, d, speed, FREE, 0.0)


def test_prior_until_a_bus_reports(est_and_seg):
    est, seg = est_and_seg
    assert est.load(seg, now=0.0) == pytest.approx(est.cfg.est_prior_load)


def test_slow_bus_raises_load_and_fast_bus_lowers_it(est_and_seg):
    est, seg = est_and_seg
    est.on_telemetry(tel(seg, 100.0, 0.25 * FREE))  # 25% of the limit => load 0.75
    assert est.raw_load(seg) == pytest.approx(0.75)
    for _ in range(30):
        est.on_telemetry(tel(seg, 100.0, FREE))
    assert est.raw_load(seg) < 0.05


def test_ewma_update_uses_alpha(est_and_seg):
    est, seg = est_and_seg
    est.on_telemetry(tel(seg, 100.0, FREE))  # seeds ratio 1.0
    est.on_telemetry(tel(seg, 100.0, 0.0))
    assert est.ratio[seg] == pytest.approx(1.0 - est.cfg.est_alpha)


def test_samples_near_the_stop_line_or_edge_start_are_ignored(est_and_seg):
    est, seg = est_and_seg
    est.on_telemetry(tel(seg, 10.0, 0.0))  # braking zone
    est.on_telemetry(tel(seg, 190.0, 0.0))  # just left the previous junction
    assert est.samples[seg] == 0


def test_confidence_decays_with_staleness(est_and_seg):
    est, seg = est_and_seg
    for _ in range(4):
        est.on_telemetry(tel(seg, 100.0, 2.0, t=0.0))
    assert est.load(seg, now=0.0) > 0.7
    assert est.load(seg, now=600.0) == pytest.approx(est.cfg.est_prior_load, abs=0.02)
