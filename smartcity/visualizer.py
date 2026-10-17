"""Pygame renderer: a dark smart-city dashboard over the real road network, with pan and zoom."""

from __future__ import annotations

import math
import time

import pygame

from .hud import (
    AMBER,
    BG,
    CYAN,
    DIM,
    KIND_COLOR,
    LIME,
    RED,
    Hud,
    UiState,
    line_color,
    speed_color,
)
from .network import RoadNetwork
from .simulation import Simulation
from .traffic_model import Vehicle

ASPHALT = (64, 71, 88)
ASPHALT_EDGE = (30, 34, 46)
NODE_RING = (70, 80, 104)
SIGNAL = {"G": (57, 255, 20), "Y": (255, 225, 0), "R": (255, 23, 68)}  # neon
CAR = (150, 205, 255)
CAR_OUTLINE = (12, 18, 30)
ROAD_WIDTH_M = {"primary": 11.0, "secondary": 10.0, "tertiary": 9.0}  # a lane each way plus a margin
RING_KINDS = {"LOCAL_EXTEND", "PREDICTIVE_EXTEND", "PREDICTIVE_SWITCH", "THROTTLE", "EARLY_SWITCH"}


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    k = max(0.0, min(1.0, k))
    return (int(a[0] + (b[0] - a[0]) * k), int(a[1] + (b[1] - a[1]) * k), int(a[2] + (b[2] - a[2]) * k))


class Camera:
    """World metres (east, north of the map centre) <-> screen pixels, with zoom about the cursor."""

    MIN_SCALE, MAX_SCALE = 0.05, 8.0

    def __init__(self, rect: pygame.Rect, net: RoadNetwork):
        self.rect, self.net = rect, net
        self.cx = self.cy = 0.0
        self.scale = 0.2
        self.version = 0
        self.fit()

    def fit(self) -> None:
        x0, y0, x1, y1 = self.net.bounds
        self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.scale = 0.94 * min(self.rect.width / max(1.0, x1 - x0), self.rect.height / max(1.0, y1 - y0))
        self.version += 1

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.rect.centerx + (x - self.cx) * self.scale, self.rect.centery - (y - self.cy) * self.scale

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return self.cx + (sx - self.rect.centerx) / self.scale, self.cy - (sy - self.rect.centery) / self.scale

    def latlon_to_screen(self, lat: float, lon: float) -> tuple[float, float]:
        """Latitude/longitude to 2D screen coordinates (equirectangular projection, then pan and zoom)."""
        return self.to_screen(*self.net.proj.to_xy(lat, lon))

    def zoom_at(self, sx: float, sy: float, factor: float) -> None:
        wx, wy = self.to_world(sx, sy)
        self.scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self.scale * factor))
        self.cx = wx - (sx - self.rect.centerx) / self.scale
        self.cy = wy + (sy - self.rect.centery) / self.scale
        self.version += 1

    def pan_pixels(self, dx: float, dy: float) -> None:
        self.cx -= dx / self.scale
        self.cy += dy / self.scale
        self.version += 1


