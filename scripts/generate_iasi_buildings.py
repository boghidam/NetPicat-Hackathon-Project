"""Generate the deterministic Iași residential buildings dataset (`data/iasi_buildings.json`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcity.buildings import generate_buildings, save_buildings_json
from smartcity.config import Config
from smartcity.data import DATA_DIR, load_iasi
from smartcity.network import RoadNetwork


def main() -> None:
    cfg = Config()
    graph, routes = load_iasi(DATA_DIR)
    net = RoadNetwork(graph, cfg, routes)
    buildings = generate_buildings(net, seed=42, urban_radius_m=4200.0)
    out = DATA_DIR / "iasi_buildings.json"
    save_buildings_json(buildings, out, urban_radius_m=4200.0)
    city_a = sum(1 for b in buildings if b.zone == "city" and b.kind == "apartment")
    city_h = sum(1 for b in buildings if b.zone == "city" and b.kind == "house")
    out_a = sum(1 for b in buildings if b.zone == "outer" and b.kind == "apartment")
    out_h = sum(1 for b in buildings if b.zone == "outer" and b.kind == "house")
    print(
        f"Generated {len(buildings)} buildings -> {out} "
        f"(city: {city_a} apts + {city_h} houses | outer: {out_a} apts + {out_h} houses)"
    )


if __name__ == "__main__":
    main()
