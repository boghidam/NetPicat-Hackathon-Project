"""Residential buildings (apartment blocks & houses) anchored to real road segments.

Used on real city networks (Iași) to:
1. Render clean architectural apartment-block and detached-house footprints on the map.
2. Act as real Origin-Destination (OD) residential endpoints for simulated car trips
   (60% city->city 1-8 km, 20% outside->city 5-20 km, alongside 20% cross-city through-traffic).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .network import RoadNetwork

# Iași urban center (Piața Unirii / Palas corridor)
IASI_CENTER_LAT = 47.161
IASI_CENTER_LON = 27.583

# Realistic Iași district anchors for neighborhood-accurate apartment vs house zoning
_APARTMENT_HUBS_LL: tuple[tuple[float, float, float], ...] = (
    (47.165, 27.552, 1300.0),  # Alexandru cel Bun & Dacia
    (47.173, 27.555, 1100.0),  # Păcurari
    (47.143, 27.583, 1350.0),  # Nicolina & CUG I
    (47.152, 27.588, 950.0),   # Podu Roș & Cantemir
    (47.156, 27.606, 1100.0),  # Tudor Vladimirescu
    (47.166, 27.612, 1250.0),  # Tătărași
    (47.164, 27.584, 900.0),   # Centru & Independenței
    (47.175, 27.571, 800.0),   # Lower Copou
    (47.157, 27.638, 700.0),   # Dancu (outer apartment cluster)
    (47.116, 27.580, 650.0),   # Lunca Cetățuii (outer apartment cluster)
    (47.180, 27.508, 650.0),   # Valea Lupului (mixed suburban blocks)
)

_HOUSE_HUBS_LL: tuple[tuple[float, float, float], ...] = (
    (47.172, 27.594, 850.0),   # Țicău & Sărărie
    (47.147, 27.558, 900.0),   # Galata
    (47.144, 27.597, 800.0),   # Frumoasa & Poitiers side streets
    (47.176, 27.614, 900.0),   # Moara de Vânt
    (47.187, 27.562, 1000.0),  # Upper Copou / Breazu approach
)


@dataclass(frozen=True)
class Building:
    id: int
    kind: str       # "apartment" | "house"
    zone: str       # "city" | "outer"
    seg_id: int     # directed street segment this building fronts onto
    pos_m: float    # driveway / curb position (m) along seg_id
    x: float        # footprint centre X (m) in local network coordinates
    y: float        # footprint centre Y (m) in local network coordinates
    lat: float
    lon: float
    w: float        # footprint length parallel to the street (m)
    d: float        # footprint depth perpendicular to the street (m)
    angle: float    # orientation (radians, math angle along road tangent)
    capacity: int   # relative trip weight (apartment > house)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "zone": self.zone,
            "seg_id": self.seg_id,
            "pos_m": round(self.pos_m, 1),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "w": round(self.w, 1),
            "d": round(self.d, 1),
            "angle": round(self.angle, 4),
            "capacity": self.capacity,
        }

    def to_map_tuple(self) -> list[Any]:
        """Compact array representation sent once in `/api/map`."""
        return [
            self.id,
            1 if self.kind == "apartment" else 0,
            1 if self.zone == "city" else 0,
            round(self.x, 1),
            round(self.y, 1),
            round(self.w, 1),
            round(self.d, 1),
            round(self.angle, 3),
        ]


def _apartment_probability(x: float, y: float, zone: str, apt_hubs: list[tuple[float, float, float]], house_hubs: list[tuple[float, float, float]]) -> float:
    """Probability a building at (x, y) is an apartment block rather than a detached house."""
    in_apt = any(math.hypot(x - hx, y - hy) <= hr for hx, hy, hr in apt_hubs)
    in_house = any(math.hypot(x - hx, y - hy) <= hr for hx, hy, hr in house_hubs)
    if zone == "city":
        if in_apt and not in_house:
            return 0.84
        if in_house and not in_apt:
            return 0.28
        return 0.65
    # Outer / suburban ring
    if in_apt:
        return 0.45
    return 0.14


def generate_buildings(net: RoadNetwork, seed: int = 42, urban_radius_m: float = 4200.0) -> list[Building]:
    """Deterministically generate non-overlapping street-aligned apartments & houses for a real road network."""
    rng = random.Random(seed)
    cx, cy = net.proj.to_xy(IASI_CENTER_LAT, IASI_CENTER_LON)
    apt_hubs = [(*net.proj.to_xy(lat, lon), rad) for lat, lon, rad in _APARTMENT_HUBS_LL]
    house_hubs = [(*net.proj.to_xy(lat, lon), rad) for lat, lon, rad in _HOUSE_HUBS_LL]

    # 1. Build a fast spatial hash of all road centerline samples (every 12 m) for collision checks
    road_cell = 28.0
    road_grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for seg in net.segments:
        n_steps = max(2, int(seg.length // 12.0) + 1)
        for k in range(n_steps + 1):
            px, py, _, _ = net.point_at(seg.id, (k / n_steps) * seg.length)
            key = (int(math.floor(px / road_cell)), int(math.floor(py / road_cell)))
            road_grid.setdefault(key, []).append((px, py))

    def near_any_road(px: float, py: float, min_dist: float) -> bool:
        gx, gy = int(math.floor(px / road_cell)), int(math.floor(py / road_cell))
        min_d2 = min_dist * min_dist
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for rx, ry in road_grid.get((gx + dx, gy + dy), ()):
                    ddx = px - rx
                    ddy = py - ry
                    if ddx * ddx + ddy * ddy < min_d2:
                        return True
        return False

    # 2. Spatial hash of placed buildings (center + bounding radius)
    b_cell = 45.0
    b_grid: dict[tuple[int, int], list[tuple[float, float, float]]] = {}

    def overlaps_building(px: float, py: float, rad: float) -> bool:
        gx, gy = int(math.floor(px / b_cell)), int(math.floor(py / b_cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for bx, by, brad in b_grid.get((gx + dx, gy + dy), ()):
                    need = rad + brad + 7.5
                    if (px - bx) ** 2 + (py - by) ** 2 < need * need:
                        return True
        return False

    def register_building(px: float, py: float, rad: float) -> None:
        key = (int(math.floor(px / b_cell)), int(math.floor(py / b_cell)))
        b_grid.setdefault(key, []).append((px, py, rad))

    # 3. Walk eligible segments (city streets first so urban core gets dense crisp coverage)
    eligible = []
    for seg in net.segments:
        hwy = (seg.highway or "").lower()
        if "motorway" in hwy or "trunk_link" in hwy:
            continue
        if seg.length < 68.0 or seg.stop_pos - 16.0 <= 28.0:
            continue
        mx, my, _, _ = net.point_at(seg.id, seg.length * 0.5)
        dist_c = math.hypot(mx - cx, my - cy)
        zone = "city" if dist_c <= urban_radius_m else "outer"
        eligible.append((0 if zone == "city" else 1, dist_c, seg.id, zone))

    eligible.sort()

    buildings: list[Building] = []
    for _, _, seg_id, zone in eligible:
        seg = net.segments[seg_id]
        usable_start = 26.0
        usable_end = seg.stop_pos - 16.0
        usable_len = usable_end - usable_start
        if usable_len < 20.0:
            continue

        # Target spacing along the street (crisp urban fabric inside city, airier in suburbs)
        spacing = 125.0 if zone == "city" else 235.0
        max_on_seg = 3 if zone == "city" else 2
        n_slots = min(max_on_seg, max(1, int(usable_len / spacing) + 1))

        # Slight jittered centers across the usable span
        step = usable_len / n_slots
        for slot_i in range(n_slots):
            pos_m = usable_start + (slot_i + 0.5) * step + rng.uniform(-0.22, 0.22) * min(step, 35.0)
            pos_m = max(usable_start, min(usable_end, pos_m))

            rx, ry, tx, ty = net.point_at(seg_id, pos_m)
            # Re-evaluate zone from exact building curb position
            pt_zone = "city" if math.hypot(rx - cx, ry - cy) <= urban_radius_m else "outer"
            apt_prob = _apartment_probability(rx, ry, pt_zone, apt_hubs, house_hubs)
            is_apt = rng.random() < apt_prob
            kind = "apartment" if is_apt else "house"

            if is_apt:
                w = rng.uniform(28.0, 44.0)
                d = rng.uniform(14.0, 18.0)
                capacity = 4
            else:
                w = rng.uniform(11.5, 16.0)
                d = rng.uniform(9.5, 13.0)
                capacity = 1

            # Offset onto right-hand sidewalk side of directed segment: right normal = (ty, -tx)
            road_half = 6.0 if "primary" in (seg.highway or "") else 4.8
            setback = road_half + d * 0.5 + rng.uniform(9.5, 14.5)
            bx = rx + ty * setback
            by = ry - tx * setback
            angle = math.atan2(ty, tx)
            rad = 0.5 * math.hypot(w, d)

            # Check all 4 corners + center against all roads and other buildings
            hw, hd = w * 0.5, d * 0.5
            nx_v, ny_v = ty, -tx
            corners = (
                (bx, by),
                (bx + tx * hw + nx_v * hd, by + ty * hw + ny_v * hd),
                (bx - tx * hw + nx_v * hd, by - ty * hw + ny_v * hd),
                (bx + tx * hw - nx_v * hd, by + ty * hw - ny_v * hd),
                (bx - tx * hw - nx_v * hd, by - ty * hw - ny_v * hd),
            )
            if any(near_any_road(px, py, 10.5) for px, py in corners):
                continue
            if overlaps_building(bx, by, rad):
                continue

            lat, lon = net.proj.to_latlon(bx, by)
            buildings.append(
                Building(
                    id=len(buildings),
                    kind=kind,
                    zone=pt_zone,
                    seg_id=seg_id,
                    pos_m=pos_m,
                    x=bx,
                    y=by,
                    lat=lat,
                    lon=lon,
                    w=w,
                    d=d,
                    angle=angle,
                    capacity=capacity,
                )
            )
            register_building(bx, by, rad)

    return buildings


def load_buildings_json(path: Path, net: RoadNetwork) -> list[Building]:
    """Load pre-generated buildings JSON and project `(lat, lon)` into `net.proj`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("buildings", raw) if isinstance(raw, dict) else raw
    result: list[Building] = []
    n_segs = len(net.segments)
    for idx, b in enumerate(items):
        seg_id = int(b["seg_id"])
        if seg_id < 0 or seg_id >= n_segs:
            continue
        lat, lon = float(b["lat"]), float(b["lon"])
        x, y = net.proj.to_xy(lat, lon)
        seg = net.segments[seg_id]
        pos_m = max(10.0, min(seg.stop_pos - 8.0, float(b["pos_m"])))
        result.append(
            Building(
                id=int(b.get("id", idx)),
                kind=str(b["kind"]),
                zone=str(b["zone"]),
                seg_id=seg_id,
                pos_m=pos_m,
                x=x,
                y=y,
                lat=lat,
                lon=lon,
                w=float(b["w"]),
                d=float(b["d"]),
                angle=float(b["angle"]),
                capacity=int(b.get("capacity", 4 if b["kind"] == "apartment" else 1)),
            )
        )
    return result


def save_buildings_json(buildings: list[Building], path: Path, urban_radius_m: float = 4200.0) -> None:
    """Persist deterministic buildings dataset shared by Python backend and GitHub Pages JS."""
    city_apts = sum(1 for b in buildings if b.zone == "city" and b.kind == "apartment")
    city_houses = sum(1 for b in buildings if b.zone == "city" and b.kind == "house")
    outer_apts = sum(1 for b in buildings if b.zone == "outer" and b.kind == "apartment")
    outer_houses = sum(1 for b in buildings if b.zone == "outer" and b.kind == "house")
    payload = {
        "meta": {
            "center_lat": IASI_CENTER_LAT,
            "center_lon": IASI_CENTER_LON,
            "urban_radius_m": urban_radius_m,
            "total": len(buildings),
            "city_apartments": city_apts,
            "city_houses": city_houses,
            "outer_apartments": outer_apts,
            "outer_houses": outer_houses,
        },
        "buildings": [b.to_dict() for b in buildings],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
