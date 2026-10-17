"""Right-hand dashboard: live bus telemetry, AI predictions, detection loops, automated cycle adjustments."""

from __future__ import annotations

from dataclasses import dataclass, field

import pygame

from .events import OVERRIDE_KINDS
from .simulation import Simulation

# --- neon-on-dark palette -------------------------------------------------------
BG = (11, 13, 19)
PANEL = (16, 19, 27)
RULE = (38, 44, 60)
TEXT = (214, 220, 232)
DIM = (112, 122, 146)
CYAN = (0, 229, 255)
MAGENTA = (255, 64, 200)
LIME = (57, 255, 20)
AMBER = (255, 196, 0)
RED = (255, 23, 68)
ORANGE = (255, 145, 0)
VIOLET = (170, 120, 255)
LINE_COLORS = [CYAN, MAGENTA, ORANGE, LIME, VIOLET, AMBER, (255, 105, 135), (0, 255, 170)]

KIND_COLOR = {
    "LOCAL_EXTEND": ORANGE,
    "PREDICTIVE_EXTEND": CYAN,
    "PREDICTIVE_SWITCH": CYAN,
    "EARLY_SWITCH": AMBER,
    "SPLIT_ADJUST": DIM,
    "THROTTLE": MAGENTA,
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
    "BUDGET_DENIED": "BUDGET",
}
LOG_KINDS = OVERRIDE_KINDS | {"INCIDENT", "INFO", "BUDGET_DENIED"}


def speed_color(ratio: float) -> tuple[int, int, int]:
    """Neon red (stopped) -> amber -> lime (free flow)."""
    r = max(0.0, min(1.0, ratio))
    if r < 0.5:
        k = r / 0.5
        return (255, int(23 + (196 - 23) * k), int(68 * (1 - k)))
    k = (r - 0.5) / 0.5
    return (int(255 + (57 - 255) * k), int(196 + (255 - 196) * k), int(20 * k))


def line_color(sim: Simulation, ref: str) -> tuple[int, int, int]:
    refs = [r.ref for r in sim.net.routes]
    return LINE_COLORS[refs.index(ref) % len(LINE_COLORS)] if ref in refs else DIM


@dataclass
class BusRow:
    bus_id: str
    line: str
    speed_kmh: float
    ratio: float
    dist: float
    delay: float
    predicted: float  # extra seconds the AI expects over the next links of its route


@dataclass
class UiState:
    speed: float = 1.0
    paused: bool = False
    heat: int = 0  # 0 off, 1 true occupancy, 2 estimated load
    show_lines: bool = True
    rows: list[BusRow] = field(default_factory=list)


def snapshot_buses(sim: Simulation, n: int = 10) -> list[BusRow]:
    """The buses the AI expects to lose the most time, with their live telemetry."""
    extra: dict[str, float] = {}
    for f in sim.predictor.active(sim.t):
        extra[f.source_bus] = extra.get(f.source_bus, 0.0) + f.predicted_delay
    latest = sim.loops.latest
    rows = []
    for bus_id, tel in latest.items():
        rows.append(
            BusRow(
                bus_id,
                tel.line,
                tel.speed * 3.6,
                tel.speed / tel.free_speed,
                tel.dist_to_next_node,
                tel.delay,
                extra.get(bus_id, 0.0),
            )
        )
    rows.sort(key=lambda r: (-r.predicted, -r.delay))
    return rows[:n]


