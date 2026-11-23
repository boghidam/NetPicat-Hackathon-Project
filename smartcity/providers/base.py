"""Abstract interface for pluggable live transit telemetry providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..mock_api import BusTelemetry


@dataclass
class ProviderStatus:
    provider: str
    connected: bool = False
    active_vehicles: int = 0
    mapped_vehicles: int = 0
    last_sync_at: float | None = None
    poll_interval_s: float = 15.0
    error: str | None = None
    agency_id: str = "1"
    route_counts: dict[str, int] = field(default_factory=dict)
    attribution: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "connected": self.connected,
            "active_vehicles": self.active_vehicles,
            "mapped_vehicles": self.mapped_vehicles,
            "last_sync_at": self.last_sync_at,
            "poll_interval_s": self.poll_interval_s,
            "error": self.error,
            "agency_id": self.agency_id,
            "route_counts": dict(self.route_counts),
            "attribution": self.attribution,
        }


@dataclass
class LiveBusSnapshot:
    bus_id: str
    line: str
    lat: float
    lon: float
    x: float
    y: float
    dx: float
    dy: float
    speed: float  # m/s
    free_speed: float  # m/s
    segment_id: int
    dist_to_next_node: float
    delay: float = 0.0
    status: str = "DRIVING"  # "DRIVING" | "LAYOVER" | "DRIVER_BREAK"


class TelemetryProvider(ABC):
    """Common contract for real-time vehicle feeds (Tranzy API, future private CTP API, etc.)."""

    @abstractmethod
    def start(self) -> None:
        """Start background polling or streaming."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background polling and release resources."""

    @abstractmethod
    def step(self, now: float, dt: float) -> list[BusTelemetry]:
        """Advance smooth interpolation by `dt` seconds and publish `BusTelemetry` into `TelemetryBus`."""

    @abstractmethod
    def status(self) -> ProviderStatus:
        """Return current connection health, counts, and attribution."""

    @abstractmethod
    def snapshots(self) -> list[LiveBusSnapshot]:
        """Return current map-ready vehicle snapshots."""
