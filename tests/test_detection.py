import pytest

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.detection import DetectionLoops
from smartcity.mock_api import BusTelemetry


@pytest.fixture
def loops_and_net():
    cfg = Config()
    net = build_city("grid", cfg)
    return DetectionLoops(net, cfg), net


def tel(seg, d, speed, bus="B00", free=13.9):
    return BusTelemetry(bus, 0.0, 0.0, 0.0, seg, d, speed, free, 0.0, line="G1")


def read(loops, *records):
    for r in records:
        loops.observe(r)
    loops.refresh()


def road(net, a, b):
    return net.segment_between(a, b)


def test_windows_at_50_150_and_300_m_count_buses_on_the_approach(loops_and_net):
    loops, net = loops_and_net
    seg = road(net, 4, 5)
    read(loops, tel(seg, 30.0, 12.0, "A"), tel(seg, 100.0, 12.0, "B"), tel(seg, 140.0, 12.0, "C"))
    assert loops.reading(seg).count == [1, 3, 3]
    loops2 = DetectionLoops(net, Config())
    read(loops2, tel(seg, 180.0, 12.0))
    assert loops2.reading(seg).count == [0, 0, 1]  # only the outer window reaches that far


def test_windows_reach_back_across_the_junction_to_the_street_before(loops_and_net):
    loops, net = loops_and_net
    upstream, approach = road(net, 4, 5), road(net, 5, 6)
    straight = net.segments[upstream].stop_pos
    read(loops, tel(upstream, 60.0, 12.0))  # 60 m + 14 m box + 186 m = 260 m from the next stop line
    reading = loops.reading(approach)
    assert reading is not None and reading.count == [0, 0, 1]
    assert straight > 0


def test_a_street_that_cannot_turn_into_the_approach_does_not_feed_it(loops_and_net):
    loops, net = loops_and_net
    read(loops, tel(road(net, 6, 5), 60.0, 12.0))  # would have to U-turn to reach 5 -> 6
    assert loops.reading(road(net, 5, 6)) is None


def test_a_bus_standing_at_its_own_stop_line_is_not_congestion(loops_and_net):
    loops, net = loops_and_net
    seg = road(net, 4, 5)
    read(loops, tel(seg, 3.0, 0.0))  # waiting for the light, or just pulling away
    assert loops.reading(seg) is None or loops.reading(seg).density is None


def test_a_standing_bus_further_back_proves_a_queue_of_that_length(loops_and_net):
    loops, net = loops_and_net
    seg = road(net, 4, 5)
    read(loops, tel(seg, 80.0, 0.0))
    r = loops.reading(seg)
    stop = net.segments[seg].stop_pos
    assert r.queue_occupancy == pytest.approx((80.0 + Config().bus_length) / stop)
    assert r.density == pytest.approx(r.queue_occupancy) and r.density < 0.6  # not a full jam


def test_a_standing_bus_on_the_previous_street_means_the_queue_covers_the_whole_approach(loops_and_net):
    loops, net = loops_and_net
    read(loops, tel(road(net, 4, 5), 60.0, 0.0))
    assert loops.reading(road(net, 5, 6)).queue_occupancy == 1.0


def test_moving_but_slowed_buses_give_a_proportional_density(loops_and_net):
    loops, net = loops_and_net
    seg = road(net, 4, 5)
    read(loops, tel(seg, 100.0, 0.5 * 13.9))
    r = loops.reading(seg)
    assert r.queue_occupancy == 0.0 and r.density == pytest.approx(0.5, abs=0.01)


def test_no_buses_means_no_reading(loops_and_net):
    loops, net = loops_and_net
    loops.refresh()
    assert loops.reading(road(net, 4, 5)) is None


def test_slowest_bus_in_the_150m_window_is_reported_only_when_crawling(loops_and_net):
    loops, net = loops_and_net
    seg = road(net, 4, 5)
    read(loops, tel(seg, 100.0, 2.0, "SLOW"), tel(seg, 120.0, 12.0, "FAST"))
    assert loops.reading(seg).slowest.bus_id == "SLOW"
    loops2 = DetectionLoops(net, Config())
    read(loops2, tel(seg, 100.0, 12.0))
    assert loops2.reading(seg).slowest is None
