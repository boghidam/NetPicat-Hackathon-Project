"""Generate deterministic Iași traffic-realism metadata (`data/iasi_realism.json`).

Pure standard-library generator reading the committed Iași road network (`iasi_core.json` +
`iasi_routes.json`) using the exact projection and topology rules of `smartcity.network` so it
produces:
- Real lane counts per directed segment (1, 2, or 3 mixed-traffic bands per direction)
- All 16 real-world Iași car roundabouts (*Sensuri Giratorii* — 3 signalized + 13 yield-to-left rings)
- Unsignalized priority/yield junctions (*Cedează Trecerea* — minor 3-arm T-junctions & 2-in merges)
- Curated junction names for major Iași intersections & former roundabout hubs
- All 14 real-world CTP Iași *rond* transit turnaround loops (`rond_loops`)
- Deduplicated real CTP Iași bus stations snapped from GPS coordinates in `iasi_routes.json`
- Iași elevation & road grade percentages, zebra crosswalks, CFR rail crossings, and hospitals
"""

from __future__ import annotations

import bisect
import json
import math
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0
OPPOSITE_TOLERANCE = math.radians(40.0)

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

# All 16 verified real-world Iași car roundabouts (*Sensuri Giratorii*) on the committed road graph:
# (node_id, name, radius_m, ring_lanes, signalized)
ROUNDABOUT_SPECS: tuple[tuple[int, str, float, int, bool], ...] = (
    (28, "Rond Podu Roș", 23.0, 2, True),
    (95, "Rond Podu de Piatră", 21.0, 2, True),
    (83, "Rond Fundație (Piața Mihai Eminescu)", 22.0, 2, True),
    (80, "Rond Zimbru (Alexandru – Dacia)", 18.0, 2, False),
    (76, "Rond Pasaj Octav Băncilă / ANL Zimbru", 20.0, 2, False),
    (70, "Rond Păcurari", 19.0, 2, False),
    (193, "Rond Agronomie (Copou)", 17.0, 2, False),
    (109, "Rond Râpa Galbenă / Gării", 13.5, 2, False),
    (251, "Rond Bd. Socola (Granit)", 14.5, 2, False),
    (1, "Rond Ciric / Aeroport", 13.0, 1, False),
    (38, "Rond Centura Iași (Uricani / Miroslava Vest)", 16.0, 2, False),
    (232, "Rond Centura Iași (Miroslava / Vorovești)", 17.0, 2, False),
    (40, "Rond Centura Iași / Str. Basarabia", 16.0, 2, False),
    (189, "Rond Aroneanu", 16.0, 1, False),
    (205, "Rond Horpaz / Valea Adâncă", 12.5, 1, False),
    (204, "Rond CUG / Kaufland – Expo", 18.0, 2, False),
)

# Curated names for key non-roundabout Iași junctions (used for crisp tooltips & inspection)
JUNCTION_NAMES: dict[str, str] = {
    "36": "Intersecția Cotnari",
    "33": "Intersecția Țesătura",
    "75": "Intersecția Canta",
    "235": "Intersecția CUG / Tehnopolis",
    "196": "Intersecția Centura / Lunca Cetățuii",
    "143": "Intersecția Rond Vechi CUG",
    "145": "Intersecția Str. Libertății (CUG)",
    "9": "Intersecția Ciric / Veneția",
    "210": "Capăt Dacia (Bd. Dacia × Str. Tabacului)",
    "155": "Piața Gării",
    "13": "Târgu Cucu",
    "22": "Piața Unirii / Bd. Independenței",
    "31": "Palas / Str. Palat",
    "60": "Intersecția Baza 3",
    "54": "Intersecția Tudor Vladimirescu",
    "166": "Intersecția Triumf (Copou)",
    "254": "Intersecția Bucium",
    "73": "Păcurari / Petru Poni",
    "260": "Tătărași Nord (Flora)",
    "262": "Tătărași Sud",
    "57": "Intersecția Dancu",
}

