"""Tranzy Open Data API provider (`Terra (Real)` mode) with GPS-to-segment map-matching."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import numpy as np
import requests

from ..mock_api import BusTelemetry, TelemetryBus
from ..network import RoadNetwork
from .base import LiveBusSnapshot, ProviderStatus, TelemetryProvider
from .credentials import DEFAULT_TRANZY_AGENCY_ID

TRANZY_BASE_URL = "https://api.tranzy.ai/v1/opendata"
TRANZY_ATTRIBUTION = "Data © Tranzy AI SRL & Compania de Transport Public Iași SA"


class MapMatcher:
    """Fast vectorized GPS `(lat, lon)` -> Iași `RoadNetwork` segment snapper."""

    def __init__(self, net: RoadNetwork):
        self.net = net
        # Precompute polyline segment midpoints and bounding vectors for fast nearest-edge lookup
        self._seg_ids: list[int] = []
        self._ax: list[float] = []
        self._ay: list[float] = []
        self._bx: list[float] = []
        self._by: list[float] = []
        self._cum_start: list[float] = []
        self._seg_len: list[float] = []
        self._by_seg: dict[int, list[int]] = {}
        for seg in net.segments:
            pts = seg.points
            cum = seg.cum
            idxs: list[int] = []
            for i in range(len(pts) - 1):
                idx = len(self._seg_ids)
                self._seg_ids.append(seg.id)
                self._ax.append(pts[i][0])
                self._ay.append(pts[i][1])
                self._bx.append(pts[i + 1][0])
                self._by.append(pts[i + 1][1])
                self._cum_start.append(cum[i])
                self._seg_len.append(max(1e-6, cum[i + 1] - cum[i]))
                idxs.append(idx)
            self._by_seg[seg.id] = idxs

        self.ax = np.array(self._ax, dtype=float)
        self.ay = np.array(self._ay, dtype=float)
        self.bx = np.array(self._bx, dtype=float)
        self.by = np.array(self._by, dtype=float)
        self.dx = self.bx - self.ax
        self.dy = self.by - self.ay
        self.len2 = np.maximum(1e-9, self.dx * self.dx + self.dy * self.dy)
        self.cum_start = np.array(self._cum_start, dtype=float)
        self.piece_len = np.array(self._seg_len, dtype=float)
        self.seg_ids = np.array(self._seg_ids, dtype=int)

        # Precompute index masks for each route so route-aware snapping is O(route pieces)
        self._route_piece_idx: dict[str, np.ndarray] = {}
        self._route_edge_leg: dict[str, dict[int, int]] = {}
        for r in net.routes:
            pieces: list[int] = []
            edge_to_leg: dict[int, int] = {}
            for leg, eid in enumerate(r.edges):
                if eid not in edge_to_leg:
                    edge_to_leg[eid] = leg
                pieces.extend(self._by_seg.get(eid, ()))
            if pieces:
                self._route_piece_idx[r.ref.upper()] = np.array(pieces, dtype=int)
                self._route_edge_leg[r.ref.upper()] = edge_to_leg

    def snap(
        self,
        lat: float,
        lon: float,
        line: str = "",
        max_dist_m: float = 350.0,
    ) -> tuple[int, float, int, float] | None:
        """Snap `(lat, lon)` to `(segment_id, pos_m, leg, dist_m)`, preferring the vehicle's route."""
        x, y = self.net.proj.to_xy(lat, lon)
        key = line.strip().upper()
        cand = self._route_piece_idx.get(key)
        if cand is not None and len(cand) > 0:
            res = self._snap_subset(x, y, cand)
            if res is not None and res[2] <= 180.0:
                seg_id, pos, dist = res
                leg = self._route_edge_leg.get(key, {}).get(seg_id, 0)
                return seg_id, pos, leg, dist

        res = self._snap_subset(x, y, None)
        if res is None or res[2] > max_dist_m:
            return None
        seg_id, pos, dist = res
        leg = self._route_edge_leg.get(key, {}).get(seg_id, 0)
        return seg_id, pos, leg, dist

    def _snap_subset(self, x: float, y: float, subset: np.ndarray | None) -> tuple[int, float, float] | None:
        if len(self.ax) == 0:
            return None
        if subset is None:
            ax, ay, dx, dy, len2 = self.ax, self.ay, self.dx, self.dy, self.len2
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / len2, 0.0, 1.0)
            px = ax + t * dx
            py = ay + t * dy
            d2 = (x - px) ** 2 + (y - py) ** 2
            best = int(np.argmin(d2))
            idx = best
            t_best = float(t[best])
            dist = math.sqrt(float(d2[best]))
        else:
            ax, ay = self.ax[subset], self.ay[subset]
            dx, dy = self.dx[subset], self.dy[subset]
            len2 = self.len2[subset]
            t = np.clip(((x - ax) * dx + (y - ay) * dy) / len2, 0.0, 1.0)
            px = ax + t * dx
            py = ay + t * dy
            d2 = (x - px) ** 2 + (y - py) ** 2
            loc = int(np.argmin(d2))
            idx = int(subset[loc])
            t_best = float(t[loc])
            dist = math.sqrt(float(d2[loc]))

        seg_id = int(self.seg_ids[idx])
        pos = float(self.cum_start[idx] + t_best * self.piece_len[idx])
        seg = self.net.segments[seg_id]
        return seg_id, max(0.0, min(seg.length, pos)), dist