class CityView:
    def __init__(self, sim: Simulation, rect: pygame.Rect):
        self.rect = rect
        self.camera = Camera(rect, sim.net)
        names = "menlo,dejavusansmono,consolas,monospace"
        self.label = pygame.font.SysFont(names, 10)
        self.chip = pygame.font.SysFont(names, 10, bold=True)
        self.banner = pygame.font.SysFont(names, 15, bold=True)
        self._layer = pygame.Surface(rect.size)
        self._layer_state: tuple | None = None  # (cx, cy, scale, show_lines)
        self._layer_time = 0.0
        self._chips: dict[str, pygame.Surface] = {}

    # -- input helpers -------------------------------------------------------
    def zoom(self, pos: tuple[int, int], factor: float) -> None:
        if self.rect.collidepoint(pos):
            self.camera.zoom_at(pos[0], pos[1], factor)

    def zoom_centre(self, factor: float) -> None:
        self.camera.zoom_at(self.rect.centerx, self.rect.centery, factor)

    def drag(self, rel: tuple[int, int]) -> None:
        self.camera.pan_pixels(*rel)

    def fit(self) -> None:
        self.camera.fit()

    # -- frame ---------------------------------------------------------------
    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        screen.set_clip(self.rect)
        self._draw_layer(screen, sim, ui)
        self._heat(screen, sim, ui)
        self._junctions(screen, sim)
        self._forecasts(screen, sim)
        self._rings(screen, sim)
        self._vehicles(screen, sim)
        self._legend(screen, sim, ui)
        screen.set_clip(None)

    # -- cached road layer ------------------------------------------------------
    def _draw_layer(self, screen, sim: Simulation, ui: UiState) -> None:
        cam = self.camera
        state = (cam.cx, cam.cy, cam.scale, ui.show_lines)
        if state != self._layer_state:
            panned_only = self._layer_state is not None and state[2:] == self._layer_state[2:]
            if panned_only and time.time() - self._layer_time < 0.1:
                # dragging: reuse the old layer, shifted, and re-render once the view settles
                ox = (self._layer_state[0] - cam.cx) * cam.scale
                oy = -(self._layer_state[1] - cam.cy) * cam.scale
                screen.fill(BG, self.rect)
                screen.blit(self._layer, (self.rect.x + ox, self.rect.y + oy))
                return
            self._render_layer(sim, ui)
            self._layer_state, self._layer_time = state, time.time()
        screen.blit(self._layer, self.rect.topleft)

    def _poly(self, sim: Simulation, seg_id: int, shift_m: float = 0.0) -> list[tuple[float, float]]:
        """Screen-space polyline of a segment (relative to the layer), optionally shifted to its right."""
        seg, cam = sim.net.segments[seg_id], self.camera
        sx = sy = 0.0
        if shift_m:
            dx, dy = seg.points[-1][0] - seg.points[0][0], seg.points[-1][1] - seg.points[0][1]
            norm = math.hypot(dx, dy) or 1.0
            sx, sy = dy / norm * shift_m, -dx / norm * shift_m
        out = []
        for x, y in seg.points:
            a, b = cam.to_screen(x + sx, y + sy)
            out.append((a - self.rect.x, b - self.rect.y))
        return out

    def _render_layer(self, sim: Simulation, ui: UiState) -> None:
        surf, net, cam = self._layer, sim.net, self.camera
        surf.fill(BG)
        twin = {(s.from_node, s.to_node): s.id for s in net.segments}
        drawn: list[tuple[list[tuple[float, float]], int]] = []
        for seg in net.segments:
            back = twin.get((seg.to_node, seg.from_node))
            if back is not None and back < seg.id:
                continue  # a two-way road is drawn once
            # exaggerate road width when zoomed out so the network stays readable; true scale when zoomed in
            exaggerate = 1.0 + 1.1 * max(0.0, min(1.0, (0.6 - cam.scale) / 0.5))
            width = max(3, int(exaggerate * ROAD_WIDTH_M.get(seg.highway.replace("_link", ""), 9.0) * cam.scale))
            drawn.append((self._poly(sim, seg.id), width))
        for pts, width in drawn:  # dark casing under every road so crossings read cleanly
            pygame.draw.lines(surf, ASPHALT_EDGE, False, pts, width + 2)
        for pts, width in drawn:
            pygame.draw.lines(surf, ASPHALT, False, pts, width)
            if width >= 5:
                for p in pts:
                    pygame.draw.circle(surf, ASPHALT, p, width // 2)
        if ui.show_lines:
            for k, route in enumerate(net.routes):
                color = line_color(sim, route.ref)
                shift = (2.0 + 2.0 * k) / cam.scale  # a few pixels to the right, a different offset per line
                for sid in route.edges:
                    pts = self._poly(sim, sid, shift)
                    if len(pts) >= 2:
                        pygame.draw.lines(surf, lerp(color, ASPHALT, 0.5), False, pts, 1)
        for name, lat, lon in net.landmarks_ll:
            sx, sy = cam.latlon_to_screen(lat, lon)
            px, py = sx - self.rect.x, sy - self.rect.y
            pygame.draw.circle(surf, CYAN, (px, py), 3, 1)
            surf.blit(self.label.render(name.upper(), True, (80, 140, 160)), (px + 6, py - 5))

    # -- dynamic layers ---------------------------------------------------------
    def _heat(self, screen, sim: Simulation, ui: UiState) -> None:
        if ui.heat == 0:
            return
        cam = self.camera
        for seg in sim.net.segments:
            load = sim.model.density(seg.id) if ui.heat == 1 else sim.coord.load[seg.id]
            if load < 0.15:
                continue
            color = lerp(ASPHALT, speed_color(1.0 - load), min(1.0, load * 1.5))
            pts = [cam.to_screen(*sim.net.position(seg.id, p)[:2]) for p in (seg.stop_pos * k / 4 for k in range(5))]
            pygame.draw.lines(screen, color, False, pts, max(3, int(6 * cam.scale)))

    def _junctions(self, screen, sim: Simulation) -> None:
        cam, net = self.camera, sim.net
        r = max(2, int(sim.cfg.box_half * cam.scale))
        half = max(3.0, 3.2 * cam.scale) / cam.scale  # stop-line half width in metres
        for node in net.nodes:
            cx, cy = cam.to_screen(node.x, node.y)
            if not self.rect.inflate(60, 60).collidepoint(cx, cy):
                continue
            if node.gate:
                pygame.draw.polygon(screen, CYAN, [(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)], 1)
                continue
            ctl = sim.controllers.get(node.id)
            if ctl is None:
                continue
            pygame.draw.circle(screen, NODE_RING, (cx, cy), r, 1)
            for seg_id in ctl.approaches:
                seg = net.segments[seg_id]
                x, y, dx, dy = net.position(seg_id, seg.stop_pos)
                a = cam.to_screen(x - dy * half, y + dx * half)
                b = cam.to_screen(x + dy * half, y - dx * half)
                pygame.draw.line(screen, SIGNAL[ctl.state_for(seg_id)], a, b, 4 if cam.scale > 0.4 else 3)

    def _forecasts(self, screen, sim: Simulation) -> None:
        cam = self.camera
        for f in sim.predictor.active(sim.t):
            if f.predicted_density < sim.cfg.pred_density_min:
                continue
            seg = sim.net.segments[f.target_segment]
            cx, cy = cam.to_screen(*sim.net.position(seg.id, max(0.0, seg.stop_pos - 25))[:2])
            r = int(5 + 7 * f.confidence * (1.0 if f.hop == 1 else 0.7))
            pygame.draw.polygon(screen, CYAN, [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], 2)

    def _rings(self, screen, sim: Simulation) -> None:
        cam = self.camera
        pulse = 0.5 + 0.5 * math.sin(sim.t * 6.0)
        seen: set[int] = set()
        for e in reversed(sim.events.since(sim.t - 3.0)):
            if e.node is None or e.kind not in RING_KINDS or e.node in seen:
                continue
            seen.add(e.node)
            node = sim.net.nodes[e.node]
            cx, cy = cam.to_screen(node.x, node.y)
            age = (sim.t - e.t) / 3.0
            r = int(14 + 6 * pulse + 10 * age)
            pygame.draw.circle(screen, KIND_COLOR.get(e.kind, AMBER), (cx, cy), r, 2)

    def _body(self, screen, sim, v: Vehicle, length_px: float, width_px: float, color, outline) -> tuple[float, float]:
        x, y, dx, dy = sim.net.position(v.seg, v.pos - v.length / 2)
        cx, cy = self.camera.to_screen(x, y)
        ux, uy = dx, -dy  # heading on screen
        nx, ny = -uy, ux
        hl, hw = length_px / 2, width_px / 2
        pts = [
            (cx + ux * hl + nx * hw, cy + uy * hl + ny * hw),
            (cx + ux * hl - nx * hw, cy + uy * hl - ny * hw),
            (cx - ux * hl - nx * hw, cy - uy * hl - ny * hw),
            (cx - ux * hl + nx * hw, cy - uy * hl + ny * hw),
        ]
        pygame.draw.polygon(screen, color, pts)
        pygame.draw.polygon(screen, outline, pts, 1)
        return cx, cy

    def _chip(self, ref: str, color) -> pygame.Surface:
        key = f"{ref}{color}"
        if key not in self._chips:
            text = self.chip.render(ref, True, (8, 10, 16))
            surf = pygame.Surface((text.get_width() + 6, text.get_height() + 2), pygame.SRCALPHA)
            pygame.draw.rect(surf, color, surf.get_rect(), border_radius=3)
            surf.blit(text, (3, 1))
            self._chips[key] = surf
        return self._chips[key]

    def _vehicles(self, screen, sim: Simulation) -> None:
        cam = self.camera
        view = self.rect.inflate(40, 40)
        buses = []
        for v in sim.model.iter_vehicles():
            if v.kind == "blocker":
                cx, cy = self._body(screen, sim, v, 9.0, 6.0, (150, 20, 40), RED)
                pygame.draw.line(screen, RED, (cx - 7, cy - 7), (cx + 7, cy + 7), 3)
                pygame.draw.line(screen, RED, (cx - 7, cy + 7), (cx + 7, cy - 7), 3)
            elif v.kind == "bus":
                buses.append(v)
            else:
                x, y, _, _ = sim.net.position(v.seg, v.pos)
                if view.collidepoint(cam.to_screen(x, y)):
                    self._body(
                        screen,
                        sim,
                        v,
                        max(6.0, v.length * cam.scale),
                        max(4.0, 2.0 * cam.scale * 1.3),
                        CAR,
                        CAR_OUTLINE,
                    )
        for v in buses:  # buses on top, coloured by speed, tagged with their line number
            seg = sim.net.segments[v.seg]
            cx, cy = self._body(
                screen,
                sim,
                v,
                max(10.0, v.length * cam.scale),
                max(4.5, 2.6 * cam.scale * 1.3),
                speed_color(v.v / seg.speed_limit),
                (245, 250, 255),
            )
            screen.blit(self._chip(v.line or "?", line_color(sim, v.line or "")), (cx + 6, cy - 14))

    def _legend(self, screen, sim: Simulation, ui: UiState) -> None:
        x, y = self.rect.x + 14, self.rect.y + 10
        mode, color = ("ADAPTIVE", LIME) if sim.adaptive else ("FIXED TIMERS", AMBER)
        screen.blit(self.banner.render(mode, True, color), (x, y))
        heat = ("", "link occupancy: true (hidden)", "link load: estimated from buses")[ui.heat]
        if heat:
            screen.blit(self.label.render(heat, True, DIM), (x, y + 20))
        rows = [
            (CAR, "car"),
            (speed_color(1.0), "bus: free flow"),
            (speed_color(0.5), "bus: slowing"),
            (speed_color(0.0), "bus: stopped"),
        ]
        base = self.rect.bottom - 14 * len(rows) - 10
        for i, (c, text) in enumerate(rows):
            pygame.draw.rect(screen, c, (x, base + 14 * i + 1, 10, 9))
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x + 15, base + 14 * i))
        x2 = x + 140
        for i, (state, text) in enumerate((("G", "green"), ("Y", "yellow"), ("R", "red / all-red"))):
            pygame.draw.line(screen, SIGNAL[state], (x2, base + 14 * i + 6), (x2 + 12, base + 14 * i + 6), 4)
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x2 + 18, base + 14 * i))
        pygame.draw.polygon(
            screen, CYAN, [(x2 + 5, base + 49), (x2 + 10, base + 54), (x2 + 5, base + 59), (x2, base + 54)], 1
        )
        screen.blit(self.label.render("gate / AI forecast", True, (170, 176, 195)), (x2 + 18, base + 47))
        for i, (kind, text) in enumerate(
            (("LOCAL_EXTEND", "bus extension"), ("PREDICTIVE_EXTEND", "AI override"), ("THROTTLE", "throttle"))
        ):
            pygame.draw.circle(screen, KIND_COLOR[kind], (x2 + 150, base + 14 * i + 6), 5, 2)
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x2 + 160, base + 14 * i))
        if ui.show_lines:
            for k, route in enumerate(sim.net.routes[:8]):
                col, row = divmod(k, 4)
                px = x2 + 270 + col * 70
                screen.blit(self._chip(route.ref, line_color(sim, route.ref)), (px, base + 14 * row))
        scale_m = 500
        bar = scale_m * self.camera.scale
        by = self.rect.bottom - 10
        bx = self.rect.right - 20 - bar
        pygame.draw.line(screen, DIM, (bx, by), (bx + bar, by), 2)
        txt = self.label.render(f"{scale_m} m", True, DIM)
        screen.blit(txt, (bx + bar - txt.get_width(), by - 14))


WINDOW = (1440, 860)
HUD_WIDTH = 560


def make_views(sim: Simulation) -> tuple[CityView, Hud]:
    city = CityView(sim, pygame.Rect(0, 0, WINDOW[0] - HUD_WIDTH, WINDOW[1]))
    hud = Hud(pygame.Rect(WINDOW[0] - HUD_WIDTH, 0, HUD_WIDTH, WINDOW[1]))
    return city, hud