# All 14 real-world CTP Iași *rond* transit turnaround loops:
# (name, anchor_node_id, lat, lon, radius_m, kind)
ROND_LOOP_SPECS: tuple[tuple[str, int, float, float, float, str], ...] = (
    ("Rond Dacia", 210, 47.16658, 27.54330, 15.0, "tram_bus"),
    ("Rond Canta", 74, 47.17241, 27.53765, 15.5, "tram_bus"),
    ("Rond CUG / Tehnopolis", 235, 47.12312, 27.56755, 18.0, "tram_bus"),
    ("Rond Vechi CUG", 143, 47.13420, 27.57465, 14.0, "bus"),
    ("Rond Piața Gării", 155, 47.16582, 27.57025, 16.0, "tram_bus"),
    ("Rond Târgu Cucu", 13, 47.16508, 27.59082, 15.0, "tram_bus"),
    ("Rond Tătărași Nord", 260, 47.15928, 27.61889, 14.0, "tram_bus"),
    ("Rond Tătărași Sud", 262, 47.15617, 27.62396, 15.0, "tram_bus"),
    ("Rond Baza 3", 60, 47.14672, 27.60955, 15.0, "tram_bus"),
    ("Rond Copou / Agronomie", 193, 47.19078, 27.55945, 14.0, "tram_bus"),
    ("Rond Triumf (Copou)", 166, 47.18115, 27.56710, 13.5, "bus"),
    ("Rond Tudor Vladimirescu", 54, 47.14835, 27.60260, 14.0, "tram_bus"),
    ("Rond Dancu", 57, 47.14348, 27.64765, 14.5, "tram_bus"),
    ("Rond Bucium", 254, 47.12385, 27.62135, 14.0, "bus"),
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


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _pt_seg_dist2(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    qx, qy = ax + t * dx, ay + t * dy
    return (px - qx) ** 2 + (py - qy) ** 2


def _polylines_coincide(
    pts_a: tuple[tuple[float, float], ...],
    pts_b: tuple[tuple[float, float], ...],
    tol_m: float = 2.5,
) -> bool:
    if len(pts_a) < 2 or len(pts_b) < 2:
        return False
    tol2 = tol_m * tol_m
    for px, py in pts_a:
        if min(_pt_seg_dist2(px, py, a[0], a[1], b[0], b[1]) for a, b in zip(pts_b, pts_b[1:], strict=False)) > tol2:
            return False
    for px, py in pts_b:
        if min(_pt_seg_dist2(px, py, a[0], a[1], b[0], b[1]) for a, b in zip(pts_a, pts_a[1:], strict=False)) > tol2:
            return False
    return True


def estimate_elevation(lat: float, lon: float) -> float:
    """Smooth inverse-distance-weighted elevation estimate (metres ASL) across Iași."""
    num = 0.0
    den = 0.0
    for alat, alon, elev in ELEVATION_ANCHORS:
        d = math.hypot((lat - alat) * M_PER_DEG_LAT, (lon - alon) * 75500.0)
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


def _interp(pts: tuple[tuple[float, float], ...], cum: tuple[float, ...], s: float) -> tuple[float, float]:
    s = max(0.0, min(cum[-1], s))
    i = min(bisect.bisect_right(cum, s) - 1, len(pts) - 2)
    span = cum[i + 1] - cum[i]
    f = (s - cum[i]) / span if span > 0 else 0.0
    return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f


def _physical_arm_count(in_e: list[dict], out_e: list[dict], pts_by_id: dict[int, tuple[tuple[float, float], ...]]) -> int:
    dirs: list[float] = []
    for e in in_e:
        pts = pts_by_id[e["id"]]
        dirs.append(math.atan2(pts[-2][1] - pts[-1][1], pts[-2][0] - pts[-1][0]))
    for e in out_e:
        pts = pts_by_id[e["id"]]
        dirs.append(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))
    clusters: list[list[float]] = []
    for a in sorted(dirs):
        for c in clusters:
            if abs(_wrap(a - c[0])) < math.radians(25.0):
                c.append(a)
                break
        else:
            clusters.append([a])
    if len(clusters) >= 2 and abs(_wrap(clusters[-1][0] - clusters[0][0])) < math.radians(25.0):
        clusters[0].extend(clusters.pop())
    return len(clusters)


def main() -> None:
    core = json.loads((DATA_DIR / "iasi_core.json").read_text(encoding="utf-8"))
    routes_doc = json.loads((DATA_DIR / "iasi_routes.json").read_text(encoding="utf-8"))

    raw_nodes = core["nodes"]
    raw_edges = core["edges"]
    landmarks_ll = [(m["name"], m["lat"], m["lon"]) for m in core["meta"].get("landmarks", [])]
    routes = routes_doc["routes"]

    lat0 = sum(n["lat"] for n in raw_nodes) / len(raw_nodes)
    lon0 = sum(n["lon"] for n in raw_nodes) / len(raw_nodes)
    kx = M_PER_DEG_LON * math.cos(math.radians(lat0))

    def to_xy(lat: float, lon: float) -> tuple[float, float]:
        return (lon - lon0) * kx, (lat - lat0) * M_PER_DEG_LAT

    def to_latlon(x: float, y: float) -> tuple[float, float]:
        return lat0 + y / M_PER_DEG_LAT, lon0 + x / kx

    nodes_by_id = {n["id"]: n for n in raw_nodes}
    xy = {n["id"]: to_xy(n["lat"], n["lon"]) for n in raw_nodes}

    in_segs: dict[int, list[dict]] = {n["id"]: [] for n in raw_nodes}
    out_segs: dict[int, list[dict]] = {n["id"]: [] for n in raw_nodes}
    pts_by_id: dict[int, tuple[tuple[float, float], ...]] = {}
    cum_by_id: dict[int, tuple[float, ...]] = {}
    tan_by_id: dict[int, tuple[tuple[float, float], ...]] = {}
    stop_pos_by_id: dict[int, float] = {}
    hdg_in_by_id: dict[int, float] = {}

    for e in raw_edges:
        sid = e["id"]
        pts = tuple(to_xy(lat, lon) for lon, lat in e["geometry"])
        cum_raw = [0.0]
        for a, b in zip(pts, pts[1:], strict=False):
            cum_raw.append(cum_raw[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        scale = e["length"] / cum_raw[-1] if cum_raw[-1] > 0 else 1.0
        cum = tuple(c * scale for c in cum_raw)
        tans = tuple(
            ((b[0] - a[0]) / nrm, (b[1] - a[1]) / nrm)
            for a, b in zip(pts, pts[1:], strict=False)
            for nrm in [math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0]
        )
        pts_by_id[sid] = pts
        cum_by_id[sid] = cum
        tan_by_id[sid] = tans
        stop_pos_by_id[sid] = e["length"] - min(14.0, 0.4 * e["length"])
        span = min(20.0, e["length"] / 2.0)
        pa = _interp(pts, cum, e["length"] - span)
        pb = _interp(pts, cum, e["length"])
        hdg_in_by_id[sid] = math.atan2(pb[1] - pa[1], pb[0] - pa[0])
        out_segs[e["u"]].append(e)
        in_segs[e["v"]].append(e)

    # Gates matching smartcity/network.py::_choose_gates
    xs = [p[0] for p in xy.values()]
    ys = [p[1] for p in xy.values()]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    gate_cands = []
    for nid, (nx, ny) in xy.items():
        if min(nx - x0, x1 - nx, ny - y0, y1 - ny) <= 700.0 and in_segs[nid] and out_segs[nid]:
            spd = max(
                max(20.0, min(70.0, float(e.get("maxspeed_kph", 50.0)))) / 3.6
                for e in in_segs[nid] + out_segs[nid]
            )
            gate_cands.append((nid, len(in_segs[nid]) + spd / 20.0))
    gate_cands.sort(key=lambda c: -c[1])
    gates: list[int] = [gate_cands[0][0]]
    while len(gates) < min(16, len(gate_cands)):
        best = max(
            (c for c in gate_cands if c[0] not in gates),
            key=lambda c: min(math.dist(xy[c[0]], xy[g]) for g in gates),
        )
        gates.append(best[0])
    gate_set = set(gates)

    # Coincident opposite twins (2-way centerlines vs separated dual carriageways)
    by_uv: dict[tuple[int, int], list[dict]] = {}
    for e in raw_edges:
        by_uv.setdefault((e["u"], e["v"]), []).append(e)
    twins: dict[int, int] = {}
    for e in raw_edges:
        sid = e["id"]
        if sid in twins:
            continue
        for rev in by_uv.get((e["v"], e["u"]), ()):
            rsid = rev["id"]
            if rsid in twins:
                continue
            if _polylines_coincide(pts_by_id[sid], pts_by_id[rsid], tol_m=2.5):
                twins[sid] = rsid
                twins[rsid] = sid
                break

    def point_at(sid: int, pos: float) -> tuple[float, float, float, float]:
        pts = pts_by_id[sid]
        cum = cum_by_id[sid]
        tans = tan_by_id[sid]
        pos = max(0.0, min(cum[-1], pos))
        i = min(bisect.bisect_right(cum, pos) - 1, len(pts) - 2)
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        span = cum[i + 1] - cum[i]
        f = (pos - cum[i]) / span if span > 0 else 0.0
        tx, ty = tans[i]
        return ax + (bx - ax) * f, ay + (by - ay) * f, tx, ty

    def lane_pos(sid: int, pos: float, lanes: int, oneway: bool) -> tuple[float, float, float, float]:
        x, y, dx, dy = point_at(sid, pos)
        if lanes <= 1:
            off = 0.0 if oneway else 3.0
        elif oneway:
            off = ((lanes - 1) * 0.5) * 3.1
        else:
            off = 1.9 + (lanes - 1) * 3.1
        return x + dy * off, y - dx * off, dx, dy

    # 1. Lane counts & elevation grades per directed segment
    edge_pairs = {(e["u"], e["v"]) for e in raw_edges}
    edge_lanes: dict[str, int] = {}
    edge_grades: dict[str, float] = {}
    for e in raw_edges:
        sid = e["id"]
        is_oneway = (e["v"], e["u"]) not in edge_pairs
        lanes = infer_lanes(e.get("name", ""), e.get("highway", ""), e["length"], is_oneway)
        edge_lanes[str(sid)] = lanes

        u_n = nodes_by_id[e["u"]]
        v_n = nodes_by_id[e["v"]]
        dz = estimate_elevation(v_n["lat"], v_n["lon"]) - estimate_elevation(u_n["lat"], u_n["lon"])
        grade_pct = max(-8.0, min(8.0, (dz / max(40.0, e["length"])) * 100.0))
        if abs(grade_pct) >= 1.2:
            edge_grades[str(sid)] = round(grade_pct, 2)

    # 2. All 16 real-world Iași car roundabouts
    rb_nodes: set[int] = set()
    roundabouts: list[dict] = []
    for nid, name, radius_m, ring_lanes, signalized in ROUNDABOUT_SPECS:
        inc = in_segs[nid] + out_segs[nid]
        inc_lens = [e["length"] for e in inc]
        safe_r = round(min(radius_m, max(12.0, min(inc_lens) * 0.28)), 1)
        sig = bool(signalized) and nid not in gate_set
        rb_nodes.add(nid)
        roundabouts.append(
            {
                "node": nid,
                "name": name,
                "radius_m": safe_r,
                "ring_lanes": ring_lanes,
                "signalized": sig,
            }
        )
        for e in in_segs[nid]:
            stop_pos_by_id[e["id"]] = e["length"] - min(safe_r + 4.5, 0.42 * e["length"])

    # 3. Unsignalized Priority/Yield Junctions (*Cedează Trecerea*):
    #    a) Minor 3-in tertiary/residential/unclassified T-junctions
    #    b) 2-in >=3-arm non-opposite yield merges (minor side streets merging into boulevards)
    landmark_nodes: set[int] = set()
    sig_candidates = {nid for nid in nodes_by_id if len(in_segs[nid]) >= 3 and nid not in gate_set}
    for _, llat, llon in landmarks_ll:
        lx, ly = to_xy(llat, llon)
        nearest = min(sig_candidates, key=lambda nid: math.hypot(xy[nid][0] - lx, xy[nid][1] - ly))
        landmark_nodes.add(nearest)

    unsig_rb_nodes = {r["node"] for r in roundabouts if not r["signalized"]}
    sig_remaining = sum(1 for nid in sig_candidates if nid not in unsig_rb_nodes)

    yield_nodes: list[int] = []
    for nid in sorted(nodes_by_id):
        if nid not in sig_candidates or nid in rb_nodes or nid in landmark_nodes:
            continue
        if len(in_segs[nid]) != 3:
            continue
        hwys = {(e.get("highway") or "").replace("_link", "") for e in in_segs[nid]}
        if "primary" not in hwys and hwys <= {"tertiary", "residential", "unclassified", "service"}:
            if sig_remaining > 115:
                yield_nodes.append(nid)
                sig_remaining -= 1

    # Add true 2-in >=3-arm non-opposite yield merges
    for nid in sorted(nodes_by_id):
        if nid in rb_nodes or nid in gate_set or len(in_segs[nid]) != 2:
            continue
        if _physical_arm_count(in_segs[nid], out_segs[nid], pts_by_id) < 3:
            continue
        h0 = hdg_in_by_id[in_segs[nid][0]["id"]]
        h1 = hdg_in_by_id[in_segs[nid][1]["id"]]
        if abs(abs(_wrap(h0 - h1)) - math.pi) >= OPPOSITE_TOLERANCE:
            yield_nodes.append(nid)

    yield_nodes = sorted(set(yield_nodes))

    # 4. All 14 real-world CTP Iași *rond* turnaround loops
    rond_loops: list[dict] = []
    for name, nid, rlat, rlon, rad_m, kind in ROND_LOOP_SPECS:
        term_lines: list[str] = []
        serve_lines: list[str] = []
        for r in routes:
            es = r["edges"]
            tleg = int(r.get("turnaround_leg", 0))
            t_nodes = {raw_edges[es[0]]["u"]}
            if 0 < tleg < len(es):
                t_nodes.add(raw_edges[es[tleg]]["v"])
            if nid in t_nodes and r["ref"] not in term_lines:
                term_lines.append(r["ref"])
            r_nodes = {raw_edges[eid]["u"] for eid in es} | {raw_edges[eid]["v"] for eid in es}
            if nid in r_nodes and r["ref"] not in serve_lines:
                serve_lines.append(r["ref"])
        lines = term_lines if term_lines else serve_lines
        rond_loops.append(
            {
                "node": nid,
                "name": name,
                "lat": round(rlat, 6),
                "lon": round(rlon, 6),
                "radius_m": round(rad_m, 1),
                "kind": kind,
                "lines": lines,
            }
        )

    # 5. Deduplicated real CTP Iași bus stops snapped from the 841 GPS coordinates in iasi_routes.json
    hub_xy = [(name, *to_xy(lat, lon), weight) for name, lat, lon, weight in PEDESTRIAN_HUBS]
    stops_by_seg: dict[int, list[tuple[float, float, str]]] = {}
    for r in routes:
        seg_set = list(dict.fromkeys(r["edges"]))
        for st in r["stops"]:
            sx, sy = to_xy(st["lat"], st["lon"])
            best_sid, best_pos, best_d = -1, 0.0, 95.0
            for sid in seg_set:
                e = raw_edges[sid]
                if e["length"] < 75.0:
                    continue
                lo, hi = 24.0, stop_pos_by_id[sid] - 22.0
                if hi <= lo:
                    continue
                lanes = edge_lanes[str(sid)]
                oneway = sid not in twins
                for k in range(6):
                    p = lo + (hi - lo) * (k / 5.0)
                    px, py, _, _ = lane_pos(sid, p, lanes, oneway)
                    d = math.hypot(px - sx, py - sy)
                    if d < best_d:
                        best_d, best_sid, best_pos = d, sid, p
            if best_sid >= 0:
                existing = stops_by_seg.setdefault(best_sid, [])
                if all(abs(ep - best_pos) >= 140.0 for ep, _, _ in existing):
                    demand = 1.0
                    hname = raw_edges[best_sid].get("name") or f"Stație {best_sid}"
                    lanes_b = edge_lanes[str(best_sid)]
                    px, py, _, _ = lane_pos(best_sid, best_pos, lanes_b, best_sid not in twins)
                    for hn, hx, hy, hw in hub_xy:
                        if math.hypot(px - hx, py - hy) <= 380.0:
                            demand = max(demand, hw)
                            hname = hn
                    existing.append((best_pos, demand, hname))

    bus_stops: list[dict] = []
    for sid, items in sorted(stops_by_seg.items()):
        lanes = edge_lanes[str(sid)]
        oneway = sid not in twins
        for pos_m, demand, sname in sorted(items):
            px, py, _, _ = lane_pos(sid, pos_m, lanes, oneway)
            slat, slon = to_latlon(px, py)
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

    # 6. Marked high-pedestrian zebra crosswalks (*Treceri de pietoni*)
    crosswalks: list[dict] = []
    used_cw_segs: set[int] = set()
    for hname, hx, hy, hw in hub_xy:
        cands = []
        for e in raw_edges:
            sid = e["id"]
            if sid in used_cw_segs or e["length"] < 70.0:
                continue
            p = max(22.0, stop_pos_by_id[sid] - 10.0)
            px, py, _, _ = point_at(sid, p)
            d = math.hypot(px - hx, py - hy)
            if d <= 260.0:
                cands.append((d, sid, p))
        cands.sort()
        for _, sid, pos_m in cands[:3]:
            used_cw_segs.add(sid)
            px, py, _, _ = point_at(sid, pos_m)
            clat, clon = to_latlon(px, py)
            crosswalks.append(
                {
                    "id": len(crosswalks),
                    "seg_id": sid,
                    "pos_m": round(pos_m, 1),
                    "lat": round(clat, 6),
                    "lon": round(clon, 6),
                    "hub": hname,
                    "rate": round(0.018 * hw, 4),
                }
            )

    # 7. CFR railway level crossings
    rail_spots = (
        ("Pasaj CFR Socola", 47.1435, 27.6005),
        ("Pasaj CFR Nicolina", 47.1488, 27.5785),
    )
    rail_crossings: list[dict] = []
    for rname, rlat, rlon in rail_spots:
        rx, ry = to_xy(rlat, rlon)
        best_seg = min(
            (e for e in raw_edges if e["length"] >= 90.0),
            key=lambda e: math.hypot(
                point_at(e["id"], e["length"] * 0.5)[0] - rx,
                point_at(e["id"], e["length"] * 0.5)[1] - ry,
            ),
        )
        pos_m = round(best_seg["length"] * 0.5, 1)
        px, py, _, _ = point_at(best_seg["id"], pos_m)
        lat_c, lon_c = to_latlon(px, py)
        rail_crossings.append(
            {
                "name": rname,
                "seg_id": best_seg["id"],
                "pos_m": pos_m,
                "lat": round(lat_c, 6),
                "lon": round(lon_c, 6),
                "period_s": 420.0,
                "closed_s": 38.0,
                "offset_s": 140.0 + len(rail_crossings) * 210.0,
            }
        )

    # 8. Hospital dispatch nodes
    hospitals: list[dict] = []
    for hname, hlat, hlon in HOSPITALS:
        hx, hy = to_xy(hlat, hlon)
        node = min(raw_nodes, key=lambda n: math.hypot(xy[n["id"]][0] - hx, xy[n["id"]][1] - hy))
        hospitals.append({"name": hname, "node": node["id"], "lat": hlat, "lon": hlon})

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
            "rond_loops": len(rond_loops),
            "bus_stops": len(bus_stops),
            "crosswalks": len(crosswalks),
            "hilly_edges": len(edge_grades),
        },
        "edge_lanes": edge_lanes,
        "edge_grades": edge_grades,
        "roundabouts": roundabouts,
        "yield_nodes": yield_nodes,
        "junction_names": JUNCTION_NAMES,
        "rond_loops": rond_loops,
        "bus_stops": bus_stops,
        "crosswalks": crosswalks,
        "rail_crossings": rail_crossings,
        "hospitals": hospitals,
    }
    out = DATA_DIR / "iasi_realism.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Wrote {out} | lanes={lane_dist} | roundabouts={len(roundabouts)} "
        f"(unsig={sum(not r['signalized'] for r in roundabouts)}) | yield_J={len(yield_nodes)} | "
        f"rond_loops={len(rond_loops)} | stops={len(bus_stops)} | crosswalks={len(crosswalks)} | hills={len(edge_grades)}"
    )


if __name__ == "__main__":
    main()
