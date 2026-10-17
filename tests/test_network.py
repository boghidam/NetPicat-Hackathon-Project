import random

import pytest

from smartcity.config import Config
from smartcity.network import CityGrid, Dir


@pytest.fixture
def grid():
    return CityGrid(Config())


def test_roads_are_two_way_and_gates_attach_to_west_and_east_edges(grid):
    roads = [s for s in grid.segments if s.kind == "road"]
    assert len(roads) == 48  # 2 * (4*3 + 4*3) directed one-lane roads
    assert len(grid.entries) == len(grid.exits) == 8
    for seg in roads:
        back = grid.segment_between(seg.to_node, seg.from_node)
        assert grid.segments[back].direction == seg.direction.opposite


def test_junction_types_three_way_and_four_way(grid):
    counts = {n.id: len(grid.approaches(n.id)) for n in grid.nodes}
    assert counts[0] == 3  # corner: east, south and the west gate
    assert counts[1] == 3  # top edge: west, east, south
    assert counts[5] == 4  # interior
    assert counts[4] == 4  # west edge: north, south, east and the west gate
    assert set(counts.values()) == {3, 4}


def test_approaches_are_ordered_by_the_side_they_arrive_from(grid):
    sides = [s.from_side for s in grid.approaches(5)]
    assert sides == [Dir.N, Dir.E, Dir.S, Dir.W]


def test_stop_line_is_set_back_from_the_intersection_centre(grid):
    cfg = grid.cfg
    for seg in grid.segments:
        if seg.signalised:
            assert seg.stop_pos == pytest.approx(seg.length - cfg.box_half)
        else:
            assert seg.stop_pos == seg.length  # exits lead off the map


def test_turn_shares_sum_to_one_and_never_include_a_u_turn(grid):
    for seg in grid.segments:
        targets = grid.turn_targets(seg.id)
        if not seg.signalised:
            assert targets == []
            continue
        assert sum(s for _, s in targets) == pytest.approx(1.0)
        for t, _ in targets:
            assert grid.segments[t].direction != seg.direction.opposite


def test_t_junction_has_only_left_and_right_turns_when_straight_is_missing(grid):
    seg = grid.segments[grid.in_seg[1][Dir.N]]  # heading north into the top row at node 1
    dirs = {grid.segments[t].direction for t, _ in grid.turn_targets(seg.id)}
    assert dirs == {Dir.W, Dir.E}


def test_straight_next_continues_in_same_direction(grid):
    nxt = grid.straight_next(grid.segment_between(0, 1))
    assert grid.segments[nxt].from_node == 1 and grid.segments[nxt].direction == Dir.E


def test_lane_offset_is_on_the_right_hand_side(grid):
    north = grid.segment_between(4, 0)
    x_mid, _ = grid.position(north, 100.0)
    assert x_mid > grid.nodes[0].x  # northbound traffic keeps to the east side


def test_cars_are_steered_toward_their_exit(grid):
    rng = random.Random(1)
    goal = grid.exits[-1]  # east gate on the bottom row
    seg = grid.entries[0]  # west gate on the top row
    seen_goal = False
    for _ in range(200):
        cur, hops = seg, 0
        while cur != goal and hops < 20:
            cur = grid.choose_next(cur, goal, rng, follow=1.0)
            hops += 1
        seen_goal = seen_goal or cur == goal
        assert hops < 20 and cur == goal
    assert seen_goal
