import math

import pytest

from smartcity.network import LocalProjection, compass, wrap

from .helpers import bent_net, star_net

# -- projection ---------------------------------------------------------------


def test_projection_round_trips_and_has_sensible_scale():
    p = LocalProjection(47.16, 27.58)
    x, y = p.to_xy(47.17, 27.59)
    assert y == pytest.approx(0.01 * 110540, rel=1e-6)
    assert x == pytest.approx(0.01 * 111320 * math.cos(math.radians(47.16)), rel=1e-6)
    lat, lon = p.to_latlon(x, y)
    assert (lat, lon) == (pytest.approx(47.17), pytest.approx(27.59))


def test_compass_names_and_angle_wrap():
    assert (
        compass(0.0) == "E" and compass(math.pi / 2) == "N" and compass(math.pi) == "W" and compass(-math.pi / 2) == "S"
    )
    assert abs(wrap(3 * math.pi)) == pytest.approx(math.pi) and wrap(-3 * math.pi / 2) == pytest.approx(math.pi / 2)


# -- geometry ----------------------------------------------------------------


def test_polyline_position_follows_the_bend_and_keeps_to_the_right():
    net = bent_net()
    seg = net.segments[0]
    assert seg.cum[-1] == pytest.approx(200.0)
    x, y, dx, dy = net.point_at(0, 50.0)
    assert (dx, dy) == (pytest.approx(1.0), pytest.approx(0.0, abs=1e-6))  # heading east on the first leg
    x2, y2, dx2, dy2 = net.point_at(0, 150.0)
    assert (dx2, dy2) == (pytest.approx(0.0, abs=1e-6), pytest.approx(1.0))  # heading north after the corner
    assert y2 - y > 40  # it really went north
    px, py, _, _ = net.position(0, 50.0)
    assert py < y  # right-hand lane of an eastbound road is the southern side
    assert math.dist((px, py), (x, y)) == pytest.approx(net.cfg.lane_offset)


def test_stop_line_is_set_back_from_the_junction_centre():
    net = bent_net()
    for seg in net.segments:
        assert seg.stop_pos == pytest.approx(seg.length - net.cfg.box_half)


def test_headings_describe_the_ends_of_a_bent_road():
    seg = bent_net().segments[0]
    assert seg.heading_out == pytest.approx(0.0, abs=0.05)  # leaves heading east
    assert seg.heading_in == pytest.approx(math.pi / 2, abs=0.05)  # arrives heading north
    assert seg.arrives_from == "S"


# -- signal phases at different junction shapes ------------------------------------


def _sorted_sizes(net, node=0):
    return sorted(len(g) for g in net.phase_groups(node))


def test_four_way_pairs_opposite_streets():
    net = star_net([0, 90, 180, 270])
    groups = net.phase_groups(0)
    assert _sorted_sizes(net) == [2, 2]
    for a, b in groups:
        assert net.partner[a] == b and net.partner[b] == a
        assert abs(abs(wrap(net.segments[a].heading_in - net.segments[b].heading_in)) - math.pi) < 0.1


def test_t_junction_pairs_the_through_road_and_leaves_the_stem_alone():
    net = star_net([0, 180, 270])  # east, west and a stem to the south
    assert _sorted_sizes(net) == [1, 2]
    single = next(g for g in net.phase_groups(0) if len(g) == 1)
    assert net.segments[single[0]].arrives_from == "S"
    assert single[0] not in net.partner


def test_skewed_junctions_still_pair_roughly_opposite_streets():
    net = star_net([10, 100, 190, 285])  # a slightly skewed crossroads
    assert _sorted_sizes(net) == [2, 2]


def test_five_way_junction_gets_three_phases_covering_every_approach_once():
    net = star_net([0, 72, 144, 216, 288])
    groups = net.phase_groups(0)
    assert len(groups) == 3
    assert sorted(a for g in groups for a in g) == sorted(net.in_segs[0])


def test_two_approach_nodes_are_not_signalised():
    net = star_net([0, 180])
    assert not net.nodes[0].signalised  # just a road passing through
    assert net.phase_groups(0) == []


def test_turn_targets_exclude_u_turns_and_prefer_straight():
    net = star_net([0, 90, 180, 270])
    for seg in net.segments:
        if seg.to_node != 0:
            continue
        targets = dict(net.turn_targets(seg.id))
        assert sum(targets.values()) == pytest.approx(1.0)
        assert all(net.segments[t].to_node != seg.from_node for t in targets)
        kinds = {net.turn_kind(seg.id, t): s for t, s in targets.items()}
        assert kinds["straight"] > kinds["left"] and set(kinds) == {"straight", "left", "right"}


def test_dead_end_allows_a_u_turn():
    net = star_net([0])  # centre with a single arm
    arm_end = net.segments[0].to_node
    seg = net.segments[0]
    assert [net.segments[t].to_node for t, _ in net.turn_targets(seg.id)] == [0] and arm_end == 1


# -- synthetic grid and routing ------------------------------------------------------


def test_grid_has_gates_signals_and_connected_routes(grid_net):
    net = grid_net
    assert len(net.segments) == 48 and len(net.gates) == 10
    assert all(net.nodes[n].signalised for n in (5, 6, 9, 10))  # interior crossroads
    assert not any(net.nodes[g].signalised for g in net.gates)
    assert len(net.routes) == 9
    a, b = net.gates[0], net.gates[-1]
    path = net.path_between(a, b)
    assert path and net.segments[path[0]].from_node == a and net.segments[path[-1]].to_node == b
    for e1, e2 in zip(path, path[1:], strict=False):
        assert net.segments[e1].to_node == net.segments[e2].from_node


def test_set_routes_rejects_a_broken_line(grid_net):
    from smartcity.network import RouteData

    with pytest.raises(AssertionError):
        grid_net.set_routes([RouteData("X", "broken", [0, 5])])


def test_gates_are_chosen_on_the_boundary_with_traffic_both_ways():
    net = star_net([0, 90, 180, 270], pad=0)
    assert set(net.gates) == {1, 2, 3, 4}  # the four arm ends; the centre is not on the boundary
    assert net.nodes[0].signalised and not any(net.nodes[g].signalised for g in net.gates)
