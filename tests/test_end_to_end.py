"""Closed-loop scenarios on the synthetic grid and on the real Iași network."""

import pytest

from smartcity.config import Config
from smartcity.simulation import Simulation


@pytest.fixture(scope="module")
def grid():
    from smartcity.data import build_city

    return build_city("grid", Config())


def sim_for(name, iasi_net, grid, adaptive, seed=2, **kw):
    net = iasi_net if name == "iasi" else grid
    return Simulation(Config(seed=seed, **kw), adaptive=adaptive, incident_at=60.0, net=net)


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("name,seconds", [("grid", 300), ("iasi", 200)])
def test_vehicles_never_pass_a_red_stop_line_or_overlap(name, seconds, adaptive, iasi_net, grid):
    sim = sim_for(name, iasi_net, grid, adaptive)
    segs = sim.net.segments
    before: dict[str, tuple[int, float]] = {}
    crossings = 0
    for _ in range(int(seconds / sim.cfg.dt)):
        sim.step()
        for seg in segs:
            lane = sim.model.lanes[seg.id]
            for ahead, behind in zip(lane, lane[1:], strict=False):
                assert behind.pos <= ahead.pos - ahead.length + 1e-6, f"vehicles overlap on edge {seg.id}"
            for v in lane:
                prev = before.get(v.id)
                if prev and prev[0] == seg.id and prev[1] <= seg.stop_pos < v.pos:
                    crossings += 1
                    assert sim.coord.signal_state(seg.id) in ("G", "Y"), f"{v.id} ran a red on edge {seg.id}"
                before[v.id] = (seg.id, v.pos)
    assert crossings > 100


def test_junction_box_is_clear_when_a_new_street_turns_green(iasi_net):
    """Yellow + all-red must empty the box of anything that could conflict with the next phase."""
    sim = Simulation(Config(seed=4), adaptive=False, net=iasi_net)
    net, cfg = sim.net, sim.cfg
    last = {n: (c.idx, c.phase) for n, c in sim.controllers.items()}
    checked = still_entering = 0
    for _ in range(int(400 / cfg.dt)):
        sim.step()
        for n, c in sim.controllers.items():
            now = (c.idx, c.phase)
            if now[1] == "GREEN" and last[n][1] == "ALLRED":
                checked += 1
                for a in c.approaches:  # vehicles between a stop line and the centre
                    still_entering += sum(1 for v in sim.model.lanes[a] if v.pos > net.segments[a].stop_pos + 0.5)
            last[n] = now
    assert checked > 300
    assert still_entering <= 0.01 * checked  # the clearance interval empties the box (a rare slow vehicle aside)


def test_buses_stay_on_their_line_and_cars_on_their_route(iasi_net):
    sim = Simulation(Config(seed=3), adaptive=True, net=iasi_net)
    for _ in range(int(300 / sim.cfg.dt)):
        sim.step()
        for v in sim.model.iter_vehicles():
            if v.kind in ("bus", "car"):
                assert v.route is not None and v.seg == v.route[v.leg]
    kinds = {v.kind for v in sim.model.iter_vehicles()}
    assert {"car", "bus"} <= kinds and sim.model.exited > 0
    assert {b.line for b in sim.fleet.buses} == {r.ref for r in iasi_net.routes}


def test_adaptive_control_is_close_to_fixed_timers_and_never_cuts_a_green_short(iasi_net):
    """Only buses are observed and greens are only ever extended, so the gains are small. They must not be losses."""
    fixed, adaptive = [], []
    for seed in (1, 2, 3, 4):
        for mode, bucket in ((False, fixed), (True, adaptive)):
            sim = Simulation(Config(seed=seed, gate_demand=0.08), adaptive=mode, incident_at=120.0, net=iasi_net)
            sim.run(480)
            bucket.append(sim.metrics())

    def mean(rows, key):
        return sum(r[key] for r in rows) / len(rows)

    assert mean(adaptive, "bus_delay_ratio") <= mean(fixed, "bus_delay_ratio") + 0.01
    assert mean(adaptive, "car_speed") > 0.95 * mean(fixed, "car_speed")
    assert mean(adaptive, "throughput") > 0.95 * mean(fixed, "throughput")


def test_adaptive_run_exercises_the_decision_paths_and_the_ai_feed_forward(iasi_net):
    sim = Simulation(Config(seed=1), adaptive=True, incident_at=120.0, net=iasi_net)
    sim.run(600)
    counts = sim.events.counts
    assert counts["INCIDENT"] == 1
    assert counts["LOCAL_EXTEND"] > 5  # buses held through whole cycles earn longer greens
    assert counts["THROTTLE"] > 0
    assert counts["FORECAST"] > 0 and counts["FORECAST_RESOLVED"] > 0
    assert sim.predictor.model_name == "RIDGE"
    assert sim.api.published == len(sim.fleet.buses) * int(600 / sim.cfg.dt)
    for e in sim.events.recent(3000, {"LOCAL_EXTEND"}):
        assert "cycle" in e.text and "% of free flow" in e.text  # every extension cites its evidence


def test_fixed_mode_never_overrides_the_timer(iasi_net):
    sim = Simulation(Config(seed=1), adaptive=False, incident_at=60.0, net=iasi_net)
    sim.run(200)
    assert sim.events.overrides == 0


