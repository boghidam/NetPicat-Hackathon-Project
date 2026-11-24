"""Load the committed Iași dataset (built by scripts/fetch_iasi.py) into plain graph data."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .buildings import generate_buildings, load_buildings_json
from .config import Config
from .network import EdgeData, GraphData, RoadNetwork, RouteData

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_iasi(data_dir: Path | None = None) -> tuple[GraphData, list[RouteData]]:
    """Road graph and CTP Iași bus routes for the whole city."""
    folder = data_dir or DATA_DIR
    core = json.loads((folder / "iasi_core.json").read_text(encoding="utf-8"))
    routes = json.loads((folder / "iasi_routes.json").read_text(encoding="utf-8"))
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
        RouteData(
            r["ref"],
            r["name"],
            r["edges"],
            [(s["lat"], s["lon"]) for s in r["stops"]],
            backbone=r.get("backbone", False),
            turnaround_leg=int(r.get("turnaround_leg", 0)),
            metropolitan=bool(r.get("metropolitan", False)),
        )
        for r in routes["routes"]
    ]
    return graph, lines


def build_city(name: str, cfg: Config) -> RoadNetwork:
    """The road network for a map name: "iasi" (real data) or "grid" (small synthetic city)."""
    if name == "iasi":
        graph, routes = load_iasi()
        net = RoadNetwork(graph, cfg, routes)
        r_path = DATA_DIR / "iasi_realism.json"
        if r_path.exists():
            net.apply_realism(json.loads(r_path.read_text(encoding="utf-8")))
        b_path = DATA_DIR / "iasi_buildings.json"
        buildings = (
            load_buildings_json(b_path, net)
            if b_path.exists()
            else generate_buildings(net, seed=42, urban_radius_m=cfg.urban_radius_m)
        )
        net.set_buildings(buildings)
        return net
    from .synthetic import GRID_GATE_BAND, GRID_MAX_GATES, grid_data, grid_routes

    # The test grid is a few hundred metres across: its gates are only the nodes on its edge, so the city-scale
    # gate settings in the config (a 700 m band, up to 16 gates) do not apply to it.
    net = RoadNetwork(grid_data(), replace(cfg, gate_band=GRID_GATE_BAND, max_gates=GRID_MAX_GATES))
    net.set_routes(grid_routes(net))
    return net
