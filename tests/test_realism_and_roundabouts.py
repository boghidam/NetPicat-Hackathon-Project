"""Tests for multi-lane MOBIL overtaking, Romanian roundabouts (OUG 195 + HG 130/2023),
unsignalized Cedează yield junctions, real CTP bus stops, vehicle classes, weather, day-cycle,
urban double-parking friction, and emergency ambulance preemption."""

from __future__ import annotations

import math

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.mock_api import BusTelemetry
from smartcity.simulation import Simulation


def test_iasi_multi_lane_roundabouts_stops_and_grid_isolation() -> None:
    cfg = Config()
    iasi = build_city("iasi", cfg)
    grid = build_city("grid", cfg)

    # Grid remains pure 1-lane without roundabouts/stops
    assert all(s.lanes == 1 for s in grid.segments)
    assert len(grid.roundabouts) == 0
    assert len(grid.bus_stops) == 0

    # Iași has real mixed-traffic 1, 2, and 3-lane streets, 16 real car roundabouts, yield junctions, 14 CTP rond loops, stops, crosswalks
    assert sum(1 for s in iasi.segments if s.lanes == 2) >= 180
    assert sum(1 for s in iasi.segments if s.lanes == 3) >= 50
    assert len(iasi.roundabouts) == 16
    assert sum(1 for r in iasi.roundabouts if not r.signalized) == 13
    assert sum(1 for r in iasi.roundabouts if r.signalized) == 3  # Podu Roș, Podu de Piatră & Fundație
    rb_nodes = {r.node for r in iasi.roundabouts}
    assert {80, 76, 83} <= rb_nodes  # Rond Zimbru, Rond Pasaj Octav Băncilă (ANL-uri), Rond Fundație
    assert len(iasi.rond_loops) == 14
    assert len(iasi.yield_nodes) >= 30
    assert len(iasi.bus_stops) >= 180
    assert len(iasi.crosswalks) >= 20
    assert len(iasi.hospitals) >= 4
    # Unsignalized roundabouts & yield junctions must NOT have traffic signals
    for rb in iasi.roundabouts:
        assert iasi.nodes[rb.node].kind == "roundabout"
        assert iasi.nodes[rb.node].signalised is (rb.signalized and not iasi.nodes[rb.node].gate)
    for yn in iasi.yield_nodes:
        assert iasi.nodes[yn].kind == "yield"
        assert iasi.nodes[yn].signalised is False


def test_unsignalized_roundabout_enforces_yield_to_left_gap_acceptance() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    model = sim.model

    rb = next(r for r in net.roundabouts if not r.signalized)
    in_seg_id = net.in_segs[rb.node][0]
    out_seg_id = next(s for s in net.out_segs[rb.node] if net.segments[s].to_node != net.segments[in_seg_id].from_node)
    in_seg = net.segments[in_seg_id]

    # Clear the roundabout ring first
    model.roundabout_rings[rb.node].clear()
    model._rb_last_entry[in_seg_id] = -99.0

    entering = model._make_car([in_seg_id, out_seg_id], 0, in_seg.stop_pos - 3.0, 5.0)
    # 1. Empty ring -> entry is OPEN
    assert model._roundabout_entry_closed(entering, in_seg, rb, "G", now=10.0) is False

    # 2. Place a circulating vehicle approaching right from the left (~12 m arc behind entry at 7 m/s -> TTA ~1.7s < Tc)
    entry_ang = rb.arm_angles[in_seg_id]
    r_eff = max(6.0, rb.radius_m - 1.8)
    circ = model._make_car([in_seg_id, out_seg_id], 0, in_seg.length, 7.0)
    circ.rb_node = rb.node
    circ.rb_lane = 0
    circ.rb_angle = (entry_ang - 12.0 / r_eff) % (2.0 * math.pi)
    circ.rb_remaining = 2.0
    model.roundabout_rings[rb.node].append(circ)

    assert model._roundabout_entry_closed(entering, in_seg, rb, "G", now=10.0) is True