def test_incident_blocks_the_busiest_corridor_edge_then_clears(iasi_net):
    sim = Simulation(Config(seed=1), adaptive=False, incident_at=30.0, net=iasi_net)
    sid = sim.incident_segment
    assert sum(sid in r.edges for r in iasi_net.routes) >= 2  # a corridor shared by several lines
    sim.run(60)
    assert sid in sim.model.incidents
    sim.run(sim.cfg.incident_duration + 5)
    assert sid not in sim.model.incidents


def test_toggling_modes_at_runtime_is_safe(grid):
    sim = Simulation(Config(), adaptive=True, net=grid)
    sim.run(60)
    sim.set_adaptive(False)
    before = sim.events.overrides
    sim.run(60)
    assert sim.events.overrides == before
    sim.set_adaptive(True)
    sim.run(60)
    assert sim.t > 179


# -- the city-wide fleet -------------------------------------------------------------------


def watch_a_big_fleet(sim, seconds):
    """Run and check, every tick: lanes stay ordered, nothing runs a red, and the signal timings keep to SUMO's rules."""
    segs, cfg = sim.net.segments, sim.cfg
    before: dict[str, tuple[int, float]] = {}
    crossings = 0
    since = {n: (c.idx, c.phase, sim.t) for n, c in sim.controllers.items()}
    seen: dict[str, list[float]] = {"GREEN": [], "YELLOW": [], "ALLRED": []}
    skipped: set[int] = set()
    for _ in range(int(seconds / cfg.dt)):
        sim.step()
        for n, c in sim.controllers.items():
            idx, phase, start = since[n]
            if (c.idx, c.phase) != (idx, phase):
                if n in skipped:  # the first interval of each light is cut short by its staggered start
                    seen[phase].append(round(sim.t - start, 1))
                skipped.add(n)
                since[n] = (c.idx, c.phase, sim.t)
        for seg in segs:
            lane = sim.model.lanes[seg.id]
            for ahead, behind in zip(lane, lane[1:], strict=False):
                assert behind.pos <= ahead.pos - ahead.length + 1e-6, f"vehicles overlap on edge {seg.id}"
            for v in lane:
                prev = before.get(v.id)
                if prev and prev[0] == seg.id and prev[1] <= seg.stop_pos < v.pos:
                    crossings += 1
                    assert sim.coord.signal_state(seg.id) in ("G", "Y"), f"{v.id} ran a red on edge {seg.id}"
                before[v.id] = (seg.id, v.pos)
    return crossings, seen


@pytest.mark.parametrize("percent", [100, 300])
def test_a_big_fleet_stays_ordered_obeys_signals_and_keeps_the_sumo_timings(iasi_net, percent):
    sim = Simulation(Config(seed=6, traffic_amount=percent / 100), adaptive=True, incident_at=40.0, net=iasi_net)
    assert len(sim.fleet.buses) >= 100 * percent / 100
    crossings, seen = watch_a_big_fleet(sim, 150)
    assert crossings > 1000
    cfg = sim.cfg
    assert len(seen["GREEN"]) > 300
    assert min(seen["GREEN"]) >= cfg.min_green  # 7 s at the very least (the standard timer is longer)
    assert set(seen["YELLOW"]) == {cfg.yellow} and set(seen["ALLRED"]) == {cfg.all_red}  # 3 s, then 2 s all red


def test_every_bus_of_a_big_fleet_is_tracked_by_the_detection_loops(iasi_net):
    sim = Simulation(Config(seed=6, traffic_amount=3.0), adaptive=True, net=iasi_net)
    sim.run(60)
    assert len(sim.fleet.buses) == 300 and set(sim.loops.latest) == {b.id for b in sim.fleet.buses}
    near = [r for r in sim.loops.readings.values() if r.count[2] > 0]  # buses inside the 300 m window
    assert len(near) > 100  # many approaches see a bus coming at once
    for r in near:
        assert r.count[0] <= r.count[1] <= r.count[2]  # the 50 / 150 / 300 m windows nest


def test_vehicles_move_smoothly_along_their_polylines_and_never_jump_across_nodes(iasi_net):
    """Per tick a vehicle advances at most one tick of the fastest speed limit and moves on only to an edge that
    starts where its last one ended; the only backward step is a merging vehicle falling in behind another."""
    sim = Simulation(Config(seed=8, traffic_amount=3.0), adaptive=True, incident_at=30.0, net=iasi_net)
    segs, cfg = sim.net.segments, sim.cfg
    fastest = max(s.speed_limit for s in segs) * (1 + cfg.car_speed_var) * cfg.dt
    back = cfg.bus_length + cfg.min_gap + fastest  # landing behind a bus that is itself still moving
    prev: dict[str, tuple[int, float]] = {}
    steps = 0
    for _ in range(int(90 / cfg.dt)):
        sim.step()
        for v in sim.model.iter_vehicles():
            if v.kind == "blocker":
                continue
            if v.id in prev:
                seg_before, pos_before = prev[v.id]
                if seg_before == v.seg:
                    moved = v.pos - pos_before
                else:
                    assert segs[seg_before].to_node == segs[v.seg].from_node, f"{v.id} jumped between unconnected edges"
                    moved = segs[seg_before].length - pos_before + v.pos
                assert -back - 1e-6 <= moved <= fastest + 1e-6, f"{v.id} moved {moved:.2f} m in one tick"
                steps += 1
            prev[v.id] = (v.seg, v.pos)
    assert steps > 100_000