@dataclass
class _TrackedVehicle:
    bus_id: str
    line: str
    lat: float
    lon: float
    seg_id: int
    pos: float
    leg: int
    speed: float  # m/s
    delay: float
    last_seen: float
    is_parked: bool = False


class TranzyProvider(TelemetryProvider):
    """Background polling provider for the Tranzy Open Data API (`Terra (Real)` mode)."""

    def __init__(
        self,
        net: RoadNetwork,
        bus: TelemetryBus,
        api_key: str = "",
        agency_id: str = DEFAULT_TRANZY_AGENCY_ID,
        poll_interval_s: float = 15.0,
        base_url: str = TRANZY_BASE_URL,
    ):
        self.net = net
        self.api = bus
        self.api_key = api_key.strip()
        self.agency_id = str(agency_id).strip() or DEFAULT_TRANZY_AGENCY_ID
        self.poll_interval_s = max(5.0, float(poll_interval_s))
        self.base_url = base_url.rstrip("/")
        self.matcher = MapMatcher(net)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._route_names: dict[str, str] = {}
        self._vehicles: dict[str, _TrackedVehicle] = {}
        self._status = ProviderStatus(
            provider="tranzy",
            connected=False,
            poll_interval_s=self.poll_interval_s,
            agency_id=self.agency_id,
            attribution=TRANZY_ATTRIBUTION,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-API-KEY": self.api_key,
            "X-Agency-Id": self.agency_id,
            "Accept": "application/json",
            "User-Agent": "noodle-traffic-control/1.0",
        }

    def configure(self, api_key: str, agency_id: str = DEFAULT_TRANZY_AGENCY_ID) -> None:
        with self._lock:
            self.api_key = api_key.strip()
            self.agency_id = str(agency_id).strip() or DEFAULT_TRANZY_AGENCY_ID
            self._status.agency_id = self.agency_id
            self._route_names.clear()
        if self._thread is not None and self._thread.is_alive():
            self.poll_once()

    @classmethod
    def test_connection(
        cls,
        api_key: str,
        agency_id: str = DEFAULT_TRANZY_AGENCY_ID,
        base_url: str = TRANZY_BASE_URL,
    ) -> tuple[bool, str, int]:
        """Validate a Tranzy API key and agency ID synchronously."""
        key = api_key.strip()
        if not key:
            return False, "API key is empty.", 0
        headers = {
            "X-API-KEY": key,
            "X-Agency-Id": str(agency_id).strip() or DEFAULT_TRANZY_AGENCY_ID,
            "Accept": "application/json",
            "User-Agent": "noodle-traffic-control/1.0",
        }
        try:
            resp = requests.get(f"{base_url.rstrip('/')}/vehicles", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                n = len(data) if isinstance(data, list) else 0
                return True, f"Connected to Tranzy API ({n} active vehicles reported).", n
            if resp.status_code in (401, 403):
                return False, f"Authentication rejected (HTTP {resp.status_code}). Check your X-API-KEY and X-Agency-Id.", 0
            return False, f"Tranzy API returned HTTP {resp.status_code}.", 0
        except requests.RequestException as err:
            return False, f"Network error reaching Tranzy API: {err}", 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="tranzy-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self.poll_interval_s)

    def _fetch_routes_locked(self) -> None:
        if self._route_names or not self.api_key:
            return
        try:
            resp = requests.get(f"{self.base_url}/routes", headers=self._headers, timeout=10)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, list):
                    for r in payload:
                        rid = str(r.get("route_id", ""))
                        short = str(r.get("route_short_name") or r.get("route_long_name") or "").strip()
                        if rid and short:
                            self._route_names[rid] = short
        except requests.RequestException:
            pass

    def poll_once(self) -> bool:
        """Fetch latest vehicles from Tranzy API and ingest into tracked state."""
        if not self.api_key:
            with self._lock:
                self._status.connected = False
                self._status.error = "Tranzy API key not configured."
            return False
        try:
            self._fetch_routes_locked()
            resp = requests.get(f"{self.base_url}/vehicles", headers=self._headers, timeout=12)
            if resp.status_code != 200:
                with self._lock:
                    self._status.connected = False
                    self._status.error = f"Tranzy HTTP {resp.status_code}"
                return False
            raw_list = resp.json()
            if not isinstance(raw_list, list):
                with self._lock:
                    self._status.connected = False
                    self._status.error = "Unexpected Tranzy response payload."
                return False
            self.ingest_payload(raw_list, now_wall=time.time())
            return True
        except requests.RequestException as err:
            with self._lock:
                self._status.connected = False
                self._status.error = f"Connection error: {err}"
            return False

    def ingest_payload(self, vehicles_json: list[dict], now_wall: float | None = None) -> None:
        """Parse a Tranzy `/vehicles` JSON array and update tracked vehicles & route counts."""
        ts = time.time() if now_wall is None else now_wall
        route_counts: dict[str, int] = {}
        seen_ids: set[str] = set()
        mapped = 0

        with self._lock:
            for item in vehicles_json:
                # Filter to buses when vehicle_type is present (3 = Bus in GTFS/Tranzy; 0 = Tram)
                vtype = item.get("vehicle_type")
                if vtype is not None and str(vtype) not in ("3", "bus", "BUS"):
                    continue
                lat = item.get("latitude")
                lon = item.get("longitude")
                if lat is None or lon is None:
                    continue
                try:
                    lat_f, lon_f = float(lat), float(lon)
                except (TypeError, ValueError):
                    continue

                vid = str(item.get("label") or item.get("id") or f"T{len(seen_ids)}")
                rid = str(item.get("route_id") or "")
                line = str(item.get("route_short_name") or self._route_names.get(rid) or rid or "?").strip()
                if line and line != "?":
                    route_counts[line] = route_counts.get(line, 0) + 1

                spd_raw = item.get("speed")
                # Tranzy reports speed in km/h (integer/float); convert to m/s
                speed_ms = max(0.0, float(spd_raw) / 3.6) if spd_raw is not None else 0.0
                snapped = self.matcher.snap(lat_f, lon_f, line=line)
                if snapped is None:
                    continue
                seg_id, pos, leg, _dist = snapped
                mapped += 1
                seen_ids.add(vid)
                prev = self._vehicles.get(vid)
                delay = prev.delay if prev is not None else 0.0
                self._vehicles[vid] = _TrackedVehicle(
                    bus_id=vid,
                    line=line,
                    lat=lat_f,
                    lon=lon_f,
                    seg_id=seg_id,
                    pos=pos,
                    leg=leg,
                    speed=min(speed_ms, self.net.segments[seg_id].speed_limit * 1.15),
                    delay=delay,
                    last_seen=ts,
                    is_parked=speed_ms < 0.3 and pos < 15.0,
                )

            # Drop stale vehicles missing for > 90 s
            for old_id in [k for k, v in self._vehicles.items() if ts - v.last_seen > 90.0]:
                del self._vehicles[old_id]

            self._status.connected = True
            self._status.error = None
            self._status.active_vehicles = len(vehicles_json)
            self._status.mapped_vehicles = mapped
            self._status.last_sync_at = ts
            self._status.route_counts = dict(sorted(route_counts.items()))

    def step(self, now: float, dt: float) -> list[BusTelemetry]:
        """Advance smooth interpolation between Tranzy polls and publish `BusTelemetry`."""
        emitted: list[BusTelemetry] = []
        with self._lock:
            for v in self._vehicles.values():
                seg = self.net.segments[v.seg_id]
                # Smoothly advance position along the snapped segment (capped at stop line)
                if v.speed > 0.2 and not v.is_parked:
                    v.pos = min(seg.stop_pos, v.pos + v.speed * dt)
                    v.delay += dt * max(0.0, 1.0 - min(1.0, v.speed / seg.speed_limit))
                x, y, _dx, _dy = self.net.position(seg.id, v.pos)
                v.lat, v.lon = self.net.proj.to_latlon(x, y)
                if v.is_parked:
                    continue
                rec = BusTelemetry(
                    bus_id=v.bus_id,
                    t=now,
                    x=x,
                    y=y,
                    segment_id=seg.id,
                    dist_to_next_node=max(0.0, seg.stop_pos - v.pos),
                    speed=v.speed,
                    free_speed=seg.speed_limit,
                    delay=v.delay,
                    line=v.line,
                    leg=v.leg,
                )
                self.api.publish(rec)
                emitted.append(rec)
        return emitted

    def status(self) -> ProviderStatus:
        with self._lock:
            return ProviderStatus(
                provider=self._status.provider,
                connected=self._status.connected,
                active_vehicles=self._status.active_vehicles,
                mapped_vehicles=self._status.mapped_vehicles,
                last_sync_at=self._status.last_sync_at,
                poll_interval_s=self._status.poll_interval_s,
                error=self._status.error,
                agency_id=self._status.agency_id,
                route_counts=dict(self._status.route_counts),
                attribution=self._status.attribution,
            )

    def snapshots(self) -> list[LiveBusSnapshot]:
        out: list[LiveBusSnapshot] = []
        with self._lock:
            for v in self._vehicles.values():
                seg = self.net.segments[v.seg_id]
                x, y, dx, dy = self.net.position(seg.id, v.pos)
                out.append(
                    LiveBusSnapshot(
                        bus_id=v.bus_id,
                        line=v.line,
                        lat=v.lat,
                        lon=v.lon,
                        x=x,
                        y=y,
                        dx=dx,
                        dy=dy,
                        speed=v.speed,
                        free_speed=seg.speed_limit,
                        segment_id=seg.id,
                        dist_to_next_node=max(0.0, seg.stop_pos - v.pos),
                        delay=v.delay,
                        status="LAYOVER" if v.is_parked else "DRIVING",
                    )
                )
        return out
