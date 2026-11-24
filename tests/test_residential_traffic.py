"""Tests for Iași residential buildings, 60-80% in-city car distribution, 60/20/20 OD trips,
strict distance bounds, arrival-only building despawn, Wave 2 uncrowded spawning, and UI cleanup."""

from __future__ import annotations

import math
from pathlib import Path

from smartcity.config import Config
from smartcity.data import build_city
from smartcity.simulation import Simulation

ROOT = Path(__file__).resolve().parent.parent


def test_iasi_buildings_loaded_and_grid_stays_untouched() -> None:
    cfg = Config()
    iasi = build_city("iasi", cfg)
    grid = build_city("grid", cfg)

    assert len(grid.buildings) == 0
    assert len(iasi.buildings) >= 500
    assert any(b.kind == "apartment" and b.zone == "city" for b in iasi.buildings)
    assert any(b.kind == "house" and b.zone == "city" for b in iasi.buildings)
    assert any(b.kind == "house" and b.zone == "outer" for b in iasi.buildings)
    for b in iasi.buildings:
        seg = iasi.segments[b.seg_id]
        assert 0.0 < b.pos_m < seg.stop_pos


def test_in_city_distribution_and_60_20_20_od_bounds() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")
    cx, cy = net.city_center_xy

    def check_snapshot() -> None:
        cars = [v for v in sim.model.iter_vehicles() if v.kind == "car"]
        assert len(cars) >= 1300
        # Core urban Iași (<= 3.5 km around Piața Unirii / Palas) must hold 60-80% of active cars
        in_urban_core = sum(
            1 for c in cars if math.hypot(net.position(c.seg, c.pos)[0] - cx, net.position(c.seg, c.pos)[1] - cy) <= 3500.0
        )
        urban_share = in_urban_core / len(cars)
        assert 0.60 <= urban_share <= 0.80, f"expected 60-80% in city core, got {urban_share:.1%}"

        # Exact 60% city->city / 20% outer->city / 20% outside->outside mix
        c_cnt = sum(1 for c in cars if c.trip_kind == "city_to_building")
        o_cnt = sum(1 for c in cars if c.trip_kind == "outer_to_building")
        t_cnt = sum(1 for c in cars if c.trip_kind == "through_city")
        assert abs(c_cnt / len(cars) - 0.60) <= 0.03
        assert abs(o_cnt / len(cars) - 0.20) <= 0.03
        assert abs(t_cnt / len(cars) - 0.20) <= 0.03

        # Strict road-network distance rules on EVERY active car
        for c in cars:
            if c.trip_kind == "city_to_building":
                assert c.dest_building_id is not None and c.dest_pos is not None
                assert cfg.city_trip_min_m <= c.trip_dist_m <= cfg.city_trip_max_m
            elif c.trip_kind == "outer_to_building":
                assert c.dest_building_id is not None and c.dest_pos is not None
                assert cfg.outer_trip_min_m <= c.trip_dist_m <= cfg.outer_trip_max_m
            else:
                assert c.dest_building_id is None and c.dest_pos is None
                assert cfg.outer_trip_min_m <= c.trip_dist_m <= cfg.outer_trip_max_m

    check_snapshot()  # Wave 1 at t = 0 s
    for _ in range(1200):  # 120 s of Wave 2 arrivals & residential respawns
        sim.step()
    check_snapshot()
    assert sim.model.wave1_done is True
    assert sim.model.building_arrivals > 50
    assert sim.model.residential_spawns > 50


