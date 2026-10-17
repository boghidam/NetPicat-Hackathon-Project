"""AI post-intersection prediction: feed-forward traffic forecasts from bus history.

Each time a bus crosses a stop line, the predictor looks at the speed and delay the bus
accumulated over its last few road segments and predicts the speed it will see on the
segment it just entered (and, with lower confidence, the one after). The forecast is
posted to the downstream intersection *before* the bus arrives.

The model is deliberately lightweight: an EWMA baseline at cold start, replaced by an
online ridge regression (NumPy) once enough outcomes have been observed.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import Config
from .events import EventLog
from .mock_api import BusTelemetry
from .network import CityGrid

MIN_RATIO = 0.05


@dataclass(frozen=True)
class Forecast:
    id: int
    source_bus: str
    target_segment: int
    hop: int  # 1 = segment the bus just entered, 2 = the one after
    issued_at: float
    eta: float  # absolute sim time the bus is expected at the stop line
    predicted_speed: float
    predicted_density: float  # 0..1, fraction of jam density
    confidence: float  # 0..1
    expires_at: float


@dataclass
class _Leg:
    ratio: float
    delay_gain: float
    duration: float


class _Track:
    """What the predictor remembers about one bus."""

    def __init__(self, tel: BusTelemetry):
        self.segment_id = tel.segment_id
        self.start_t = tel.t
        self.delay_at_start = tel.delay
        self.ratio_sum = 0.0
        self.ratio_n = 0
        self.arrived_t: float | None = None
        self.history: deque[_Leg] = deque(maxlen=8)
        self.pending: tuple[Forecast, np.ndarray] | None = None


class PostIntersectionPredictor:
    FEATURES = 5  # bias, mean ratio, last ratio, delay rate, trend

    def __init__(self, grid: CityGrid, cfg: Config, events: EventLog):
        self.grid, self.cfg, self.events = grid, cfg, events
        self._tracks: dict[str, _Track] = {}
        self._active: dict[int, Forecast] = {}
        self._ids = itertools.count(1)
        self._train: deque[tuple[np.ndarray, float]] = deque(maxlen=cfg.pred_window)
        self._trained_total = 0
        self._weights: np.ndarray | None = None
        self.mae_ratio = 0.15  # EWMA of |predicted - actual| speed ratio
        self.mae_speed = 0.15 * cfg.free_speed
        self.mae_eta = 0.0
        self.resolved = 0
        self.issued = 0
        self.recent_resolved: deque[str] = deque(maxlen=20)

    # -- public queries ----------------------------------------------------
    @property
    def model_name(self) -> str:
        return "RIDGE" if self._weights is not None else "EWMA"

    def incoming(self, seg_id: int, now: float) -> list[Forecast]:
        return [f for f in self._active.values() if f.target_segment == seg_id and f.expires_at > now]

    def active(self, now: float) -> list[Forecast]:
        return sorted((f for f in self._active.values() if f.expires_at > now), key=lambda f: f.eta)

    # -- telemetry intake --------------------------------------------------
    def on_telemetry(self, tel: BusTelemetry) -> None:
        track = self._tracks.get(tel.bus_id)
        if track is None:
            self._tracks[tel.bus_id] = _Track(tel)
            return
        if tel.segment_id != track.segment_id:
            self._on_crossing(track, tel)
        self._accumulate(track, tel)

    def _accumulate(self, track: _Track, tel: BusTelemetry) -> None:
        cfg = self.cfg
        seg = self.grid.segments[tel.segment_id]
        pos = seg.stop_pos - tel.dist_to_next_node
        if pos >= cfg.est_skip_start and tel.dist_to_next_node >= cfg.est_skip_end:
            track.ratio_sum += max(0.0, min(1.0, tel.speed / tel.free_speed))
            track.ratio_n += 1
        if track.arrived_t is None and tel.dist_to_next_node <= 5.0:
            track.arrived_t = tel.t

    # -- the post-intersection hook ---------------------------------------
    def _on_crossing(self, track: _Track, tel: BusTelemetry) -> None:
        duration = max(tel.t - track.start_t, 1e-6)
        gain = tel.delay - track.delay_at_start
        if track.ratio_n:
            ratio = track.ratio_sum / track.ratio_n
        else:  # never seen in the mid-block window (queue spilled back to the line)
            ratio = max(0.0, 1.0 - gain / duration)

        if track.pending is not None:
            self._resolve(track, ratio, tel)
        # a hop-2 forecast from this bus for the segment it just entered is now realised
        for fid in [
            f.id for f in self._active.values() if f.source_bus == tel.bus_id and f.target_segment == tel.segment_id
        ]:
            del self._active[fid]

        track.history.append(_Leg(ratio, gain, duration))
        track.segment_id, track.start_t, track.delay_at_start = tel.segment_id, tel.t, tel.delay
        track.ratio_sum, track.ratio_n, track.arrived_t = 0.0, 0, None
        track.pending = None
        self._issue(track, tel)

    def _features(self, track: _Track) -> tuple[np.ndarray, float]:
        legs = list(track.history)[-self.cfg.pred_history :]
        ratios = [leg.ratio for leg in legs]
        mean_ratio = float(np.mean(ratios))
        last_ratio = ratios[-1]
        delay_rate = sum(leg.delay_gain for leg in legs) / max(sum(leg.duration for leg in legs), 1e-6)
        trend = ratios[-1] - ratios[0]
        ewma = ratios[0]
        for r in ratios[1:]:
            ewma = 0.5 * r + 0.5 * ewma
        return np.array([1.0, mean_ratio, last_ratio, min(1.0, delay_rate), trend]), ewma

    def _issue(self, track: _Track, tel: BusTelemetry) -> None:
        cfg = self.cfg
        x, baseline = self._features(track)
        if self._weights is not None:
            ratio = float(x @ self._weights)
            skill = 1.0 - self.mae_ratio / 0.3
            confidence = min(0.95, max(0.25, skill))
        else:
            ratio = baseline
            confidence = 0.45
        ratio = min(1.0, max(MIN_RATIO, ratio))
        confidence *= 0.6 + 0.4 * min(1.0, len(track.history) / cfg.pred_history)

        seg = self.grid.segments[tel.segment_id]
        speed = max(1.0, ratio * cfg.free_speed)
        eta = tel.t + tel.dist_to_next_node / speed
        f1 = self._publish(tel, seg.id, 1, eta, ratio, confidence)
        track.pending = (f1, x)

        nxt = self.grid.straight_next(seg.id)
        if nxt is not None and self.grid.segments[nxt].signalised:
            ratio2 = 0.6 * ratio + 0.4 * (1.0 - cfg.est_prior_load)  # persistence, regressing to normal
            eta2 = eta + self.grid.segments[nxt].length / max(1.0, ratio2 * cfg.free_speed)
            self._publish(tel, nxt, 2, eta2, ratio2, confidence * cfg.pred_hop2_decay)

    def _publish(
        self, tel: BusTelemetry, seg_id: int, hop: int, eta: float, ratio: float, confidence: float
    ) -> Forecast:
        cfg = self.cfg
        f = Forecast(
            id=next(self._ids),
            source_bus=tel.bus_id,
            target_segment=seg_id,
            hop=hop,
            issued_at=tel.t,
            eta=eta,
            predicted_speed=ratio * cfg.free_speed,
            predicted_density=1.0 - ratio,
            confidence=confidence,
            expires_at=eta + cfg.pred_ttl_margin,
        )
        self._active[f.id] = f
        self.issued += 1
        target = self.grid.segments[seg_id].to_node
        self.events.add(
            tel.t,
            "FORECAST",
            f"{tel.bus_id} -> seg{seg_id} (n{target}) hop{hop}: density {f.predicted_density:.2f} "
            f"ETA {eta - tel.t:4.1f}s conf {confidence:.2f} [{self.model_name}]",
            node=target,
        )
        return f

    # -- learning ---------------------------------------------------------
    def _resolve(self, track: _Track, actual_ratio: float, tel: BusTelemetry) -> None:
        cfg = self.cfg
        forecast, x = track.pending  # type: ignore[misc]
        self._active.pop(forecast.id, None)
        arrival = track.arrived_t if track.arrived_t is not None else tel.t
        err_ratio = abs(forecast.predicted_speed / cfg.free_speed - actual_ratio)
        err_eta = abs(forecast.eta - arrival)
        a = 0.1
        self.mae_ratio += a * (err_ratio - self.mae_ratio)
        self.mae_speed = self.mae_ratio * cfg.free_speed
        self.mae_eta += a * (err_eta - self.mae_eta) if self.resolved else (err_eta - self.mae_eta)
        self.resolved += 1
        msg = (
            f"{forecast.source_bus} seg{forecast.target_segment}: predicted density "
            f"{forecast.predicted_density:.2f}, actual {1 - actual_ratio:.2f} (ETA err {err_eta:.1f}s)"
        )
        self.recent_resolved.append(msg)
        self.events.add(tel.t, "FORECAST_RESOLVED", msg, node=self.grid.segments[forecast.target_segment].to_node)

        self._train.append((x, actual_ratio))
        self._trained_total += 1
        if len(self._train) >= cfg.pred_min_samples and self._trained_total % cfg.pred_refit_every == 0:
            self._fit()

    def _fit(self) -> None:
        X = np.array([x for x, _ in self._train])
        y = np.array([t for _, t in self._train])
        reg = self.cfg.pred_ridge_lambda * np.eye(self.FEATURES)
        reg[0, 0] = 0.0  # do not shrink the intercept
        self._weights = np.linalg.solve(X.T @ X + reg, X.T @ y)
