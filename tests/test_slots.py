"""Tests for the Versioned Atomic JSON SlotManager and SlotRuntime lifecycle."""

from __future__ import annotations

import atexit
import json

import numpy as np
import pytest

import smartcity.slots as slots_mod
from smartcity.config import Config
from smartcity.mock_api import BusTelemetry
from smartcity.simulation import Simulation
from smartcity.slots import CURRENT_SCHEMA_VERSION, SlotManager, SlotRuntime


def _drive_predictor_to_ridge(sim: Simulation) -> None:
    """Feed synthetic bus crossings so `sim.predictor` fits its Ridge regression weights in RAM."""
    route = sim.net.routes[0]
    t = 0.0
    for i in range(35):
        r = 0.25 + 0.6 * ((i * 29) % 100) / 100.0
        for leg in (0, 1, 2):
            seg = sim.net.segments[route.edges[leg % len(route.edges)]]
            spd = r * seg.speed_limit
            for d in (150.0, 80.0, 4.0):
                t += 2.0
                sim.predictor.on_telemetry(
                    BusTelemetry(f"TB{i}", t, 0.0, 0.0, seg.id, d, spd, seg.speed_limit, 1.0, line=route.ref, leg=leg)
                )


def test_named_slots_flexibility_and_isolation(tmp_path):
    mgr = SlotManager(slots_dir=tmp_path / "slots")
    assert mgr.list_slots() == []

    # Create and populate two independent named slots
    s1 = mgr.clean_state("slot_1", map_name="iasi")
    s1["policy"]["trust"]["66"] = {"BUS_HOLD": 1.15}
    s1["metadata"]["episodes"] = 5
    mgr.save_slot("slot_1", s1)

    s2 = mgr.clean_state("rush_hour_exp", map_name="iasi")
    s2["policy"]["trust"]["195"] = {"LOCAL_EXTEND": 0.62}
    s2["metadata"]["episodes"] = 9
    mgr.save_slot("rush_hour_exp", s2)

    assert mgr.list_slots() == ["rush_hour_exp", "slot_1"]

    # a) Load existing slot to continue fine-tuning
    loaded_s1 = mgr.load_slot("slot_1")
    assert loaded_s1["policy"]["trust"]["66"]["BUS_HOLD"] == pytest.approx(1.15)
    assert loaded_s1["metadata"]["episodes"] == 5

    # b) Reset slot_1 to clean defaults -> rush_hour_exp must remain completely untouched
    reset_s1 = mgr.reset_slot("slot_1", map_name="iasi")
    assert reset_s1["policy"]["trust"] == {}
    assert reset_s1["metadata"]["episodes"] == 0
    assert mgr.load_slot("slot_1")["policy"]["trust"] == {}

    still_s2 = mgr.load_slot("rush_hour_exp")
    assert still_s2["policy"]["trust"]["195"]["LOCAL_EXTEND"] == pytest.approx(0.62)
    assert still_s2["metadata"]["episodes"] == 9

    # Starting from_clean in SlotRuntime does not delete or overwrite existing slot files on startup
    rt_clean = SlotRuntime(slot_name="rush_hour_exp", from_clean=True, manager=mgr, register_atexit=False)
    assert rt_clean.data["policy"]["trust"] == {}
    assert mgr.load_slot("rush_hour_exp")["policy"]["trust"]["195"]["LOCAL_EXTEND"] == pytest.approx(0.62)


def test_schema_v1_to_v2_and_sequential_forward_migration(tmp_path, monkeypatch):
    slots_dir = tmp_path / "slots"
    slots_dir.mkdir(parents=True)
    mgr = SlotManager(slots_dir=slots_dir)

    # Write a legacy v1 flat JSON file (no schema_version, no metadata/policy wrapper)
    legacy_v1 = {
        "map": "iasi",
        "episodes": 14,
        "default_trust": {"BUS_HOLD": 1.0, "LOCAL_EXTEND": 0.45},
        "phase_bonus": {"195": [22.0, 0.0]},
        "trust": {"66": {"BUS_HOLD": 1.1315, "LOCAL_EXTEND": 0.5101}},
        "count": {"66": 14},
        "outcomes": {
            "BUS_HOLD": {"decisive": 264, "wasted": 0, "missed": 2},
            "LOCAL_EXTEND": {"decisive": 4, "wasted": 12, "missed": 4},
        },
        "custom_note": "keep_me",
    }
    (slots_dir / "legacy_slot.json").write_text(json.dumps(legacy_v1), encoding="utf-8")

    migrated = mgr.load_slot("legacy_slot", map_name="iasi")
    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION == 2
    assert migrated["metadata"]["slot_name"] == "legacy_slot"
    assert migrated["metadata"]["episodes"] == 14
    assert "created_at" in migrated["metadata"] and "updated_at" in migrated["metadata"]
    assert migrated["metadata"]["sim_seconds_trained"] == 0.0
    assert migrated["policy"]["trust"]["66"]["BUS_HOLD"] == pytest.approx(1.1315)
    assert migrated["policy"]["outcomes"]["BUS_HOLD"]["decisive"] == 264
    assert migrated["custom_note"] == "keep_me"

    # Verify multi-step sequential migration pipeline (v1 -> v2 -> v3)
    def _migrate_v2_to_v3(d):
        d["metadata"]["migrated_to_v3"] = True
        d["schema_version"] = 3
        return d

    monkeypatch.setattr(slots_mod, "CURRENT_SCHEMA_VERSION", 3)
    monkeypatch.setitem(slots_mod.MIGRATIONS, 2, _migrate_v2_to_v3)
    migrated_v3 = mgr.load_slot("legacy_slot", map_name="iasi")
    assert migrated_v3["schema_version"] == 3
    assert migrated_v3["metadata"]["migrated_to_v3"] is True
    assert migrated_v3["policy"]["trust"]["66"]["BUS_HOLD"] == pytest.approx(1.1315)


