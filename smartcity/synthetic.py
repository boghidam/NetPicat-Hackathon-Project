"""A small synthetic city (rectangular grid) with the same interface as the real data.

Used by the tests, where a city whose layout is easy to reason about matters more than realism,
and as an offline fallback (`--map grid`).
"""

from __future__ import annotations

from .network import EdgeData, GraphData, LocalProjection, RoadNetwork, RouteData

GRID_GATE_BAND = 150.0  # m: only the nodes on the grid's own edge are gates
GRID_MAX_GATES = 10


def grid_data(rows: int = 4, cols: int = 4, spacing: float = 200.0, speed_kph: float = 50.0) -> GraphData:
    proj = LocalProjection(47.16, 27.58)
    xy = {r * cols + c: (c * spacing, -r * spacing) for r in range(rows) for c in range(cols)}  # row 0 is the top
    nodes = [(nid, *proj.to_latlon(x, y)) for nid, (x, y) in xy.items()]
    edges: list[EdgeData] = []
    for r in range(rows):
        for c in range(cols):
            a = r * cols + c
            for dr, dc in ((-1, 0), (0, 1), (1, 0), (0, -1)):  # N, E, S, W
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    b = rr * cols + cc
                    geometry = [tuple(reversed(proj.to_latlon(*xy[n]))) for n in (a, b)]  # (lon, lat)
                    edges.append(EdgeData(len(edges), a, b, spacing, speed_kph, geometry, "secondary", f"G{a}-{b}"))
    return GraphData(nodes, edges, source="synthetic grid")


def _rect(net: RoadNetwork, cols: int, r0: int, c0: int, r1: int, c1: int, clockwise: bool) -> list[int]:
    nodes = (
        [r0 * cols + c for c in range(c0, c1)]
        + [r * cols + c1 for r in range(r0, r1)]
        + [r1 * cols + c for c in range(c1, c0, -1)]
        + [r * cols + c0 for r in range(r1, r0, -1)]
    )
    return nodes if clockwise else [nodes[0]] + nodes[:0:-1]


def _line(cols: int, a: tuple[int, int], b: tuple[int, int]) -> list[int]:
    (r0, c0), (r1, c1) = a, b
    steps = max(abs(r1 - r0), abs(c1 - c0))
    dr, dc = (r1 > r0) - (r1 < r0), (c1 > c0) - (c1 < c0)
    out = [(r0 + dr * i) * cols + (c0 + dc * i) for i in range(steps + 1)]
    return out + out[-2:0:-1]


def grid_routes(net: RoadNetwork, rows: int = 4, cols: int = 4) -> list[RouteData]:
    """Perimeter, inner ring, rows, columns and a wide rectangle, as cyclic edge lists."""
    R, C = rows - 1, cols - 1
    cycles = [
        _rect(net, cols, 0, 0, R, C, True),
        _rect(net, cols, 0, 0, R, C, False),
        _rect(net, cols, 1, 1, R - 1, C - 1, True),
        _rect(net, cols, 1, 1, R - 1, C - 1, False),
        _line(cols, (1, 0), (1, C)),
        _line(cols, (R - 1, 0), (R - 1, C)),
        _line(cols, (0, 1), (R, 1)),
        _line(cols, (0, C - 1), (R, C - 1)),
        _rect(net, cols, 0, 1, R, C - 1, True),
    ]
    return [
        RouteData(
            f"G{i + 1}", f"grid loop {i + 1}", [net.segment_between(c[k], c[(k + 1) % len(c)]) for k in range(len(c))]
        )
        for i, c in enumerate(cycles)
    ]
