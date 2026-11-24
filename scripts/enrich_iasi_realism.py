"""Generate deterministic Iași traffic-realism metadata (`data/iasi_realism.json`).

Enriches the committed Iași road network (`iasi_core.json` + `iasi_routes.json`) with:
- Real lane counts per directed segment (1, 2, or 3 mixed-traffic bands per direction — no dedicated bus lanes)
- Real Iași roundabouts (unsignalized OUG 195 / HG 130/2023 yield-to-left rings + signalized Podu Roș / Podu de Piatră)
- Unsignalized priority/yield T-junctions (*Cedează Trecerea*) for minor residential/tertiary side-streets
- Deduplicated real CTP Iași bus stations snapped from the 841 GPS stop coordinates in `iasi_routes.json`
- Iași elevation & road grade percentages (Copou, Bucium, Galata, Tătărași, Cetățuia hills)
- High-pedestrian Iași zebra crosswalks (*Treceri de pietoni*) & CFR railway crossings & hospital anchors
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcity.config import Config
from smartcity.data import DATA_DIR, load_iasi
from smartcity.network import RoadNetwork

# Real Iași 3-lane-per-direction boulevards (name substrings, case-insensitive)
THREE_LANE_NAMES = (
    "independenței",
    "păcurari",
    "anastasie panu",
    "palat",
    "tudor vladimirescu",
    "nicolae iorga",
    "nicolina",
    "primăverii",
    "podu roș",
)

# Real Iași 2-lane-per-direction boulevards & major arterials
TWO_LANE_NAMES = (
    "carol i",
    "socola",
    "poitiers",
    "bucium",
    "alexandru cel bun",
    "dacia",
    "dimitrie cantemir",
    "națională",
    "chișinăului",
    "vasile lupu",
    "elena doamna",
    "bahlui",
    "țuțora",
    "regele mihai",
    "regele ferdinand",
    "mangeron",
    "rosetti",
    "centura",
    "ștefan cel mare",
    "sfântul lazăr",
    "pasajul alexandru",
    "canta",
    "moara de foc",
    "cuza vodă",
    "ciurchi",
    "tătărași",
    "tomești",
    "cug",
    "libertății",
    "copou",
)

# Major real Iași roundabouts: (name, approx_lat, approx_lon, radius_m, ring_lanes, signalized)
ROUNDABOUT_SPECS = (
    ("Rond Podu Roș", 47.1511, 27.5877, 23.0, 2, True),
    ("Rond Podu de Piatră", 47.1588, 27.5753, 21.0, 2, True),
    ("Rond Păcurari (Canta)", 47.1733, 27.5461, 18.0, 2, False),
    ("Rond Era / Păcurari Vest", 47.1806, 27.5231, 19.0, 2, False),
    ("Rond Agronomie (Copou)", 47.1905, 27.5590, 16.0, 1, False),
    ("Rond CUG / Tehnopolis", 47.1233, 27.5669, 19.0, 2, False),
    ("Rond Vechi CUG", 47.1319, 27.5819, 17.0, 2, False),
    ("Rond Dacia", 47.1666, 27.5438, 16.0, 2, False),
    ("Rond Tătărași / Ciric", 47.1636, 27.6248, 16.0, 1, False),
    ("Rond Centura / Lunca Cetățuii", 47.1000, 27.5369, 21.0, 2, False),
    ("Rond Cotnari", 47.1474, 27.5920, 18.0, 2, False),
    ("Rond Țesătura", 47.1493, 27.5946, 18.0, 2, False),
)

# Elevation anchors across Iași's 7 hills & Bahlui valley (lat, lon, elevation_m)
ELEVATION_ANCHORS = (
    (47.151, 27.587, 42.0),   # Bahlui Valley / Podu Roș
    (47.158, 27.575, 43.0),   # Bahlui / Podu de Piatră
    (47.155, 27.604, 44.0),   # Bahlui / Tudor Vladimirescu
    (47.165, 27.570, 50.0),   # Gara Iași
    (47.167, 27.580, 68.0),   # Piața Unirii
    (47.174, 27.572, 118.0),  # UAIC / Copou mid-hill
    (47.182, 27.565, 148.0),  # Parcul Copou
    (47.193, 27.555, 172.0),  # Agronomie / Breazu ridge
    (47.175, 27.545, 78.0),   # Păcurari
    (47.166, 27.548, 52.0),   # Dacia / Alexandru cel Bun
    (47.147, 27.558, 132.0),  # Dealul Galata
    (47.136, 27.582, 76.0),   # Nicolina / CUG
    (47.120, 27.582, 115.0),  # Dealul Cetățuia / Tehnopolis
    (47.143, 27.603, 88.0),   # Socola / Bucium foot
    (47.131, 27.619, 178.0),  # Dealul Bucium
    (47.118, 27.632, 245.0),  # Bucium / Repedea upper hill
    (47.167, 27.611, 116.0),  # Dealul Tătărași
    (47.176, 27.614, 135.0),  # Moara de Vânt / Ciric
)

# High-pedestrian Iași hubs for zebra crosswalks & high bus-boarding dwell times
PEDESTRIAN_HUBS = (
    ("UAIC Copou", 47.1743, 27.5725, 1.4),
    ("Piața Unirii", 47.1667, 27.5801, 1.5),
    ("Bd. Independenței / UMF", 47.1687, 27.5789, 1.4),
    ("Palas / Palat", 47.1553, 27.5861, 1.5),
    ("Podu Roș", 47.1506, 27.5865, 1.4),
    ("Tudor Vladimirescu Campus", 47.1541, 27.6040, 1.5),
    ("Gara Iași", 47.1655, 27.5699, 1.3),
    ("Hala Centrală", 47.1612, 27.5872, 1.3),
    ("Piața Alexandru cel Bun", 47.1645, 27.5535, 1.2),
    ("Păcurari / Petru Poni", 47.1740, 27.5579, 1.2),
    ("Tătărași / Piața Chirilă", 47.1659, 27.6115, 1.2),
    ("Nicolina / Belvedere", 47.1432, 27.5828, 1.2),
    ("Târgu Cucu", 47.1669, 27.5944, 1.3),
)

# Iași major hospitals for realistic emergency vehicle dispatch
HOSPITALS = (
    ("Spitalul Sf. Spiridon", 47.1684, 27.5815),
    ("Spitalul C.I. Parhon", 47.1782, 27.5688),
    ("Spitalul Sf. Maria", 47.1647, 27.6095),
    ("Spitalul Neurochirurgie", 47.1636, 27.5988),
)


def estimate_elevation(lat: float, lon: float) -> float:
    """Smooth inverse-distance-weighted elevation estimate (metres ASL) across Iași."""
    num = 0.0
    den = 0.0
    for alat, alon, elev in ELEVATION_ANCHORS:
        d = math.hypot((lat - alat) * 110540.0, (lon - alon) * 75500.0)
        if d < 15.0:
            return elev
        w = 1.0 / (d * d)
        num += w * elev
        den += w
    return num / max(1e-9, den)


def infer_lanes(name: str, highway: str, length: float, is_oneway: bool) -> int:
    """Realistic Iași lane count (1, 2, or 3 mixed-traffic bands per direction)."""
    low = (name or "").lower()
    hwy = (highway or "").lower()
    if any(k in low for k in THREE_LANE_NAMES) and ("primary" in hwy or "secondary" in hwy) and length >= 75.0:
        return 3
    if any(k in low for k in TWO_LANE_NAMES) and hwy not in ("residential", "service", "living_street"):
        return 2
    if "primary" in hwy and not hwy.endswith("_link"):
        return 2
    if "secondary" in hwy and is_oneway and length >= 80.0:
        return 2
    return 1


def main() -> None:
    cfg = Config()
    graph, routes = load_iasi(DATA_DIR)
    net = RoadNetwork(graph, cfg, routes)

    # 1. Lane counts & elevation grades per directed segment
    edge_pairs = {(s.from_node, s.to_node) for s in net.segments}
    edge_lanes: dict[str, int] = {}
    edge_grades: dict[str, float] = {}
    for seg in net.segments:
        oneway = (seg.to_node, seg.from_node) not in edge_pairs
        lanes = infer_lanes(seg.name, seg.highway, seg.length, oneway)
        edge_lanes[str(seg.id)] = lanes

        u_n = net.nodes[seg.from_node]
        v_n = net.nodes[seg.to_node]
        dz = estimate_elevation(v_n.lat, v_n.lon) - estimate_elevation(u_n.lat, u_n.lon)
        grade_pct = max(-8.0, min(8.0, (dz / max(40.0, seg.length)) * 100.0))
        if abs(grade_pct) >= 1.2:
            edge_grades[str(seg.id)] = round(grade_pct, 2)

    # 2. Roundabouts mapped cleanly onto existing hub nodes (avoiding duplicate nodes & gates)
    used_rb_nodes: set[int] = set()
    roundabouts: list[dict] = []
    gate_set = set(net.gates)
    for name, lat, lon, radius_m, ring_lanes, signalized in ROUNDABOUT_SPECS:
        best = min(
            (n for n in net.nodes if n.id not in gate_set and n.id not in used_rb_nodes and len(net.in_segs[n.id]) >= 2 and len(net.out_segs[n.id]) >= 2),
            key=lambda n: math.hypot((n.lat - lat) * 110540.0, (n.lon - lon) * 75500.0),
        )
        # Keep roundabout radius physically smaller than incident edges so yield lines fit cleanly
        inc_lens = [net.segments[s].length for s in net.in_segs[best.id] + net.out_segs[best.id]]
        safe_r = round(min(radius_m, max(12.0, min(inc_lens) * 0.28)), 1)
        used_rb_nodes.add(best.id)
        roundabouts.append(
            {
                "node": best.id,
                "name": name,
                "radius_m": safe_r,
                "ring_lanes": ring_lanes,
                "signalized": signalized,
            }
        )

    # 3. Unsignalized Priority/Yield T-junctions (*Cedează Trecerea*) for minor tertiary/residential nodes
    # Protect all 8 key landmark nearest junctions and keep >= 115 real signalised intersections.
    landmark_nodes: set[int] = set()
    for _, llat, llon in net.landmarks_ll:
        lx, ly = net.proj.to_xy(llat, llon)
        nearest = min((n for n in net.nodes if n.signalised), key=lambda n: math.hypot(n.x - lx, n.y - ly))
        landmark_nodes.add(nearest.id)

    yield_nodes: list[int] = []
    sig_remaining = sum(1 for n in net.nodes if n.signalised and n.id not in {r["node"] for r in roundabouts if not r["signalized"]})
    for n in net.nodes:
        if not n.signalised or n.id in used_rb_nodes or n.id in landmark_nodes:
            continue
        if len(net.in_segs[n.id]) != 3:
            continue
        hwys = {(net.segments[s].highway or "").replace("_link", "") for s in net.in_segs[n.id]}
        # Minor side-street T-junctions (tertiary / residential / unclassified only, no primary boulevard crossing)
        if "primary" not in hwys and hwys <= {"tertiary", "residential", "unclassified", "service"}:
            if sig_remaining > 118:
                yield_nodes.append(n.id)
                sig_remaining -= 1

    # 4. Deduplicated real CTP Iași bus stops snapped from the 841 GPS stop coordinates in iasi_routes.json
    hub_xy = [(name, *net.proj.to_xy(lat, lon), weight) for name, lat, lon, weight in PEDESTRIAN_HUBS]
    stops_by_seg: dict[int, list[tuple[float, float, str]]] = {}
    for r in routes:
        seg_set = list(dict.fromkeys(r.edges))
        for slat, slon in r.stops:
            sx, sy = net.proj.to_xy(slat, slon)
            best_sid, best_pos, best_d = -1, 0.0, 95.0
            for sid in seg_set:
                seg = net.segments[sid]
                if seg.length < 75.0:
                    continue
                # Sample 6 points along the usable middle of the segment
                lo, hi = 24.0, seg.stop_pos - 22.0
                if hi <= lo:
                    continue
                for k in range(6):
                    p = lo + (hi - lo) * (k / 5.0)
                    px, py, _, _ = net.position(sid, p)
                    d = math.hypot(px - sx, py - sy)
                    if d < best_d:
                        best_d, best_sid, best_pos = d, sid, p
            if best_sid >= 0:
                # Deduplicate stops within 140 m on the same directed segment
                existing = stops_by_seg.setdefault(best_sid, [])
                if all(abs(ep - best_pos) >= 140.0 for ep, _, _ in existing):
                    # Hub demand weight (higher boarding dwell near Unirii, Podu Roș, Tudor, Copou, Palas)
                    demand = 1.0
                    hname = net.segments[best_sid].name or f"Stație {best_sid}"
                    px, py, _, _ = net.position(best_sid, best_pos)
                    for hn, hx, hy, hw in hub_xy:
                        if math.hypot(px - hx, py - hy) <= 380.0:
                            demand = max(demand, hw)
                            hname = hn
                    existing.append((best_pos, demand, hname))

    bus_stops: list[dict] = []
    for sid, items in sorted(stops_by_seg.items()):
        lanes = edge_lanes[str(sid)]
        for pos_m, demand, sname in sorted(items):
            px, py, _, _ = net.position(sid, pos_m)
            slat, slon = net.proj.to_latlon(px, py)
            bus_stops.append(
                {
                    "id": len(bus_stops),
                    "seg_id": sid,
                    "pos_m": round(pos_m, 1),
                    "lat": round(slat, 6),
                    "lon": round(slon, 6),
                    "name": sname,
                    "has_bay": lanes >= 2 or demand >= 1.3,
                    "demand": round(demand, 2),
                }
            )

    # 5. Marked high-pedestrian zebra crosswalks (*Treceri de pietoni*) near Iași's busiest pedestrian hubs
    crosswalks: list[dict] = []
    used_cw_segs: set[int] = set()
    for hname, hx, hy, hw in hub_xy:
        # Pick up to 2–3 nearby segments per hub
        cands = []
        for seg in net.segments:
            if seg.id in used_cw_segs or seg.length < 70.0:
                continue
            p = max(22.0, seg.stop_pos - 10.0)
            px, py, _, _ = net.point_at(seg.id, p)
            d = math.hypot(px - hx, py - hy)
            if d <= 260.0:
                cands.append((d, seg.id, p))
        cands.sort()
        for _, sid, pos_m in cands[:3]:
            used_cw_segs.add(sid)
            px, py, _, _ = net.point_at(sid, pos_m)
            clat, clon = net.proj.to_latlon(px, py)
            crosswalks.append(
                {
                    "id": len(crosswalks),
                    "seg_id": sid,
                    "pos_m": round(pos_m, 1),
                    "lat": round(clat, 6),
                    "lon": round(clon, 6),
                    "hub": hname,
                    "rate": round(0.018 * hw, 4),  # pedestrian pulse frequency (events/s)
                }
            )

    # 6. CFR railway level crossings (Nicolina / Socola rail corridor)
    rail_spots = (
        ("Pasaj CFR Socola", 47.1435, 27.6005),
        ("Pasaj CFR Nicolina", 47.1488, 27.5785),
    )
    rail_crossings: list[dict] = []
    for rname, rlat, rlon in rail_spots:
        rx, ry = net.proj.to_xy(rlat, rlon)
        best_seg = min(
            (s for s in net.segments if s.length >= 90.0),
            key=lambda s: math.hypot(net.point_at(s.id, s.length * 0.5)[0] - rx, net.point_at(s.id, s.length * 0.5)[1] - ry),
        )
        pos_m = round(best_seg.length * 0.5, 1)
        px, py, _, _ = net.point_at(best_seg.id, pos_m)
        lat_c, lon_c = net.proj.to_latlon(px, py)
        rail_crossings.append(
            {
                "name": rname,
                "seg_id": best_seg.id,
                "pos_m": pos_m,
                "lat": round(lat_c, 6),
                "lon": round(lon_c, 6),
                "period_s": 420.0,   # train every ~7 min
                "closed_s": 38.0,    # barrier down for 38 s
                "offset_s": 140.0 + len(rail_crossings) * 210.0,
            }
        )

    # 7. Hospital dispatch nodes for emergency vehicles
    hospitals: list[dict] = []
    for hname, hlat, hlon in HOSPITALS:
        hx, hy = net.proj.to_xy(hlat, hlon)
        node = min(net.nodes, key=lambda n: math.hypot(n.x - hx, n.y - hy))
        hospitals.append({"name": hname, "node": node.id, "lat": hlat, "lon": hlon})

    lane_dist = {1: 0, 2: 0, 3: 0}
    for v in edge_lanes.values():
        lane_dist[v] = lane_dist.get(v, 0) + 1

    payload = {
        "meta": {
            "city": "Iași",
            "mixed_traffic_only": True,
            "lane_counts": lane_dist,
            "roundabouts_total": len(roundabouts),
            "roundabouts_unsignalized": sum(1 for r in roundabouts if not r["signalized"]),
            "yield_junctions": len(yield_nodes),
            "bus_stops": len(bus_stops),
            "crosswalks": len(crosswalks),
            "hilly_edges": len(edge_grades),
        },
        "edge_lanes": edge_lanes,
        "edge_grades": edge_grades,
        "roundabouts": roundabouts,
        "yield_nodes": sorted(yield_nodes),
        "bus_stops": bus_stops,
        "crosswalks": crosswalks,
        "rail_crossings": rail_crossings,
        "hospitals": hospitals,
    }
    out = DATA_DIR / "iasi_realism.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {out} | lanes={lane_dist} | roundabouts={len(roundabouts)} "
        f"(unsig={sum(not r['signalized'] for r in roundabouts)}) | yield_T={len(yield_nodes)} | "
        f"stops={len(bus_stops)} | crosswalks={len(crosswalks)} | hills={len(edge_grades)}"
    )


if __name__ == "__main__":
    main()