class Hud:
    def __init__(self, rect: pygame.Rect):
        self.rect = rect
        names = "menlo,dejavusansmono,consolas,couriernew,monospace"
        self.font = pygame.font.SysFont(names, 13)
        self.small = pygame.font.SysFont(names, 11)
        self.title = pygame.font.SysFont(names, 14, bold=True)

    def fit(self, text: str, font: pygame.font.Font, width: int) -> str:
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        pygame.draw.rect(screen, PANEL, self.rect)
        pygame.draw.line(screen, CYAN, self.rect.topleft, self.rect.bottomleft, 1)
        x, w = self.rect.x + 14, self.rect.width - 28
        y = self.rect.y + 10
        y = self._status(screen, sim, ui, x, y, w)
        y = self._buses(screen, sim, ui, x, y + 8, w)
        y = self._predictions(screen, sim, x, y + 8, w)
        y = self._loops(screen, sim, x, y + 8, w)
        y = self._overrides(screen, sim, x, y + 8, w)
        self._controls(screen, x, self.rect.bottom - 44, w)

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
        mode, color = ("ADAPTIVE AI CONTROL", LIME) if sim.adaptive else ("FIXED TIMERS (baseline)", AMBER)
        screen.blit(self.title.render(mode, True, color), (x, y))
        state = "PAUSED" if ui.paused else f"{ui.speed:g}x"
        t = f"t={sim.t:6.1f}s  {state}"
        screen.blit(self.font.render(t, True, TEXT), (x + w - self.font.size(t)[0], y))
        y += 22
        net = sim.net
        junctions = sum(n.signalised for n in net.nodes)
        y = self._line(
            screen,
            f"Iași central spine: {junctions} smart junctions, {len(net.segments)} edges, {len(net.routes)} CTP lines",
            x,
            y,
            w,
            DIM,
            self.small,
        )
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
            f"cars {int(m['cars'])}  speed {m['car_speed']:4.0%}  exited {m['throughput']:4.0f}  queued at gates {int(m['queued_outside'])}  adjustments {int(m['overrides'])}",
            x,
            y,
            w,
            DIM,
            self.small,
        )
        return y

    def _buses(self, screen, sim: Simulation, ui: UiState, x: int, y: int, w: int) -> int:
        y = self._heading(
            screen,
            f"LIVE CTP BUS TELEMETRY  ({sim.api.published:,} msgs, {len(sim.fleet.buses)} buses, {1 / sim.cfg.dt:.0f} Hz)",
            x,
            y,
            w,
        )
        y = self._line(screen, "BUS   LINE  SPEED   TO LINE  DELAY   AI: EXTRA DELAY AHEAD", x, y, w, DIM, self.small)
        for r in ui.rows:
            color = speed_color(r.ratio)
            text = f"{r.bus_id:<5s} {r.line:<4s} {r.speed_kmh:4.0f}km/h {r.dist:5.0f}m {r.delay:5.0f}s   {'+' if r.predicted else ' '}{r.predicted:4.0f}s"
            screen.blit(self.small.render(text, True, color), (x + 14, y))
            pygame.draw.rect(screen, line_color(sim, r.line), (x, y + 1, 8, 9))
            y += self.small.get_linesize() + 1
        return y + (10 - len(ui.rows)) * (self.small.get_linesize() + 1)

    def _predictions(self, screen, sim: Simulation, x: int, y: int, w: int) -> int:
        p = sim.predictor
        y = self._heading(screen, "AI PREDICTIONS (along each bus's route)", x, y, w)
        y = self._line(
            screen,
            f"model {p.model_name}  speed MAE {p.mae_speed * 3.6:4.1f} km/h  ETA err {p.mae_eta:4.1f}s  resolved {p.resolved}",
            x,
            y,
            w,
            DIM,
            self.small,
        )
        upcoming = [f for f in p.active(sim.t) if f.hop == 1][:4]
        if not upcoming:
            y = self._line(screen, "(waiting for a bus to clear a junction)", x, y, w, DIM, self.small)
        for f in upcoming:
            seg = sim.net.segments[f.target_segment]
            color = RED if f.predicted_delay > 20 else AMBER if f.predicted_delay > 6 else LIME
            eta = f"ETA {f.eta - sim.t:3.0f}s" if f.eta > sim.t else "at line"
            name = (seg.name or seg.highway)[:20]
            y = self._line(
                screen,
                f"{f.source_bus} L{f.line:<3s} -> n{seg.to_node} {name:<20s} +{f.predicted_delay:3.0f}s {eta:>7s} c{f.confidence:.2f}",
                x,
                y,
                w,
                color,
                self.small,
            )
        for msg in list(p.recent_resolved)[-1:]:
            y = self._line(screen, "done: " + msg, x, y, w, DIM, self.small)
        return y

    def _loops(self, screen, sim: Simulation, x: int, y: int, w: int) -> int:
        y = self._heading(screen, "VIRTUAL DETECTION LOOPS (50 / 150 / 300 m)", x, y, w)
        ranked = sorted(
            (
                (r.density or 0.0, sid, r)
                for sid, r in sim.loops.readings.items()
                if sim.net.segments[sid].to_node in sim.controllers
            ),
            key=lambda t: -t[0],
        )[:3]
        if not ranked:
            y = self._line(screen, "(no bus inside a window)", x, y, w, DIM, self.small)
        for dens, sid, r in ranked:
            seg = sim.net.segments[sid]
            color = RED if dens > 0.6 else AMBER if dens > 0.3 else LIME
            y = self._line(
                screen,
                f"n{seg.to_node} {(seg.name or seg.highway)[:20]:<20s} buses {r.count[0]}/{r.count[1]}/{r.count[2]}  load {dens:4.2f}",
                x,
                y,
                w,
                color,
                self.small,
            )
        return y + (3 - len(ranked)) * (self.small.get_linesize() + 1)

    def _overrides(self, screen, sim: Simulation, x: int, y: int, w: int) -> int:
        y = self._heading(screen, "AUTOMATED CYCLE ADJUSTMENTS (overrides of the standard timer)", x, y, w)
        room = (self.rect.bottom - 52 - y) // (self.small.get_linesize() + 1)
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
        self._line(
            screen,
            "SPACE pause  B adaptive/fixed  I incident  1 2 4 8 speed  H heat  L bus lines  R reset",
            x,
            y,
            w,
            DIM,
            self.small,
        )
        self._line(
            screen,
            "mouse: wheel zoom, drag pan   F fit map   +/- zoom   arrows pan   Q quit",
            x,
            y + 14,
            w,
            DIM,
            self.small,
        )
