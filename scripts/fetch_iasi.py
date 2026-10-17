"""Build the Iași dataset used by the simulation (run once; the results are committed).

    python scripts/fetch_iasi.py            # reuse data/raw/ if present
    python scripts/fetch_iasi.py --refresh  # download again from OpenStreetMap

Steps:
 1. Download the drive network for the central Iași transit spine with OSMnx.
 2. Download the CTP Iași bus route relations (stops and ways) from Overpass.
 3. Turn each chosen bus line into a cyclic sequence of graph edges: the stops in relation
    order are snapped to the graph and joined by shortest paths that prefer the relation's own ways.
 4. Keep the arterial roads (primary/secondary/tertiary and links) plus whatever the routes
    need, take the largest strongly connected component, merge junction clusters closer than
    25 m (OSMnx), and write compact JSON for the runtime.

Map data (c) OpenStreetMap contributors, ODbL.
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

# North, south, east, west: covers Copou, Păcurari, Piața Unirii, Podu Roș and Tudor Vladimirescu.
BBOX = (47.186, 47.147, 27.607, 27.553)
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
ARTERIAL = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link"}
CHOSEN_LINES = ["19", "28", "36", "41", "20", "42", "43", "47"]
DEFAULT_KPH = {
    "primary": 50,
    "secondary": 50,
    "tertiary": 40,
    "primary_link": 30,
    "secondary_link": 30,
    "tertiary_link": 30,
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
def load_full_graph(refresh: bool) -> nx.MultiDiGraph:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / "iasi_full.graphml"
    if refresh or not path.exists():
        north, south, east, west = BBOX
        print("downloading drive network ...")
        graph = ox.graph_from_bbox((west, south, east, north), network_type="drive", simplify=True)
        ox.save_graphml(graph, path)
    return ox.load_graphml(path)


def load_bus_relations(refresh: bool) -> dict:
    path = RAW / "iasi_bus_raw.json"
    if refresh or not path.exists():
        north, south, east, west = BBOX
        query = (
            f'[out:json][timeout:90];(relation["route"="bus"]({south},{west},{north},{east}););out body;>;out skel qt;'
        )
        for attempt in range(6):
            endpoint = OVERPASS[attempt % len(OVERPASS)]
            print(f"overpass attempt {attempt + 1} ({endpoint}) ...")
            reply = requests.post(endpoint, data={"data": query}, headers=UA, timeout=150)
            if reply.status_code == 200 and reply.content[:1] == b"{":
                path.write_text(reply.text)
                break
            time.sleep(4)
        else:
            sys.exit("Overpass did not answer; try again later")
    return json.loads(path.read_text())


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


def route_edges(graph, relation, elements, preferred_ways, snapper) -> tuple[list, list]:
    """Stops in order -> snapped nodes -> shortest paths preferring the relation's own ways."""
    stops = []
    for m in relation["members"]:
        if m["type"] == "node" and m.get("role", "").startswith(("stop", "platform")):
            node = elements[m["ref"]]
            snapped = snapper.nearest(node["lat"], node["lon"])
            if snapped is not None:
                stops.append((snapped, node["lat"], node["lon"]))
    nodes = []
    for snapped, _, _ in stops:
        if not nodes or nodes[-1] != snapped:
            nodes.append(snapped)

    def weight(u, v, data):
        w = min(
            d["length"] / (kph(d) / 3.6) * (0.2 if way_ids(d.get("osmid", [])) & preferred_ways else 1.0)
            for d in data.values()
        )
        return w

    edges = []
    for a, b in zip(nodes, nodes[1:], strict=False):
        try:
            path = nx.shortest_path(graph, a, b, weight=weight)
        except nx.NetworkXNoPath:
            continue
        edges += [(u, v, best_key(graph, u, v, weight)) for u, v in zip(path, path[1:], strict=False)]
    return edges, stops


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

    full = load_full_graph(args.refresh)
    raw = load_bus_relations(args.refresh)
    elements = {e["id"]: e for e in raw["elements"] if e["type"] == "node"}
    relations = [
        e for e in raw["elements"] if e["type"] == "relation" and e.get("tags", {}).get("network") == "CTP Iași"
    ]
    print(f"full network: {len(full.nodes)} nodes, {len(full.edges)} edges; {len(relations)} CTP relations in the box")

    snapper = Snapper(full)

    def weight(u, v, data):
        return min(d["length"] / (kph(d) / 3.6) for d in data.values())

    # --- bus lines -> cyclic edge sequences on the full graph
    routes = []
    for ref in CHOSEN_LINES:
        group = sorted((r for r in relations if r["tags"].get("ref") == ref), key=lambda r: r["id"])
        if not group:
            print(f"line {ref}: not in OSM inside the box, skipped")
            continue
        fwd = group[0]
        back = next((r for r in group[1:] if r["tags"].get("from") == fwd["tags"].get("to")), None)
        ways_f = {m["ref"] for m in fwd["members"] if m["type"] == "way"}
        edges_f, stops_f = route_edges(full, fwd, elements, ways_f, snapper)
        if back is not None:
            ways_b = {m["ref"] for m in back["members"] if m["type"] == "way"}
            edges_b, stops_b = route_edges(full, back, elements, ways_b, snapper)
        else:  # no opposite relation (e.g. line 36): come back along the cheapest path
            edges_b, stops_b = [], []
            if edges_f:
                path = nx.shortest_path(full, edges_f[-1][1], edges_f[0][0], weight=weight)
                edges_b = [(u, v, best_key(full, u, v, weight)) for u, v in zip(path, path[1:], strict=False)]
        if not edges_f or not edges_b:
            print(f"line {ref}: could not build a path, skipped")
            continue
        cycle = edges_f + join(full, edges_f, edges_b[0][0], weight) + edges_b
        cycle += join(full, cycle, edges_f[0][0], weight)
        cycle = drop_u_turns(cycle)
        routes.append(
            {
                "ref": ref,
                "name": f"{ref}: {fwd['tags'].get('from', '?')} - {fwd['tags'].get('to', '?')}",
                "from": fwd["tags"].get("from"),
                "to": fwd["tags"].get("to"),
                "relations": [fwd["id"]] + ([back["id"]] if back else []),
                "orig_edges": cycle,
                "stops": [{"lat": la, "lon": lo} for _, la, lo in stops_f + stops_b],
            }
        )
        print(
            f"line {ref}: {len(cycle)} edges, {sum(full[u][v][k]['length'] for u, v, k in cycle) / 1000:.1f} km cycle"
        )

    # --- keep arterials plus the route edges, largest strongly connected component
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
    print(f"core graph: {len(core.nodes)} nodes, {len(core.edges)} edges; {len(routes)} routes kept")

    # --- merge junction clusters (dual carriageways, roundabouts) and go back to lat/lon
    projected = ox.project_graph(core)
    merged = ox.consolidate_intersections(projected, tolerance=25, rebuild_graph=True, dead_ends=True)
    for _, _, _, d in merged.edges(keys=True, data=True):
        d["length"] = float(d["geometry"].length)  # metres in UTM
    simple_geom = {}
    for u, v, k, d in merged.edges(keys=True, data=True):
        simple_geom[(u, v, k)] = d["geometry"].simplify(1.5)
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

    routes_out = []
    for r in routes:
        seq = []
        for u, v, _k in r["orig_edges"]:
            options = lookup.get((int(u), int(v)))
            if options:  # an edge swallowed by a merged cluster simply disappears from the route
                seq.append(edge_ids[min(options, key=lambda o: o[3])[:3]])
        # keep only a contiguous cycle: repair gaps with shortest paths on the final graph
        final = nx.MultiDiGraph()
        for e in edges_out:
            final.add_edge(e["u"], e["v"], key=e["id"], length=e["length"])
        fixed = []
        for eid in seq:
            if fixed and edges_out[fixed[-1]]["v"] != edges_out[eid]["u"]:
                path = nx.shortest_path(final, edges_out[fixed[-1]]["v"], edges_out[eid]["u"], weight="length")
                fixed += [
                    min(final[a][b], key=lambda kk: final[a][b][kk]["length"])
                    for a, b in zip(path, path[1:], strict=False)
                ]
            if not fixed or fixed[-1] != eid:
                fixed.append(eid)
        if edges_out[fixed[-1]]["v"] != edges_out[fixed[0]]["u"]:
            path = nx.shortest_path(final, edges_out[fixed[-1]]["v"], edges_out[fixed[0]]["u"], weight="length")
            fixed += [
                min(final[a][b], key=lambda kk: final[a][b][kk]["length"]) for a, b in zip(path, path[1:], strict=False)
            ]
        routes_out.append(
            {
                "ref": r["ref"],
                "name": r["name"],
                "from": r["from"],
                "to": r["to"],
                "osm_relations": r["relations"],
                "edges": fixed,
                "length_m": round(sum(edges_out[e]["length"] for e in fixed), 0),
                "stops": r["stops"],
            }
        )

    meta = {
        "source": "OpenStreetMap contributors (ODbL), via OSMnx and Overpass",
        "generated": datetime.date.today().isoformat(),
        "bbox_nswe": BBOX,
        "arterial_classes": sorted(ARTERIAL),
        "consolidation_tolerance_m": 25,
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
