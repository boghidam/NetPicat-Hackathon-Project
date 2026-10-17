"""Right-hand overlay: API stream, AI predictions, algorithm overrides, metrics."""

from __future__ import annotations

from dataclasses import dataclass, field

import pygame

from .events import OVERRIDE_KINDS
from .mock_api import BusTelemetry
from .simulation import Simulation

BG = (22, 25, 33)
RULE = (52, 58, 74)
TEXT = (210, 214, 224)
DIM = (125, 132, 150)
GREEN = (80, 220, 120)
AMBER = (240, 190, 60)
RED = (235, 70, 70)
CYAN = (70, 200, 255)
MAGENTA = (230, 90, 220)
ORANGE = (255, 160, 50)

KIND_COLOR = {
    "LOCAL_EXTEND": ORANGE,
    "PREDICTIVE_EXTEND": CYAN,
    "PREDICTIVE_SWITCH": CYAN,
    "EARLY_SWITCH": (230, 230, 120),
    "SPLIT_ADJUST": (170, 170, 190),
    "THROTTLE": MAGENTA,
    "SPILLBACK_HOLD": RED,
    "BUDGET_DENIED": DIM,
    "INCIDENT": RED,
    "INFO": TEXT,
}
KIND_LABEL = {
    "LOCAL_EXTEND": "EXTEND",
    "PREDICTIVE_EXTEND": "PRED-EXT",
    "PREDICTIVE_SWITCH": "PRED-SW",
    "EARLY_SWITCH": "EARLY",
    "SPLIT_ADJUST": "SPLIT",
    "THROTTLE": "THROTTLE",
    "SPILLBACK_HOLD": "SPILLBACK",
    "BUDGET_DENIED": "BUDGET",
}
LOG_KINDS = OVERRIDE_KINDS | {"INCIDENT", "INFO", "BUDGET_DENIED"}


def speed_color(ratio: float) -> tuple[int, int, int]:
    """Red (stopped) -> amber -> green (free flow)."""
    r = max(0.0, min(1.0, ratio))
    if r < 0.5:
        k = r / 0.5
        return (int(235 + (240 - 235) * k), int(60 + (190 - 60) * k), int(60 + (50 - 60) * k))
    k = (r - 0.5) / 0.5
    return (int(240 + (70 - 240) * k), int(190 + (215 - 190) * k), int(50 + (110 - 50) * k))


@dataclass
class UiState:
    speed: float = 1.0
    paused: bool = False
    heat: int = 1  # 0 off, 1 true density, 2 estimated load
    api_snapshot: list[BusTelemetry] = field(default_factory=list)
    api_refresh_at: float = 0.0


