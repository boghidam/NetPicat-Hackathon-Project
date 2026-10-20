"""Professional operations dashboard: live CTP bus telemetry, AI predictions, detection loops, automated cycle adjustments."""

from __future__ import annotations

from dataclasses import dataclass, field

import pygame

from .events import OVERRIDE_KINDS
from .simulation import Simulation

# --- Refined Mission-Control Dark Theme ---------------------------------------
BG = (11, 13, 19)  # Deep slate canvas background
PANEL = (17, 21, 30)  # Operations panel background
PANEL_BORDER = (34, 42, 58)  # Subtle panel boundary
CARD_BG = (22, 27, 38)  # Structured card container
CARD_BORDER = (36, 45, 62)  # Clean card outline
CARD_HEADER_BG = (27, 34, 48)  # Card header surface
ROW_ALT = (19, 24, 34)  # Alternating table row background

# Typography colors
TEXT = (240, 244, 250)  # Crisp primary off-white
TEXT_MUTED = (142, 154, 175)  # Secondary descriptive labels
TEXT_DIM = (96, 108, 128)  # Muted units & metadata
DIM = TEXT_MUTED  # Backward compatibility alias
RULE = (34, 42, 58)  # Hairline card divider

# Semantic status accents
LIME = (34, 197, 94)  # Emerald green (normal / adaptive / free-flow)
AMBER = (245, 158, 11)  # Warm amber (warning / slowing / baseline)
RED = (239, 68, 68)  # Crimson (congestion / incident / stalled)
CYAN = (14, 165, 233)  # Technical blue (telemetry / forecasts)
MAGENTA = (236, 72, 153)  # Rose (metering / throttling)
ORANGE = (249, 115, 22)  # Amber orange (extensions)
VIOLET = (139, 92, 246)  # Violet

# CTP line palette: one distinct colour per line (assigned in route order), enough for every line in the city
LINE_COLORS = [
    (14, 165, 233),  # sky blue
    (168, 85, 247),  # purple
    (249, 115, 22),  # orange
    (34, 197, 94),  # emerald
    (236, 72, 153),  # rose
    (234, 179, 8),  # gold
    (99, 102, 241),  # indigo
    (20, 184, 166),  # teal
    (163, 230, 53),  # lime
    (34, 211, 238),  # cyan
    (248, 113, 113),  # coral
    (232, 121, 249),  # fuchsia
    (59, 130, 246),  # blue
    (134, 239, 172),  # mint
    (196, 181, 253),  # lavender
    (253, 186, 116),  # apricot
    (125, 211, 252),  # ice
    (254, 240, 138),  # lemon
    (94, 234, 212),  # aqua
    (190, 150, 110),  # sand
]

KIND_COLOR = {
    "BUS_HOLD": LIME,
    "LOCAL_EXTEND": ORANGE,
    "PREDICTIVE_EXTEND": CYAN,
    "THROTTLE": MAGENTA,
    "BUDGET_DENIED": TEXT_DIM,
    "INCIDENT": RED,
    "INFO": TEXT,
}
KIND_LABEL = {
    "BUS_HOLD": "HOLD",
    "LOCAL_EXTEND": "EXTEND",
    "PREDICTIVE_EXTEND": "PRED-EXT",
    "THROTTLE": "THROTTLE",
    "BUDGET_DENIED": "BUDGET",
}
LOG_KINDS = OVERRIDE_KINDS | {"INCIDENT", "INFO", "BUDGET_DENIED"}


def speed_color(ratio: float) -> tuple[int, int, int]:
    """Smooth semantic ramp: Crimson (stopped) -> Amber (slowed) -> Emerald (free flow)."""
    r = max(0.0, min(1.0, ratio))
    if r < 0.5:
        k = r / 0.5
        return (
            int(239 + (245 - 239) * k),
            int(68 + (158 - 68) * k),
            int(68 + (11 - 68) * k),
        )
    k = (r - 0.5) / 0.5
    return (
        int(245 + (34 - 245) * k),
        int(158 + (197 - 158) * k),
        int(11 + (94 - 11) * k),
    )


