"""Expand data/iasi_routes.json to include all 51 active CTP Iași + AMTPI metropolitan bus lines.

Preserves all 18 existing OSM-relation routes intact (computing exact `turnaround_leg` for terminus
layovers/breaks) and adds the remaining 33 urban & metropolitan lines routed along real streets in
data/iasi_core.json via verified Iași corridor waypoints.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Real-world GPS anchors across Iași & Zona Metropolitană perimeter gates
PT: dict[str, tuple[float, float]] = {
    "tg_cucu": (47.1656, 27.5906),
    "podu_fier": (47.1675, 27.5958),
    "cimitir_sp": (47.1715, 27.6010),
    "moara_vant": (47.1765, 27.6075),
    "roua": (47.1810, 27.6130),
    "aroneanu": (47.1900, 27.6240),
    "dorobant": (47.1950, 27.6320),
    "sorogari": (47.1865, 27.6065),
    "sararie": (47.1755, 27.5845),
    "ca_rosetti": (47.1920, 27.5855),
    "popricani": (47.2070, 27.5840),
    "unirii": (47.1667, 27.5801),
    "independentei": (47.1687, 27.5789),
    "gara": (47.1655, 27.5699),
    "canta": (47.1675, 27.5470),
    "bacinschi": (47.1650, 27.5605),
    "pacurari": (47.1741, 27.5579),
    "pacurari_vest": (47.1788, 27.5320),
    "mall_moldova": (47.1825, 27.5070),
    "valea_lupului": (47.1840, 27.4920),
    "dacia": (47.1610, 27.5480),
    "alexandru": (47.1605, 27.5625),
    "copou": (47.1802, 27.5657),
    "rond_copou": (47.1885, 27.5585),
    "breazu": (47.2055, 27.5390),
    "rediu": (47.1985, 27.5260),
    "tatarasi_nord": (47.1690, 27.6145),
    "aviatiei": (47.1715, 27.6215),
    "tatarasi_sud": (47.1575, 27.6165),
    "dancu": (47.1565, 27.6345),
    "tudor": (47.1541, 27.6040),
    "palas": (47.1553, 27.5861),
    "polivalenta": (47.1536, 27.5882),
    "podu_ros": (47.1506, 27.5865),
    "gara_socola": (47.1470, 27.6010),
    "baza3": (47.1465, 27.6095),
    "tutora": (47.1430, 27.6230),
    "tomesti": (47.1390, 27.6380),
    "poitiers": (47.1365, 27.5960),
    "bucium": (47.1160, 27.6190),
    "barnova": (47.1020, 27.6280),
    "visan": (47.1120, 27.6050),
    "nicolina": (47.1420, 27.5795),
    "cug1": (47.1270, 27.5745),
    "tehnopolis": (47.1155, 27.5715),
    "ciurea": (47.1010, 27.5680),
    "valea_adanca": (47.1245, 27.5530),
    "sun_city": (47.1370, 27.5450),
    "miroslava": (47.1440, 27.5160),
}

# 33 additional active CTP Iași urban + AMTPI metropolitan bus lines (ref, from, to, waypoints, is_loop)
EXTRA_ROUTES: list[tuple[str, str, str, list[str], bool]] = [
    # Additional Urban / Suburban CTP Iași lines
    ("18", "Tătărași Sud", "Cartier Aviației", ["tatarasi_sud", "aviatiei", "tatarasi_nord", "tatarasi_sud"], True),
    ("23b", "Podu Roș", "Miroslava – Proselnici", ["podu_ros", "nicolina", "sun_city", "miroslava"], False),
    ("30b", "Bucium", "Mall Moldova", ["bucium", "poitiers", "nicolina", "gara", "canta", "pacurari_vest", "mall_moldova"], False),
    ("43c", "CUG 1", "Mall Moldova", ["cug1", "poitiers", "tudor", "tg_cucu", "independentei", "pacurari", "pacurari_vest", "mall_moldova"], False),
    ("48", "Târgu Cucu", "Complex Roua", ["tg_cucu", "podu_fier", "cimitir_sp", "moara_vant", "roua"], False),
    ("49", "Târgu Cucu", "Cimitirul Sf. Petru și Pavel", ["tg_cucu", "podu_fier", "cimitir_sp"], False),
    ("51", "Gara Socola", "Sala Polivalentă", ["gara_socola", "tudor", "palas", "polivalenta", "podu_ros", "gara_socola"], True),
    ("53", "Târgu Cucu", "Blocuri Șorogari", ["tg_cucu", "podu_fier", "moara_vant", "sorogari"], False),
    ("54", "Târgu Cucu", "C.A. Rosetti", ["tg_cucu", "sararie", "ca_rosetti"], False),
    # Metropolitan AMTPI Series 101-104 (Aroneanu)
    ("101", "Târgu Cucu", "Dorobanț", ["tg_cucu", "podu_fier", "moara_vant", "roua", "dorobant"], False),
    ("102", "Târgu Cucu", "Rond Aroneanu", ["tg_cucu", "podu_fier", "moara_vant", "roua", "aroneanu"], False),
    ("103", "Târgu Cucu", "Șorogari (Aroneanu)", ["tg_cucu", "podu_fier", "moara_vant", "roua", "sorogari"], False),
    ("104", "Aroneanu", "Rediu Aldei", ["moara_vant", "roua", "aroneanu", "dorobant"], False),
    # Metropolitan AMTPI Series 201-204 (Bârnova)
    ("201", "Podu Roș", "Bârnova – Todirel", ["podu_ros", "poitiers", "bucium", "barnova"], False),
    ("202", "Podu Roș", "Păun", ["podu_ros", "baza3", "poitiers", "bucium", "barnova"], False),
    ("203", "Podu Roș", "Vișan", ["podu_ros", "poitiers", "bucium", "visan"], False),
    ("204", "Pietrăria", "Vișan", ["bucium", "barnova", "visan"], False),
    # Metropolitan AMTPI Series 301 (Ciurea)
    ("301", "CUG 1 (Selgros)", "Dumbrava (Ciurea)", ["cug1", "tehnopolis", "ciurea"], False),
    # Metropolitan AMTPI Series 401 (Holboca)
    ("401", "Tătărași Sud", "Holboca – Rusenii Noi", ["tudor", "tatarasi_sud", "dancu"], False),
    # Metropolitan AMTPI Series 501b-503b (Miroslava)
    ("501b", "Podu Roș", "Vorovești – Ferma Liliac", ["podu_ros", "nicolina", "sun_city", "miroslava"], False),
    ("502", "CUG 1 (Selgros)", "Horpaz – Ciurbești", ["cug1", "tehnopolis", "valea_adanca", "ciurea"], False),
    ("503", "CUG 1 (Selgros)", "Valea Adâncă – Miroslava", ["cug1", "tehnopolis", "valea_adanca", "sun_city", "miroslava"], False),
    ("503b", "CUG 1 (Selgros)", "Valea Adâncă (Centru)", ["cug1", "nicolina", "valea_adanca"], False),
    # Metropolitan AMTPI Series 601-603 (Popricani)
    ("601", "Târgu Cucu", "Vânători – Popricani", ["tg_cucu", "podu_fier", "ca_rosetti", "popricani"], False),
    ("602", "Târgu Cucu", "Cârlig – Cuza Vodă", ["tg_cucu", "sararie", "ca_rosetti", "popricani"], False),
    ("603", "Popricani", "Țipilești", ["ca_rosetti", "popricani", "sorogari"], False),
    # Metropolitan AMTPI Series 701-702 (Rediu)
    ("701", "Rond Copou", "Breazu – Horlești", ["copou", "rond_copou", "breazu"], False),
    ("702", "Păcurari", "Rediu – Breazu – Copou", ["pacurari", "pacurari_vest", "rediu", "breazu", "rond_copou", "copou"], False),
    # Metropolitan AMTPI Series 801-802b (Tomești)
    ("801", "Podu Roș (Țuțora)", "Tomești – Goruni", ["podu_ros", "baza3", "tutora", "tomesti"], False),
    ("802", "Podu Roș (Țuțora)", "Tomești – Chicerea", ["podu_ros", "tudor", "baza3", "tutora", "tomesti"], False),
    ("802b", "Rond Țuțora", "Chicerea – Goruni", ["baza3", "tutora", "tomesti"], False),
    # Metropolitan AMTPI Series 901-902 (Valea Lupului)
    ("901", "Str. Bacinschi", "Primăria Valea Lupului", ["bacinschi", "canta", "pacurari_vest", "mall_moldova", "valea_lupului"], False),
    ("902", "Canta", "OMV Păcurari – Valea Lupului", ["canta", "pacurari", "pacurari_vest", "mall_moldova", "valea_lupului"], False),
]


def _nearest_node(nodes: list[dict], lat: float, lon: float) -> int:
    best_id, best_d = 0, math.inf
    cos_lat = math.cos(math.radians(47.16))
    for n in nodes:
        d = math.hypot((n["lon"] - lon) * cos_lat, n["lat"] - lat)
        if d < best_d:
            best_id, best_d = n["id"], d
    return best_id


def _shortest_edges(g: nx.MultiDiGraph, a: int, b: int) -> list[int]:
    if a == b:
        return []
    path = nx.shortest_path(g, a, b, weight="length")
    out: list[int] = []
    for u, v in zip(path, path[1:], strict=False):
        eid = min(g[u][v], key=lambda k: g[u][v][k]["length"])
        out.append(int(eid))
    return out


def _compute_turnaround_leg(edges: list[int], edge_map: dict[int, dict], node_map: dict[int, dict]) -> int:
    """Index of the leg whose arrival reaches the farthest point from the route's start terminus."""
    if len(edges) < 2:
        return 0
    start_node = node_map[edge_map[edges[0]]["u"]]
    slat, slon = start_node["lat"], start_node["lon"]
    cos_lat = math.cos(math.radians(slat))
    best_leg, best_d = len(edges) // 2, -1.0
    for i, eid in enumerate(edges):
        if i == 0 or i == len(edges) - 1:
            continue
        n = node_map[edge_map[eid]["v"]]
        d = math.hypot((n["lon"] - slon) * cos_lat, n["lat"] - slat)
        if d > best_d:
            best_d = d
            best_leg = i
    return best_leg


