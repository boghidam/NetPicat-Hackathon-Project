"""Ground-truth microscopic traffic: multi-lane roads, Romanian roundabouts (OUG 195 + HG 130/2023),
real CTP bus stops, heterogeneous vehicle classes, hills, crosswalks, weather, and emergency preemption.

Controllers never read car positions directly — buses remain the sole traffic sensors.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .config import Config
from .network import RoadNetwork, Roundabout, Segment

INF = math.inf
TWO_PI = 2.0 * math.pi

ARTICULATED_LINES = frozenset({"19", "28", "30", "42", "43", "46", "52"})
MINIBUS_LINES = frozenset({"18", "23b", "48", "49", "53", "104", "204", "501b", "503b", "603", "802b"})

WEATHER_PROFILES: dict[str, tuple[float, float, float, float]] = {
    # weather -> (speed_mult, accel_mult, gap_mult, demand_mult)
    "clear": (1.00, 1.00, 1.00, 1.00),
    "rain": (0.85, 0.80, 1.25, 1.18),
    "snow": (0.70, 0.62, 1.55, 1.05),
}


@dataclass
class Vehicle:
    id: str
    kind: str  # "car", "bus" or "blocker" (a stalled vehicle / double-parked van)
    seg: int
    pos: float  # m from the segment start to the vehicle's front bumper
    v: float
    length: float
    speed_factor: float  # desired speed as a share of the segment's speed limit
    route: list[int] | None = None  # segment ids to follow
    cyclic: bool = False  # buses repeat their route
    leg: int = 0  # index of the current segment in the route
    next_seg: int | None = None  # segment taken after the intersection ahead (None: leaves the map)
    line: str | None = None  # buses: public line number
    delay: float = 0.0  # seconds beyond the free-flow schedule
    elapsed: float = 0.0
    laps: int = 0
    status: str = "DRIVING"  # "DRIVING" | "LAYOVER" | "DRIVER_BREAK"
    rest_remaining: float = 0.0  # s remaining at terminus bay
    stops_since_break: int = 0
    next_break_at: int = 4
    turnaround_leg: int = 0
    trip_kind: str = "through_city"  # "city_to_building" | "outer_to_building" | "through_city" | "emergency"
    dest_building_id: int | None = None
    dest_pos: float | None = None  # curb arrival position (m) on the final leg for building-destined cars
    trip_dist_m: float = 0.0  # total planned route distance (m)
    # --- multi-lane, vehicle-class, roundabout & stop realism ----------------
    lane: int = 0  # 0 = outer/curb lane (Bandă 1), n-1 = inner/left lane
    lane_visual: float = 0.0  # smoothly interpolated visual lane index
    lane_cooldown: float = 0.0  # s before next lane change
    vehicle_class: str = "car"  # "car" | "van" | "truck" | "bus_std" | "bus_art" | "bus_mini" | "ambulance"
    accel_factor: float = 1.0
    reaction_gap: float = 0.0
    impatience: float = 0.0
    # Roundabout circulating kinematics (None when on a regular segment)
    rb_node: int | None = None
    rb_lane: int = 0  # 0 = Bandă 1 (outer), 1 = Bandă 2 (inner)
    rb_lane_visual: float = 0.0
    rb_angle: float = 0.0  # current angle (rad) around roundabout center
    rb_exit_angle: float = 0.0
    rb_remaining: float = 0.0  # counterclockwise arc (rad) left to reach exit
    rb_target_seg: int | None = None
    # Real CTP bus stop boarding state
    stop_dwell: float = 0.0
    last_stop_id: int = -1
    in_bus_bay: bool = False


class TrafficModel:
    def __init__(self, net: RoadNetwork, cfg: Config, rng: random.Random):
        self.net, self.cfg, self.rng = net, cfg, rng
        self._is_real_city = "synthetic" not in net.source
        # Multi-lane sorted vehicle lists: lane_tracks[seg_id][lane_idx], front vehicle first.
        # `self.lanes[seg_id]` aliases `lane_tracks[seg_id][0]` for 100% compatibility on 1-lane streets & grid tests.
        self.lane_tracks: list[list[list[Vehicle]]] = [[[] for _ in range(seg.lanes)] for seg in net.segments]
        self.lanes: list[list[Vehicle]] = [tracks[0] for tracks in self.lane_tracks]
        self.buses: list[Vehicle] = []
        self.resting_buses: list[Vehicle] = []  # buses parked in terminal bays (layover / driver break)
        self.bay_buses: list[Vehicle] = []  # buses boarding passengers inside recessed roadside bus bays
        self.on_bus_rest: list[Callable[[str], None]] = []  # notified when a bus parks at a terminus
        self.incidents: dict[int, tuple[Vehicle, float]] = {}  # segment -> (blocker, expiry)
        self.friction_blockers: dict[int, tuple[Vehicle, float]] = {}  # curbside double-parked vans on multi-lane roads
        self.backlog = {g: 0 for g in net.gates}  # cars waiting outside a gate
        self.demand_scale = 1.0  # runtime car traffic amount: 1.0 is normal (100%), 0 spawns nothing
        self.day_demand_mult = 1.0  # optional 24h day-cycle multiplier
        self.rush_node: int | None = None  # key point under rush-hour pressure
        self.rush_backlog = {g: 0 for g in net.gates}
        self.next_release = [0.0] * len(net.segments)
        self.exited = 0
        self.dropped = 0
        self.building_arrivals = 0
        self.residential_spawns = 0
        self.roundabout_entries = 0
        self.lane_changes = 0
        self.stop_boardings = 0
        self.wave1_done = False
        self.active_by_kind: dict[str, int] = {
            "city_to_building": 0,
            "outer_to_building": 0,
            "through_city": 0,
        }
        self._city_bldg_pool = [b for b in net.city_buildings for _ in range(max(1, b.capacity))]
        self._outer_bldg_pool = [b for b in net.outer_buildings for _ in range(max(1, b.capacity))]

        # Roundabout circulating vehicles & per-approach / per-ring-lane follow-up headway timestamps
        self.roundabout_rings: dict[int, list[Vehicle]] = {rb.node: [] for rb in net.roundabouts}
        self._rb_last_entry: dict[int, float] = {}
        self._rb_lane_entry_t: dict[tuple[int, int], float] = {}
        self._lc_accum: float = 0.0

        # Dynamic realism environment state
        self.weather: str = cfg.weather if cfg.weather in WEATHER_PROFILES else "clear"
        self.bus_stops_enabled: bool = cfg.bus_stops_enabled and self._is_real_city
        self.crosswalks_enabled: bool = cfg.crosswalks_enabled and self._is_real_city
        self.rail_crossings_enabled: bool = cfg.rail_crossings_enabled and self._is_real_city
        self.urban_friction_enabled: bool = cfg.urban_friction_enabled and self._is_real_city
        self.active_crosswalks: dict[int, float] = {}  # crosswalk_id -> expiry sim time
        self.active_rail_crossings: set[int] = set()  # seg_ids with CFR rail barrier currently down
        self.ambulances: list[Vehicle] = []
        self._friction_next_t: float = 25.0

        self.car_speed_ratio = 1.0  # EMA of car speed / speed limit
        self.car_ratio_sum = 0.0  # car speed / speed limit, summed over every car on every tick
        self.car_ratio_n = 0  # car-ticks in that sum: their ratio is the mean car speed over the whole run
        self._ids = 0
        self._release_factor: Callable[[int], float] = lambda s: 1.0
        self._signal_state: Callable[[int], str] = lambda s: "G"
        # vehicles that have passed a stop line and are crossing the box: target edge -> {vehicle id: source edge}
        self.in_box: dict[int, dict[str, int]] = {}
        self._seed_cars()

    # -- weather & environment helpers ---------------------------------------
    def _weather_mults(self) -> tuple[float, float, float, float]:
        return WEATHER_PROFILES.get(self.weather, WEATHER_PROFILES["clear"])

    def set_weather(self, weather: str) -> None:
        w = weather.strip().lower()
        if w not in WEATHER_PROFILES:
            raise ValueError(f"unknown weather {weather!r}; choose clear, rain, or snow")
        self.weather = w

    def set_urban_friction(self, enabled: bool) -> None:
        self.urban_friction_enabled = bool(enabled) and self._is_real_city
        if not self.urban_friction_enabled:
            for sid in list(self.friction_blockers):
                self._clear_friction_blocker(sid)

    # -- creation ------------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:03d}"

    def _free_at(self, seg_id: int, pos: float, length: float, lane: int = 0) -> bool:
        gap = self.cfg.min_gap + 1.0
        track = self.lane_tracks[seg_id][min(lane, len(self.lane_tracks[seg_id]) - 1)]
        return all(abs(u.pos - pos) >= (u.length if u.pos > pos else length) + gap for u in track)

    @staticmethod
    def _insert(lane: list[Vehicle], v: Vehicle) -> None:
        i = 0
        while i < len(lane) and lane[i].pos > v.pos:
            i += 1
        lane.insert(i, v)

    @staticmethod
    def _next_on_route(v: Vehicle) -> int | None:
        assert v.route is not None
        if v.cyclic:
            return v.route[(v.leg + 1) % len(v.route)]
        return v.route[v.leg + 1] if v.leg + 1 < len(v.route) else None

    def _sample_vehicle_profile(self, trip_kind: str) -> tuple[str, float, float, float, float]:
        """Return `(vehicle_class, length, accel_factor, speed_mult, reaction_gap)`."""
        if not self._is_real_city:
            return "car", self.cfg.car_length, 1.0, 1.0, 0.0
        if trip_kind == "emergency":
            return "ambulance", 6.2, 1.35, 1.28, 0.0
        r = self.rng.random()
        # Heavy trucks concentrate on outer / cross-city corridors; urban core is mostly cars & delivery vans
        truck_p = 0.14 if trip_kind == "through_city" else (0.09 if trip_kind == "outer_to_building" else 0.04)
        van_p = 0.16
        if r < truck_p:
            return "truck", 10.8, 0.56, 0.84, self.rng.uniform(0.4, 1.0)
        if r < truck_p + van_p:
            return "van", 5.8, 0.82, 0.94, self.rng.uniform(0.2, 0.7)
        return "car", self.cfg.car_length, self.rng.uniform(0.92, 1.12), 1.0, self.rng.uniform(0.0, 0.65)

    def _bus_profile(self, line: str | None) -> tuple[str, float, float]:
        if not self._is_real_city or not line:
            return "bus_std", self.cfg.bus_length, 1.0
        if line in ARTICULATED_LINES:
            return "bus_art", 12.6, 0.72
        if line in MINIBUS_LINES:
            return "bus_mini", 8.4, 0.88
        return "bus_std", 11.0, 0.80

    def _make_car(
        self,
        route: list[int],
        leg: int,
        pos: float,
        speed: float,
        *,
        trip_kind: str = "through_city",
        dest_building_id: int | None = None,
        dest_pos: float | None = None,
        trip_dist_m: float = 0.0,
        lane: int = 0,
    ) -> Vehicle:
        cfg = self.cfg
        vclass, length, acc_f, spd_m, rgap = self._sample_vehicle_profile(trip_kind)
        factor = spd_m * (1.0 + self.rng.uniform(-cfg.car_speed_var, cfg.car_speed_var))
        seg_id = route[leg]
        lane = max(0, min(self.net.segments[seg_id].lanes - 1, lane))
        prefix = "A" if vclass == "ambulance" else "C"
        car = Vehicle(
            self._new_id(prefix), "car", seg_id, pos, speed, length, factor,
            route=route, leg=leg, trip_kind=trip_kind, dest_building_id=dest_building_id,
            dest_pos=dest_pos, trip_dist_m=trip_dist_m,
            lane=lane, lane_visual=float(lane), vehicle_class=vclass,
            accel_factor=acc_f, reaction_gap=rgap,
        )  # fmt: skip
        car.next_seg = self._next_on_route(car)
        return car

    def _register_car(self, car: Vehicle) -> None:
        if car.trip_kind in self.active_by_kind:
            self.active_by_kind[car.trip_kind] += 1

    def _unregister_car(self, car: Vehicle) -> None:
        if car.kind == "car" and car.trip_kind in self.active_by_kind:
            self.active_by_kind[car.trip_kind] = max(0, self.active_by_kind[car.trip_kind] - 1)
        if car in self.ambulances:
            self.ambulances.remove(car)

    def _can_spawn_at_curb(self, seg_id: int, pos: float) -> bool:
        """Verify residential street is uncrowded and curb lane (Bandă 1) has safe front/rear clearance at `pos`."""
        if self.density(seg_id) >= self.cfg.building_spawn_max_density:
            return False
        if self.in_box.get(seg_id):
            return False
        cl = self.cfg.car_length
        for u in self.lane_tracks[seg_id][0]:
            if u.pos >= pos:
                if u.pos - u.length - pos < self.cfg.min_gap + 4.0:
                    return False
            else:
                if pos - cl - u.pos < max(12.0, u.v * 1.1):
                    return False
        return True

    def _pick_initial_lane(self, seg_id: int, pos: float, length: float, prefer_curb: bool = False) -> int | None:
        n_lanes = self.net.segments[seg_id].lanes
        order = list(range(n_lanes))
        if not prefer_curb and n_lanes > 1:
            self.rng.shuffle(order)
        for li in order:
            if self._free_at(seg_id, pos, length, lane=li):
                return li
        return None

    def _seed_cars(self) -> None:
        n = self.cfg.initial_cars
        if "synthetic" in self.net.source and n > 120:
            n = 90  # keep the tiny 4x4 synthetic test grid unclogged when using city-scale defaults
        self.scatter_cars(n)
        self.wave1_done = True

    def _place_on_trip(
        self,
        route: list[int],
        *,
        trip_kind: str,
        dest_building_id: int | None = None,
        dest_pos: float | None = None,
        trip_dist_m: float = 0.0,
        start_min_pos: float = 5.0,
    ) -> bool:
        """Place a Wave 1 car at a free spot along `route`, weighted by leg travel time (ergodic steady state)."""
        segs = self.net.segments
        weights: list[float] = []
        total_w = 0.0
        for i, sid in enumerate(route):
            s = segs[sid]
            lo = start_min_pos if i == 0 else 0.0
            hi = min(s.length, dest_pos) if (dest_pos is not None and i == len(route) - 1) else s.length
            w = max(0.0, hi - lo) / max(1.0, s.speed_limit)
            total_w += w
            weights.append(total_w)
        if total_w <= 0.0:
            return False

        for _ in range(5):
            r = self.rng.random() * total_w
            leg = 0
            while leg < len(weights) - 1 and weights[leg] < r:
                leg += 1
            seg = segs[route[leg]]
            lo = start_min_pos if leg == 0 else 5.0
            hi = seg.stop_pos - 4.0
            if dest_pos is not None and leg == len(route) - 1:
                hi = min(hi, dest_pos - 6.0)
            if hi <= lo + 1.0:
                continue
            pos = self.rng.uniform(lo, hi)
            prefer_curb = dest_pos is not None and leg == len(route) - 1
            chosen_lane = self._pick_initial_lane(seg.id, pos, 11.0, prefer_curb=prefer_curb)
            if chosen_lane is not None:
                car = self._make_car(
                    route, leg, pos, 0.6 * seg.speed_limit,
                    trip_kind=trip_kind, dest_building_id=dest_building_id,
                    dest_pos=dest_pos, trip_dist_m=trip_dist_m, lane=chosen_lane,
                )  # fmt: skip
                if self._free_at(seg.id, pos, car.length, lane=chosen_lane):
                    self._insert(self.lane_tracks[seg.id][chosen_lane], car)
                    self._register_car(car)
                    return True
        return False

    def _scatter_residential_cars(self, n: int) -> int:
        """Wave 1 seeding on real city maps: 60% city->city building, 20% outside->city building, 20% cross-city."""
        cfg = self.cfg
        n_city = round(n * cfg.share_city_to_building)
        n_outer = round(n * cfg.share_outer_to_building)
        n_thru = max(0, n - n_city - n_outer)
        placed = 0

        c_placed = 0
        for _ in range(n_city * 6):
            if c_placed >= n_city or not self._city_bldg_pool:
                break
            orig = self.rng.choice(self._city_bldg_pool)
            res = self.net.building_to_building_trip(orig, self.rng, cfg.city_trip_min_m, cfg.city_trip_max_m)
            if not res:
                continue
            route, dest, dist_m = res
            if self._place_on_trip(
                route, trip_kind="city_to_building", dest_building_id=dest.id,
                dest_pos=dest.pos_m, trip_dist_m=dist_m, start_min_pos=orig.pos_m,
            ):
                c_placed += 1
                placed += 1

        o_placed = 0
        for _ in range(n_outer * 6):
            if o_placed >= n_outer:
                break
            use_outer_bldg = bool(self._outer_bldg_pool) and self.rng.random() < 0.5
            if use_outer_bldg:
                orig = self.rng.choice(self._outer_bldg_pool)
                res = self.net.building_to_building_trip(orig, self.rng, cfg.outer_trip_min_m, cfg.outer_trip_max_m)
                start_min = orig.pos_m
            else:
                gate = self.rng.choice(self.net.gates)
                res = self.net.gate_to_building_trip(gate, self.rng, cfg.outer_trip_min_m, cfg.outer_trip_max_m)
                start_min = 5.0
            if not res:
                continue
            route, dest, dist_m = res
            if self._place_on_trip(
                route, trip_kind="outer_to_building", dest_building_id=dest.id,
                dest_pos=dest.pos_m, trip_dist_m=dist_m, start_min_pos=start_min,
            ):
                o_placed += 1
                placed += 1

        t_placed = 0
        for _ in range(n_thru * 6):
            if t_placed >= n_thru:
                break
            res_t = self.net.through_city_trip(self.rng)
            if not res_t:
                break
            route, dist_m = res_t
            if self._place_on_trip(route, trip_kind="through_city", trip_dist_m=dist_m):
                t_placed += 1
                placed += 1

        return placed

    def scatter_cars(self, n: int, via: int | None = None) -> int:
        """Place up to `n` cars at random free spots (residential OD + cross-city on real maps, gate-to-gate on grid)."""
        if not self.net.gates:
            return 0
        if self.net.buildings and via is None:
            return self._scatter_residential_cars(n)
        placed = 0
        for _ in range(n * 6):
            if placed >= n:
                break
            route = self.net.random_trip(self.rng) if via is None else self.trip_via(via)
            leg = self.rng.randrange(len(route)) if via is None else self._leg_near(route, via)
            seg = self.net.segments[route[leg]]
            pos = self.rng.uniform(5.0, max(6.0, seg.stop_pos - 4.0))
            lane_i = self._pick_initial_lane(seg.id, pos, 11.0 if self._is_real_city else self.cfg.car_length)
            if lane_i is not None:
                car = self._make_car(route, leg, pos, 0.6 * seg.speed_limit, trip_kind="through_city", lane=lane_i)
                if self._free_at(seg.id, pos, car.length, lane=lane_i):
                    self._insert(self.lane_tracks[seg.id][lane_i], car)
                    self._register_car(car)
                    placed += 1
        return placed

    def _leg_near(self, route: list[int], node: int) -> int:
        segs = self.net.segments
        ends, total = [], 0.0
        for e in route:
            total += segs[e].length
            ends.append(total)
        at = next((i for i, e in enumerate(route) if segs[e].to_node == node), None)
        if at is None:
            return self.rng.randrange(len(route))
        near = [i for i, e in enumerate(route) if abs(ends[i] - segs[e].length / 2 - ends[at]) <= self.cfg.rush_reach]
        return self.rng.choice(near or [at])

    def trip_via(self, node: int) -> list[int]:
        net = self.net
        for _ in range(40):
            a, b = self.rng.sample(net.gates, 2)
            into, out = net.path_between(a, node), net.path_between(node, b)
            if into and out and net.segments[out[0]].to_node != net.segments[into[-1]].from_node:
                return into + out
        return net.random_trip(self.rng)

    def add_bus(
        self, bus_id: str, line: str, route: list[int], leg: int, pos: float, turnaround_leg: int = 0
    ) -> Vehicle:
        """Place a bus on Bandă 1 (lane 0), sliding it back if the spot is taken."""
        seg = self.net.segments[route[leg]]
        bclass, blen, bacc = self._bus_profile(line)
        pos = min(pos, max(0.0, seg.stop_pos - 6.0))
        while pos > 0 and not self._free_at(seg.id, pos, blen, lane=0):
            pos -= 6.0
        bus = Vehicle(
            bus_id, "bus", seg.id, max(pos, 0.0), 0.5 * seg.speed_limit, blen, 1.0,
            route=route, cyclic=True, leg=leg, line=line,
            next_break_at=self.rng.randint(self.cfg.bus_break_every_min, self.cfg.bus_break_every_max),
            turnaround_leg=turnaround_leg, lane=0, lane_visual=0.0,
            vehicle_class=bclass, accel_factor=bacc,
        )  # fmt: skip
        bus.next_seg = self._next_on_route(bus)
        self._insert(self.lane_tracks[seg.id][0], bus)
        self.buses.append(bus)
        return bus

    # -- emergency vehicle dispatch ------------------------------------------
    def dispatch_ambulance(self) -> Vehicle | None:
        """Dispatch an emergency ambulance from one of Iași's hospitals across the urban core."""
        if not self.net.hospitals or not self.net.city_buildings:
            return None
        for _ in range(12):
            hosp = self.rng.choice(self.net.hospitals)
            dest_b = self.rng.choice(self.net.city_buildings)
            dest_seg = self.net.segments[dest_b.seg_id]
            path = self.net.path_between(hosp.node, dest_seg.from_node)
            if not path or len(path) < 3:
                continue
            route = [*path, dest_seg.id]
            first = route[0]
            seg = self.net.segments[first]
            # Prefer fast inner lane on multi-lane boulevards
            lane_i = seg.lanes - 1
            if not self._free_at(first, 6.0, 6.5, lane=lane_i):
                lane_i = 0
                if not self._free_at(first, 6.0, 6.5, lane=lane_i):
                    continue
            dist_m = self.net.route_length_m(route, start_pos=6.0, dest_pos=dest_b.pos_m)
            amb = self._make_car(
                route, 0, 6.0, 0.85 * seg.speed_limit,
                trip_kind="emergency", dest_building_id=dest_b.id, dest_pos=dest_b.pos_m,
                trip_dist_m=dist_m, lane=lane_i,
            )
            self._insert(self.lane_tracks[first][lane_i], amb)
            self.ambulances.append(amb)
            return amb
        return None

    def emergency_approaches(self, window_m: float = 140.0) -> set[int]:
        """Approaches where an active ambulance is within `window_m` of the stop line (for green preemption)."""
        out: set[int] = set()
        for a in self.ambulances:
            if a.rb_node is not None:
                continue
            seg = self.net.segments[a.seg]
            if 0.0 <= seg.stop_pos - a.pos <= window_m:
                out.add(a.seg)
            elif a.next_seg is not None and seg.length - a.pos <= 45.0:
                out.add(a.next_seg)
        return out

    # -- state queries -------------------------------------------------------
    def iter_vehicles(self) -> Iterator[Vehicle]:
        for seg_tracks in self.lane_tracks:
            for track in seg_tracks:
                yield from track
        for ring in self.roundabout_rings.values():
            yield from ring
        yield from self.bay_buses

    @staticmethod
    def _rb_lane_radius(rb: Roundabout, rb_lane_val: float) -> float:
        """Exact ring centerline radius (metres) matching the rendered white roundabout annulus."""
        if rb.ring_lanes <= 1:
            return max(6.0, rb.radius_m + 0.2)
        return max(6.0, rb.radius_m + 0.7 - rb_lane_val * 3.6)

    def vehicle_xy(self, v: Vehicle) -> tuple[float, float, float, float]:
        """Exact world position and unit heading `(x, y, dx, dy)` including multi-lane, roundabouts & bus bays."""
        if v.rb_node is not None:
            rb = self.net.roundabout_by_node[v.rb_node]
            r_eff = self._rb_lane_radius(rb, v.rb_lane_visual)
            ca, sa = math.cos(v.rb_angle), math.sin(v.rb_angle)
            # Counterclockwise unit tangent is (-sin(a), cos(a))
            return rb.x + r_eff * ca, rb.y + r_eff * sa, -sa, ca
        if v.kind == "bus" and v.status != "DRIVING":
            seg = self.net.segments[v.seg]
            loop = self.net.nearest_rond_loop(seg.from_node)
            if loop is not None:
                r_bay = max(5.5, loop.radius_m - 1.4)
                slot_ang = (sum(ord(ch) for ch in v.id) * 1.17) % TWO_PI
                ca, sa = math.cos(slot_ang), math.sin(slot_ang)
                return loop.x + r_bay * ca, loop.y + r_bay * sa, -sa, ca
        seg = self.net.segments[v.seg]
        pos_draw = max(0.0, v.pos - v.length / 2) if v.status == "DRIVING" else 0.0
        # Keep vehicles outside the circular island of any connected roundabout (seamless rim entry/exit)
        rb_from = self.net.roundabout_by_node.get(seg.from_node)
        rb_to = self.net.roundabout_by_node.get(seg.to_node)
        r_start = min(rb_from.radius_m + 2.5, seg.length * 0.35) if rb_from is not None else 0.0
        r_end = min(rb_to.radius_m + 2.5, seg.length * 0.42) if rb_to is not None else 0.0
        if r_start > 0.0 or r_end > 0.0:
            usable = max(1.0, seg.length - r_start - r_end)
            if r_start > 0.0 and seg.length > 3.5 * r_start + r_end:
                if pos_draw < 3.0 * r_start:
                    pos_draw = r_start + pos_draw * (2.0 / 3.0)
                if r_end > 0.0:
                    pos_draw = min(pos_draw, seg.length - r_end)
            else:
                frac = max(0.0, min(1.0, pos_draw / max(1.0, seg.length)))
                pos_draw = r_start + frac * usable
        x, y, dx, dy = self.net.position(seg.id, pos_draw, lane=v.lane_visual)
        if v.in_bus_bay:
            # Recessed right-hand bus bay sits 2.3 m further toward the curb
            x += dy * 2.3
            y -= dx * 2.3
        return x, y, dx, dy

    def density(self, seg_id: int) -> float:
        """Fraction of total lane-length on `seg_id` occupied by vehicles plus standstill gaps, 0..1."""
        gap = self.cfg.min_gap
        seg = self.net.segments[seg_id]
        used = sum(v.length + gap for track in self.lane_tracks[seg_id] for v in track)
        return min(1.0, used / max(1.0, seg.length * seg.lanes))

    def mean_density(self) -> float:
        return sum(self.density(s.id) for s in self.net.segments) / len(self.net.segments)

    @property
    def n_cars(self) -> int:
        return sum(1 for v in self.iter_vehicles() if v.kind == "car")

    @property
    def queued_outside(self) -> int:
        return sum(self.backlog.values())

    # -- incidents & urban double-parking friction ---------------------------
    def start_incident(self, seg_id: int, now: float) -> None:
        """A vehicle stalls in Bandă 1 (lane 0). On 2-3 lane roads, traffic merges around it into Bandă 2!"""
        if seg_id in self.incidents:
            return
        seg = self.net.segments[seg_id]
        blocker = Vehicle(self._new_id("X"), "blocker", seg_id, 0.5 * seg.stop_pos, 0.0, self.cfg.car_length, 0.0, lane=0, lane_visual=0.0)
        track = self.lane_tracks[seg_id][0]
        for _ in range(40):
            clash = next(
                (u for u in track if u.pos - u.length - 1 < blocker.pos and u.pos + 1 > blocker.pos - blocker.length),
                None,
            )
            if clash is None:
                break
            blocker.pos = clash.pos - clash.length - self.cfg.min_gap - 0.5
        self._insert(track, blocker)
        self.incidents[seg_id] = (blocker, now + self.cfg.incident_duration)

    def clear_incident(self, seg_id: int) -> None:
        blocker, _ = self.incidents.pop(seg_id)
        track = self.lane_tracks[seg_id][0]
        if blocker in track:
            track.remove(blocker)

    def _clear_friction_blocker(self, seg_id: int) -> None:
        item = self.friction_blockers.pop(seg_id, None)
        if item is not None:
            blocker, _ = item
            track = self.lane_tracks[seg_id][0]
            if blocker in track:
                track.remove(blocker)

    def _step_urban_friction(self, now: float) -> None:
        for sid, (_, exp) in list(self.friction_blockers.items()):
            if now >= exp:
                self._clear_friction_blocker(sid)
        if not self.urban_friction_enabled or now < self._friction_next_t:
            return
        self._friction_next_t = now + self.rng.uniform(22.0, 40.0)
        if len(self.friction_blockers) >= 7:
            return
        # Spawn a realistic curbside delivery/hazard stop ("5 min pe avarii") ONLY on multi-lane urban streets
        cands = [
            s for s in self.net.segments
            if s.lanes >= 2 and s.length >= 130.0 and s.id in self.net.city_seg_ids
            and s.id not in self.incidents and s.id not in self.friction_blockers
        ]
        if not cands:
            return
        seg = self.rng.choice(cands)
        pos = self.rng.uniform(35.0, max(36.0, seg.stop_pos - 35.0))
        if not self._free_at(seg.id, pos, 5.8, lane=0):
            return
        blocker = Vehicle(
            self._new_id("F"), "blocker", seg.id, pos, 0.0, 5.8, 0.0,
            lane=0, lane_visual=0.0, vehicle_class="van",
        )
        self._insert(self.lane_tracks[seg.id][0], blocker)
        self.friction_blockers[seg.id] = (blocker, now + self.rng.uniform(90.0, 210.0))

    # -- crosswalks & CFR rail crossings -------------------------------------
    def _step_crosswalks_and_rail(self, now: float, dt: float) -> None:
        if self.crosswalks_enabled and self.net.crosswalks:
            for cwid, exp in list(self.active_crosswalks.items()):
                if now >= exp:
                    del self.active_crosswalks[cwid]
            mult = max(0.3, min(2.0, self.demand_scale * self.day_demand_mult))
            for cw in self.net.crosswalks:
                if cw.id not in self.active_crosswalks:
                    if self.rng.random() < cw.rate * mult * dt:
                        self.active_crosswalks[cw.id] = now + self.rng.uniform(4.5, 8.0)
        else:
            self.active_crosswalks.clear()

        self.active_rail_crossings.clear()
        if self.rail_crossings_enabled and self.net.rail_crossings:
            for rc in self.net.rail_crossings:
                phase = (now + rc.offset_s) % rc.period_s
                if phase < rc.closed_s:
                    self.active_rail_crossings.add(rc.seg_id)

    # -- dynamics ------------------------------------------------------------
    def step(
        self,
        now: float,
        dt: float,
        signal_state: Callable[[int], str],
        release_factor: Callable[[int], float],
    ) -> None:
        """Advance one tick across all lanes, roundabouts, bus bays, crosswalks, and spawners."""
        self._release_factor = release_factor
        self._signal_state = signal_state
        for seg_id, (_, expiry) in list(self.incidents.items()):
            if now >= expiry:
                self.clear_incident(seg_id)
        self._step_urban_friction(now)
        self._step_crosswalks_and_rail(now, dt)
        self._step_resting_buses(dt)
        self._step_bay_buses(dt)
        self._step_roundabouts(dt, now)
        self._spawn(dt)

        # Periodic multi-lane MOBIL lane-changing pass (~3.3 Hz)
        self._lc_accum += dt
        if self._lc_accum >= 0.30:
            self._step_lane_changes(self._lc_accum)
            self._lc_accum = 0.0

        cfg = self.cfg
        w_spd, w_acc, w_gap, _ = self._weather_mults()
        base_accel = cfg.accel * w_acc
        hard = cfg.max_brake * (0.85 if self.weather == "rain" else (0.68 if self.weather == "snow" else 1.0))
        comfort = cfg.comfort_brake * w_acc
        base_gap = cfg.min_gap * w_gap
        lc_rate = dt / max(0.2, cfg.lane_change_duration)

        crossings: list[tuple[Segment, Vehicle]] = []
        pending_tgt_load: dict[int, float] = {}
        ratio_sum, ratio_n = 0.0, 0
        for seg in self.net.segments:
            seg_tracks = self.lane_tracks[seg.id]
            if not any(seg_tracks):
                continue
            stop, length = seg.stop_pos, seg.length
            limit_speed = seg.speed_limit * w_spd
            state = signal_state(seg.id)
            grade_sin = seg.grade_pct / 100.0
            seg_stops = self.net.stops_by_seg.get(seg.id) if self.bus_stops_enabled else None
            seg_cws = self.net.crosswalks_by_seg.get(seg.id) if self.crosswalks_enabled else None
            rail_rc = self.net.rail_by_seg.get(seg.id) if (seg.id in self.active_rail_crossings) else None
            has_amb = any(v.vehicle_class == "ambulance" for tr in seg_tracks for v in tr)

            for lane_i, lane in enumerate(seg_tracks):
                if not lane:
                    continue
                prev: Vehicle | None = None
                arrived_idx: list[int] = []
                bay_enter_idx: list[int] = []
                for idx, v in enumerate(lane):
                    if v.kind == "blocker":
                        prev = v
                        continue

                    # Smooth lateral visual interpolation
                    if v.lane_visual != v.lane:
                        diff = v.lane - v.lane_visual
                        if abs(diff) <= lc_rate:
                            v.lane_visual = float(v.lane)
                        else:
                            v.lane_visual += math.copysign(lc_rate, diff)
                    if v.lane_cooldown > 0.0:
                        v.lane_cooldown = max(0.0, v.lane_cooldown - dt)

                    # Active CTP bus stop passenger boarding (non-bay curb stop in lane 0)
                    if v.kind == "bus" and v.stop_dwell > 0.0:
                        v.stop_dwell = max(0.0, v.stop_dwell - dt)
                        v.v = 0.0
                        v.elapsed += dt
                        prev = v
                        continue

                    vfree = limit_speed * v.speed_factor
                    # Realistic hill-grade acceleration penalty (heavier on trucks & buses climbing Copou/Bucium)
                    if grade_sin != 0.0:
                        g_sens = 1.15 if v.vehicle_class == "truck" else (0.85 if v.kind == "bus" else 0.40)
                        v_accel = max(0.35, base_accel * v.accel_factor - 9.81 * grade_sin * g_sens)
                    else:
                        v_accel = base_accel * v.accel_factor

                    # Realistic cornering speed reduction near turns on real city maps
                    if self._is_real_city and v.next_seg is not None and stop - v.pos < 20.0:
                        if seg.to_node in self.net.roundabout_by_node:
                            vfree = min(vfree, cfg.roundabout_speed * w_spd)
                        else:
                            tk = self.net.turn_kind(seg.id, v.next_seg)
                            if tk in ("left", "right"):
                                vfree = min(vfree, 6.4 * w_spd)
                            elif tk == "u":
                                vfree = min(vfree, 4.5 * w_spd)

                    gap = base_gap + v.reaction_gap
                    limit = INF if prev is None else prev.pos - prev.length - gap

                    # Scheduled CTP Bus Stop approach (lane 0)
                    next_stop = None
                    if seg_stops and v.kind == "bus" and lane_i == 0:
                        for st in seg_stops:
                            if st.id != v.last_stop_id and st.pos_m >= v.pos - 2.0:
                                next_stop = st
                                limit = min(limit, st.pos_m)
                                break

                    # Active pedestrian zebra crosswalk pulse (ambulances with sirens pass carefully)
                    if seg_cws and v.vehicle_class != "ambulance":
                        for cw in seg_cws:
                            if cw.id in self.active_crosswalks and v.pos < cw.pos_m - 0.5:
                                # Only stop if vehicle can comfortably stop before the zebra stripes
                                if v.v * v.v / (2.0 * hard) <= cw.pos_m - v.pos + 1.5:
                                    limit = min(limit, cw.pos_m - 1.0)
                                break

                    # Closed CFR railway barrier
                    if rail_rc is not None and v.pos < rail_rc.pos_m - 0.5:
                        limit = min(limit, rail_rc.pos_m - 1.5)

                    # Intersection / Roundabout / Yield stop-line check
                    held_at_line = False
                    if v.pos < stop and (prev is None or prev.pos >= stop):
                        if self._line_closed(v, seg, state, now):
                            limit = min(limit, stop - 0.3)
                            held_at_line = True

                    if held_at_line and stop - v.pos < 12.0 and v.v < 1.0:
                        v.impatience = min(30.0, v.impatience + dt)
                    elif not held_at_line:
                        v.impatience = max(0.0, v.impatience - dt * 2.0)

                    # Cars yield slightly to approaching ambulance in the same lane (lateral pull-over handled in MOBIL)
                    if has_amb and v.vehicle_class != "ambulance" and seg.lanes >= 2 and lane_i == seg.lanes - 1:
                        vfree *= 0.78

                    room = max(0.0, limit - v.pos)
                    v_des = vfree if room == INF else min(vfree, math.sqrt(2.0 * comfort * room))
                    v.v = max(0.0, v.v + max(-hard * dt, min(v_accel * dt, v_des - v.v)))
                    new = v.pos + v.v * dt
                    if new > limit:
                        new = max(v.pos, limit)
                        v.v = (new - v.pos) / dt

                    if v.pos < stop <= new:
                        if v.next_seg is not None and seg.to_node not in self.net.roundabout_by_node:
                            self.in_box.setdefault(v.next_seg, {})[v.id] = seg.id
                        if v.kind == "car" and v.vehicle_class != "ambulance":
                            g = release_factor(seg.id)
                            self.next_release[seg.id] = now + (1.0 / (cfg.sat_flow * g) if 0.0 < g < 1.0 else 0.0)
                    v.pos = new
                    v.elapsed += dt

                    # Check arrival at a scheduled CTP Bus Stop
                    if next_stop is not None and next_stop.pos_m - v.pos <= 1.2 and v.v <= 1.8:
                        v.pos = next_stop.pos_m
                        v.v = 0.0
                        v.last_stop_id = next_stop.id
                        v.stop_dwell = self.rng.uniform(7.0, 14.5) * next_stop.demand
                        self.stop_boardings += 1
                        if next_stop.has_bay:
                            v.in_bus_bay = True
                            bay_enter_idx.append(idx)
                            continue  # pulls off lane 0 into recessed bay!
                        prev = v
                        continue

                    if v.kind == "bus":
                        v.delay += dt * (1.0 - min(1.0, v.v / max(1.0, seg.speed_limit)))
                    else:
                        ratio_sum += min(1.0, v.v / max(1.0, seg.speed_limit))
                        ratio_n += 1
                        # Strict arrival-only despawn for building-destined cars (and emergency ambulances)
                        if (
                            v.dest_pos is not None
                            and v.route is not None
                            and v.leg == len(v.route) - 1
                            and v.pos >= v.dest_pos
                        ):
                            arrived_idx.append(idx)
                            continue
                    prev = v

                # Remove vehicles that pulled into recessed bus bays or arrived at destination buildings
                if bay_enter_idx or arrived_idx:
                    bay_set = set(bay_enter_idx)
                    for idx in sorted(bay_set | set(arrived_idx), reverse=True):
                        veh = lane.pop(idx)
                        if idx in bay_set:
                            self.bay_buses.append(veh)
                        else:
                            self.exited += 1
                            if veh.trip_kind != "emergency":
                                self.building_arrivals += 1
                            self._unregister_car(veh)

                rb_to = self.net.roundabout_by_node.get(seg.to_node)
                if rb_to is not None:
                    # Enter circular roundabout right at the outer rim (never drive into the green center island!)
                    entry_line = min(length, seg.stop_pos + 1.5)
                    while lane and lane[0].pos >= entry_line:
                        cand = lane[0]
                        if self._rb_slot_available(cand, seg, rb_to, now):
                            lane.pop(0)
                            self._enter_roundabout(seg, cand, rb_to, now)
                        else:
                            hold = entry_line - 0.05
                            if len(lane) > 1:
                                hold = max(hold, lane[1].pos + cand.length + 0.05)
                            cand.pos = min(length - 0.05, hold)
                            cand.v = 0.0
                            break
                else:
                    while lane and lane[0].pos >= length:
                        cand = lane[0]
                        if cand.next_seg is not None:
                            tracks_tgt = self.lane_tracks[cand.next_seg]
                            check_l = [0] if cand.kind == "bus" else list(range(len(tracks_tgt)))
                            pending_pen = pending_tgt_load.get(cand.next_seg, 0.0) / max(1, len(check_l))
                            best_room = max(
                                ((0.0 if not tracks_tgt[li] else (tracks_tgt[li][-1].pos - tracks_tgt[li][-1].length - cfg.min_gap)) - pending_pen)
                                for li in check_l
                            )
                            if best_room < -11.0:
                                hold = length - 0.05
                                if len(lane) > 1:
                                    hold = max(hold, lane[1].pos + cand.length + 0.05)
                                cand.pos = min(length - 0.02, hold)
                                cand.v = 0.0
                                break
                            pending_tgt_load[cand.next_seg] = pending_tgt_load.get(cand.next_seg, 0.0) + cand.length + cfg.min_gap
                        crossings.append((seg, lane.pop(0)))

        if ratio_n:
            self.car_speed_ratio += (ratio_sum / ratio_n - self.car_speed_ratio) * dt / 30.0
            self.car_ratio_sum += ratio_sum
            self.car_ratio_n += ratio_n
        for seg, v in crossings:
            self._cross(seg, v, now)

    # -- multi-lane MOBIL overtaking & mandatory turn pre-selection ----------
    def _step_lane_changes(self, dt: float) -> None:
        cfg = self.cfg
        for seg in self.net.segments:
            if seg.lanes <= 1:
                continue
            seg_tracks = self.lane_tracks[seg.id]
            has_amb = any(v.vehicle_class == "ambulance" for tr in seg_tracks for v in tr)
            rb = self.net.roundabout_by_node.get(seg.to_node)
            seg_stops = self.net.stops_by_seg.get(seg.id) if self.bus_stops_enabled else None

            for lane_i in range(seg.lanes):
                track = seg_tracks[lane_i]
                if not track:
                    continue
                for idx in range(len(track) - 1, -1, -1):
                    v = track[idx]
                    if v.kind == "blocker" or v.lane_cooldown > 0.0 or v.stop_dwell > 0.0:
                        continue
                    dist_stop = seg.stop_pos - v.pos
                    if dist_stop < 12.0 or v.pos < 10.0:
                        continue  # don't switch lanes right inside stop-line or junction entry

                    ahead = track[idx - 1] if idx > 0 else None
                    headway = (ahead.pos - ahead.length - v.pos) if ahead is not None else INF
                    vfree = seg.speed_limit * v.speed_factor

                    desired_lane: int | None = None
                    # 1. Ambulance clears right or picks fastest open lane
                    if v.vehicle_class == "ambulance":
                        best_l = max(range(seg.lanes), key=lambda li: self._ahead_gap(seg.id, li, v.pos))
                        if best_l != lane_i:
                            desired_lane = lane_i + (1 if best_l > lane_i else -1)
                    # 2. Cars move right out of inner lane when an ambulance is behind them
                    elif has_amb and lane_i > 0:
                        desired_lane = lane_i - 1
                    # 3. Bus needs Bandă 1 (lane 0) for upcoming CTP stop (unless lane 0 has a blocker right ahead!)
                    elif v.kind == "bus":
                        blocker_ahead = ahead is not None and ahead.kind == "blocker" and headway < 45.0
                        if blocker_ahead and lane_i == 0 and seg.lanes > 1:
                            desired_lane = 1
                        elif lane_i > 0:
                            need_stop = bool(seg_stops and any(st.id != v.last_stop_id and 0 < st.pos_m - v.pos < 140 for st in seg_stops))
                            if need_stop or (ahead is None or headway > 28.0):
                                desired_lane = lane_i - 1
                    else:
                        # 4. Mandatory curb lane for destination building on final leg
                        if v.dest_pos is not None and v.route is not None and v.leg == len(v.route) - 1 and v.dest_pos - v.pos < 120.0:
                            if lane_i > 0:
                                desired_lane = lane_i - 1
                        # 5. Mandatory turn / roundabout HG 130/2023 lane pre-selection (< 120 m)
                        elif dist_stop < 120.0 and v.next_seg is not None:
                            if rb is not None and rb.ring_lanes >= 2:
                                arc = self._roundabout_arc(rb, seg.id, v.next_seg)
                                if arc < 2.2 and lane_i > 0:  # 1st/right exit -> Bandă 1
                                    desired_lane = lane_i - 1
                                elif arc >= 2.85 and lane_i < seg.lanes - 1:  # 3rd+/left exit -> Bandă 2
                                    desired_lane = lane_i + 1
                            else:
                                tk = self.net.turn_kind(seg.id, v.next_seg)
                                if tk == "right" and lane_i > 0:
                                    desired_lane = lane_i - 1
                                elif tk in ("left", "u") and lane_i < seg.lanes - 1:
                                    desired_lane = lane_i + 1
                        # 6. Discretionary MOBIL overtaking (around slower vehicles, stopped buses, or lane-0 blockers)
                        if desired_lane is None and ahead is not None and (
                            ahead.kind == "blocker"
                            or ahead.stop_dwell > 0.0
                            or (headway < 32.0 and ahead.v < vfree * 0.78)
                        ):
                            best_adj = None
                            best_gain = 14.0 if ahead.kind != "blocker" else 4.0
                            for cand in (lane_i + 1, lane_i - 1):
                                if 0 <= cand < seg.lanes:
                                    g = self._ahead_gap(seg.id, cand, v.pos) - headway
                                    if g > best_gain:
                                        best_gain = g
                                        best_adj = cand
                            desired_lane = best_adj

                    if desired_lane is not None and self._lane_change_safe(seg.id, desired_lane, v):
                        track.pop(idx)
                        v.lane = desired_lane
                        v.lane_cooldown = cfg.lane_change_cooldown
                        self._insert(seg_tracks[desired_lane], v)
                        self.lane_changes += 1

    def _ahead_gap(self, seg_id: int, lane_i: int, pos: float) -> float:
        best = INF
        for u in self.lane_tracks[seg_id][lane_i]:
            if u.pos >= pos:
                best = min(best, u.pos - u.length - pos)
        return best

    def _lane_change_safe(self, seg_id: int, target_lane: int, v: Vehicle) -> bool:
        min_g = self.cfg.min_gap
        for u in self.lane_tracks[seg_id][target_lane]:
            if u.pos >= v.pos:
                need_front = min_g + 1.5 + max(0.0, (v.v - u.v) * 0.8)
                if u.pos - u.length - v.pos < need_front:
                    return False
            else:
                need_back = min_g + 2.5 + max(0.0, (u.v - v.v) * 1.0)
                if v.pos - v.length - u.pos < need_back:
                    return False
        return True

    # -- stop-line / yield / roundabout entry rules --------------------------
    def _roundabout_arc(self, rb: Roundabout, in_seg: int, out_seg: int) -> float:
        a_in = rb.arm_angles.get(in_seg, self.net.segments[in_seg].heading_in + math.pi)
        a_out = rb.arm_angles.get(out_seg, self.net.segments[out_seg].heading_out)
        arc = (a_out - a_in) % TWO_PI
        return arc if arc >= 0.45 else arc + TWO_PI

    def _rb_target_lane(self, rb: Roundabout, seg: Segment, v: Vehicle) -> int:
        if rb.ring_lanes <= 1 or v.kind == "bus":
            return 0
        arc = self._roundabout_arc(rb, seg.id, v.next_seg) if v.next_seg is not None else 1.6
        return 1 if arc >= 2.85 else 0

    def _rb_slot_available(self, v: Vehicle, seg: Segment, rb: Roundabout, now: float) -> bool:
        """Real-time physical slot check right at the roundabout rim so two vehicles NEVER stack at the same angle."""
        cfg = self.cfg
        ring = self.roundabout_rings[rb.node]
        max_ring_cap = max(4, int(TWO_PI * rb.radius_m * rb.ring_lanes / 15.0))
        if len(ring) >= max_ring_cap:
            return False
        target_rb_lane = self._rb_target_lane(rb, seg, v)
        min_tf = 0.75 if rb.signalized else max(0.95, cfg.roundabout_tf * 0.55)
        if v.vehicle_class == "ambulance":
            min_tf *= 0.65
        if now - self._rb_lane_entry_t.get((rb.node, target_rb_lane), -99.0) < min_tf:
            return False
        entry_ang = rb.arm_angles.get(seg.id, seg.heading_in + math.pi)
        r_target = self._rb_lane_radius(rb, float(target_rb_lane))
        for u in ring:
            if u.rb_lane != target_rb_lane:
                continue
            ang_ahead = (u.rb_angle - entry_ang) % TWO_PI
            ang_behind = (entry_ang - u.rb_angle) % TWO_PI
            if ang_ahead * r_target < u.length + cfg.min_gap + 2.5:
                return False
            if ang_behind * r_target < v.length + cfg.min_gap + 2.5:
                return False
        return True

    def _roundabout_entry_closed(self, v: Vehicle, seg: Segment, rb: Roundabout, state: str, now: float) -> bool:
        """Romanian OUG 195/2002 + HG 130/2023 roundabout entry check."""
        cfg = self.cfg
        # Signalized mega-roundabouts (Podu Roș, Podu de Piatră) obey entry traffic light first
        if rb.signalized:
            if state == "R":
                return True
            dist = seg.stop_pos - v.pos
            if state == "Y" and v.v * v.v / (2.0 * cfg.max_brake) <= dist:
                return True
            if v.kind == "car" and v.vehicle_class != "ambulance":
                g = self._release_factor(seg.id)
                if g <= 0.0:
                    return True
                brake_dist = v.v * v.v / (2.0 * cfg.comfort_brake)
                if g < 1.0 and now < self.next_release[seg.id] and dist < max(15.0, brake_dist + 5.0):
                    return True

        # Prevent exit/ring spillback: if ring already near capacity, wait outside
        ring = self.roundabout_rings[rb.node]
        max_ring_cap = max(4, int(TWO_PI * rb.radius_m * rb.ring_lanes / 15.0))
        if len(ring) >= max_ring_cap:
            return True

        # Don't enter roundabout if our target exit street is completely backed up right at the rim
        if v.next_seg is not None and v.impatience < 5.0:
            exit_tracks = [self.lane_tracks[v.next_seg][0]] if v.kind == "bus" else self.lane_tracks[v.next_seg]
            if all(tr and tr[-1].pos - tr[-1].length < 1.5 for tr in exit_tracks):
                return True

        target_rb_lane = self._rb_target_lane(rb, seg, v)
        # Follow-up headway (Tf) between successive entries into the same ring lane
        tf_need = (0.80 if rb.signalized else cfg.roundabout_tf) * (0.6 if v.vehicle_class == "ambulance" else 1.0)
        if now - self._rb_lane_entry_t.get((rb.node, target_rb_lane), -99.0) < tf_need:
            return True
        if not rb.signalized:
            last_t = self._rb_last_entry.get(seg.id, -99.0)
            if now - last_t < tf_need * 0.85:
                return True

        # Yield-to-the-left gap acceptance on circulating ring vehicles
        entry_ang = rb.arm_angles.get(seg.id, seg.heading_in + math.pi)
        _, _, w_gap, _ = self._weather_mults()
        base_tc = (
            1.8 if v.vehicle_class == "ambulance"
            else (cfg.roundabout_tc_bus if (v.kind == "bus" or v.vehicle_class == "truck") else cfg.roundabout_tc_car)
        ) * w_gap
        # Signalized roundabouts protect entry arms with lights, so only check immediate physical clearance
        if rb.signalized:
            tc = 1.4
        else:
            # Driver impatience gently lowers Tc over wait time (prevents infinite starvation in heavy rush hour)
            tc = max(1.9, base_tc - 0.12 * v.impatience)

        for u in ring:
            # Entering Bandă 2 crosses Bandă 1, so check outer lane too when targeting Bandă 2
            if u.rb_lane != target_rb_lane and not (target_rb_lane == 1 and u.rb_lane == 0):
                continue
            r_u = self._rb_lane_radius(rb, float(u.rb_lane))
            ang_behind = (entry_ang - u.rb_angle) % TWO_PI
            # Also check immediately ahead of entry point (physical clearance)
            ang_ahead = (u.rb_angle - entry_ang) % TWO_PI
            if ang_ahead * r_u < u.length + cfg.min_gap + 2.5:
                return True
            if ang_behind < math.pi:
                arc_dist = ang_behind * r_u
                tta = arc_dist / max(u.v, 2.5)
                if tta < tc or arc_dist < u.length + v.length + 3.0:
                    return True
        return False

    def _yield_junction_closed(self, v: Vehicle, seg: Segment) -> bool:
        """Romanian Cedează Trecerea check at unsignalized T-junctions: minor stem yields to main road."""
        node = seg.to_node
        minor = self.net.yield_minor_approaches.get(node)
        if not minor or seg.id not in minor:
            return False  # main priority road passes freely!
        if v.vehicle_class == "ambulance":
            return False
        _, _, w_gap, _ = self._weather_mults()
        tc = max(1.8, self.cfg.yield_tc * w_gap - 0.14 * v.impatience)
        for main_sid in self.net.in_segs[node]:
            if main_sid in minor:
                continue
            mseg = self.net.segments[main_sid]
            for track in self.lane_tracks[main_sid]:
                if not track:
                    continue
                lead = track[0]
                if lead.kind == "blocker":
                    continue
                rem = mseg.length - lead.pos
                if rem < 0.0:
                    continue
                tta = rem / max(lead.v, 3.0)
                if tta < tc and rem < 55.0:
                    return True
        return False

    def _left_turn_oncoming_blocked(self, v: Vehicle, seg: Segment) -> bool:
        """Unprotected left-turn yielding to oncoming straight/right traffic during shared green (real city only)."""
        if not self._is_real_city or v.next_seg is None or v.vehicle_class == "ambulance":
            return False
        if v.impatience >= 4.2:
            return False  # clear intersection cleanly at end of phase (Romanian junction clearance behavior)
        if self.net.turn_kind(seg.id, v.next_seg) != "left":
            return False
        opp = self.net.partner.get(seg.id)
        if opp is None:
            return False
        oseg = self.net.segments[opp]
        for track in self.lane_tracks[opp]:
            if not track:
                continue
            lead = track[0]
            if lead.kind == "blocker" or lead.v < 1.5:
                continue
            if lead.next_seg is not None and self.net.turn_kind(opp, lead.next_seg) == "left":
                continue  # two opposite left-turners pass cleanly beside each other
            rem = oseg.length - lead.pos
            if 0.0 <= rem <= 36.0 and rem / max(lead.v, 3.0) < 2.4:
                return True
        return False

    def _line_closed(self, v: Vehicle, seg: Segment, state: str, now: float) -> bool:
        """Must this vehicle hold at the stop/yield line?"""
        rb = self.net.roundabout_by_node.get(seg.to_node)
        if rb is not None:
            return self._roundabout_entry_closed(v, seg, rb, state, now)

        if seg.to_node in self.net.yield_nodes:
            if not self._can_enter(v):
                return True
            return self._yield_junction_closed(v, seg)

        if state == "R":
            return True
        dist = seg.stop_pos - v.pos
        brake_dist = v.v * v.v / (2.0 * self.cfg.comfort_brake)
        if state == "Y" and v.v * v.v / (2.0 * self.cfg.max_brake) <= dist:
            return True
        if not self._can_enter(v):
            return True
        if self._left_turn_oncoming_blocked(v, seg):
            return True
        if v.kind == "car" and v.vehicle_class != "ambulance":
            g = self._release_factor(seg.id)
            if g <= 0.0:
                return True
            if g < 1.0 and now < self.next_release[seg.id] and dist < max(15.0, brake_dist + 5.0):
                return True
        return False

    def _can_enter(self, v: Vehicle) -> bool:
        if v.next_seg is None:
            return True
        other_in_box = sum(1 for vid in self.in_box.get(v.next_seg, ()) if vid != v.id)
        tracks = self.lane_tracks[v.next_seg]
        # On multi-lane exit roads, check if ANY receiving lane (or preferred lane 0 for buses) has room
        check_lanes = [0] if v.kind == "bus" else list(range(len(tracks)))
        arrive = (self.net.segments[v.seg].length - v.pos) / max(v.v, 3.0)
        for li in check_lanes:
            track = tracks[li]
            if not track:
                if other_in_box <= len(tracks):
                    return True
                continue
            tail = track[-1]
            # Reserve realistic space for any other vehicles already crossing the junction box into this edge
            box_reserve = other_in_box * (self.cfg.car_length + self.cfg.min_gap + 2.0)
            need = v.length + self.cfg.min_gap + box_reserve
            if tail.pos - tail.length + 0.5 * tail.v * arrive >= need:
                return True
        return False

    # -- roundabout circular motion & intersection handoff -------------------
    def _enter_roundabout(self, seg: Segment, v: Vehicle, rb: Roundabout, now: float) -> None:
        tgt = v.next_seg
        if tgt is None:
            self.exited += 1
            self._unregister_car(v)
            return
        entry_ang = rb.arm_angles.get(seg.id, seg.heading_in + math.pi)
        exit_ang = rb.arm_angles.get(tgt, self.net.segments[tgt].heading_out)
        arc = self._roundabout_arc(rb, seg.id, tgt)
        rb_lane = self._rb_target_lane(rb, seg, v)

        # Keep v.pos continuous (no teleport!) — it glides to seg.length while circulating on the ring
        v.pos = min(seg.length, v.pos)
        v.v = max(2.5, min(v.v, self.cfg.roundabout_speed))
        v.rb_node = rb.node
        v.rb_lane = rb_lane
        v.rb_lane_visual = float(rb_lane)
        v.rb_angle = entry_ang % TWO_PI
        v.rb_exit_angle = exit_ang % TWO_PI
        v.rb_remaining = arc
        v.rb_target_seg = tgt
        v.impatience = 0.0
        self.roundabout_rings[rb.node].append(v)
        self._rb_last_entry[seg.id] = now
        self._rb_lane_entry_t[(rb.node, rb_lane)] = now
        self.roundabout_entries += 1

    def _step_roundabouts(self, dt: float, now: float) -> None:
        if not self.net.roundabouts:
            return
        cfg = self.cfg
        w_spd, w_acc, w_gap, _ = self._weather_mults()
        max_ring_v = cfg.roundabout_speed * w_spd
        accel = cfg.accel * w_acc
        comfort = cfg.comfort_brake * w_acc
        hard = cfg.max_brake
        gap_m = cfg.min_gap * w_gap
        lc_rate = dt / 0.85

        for rb in self.net.roundabouts:
            ring = self.roundabout_rings[rb.node]
            if not ring:
                continue
            exiting: list[Vehicle] = []
            exiting_tgts: set[int] = set()
            for v_idx, v in enumerate(ring):
                if v.rb_lane_visual != v.rb_lane:
                    diff = v.rb_lane - v.rb_lane_visual
                    v.rb_lane_visual = float(v.rb_lane) if abs(diff) <= lc_rate else v.rb_lane_visual + math.copysign(lc_rate, diff)

                seg = self.net.segments[v.seg]
                r_eff = self._rb_lane_radius(rb, float(v.rb_lane))

                # Circular car-following with strict total-order tie-breaker (zero mutual angle deadlock!)
                ahead_dist = INF
                for u_idx, u in enumerate(ring):
                    if u is v or u.rb_lane != v.rb_lane:
                        continue
                    da = (u.rb_angle - v.rb_angle) % TWO_PI
                    if da < 1e-4 or da > TWO_PI - 1e-4:
                        # Exact same angle: earlier vehicle in ring pulls ahead cleanly, later yields
                        if u_idx < v_idx:
                            ahead_dist = 0.0
                        continue
                    if da > math.pi * 1.5:
                        continue  # behind us
                    d_m = da * r_eff - u.length - gap_m
                    if d_m < ahead_dist:
                        ahead_dist = max(0.0, d_m)

                # HG 130/2023 Multi-Lane Exit Rule: Bandă 2 (inner) MUST yield to Bandă 1 (outer) before exiting!
                if v.rb_lane == 1 and v.rb_remaining <= 1.45:
                    outer_busy = any(
                        u is not v
                        and u.rb_lane == 0
                        and min((u.rb_angle - v.rb_angle) % TWO_PI, (v.rb_angle - u.rb_angle) % TWO_PI) * rb.radius_m
                        < (u.length + v.length) * 0.75 + 4.5
                        for u in ring
                    )
                    if not outer_busy:
                        v.rb_lane = 0  # safe lane change into Bandă 1 before exit!
                    elif v.rb_remaining <= 0.40:
                        # Yield smoothly in Bandă 2 without stopping dead; if exit passes while Bandă 1 is full,
                        # either exit into Bandă 2 of a 2+ lane exit or take a realistic extra Romanian roundabout loop!
                        ahead_dist = min(ahead_dist, 3.0)
                        if v.rb_remaining <= 0.10:
                            tgt = v.rb_target_seg
                            can_inner_exit = (
                                tgt is not None
                                and self.net.segments[tgt].lanes >= 2
                                and (
                                    not self.lane_tracks[tgt][1]
                                    or self.lane_tracks[tgt][1][-1].pos - self.lane_tracks[tgt][1][-1].length >= 2.0
                                )
                            )
                            if can_inner_exit:
                                v.lane = 1
                                v.rb_remaining = 0.0
                            else:
                                # Legal Romanian HG 130/2023 extra loop rather than blocking the inner ring!
                                v.rb_remaining += TWO_PI

                vfree = min(max_ring_v * v.speed_factor, seg.speed_limit * w_spd)
                v_des = vfree if ahead_dist == INF else min(vfree, math.sqrt(2.0 * comfort * max(0.0, ahead_dist)))
                v.v = max(0.0, v.v + max(-hard * dt, min(accel * v.accel_factor * dt, v_des - v.v)))
                darc = (v.v * dt) / r_eff
                v.rb_angle = (v.rb_angle + darc) % TWO_PI
                v.rb_remaining -= darc
                # Advance monotonic polyline pos toward seg.length so handoff never jumps
                if v.pos < seg.length:
                    v.pos = min(seg.length, v.pos + min(seg.speed_limit, max(v.v, 10.0)) * dt)
                v.elapsed += dt
                if v.kind == "bus":
                    v.delay += dt * (1.0 - min(1.0, v.v / max(1.0, seg.speed_limit)))

                tgt = v.rb_target_seg
                ready_lane = v.rb_lane == 0 or rb.ring_lanes == 1 or (
                    tgt is not None and self.net.segments[tgt].lanes >= 2 and v.lane >= 1
                )
                if v.rb_remaining <= 0.0 and ready_lane:
                    # Ensure v.pos reached seg.length for strict polyline continuity
                    if v.pos < seg.length - 0.5:
                        v.rb_remaining = 0.0
                        continue
                    exit_clear = tgt is None or (
                        tgt not in exiting_tgts
                        and any(
                            not tr or tr[-1].pos - tr[-1].length - cfg.min_gap >= (-2.0 if v.impatience > 2.0 else 0.0)
                            for tr in (
                                [self.lane_tracks[tgt][0]] if v.kind == "bus" else self.lane_tracks[tgt]
                            )
                        )
                    )
                    if exit_clear:
                        v.pos = seg.length
                        exiting.append(v)
                        if tgt is not None:
                            exiting_tgts.add(tgt)
                    else:
                        v.rb_remaining = 0.0
                        v.v = 0.0
                        v.impatience = min(15.0, v.impatience + dt)

            for v in exiting:
                ring.remove(v)
                tgt = v.rb_target_seg
                seg = self.net.segments[v.seg]
                v.rb_node = None
                v.rb_target_seg = None
                v.rb_remaining = 0.0
                v.impatience = 0.0
                self._handoff_to_next(seg, v, tgt)

    def _cross(self, seg: Segment, v: Vehicle, now: float) -> None:
        """The vehicle has reached the end of `seg`: enter roundabout ring or hand off to next segment."""
        tgt = v.next_seg
        if tgt is None:
            self.exited += 1
            self._unregister_car(v)
            return
        rb = self.net.roundabout_by_node.get(seg.to_node)
        if rb is not None:
            self._enter_roundabout(seg, v, rb, now)
            return
        self._handoff_to_next(seg, v, tgt)

    def _choose_receiving_lane(self, tgt: int, v: Vehicle) -> int:
        n_lanes = self.net.segments[tgt].lanes
        if n_lanes <= 1 or v.kind == "bus":
            return 0
        # Prefer matching relative lane if clear; otherwise pick lane with most open tail room
        pref = min(v.lane, n_lanes - 1)
        best_l = pref
        best_room = -INF
        for li in range(n_lanes):
            track = self.lane_tracks[tgt][li]
            room = INF if not track else (track[-1].pos - track[-1].length)
            if li == pref and room >= v.length + self.cfg.min_gap + 2.0:
                return pref
            if room > best_room:
                best_room = room
                best_l = li
        return best_l

    def _handoff_to_next(self, seg: Segment, v: Vehicle, tgt: int | None) -> None:
        if tgt is None:
            self.exited += 1
            self._unregister_car(v)
            return
        self.in_box.get(tgt, {}).pop(v.id, None)
        finished_leg = v.leg
        v.leg += 1
        wrapped = False
        if v.cyclic and v.route is not None and v.leg >= len(v.route):
            v.leg = 0
            v.laps += 1
            wrapped = True
        v.next_seg = self._next_on_route(v)
        v.last_stop_id = -1

        # Realistic terminal layover (5-15 min) & periodic driver break (15-30 min) instead of despawning
        if (
            v.kind == "bus"
            and v.cyclic
            and self.cfg.bus_layovers
            and self._is_real_city
            and (wrapped or (v.turnaround_leg > 0 and finished_leg == v.turnaround_leg))
        ):
            v.seg = tgt
            v.pos = 0.0
            v.v = 0.0
            v.lane = 0
            v.lane_visual = 0.0
            v.stops_since_break += 1
            if v.stops_since_break >= v.next_break_at:
                v.status = "DRIVER_BREAK"
                v.rest_remaining = self.rng.uniform(self.cfg.bus_break_min, self.cfg.bus_break_max)
                v.stops_since_break = 0
                v.next_break_at = self.rng.randint(self.cfg.bus_break_every_min, self.cfg.bus_break_every_max)
            else:
                v.status = "LAYOVER"
                v.rest_remaining = self.rng.uniform(self.cfg.bus_layover_min, self.cfg.bus_layover_max)
            self.resting_buses.append(v)
            for cb in self.on_bus_rest:
                cb(v.id)
            return

        recv_lane = self._choose_receiving_lane(tgt, v)
        v.seg = tgt
        v.pos = max(0.0, v.pos - seg.length)
        v.lane = recv_lane
        v.lane_visual = float(recv_lane)
        track = self.lane_tracks[tgt][recv_lane]
        if track:
            ahead = track[-1]
            room = ahead.pos - ahead.length - self.cfg.min_gap
            if v.pos > room:
                v.pos, v.v = room, min(v.v, ahead.v)
        track.append(v)

    def _step_resting_buses(self, dt: float) -> None:
        if not self.resting_buses:
            return
        still_resting: list[Vehicle] = []
        for v in self.resting_buses:
            v.rest_remaining = max(0.0, v.rest_remaining - dt)
            if v.rest_remaining <= 0.0:
                track = self.lane_tracks[v.seg][0]
                if not track or track[-1].pos - track[-1].length >= v.length + self.cfg.min_gap + 1.0:
                    v.status = "DRIVING"
                    v.rest_remaining = 0.0
                    v.pos = 0.0
                    v.lane = 0
                    v.lane_visual = 0.0
                    v.v = 0.5 * self.net.segments[v.seg].speed_limit
                    track.append(v)
                    continue
            still_resting.append(v)
        self.resting_buses = still_resting

    def _step_bay_buses(self, dt: float) -> None:
        """Count down passenger boarding in recessed bus bays and merge buses safely back into Bandă 1."""
        if not self.bay_buses:
            return
        still_in_bay: list[Vehicle] = []
        for v in self.bay_buses:
            v.stop_dwell = max(0.0, v.stop_dwell - dt)
            v.elapsed += dt
            if v.stop_dwell <= 0.0 and self._free_at(v.seg, v.pos, v.length, lane=0):
                v.in_bus_bay = False
                v.lane = 0
                v.lane_visual = 0.0
                v.v = 0.25 * self.net.segments[v.seg].speed_limit
                self._insert(self.lane_tracks[v.seg][0], v)
                continue
            still_in_bay.append(v)
        self.bay_buses = still_in_bay

    # -- spawning (Wave 2 residential OD + cross-city / grid gate-to-gate) ---
    def _spawn(self, dt: float) -> None:
        if self.net.buildings:
            self._spawn_wave2(dt)
            return
        cfg = self.cfg
        p = 1.0 - math.exp(-cfg.gate_demand * self.demand_scale * dt)
        for gate in self.net.gates:
            if self.rng.random() < p:
                if self.backlog[gate] < cfg.spawn_backlog_max:
                    self.backlog[gate] += 1
                else:
                    self.dropped += 1
            if self.backlog[gate]:
                self._try_spawn(gate)
        if self.rush_node is not None and cfg.rush_extra > 0:
            p = 1.0 - math.exp(-cfg.gate_demand * cfg.rush_extra * self.demand_scale * dt)
            for gate in self.net.gates:
                if self.rng.random() < p:
                    if self.rush_backlog[gate] < cfg.spawn_backlog_max:
                        self.rush_backlog[gate] += 1
                    else:
                        self.dropped += 1
                if self.rush_backlog[gate]:
                    self._try_spawn(gate, self.rush_backlog, self.rush_node)

    def _next_needed_trip_kind(self) -> str:
        tot = max(1, sum(self.active_by_kind.values()))
        targets = (
            ("city_to_building", self.cfg.share_city_to_building),
            ("outer_to_building", self.cfg.share_outer_to_building),
            ("through_city", self.cfg.share_through_city),
        )
        return max(targets, key=lambda kv: kv[1] - self.active_by_kind[kv[0]] / tot)[0]

    def _spawn_wave2(self, dt: float) -> None:
        if self.demand_scale <= 0.0:
            return
        cfg = self.cfg
        _, _, _, w_dem = self._weather_mults()
        eff_scale = self.demand_scale * self.day_demand_mult * w_dem
        target_cars = round(cfg.initial_cars * eff_scale)
        active_cars = sum(self.active_by_kind.values())
        deficit = target_cars - active_cars

        attempts = 0
        if deficit > 0:
            rate = max(1.5, deficit * 0.65)
            p = 1.0 - math.exp(-rate * dt)
            attempts = min(deficit, int(rate * dt) + (1 if self.rng.random() < p else 0))

        for _ in range(attempts):
            kind = self._next_needed_trip_kind()
            if kind == "city_to_building":
                self._try_spawn_from_building("city")
            elif kind == "outer_to_building":
                if self._outer_bldg_pool and self.rng.random() < 0.5:
                    if not self._try_spawn_from_building("outer"):
                        self._try_spawn_gate_to_building()
                else:
                    if not self._try_spawn_gate_to_building():
                        self._try_spawn_from_building("outer")
            else:
                self._try_spawn_through_city()

        if self.rush_node is not None and cfg.rush_extra > 0:
            p = 1.0 - math.exp(-cfg.gate_demand * cfg.rush_extra * eff_scale * dt)
            for gate in self.net.gates:
                if self.rng.random() < p:
                    if self.rush_backlog[gate] < cfg.spawn_backlog_max:
                        self.rush_backlog[gate] += 1
                    else:
                        self.dropped += 1
                if self.rush_backlog[gate]:
                    self._try_spawn(gate, self.rush_backlog, self.rush_node)

    def _try_spawn_from_building(self, zone: str) -> bool:
        pool = self._city_bldg_pool if zone == "city" else self._outer_bldg_pool
        if not pool:
            return False
        cfg = self.cfg
        min_m = cfg.city_trip_min_m if zone == "city" else cfg.outer_trip_min_m
        max_m = cfg.city_trip_max_m if zone == "city" else cfg.outer_trip_max_m
        kind = "city_to_building" if zone == "city" else "outer_to_building"

        for _ in range(16):
            orig = self.rng.choice(pool)
            if not self._can_spawn_at_curb(orig.seg_id, orig.pos_m):
                continue
            res = self.net.building_to_building_trip(orig, self.rng, min_m, max_m)
            if not res:
                continue
            route, dest, dist_m = res
            seg = self.net.segments[orig.seg_id]
            car = self._make_car(
                route, 0, orig.pos_m, 0.4 * seg.speed_limit,
                trip_kind=kind, dest_building_id=dest.id, dest_pos=dest.pos_m, trip_dist_m=dist_m, lane=0,
            )  # fmt: skip
            if not self._free_at(orig.seg_id, orig.pos_m, car.length, lane=0):
                continue
            self._insert(self.lane_tracks[orig.seg_id][0], car)
            self._register_car(car)
            self.residential_spawns += 1
            return True
        return False

    def _try_spawn_gate_to_building(self) -> bool:
        if not self.net.gates:
            return False
        cfg = self.cfg
        for _ in range(10):
            gate = self.rng.choice(self.net.gates)
            res = self.net.gate_to_building_trip(gate, self.rng, cfg.outer_trip_min_m, cfg.outer_trip_max_m)
            if not res:
                continue
            route, dest, dist_m = res
            first = route[0]
            if self.in_box.get(first) or self.density(first) >= 0.45:
                continue
            seg = self.net.segments[first]
            for li in range(seg.lanes):
                track = self.lane_tracks[first][li]
                if not track or track[-1].pos - track[-1].length >= 11.5 + cfg.min_gap + 2.0:
                    car = self._make_car(
                        route, 0, 0.0, 0.7 * seg.speed_limit,
                        trip_kind="outer_to_building", dest_building_id=dest.id, dest_pos=dest.pos_m,
                        trip_dist_m=dist_m, lane=li,
                    )  # fmt: skip
                    track.append(car)
                    self._register_car(car)
                    return True
        return False

    def _try_spawn_through_city(self) -> bool:
        cfg = self.cfg
        for _ in range(10):
            res = self.net.through_city_trip(self.rng)
            if not res:
                return False
            route, dist_m = res
            first = route[0]
            if self.in_box.get(first) or self.density(first) >= 0.45:
                continue
            seg = self.net.segments[first]
            for li in range(seg.lanes):
                track = self.lane_tracks[first][li]
                if not track or track[-1].pos - track[-1].length >= 11.5 + cfg.min_gap + 2.0:
                    car = self._make_car(
                        route, 0, 0.0, 0.7 * seg.speed_limit,
                        trip_kind="through_city", trip_dist_m=dist_m, lane=li,
                    )  # fmt: skip
                    track.append(car)
                    self._register_car(car)
                    return True
        return False

    def _try_spawn(self, gate: int, backlog: dict[int, int] | None = None, via: int | None = None) -> None:
        backlog = self.backlog if backlog is None else backlog
        if via is None:
            for _ in range(4):
                dest = self.rng.choice([g for g in self.net.gates if g != gate])
                path = self.net.path_between(gate, dest)
                if path:
                    break
            else:
                return
        else:
            path = self._path_via(gate, via)
            if not path:
                return
        first = path[0]
        if self.in_box.get(first):
            return
        seg = self.net.segments[first]
        for li in range(seg.lanes):
            track = self.lane_tracks[first][li]
            if not track or track[-1].pos - track[-1].length >= self.cfg.car_length + self.cfg.min_gap + 2.0:
                backlog[gate] -= 1
                car = self._make_car(path, 0, 0.0, 0.7 * seg.speed_limit, trip_kind="through_city", lane=li)
                track.append(car)
                self._register_car(car)
                return

    def _path_via(self, gate: int, via: int) -> list[int] | None:
        net = self.net
        into = net.path_between(gate, via)
        if not into:
            return None
        for _ in range(6):
            out = net.path_between(via, self.rng.choice([g for g in net.gates if g != gate]))
            if out and net.segments[out[0]].to_node != net.segments[into[-1]].from_node:
                return into + out
        return None

    # -- runtime traffic controls --------------------------------------------
    def remove_vehicle(self, v: Vehicle) -> None:
        removed = False
        if v.rb_node is not None and v in self.roundabout_rings.get(v.rb_node, ()):
            self.roundabout_rings[v.rb_node].remove(v)
            removed = True
        for track in self.lane_tracks[v.seg]:
            if v in track:
                track.remove(v)
                removed = True
        if v in self.bay_buses:
            self.bay_buses.remove(v)
            removed = True
        if removed:
            self._unregister_car(v)
        if v.next_seg is not None:
            self.in_box.get(v.next_seg, {}).pop(v.id, None)
        if v in self.resting_buses:
            self.resting_buses.remove(v)
        if v in self.buses:
            self.buses.remove(v)

    def try_place_bus(
        self, bus_id: str, line: str, route: list[int], leg: int, pos: float, turnaround_leg: int = 0
    ) -> Vehicle | None:
        seg = self.net.segments[route[leg]]
        bclass, blen, bacc = self._bus_profile(line)
        pos = min(pos, max(0.0, seg.stop_pos - 6.0))
        if not self._free_at(seg.id, pos, blen, lane=0):
            return None
        bus = Vehicle(
            bus_id, "bus", seg.id, pos, 0.5 * seg.speed_limit, blen, 1.0,
            route=route, cyclic=True, leg=leg, line=line,
            next_break_at=self.rng.randint(self.cfg.bus_break_every_min, self.cfg.bus_break_every_max),
            turnaround_leg=turnaround_leg, lane=0, lane_visual=0.0,
            vehicle_class=bclass, accel_factor=bacc,
        )  # fmt: skip
        bus.next_seg = self._next_on_route(bus)
        self._insert(self.lane_tracks[seg.id][0], bus)
        self.buses.append(bus)
        return bus

    def set_car_percent(self, new: float, old: float) -> None:
        cfg = self.cfg
        self.demand_scale = new / 100.0
        cars = [v for v in self.iter_vehicles() if v.kind == "car"]
        if new <= 0:
            for v in cars:
                self.remove_vehicle(v)
            for g in self.backlog:
                self.backlog[g] = self.rush_backlog[g] = 0
        elif old <= 0:
            self.scatter_cars(round(cfg.initial_cars * new / 100.0))
        elif new < old:
            keep = new / old
            for v in cars:
                if self.rng.random() > keep:
                    self.remove_vehicle(v)
            for g in self.backlog:
                self.backlog[g] = int(self.backlog[g] * keep)
                self.rush_backlog[g] = int(self.rush_backlog[g] * keep)
        elif new > old:
            self.scatter_cars(round(len(cars) * (new / old - 1.0)))

    def set_rush_node(self, node: int | None) -> None:
        self.rush_node = node
        for g in self.rush_backlog:
            self.rush_backlog[g] = 0
        if node is not None:
            self.scatter_cars(round(self.cfg.rush_seed * self.demand_scale), via=node)