def line_color(sim: Simulation, ref: str) -> tuple[int, int, int]:
    i = sim.net.route_index.get(ref)
    return TEXT_MUTED if i is None else LINE_COLORS[i % len(LINE_COLORS)]


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
    if latest:
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
    else:
        for b in sim.fleet.buses:
            seg = sim.net.segments[b.seg]
            rows.append(
                BusRow(
                    b.id,
                    b.line or "",
                    b.v * 3.6,
                    b.v / seg.speed_limit,
                    min(seg.stop_pos, max(0.0, seg.stop_pos - b.pos)),
                    b.delay,
                    0.0,
                )
            )
    rows.sort(key=lambda r: (-r.predicted, -r.delay))
    return rows[:n]


class CachedFont:
    """A pygame font that remembers what it measured and drew.

    The HUD and the map labels draw hundreds of short strings every frame, most of them identical from one
    frame to the next. Measuring and rendering are the slow parts, so both are memoised (bounded, so a
    stream of ever-changing numbers cannot grow the cache without limit). Rendered surfaces are shared and
    must be treated as read-only; every other font method falls through to the real font.
    """

    LIMIT = 1500

    def __init__(self, font: pygame.font.Font):
        self._font = font
        self._sizes: dict[str, tuple[int, int]] = {}
        self._images: dict[tuple, pygame.Surface] = {}

    def size(self, text: str) -> tuple[int, int]:
        got = self._sizes.get(text)
        if got is None:
            if len(self._sizes) >= self.LIMIT:
                self._sizes.clear()
            got = self._sizes[text] = self._font.size(text)
        return got

    def render(self, text: str, antialias: bool, color, background=None) -> pygame.Surface:
        key = (text, antialias, tuple(color), None if background is None else tuple(background))
        img = self._images.get(key)
        if img is None:
            if len(self._images) >= self.LIMIT:
                self._images.clear()
            img = self._images[key] = (
                self._font.render(text, antialias, color)
                if background is None
                else self._font.render(text, antialias, color, background)
            )
        return img

    def __getattr__(self, name: str):
        return getattr(self._font, name)


def sysfont(names: str, size: int, bold: bool = False) -> CachedFont:
    return CachedFont(pygame.font.SysFont(names, size, bold=bold))


