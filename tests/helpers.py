"""Small hand-made cities for tests: an arbitrary star-shaped junction and a bent road."""

from __future__ import annotations

import math

from smartcity.config import Config
from smartcity.network import EdgeData, GraphData, LocalProjection, RoadNetwork

PROJ = LocalProjection(47.16, 27.58)


def _ll(x: float, y: float) -> tuple[float, float]:
    return PROJ.to_latlon(x, y)


def star_data(angles_deg: list[float], arm: float = 200.0, speed_kph: float = 50.0, pad: float = 400.0) -> GraphData:
    """One centre node (id 0) with a two-way road to a node at the end of each arm.

    `pad` adds isolated far-away nodes so the junction itself is not on the map boundary (and so not a gate).
    """
    nodes = [(0, *_ll(0.0, 0.0))]
    edges: list[EdgeData] = []
    for i, a in enumerate(angles_deg, start=1):
        x, y = arm * math.cos(math.radians(a)), arm * math.sin(math.radians(a))
        nodes.append((i, *_ll(x, y)))
        for u, v, p, q in ((0, i, (0.0, 0.0), (x, y)), (i, 0, (x, y), (0.0, 0.0))):
            geometry = [tuple(reversed(_ll(*p))), tuple(reversed(_ll(*q)))]  # (lon, lat)
            edges.append(EdgeData(len(edges), u, v, arm, speed_kph, geometry, "secondary", f"arm{i}"))
    if pad:
        for k, (sx, sy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
            nodes.append((len(angles_deg) + 1 + k, *_ll(sx * pad, sy * pad)))
    return GraphData(nodes, edges)


def star_net(angles_deg: list[float], cfg: Config | None = None, pad: float = 400.0) -> RoadNetwork:
    return RoadNetwork(star_data(angles_deg, pad=pad), cfg or Config())


def bent_net(cfg: Config | None = None) -> RoadNetwork:
    """Two nodes joined by an L-shaped road: 100 m east, then 100 m north (plus the way back)."""
    pts = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    geometry = [tuple(reversed(_ll(x, y))) for x, y in pts]
    nodes = [(0, *_ll(*pts[0])), (1, *_ll(*pts[-1]))]
    edges = [
        EdgeData(0, 0, 1, 200.0, 50.0, geometry, "secondary", "bend"),
        EdgeData(1, 1, 0, 200.0, 50.0, list(reversed(geometry)), "secondary", "bend"),
    ]
    return RoadNetwork(GraphData(nodes, edges), cfg or Config())