def test_hg_130_2023_inner_lane_yields_to_occupied_outer_lane_in_roundabout() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    model = sim.model

    rb = next(r for r in net.roundabouts if r.ring_lanes >= 2)
    in_seg_id = net.in_segs[rb.node][0]
    out_seg_id = net.out_segs[rb.node][0]
    model.roundabout_rings[rb.node].clear()

    # Inner Bandă 2 vehicle preparing to exit (rb_remaining = 0.6 rad <= 1.25 rad)
    inner = model._make_car([in_seg_id, out_seg_id], 0, net.segments[in_seg_id].length, 6.5)
    inner.rb_node = rb.node
    inner.rb_lane = 1
    inner.rb_lane_visual = 1.0
    inner.rb_angle = 1.0
    inner.rb_remaining = 0.6
    inner.rb_target_seg = out_seg_id

    # Outer Bandă 1 vehicle driving right beside it at the same angle -> HG 130/2023 blocks cut-across!
    outer = model._make_car([in_seg_id, out_seg_id], 0, net.segments[in_seg_id].length, 6.5)
    outer.rb_node = rb.node
    outer.rb_lane = 0
    outer.rb_lane_visual = 0.0
    outer.rb_angle = 1.02
    outer.rb_remaining = 2.5
    outer.rb_target_seg = out_seg_id

    model.roundabout_rings[rb.node] = [inner, outer]
    model._step_roundabouts(0.1, now=1.0)
    assert inner.rb_lane == 1  # MUST stay in Bandă 2 while Bandă 1 is occupied!

    # Once outer Bandă 1 vehicle moves far away, inner vehicle safely merges to Bandă 1 to exit
    outer.rb_angle = 3.5
    model._step_roundabouts(0.1, now=1.1)
    assert inner.rb_lane == 0


def test_multi_lane_overtaking_and_zero_overlap_on_iasi() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    # Trigger a single-lane incident on a 2- or 3-lane boulevard
    multi_seg = next(s for s in net.segments if s.lanes >= 2 and s.length >= 160.0 and s.id in net.city_seg_ids)
    sim.trigger_incident(multi_seg.id)
    assert len(sim.model.lane_tracks[multi_seg.id]) >= 2

    for _ in range(600):  # 60 s
        sim.step()
        for seg in net.segments:
            for tr in sim.model.lane_tracks[seg.id]:
                for ahead, behind in zip(tr, tr[1:], strict=False):
                    assert behind.pos <= ahead.pos - ahead.length + 1e-5, f"overlap seg={seg.id} lane={ahead.lane}"

    assert sim.model.lane_changes > 50
    assert sim.model.roundabout_entries > 15
    assert sim.model.stop_boardings > 20


def test_ctp_bus_stop_boarding_does_not_trigger_false_congestion() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    seg = net.segments[net.bus_stops[0].seg_id]
    before_samples = sim.estimator.samples[seg.id]
    # Emit a zero-speed telemetry sample marked at_stop=True -> estimator must ignore it!
    sim.estimator.on_telemetry(
        BusTelemetry("B01", 10.0, 0.0, 0.0, seg.id, seg.stop_pos * 0.5, 0.0, seg.speed_limit, 0.0, line="30", at_stop=True)
    )
    assert sim.estimator.samples[seg.id] == before_samples


