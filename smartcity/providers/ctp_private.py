"""Future partner interface stub for a direct private CTP Iași high-frequency AVL feed."""

from __future__ import annotations

from ..mock_api import BusTelemetry, TelemetryBus
from ..network import RoadNetwork
from .base import LiveBusSnapshot, ProviderStatus, TelemetryProvider


class CTPPrivateProvider(TelemetryProvider):
    """Extensible stub implementing `TelemetryProvider` for a future private CTP Iași partnership API.

    Once a private high-frequency (1–5 Hz) WebSocket/REST AVL endpoint is available from CTP Iași,
    only this adapter needs to be configured — the entire AI signal engine, SlotManager, and Web UI
    consume the same `BusTelemetry` records with zero changes.
    """

    def __init__(self, net: RoadNetwork, bus: TelemetryBus, endpoint: str = "", token: str = ""):
        self.net = net
        self.api = bus
        self.endpoint = endpoint
        self.token = token
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def step(self, now: float, dt: float) -> list[BusTelemetry]:
        return []

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            provider="ctp_private",
            connected=False,
            error="Private CTP Iași AVL partnership endpoint not configured.",
            attribution="Compania de Transport Public Iași SA",
        )

    def snapshots(self) -> list[LiveBusSnapshot]:
        return []