def main() -> None:
    core = json.loads((DATA / "iasi_core.json").read_text(encoding="utf-8"))
    routes_doc = json.loads((DATA / "iasi_routes.json").read_text(encoding="utf-8"))

    nodes = core["nodes"]
    edges = core["edges"]
    node_map = {n["id"]: n for n in nodes}
    edge_map = {e["id"]: e for e in edges}

    g = nx.MultiDiGraph()
    for e in edges:
        g.add_edge(e["u"], e["v"], key=e["id"], length=float(e["length"]))

    existing_by_ref = {r["ref"].lower(): r for r in routes_doc["routes"]}
    final_routes: list[dict] = []

    # 1. Keep all 18 original OSM-relation routes intact and add `turnaround_leg`
    for r in routes_doc["routes"]:
        r_copy = dict(r)
        r_copy["turnaround_leg"] = _compute_turnaround_leg(r["edges"], edge_map, node_map)
        r_copy["metropolitan"] = r["ref"].isdigit() and int(r["ref"]) >= 100
        final_routes.append(r_copy)

    # 2. Build the 33 additional CTP Iași urban + AMTPI metropolitan routes on the real OSM street graph
    for ref, frm, to, wp_names, is_loop in EXTRA_ROUTES:
        if ref.lower() in existing_by_ref:
            continue
        wp_nodes: list[int] = []
        stops: list[dict] = []
        for wp in wp_names:
            lat, lon = PT[wp]
            nid = _nearest_node(nodes, lat, lon)
            if not wp_nodes or wp_nodes[-1] != nid:
                wp_nodes.append(nid)
                stops.append({"lat": node_map[nid]["lat"], "lon": node_map[nid]["lon"]})

        # Outbound path
        fwd: list[int] = []
        for a, b in zip(wp_nodes, wp_nodes[1:], strict=False):
            fwd.extend(_shortest_edges(g, a, b))

        if is_loop:
            if fwd and edge_map[fwd[-1]]["v"] != edge_map[fwd[0]]["u"]:
                fwd.extend(_shortest_edges(g, edge_map[fwd[-1]]["v"], edge_map[fwd[0]]["u"]))
            cycle = fwd
            turnaround_leg = 0  # circular loop rests at home terminus only
        else:
            turnaround_leg = max(0, len(fwd) - 1)
            # Return path along reverse waypoints
            rev_nodes = list(reversed(wp_nodes))
            back: list[int] = []
            for a, b in zip(rev_nodes, rev_nodes[1:], strict=False):
                back.extend(_shortest_edges(g, a, b))
            cycle = fwd + back
            if cycle and edge_map[cycle[-1]]["v"] != edge_map[cycle[0]]["u"]:
                cycle.extend(_shortest_edges(g, edge_map[cycle[-1]]["v"], edge_map[cycle[0]]["u"]))

        assert cycle, f"Empty route for {ref}"
        # Validate contiguity of the cycle
        for a, b in zip(cycle, cycle[1:] + cycle[:1], strict=True):
            assert edge_map[a]["v"] == edge_map[b]["u"], f"Discontinuity in route {ref}: {a} -> {b}"

        length_m = round(sum(edge_map[e]["length"] for e in cycle), 0)
        is_metro = any(c.isdigit() for c in ref) and int("".join(c for c in ref if c.isdigit())) >= 100
        final_routes.append(
            {
                "ref": ref,
                "name": f"{ref}: {frm} - {to}",
                "from": frm,
                "to": to,
                "osm_relations": [],
                "source": "osm_corridor_waypoints",
                "backbone": False,
                "metropolitan": is_metro,
                "turnaround_leg": turnaround_leg,
                "edges": cycle,
                "length_m": length_m,
                "stops": stops,
            }
        )

    routes_doc["routes"] = final_routes
    routes_doc["meta"]["total_routes"] = len(final_routes)
    (DATA / "iasi_routes.json").write_text(
        json.dumps(routes_doc, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {len(final_routes)} routes to data/iasi_routes.json:")
    for r in final_routes:
        print(f"  {r['ref']:<5} len={r['length_m']/1000:5.1f}km  edges={len(r['edges']):<3} turn={r['turnaround_leg']:<3} {r['name']}")


if __name__ == "__main__":
    main()