def test_atomic_write_crash_safety_preserves_existing_slot_and_leaves_no_temp_files(tmp_path, monkeypatch):
    mgr = SlotManager(slots_dir=tmp_path / "slots")
    initial = mgr.clean_state("safe_slot", map_name="grid")
    initial["policy"]["trust"]["5"] = {"BUS_HOLD": 1.12}
    path = mgr.save_slot("safe_slot", initial)
    original_bytes = path.read_text(encoding="utf-8")

    # Simulate an abrupt failure mid-write before os.replace
    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk failure mid-write")

    monkeypatch.setattr(slots_mod.os, "fsync", _boom)
    broken = mgr.clean_state("safe_slot", map_name="grid")
    broken["policy"]["trust"]["5"] = {"BUS_HOLD": 0.40}
    with pytest.raises(OSError, match="simulated disk failure"):
        mgr.save_slot("safe_slot", broken)

    # Original file is 100% intact (not 0-byte or half-written) and no orphan .tmp files remain
    assert path.read_text(encoding="utf-8") == original_bytes
    assert list((tmp_path / "slots").glob("*.tmp")) == []
    assert list((tmp_path / "slots").glob(".*")) == []


def test_numpy_weights_roundtrip_and_zero_disk_io_during_steps(tmp_path, monkeypatch):
    mgr = SlotManager(slots_dir=tmp_path / "slots")
    writes: list[str] = []
    orig_write = slots_mod.atomic_write_json

    def _spy_write(path, payload):
        writes.append(path.name)
        return orig_write(path, payload)

    monkeypatch.setattr(slots_mod, "atomic_write_json", _spy_write)

    registered_callbacks = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered_callbacks.append(fn) or fn)
    monkeypatch.setattr(
        atexit,
        "unregister",
        lambda fn: registered_callbacks.remove(fn) if fn in registered_callbacks else None,
    )

    rt = SlotRuntime(
        slot_name="live_slot",
        map_name="grid",
        checkpoint_interval=10.0,  # flush every 10 sim seconds
        manager=mgr,
        register_atexit=True,
    )
    assert rt.close in registered_callbacks

    sim = Simulation(Config(seed=1), adaptive=True, map_name="grid", memory=rt.data)
    rt.attach(sim)
    _drive_predictor_to_ridge(sim)
    assert sim.predictor.model_name == "RIDGE"
    expected_weights = sim.predictor._weights.copy()

    # Step 9.5 sim seconds (95 ticks < 10.0s checkpoint): strictly zero disk writes!
    for _ in range(95):
        rt.step()
    assert writes == []

    # Crossing the 10.0s checkpoint triggers exactly 1 atomic disk flush
    for _ in range(6):
        rt.step()
    assert writes == ["live_slot.json"]

    # Step 3 more seconds and invoke close() (simulating clean shutdown / atexit)
    for _ in range(30):
        rt.step()
    rt.close()
    assert len(writes) == 2
    assert rt.close not in registered_callbacks

    # Verify JSON on disk uses pure Python list for weights and reconstructs np.ndarray on load
    raw_disk = json.loads(mgr.slot_path("live_slot").read_text(encoding="utf-8"))
    assert isinstance(raw_disk["predictor"]["weights"], list)
    assert raw_disk["metadata"]["sim_seconds_trained"] == pytest.approx(sim.t, abs=0.2)

    loaded = mgr.load_slot("live_slot", map_name="grid")
    sim2 = Simulation(Config(seed=2), adaptive=True, map_name="grid", memory=loaded)
    assert isinstance(sim2.predictor._weights, np.ndarray)
    assert sim2.predictor.model_name == "RIDGE"
    assert np.allclose(sim2.predictor._weights, expected_weights)
