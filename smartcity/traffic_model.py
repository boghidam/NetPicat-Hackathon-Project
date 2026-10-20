"""Ground-truth microscopic traffic: individual cars and buses on one-lane roads.

Controllers never read this directly. They only see what the buses report, so the buses
really are the sensors. The world enforces the physical rules (SUMO-style, in plain Python):

* a vehicle may not pass the stop line while its light is red, nor on yellow if it can still
  stop, and it never enters the junction box unless there will be room on the next edge when it
  gets there;
* vehicles follow the one ahead at a safe distance along the edge polyline and cannot overtake,
  so they never overlap and never jump across nodes;
* both streets of a phase pass together: nothing yields to oncoming traffic, and two streams that
  merge into the same edge fall in behind one another when they land;
* a throttled approach (see the coordinator) releases cars at a reduced rate.

Every vehicle follows a route: buses loop forever along their line, cars follow a gate-to-gate
fastest path.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .config import Config
from .network import RoadNetwork, Segment

INF = math.inf


@dataclass
class Vehicle:
    id: str
    kind: str  # "car", "bus" or "blocker" (a stalled vehicle used for incidents)
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


class TrafficModel:
    def __init__(self, net: RoadNetwork, cfg: Config, rng: random.Random):
        self.net, self.cfg, self.rng = net, cfg, rng
        self.lanes: list[list[Vehicle]] = [[] for _ in net.segments]  # front vehicle first
        self.buses: list[Vehicle] = []
        self.incidents: dict[int, tuple[Vehicle, float]] = {}  # segment -> (blocker, expiry)
        self.backlog = {g: 0 for g in net.gates}  # cars waiting outside a gate
        self.demand_scale = 1.0  # runtime traffic amount: 1.0 is normal (100%), 0 spawns nothing
        self.rush_node: int | None = None  # key point under rush-hour pressure
        self.rush_backlog = {g: 0 for g in net.gates}
        self.next_release = [0.0] * len(net.segments)
        self.exited = 0
        self.dropped = 0
        self.car_speed_ratio = 1.0  # EMA of car speed / speed limit
        self.car_ratio_sum = 0.0  # car speed / speed limit, summed over every car on every tick
        self.car_ratio_n = 0  # car-ticks in that sum: their ratio is the mean car speed over the whole run
        self._ids = 0
        self._release_factor: Callable[[int], float] = lambda s: 1.0
        self._signal_state: Callable[[int], str] = lambda s: "G"
        # vehicles that have passed a stop line and are crossing the box (used so that cars are not spawned into an
        # edge that traffic is entering): target edge -> {vehicle id: source edge}
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

    @staticmethod
    def _next_on_route(v: Vehicle) -> int | None:
        assert v.route is not None
        if v.cyclic:
            return v.route[(v.leg + 1) % len(v.route)]
        return v.route[v.leg + 1] if v.leg + 1 < len(v.route) else None

    def _make_car(self, route: list[int], leg: int, pos: float, speed: float) -> Vehicle:
        cfg = self.cfg
        factor = 1.0 + self.rng.uniform(-cfg.car_speed_var, cfg.car_speed_var)
        car = Vehicle(self._new_id("C"), "car", route[leg], pos, speed, cfg.car_length, factor, route=route, leg=leg)
        car.next_seg = self._next_on_route(car)
        return car

    def _seed_cars(self) -> None:
        self.scatter_cars(self.cfg.initial_cars)

    def scatter_cars(self, n: int, via: int | None = None) -> int:
        """Place up to `n` cars at random free spots along gate-to-gate trips (through node `via` if given)."""
        if not self.net.gates:
            return 0
        placed = 0
        for _ in range(n * 6):
            if placed >= n:
                break
            route = self.net.random_trip(self.rng) if via is None else self.trip_via(via)
            leg = self.rng.randrange(len(route)) if via is None else self._leg_near(route, via)
            seg = self.net.segments[route[leg]]
            pos = self.rng.uniform(5.0, max(6.0, seg.stop_pos - 4.0))
            if self._free_at(seg.id, pos, self.cfg.car_length):
                car = self._make_car(route, leg, pos, 0.6 * seg.speed_limit)
                self._insert(self.lanes[seg.id], car)
                placed += 1
        return placed

    def _leg_near(self, route: list[int], node: int) -> int:
        """A random leg of a trip that passes `node`, within `rush_reach` metres of it: the build-up around a
        key point (trips on the whole city are long, so cars spread along their entire length would hardly
        load the junction)."""
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
        """A gate-to-gate path that goes through `node` (a rush-hour commute), without a U-turn there."""
        net = self.net
        for _ in range(40):  # many gate pairs have the key point "behind" them, which would need a U-turn there
            a, b = self.rng.sample(net.gates, 2)
            into, out = net.path_between(a, node), net.path_between(node, b)
            if into and out and net.segments[out[0]].to_node != net.segments[into[-1]].from_node:
                return into + out
        return net.random_trip(self.rng)

    def add_bus(self, bus_id: str, line: str, route: list[int], leg: int, pos: float) -> Vehicle:
        """Place a bus on its route, sliding it back if the spot is taken."""
        seg = self.net.segments[route[leg]]
        pos = min(pos, max(0.0, seg.stop_pos - 6.0))
        while pos > 0 and not self._free_at(seg.id, pos, self.cfg.bus_length):
            pos -= 6.0
        bus = Vehicle(
            bus_id, "bus", seg.id, max(pos, 0.0), 0.5 * seg.speed_limit, self.cfg.bus_length, 1.0,
            route=route, cyclic=True, leg=leg, line=line,
        )  # fmt: skip
        bus.next_seg = self._next_on_route(bus)
        self._insert(self.lanes[seg.id], bus)
        self.buses.append(bus)
        return bus

    # -- state queries ---------------------------------------------------------
    def iter_vehicles(self) -> Iterator[Vehicle]:
        for lane in self.lanes:
            yield from lane

    def density(self, seg_id: int) -> float:
        """Fraction of the edge's length occupied by vehicles plus standstill gaps, 0..1."""
        gap = self.cfg.min_gap
        used = sum(v.length + gap for v in self.lanes[seg_id])
        return min(1.0, used / self.net.segments[seg_id].length)

    def mean_density(self) -> float:
        return sum(self.density(s.id) for s in self.net.segments) / len(self.net.segments)

    @property
    def n_cars(self) -> int:
        return sum(1 for v in self.iter_vehicles() if v.kind == "car")

    @property
    def queued_outside(self) -> int:
        return sum(self.backlog.values())

    # -- incidents ----------------------------------------------------------
    def start_incident(self, seg_id: int, now: float) -> None:
        """A vehicle stalls mid-edge. Nobody can overtake on a one-lane road, so the lane is blocked."""
        if seg_id in self.incidents:
            return
        seg = self.net.segments[seg_id]
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
        for seg in self.net.segments:
            lane = self.lanes[seg.id]
            if not lane:
                continue
            stop, length, limit_speed = seg.stop_pos, seg.length, seg.speed_limit
            state = signal_state(seg.id)
            prev: Vehicle | None = None
            for v in lane:
                if v.kind == "blocker":
                    prev = v
                    continue
                vfree = limit_speed * v.speed_factor
                limit = INF if prev is None else prev.pos - prev.length - gap
                if v.pos < stop and (prev is None or prev.pos >= stop):
                    if self._line_closed(v, seg, state, now):
                        limit = min(limit, stop - 0.3)
                room = max(0.0, limit - v.pos)
                v_des = vfree if room == INF else min(vfree, math.sqrt(2 * comfort * room))
                v.v = max(0.0, v.v + max(-hard * dt, min(accel * dt, v_des - v.v)))
                new = v.pos + v.v * dt
                if new > limit:
                    new = max(v.pos, limit)
                    v.v = (new - v.pos) / dt
                if v.pos < stop <= new:
                    if v.next_seg is not None:
                        self.in_box.setdefault(v.next_seg, {})[v.id] = seg.id
                    if v.kind == "car":
                        g = release_factor(seg.id)
                        self.next_release[seg.id] = now + (1.0 / (cfg.sat_flow * g) if 0.0 < g < 1.0 else 0.0)
                v.pos = new
                v.elapsed += dt
                if v.kind == "bus":
                    v.delay += dt * (1.0 - min(1.0, v.v / limit_speed))
                else:
                    ratio_sum += min(1.0, v.v / limit_speed)
                    ratio_n += 1
                prev = v
            while lane and lane[0].pos >= length:
                crossings.append((seg, lane.pop(0)))

        if ratio_n:
            self.car_speed_ratio += (ratio_sum / ratio_n - self.car_speed_ratio) * dt / 30.0
            self.car_ratio_sum += ratio_sum
            self.car_ratio_n += ratio_n
        for seg, v in crossings:
            self._cross(seg, v)

    def _line_closed(self, v: Vehicle, seg: Segment, state: str, now: float) -> bool:
        """Must this vehicle hold at the stop line?"""
        if state == "R":
            return True
        dist = seg.stop_pos - v.pos
        brake_dist = v.v * v.v / (2 * self.cfg.comfort_brake)
        if state == "Y" and v.v * v.v / (2 * self.cfg.max_brake) <= dist:
            return True  # can still stop, so it must; only vehicles too fast to stop run the yellow
        if not self._can_enter(v):
            return True  # never enter the box without room to leave it
        if v.kind == "car":
            g = self._release_factor(seg.id)
            if g <= 0.0:
                return True
            if g < 1.0 and now < self.next_release[seg.id] and dist < max(15.0, brake_dist + 5.0):
                return True
        return False

    def _can_enter(self, v: Vehicle) -> bool:
        if v.next_seg is None:
            return True
        lane = self.lanes[v.next_seg]
        if not lane:
            return True
        # Room is judged for the moment this vehicle reaches the junction centre, not for now: whoever is
        # last on the next edge will have moved on by then, unless it is standing (a queue, a stalled vehicle).
        tail = lane[-1]
        arrive = (self.net.segments[v.seg].length - v.pos) / max(v.v, 3.0)
        return tail.pos - tail.length + tail.v * arrive >= v.length + self.cfg.min_gap

    def _cross(self, seg: Segment, v: Vehicle) -> None:
        """The vehicle has reached the intersection centre: hand it to the next segment."""
        tgt = v.next_seg
        if tgt is None:  # a car reached its exit gate
            self.exited += 1
            return
        self.in_box.get(tgt, {}).pop(v.id, None)
        v.seg, v.pos = tgt, max(0.0, v.pos - seg.length)
        lane = self.lanes[tgt]
        if lane:  # two streams may merge into this edge at once: the later one falls in behind the earlier
            ahead = lane[-1]
            room = ahead.pos - ahead.length - self.cfg.min_gap
            if v.pos > room:
                v.pos, v.v = room, min(v.v, ahead.v)  # may be a little before the centre: still in the junction
        lane.append(v)
        v.leg += 1
        if v.cyclic and v.route is not None and v.leg >= len(v.route):
            v.leg = 0
            v.laps += 1
        v.next_seg = self._next_on_route(v)

    def _spawn(self, dt: float) -> None:
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
        if (
            self.rush_node is not None and cfg.rush_extra > 0
        ):  # the rush-hour stream: extra commuters through the key point
            p = 1.0 - math.exp(-cfg.gate_demand * cfg.rush_extra * self.demand_scale * dt)
            for gate in self.net.gates:
                if self.rng.random() < p:
                    if self.rush_backlog[gate] < cfg.spawn_backlog_max:
                        self.rush_backlog[gate] += 1
                    else:
                        self.dropped += 1
                if self.rush_backlog[gate]:
                    self._try_spawn(gate, self.rush_backlog, self.rush_node)

    def _try_spawn(self, gate: int, backlog: dict[int, int] | None = None, via: int | None = None) -> None:
        backlog = self.backlog if backlog is None else backlog
        if via is None:
            for _ in range(4):  # pick a destination that is reachable
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
        lane = self.lanes[first]
        if self.in_box.get(first):  # through traffic is already crossing the gate junction into this edge
            return
        if not lane or lane[-1].pos - lane[-1].length >= self.cfg.car_length + self.cfg.min_gap + 2.0:
            backlog[gate] -= 1
            self.lanes[first].append(self._make_car(path, 0, 0.0, 0.7 * self.net.segments[first].speed_limit))

    def _path_via(self, gate: int, via: int) -> list[int] | None:
        net = self.net
        into = net.path_between(gate, via)
        if not into:
            return None
        for _ in range(6):
            out = net.path_between(via, self.rng.choice([g for g in net.gates if g != gate]))
            if out and net.segments[out[0]].to_node != net.segments[into[-1]].from_node:  # no U-turn at the key point
                return into + out
        return None

    # -- runtime traffic controls ---------------------------------------------
    def remove_vehicle(self, v: Vehicle) -> None:
        """Take a vehicle off the map (used when the traffic amount is lowered)."""
        lane = self.lanes[v.seg]
        if v in lane:
            lane.remove(v)
        if v.next_seg is not None:
            self.in_box.get(v.next_seg, {}).pop(v.id, None)
        if v in self.buses:
            self.buses.remove(v)

    def try_place_bus(self, bus_id: str, line: str, route: list[int], leg: int, pos: float) -> Vehicle | None:
        """Add a bus at a spot that is free right now; None if the spot is taken."""
        seg = self.net.segments[route[leg]]
        pos = min(pos, max(0.0, seg.stop_pos - 6.0))
        if not self._free_at(seg.id, pos, self.cfg.bus_length):
            return None
        bus = Vehicle(
            bus_id, "bus", seg.id, pos, 0.5 * seg.speed_limit, self.cfg.bus_length, 1.0,
            route=route, cyclic=True, leg=leg, line=line,
        )  # fmt: skip
        bus.next_seg = self._next_on_route(bus)
        self._insert(self.lanes[seg.id], bus)
        self.buses.append(bus)
        return bus

    def set_car_percent(self, new: float, old: float) -> None:
        """Change the car amount from `old` to `new` percent of normal: spawning scales, and cars on the
        map are thinned out or topped up straight away so the change is visible at once."""
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
        """Put a junction under rush-hour pressure (or lift it): an extra car stream is routed through it."""
        self.rush_node = node
        for g in self.rush_backlog:
            self.rush_backlog[g] = 0
        if node is not None:
            self.scatter_cars(round(self.cfg.rush_seed * self.demand_scale), via=node)