class Hud:
    def __init__(self, rect: pygame.Rect):
        self.rect = rect
        sans = "helveticaneue,segoeui,inter,arial,sans-serif"
        mono = "menlo,jetbrainsmono,consolas,dejavusansmono,monospace"

        # Typographic scale
        self.font_brand = sysfont(sans, 13, bold=True)
        self.font_card_title = sysfont(sans, 11, bold=True)
        self.font_sans_sub = sysfont(sans, 10)
        self.font_sans_bold = sysfont(sans, 10, bold=True)
        self.font_badge = sysfont(sans, 9, bold=True)

        self.font_kpi_val = sysfont(sans, 16, bold=True)
        self.font_kpi_lbl = sysfont(sans, 9, bold=True)
        self.font_kpi_sub = sysfont(sans, 9)

        self.font_mono = sysfont(mono, 11)
        self.font_mono_bold = sysfont(mono, 11, bold=True)
        self.font_mono_small = sysfont(mono, 10)

        # Backward compatibility for existing code / tests
        self.font = self.font_mono
        self.small = self.font_mono_small
        self.title = self.font_brand

    def fit(self, text: str, font: pygame.font.Font, width: int) -> str:
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        # Panel container & left separator
        pygame.draw.rect(screen, PANEL, self.rect)
        pygame.draw.line(screen, PANEL_BORDER, self.rect.topleft, self.rect.bottomleft, 1)

        x = self.rect.x + 14
        w = self.rect.width - 28

        # Vertical layout
        y = self._draw_header(screen, sim, ui, x, self.rect.y + 10, w)
        y = self._draw_kpi_row(screen, sim, x, y + 8, w)
        y = self._draw_buses(screen, sim, ui, x, y + 8, w)
        y = self._draw_predictions(screen, sim, x, y + 8, w)
        y = self._draw_loops(screen, sim, x, y + 8, w)
        y = self._draw_overrides(screen, sim, x, y + 8, w)
        self._draw_controls(screen, x, self.rect.bottom - 66, w)

    # -- Primitives & Micro-Components -----------------------------------------
    def _card(
        self,
        screen: pygame.Surface,
        x: int,
        y: int,
        w: int,
        h: int,
        title: str | None = None,
        badge: str | None = None,
        badge_color: tuple[int, int, int] = TEXT_DIM,
    ) -> int:
        rect = pygame.Rect(x, y, w, h)
        pygame.draw.rect(screen, CARD_BG, rect, border_radius=6)
        pygame.draw.rect(screen, CARD_BORDER, rect, width=1, border_radius=6)
        if title:
            # Header area
            header_rect = pygame.Rect(x, y, w, 24)
            pygame.draw.rect(screen, CARD_HEADER_BG, header_rect, border_top_left_radius=6, border_top_right_radius=6)
            pygame.draw.line(screen, CARD_BORDER, (x, y + 24), (x + w, y + 24), 1)

            # Title with small accent dot
            pygame.draw.circle(screen, CYAN, (x + 10, y + 12), 3)
            screen.blit(self.font_card_title.render(title.upper(), True, TEXT), (x + 18, y + 5))

            if badge:
                bw, bh = self.font_badge.size(badge)
                bx = x + w - bw - 14
                by = y + 5
                b_rect = pygame.Rect(bx - 4, by - 1, bw + 8, bh + 2)
                pygame.draw.rect(screen, (32, 39, 54), b_rect, border_radius=3)
                screen.blit(self.font_badge.render(badge, True, badge_color), (bx, by))
            return y + 30
        return y + 6

    def _badge(
        self,
        screen: pygame.Surface,
        text: str,
        x: int,
        y: int,
        fg_color: tuple[int, int, int],
        bg_color: tuple[int, int, int],
        border_color: tuple[int, int, int] | None = None,
    ) -> int:
        tw, th = self.font_badge.size(text)
        rect = pygame.Rect(x, y, tw + 8, th + 4)
        pygame.draw.rect(screen, bg_color, rect, border_radius=3)
        if border_color:
            pygame.draw.rect(screen, border_color, rect, width=1, border_radius=3)
        screen.blit(self.font_badge.render(text, True, fg_color), (x + 4, y + 2))
        return rect.right

    def _micro_bar(
        self, screen: pygame.Surface, x: int, y: int, w: int, h: int, ratio: float, color: tuple[int, int, int]
    ) -> None:
        pygame.draw.rect(screen, (32, 40, 56), (x, y, w, h), border_radius=2)
        filled = max(2, int(w * max(0.0, min(1.0, ratio))))
        pygame.draw.rect(screen, color, (x, y, filled, h), border_radius=2)

    # -- Header & Top KPI Row --------------------------------------------------
    def _draw_header(self, screen: pygame.Surface, sim: Simulation, ui: UiState, x: int, y: int, w: int) -> int:
        # Brand title & Municipal identification
        screen.blit(self.font_brand.render("IAȘI METROPOLITAN TRANSIT", True, TEXT), (x, y))
        screen.blit(self.font_sans_sub.render("CTP FLEET AVL · ADAPTIVE SIGNAL CONTROL", True, TEXT_MUTED), (x, y + 16))

        # Status pills on right
        if sim.adaptive:
            mode_text = "● ADAPTIVE TSP"
            fg, bg, bd = (52, 211, 153), (18, 48, 34), (34, 197, 94)
        else:
            mode_text = "● FIXED BASELINE"
            fg, bg, bd = (251, 191, 36), (50, 38, 18), (245, 158, 11)

        rx = x + w - self.font_badge.size(mode_text)[0] - 12
        self._badge(screen, mode_text, rx, y, fg, bg, bd)

        speed_lbl = "PAUSED" if ui.paused else f"{ui.speed:g}× SPEED"
        time_str = f"t={sim.t:5.1f}s  {speed_lbl}"
        screen.blit(
            self.font_mono_small.render(time_str, True, TEXT_MUTED),
            (x + w - self.font_mono_small.size(time_str)[0], y + 17),
        )

        return y + 36

    def _draw_kpi_row(self, screen: pygame.Surface, sim: Simulation, x: int, y: int, w: int) -> int:
        m = sim.metrics()
        gap = 8
        card_w = (w - gap * 2) // 3
        card_h = 48

        kpis = [
            (
                "BUS TIME LOSS",
                f"{m['bus_delay_ratio']:.1%}",
                f"Recent: {m['bus_delay_recent']:.1%}",
                AMBER if m["bus_delay_ratio"] > 0.35 else LIME,
            ),
            (
                "TRAFFIC FLOW",
                f"{m['car_speed']:.0%}",
                f"{int(m['cars'])} cars · {int(m['throughput'])} out · {int(m['queued_outside'])} queued",
                CYAN,
            ),
            (
                "TSP OVERRIDES",
                f"{int(m['overrides'])}",
                "Extend & Throttle cycles",
                ORANGE if sim.adaptive else TEXT_DIM,
            ),
        ]

        for i, (label, val, sub, col) in enumerate(kpis):
            cx = x + i * (card_w + gap)
            rect = pygame.Rect(cx, y, card_w, card_h)
            pygame.draw.rect(screen, CARD_BG, rect, border_radius=5)
            pygame.draw.rect(screen, CARD_BORDER, rect, width=1, border_radius=5)

            screen.blit(self.font_kpi_lbl.render(label, True, TEXT_MUTED), (cx + 8, y + 4))
            screen.blit(self.font_kpi_val.render(val, True, col), (cx + 8, y + 15))
            screen.blit(self.font_kpi_sub.render(sub, True, TEXT_DIM), (cx + 8, y + 33))

        return y + card_h

    # -- Live Bus Telemetry Card -----------------------------------------------
    def _draw_buses(self, screen: pygame.Surface, sim: Simulation, ui: UiState, x: int, y: int, w: int) -> int:
        h = 246
        inner_y = self._card(
            screen,
            x,
            y,
            w,
            h,
            title="Live CTP Bus Telemetry",
            badge=f"{sim.api.published:,} MSGS · {len(sim.fleet.buses)} BUSES · 10 HZ",
            badge_color=CYAN,
        )

        # Table Column Headers
        col_bus = x + 10
        col_line = x + 44
        col_spd = x + 96
        col_dist = x + 188
        col_dly = x + 266
        col_ai = x + 342
        col_bar = x + 460

        headers = [
            (col_bus, "BUS"),
            (col_line, "LINE"),
            (col_spd, "SPEED"),
            (col_dist, "TO STOP"),
            (col_dly, "DELAY"),
            (col_ai, "AI DELAY AHEAD"),
            (col_bar, "FLOW"),
        ]
        for col_x, name in headers:
            screen.blit(self.font_badge.render(name, True, TEXT_DIM), (col_x, inner_y))

        inner_y += 14
        row_h = 20

        visible_rows = ui.rows[:9]
        for idx, r in enumerate(visible_rows):
            ry = inner_y + idx * row_h
            if idx % 2 == 1:
                pygame.draw.rect(screen, ROW_ALT, (x + 4, ry - 1, w - 8, row_h), border_radius=3)

            # Bus ID
            screen.blit(self.font_mono.render(r.bus_id, True, TEXT), (col_bus, ry + 2))

            # Transit Line Pill Badge
            lc = line_color(sim, r.line)
            line_w, line_h = self.font_mono_small.size(r.line)
            l_rect = pygame.Rect(col_line, ry + 2, line_w + 6, line_h + 1)
            pygame.draw.rect(screen, lc, l_rect, border_radius=2)
            screen.blit(self.font_mono_small.render(r.line, True, (12, 16, 24)), (col_line + 3, ry + 2))

            # Speed with right alignment
            sc = speed_color(r.ratio)
            spd_text = f"{r.speed_kmh:4.0f} km/h"
            screen.blit(self.font_mono.render(spd_text, True, sc), (col_spd, ry + 2))

            # Distance to stop line
            dist_text = f"{r.dist:5.0f} m"
            screen.blit(self.font_mono.render(dist_text, True, TEXT_MUTED), (col_dist, ry + 2))

            # Accumulated Delay
            dly_text = f"{r.delay:5.0f} s"
            dly_col = RED if r.delay > 60 else (AMBER if r.delay > 20 else TEXT_MUTED)
            screen.blit(self.font_mono.render(dly_text, True, dly_col), (col_dly, ry + 2))

            # AI predicted extra delay ahead
            if r.predicted > 0.5:
                pred_text = f"+{r.predicted:4.0f} s"
                pred_col = RED if r.predicted > 20 else AMBER
            else:
                pred_text = "   —"
                pred_col = TEXT_DIM
            screen.blit(self.font_mono.render(pred_text, True, pred_col), (col_ai, ry + 2))

            # Speed Flow Micro-bar
            self._micro_bar(screen, col_bar, ry + 6, 52, 6, r.ratio, sc)

        return y + h

    # -- AI Route Forecasts Card -----------------------------------------------
    def _draw_predictions(self, screen: pygame.Surface, sim: Simulation, x: int, y: int, w: int) -> int:
        p = sim.predictor
        h = 104
        inner_y = self._card(
            screen,
            x,
            y,
            w,
            h,
            title="AI Route Predictions (Feed-Forward)",
            badge=f"{p.model_name} · MAE {p.mae_speed * 3.6:3.1f} KM/H · ETA ±{p.mae_eta:3.1f}S · {p.resolved} DONE",
            badge_color=CYAN,
        )

        upcoming = sorted(
            (f for f in p.active(sim.t) if f.hop == 1),
            key=lambda f: (-round(f.predicted_delay), f.eta),
        )[:3]
        if not upcoming:
            screen.blit(
                self.font_mono_small.render(
                    "● Waiting for transit vehicles to clear approach stop lines…", True, TEXT_DIM
                ),
                (x + 12, inner_y + 12),
            )
        else:
            for idx, f in enumerate(upcoming):
                fy = inner_y + idx * 16
                seg = sim.net.segments[f.target_segment]
                color = RED if f.predicted_delay > 20 else (AMBER if f.predicted_delay > 6 else LIME)
                eta_s = f"{f.eta - sim.t:3.0f}s" if f.eta > sim.t else "at line"
                name = (seg.name or seg.highway)[:17]

                # Ticket line: Bus -> Node Street | Delay | ETA | Conf
                prefix = f"{f.source_bus} L{f.line:<2s} → n{seg.to_node:<3d} {name:<17s}"
                screen.blit(self.font_mono_small.render(prefix, True, TEXT), (x + 10, fy))

                stats = f"+{f.predicted_delay:2.0f}s delay  ETA {eta_s:>6s}  conf {f.confidence:.2f}"
                screen.blit(self.font_mono_small.render(stats, True, color), (x + 288, fy))

        # Recent resolved summary
        if p.recent_resolved:
            last = list(p.recent_resolved)[-1]
            ry = inner_y + 54
            pygame.draw.line(screen, RULE, (x + 8, ry - 3), (x + w - 8, ry - 3), 1)
            screen.blit(self.font_badge.render("RESOLVED:", True, TEXT_DIM), (x + 10, ry))
            screen.blit(
                self.font_mono_small.render(self.fit(last, self.font_mono_small, w - 76), True, TEXT_MUTED),
                (x + 70, ry - 1),
            )

        return y + h

    # -- Virtual Detection Loops Card ------------------------------------------
    def _draw_loops(self, screen: pygame.Surface, sim: Simulation, x: int, y: int, w: int) -> int:
        h = 92
        inner_y = self._card(
            screen,
            x,
            y,
            w,
            h,
            title="Virtual Detection Loops",
            badge="50 / 150 / 300 M WINDOWS",
            badge_color=TEXT_MUTED,
        )

        ranked = sorted(
            (
                (r.density or 0.0, sid, r)
                for sid, r in sim.loops.readings.items()
                if sim.net.segments[sid].to_node in sim.controllers
            ),
            key=lambda t: -t[0],
        )[:3]

        if not ranked:
            screen.blit(
                self.font_mono_small.render("● No transit vehicles currently inside detector windows", True, TEXT_DIM),
                (x + 12, inner_y + 12),
            )
        else:
            for idx, (dens, sid, r) in enumerate(ranked):
                ly = inner_y + idx * 17
                seg = sim.net.segments[sid]
                color = RED if dens > 0.6 else (AMBER if dens > 0.3 else LIME)
                name = (seg.name or seg.highway)[:19]

                txt = f"n{seg.to_node:<3d} {name:<19s}  buses [{r.count[0]}/{r.count[1]}/{r.count[2]}]"
                screen.blit(self.font_mono_small.render(txt, True, TEXT), (x + 10, ly))

                load_txt = f"{dens:.2f}"
                screen.blit(self.font_mono_small.render(load_txt, True, color), (x + 360, ly))
                self._micro_bar(screen, x + 400, ly + 4, 110, 6, dens, color)

        return y + h

    # -- Automated Cycle Adjustments (Override Log) ----------------------------
    def _draw_overrides(self, screen: pygame.Surface, sim: Simulation, x: int, y: int, w: int) -> int:
        h = max(168, self.rect.bottom - 76 - y)
        inner_y = self._card(
            screen,
            x,
            y,
            w,
            h,
            title="Automated Cycle Adjustments",
            badge=f"{sim.events.overrides} TOTAL OVERRIDES",
            badge_color=ORANGE if sim.events.overrides else TEXT_DIM,
        )

        max_rows = max(7, (h - 36) // 16)
        events = sim.events.recent(max_rows, LOG_KINDS)
        if not events:
            screen.blit(
                self.font_mono_small.render(
                    "● Standard fixed-time signal cycles running (no overrides required)", True, TEXT_DIM
                ),
                (x + 12, inner_y + 16),
            )
        else:
            for idx, e in enumerate(reversed(events)):
                ey = inner_y + idx * 16

                # Time tag
                t_str = f"{e.t:5.1f}s"
                screen.blit(self.font_mono_small.render(t_str, True, TEXT_DIM), (x + 10, ey))

                # Kind badge
                kind_lbl = KIND_LABEL.get(e.kind, e.kind)
                kind_col = KIND_COLOR.get(e.kind, TEXT)
                kw, kh = self.font_badge.size(kind_lbl)
                kb_rect = pygame.Rect(x + 58, ey, kw + 6, kh + 1)
                pygame.draw.rect(screen, (32, 40, 56), kb_rect, border_radius=2)
                screen.blit(self.font_badge.render(kind_lbl, True, kind_col), (x + 61, ey))

                # Description text
                desc = self.fit(e.text, self.font_mono_small, w - 140)
                screen.blit(self.font_mono_small.render(desc, True, TEXT_MUTED), (x + 130, ey))

        return y + h

    # -- Footer Quick Controls Bar ---------------------------------------------
    def _draw_controls(self, screen: pygame.Surface, x: int, y: int, w: int) -> None:
        pygame.draw.line(screen, PANEL_BORDER, (x, y - 4), (x + w, y - 4), 1)

        def key_pill(key: str, desc: str, px: int, py: int) -> int:
            kw, kh = self.font_badge.size(key)
            rect = pygame.Rect(px, py, kw + 6, kh + 2)
            pygame.draw.rect(screen, (34, 42, 60), rect, border_radius=3)
            pygame.draw.rect(screen, (52, 64, 88), rect, width=1, border_radius=3)
            screen.blit(self.font_badge.render(key, True, TEXT), (px + 3, py + 1))
            screen.blit(self.font_sans_sub.render(desc, True, TEXT_MUTED), (px + kw + 10, py))
            return px + kw + 10 + self.font_sans_sub.size(desc)[0] + 12

        # Row 1: Primary action keys
        px = x
        px = key_pill("SPACE", "Pause", px, y)
        px = key_pill("B", "Adaptive", px, y)
        px = key_pill("I", "Incident", px, y)
        px = key_pill("1-8", "Speed", px, y)
        px = key_pill("H", "Heat", px, y)
        px = key_pill("L", "Lines", px, y)
        key_pill("R", "Reset", px, y)

        # Row 2: Camera & navigation
        py = y + 18
        px = x
        px = key_pill("WHEEL", "Zoom", px, py)
        px = key_pill("DRAG", "Pan", px, py)
        px = key_pill("F", "Fit Map", px, py)
        px = key_pill("+ / -", "Scale", px, py)
        key_pill("Q", "Quit", px, py)

        # Row 3: traffic amount and rush hour
        py = y + 36
        px = x
        px = key_pill("[ ]", "Traffic less / more", px, py)
        px = key_pill("\\", "100%", px, py)
        px = key_pill("K", "Rush-hour key point", px, py)
        key_pill("CLICK", "Pick key point", px, py)
