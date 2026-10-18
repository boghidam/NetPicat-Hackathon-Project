"""Professional GIS map renderer: dark cartographic visualization of Iași with real-time transit telemetry."""

from __future__ import annotations

import math
import time

import pygame

from .hud import (
    AMBER,
    BG,
    CYAN,
    KIND_COLOR,
    LIME,
    RED,
    TEXT,
    Hud,
    UiState,
    line_color,
    speed_color,
    sysfont,
)
from .network import RoadNetwork
from .simulation import Simulation
from .traffic_model import Vehicle

# --- Cartographic Road Hierarchy Palette -------------------------------------
ROAD_PRIMARY = (54, 63, 80)
ROAD_PRIMARY_EDGE = (22, 27, 36)
ROAD_SECONDARY = (44, 52, 68)
ROAD_SECONDARY_EDGE = (18, 23, 31)
ROAD_TERTIARY = (34, 41, 54)
ROAD_TERTIARY_EDGE = (15, 19, 26)

ASPHALT = ROAD_PRIMARY
ASPHALT_EDGE = ROAD_PRIMARY_EDGE
NODE_RING = (48, 58, 76)

# Signal lights (vibrant, high-contrast traffic optics)
SIGNAL = {
    "G": (34, 197, 94),  # Emerald green
    "Y": (245, 158, 11),  # Amber
    "R": (239, 68, 68),  # Crimson
}

# Vehicle styling
CAR = (147, 197, 253)  # Sky blue metallic
CAR_OUTLINE = (15, 23, 42)  # Dark slate outline
ROAD_WIDTH_M = {"primary": 12.0, "secondary": 10.0, "tertiary": 8.0}
RING_KINDS = {"LOCAL_EXTEND", "PREDICTIVE_EXTEND", "THROTTLE"}
RUSH = (255, 122, 40)  # rush-hour orange: the key point under pressure


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

    def zoom_centre(self, factor: float) -> None:
        self.zoom_at(self.rect.centerx, self.rect.centery, factor)

    def pan_pixels(self, dx: float, dy: float) -> None:
        self.cx -= dx / self.scale
        self.cy += dy / self.scale
        self.version += 1


