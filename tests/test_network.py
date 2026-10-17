import pytest

from smartcity.config import Config
from smartcity.network import Axis, CityGrid, Dir


@pytest.fixture
def grid():
    return CityGrid(Config())


def test_segment_count_and_connectivity(grid):
    # 4x4 grid: 2 * (4*3 + 4*3) = 48 directed segments
    assert len(grid.segments) == 48
    for seg in grid.segments:
        back = grid.segment_between(seg.to_node, seg.from_node)
        assert grid.segments[back].direction == Dir((seg.direction + 2) % 4)


def test_corner_has_two_approaches_interior_has_four(grid):
    assert sum(len(grid.approaches(0, a)) for a in Axis) == 2
    assert sum(len(grid.approaches(5, a)) for a in Axis) == 4


def test_turn_shares_sum_to_one(grid):
    for seg in grid.segments:
        assert sum(s for _, s in grid.turn_targets(seg.id)) == pytest.approx(1.0)


def test_boundary_segment_exits_some_traffic(grid):
    # heading north into the top row: straight is impossible so that share exits
    seg = grid.segments[grid.in_seg[1][Dir.N]]
    targets = dict(grid.turn_targets(seg.id))
    assert targets[None] == pytest.approx(0.7)


def test_straight_next_continues_in_same_direction(grid):
    sid = grid.segment_between(0, 1)  # eastbound along the top row
    nxt = grid.straight_next(sid)
    assert grid.segments[nxt].from_node == 1 and grid.segments[nxt].direction == Dir.E


def test_lane_offset_is_on_the_right_hand_side(grid):
    north = grid.segment_between(4, 0)
    x_mid, _ = grid.position(north, 100.0)
    assert x_mid > grid.nodes[0].x  # northbound traffic keeps to the east side
