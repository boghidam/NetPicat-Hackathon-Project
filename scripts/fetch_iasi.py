"""Build the Iași dataset used by the simulation (run once; the results are committed).

    python scripts/fetch_iasi.py            # reuse data/raw/ if present
    python scripts/fetch_iasi.py --refresh  # download again from OpenStreetMap

Steps:
 1. Download, from Overpass, the arterial roads of a city-wide box *plus every way of the chosen
    CTP Iași bus relations*, so each bus corridor exists in the graph exactly as it is mapped
    (including the residential streets some lines use), and build the drive graph with OSMnx.
 2. Download the same relations (stops and member ways).
 3. Turn each chosen line into a cyclic sequence of graph edges: waypoints (the stops in relation
    order or, when OSM lists none as for line 52, points sampled along its ways) are joined by
    shortest paths that strongly prefer the relation's own ways.
 4. Take the largest strongly connected component, merge junction clusters closer than 25 m
    (OSMnx), and write compact JSON for the runtime.

The ten backbone lines (20, 23, 28, 30, 36, 41, 42, 43, 47, 52) come first, then the other lines
that OSM maps for CTP Iași. Map data (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
import time
from pathlib import Path

import networkx as nx
import numpy as np
import osmnx as ox
import requests
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"

# North, south, east, west: most of the built-up city from Breazu in the north to Blocuri Ciurea in the south,
# CUG and Canta in the west and south-east, Tătărași and Podu Roș in the east.
BBOX = (47.213, 47.095, 27.640, 27.465)
LANDMARKS = {
    "Copou": (47.18019, 27.56568),
    "Păcurari": (47.17405, 27.55788),
    "Gara Iași": (47.16553, 27.56987),
    "Piața Unirii": (47.16669, 27.58012),
    "Bd. Independenței": (47.16868, 27.57894),
    "Palas": (47.15531, 27.58611),
    "Podu Roș": (47.15056, 27.58651),
    "Tudor Vladimirescu": (47.15412, 27.60399),
}
BACKBONE = ["20", "23", "28", "30", "36", "41", "42", "43", "47", "52"]
OTHER = ["19", "29", "44", "46", "50", "6B", "27", "501"]
CHOSEN_LINES = BACKBONE + OTHER
ARTERIAL = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link"}
ROUTE_ROADS = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service"
DEFAULT_KPH = {
    "primary": 50,
    "secondary": 50,
    "tertiary": 40,
    "primary_link": 30,
    "secondary_link": 30,
    "tertiary_link": 30,
    "unclassified": 40,
    "residential": 40,
    "living_street": 20,
    "service": 20,
}
ABBREV = [
    ("Bulevardul ", "Bd. "),
    ("Strada ", "Str. "),
    ("Șoseaua ", "Șos. "),
    ("Splaiul ", "Spl. "),
    ("Piața ", "P-ța "),
]
UA = {"User-Agent": "smartcity-demo/0.1 (hackathon project, github.com/boghidam)"}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
PREFER = 0.05  # cost factor for edges that belong to the relation's own ways


def first(value):
    return value[0] if isinstance(value, list) else value


def kph(data: dict) -> float:
    raw = first(data.get("maxspeed"))
    if raw is not None:
        m = re.match(r"\s*(\d+)", str(raw))
        if m and float(m.group(1)) >= 10:  # "0" and similar are tagging noise
            return float(m.group(1))
        if str(raw).endswith("urban"):
            return 50.0
    return float(DEFAULT_KPH.get(first(data.get("highway")), 40))


def short_name(value) -> str:
    name = first(value) or ""
    for long, short in ABBREV:
        name = name.replace(long, short)
    return name


# ---------------------------------------------------------------------------- download
def overpass(query: str, path: Path, *, refresh: bool, binary_ok: bytes) -> Path:
    """Run an Overpass query once and cache the answer; `binary_ok` is the first byte of a valid reply."""
    if path.exists() and not refresh:
        return path
    for attempt in range(8):
        endpoint = OVERPASS[attempt % len(OVERPASS)]
        print(f"overpass attempt {attempt + 1} ({endpoint}) ...", flush=True)
        try:
            reply = requests.post(endpoint, data={"data": query}, headers=UA, timeout=300)
        except requests.RequestException as err:
            print(f"  {err}")
            time.sleep(5)
            continue
        if reply.status_code == 200 and reply.content[:1] == binary_ok and b"<remark>" not in reply.content[:2000]:
            path.write_bytes(reply.content)
            return path
        print(f"  status {reply.status_code}")
        time.sleep(5)
    sys.exit("Overpass did not answer; try again later")


def download(refresh: bool) -> tuple[Path, Path]:
    RAW.mkdir(parents=True, exist_ok=True)
    north, south, east, west = BBOX
    refs = "|".join(CHOSEN_LINES)
    lines = f'rel["route"="bus"]["network"="CTP Iași"]["ref"~"^({refs})$"]'
    roads = (
        f"[out:xml][timeout:300];{lines}->.lines;"
        f'(way["highway"~"^(primary|secondary|tertiary)(_link)?$"]({south},{west},{north},{east});'
        f'way(r.lines)["highway"~"^({ROUTE_ROADS})(_link)?$"];);(._;>;);out;'
    )
    relations = f"[out:json][timeout:300];{lines};out body;>;out skel qt;"
    return (
        overpass(roads, RAW / "iasi_roads.osm", refresh=refresh, binary_ok=b"<"),
        overpass(relations, RAW / "iasi_lines.json", refresh=refresh, binary_ok=b"{"),
    )


# ---------------------------------------------------------------------------- helpers
class Snapper:
    """Nearest graph node by metric distance (equirectangular around the box centre)."""

    def __init__(self, graph: nx.MultiDiGraph):
        self.ids = np.array(list(graph.nodes))
        lat0 = math.radians(sum(BBOX[:2]) / 2)
        self.k = (111320 * math.cos(lat0), 110540)
        self.xy = np.array([[graph.nodes[n]["x"] * self.k[0], graph.nodes[n]["y"] * self.k[1]] for n in self.ids])

    def nearest(self, lat: float, lon: float, within: float = 120.0):
        d = np.hypot(self.xy[:, 0] - lon * self.k[0], self.xy[:, 1] - lat * self.k[1])
        i = int(d.argmin())
        return int(self.ids[i]) if d[i] <= within else None


def way_ids(osmid) -> set[int]:
    values = osmid if isinstance(osmid, list) else [osmid]
    return {int(v) for v in values}


def best_key(graph, a, b, weight):
    return min(graph[a][b], key=lambda k: weight(a, b, {k: graph[a][b][k]}))


def waypoints(relation, elements, ways, snapper: Snapper) -> tuple[list[int], list[tuple[float, float]]]:
    """Graph nodes to pass through, in order: the snapped stops, or points along the ways when stops are scarce."""
    stops, coords = [], []
    for m in relation["members"]:
        if m["type"] == "node" and m.get("role", "").startswith(("stop", "platform")) and m["ref"] in elements:
            node = elements[m["ref"]]
            snapped = snapper.nearest(node["lat"], node["lon"])
            if snapped is not None:
                stops.append(snapped)
                coords.append((node["lat"], node["lon"]))
    if len(stops) < 6:  # e.g. line 52: OSM lists ways but no stops, so follow the ways in member order
        member_ways = [ways[m["ref"]] for m in relation["members"] if m["type"] == "way" and m["ref"] in ways]
        picked = member_ways[:: max(1, len(member_ways) // 12)] + member_ways[-1:]
        stops, coords = [], []
        for w in picked:
            node = elements.get(w["nodes"][len(w["nodes"]) // 2])
            snapped = snapper.nearest(node["lat"], node["lon"]) if node else None
            if snapped is not None:
                stops.append(snapped)
                coords.append((node["lat"], node["lon"]))
    nodes = []
    for s in stops:
        if not nodes or nodes[-1] != s:
            nodes.append(s)
    return nodes, coords


def route_edges(graph, relation, preferred_ways, wp: list[int]) -> list:
    def weight(u, v, data):
        return min(
            d["length"] / (kph(d) / 3.6) * (PREFER if way_ids(d.get("osmid", [])) & preferred_ways else 1.0)
            for d in data.values()
        )

    edges = []
    for a, b in zip(wp, wp[1:], strict=False):
        try:
            path = nx.shortest_path(graph, a, b, weight=weight)
        except nx.NetworkXNoPath:
            continue
        edges += [(u, v, best_key(graph, u, v, weight)) for u, v in zip(path, path[1:], strict=False)]
    return edges


def join(graph, edges, target_start, weight):
    """Append a shortest path so that `edges` ends where `target_start` begins."""
    if not edges or edges[-1][1] == target_start:
        return []
    path = nx.shortest_path(graph, edges[-1][1], target_start, weight=weight)
    return [(u, v, best_key(graph, u, v, weight)) for u, v in zip(path, path[1:], strict=False)]


def drop_u_turns(edges):
    """Remove immediate a->b->a reversals that snapping can create."""
    out: list = []
    for e in edges:
        if out and out[-1][0] == e[1] and out[-1][1] == e[0]:
            out.pop()
        else:
            out.append(e)
    return out


# ---------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="download from OpenStreetMap again")
    args = ap.parse_args()
    ox.settings.log_console = False
    ox.settings.cache_folder = str(RAW / "osmnx_cache")

    roads_path, lines_path = download(args.refresh)
    print("building the drive graph ...", flush=True)
    full = ox.graph_from_xml(roads_path, bidirectional=False, simplify=True, retain_all=True)
    mainland = max(nx.strongly_connected_components(full), key=len)
    print(f"  {len(full.nodes) - len(mainland)} nodes outside the main connected network dropped", flush=True)
    full = full.subgraph(mainland).copy()  # one-way stubs and islands cannot carry a looping line
    raw = json.loads(lines_path.read_text())
    elements = {e["id"]: e for e in raw["elements"] if e["type"] == "node"}
    ways = {e["id"]: e for e in raw["elements"] if e["type"] == "way"}
    relations = [e for e in raw["elements"] if e["type"] == "relation"]
    print(f"graph: {len(full.nodes)} nodes, {len(full.edges)} edges; {len(relations)} relations", flush=True)

    snapper = Snapper(full)

    def weight(u, v, data):
        return min(d["length"] / (kph(d) / 3.6) for d in data.values())

    # --- bus lines -> cyclic edge sequences on the full graph
    routes = []
    for ref in CHOSEN_LINES:
        group = sorted((r for r in relations if r["tags"].get("ref") == ref), key=lambda r: r["id"])
        group = [r for r in group if any(m["type"] == "way" for m in r["members"])]
        if not group:
            print(f"line {ref}: not in OSM, skipped")
            continue
        fwd = max(group, key=lambda r: sum(m["type"] == "way" for m in r["members"]))
        back = next((r for r in group if r is not fwd and r["tags"].get("from") == fwd["tags"].get("to")), None)
        legs = []
        for rel in (fwd, back):
            if rel is None:
                continue
            pref = {m["ref"] for m in rel["members"] if m["type"] == "way"}
            wp, _ = waypoints(rel, elements, ways, snapper)
            legs.append(route_edges(full, rel, pref, wp))
        edges_f = legs[0]
        if not edges_f:
            print(f"line {ref}: no path between its stops, skipped")
            continue
        if len(legs) == 2 and legs[1]:
            edges_b = legs[1]
        elif edges_f[-1][1] == edges_f[0][0]:  # a loop line (18): the forward leg already comes back to its start
            edges_b = []
        else:  # no opposite relation (lines 36, 52, 6B...): come back along the cheapest path
            path = nx.shortest_path(full, edges_f[-1][1], edges_f[0][0], weight=weight)
            edges_b = [(u, v, best_key(full, u, v, weight)) for u, v in zip(path, path[1:], strict=False)]
        if edges_b:
            cycle = edges_f + join(full, edges_f, edges_b[0][0], weight) + edges_b
        else:
            cycle = list(edges_f)
        cycle += join(full, cycle, edges_f[0][0], weight)
        cycle = drop_u_turns(cycle)
        km = sum(full[u][v][k]["length"] for u, v, k in cycle) / 1000
        routes.append(
            {
                "ref": ref,
                "name": f"{ref}: {fwd['tags'].get('from', '?')} - {fwd['tags'].get('to', '?')}",
                "from": fwd["tags"].get("from"),
                "to": fwd["tags"].get("to"),
                "relations": [fwd["id"]] + ([back["id"]] if back else []),
                "backbone": ref in BACKBONE,
                "orig_edges": cycle,
                "stops": [
                    {"lat": c[0], "lon": c[1]}
                    for rel in (fwd, back)
                    if rel is not None
                    for c in waypoints(rel, elements, ways, snapper)[1]
                ],
            }
        )
        print(
            f"line {ref}{' (backbone)' if ref in BACKBONE else ''}: {len(cycle)} edges, {km:.1f} km cycle", flush=True
        )

    # --- largest strongly connected component of everything that is needed
    needed = {e for r in routes for e in r["orig_edges"]}
    keep = [
        (u, v, k)
        for u, v, k, d in full.edges(keys=True, data=True)
        if first(d.get("highway")) in ARTERIAL or (u, v, k) in needed
    ]
    core = full.edge_subgraph(keep).copy()
    core.remove_nodes_from(list(nx.isolates(core)))
    scc = max(nx.strongly_connected_components(core), key=len)
    core = core.subgraph(scc).copy()
    routes = [r for r in routes if all(core.has_edge(u, v, k) for u, v, k in r["orig_edges"])]
    print(f"core graph: {len(core.nodes)} nodes, {len(core.edges)} edges; {len(routes)} routes kept", flush=True)

    # --- merge junction clusters (dual carriageways, roundabouts) and go back to lat/lon
    projected = ox.project_graph(core)
    merged = ox.consolidate_intersections(projected, tolerance=25, rebuild_graph=True, dead_ends=True)
    for _, _, _, d in merged.edges(keys=True, data=True):
        d["length"] = float(d["geometry"].length)  # metres in UTM
    latlon = ox.project_graph(merged, to_latlong=True)

    # original (u, v) -> consolidated edge; route edges are mapped through it
    lookup: dict[tuple[int, int], list] = {}
    for u, v, k, d in latlon.edges(keys=True, data=True):
        lookup.setdefault((int(d["u_original"]), int(d["v_original"])), []).append((u, v, k, d["length"]))

    node_ids = {n: i for i, n in enumerate(latlon.nodes)}
    nodes_out = [
        {"id": node_ids[n], "lat": round(d["y"], 6), "lon": round(d["x"], 6)} for n, d in latlon.nodes(data=True)
    ]
    edge_ids: dict[tuple, int] = {}
    edges_out = []
    for u, v, k, d in latlon.edges(keys=True, data=True):
        coords = [[round(x, 6), round(y, 6)] for x, y in LineString(d["geometry"].coords).simplify(0.00001).coords]
        edge_ids[(u, v, k)] = len(edges_out)
        edges_out.append(
            {
                "id": len(edges_out),
                "u": node_ids[u],
                "v": node_ids[v],
                "length": round(float(merged[u][v][k]["length"]), 1),
                "highway": first(d.get("highway")),
                "name": short_name(d.get("name")),
                "maxspeed_kph": kph(d),
                "geometry": coords,
            }
        )

    final = nx.MultiDiGraph()
    for e in edges_out:
        final.add_edge(e["u"], e["v"], key=e["id"], length=e["length"])

    def shortest(a: int, b: int) -> list[int]:
        path = nx.shortest_path(final, a, b, weight="length")
        return [
            min(final[x][y], key=lambda kk: final[x][y][kk]["length"]) for x, y in zip(path, path[1:], strict=False)
        ]

    routes_out = []
    for r in routes:
        seq = []
        for u, v, _k in r["orig_edges"]:
            options = lookup.get((int(u), int(v)))
            if options:  # an edge swallowed by a merged cluster simply disappears from the route
                seq.append(edge_ids[min(options, key=lambda o: o[3])[:3]])
        fixed: list[int] = []
        for eid in seq:  # repair gaps with shortest paths on the final graph
            if fixed and edges_out[fixed[-1]]["v"] != edges_out[eid]["u"]:
                fixed += shortest(edges_out[fixed[-1]]["v"], edges_out[eid]["u"])
            if not fixed or fixed[-1] != eid:
                fixed.append(eid)
        if edges_out[fixed[-1]]["v"] != edges_out[fixed[0]]["u"]:
            fixed += shortest(edges_out[fixed[-1]]["v"], edges_out[fixed[0]]["u"])
        routes_out.append(
            {
                "ref": r["ref"],
                "name": r["name"],
                "from": r["from"],
                "to": r["to"],
                "osm_relations": r["relations"],
                "backbone": r["backbone"],
                "edges": fixed,
                "length_m": round(sum(edges_out[e]["length"] for e in fixed), 0),
                "stops": r["stops"],
            }
        )

    north, south, east, west = BBOX
    meta = {
        "source": "OpenStreetMap contributors (ODbL), via OSMnx and Overpass",
        "generated": datetime.date.today().isoformat(),
        "bbox_nswe": BBOX,
        "arterial_classes": sorted(ARTERIAL),
        "consolidation_tolerance_m": 25,
        "backbone_lines": BACKBONE,
        "landmarks": [{"name": n, "lat": la, "lon": lo} for n, (la, lo) in LANDMARKS.items()],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "iasi_core.json").write_text(
        json.dumps({"meta": meta, "nodes": nodes_out, "edges": edges_out}, ensure_ascii=False, separators=(",", ":"))
    )
    (OUT / "iasi_routes.json").write_text(
        json.dumps({"meta": meta, "routes": routes_out}, ensure_ascii=False, separators=(",", ":"))
    )
    total = sum(e["length"] for e in edges_out) / 1000
    print(f"wrote {len(nodes_out)} nodes, {len(edges_out)} edges ({total:.0f} km), {len(routes_out)} routes")


if __name__ == "__main__":
    main()