class CityView:
    LEGEND_PER_ROW = 9  # route chips per row in the legend
    CHIP_CELL = (26, 16)  # px: at most one line badge per cell, so a crowded centre stays readable

    def __init__(self, sim: Simulation, rect: pygame.Rect):
        self.rect = rect
        self.camera = Camera(rect, sim.net)
        sans = "helveticaneue,segoeui,inter,arial,sans-serif"
        mono = "menlo,jetbrainsmono,consolas,dejavusansmono,monospace"

        self.label = sysfont(sans, 10, bold=True)
        self.chip = sysfont(mono, 10, bold=True)
        self.banner = sysfont(sans, 14, bold=True)
        self.sub_banner = sysfont(sans, 10)
        self.legend_font = sysfont(sans, 9)
        self.legend_bold = sysfont(sans, 9, bold=True)

        self._layer = pygame.Surface(rect.size)
        self._layer_state: tuple | None = None  # (cx, cy, scale, show_lines)
        self._layer_time = 0.0
        self._chips: dict[str, pygame.Surface] = {}
        self._stop_lines: dict[int, tuple[float, float, float, float]] = {}
        self._landmarks = list(sim.net.landmarks_ll)

    # -- Input helpers ---------------------------------------------------------
    def zoom(self, pos: tuple[int, int], factor: float) -> None:
        if self.rect.collidepoint(pos):
            self.camera.zoom_at(pos[0], pos[1], factor)

    def zoom_centre(self, factor: float) -> None:
        self.camera.zoom_centre(factor)

    def drag(self, rel: tuple[int, int]) -> None:
        self.camera.pan_pixels(*rel)

    def fit(self) -> None:
        self.camera.fit()

    def landmark_at(self, pos: tuple[int, int]) -> str | None:
        """The key point whose pin or label is under the mouse, if any (click one to put it under pressure)."""
        best: tuple[float, str] | None = None
        for name, lat, lon in self._landmarks:
            sx, sy = self.camera.latlon_to_screen(lat, lon)
            tw, th = self.label.size(name.upper())
            w_cap, h_cap = tw + 22, th + 6  # the same capsule that is drawn on the map
            flush_right = int(sx) - 8 + w_cap > self.rect.width - 8
            bx = int(sx) - w_cap + 8 if flush_right else int(sx) - 8
            capsule = pygame.Rect(bx, int(sy - h_cap // 2), w_cap, h_cap)
            dist = math.hypot(pos[0] - sx, pos[1] - sy)
            if capsule.collidepoint(pos) or dist <= 12:
                if best is None or dist < best[0]:
                    best = (dist, name)
        return best[1] if best else None

    # -- Frame rendering -------------------------------------------------------
    def draw(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        screen.set_clip(self.rect)
        self._draw_layer(screen, sim, ui)
        self._heat(screen, sim, ui)
        self._junctions(screen, sim)
        self._forecasts(screen, sim)
        self._rings(screen, sim)
        self._vehicles(screen, sim)
        self._rush_marker(screen, sim)
        self._map_overlays(screen, sim, ui)
        self._legend(screen, sim, ui)
        screen.set_clip(None)

    # -- Cached road layer -----------------------------------------------------
    def _draw_layer(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        cam = self.camera
        state = (cam.cx, cam.cy, cam.scale, ui.show_lines)
        if state != self._layer_state:
            panned_only = self._layer_state is not None and state[2:] == self._layer_state[2:]
            if panned_only and time.time() - self._layer_time < 0.1:
                ox = (self._layer_state[0] - cam.cx) * cam.scale
                oy = -(self._layer_state[1] - cam.cy) * cam.scale
                screen.fill(BG, self.rect)
                screen.blit(self._layer, (self.rect.x + ox, self.rect.y + oy))
                return
            self._render_layer(sim, ui)
            self._layer_state, self._layer_time = state, time.time()
        screen.blit(self._layer, self.rect.topleft)

    def _poly(self, sim: Simulation, seg_id: int, shift_m: float = 0.0) -> list[tuple[float, float]]:
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
        drawn: list[tuple[list[tuple[float, float]], int, str]] = []

        for seg in net.segments:
            back = twin.get((seg.to_node, seg.from_node))
            if back is not None and back < seg.id:
                continue

            hwy = seg.highway.replace("_link", "")
            if "primary" in hwy:
                base_w = ROAD_WIDTH_M["primary"]
                tier = "primary"
            elif "secondary" in hwy:
                base_w = ROAD_WIDTH_M["secondary"]
                tier = "secondary"
            else:
                base_w = ROAD_WIDTH_M["tertiary"]
                tier = "tertiary"

            exaggerate = 1.0 + 1.1 * max(0.0, min(1.0, (0.6 - cam.scale) / 0.5))
            width = max(3, int(exaggerate * base_w * cam.scale))
            drawn.append((self._poly(sim, seg.id), width, tier))

        # Pass 1: Dark Casing for road boundary separation
        for pts, width, tier in drawn:
            edge_color = (
                ROAD_PRIMARY_EDGE
                if tier == "primary"
                else (ROAD_SECONDARY_EDGE if tier == "secondary" else ROAD_TERTIARY_EDGE)
            )
            pygame.draw.lines(surf, edge_color, False, pts, width + 2)

        # Pass 2: Road Surface Fills
        for pts, width, tier in drawn:
            fill_color = (
                ROAD_PRIMARY if tier == "primary" else (ROAD_SECONDARY if tier == "secondary" else ROAD_TERTIARY)
            )
            pygame.draw.lines(surf, fill_color, False, pts, width)
            if width >= 5:
                for p in pts:
                    pygame.draw.circle(surf, fill_color, p, width // 2)

        # Pass 3: Transit Line Corridor Overlays (L toggle)
        if ui.show_lines:
            for k, route in enumerate(net.routes):
                col = line_color(sim, route.ref)
                shift = (2.0 + 2.0 * k) / cam.scale
                for sid in route.edges:
                    pts = self._poly(sim, sid, shift)
                    if len(pts) >= 2:
                        pygame.draw.lines(surf, lerp(col, ROAD_PRIMARY, 0.4), False, pts, max(1, int(1.5 * cam.scale)))

        # Pass 4: Landmark Points of Interest (Clean Mapbox-style capsules)
        for name, lat, lon in net.landmarks_ll:
            sx, sy = cam.latlon_to_screen(lat, lon)
            px, py = sx - self.rect.x, sy - self.rect.y
            lbl = name.upper()
            tw, th = self.label.size(lbl)
            w_cap = tw + 22
            h_cap = th + 6
            if px < -w_cap or px > surf.get_width() + w_cap or py < -h_cap or py > surf.get_height() + h_cap:
                continue
            by = int(py - h_cap // 2)
            if int(px) - 8 + w_cap > surf.get_width() - 8:
                bx = int(px) - w_cap + 8
                pin_x, txt_x = bx + w_cap - 8, bx + 6
            else:
                bx = int(px) - 8
                pin_x, txt_x = bx + 8, bx + 16

            capsule = pygame.Surface((w_cap, h_cap), pygame.SRCALPHA)
            pygame.draw.rect(capsule, (17, 24, 38, 225), (0, 0, w_cap, h_cap), border_radius=4)
            pygame.draw.rect(capsule, (45, 58, 80, 240), (0, 0, w_cap, h_cap), width=1, border_radius=4)
            surf.blit(capsule, (bx, by))

            # Pin dot and label text
            pygame.draw.circle(surf, CYAN, (pin_x, by + h_cap // 2), 3)
            surf.blit(self.label.render(lbl, True, (230, 238, 250)), (txt_x, by + 3))

    # -- Dynamic Heat Layer ----------------------------------------------------
    def _heat(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        if ui.heat == 0:
            return
        cam = self.camera
        for seg in sim.net.segments:
            if ui.heat == 1:
                load = sim.model.density(seg.id)
            else:
                if (
                    sim.estimator.confidence(seg.id, sim.t) < 0.05
                    and sim.coord.fc_load[seg.id] < 0.05
                    and sim.loops.reading(seg.id) is None
                ):
                    continue
                load = sim.coord.load[seg.id]
            if load < 0.15:
                continue
            color = lerp(ROAD_PRIMARY, speed_color(1.0 - load), min(1.0, load * 1.5))
            pts = [cam.to_screen(*sim.net.position(seg.id, p)[:2]) for p in (seg.stop_pos * k / 4 for k in range(5))]
            pygame.draw.lines(screen, color, False, pts, max(3, int(6 * cam.scale)))

    # -- Signalized Junctions & Stop Lines -------------------------------------
    def _stop_line(self, net: RoadNetwork, seg_id: int) -> tuple[float, float, float, float]:
        """Where an approach stops on red (fixed for the run, so worked out once)."""
        got = self._stop_lines.get(seg_id)
        if got is None:
            got = self._stop_lines[seg_id] = net.position(seg_id, net.segments[seg_id].stop_pos)
        return got

    def _junctions(self, screen: pygame.Surface, sim: Simulation) -> None:
        cam, net = self.camera, sim.net
        r = max(2, int(sim.cfg.box_half * cam.scale))
        half = max(3.0, 3.2 * cam.scale) / cam.scale

        for node in net.nodes:
            cx, cy = cam.to_screen(node.x, node.y)
            if not self.rect.inflate(60, 60).collidepoint(cx, cy):
                continue
            if node.gate:
                pygame.draw.polygon(screen, CYAN, [(cx, cy - 6), (cx + 6, cy), (cx, cy + 6), (cx - 6, cy)], 1)
                continue
            ctl = sim.controllers.get(node.id)
            if ctl is None:
                continue

            # Junction center perimeter ring
            pygame.draw.circle(screen, NODE_RING, (cx, cy), r, 1)

            # Approach Stop-Lines (Crisp glowing bars with casing)
            for seg_id in ctl.approaches:
                x, y, dx, dy = self._stop_line(net, seg_id)
                a = cam.to_screen(x - dy * half, y + dx * half)
                b = cam.to_screen(x + dy * half, y - dx * half)

                sig_state = ctl.state_for(seg_id)
                sig_col = SIGNAL[sig_state]

                # 1px Dark edge shadow under stop bar
                pygame.draw.line(screen, (10, 14, 20), a, b, 6 if cam.scale > 0.4 else 5)
                # Vibrant stop bar
                pygame.draw.line(screen, sig_col, a, b, 4 if cam.scale > 0.4 else 3)

    # -- AI Forecast Diamonds --------------------------------------------------
    def _forecasts(self, screen: pygame.Surface, sim: Simulation) -> None:
        cam = self.camera
        for f in sim.predictor.active(sim.t):
            if f.predicted_density < sim.cfg.pred_density_min:
                continue
            seg = sim.net.segments[f.target_segment]
            cx, cy = cam.to_screen(*sim.net.position(seg.id, max(0.0, seg.stop_pos - 25))[:2])
            r = int(5 + 6 * f.confidence * (1.0 if f.hop == 1 else 0.7))
            pygame.draw.polygon(screen, CYAN, [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], 2)

    # -- Adaptive Override Pulsing Rings ---------------------------------------
    def _rings(self, screen: pygame.Surface, sim: Simulation) -> None:
        cam = self.camera
        pulse = 0.5 + 0.5 * math.sin(sim.t * 5.0)
        seen: set[int] = set()

        for e in reversed(sim.events.since(sim.t - 3.0)):
            if e.node is None or e.kind not in RING_KINDS or e.node in seen:
                continue
            seen.add(e.node)
            node = sim.net.nodes[e.node]
            cx, cy = cam.to_screen(node.x, node.y)
            age = (sim.t - e.t) / 3.0
            r = int(14 + 6 * pulse + 12 * age)
            color = KIND_COLOR.get(e.kind, AMBER)
            pygame.draw.circle(screen, color, (cx, cy), r, 2)

    # -- Vehicles & Transit Glyphs ---------------------------------------------
    def _body(
        self, screen: pygame.Surface, sim: Simulation, v: Vehicle, length_px: float, width_px: float, color, outline
    ) -> tuple[float, float]:
        x, y, dx, dy = sim.net.position(v.seg, v.pos - v.length / 2)
        cx, cy = self.camera.to_screen(x, y)
        self._draw_body(screen, cx, cy, dx, dy, length_px, width_px, color, outline)
        return cx, cy

    @staticmethod
    def _draw_body(screen, cx, cy, dx, dy, length_px, width_px, color, outline) -> None:
        ux, uy = dx, -dy
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

    def _chip(self, ref: str, color) -> pygame.Surface:
        key = f"{ref}{color}"
        if key not in self._chips:
            text = self.chip.render(ref, True, (12, 16, 24))
            surf = pygame.Surface((text.get_width() + 6, text.get_height() + 2), pygame.SRCALPHA)
            pygame.draw.rect(surf, color, surf.get_rect(), border_radius=3)
            surf.blit(text, (3, 1))
            self._chips[key] = surf
        return self._chips[key]

    def _vehicles(self, screen: pygame.Surface, sim: Simulation) -> None:
        cam, net = self.camera, sim.net
        view = self.rect.inflate(40, 40)
        car_len, car_wid = max(6.0, sim.cfg.car_length * cam.scale), max(4.0, 2.0 * cam.scale * 1.3)
        buses = []

        for v in sim.model.iter_vehicles():
            if v.kind == "blocker":
                # Stalled Incident Vehicle: High-visibility warning marker
                cx, cy = self._body(screen, sim, v, 10.0, 6.0, (180, 24, 40), (255, 200, 200))
                # Hazard cross & pulsing ring
                pulse_r = int(12 + 4 * math.sin(sim.t * 8.0))
                pygame.draw.circle(screen, RED, (cx, cy), pulse_r, 1)
                pygame.draw.line(screen, RED, (cx - 6, cy - 6), (cx + 6, cy + 6), 3)
                pygame.draw.line(screen, RED, (cx - 6, cy + 6), (cx + 6, cy - 6), 3)
            elif v.kind == "bus":
                buses.append(v)
            else:
                x, y, dx, dy = net.position(v.seg, v.pos - v.length / 2)  # one projection serves culling and drawing
                cx, cy = cam.to_screen(x, y)
                if view.collidepoint(cx, cy):
                    self._draw_body(screen, cx, cy, dx, dy, car_len, car_wid, CAR, CAR_OUTLINE)

        # Buses rendered on top with speed ramp and route badge
        taken: set[tuple[int, int]] = set()
        cell_w, cell_h = self.CHIP_CELL
        for v in buses:
            x, y, dx, dy = net.position(v.seg, v.pos - v.length / 2)
            cx, cy = cam.to_screen(x, y)
            if not view.collidepoint(cx, cy):
                continue
            col = speed_color(v.v / net.segments[v.seg].speed_limit)
            self._draw_body(
                screen,
                cx,
                cy,
                dx,
                dy,
                max(11.0, v.length * cam.scale),
                max(4.8, 2.6 * cam.scale * 1.3),
                col,
                (245, 250, 255),
            )
            cell = (int(cx) // cell_w, int(cy) // cell_h)
            if cell not in taken:  # a badge already sits here: show the bus body alone rather than pile badges up
                taken.add(cell)
                screen.blit(self._chip(v.line or "?", line_color(sim, v.line or "")), (cx + 6, cy - 14))

    # -- Rush-hour marker on the key point under pressure ------------------------
    def _rush_marker(self, screen: pygame.Surface, sim: Simulation) -> None:
        node_id = sim.model.rush_node
        if sim.rush_point is None or node_id is None:
            return
        node = sim.net.nodes[node_id]
        cx, cy = self.camera.to_screen(node.x, node.y)
        phase = (sim.t * 0.8) % 1.0
        for k in range(3):  # rings spreading outward: pressure building up around the junction
            f = (phase + k / 3) % 1.0
            pygame.draw.circle(screen, lerp(RUSH, BG, f), (cx, cy), int(14 + 46 * f), 2)
        pygame.draw.circle(screen, RUSH, (cx, cy), 9, 3)
        tag = self.legend_bold.render(f"RUSH HOUR · {sim.rush_point.upper()}", True, (20, 14, 8))
        box = pygame.Rect(int(cx) + 16, int(cy) - 30, tag.get_width() + 12, tag.get_height() + 6)
        pygame.draw.rect(screen, RUSH, box, border_radius=4)
        screen.blit(tag, (box.x + 6, box.y + 3))

    # -- Top-Left Map Status Overlay -------------------------------------------
    def _map_overlays(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        x, y = self.rect.x + 14, self.rect.y + 12

        # Floating map status capsule
        mode_str = "ADAPTIVE AI CONTROL" if sim.adaptive else "FIXED TIMERS BASELINE"
        mode_col = LIME if sim.adaptive else AMBER

        card_w, card_h = 320, 42
        capsule = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
        pygame.draw.rect(capsule, (17, 22, 32, 220), (0, 0, card_w, card_h), border_radius=6)
        pygame.draw.rect(capsule, (36, 46, 64, 240), (0, 0, card_w, card_h), width=1, border_radius=6)
        screen.blit(capsule, (x, y))

        # Status pip & Text
        pygame.draw.circle(screen, mode_col, (x + 14, y + 14), 4)
        screen.blit(self.banner.render(mode_str, True, TEXT), (x + 24, y + 6))

        heat_name = ("", "LINK DENSITY: TRUE WORLD", "LINK LOAD: ESTIMATED FROM BUSES")[ui.heat]
        corridor = "SYNTHETIC TEST GRID" if "synthetic" in sim.net.source else "IAȘI CTP NETWORK"
        sub = heat_name if heat_name else f"{corridor} · {sum(n.signalised for n in sim.net.nodes)} SIGNALS"
        screen.blit(self.sub_banner.render(sub, True, (138, 150, 170)), (x + 24, y + 24))

        # Traffic amount and rush-hour key point
        pct = sim.traffic_percent
        pct_col = CYAN if pct == 100 else (LIME if pct < 100 else (AMBER if pct <= 300 else RUSH))
        line2 = f"TRAFFIC {pct:g}%  ·  {sim.model.n_cars} CARS  ·  {len(sim.fleet.buses)} BUSES"
        rush = f"RUSH HOUR: {sim.rush_point.upper()}" if sim.rush_point else "RUSH HOUR: OFF (K OR CLICK A KEY POINT)"
        w2 = max(card_w, self.legend_bold.size(line2)[0] + 28, self.legend_bold.size(rush)[0] + 28)
        capsule2 = pygame.Surface((w2, 40), pygame.SRCALPHA)
        pygame.draw.rect(capsule2, (17, 22, 32, 220), (0, 0, w2, 40), border_radius=6)
        pygame.draw.rect(capsule2, (36, 46, 64, 240), (0, 0, w2, 40), width=1, border_radius=6)
        screen.blit(capsule2, (x, y + card_h + 6))
        screen.blit(self.legend_bold.render(line2, True, pct_col), (x + 12, y + card_h + 13))
        screen.blit(
            self.legend_bold.render(rush, True, RUSH if sim.rush_point else (110, 122, 142)), (x + 12, y + card_h + 28)
        )

    # -- Bottom-Left Interactive Legend Card -----------------------------------
    def _legend(self, screen: pygame.Surface, sim: Simulation, ui: UiState) -> None:
        # Floating legend container
        card_w = 480
        rows = -(-len(sim.net.routes) // self.LEGEND_PER_ROW)  # route chips wrap onto as many rows as needed
        card_h = 76 if not ui.show_lines else 74 + 20 * rows
        x = self.rect.x + 14
        y = self.rect.bottom - card_h - 12

        card = pygame.Surface((card_w, card_h), pygame.SRCALPHA)
        pygame.draw.rect(card, (17, 22, 32, 220), (0, 0, card_w, card_h), border_radius=6)
        pygame.draw.rect(card, (36, 46, 64, 240), (0, 0, card_w, card_h), width=1, border_radius=6)
        screen.blit(card, (x, y))

        # Row 1: Signals and vehicle types
        ry1 = y + 8
        # Signals
        sx = x + 12
        screen.blit(self.legend_bold.render("SIGNALS:", True, (138, 150, 170)), (sx, ry1))
        sx += 52
        for _st, name, col in (("G", "Green", SIGNAL["G"]), ("Y", "Yellow", SIGNAL["Y"]), ("R", "Red", SIGNAL["R"])):
            pygame.draw.line(screen, col, (sx, ry1 + 5), (sx + 10, ry1 + 5), 3)
            screen.blit(self.legend_font.render(name, True, (210, 218, 230)), (sx + 14, ry1))
            sx += 52

        # Vehicles
        sx = x + 240
        screen.blit(self.legend_bold.render("VEHICLES:", True, (138, 150, 170)), (sx, ry1))
        sx += 58
        pygame.draw.rect(screen, CAR, (sx, ry1 + 1, 9, 8), border_radius=2)
        screen.blit(self.legend_font.render("Car", True, (210, 218, 230)), (sx + 13, ry1))
        sx += 42
        for ratio, lbl in ((1.0, "Free"), (0.5, "Slow"), (0.0, "Queue")):
            pygame.draw.rect(screen, speed_color(ratio), (sx, ry1 + 1, 9, 8), border_radius=2)
            screen.blit(self.legend_font.render(lbl, True, (210, 218, 230)), (sx + 13, ry1))
            sx += 44

        # Row 2: Adaptive events
        ry2 = y + 28
        screen.blit(self.legend_bold.render("EVENTS:", True, (138, 150, 170)), (x + 12, ry2))
        ex = x + 64

        events = [
            ("LOCAL_EXTEND", "Bus Extend", KIND_COLOR["LOCAL_EXTEND"]),
            ("PREDICTIVE_EXTEND", "AI Forecast", KIND_COLOR["PREDICTIVE_EXTEND"]),
            ("THROTTLE", "Throttle", KIND_COLOR["THROTTLE"]),
        ]
        for _kind, name, col in events:
            pygame.draw.circle(screen, col, (ex + 4, ry2 + 5), 4, 2)
            screen.blit(self.legend_font.render(name, True, (210, 218, 230)), (ex + 12, ry2))
            ex += 94

        pygame.draw.polygon(screen, CYAN, [(ex + 4, ry2 + 1), (ex + 8, ry2 + 5), (ex + 4, ry2 + 9), (ex, ry2 + 5)], 1)
        screen.blit(self.legend_font.render("Gate / AI Target", True, (210, 218, 230)), (ex + 12, ry2))

        # Row 3: Active CTP Lines (if enabled)
        if ui.show_lines:
            ry3 = y + 48
            screen.blit(self.legend_bold.render("ROUTES:", True, (138, 150, 170)), (x + 12, ry3))
            for i, route in enumerate(sim.net.routes):
                c = line_color(sim, route.ref)
                row, col = divmod(i, self.LEGEND_PER_ROW)
                screen.blit(self._chip(route.ref, c), (x + 64 + col * 44, ry3 - 1 + row * 20))

        # Bottom-Right Scale Bar
        scale_m = 500
        bar = scale_m * self.camera.scale
        by = self.rect.bottom - 16
        bx = self.rect.right - 24 - bar

        # End ticks and bar
        pygame.draw.line(screen, (80, 95, 120), (bx, by), (bx + bar, by), 2)
        pygame.draw.line(screen, (80, 95, 120), (bx, by - 4), (bx, by + 4), 2)
        pygame.draw.line(screen, (80, 95, 120), (bx + bar, by - 4), (bx + bar, by + 4), 2)
        txt = self.legend_bold.render(f"{scale_m} m", True, (140, 155, 175))
        screen.blit(txt, (bx + bar / 2 - txt.get_width() / 2, by - 14))


WINDOW = (1440, 860)
HUD_WIDTH = 560


def make_views(sim: Simulation) -> tuple[CityView, Hud]:
    city = CityView(sim, pygame.Rect(0, 0, WINDOW[0] - HUD_WIDTH, WINDOW[1]))
    hud = Hud(pygame.Rect(WINDOW[0] - HUD_WIDTH, 0, HUD_WIDTH, WINDOW[1]))
    return city, hud