class Hud:
    def __init__(self, rect: pygame.Rect):
        self.rect = rect
        self.font = pygame.font.SysFont("menlo,dejavusansmono,consolas,couriernew,monospace", 13)
        self.small = pygame.font.SysFont("menlo,dejavusansmono,consolas,couriernew,monospace", 11)
        self.title = pygame.font.SysFont("menlo,dejavusansmono,consolas,couriernew,monospace", 14, bold=True)
        self.char_w = self.font.size("M")[0]

    def fit(self, text: str, font: pygame.font.Font, width: int) -> str:
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        pygame.draw.rect(screen, BG, self.rect)
        pygame.draw.line(screen, RULE, self.rect.topleft, self.rect.bottomleft, 2)
        x, w = self.rect.x + 14, self.rect.width - 28
        y = self.rect.y + 10
        y = self._status(screen, sim, ui, x, y, w)
        y = self._api(screen, sim, ui, x, y + 8, w)
        y = self._predictions(screen, sim, x, y + 8, w)
        y = self._overrides(screen, sim, x, y + 8, w)
        self._controls(screen, x, self.rect.bottom - 40, w)

    # -- helpers ---------------------------------------------------------------
    def _heading(self, screen, text: str, x: int, y: int, w: int, color=CYAN) -> int:
        screen.blit(self.title.render(text, True, color), (x, y))
        pygame.draw.line(screen, RULE, (x, y + 20), (x + w, y + 20), 1)
        return y + 25

    def _line(self, screen, text: str, x: int, y: int, w: int, color=TEXT, font=None) -> int:
        font = font or self.font
        screen.blit(font.render(self.fit(text, font, w), True, color), (x, y))
        return y + font.get_linesize() + 1

    # -- panels ----------------------------------------------------------------
    def _status(self, screen, sim: Simulation, ui: UiState, x: int, y: int, w: int) -> int:
        mode, color = ("ADAPTIVE AI CONTROL", GREEN) if sim.adaptive else ("FIXED TIMERS (baseline)", AMBER)
        screen.blit(self.title.render(mode, True, color), (x, y))
        state = "PAUSED" if ui.paused else f"{ui.speed:g}x"
        t = f"t={sim.t:6.1f}s  {state}"
        screen.blit(self.font.render(t, True, TEXT), (x + w - self.font.size(t)[0], y))
        y += 24
        m = sim.metrics()
        y = self._line(
            screen,
            f"bus time lost (recent) {m['bus_delay_recent']:5.1%}   overall {m['bus_delay_ratio']:5.1%}",
            x,
            y,
            w,
        )
        y = self._line(
            screen,
            f"mean link load {m['mean_density']:4.2f}  peak {m['peak_density']:4.2f}  exited {m['throughput']:5.0f} veh  overrides {int(m['overrides'])}",
            x,
            y,
            w,
            DIM,
        )
        return y

    def _api(self, screen, sim: Simulation, ui: UiState, x: int, y: int, w: int) -> int:
        y = self._heading(
            screen,
            f"MOCK API STREAM  ({sim.api.published:,} msgs, {len(sim.fleet.buses)} buses @ {1 / sim.cfg.dt:.0f} Hz)",
            x,
            y,
            w,
        )
        for tel in ui.api_snapshot:
            color = speed_color(tel.speed / tel.free_speed)
            y = self._line(screen, tel.format(), x, y, w, color, self.small)
        return y + (8 - len(ui.api_snapshot)) * (self.small.get_linesize() + 1)

    def _predictions(self, screen, sim: Simulation, x: int, y: int, w: int) -> int:
        p = sim.predictor
        y = self._heading(screen, "AI PREDICTIONS (feed-forward)", x, y, w)
        y = self._line(
            screen,
            f"model {p.model_name}  speed MAE {p.mae_speed:4.1f} m/s  ETA err {p.mae_eta:4.1f}s  resolved {p.resolved}",
            x,
            y,
            w,
            DIM,
        )
        upcoming = [f for f in p.active(sim.t) if f.hop == 1][:5]
        if not upcoming:
            y = self._line(screen, "(waiting for a bus to clear an intersection)", x, y, w, DIM, self.small)
        for f in upcoming:
            seg = sim.grid.segments[f.target_segment]
            color = RED if f.predicted_density > 0.6 else AMBER if f.predicted_density > 0.35 else GREEN
            eta = f"ETA {f.eta - sim.t:4.1f}s" if f.eta > sim.t else "ETA at line"
            y = self._line(
                screen,
                f"{f.source_bus} -> n{seg.to_node} {'NS' if seg.axis == 0 else 'EW'}  density {f.predicted_density:4.2f}  {eta}  conf {f.confidence:.2f}",
                x,
                y,
                w,
                color,
                self.small,
            )
        for msg in list(p.recent_resolved)[-2:]:
            y = self._line(screen, "done: " + msg, x, y, w, DIM, self.small)
        return y

    def _overrides(self, screen, sim: Simulation, x: int, y: int, w: int) -> int:
        y = self._heading(screen, "ALGORITHM OVERRIDES OF STANDARD TIMER", x, y, w)
        room = (self.rect.bottom - 48 - y) // (self.small.get_linesize() + 1)
        events = sim.events.recent(max(4, room), LOG_KINDS)
        if not events:
            y = self._line(screen, "(none yet: standard timers running)", x, y, w, DIM, self.small)
        for e in reversed(events):
            y = self._line(
                screen,
                f"{e.t:6.1f} {KIND_LABEL.get(e.kind, e.kind):<9} {e.text}",
                x,
                y,
                w,
                KIND_COLOR.get(e.kind, TEXT),
                self.small,
            )
        return y

    def _controls(self, screen, x: int, y: int, w: int) -> None:
        pygame.draw.line(screen, RULE, (x, y - 4), (x + w, y - 4), 1)
        self._line(screen, "SPACE pause   B adaptive/fixed   I incident   1 2 4 8 speed", x, y, w, DIM, self.small)
        self._line(screen, "H heat: off/true load/estimated load   R reset   Q quit", x, y + 14, w, DIM, self.small)
