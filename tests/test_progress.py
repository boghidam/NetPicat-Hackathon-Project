import pytest

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.mock_api import BusTelemetry
from smartcity.progress import RouteProgress


@pytest.fixture
def setup():
    net = build_city("grid", Config())
    return net, net.route_by_ref["G1"], RouteProgress(net)  # the perimeter loop: 12 edges of 200 m


def tel(net, route, t, s, bus="B00"):
    """Telemetry for a bus that has covered `s` metres along its line (unwrapped)."""
    total = sum(net.segments[e].length for e in route.edges)
    s %= total
    leg = 0
    while s >= net.segments[route.edges[leg]].length:
        s -= net.segments[route.edges[leg]].length
        leg += 1
    seg = net.segments[route.edges[leg]]
    return BusTelemetry(
        bus, t, 0, 0, seg.id, max(0.0, seg.stop_pos - s), 10.0, seg.speed_limit, 0.0, line=route.ref, leg=leg
    )


def drive(net, route, prog, s0, speed, t0, t1, bus="B00"):
    t = t0
    while t <= t1:
        prog.on_telemetry(tel(net, route, t, s0 + speed * (t - t0), bus))
        t += 0.1


def test_odometer_follows_the_bus_along_its_route(setup):
    net, route, prog = setup
    drive(net, route, prog, 0.0, 10.0, 0.0, 60.0)
    assert prog.odometer("B00") == pytest.approx(600.0, abs=30.0)  # within the box-length rounding


def test_odometer_keeps_counting_across_laps(setup):
    net, route, prog = setup
    total = sum(net.segments[e].length for e in route.edges)
    last = 0.0
    for k in range(0, 700):
        t = k * 5.0
        prog.on_telemetry(tel(net, route, t, 10.0 * t * 1.2))  # 12 m/s, a bit over one lap in 200 s... and more
        odo = prog.odometer("B00")
        assert odo >= last - 1e-6  # never goes backwards
        last = odo
    assert last > 2 * total  # wrapped around the loop more than twice and kept counting


def test_travelled_over_the_last_span(setup):
    net, route, prog = setup
    drive(net, route, prog, 100.0, 10.0, 0.0, 100.0)
    assert prog.travelled("B00", 100.0, 50.0) == pytest.approx(500.0, abs=30.0)


def test_a_stationary_bus_has_travelled_almost_nothing(setup):
    net, route, prog = setup
    drive(net, route, prog, 100.0, 0.0, 0.0, 80.0)
    assert prog.travelled("B00", 80.0, 60.0) == pytest.approx(0.0, abs=1.0)


def test_a_bus_not_observed_for_the_whole_span_cannot_be_judged(setup):
    net, route, prog = setup
    drive(net, route, prog, 100.0, 10.0, 0.0, 30.0)
    assert prog.travelled("B00", 30.0, 60.0) is None
    assert prog.travelled("unknown", 30.0, 10.0) is None


def test_unknown_lines_are_ignored(setup):
    net, route, prog = setup
    prog.on_telemetry(BusTelemetry("X", 0.0, 0, 0, 0, 50.0, 5.0, 13.9, 0.0, line="??", leg=0))
    assert prog.odometer("X") is None