def test_weather_day_cycle_urban_friction_and_ambulance_dispatch() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")

    # Weather presets
    sim.set_weather("rain")
    assert sim.model.weather == "rain"
    sim.set_weather("snow")
    assert sim.model.weather == "snow"
    sim.set_weather("clear")

    # 24h Day-Cycle demand curve peaks at 08:00 AM and 17:30 PM, low at 03:00 AM
    assert sim.day_demand_multiplier(8.0 * 60) > 1.25
    assert sim.day_demand_multiplier(17.5 * 60) > 1.30
    assert sim.day_demand_multiplier(3.0 * 60) < 0.50
    sim.set_day_cycle(True)
    assert sim.day_cycle_enabled is True

    # Urban double-parking friction spawns strictly on multi-lane streets (>= 2 lanes)
    sim.set_urban_friction(True)
    # Emergency ambulance dispatch from hospital
    assert sim.dispatch_ambulance() is True
    assert len(sim.model.ambulances) == 1

    sim.run(60.0)
    for sid in sim.model.friction_blockers:
        assert net.segments[sid].lanes >= 2


def test_web_api_exposes_roundabouts_lanes_stops_and_realism_ws_commands() -> None:
    from fastapi.testclient import TestClient

    from smartcity.web import create_app

    app = create_app()
    with TestClient(app) as client:
        mp = client.get("/api/map").json()
        assert len(mp["roundabouts"]) == 16
        assert len(mp["junctions"]) >= 120
        assert len(mp["rond_loops"]) == 14
        assert len(mp["bus_stops"]) >= 180
        assert len(mp["crosswalks"]) >= 20
        assert any(r["lanes"] >= 2 for r in mp["roads"])

        def recv_until(ws, pred):
            for _ in range(15):
                frame = ws.receive_json()
                if pred(frame):
                    return frame
            raise AssertionError("timed out waiting for expected WS frame")

        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_json({"cmd": "set_weather", "weather": "rain"})
            f = recv_until(ws, lambda m: m.get("weather") == "rain")
            assert f["weather"] == "rain"

            ws.send_json({"cmd": "set_day_cycle", "enabled": True})
            f = recv_until(ws, lambda m: m.get("day_cycle") is True)
            assert f["day_cycle"] is True and ":" in f["day_time"]

            ws.send_json({"cmd": "set_urban_friction", "enabled": True})
            f = recv_until(ws, lambda m: m.get("urban_friction") is True)
            assert f["urban_friction"] is True

            ws.send_json({"cmd": "dispatch_ambulance"})
            f = recv_until(ws, lambda m: m.get("ambulances", 0) >= 1)
            assert f["ambulances"] >= 1


def test_roundabouts_never_deadlock_or_cross_center_island() -> None:
    cfg = Config(seed=1)
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    sim.run(120.0)
    # 1. No vehicle connected to or inside a roundabout may ever be rendered inside its green center island
    #    (grade-separated underpasses/overpasses such as Pasajul Mihai Eminescu beneath Rond Fundație are excluded)
    for v in sim.model.iter_vehicles():
        if v.kind == "blocker":
            continue
        seg = net.segments[v.seg]
        x, y, _, _ = sim.model.vehicle_xy(v)
        for rb in net.roundabouts:
            if v.rb_node != rb.node and seg.from_node != rb.node and seg.to_node != rb.node:
                continue
            island_r = rb.radius_m - (4.7 if rb.ring_lanes >= 2 else 2.1)
            dist = math.hypot(x - rb.x, y - rb.y)
            assert dist >= island_r - 0.25, f"{v.id} entered center island of {rb.name} (dist={dist:.1f} < {island_r:.1f})"

    # 2. Every vehicle currently circulating inside a roundabout must progress (no zero-angle deadlock!)
    snap = {
        v.id: (rb.node, v.rb_angle)
        for rb in net.roundabouts
        for v in sim.model.roundabout_rings[rb.node]
    }
    sim.run(15.0)
    still_frozen = 0
    for rb in net.roundabouts:
        for v in sim.model.roundabout_rings[rb.node]:
            if v.id in snap and snap[v.id][0] == rb.node:
                d_ang = abs(v.rb_angle - snap[v.id][1])
                if d_ang < 0.05 and v.v < 0.2:
                    still_frozen += 1
    assert still_frozen == 0, f"{still_frozen} vehicles frozen inside roundabouts after 15 s"
