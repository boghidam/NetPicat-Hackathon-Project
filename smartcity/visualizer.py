"""Pygame renderer: roads, junction boxes, stop-line signals, cars, buses and override highlights."""

from __future__ import annotations

import math

import pygame

from .hud import AMBER, BG, CYAN, GREEN, KIND_COLOR, RED, Hud, UiState, speed_color
from .simulation import Simulation
from .traffic_model import Vehicle

ROAD = (52, 56, 66)
ROAD_EDGE = (70, 75, 88)
BOX_FILL = (66, 70, 84)
STOP_LINE = {"G": (70, 230, 110), "Y": (250, 205, 50), "R": (235, 60, 60)}
CAR = (120, 165, 215)
CAR_OUTLINE = (20, 30, 45)
RING_KINDS = {"LOCAL_EXTEND", "PREDICTIVE_EXTEND", "PREDICTIVE_SWITCH", "THROTTLE", "SPILLBACK_HOLD", "EARLY_SWITCH"}


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    k = max(0.0, min(1.0, k))
    return (int(a[0] + (b[0] - a[0]) * k), int(a[1] + (b[1] - a[1]) * k), int(a[2] + (b[2] - a[2]) * k))


class CityView:
    def __init__(self, sim: Simulation, rect: pygame.Rect):
        self.rect = rect
        self.scale = min(rect.width / sim.grid.width, rect.height / sim.grid.height)
        self.ox = rect.x + (rect.width - sim.grid.width * self.scale) / 2
        self.oy = rect.y + (rect.height - sim.grid.height * self.scale) / 2
        self.label = pygame.font.SysFont("menlo,dejavusansmono,consolas,monospace", 10)
        self.banner = pygame.font.SysFont("menlo,dejavusansmono,consolas,monospace", 15, bold=True)

    def px(self, x: float, y: float) -> tuple[int, int]:
        return int(self.ox + x * self.scale), int(self.oy + y * self.scale)

    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        pygame.draw.rect(screen, BG, self.rect)
        self._roads(screen, sim)
        self._heat(screen, sim, ui)
        self._intersections(screen, sim)
        self._forecasts(screen, sim)
        self._rings(screen, sim)
        self._vehicles(screen, sim)
        self._legend(screen, sim, ui)

    # -- layers ------------------------------------------------------------------
    def _roads(self, screen, sim: Simulation) -> None:
        width = max(10, int(34 * self.scale))
        for seg in sim.grid.segments:
            if seg.kind == "road" and seg.from_node > seg.to_node:
                continue  # draw each two-way road once
            if seg.kind == "exit":
                continue  # same road as the matching entry
            a, b = sim.grid.coords[seg.from_node], sim.grid.coords[seg.to_node]
            pa, pb = self.px(*a), self.px(*b)
            pygame.draw.line(screen, ROAD, pa, pb, width)
            pygame.draw.line(screen, ROAD_EDGE, pa, pb, 1)

    def _heat(self, screen, sim: Simulation, ui: UiState) -> None:
        if ui.heat == 0:
            return
        for seg in sim.grid.segments:
            if seg.kind != "road":
                continue
            load = sim.model.density(seg.id) if ui.heat == 1 else sim.coord.load[seg.id]
            if load < 0.12:
                continue
            color = lerp(ROAD, speed_color(1.0 - load), min(1.0, load * 1.4))
            p0 = self.px(*sim.grid.position(seg.id, sim.cfg.box_half + 4))
            p1 = self.px(*sim.grid.position(seg.id, seg.stop_pos - 2))
            pygame.draw.line(screen, color, p0, p1, max(5, int(11 * self.scale)))

    def _intersections(self, screen, sim: Simulation) -> None:
        half = int(sim.cfg.box_half * self.scale)
        lane = 3.2 * self.scale
        for node in sim.grid.nodes:
            cx, cy = self.px(node.x, node.y)
            pygame.draw.rect(screen, BOX_FILL, (cx - half, cy - half, 2 * half, 2 * half), border_radius=3)
            ctl = sim.controllers[node.id]
            for seg_id in ctl.approaches:
                seg = sim.grid.segments[seg_id]
                x, y = sim.grid.position(seg_id, seg.stop_pos)
                px_, py_ = self.px(x, y)
                dx, dy = seg.direction.vector
                nx, ny = -dy, dx  # across the lane
                a = (px_ + nx * lane, py_ + ny * lane)
                b = (px_ - nx * lane, py_ - ny * lane)
                pygame.draw.line(screen, STOP_LINE[ctl.state_for(seg_id)], a, b, 5)
            img = self.label.render(str(node.id), True, (200, 205, 220))
            screen.blit(img, (cx - img.get_width() // 2, cy - img.get_height() // 2))

    def _forecasts(self, screen, sim: Simulation) -> None:
        for f in sim.predictor.active(sim.t):
            if f.predicted_density < sim.cfg.pred_density_min:
                continue
            seg = sim.grid.segments[f.target_segment]
            cx, cy = self.px(*sim.grid.position(seg.id, seg.stop_pos - 26))
            r = int(5 + 7 * f.confidence * (1.0 if f.hop == 1 else 0.7))
            pygame.draw.polygon(screen, CYAN, [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], 2)

    def _rings(self, screen, sim: Simulation) -> None:
        pulse = 0.5 + 0.5 * math.sin(sim.t * 6.0)
        seen: set[int] = set()
        for e in reversed(sim.events.since(sim.t - 3.0)):
            if e.node is None or e.kind not in RING_KINDS or e.node in seen:
                continue
            seen.add(e.node)
            node = sim.grid.nodes[e.node]
            cx, cy = self.px(node.x, node.y)
            age = (sim.t - e.t) / 3.0
            r = int(24 + 8 * pulse + 10 * age)
            pygame.draw.circle(screen, KIND_COLOR.get(e.kind, AMBER), (cx, cy), r, 3)

    def _body(self, screen, sim: Simulation, v: Vehicle, length_px: float, width_px: float, color, outline) -> None:
        seg = sim.grid.segments[v.seg]
        cx, cy = self.px(*sim.grid.position(seg.id, v.pos - v.length / 2))
        dx, dy = seg.direction.vector
        nx, ny = -dy, dx
        hl, hw = length_px / 2, width_px / 2
        pts = [
            (cx + dx * hl + nx * hw, cy + dy * hl + ny * hw),
            (cx + dx * hl - nx * hw, cy + dy * hl - ny * hw),
            (cx - dx * hl - nx * hw, cy - dy * hl - ny * hw),
            (cx - dx * hl + nx * hw, cy - dy * hl + ny * hw),
        ]
        pygame.draw.polygon(screen, color, pts)
        pygame.draw.polygon(screen, outline, pts, 1)

    def _vehicles(self, screen, sim: Simulation) -> None:
        free = sim.cfg.free_speed
        buses = []
        for v in sim.model.iter_vehicles():
            if v.kind == "car":
                self._body(screen, sim, v, 7.5, 4.5, CAR, CAR_OUTLINE)
            elif v.kind == "blocker":
                self._body(screen, sim, v, 8.0, 5.0, (150, 30, 30), (255, 90, 90))
                cx, cy = self.px(*sim.grid.position(v.seg, v.pos - v.length / 2))
                pygame.draw.line(screen, RED, (cx - 7, cy - 7), (cx + 7, cy + 7), 3)
                pygame.draw.line(screen, RED, (cx - 7, cy + 7), (cx + 7, cy - 7), 3)
            else:
                buses.append(v)
        for v in buses:  # buses on top, coloured by speed
            self._body(screen, sim, v, 17.0, 7.0, speed_color(v.v / free), (245, 245, 250))
            seg = sim.grid.segments[v.seg]
            cx, cy = self.px(*sim.grid.position(seg.id, v.pos - v.length / 2))
            tag = self.label.render(v.id[1:], True, (240, 242, 248))
            screen.blit(tag, (cx + 8, cy - 15))

    def _legend(self, screen, sim: Simulation, ui: UiState) -> None:
        x, y = self.rect.x + 14, self.rect.y + 10
        mode, color = ("ADAPTIVE", GREEN) if sim.adaptive else ("FIXED TIMERS", AMBER)
        screen.blit(self.banner.render(mode, True, color), (x, y))
        heat = ("heat off", "link occupancy: true (hidden)", "link load: estimated from buses")[ui.heat]
        screen.blit(self.label.render(heat, True, (150, 156, 175)), (x, y + 20))
        left = [
            ("car", CAR),
            ("bus: free flow", speed_color(1.0)),
            ("bus: slowing", speed_color(0.5)),
            ("bus: stopped", speed_color(0.0)),
        ]
        right = [
            ("LOCAL_EXTEND", "reactive green extension"),
            ("PREDICTIVE_EXTEND", "AI predictive override"),
            ("THROTTLE", "downstream throttle"),
            ("SPILLBACK_HOLD", "spillback guard"),
        ]
        base = self.rect.bottom - 14 * len(left) - 10
        for i, (text, c) in enumerate(left):
            pygame.draw.rect(screen, c, (x, base + 14 * i + 1, 10, 9))
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x + 15, base + 14 * i))
        x2 = self.rect.x + 160
        for i, (kind, text) in enumerate(right):
            pygame.draw.circle(screen, KIND_COLOR[kind], (x2 + 5, base + 14 * i + 6), 5, 2)
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x2 + 15, base + 14 * i))
        x3, cy = self.rect.x + 340, base + 6
        pygame.draw.polygon(screen, CYAN, [(x3 + 5, cy - 6), (x3 + 11, cy), (x3 + 5, cy + 6), (x3 - 1, cy)], 2)
        screen.blit(self.label.render("AI forecast: dense traffic expected", True, (170, 176, 195)), (x3 + 18, base))
        pygame.draw.line(screen, RED, (x3 - 1, cy + 8), (x3 + 11, cy + 20), 3)
        pygame.draw.line(screen, RED, (x3 - 1, cy + 20), (x3 + 11, cy + 8), 3)
        screen.blit(self.label.render("stalled vehicle (incident)", True, (170, 176, 195)), (x3 + 18, base + 14))
        for i, (c, text) in enumerate((("G", "green: street may enter"), ("R", "red: stop line closed"))):
            y_ = base + 34 + 14 * i + 6
            pygame.draw.line(screen, STOP_LINE[c], (x3 - 1, y_), (x3 + 11, y_), 5)
            screen.blit(self.label.render(text, True, (170, 176, 195)), (x3 + 18, base + 28 + 14 * i))


WINDOW = (1440, 860)
HUD_WIDTH = 560


def make_views(sim: Simulation) -> tuple[CityView, Hud]:
    city = CityView(sim, pygame.Rect(0, 0, WINDOW[0] - HUD_WIDTH, WINDOW[1]))
    hud = Hud(pygame.Rect(WINDOW[0] - HUD_WIDTH, 0, HUD_WIDTH, WINDOW[1]))
    return city, hud
