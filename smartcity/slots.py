"""Versioned, crash-safe JSON slot manager and runtime persistence lifecycle for ML state.

All live learning (`JunctionLearner` trust/outcome updates and `PostIntersectionPredictor` Ridge
regression refits) happens strictly in RAM during simulation ticks. Disk I/O is completely decoupled
from `sim.step()` and runs only on periodic simulation checkpoints, episode boundaries, or clean
application shutdown (`atexit`), using atomic temp-file writes + `os.replace`.
"""

from __future__ import annotations

import atexit
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

if TYPE_CHECKING:
    from .simulation import Simulation

CURRENT_SCHEMA_VERSION = 2
DEFAULT_SLOT_NAME = "default"
DEFAULT_CHECKPOINT_INTERVAL = 300.0  # simulation seconds between automatic disk flushes

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_SLOTS_DIR = DATA_DIR / "slots"
LEGACY_POLICY_PATH = DATA_DIR / "learned_policy.json"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_safe(obj: Any) -> Any:
    """Convert NumPy arrays and scalars into standard JSON-serializable Python types."""
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, float)):
        return float(obj)
    if isinstance(obj, (np.integer, int)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _default_policy_for_map(map_name: str) -> dict[str, Any]:
    from .learning import DEFAULT_IASI_PHASES, DEFAULT_TRUST, LEARNED_KINDS

    if map_name == "iasi":
        default_trust = dict(DEFAULT_TRUST)
        phase_bonus = {str(k): list(v) for k, v in sorted(DEFAULT_IASI_PHASES.items())}
    else:
        default_trust = {k: 1.0 for k in LEARNED_KINDS}
        phase_bonus = {}
    return {
        "map": map_name,
        "episodes": 0,
        "default_trust": default_trust,
        "phase_bonus": phase_bonus,
        "trust": {},
        "count": {},
        "outcomes": {
            kind: {"decisive": 0, "wasted": 0, "missed": 0}
            for kind in LEARNED_KINDS
        },
    }


def _default_predictor_state() -> dict[str, Any]:
    return {
        "weights": None,
        "trained_total": 0,
        "mae_ratio": 0.15,
        "mae_eta": 0.0,
    }


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate a schema v1 (flat policy dict or early envelope) to schema v2 without data loss."""
    now = _utc_now()
    raw = copy.deepcopy(data)
    map_name = str(raw.get("map") or (raw.get("metadata") or {}).get("map") or "iasi")
    slot_name = str(raw.get("slot_name") or raw.get("name") or (raw.get("metadata") or {}).get("slot_name") or DEFAULT_SLOT_NAME)

    if isinstance(raw.get("policy"), dict):
        policy = dict(raw["policy"])
    else:
        base_pol = _default_policy_for_map(map_name)
        policy = {
            "map": map_name,
            "episodes": int(raw.get("episodes", 0)),
            "default_trust": dict(raw.get("default_trust") or base_pol["default_trust"]),
            "phase_bonus": dict(raw.get("phase_bonus") if "phase_bonus" in raw else base_pol["phase_bonus"]),
            "trust": dict(raw.get("trust") or {}),
            "count": dict(raw.get("count") or {}),
            "outcomes": dict(raw.get("outcomes") or base_pol["outcomes"]),
        }

    raw_meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    metadata = {
        **raw_meta,
        "slot_name": str(raw_meta.get("slot_name") or slot_name),
        "map": str(raw_meta.get("map") or policy.get("map") or map_name),
        "created_at": str(raw_meta.get("created_at") or raw.get("created_at") or now),
        "updated_at": str(raw_meta.get("updated_at") or raw.get("updated_at") or now),
        "episodes": int(raw_meta.get("episodes", policy.get("episodes", raw.get("episodes", 0)))),
        "sim_seconds_trained": float(raw_meta.get("sim_seconds_trained", raw.get("sim_seconds_trained", 0.0))),
    }

    raw_pred = raw.get("predictor") if isinstance(raw.get("predictor"), dict) else {}
    pred_defaults = _default_predictor_state()
    weights = raw_pred.get("weights", raw.get("weights"))
    if isinstance(weights, np.ndarray):
        weights = weights.tolist()
    predictor = {
        **pred_defaults,
        **raw_pred,
        "weights": list(weights) if isinstance(weights, (list, tuple)) else None,
        "trained_total": int(raw_pred.get("trained_total", raw.get("trained_total", 0))),
    }

    migrated: dict[str, Any] = {
        "schema_version": 2,
        "metadata": metadata,
        "policy": policy,
        "predictor": predictor,
    }
    # Preserve any custom top-level extension fields for forward/backward safety
    reserved = {
        "schema_version",
        "metadata",
        "policy",
        "predictor",
        "map",
        "episodes",
        "default_trust",
        "phase_bonus",
        "trust",
        "count",
        "outcomes",
        "slot_name",
        "name",
        "created_at",
        "updated_at",
        "sim_seconds_trained",
        "weights",
        "trained_total",
    }
    for k, v in raw.items():
        if k not in reserved:
            migrated[k] = v
    return migrated


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write `payload` as formatted JSON using a same-directory temp file + `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = _json_safe(payload)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.stem}_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            temp_name = tf.name
            json.dump(safe_payload, tf, indent=2)
            tf.write("\n")
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(temp_name, path)
    except BaseException:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise


class SlotManager:
    """Manages named, versioned JSON save slots under `data/slots/<slot_name>.json`."""

    def __init__(self, slots_dir: Path = DEFAULT_SLOTS_DIR):
        self.slots_dir = Path(slots_dir)
        self.slots_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sanitize_name(slot_name: str) -> str:
        cleaned = "".join(c for c in slot_name.strip() if c.isalnum() or c in ("-", "_"))
        if not cleaned:
            raise ValueError(f"Invalid slot name {slot_name!r}; must contain alphanumeric, '-' or '_' characters.")
        return cleaned

    def slot_path(self, slot_name: str) -> Path:
        return self.slots_dir / f"{self.sanitize_name(slot_name)}.json"

    def exists(self, slot_name: str) -> bool:
        return self.slot_path(slot_name).exists()

    def list_slots(self) -> list[str]:
        if not self.slots_dir.exists():
            return []
        return sorted(p.stem for p in self.slots_dir.glob("*.json") if not p.name.startswith("."))

    @staticmethod
    def clean_state(slot_name: str = DEFAULT_SLOT_NAME, map_name: str = "iasi") -> dict[str, Any]:
        """Return a fresh schema v2 slot structure initialized to clean baseline defaults."""
        safe = SlotManager.sanitize_name(slot_name)
        now = _utc_now()
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "metadata": {
                "slot_name": safe,
                "map": map_name,
                "created_at": now,
                "updated_at": now,
                "episodes": 0,
                "sim_seconds_trained": 0.0,
            },
            "policy": _default_policy_for_map(map_name),
            "predictor": _default_predictor_state(),
        }

    def migrate(self, data: dict[str, Any], slot_name: str = DEFAULT_SLOT_NAME, map_name: str = "iasi") -> dict[str, Any]:
        """Sequentially upgrade `data` to `CURRENT_SCHEMA_VERSION` and ensure all required keys exist."""
        current = copy.deepcopy(data)
        version = int(current.get("schema_version", 1))
        while version < CURRENT_SCHEMA_VERSION:
            migrator = MIGRATIONS.get(version)
            if migrator is None:
                raise ValueError(f"Unsupported schema_version {version}; no migration path to v{CURRENT_SCHEMA_VERSION}")
            current = migrator(current)
            next_v = int(current.get("schema_version", version + 1))
            version = next_v if next_v > version else version + 1

        # Fill any missing optional sub-keys without clobbering existing trained data
        safe = self.sanitize_name(slot_name)
        defaults = self.clean_state(safe, map_name=map_name)
        meta = current.setdefault("metadata", {})
        for k, v in defaults["metadata"].items():
            meta.setdefault(k, v)
        meta["slot_name"] = safe
        pol = current.setdefault("policy", {})
        for k, v in defaults["policy"].items():
            pol.setdefault(k, copy.deepcopy(v))
        for kind, out_defaults in defaults["policy"]["outcomes"].items():
            pol["outcomes"].setdefault(kind, {}).update(
                {ok: int(pol["outcomes"].get(kind, {}).get(ok, ov)) for ok, ov in out_defaults.items()}
            )
        pred = current.setdefault("predictor", {})
        for k, v in defaults["predictor"].items():
            pred.setdefault(k, copy.deepcopy(v))
        if isinstance(pred.get("weights"), np.ndarray):
            pred["weights"] = pred["weights"].tolist()
        current["schema_version"] = CURRENT_SCHEMA_VERSION
        return current

    def load_slot(
        self,
        slot_name: str = DEFAULT_SLOT_NAME,
        map_name: str = "iasi",
        fallback_to_default: bool = True,
    ) -> dict[str, Any]:
        """Load and auto-migrate a named slot, or return clean defaults if missing and `fallback_to_default`."""
        safe = self.sanitize_name(slot_name)
        path = self.slot_path(safe)
        if not path.exists():
            # Seamless fallback to legacy learned_policy.json for the default slot if not yet migrated
            if (
                safe == DEFAULT_SLOT_NAME
                and self.slots_dir == DEFAULT_SLOTS_DIR
                and LEGACY_POLICY_PATH.exists()
            ):
                raw_legacy = json.loads(LEGACY_POLICY_PATH.read_text(encoding="utf-8"))
                migrated = self.migrate(raw_legacy, slot_name=safe, map_name=map_name)
                if migrated["metadata"].get("map") == map_name:
                    return migrated
            if fallback_to_default:
                return self.clean_state(safe, map_name=map_name)
            raise FileNotFoundError(f"Slot {safe!r} does not exist at {path}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        return self.migrate(raw, slot_name=safe, map_name=map_name)

    def save_slot(self, slot_name: str, payload: dict[str, Any]) -> Path:
        """Atomically persist `payload` to `data/slots/<slot_name>.json`."""
        safe = self.sanitize_name(slot_name)
        path = self.slot_path(safe)
        map_name = str(
            (payload.get("metadata") or {}).get("map")
            or (payload.get("policy") or {}).get("map")
            or "iasi"
        )
        normalized = self.migrate(payload, slot_name=safe, map_name=map_name)
        normalized["metadata"]["slot_name"] = safe
        normalized["metadata"]["updated_at"] = _utc_now()
        atomic_write_json(path, normalized)
        return path

    def reset_slot(self, slot_name: str, map_name: str = "iasi") -> dict[str, Any]:
        """Reset only `slot_name` to clean factory defaults on disk, leaving all other slots untouched."""
        safe = self.sanitize_name(slot_name)
        clean = self.clean_state(safe, map_name=map_name)
        self.save_slot(safe, clean)
        return clean

    def delete_slot(self, slot_name: str) -> bool:
        """Explicitly delete a single slot file; returns True if deleted."""
        path = self.slot_path(slot_name)
        if path.exists():
            path.unlink()
            return True
        return False


class SlotRuntime:
    """Lifecycle bridge hooking `SlotManager` into `Simulation`.

    * Loads or resets the chosen slot once at startup.
    * Leaves `sim.step()` 100% in-memory (zero disk I/O per tick).
    * Flushes updated `JunctionLearner` and `PostIntersectionPredictor` state to disk only at
      periodic simulation checkpoints (default every 300 sim seconds) and on shutdown via `atexit`.
    """

    def __init__(
        self,
        slot_name: str = DEFAULT_SLOT_NAME,
        map_name: str = "iasi",
        from_clean: bool = False,
        reset_slot: bool = False,
        checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL,
        manager: SlotManager | None = None,
        register_atexit: bool = True,
    ):
        self.manager = manager or SlotManager()
        self.slot_name = self.manager.sanitize_name(slot_name)
        self.map_name = map_name
        self.checkpoint_interval = float(checkpoint_interval)
        if reset_slot:
            self.data = self.manager.reset_slot(self.slot_name, map_name=map_name)
        elif from_clean:
            self.data = self.manager.clean_state(self.slot_name, map_name=map_name)
        else:
            self.data = self.manager.load_slot(self.slot_name, map_name=map_name, fallback_to_default=True)

        self.sim: Simulation | None = None
        self._last_checkpoint_t: float = 0.0
        self._accumulated_sim_seconds: float = float(self.data["metadata"].get("sim_seconds_trained", 0.0))
        self._base_sim_t: float = 0.0
        self._closed = False
        self._atexit_registered = False
        if register_atexit:
            atexit.register(self.close)
            self._atexit_registered = True

    def attach(self, sim: Simulation) -> Simulation:
        """Bind a `Simulation` instance and hydrate its in-memory ML modules from this slot."""
        if self.sim is not None and self.sim is not sim:
            self.flush()
            self._accumulated_sim_seconds = float(self.data["metadata"].get("sim_seconds_trained", 0.0))
        self.sim = sim
        sim.apply_slot_state(self.data)
        self._last_checkpoint_t = sim.t
        self._base_sim_t = sim.t
        self._closed = False
        return sim

    def step(self) -> None:
        """Advance one simulation tick in RAM and check the periodic persistence timer."""
        if self.sim is None:
            raise RuntimeError("No Simulation attached to SlotRuntime")
        self.sim.step()
        self.maybe_checkpoint()

    def run(self, seconds: float) -> None:
        """Run `seconds` of simulation time in RAM, flushing only at `checkpoint_interval` boundaries."""
        if self.sim is None:
            raise RuntimeError("No Simulation attached to SlotRuntime")
        ticks = int(round(seconds / self.sim.cfg.dt))
        for _ in range(ticks):
            self.sim.step()
            self.maybe_checkpoint()

    def maybe_checkpoint(self) -> bool:
        """Flush to disk if at least `checkpoint_interval` simulation seconds have elapsed since the last flush."""
        if self.sim is None or self.checkpoint_interval <= 0.0 or not self.sim.adaptive:
            return False
        if self.sim.t - self._last_checkpoint_t >= self.checkpoint_interval:
            return self.flush() is not None
        return False

    def flush(self, episode_done: bool = False) -> Path | None:
        """Collect in-memory ML state from `Simulation` and perform an atomic slot write."""
        if self.sim is None or not self.sim.adaptive:
            return None
        elapsed_run = max(0.0, self.sim.t - self._base_sim_t)
        if elapsed_run <= 0.0 and not episode_done:
            return None
        snapshot = self.sim.export_slot_state(self.data)
        snapshot["metadata"]["sim_seconds_trained"] = round(self._accumulated_sim_seconds + elapsed_run, 2)
        if episode_done:
            snapshot["metadata"]["episodes"] = max(
                int(snapshot["metadata"].get("episodes", 0)),
                int(self.data["metadata"].get("episodes", 0)) + 1,
            )
            snapshot["policy"]["episodes"] = snapshot["metadata"]["episodes"]
        self.data = snapshot
        self._last_checkpoint_t = self.sim.t
        return self.manager.save_slot(self.slot_name, self.data)

    def close(self) -> None:
        """Flush final in-memory state on shutdown and unregister the `atexit` callback."""
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
            if self._atexit_registered:
                atexit.unregister(self.close)
                self._atexit_registered = False
