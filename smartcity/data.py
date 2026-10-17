"""Load the committed Iași dataset (built by scripts/fetch_iasi.py) into plain graph data."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .network import EdgeData, GraphData, RoadNetwork, RouteData

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_iasi(data_dir: Path | None = None) -> tuple[GraphData, list[RouteData]]:
    """Road graph and CTP Iași bus routes for the central transit spine."""
    folder = data_dir or DATA_DIR
    core = json.loads((folder / "iasi_core.json").read_text())
    routes = json.loads((folder / "iasi_routes.json").read_text())
    graph = GraphData(
        nodes=[(n["id"], n["lat"], n["lon"]) for n in core["nodes"]],
        edges=[
            EdgeData(
                e["id"], e["u"], e["v"], e["length"], e["maxspeed_kph"],
                [(lon, lat) for lon, lat in e["geometry"]], e["highway"] or "", e["name"],
            )
            for e in core["edges"]
        ],
        landmarks=[(m["name"], m["lat"], m["lon"]) for m in core["meta"].get("landmarks", [])],
        source=core["meta"].get("source", ""),
    )  # fmt: skip
    lines = [
        RouteData(r["ref"], r["name"], r["edges"], [(s["lat"], s["lon"]) for s in r["stops"]]) for r in routes["routes"]
    ]
    return graph, lines


def build_city(name: str, cfg: Config) -> RoadNetwork:
    """The road network for a map name: "iasi" (real data) or "grid" (small synthetic city)."""
    if name == "iasi":
        graph, routes = load_iasi()
        return RoadNetwork(graph, cfg, routes)
    from .synthetic import grid_data, grid_routes

    net = RoadNetwork(grid_data(), cfg)
    net.set_routes(grid_routes(net))
    return net