def test_building_destined_cars_only_despawn_at_destination_building() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")

    # Track every building-destined car's last seen state before it disappears
    last_state: dict[str, tuple[int, int, float, float]] = {}
    for c in sim.model.iter_vehicles():
        if c.kind == "car" and c.dest_pos is not None and c.route is not None:
            last_state[c.id] = (c.leg, len(c.route) - 1, c.pos, c.dest_pos)

    for _ in range(400):  # 40 s
        sim.step()
        alive: set[str] = set()
        for c in sim.model.iter_vehicles():
            if c.kind == "car" and c.dest_pos is not None and c.route is not None:
                alive.add(c.id)
                last_state[c.id] = (c.leg, len(c.route) - 1, c.pos, c.dest_pos)
        # Any tracked building-destined car that disappeared MUST have been on its final leg approaching dest_pos
        for cid in list(last_state.keys()):
            if cid not in alive:
                leg, last_leg, pos, dest_pos = last_state.pop(cid)
                assert leg == last_leg, f"car {cid} despawned early on leg {leg}/{last_leg}"
                assert pos >= dest_pos - 6.0, f"car {cid} despawned far before dest_pos ({pos:.1f} vs {dest_pos:.1f})"


def test_wave2_never_spawns_on_crowded_residential_street() -> None:
    cfg = Config()
    net = build_city("iasi", cfg)
    sim = Simulation(cfg, adaptive=True, net=net, map_name="iasi")

    # Verify _can_spawn_at_curb rejects any segment whose density >= building_spawn_max_density
    for b in net.city_buildings[:60]:
        dens = sim.model.density(b.seg_id)
        if dens >= cfg.building_spawn_max_density:
            assert sim.model._can_spawn_at_curb(b.seg_id, b.pos_m) is False

    # Force-crowd a target city building's street and verify _can_spawn_at_curb strictly blocks spawning there
    target_b = net.city_buildings[0]
    seg = net.segments[target_b.seg_id]
    sim.model.lanes[seg.id].clear()
    # Pack cars until density >= 0.35
    pos = seg.stop_pos - 4.0
    while sim.model.density(seg.id) < 0.35 and pos > 5.0:
        c = sim.model._make_car([seg.id], 0, pos, 0.0)
        sim.model._insert(sim.model.lanes[seg.id], c)
        pos -= cfg.car_length + cfg.min_gap + 0.5
    assert sim.model.density(seg.id) >= cfg.building_spawn_max_density
    assert sim.model._can_spawn_at_curb(seg.id, target_b.pos_m) is False


def test_ui_removed_fixed_baseline_subtitle_and_has_buildings_toggle() -> None:
    for rel in ("web/index.html", "docs/index.html"):
        html = (ROOT / rel).read_text(encoding="utf-8")
        assert "kpiDelaySub" not in html
        assert "Fixed Baseline</div>" not in html
        assert 'id="btnBuildings"' in html

    for rel in ("web/app.js", "docs/app.js"):
        js = (ROOT / rel).read_text(encoding="utf-8")
        assert "kpiDelaySub" not in js
        assert "showBuildings" in js


def test_bus_delay_reduction_is_strong_and_stable_in_web_engine() -> None:
    from smartcity.web import EngineState

    eng = EngineState()
    try:
        samples: list[float] = []
        for _ in range(36):  # 180 s sampled every 5 s
            eng.sim.run(5.0)
            frame = eng.frame_payload()
            samples.append(frame["kpis"]["bus_delay_reduction_pct"])
        avg_red = sum(samples) / len(samples)
        spread = max(samples) - min(samples)
        assert avg_red >= 11.0, f"expected >= 11% avg delay reduction, got {avg_red:.1f}% ({samples})"
        assert spread <= 5.0, f"expected smooth reduction spread <= 5pp, got {spread:.1f}pp ({min(samples)}..{max(samples)})"

        # Switching to Fixed Timers drops cleanly to 0.0%, and toggling back recovers cleanly
        eng.sim.set_adaptive(False)
        assert eng.frame_payload()["kpis"]["bus_delay_reduction_pct"] == 0.0
        eng.sim.set_adaptive(True)
        assert eng.frame_payload()["kpis"]["bus_delay_reduction_pct"] >= 10.0
    finally:
        eng.shutdown()
