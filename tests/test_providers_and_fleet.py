"""Unit tests for 51-line CTP fleet layovers/driver breaks and Tranzy/CTP live telemetry providers."""

from __future__ import annotations

import random

import pytest

from smartcity.config import IASI_LINE_BUSES, Config
from smartcity.mock_api import TelemetryBus
from smartcity.providers import CTPPrivateProvider, MapMatcher, TranzyProvider
from smartcity.simulation import Simulation
from smartcity.traffic_model import TrafficModel


def test_bus_takes_5_to_15_min_layover_and_15_to_30_min_driver_break_without_despawning(iasi_net):
    cfg = Config(
        initial_cars=0,
        gate_demand=0.0,
        bus_layovers=True,
        bus_layover_min=300.0,
        bus_layover_max=900.0,
        bus_break_min=900.0,
        bus_break_max=1800.0,
        bus_break_every_min=3,
        bus_break_every_max=3,
    )
    world = TrafficModel(iasi_net, cfg, random.Random(42))
    route = iasi_net.route_by_ref["49"]  # short urban line with turnaround_leg > 0
    assert route.turnaround_leg > 0

    bus = world.add_bus("B01", route.ref, route.edges, route.turnaround_leg, 0.0, turnaround_leg=route.turnaround_leg)
    # Advance bus to the end of turnaround_leg so it crosses into the far terminus
    seg = iasi_net.segments[bus.seg]
    bus.pos = seg.length + 0.5
    world.lanes[seg.id].remove(bus)
    world._cross(seg, bus)

    # First arrival: 5-15 min normal layover, off active lane, still in world.buses (never despawned!)
    assert bus in world.buses and bus in world.resting_buses
    assert bus not in world.lanes[bus.seg]
    assert bus.status == "LAYOVER"
    assert 300.0 <= bus.rest_remaining <= 900.0

    # Tick through the entire layover -> bus smoothly resumes DRIVING onto its return leg
    rem = bus.rest_remaining
    for i in range(int(rem / cfg.dt) + 5):
        world.step(i * cfg.dt, cfg.dt, lambda s: "G", lambda s: 1.0)
        if bus.status == "DRIVING":
            break
    assert bus.status == "DRIVING"
    assert bus in world.lanes[bus.seg] and bus not in world.resting_buses

    # Trigger 2 more terminus arrivals -> 3rd arrival triggers 15-30 min DRIVER_BREAK
    for expected_status, lo, hi in (("LAYOVER", 300.0, 900.0), ("DRIVER_BREAK", 900.0, 1800.0)):
        bus.leg = route.turnaround_leg
        bus.seg = route.edges[bus.leg]
        bus.next_seg = route.edges[(bus.leg + 1) % len(route.edges)]
        world.lanes[bus.seg].clear()
        world._cross(iasi_net.segments[bus.seg], bus)
        assert bus.status == expected_status
        assert lo <= bus.rest_remaining <= hi
        world.resting_buses.remove(bus)
        bus.status = "DRIVING"


def test_resting_bus_does_not_emit_telemetry_or_pollute_sensors(iasi_net):
    sim = Simulation(Config(seed=1), adaptive=True, net=iasi_net)
    bus = sim.fleet.buses[0]
    seg = iasi_net.segments[bus.seg]
    bus.leg = bus.turnaround_leg
    bus.seg = bus.route[bus.leg]
    bus.next_seg = bus.route[(bus.leg + 1) % len(bus.route)]
    sim.model.lanes[seg.id] = [v for v in sim.model.lanes[seg.id] if v is not bus]
    sim.model._cross(iasi_net.segments[bus.seg], bus)
    assert bus.status in ("LAYOVER", "DRIVER_BREAK")
    assert bus.id not in sim.loops.latest
    emitted = sim.fleet.emit(sim.t)
    assert all(rec.bus_id != bus.id for rec in emitted)


def test_bus_fleet_override_counts_syncs_live_counts_into_aether(iasi_net):
    sim = Simulation(Config(seed=2), adaptive=True, net=iasi_net)
    sim.fleet.override_counts({"50": 4, "30": 12}, percent=100.0)
    assert sum(1 for b in sim.fleet.buses if b.line == "50") == 4
    assert sum(1 for b in sim.fleet.buses if b.line == "30") == 12
    assert len(IASI_LINE_BUSES) == 51
    m = sim.metrics()
    assert 0.0 <= m["predicted_flow"] <= 1.0


def test_tranzy_provider_map_matches_and_emits_telemetry(iasi_net):
    bus_api = TelemetryBus()
    received = []
    bus_api.subscribe(received.append)

    provider = TranzyProvider(iasi_net, bus_api, api_key="test-key", agency_id="1")
    provider._route_names = {"10": "28", "11": "41"}
    # Use real coordinates from Iași route 28 and 41
    seg28 = iasi_net.segments[iasi_net.route_by_ref["28"].edges[5]]
    x28, y28, _, _ = iasi_net.position(seg28.id, seg28.length * 0.4)
    lat28, lon28 = iasi_net.proj.to_latlon(x28, y28)

    seg41 = iasi_net.segments[iasi_net.route_by_ref["41"].edges[5]]
    x41, y41, _, _ = iasi_net.position(seg41.id, seg41.length * 0.4)
    lat41, lon41 = iasi_net.proj.to_latlon(x41, y41)

    provider.ingest_payload(
        [
            {"id": 101, "label": "IS-01-CTP", "latitude": lat28, "longitude": lon28, "speed": 36, "route_id": 10, "vehicle_type": 3},
            {"id": 102, "label": "IS-02-CTP", "latitude": lat41, "longitude": lon41, "speed": 28, "route_id": 11, "vehicle_type": 3},
            {"id": 103, "label": "TRAM-1", "latitude": lat28, "longitude": lon28, "speed": 20, "route_id": 99, "vehicle_type": 0},
        ]
    )
    st = provider.status()
    assert st.connected is True
    assert st.mapped_vehicles == 2
    assert st.route_counts == {"28": 1, "41": 1}

    emitted = provider.step(now=1.0, dt=0.1)
    assert len(emitted) == 2
    assert len(received) == 2
    assert {r.line for r in emitted} == {"28", "41"}
    assert all(r.speed > 0 for r in emitted)

    snaps = provider.snapshots()
    assert len(snaps) == 2


def test_ctp_private_provider_stub_contract(iasi_net):
    provider = CTPPrivateProvider(iasi_net, TelemetryBus())
    provider.start()
    assert provider.step(0.0, 0.1) == []
    assert provider.status().provider == "ctp_private"
    assert provider.status().connected is False
    provider.stop()
