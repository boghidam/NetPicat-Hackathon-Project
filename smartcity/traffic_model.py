"""Ground-truth microscopic traffic: individual cars and buses on one-lane roads.

Controllers never read this directly. They only see what the buses report, so the buses
really are the sensors. The world enforces the physical rules:

* a vehicle may not pass the stop line while its light is red, nor on yellow if it can
  stop comfortably, and it never enters the junction box unless there is room to leave it;
* vehicles follow the one ahead at a safe distance and cannot overtake;
* a throttled approach (see the coordinator) releases cars at a reduced rate.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .config import Config
from .network import CityGrid, Segment

INF = math.inf


@dataclass
class Vehicle:
    id: str
    kind: str  # "car", "bus" or "blocker" (a stalled vehicle used for incidents)
    seg: int
    pos: float  # m from the segment start to the vehicle's front bumper
    v: float
    length: float
    vfree: float
    next_seg: int | None = None  # segment taken after the intersection ahead
    goal: int | None = None  # cars: exit segment they are heading for
    route: list[int] | None = None  # buses: cyclic list of segment ids
    leg: int = 0
    delay: float = 0.0  # seconds beyond the free-flow schedule
    elapsed: float = 0.0
    laps: int = 0


class TrafficModel:
    def __init__(self, grid: CityGrid, cfg: Config, rng: random.Random):
        self.grid, self.cfg, self.rng = grid, cfg, rng
        self.lanes: list[list[Vehicle]] = [[] for _ in grid.segments]  # front vehicle first
        self.buses: list[Vehicle] = []
        self.incidents: dict[int, tuple[Vehicle, float]] = {}  # segment -> (blocker, expiry)
        self.backlog = {sid: 0 for sid in grid.entries}  # cars waiting outside a gate
        self.next_release = [0.0] * len(grid.segments)
        self.exited = 0
        self.dropped = 0
        self.car_speed_ratio = 1.0  # EMA of car speed / free speed
        self._ids = 0
        self._release_factor: Callable[[int], float] = lambda s: 1.0
        self._signal_state: Callable[[int], str] = lambda s: "G"
        # vehicles that have passed a stop line and are crossing the box: target link -> {vehicle id: source link}
        self.in_box: dict[int, dict[str, int]] = {}
        self._seed_cars()

    # -- creation ------------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:03d}"

    def _free_at(self, seg_id: int, pos: float, length: float) -> bool:
        gap = self.cfg.min_gap + 1.0
        return all(abs(u.pos - pos) >= (u.length if u.pos > pos else length) + gap for u in self.lanes[seg_id])

    @staticmethod
    def _insert(lane: list[Vehicle], v: Vehicle) -> None:
        i = 0
        while i < len(lane) and lane[i].pos > v.pos:
            i += 1
        lane.insert(i, v)

    def _make_car(self, seg_id: int, pos: float, speed: float) -> Vehicle:
        cfg = self.cfg
        entry_node = self.grid.segments[seg_id].to_node
        exits = [e for e in self.grid.exits if self.grid.segments[e].from_node != entry_node]
        goal = self.rng.choice(exits)
        v = Vehicle(
            self._new_id("C"),
            "car",
            seg_id,
            pos,
            speed,
            cfg.car_length,
            cfg.free_speed * (1 + self.rng.uniform(-cfg.car_speed_var, cfg.car_speed_var)),
            goal=goal,
        )
        v.next_seg = self.grid.choose_next(seg_id, goal, self.rng, cfg.route_follow)
        return v

    def _seed_cars(self) -> None:
        roads = [s for s in self.grid.segments if s.kind in ("road", "entry")]
        placed = 0
        for _ in range(self.cfg.initial_cars * 6):
            if placed >= self.cfg.initial_cars:
                break
            seg = self.rng.choice(roads)
            pos = self.rng.uniform(10.0, seg.stop_pos - 6.0)
            if self._free_at(seg.id, pos, self.cfg.car_length):
                car = self._make_car(seg.id, pos, 0.6 * self.cfg.free_speed)
                self._insert(self.lanes[seg.id], car)
                placed += 1

    def add_bus(self, bus_id: str, route: list[int], leg: int, pos: float) -> Vehicle:
        """Place a bus on its route, sliding it back if the spot is taken."""
        seg = self.grid.segments[route[leg]]
        pos = min(pos, seg.stop_pos - 6.0)
        while pos > 0 and not self._free_at(seg.id, pos, self.cfg.bus_length):
            pos -= 6.0
        bus = Vehicle(
            bus_id, "bus", seg.id, max(pos, 0.0), 0.5 * self.cfg.free_speed, self.cfg.bus_length, self.cfg.free_speed,
            route=route, leg=leg, next_seg=route[(leg + 1) % len(route)],
        )  # fmt: skip
        self._insert(self.lanes[seg.id], bus)
        self.buses.append(bus)
        return bus

    # -- state queries ---------------------------------------------------------
    def iter_vehicles(self) -> Iterator[Vehicle]:
        for lane in self.lanes:
            yield from lane

    def density(self, seg_id: int) -> float:
        """Fraction of the link's length occupied by vehicles plus standstill gaps, 0..1."""
        gap = self.cfg.min_gap
        used = sum(v.length + gap for v in self.lanes[seg_id])
        return min(1.0, used / self.grid.segments[seg_id].length)

    def mean_density(self) -> float:
        roads = [s.id for s in self.grid.segments if s.kind == "road"]
        return sum(self.density(i) for i in roads) / len(roads)

    @property
    def n_cars(self) -> int:
        return sum(1 for v in self.iter_vehicles() if v.kind == "car")

    @property
    def queued_outside(self) -> int:
        return sum(self.backlog.values())

    # -- incidents ----------------------------------------------------------
    def start_incident(self, seg_id: int, now: float) -> None:
        """A vehicle stalls mid-link. Nobody can overtake on a one-lane road, so the lane is blocked."""
        if seg_id in self.incidents:
            return
        seg = self.grid.segments[seg_id]
        blocker = Vehicle(self._new_id("X"), "blocker", seg_id, 0.5 * seg.stop_pos, 0.0, self.cfg.car_length, 0.0)
        lane = self.lanes[seg_id]
        for _ in range(40):  # slide behind any vehicle that is sitting on the chosen spot
            clash = next(
                (u for u in lane if u.pos - u.length - 1 < blocker.pos and u.pos + 1 > blocker.pos - blocker.length),
                None,
            )
            if clash is None:
                break
            blocker.pos = clash.pos - clash.length - self.cfg.min_gap - 0.5
        self._insert(lane, blocker)
        self.incidents[seg_id] = (blocker, now + self.cfg.incident_duration)

    def clear_incident(self, seg_id: int) -> None:
        blocker, _ = self.incidents.pop(seg_id)
        self.lanes[seg_id].remove(blocker)

    # -- dynamics -----------------------------------------------------------
    def step(
        self,
        now: float,
        dt: float,
        signal_state: Callable[[int], str],
        release_factor: Callable[[int], float],
    ) -> None:
        """Advance one tick. `signal_state(seg)` is 'G', 'Y' or 'R'; `release_factor` throttles cars."""
        self._release_factor = release_factor
        self._signal_state = signal_state
        for seg_id, (_, expiry) in list(self.incidents.items()):
            if now >= expiry:
                self.clear_incident(seg_id)
        self._spawn(dt)

        cfg = self.cfg
        accel, hard, comfort, gap = cfg.accel, cfg.max_brake, cfg.comfort_brake, cfg.min_gap
        crossings: list[tuple[Segment, Vehicle]] = []
        ratio_sum, ratio_n = 0.0, 0
        for seg in self.grid.segments:
            lane = self.lanes[seg.id]
            if not lane:
                continue
            stop, length = seg.stop_pos, seg.length
            state = signal_state(seg.id) if seg.signalised else "G"
            prev: Vehicle | None = None
            for v in lane:
                if v.kind == "blocker":
                    prev = v
                    continue
                limit = INF if prev is None else prev.pos - prev.length - gap
                if seg.signalised and v.pos < stop and (prev is None or prev.pos >= stop):
                    if self._line_closed(v, seg, state, now):
                        limit = min(limit, stop - 0.3)
                room = max(0.0, limit - v.pos)
                v_des = v.vfree if room == INF else min(v.vfree, math.sqrt(2 * comfort * room))
                v.v = max(0.0, v.v + max(-hard * dt, min(accel * dt, v_des - v.v)))
                new = v.pos + v.v * dt
                if new > limit:
                    new = max(v.pos, limit)
                    v.v = (new - v.pos) / dt
                if seg.signalised and v.pos < stop <= new:
                    if v.next_seg is not None:
                        self.in_box.setdefault(v.next_seg, {})[v.id] = seg.id
                    if v.kind == "car":
                        g = release_factor(seg.id)
                        self.next_release[seg.id] = now + (1.0 / (cfg.sat_flow * g) if 0.0 < g < 1.0 else 0.0)
                v.pos = new
                v.elapsed += dt
                if v.kind == "bus":
                    v.delay += dt * (1.0 - min(1.0, v.v / cfg.free_speed))
                else:
                    ratio_sum += min(1.0, v.v / v.vfree)
                    ratio_n += 1
                prev = v
            while lane and lane[0].pos >= length:
                crossings.append((seg, lane.pop(0)))

        if ratio_n:
            self.car_speed_ratio += (ratio_sum / ratio_n - self.car_speed_ratio) * dt / 30.0
        for seg, v in crossings:
            self._cross(seg, v)

    def _line_closed(self, v: Vehicle, seg: Segment, state: str, now: float) -> bool:
        """Must this vehicle hold at the stop line?"""
        if state == "R":
            return True
        dist = seg.stop_pos - v.pos
        brake_dist = v.v * v.v / (2 * self.cfg.comfort_brake)
        left = self._turns_left(v, seg)
        if state == "Y" and v.v * v.v / (2 * self.cfg.max_brake) <= dist:
            return True  # can still stop, so it must; only vehicles too fast to stop run the yellow
        if not self._can_enter(v):
            return True  # never enter the box without room to leave it
        if left and self._oncoming_conflict(seg):
            return True  # yield to oncoming traffic, which is green at the same time
        if v.kind == "car":
            g = self._release_factor(seg.id)
            if g <= 0.0:
                return True
            if g < 1.0 and now < self.next_release[seg.id] and dist < max(15.0, brake_dist + 5.0):
                return True
        return False

    def _turns_left(self, v: Vehicle, seg: Segment) -> bool:
        return v.next_seg is not None and self.grid.segments[v.next_seg].direction == seg.direction.left()

    def _oncoming_conflict(self, seg: Segment) -> bool:
        """Is oncoming traffic about to enter the box from the opposite approach?"""
        opp_id = self.grid.in_seg[seg.to_node][seg.direction.opposite]
        if opp_id is None:
            return False
        opp = self.grid.segments[opp_id]
        for u in self.lanes[opp_id]:
            if u.kind == "blocker" or u.pos < opp.stop_pos - 25.0:
                continue
            if self._turns_left(u, opp):
                continue  # two left turns pass each other
            if u.pos > opp.stop_pos or u.v > 1.5:  # moving toward the box, or already in it
                return True
        return False

    def _can_enter(self, v: Vehicle) -> bool:
        if v.next_seg is None:
            return True
        # one stream at a time may merge into a link; the box is not shared by two merging streams
        if any(src != v.seg for src in self.in_box.get(v.next_seg, {}).values()):
            return False
        lane = self.lanes[v.next_seg]
        if not lane:
            return True
        tail = lane[-1]
        return tail.pos - tail.length >= v.length + self.cfg.min_gap

    def _cross(self, seg: Segment, v: Vehicle) -> None:
        """The vehicle has reached the intersection centre: hand it to the next segment."""
        if seg.kind == "exit":
            self.exited += 1
            return
        tgt = v.next_seg
        assert tgt is not None
        self.in_box.get(tgt, {}).pop(v.id, None)
        v.seg, v.pos = tgt, v.pos - seg.length
        self.lanes[tgt].append(v)
        if v.route is not None:
            v.leg = (v.leg + 1) % len(v.route)
            if v.leg == 0:
                v.laps += 1
            v.next_seg = v.route[(v.leg + 1) % len(v.route)]
        else:
            assert v.goal is not None
            v.next_seg = self.grid.choose_next(tgt, v.goal, self.rng, self.cfg.route_follow)

    def _spawn(self, dt: float) -> None:
        cfg = self.cfg
        p = 1.0 - math.exp(-cfg.gate_demand * dt)
        for sid in self.grid.entries:
            if self.rng.random() < p:
                if self.backlog[sid] < cfg.spawn_backlog_max:
                    self.backlog[sid] += 1
                else:
                    self.dropped += 1
            if self.backlog[sid] and self._can_spawn(sid):
                self.backlog[sid] -= 1
                self.lanes[sid].append(self._make_car(sid, 0.0, 0.7 * cfg.free_speed))

    def _can_spawn(self, seg_id: int) -> bool:
        lane = self.lanes[seg_id]
        return not lane or lane[-1].pos - lane[-1].length >= self.cfg.car_length + self.cfg.min_gap + 2.0
