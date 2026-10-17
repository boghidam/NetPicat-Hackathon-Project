"""Rendering, camera and the interactive loop run under SDL's dummy video driver."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
import pytest

from smartcity.config import Config
from smartcity.hud import BG, UiState, snapshot_buses
from smartcity.main import run_window
from smartcity.simulation import Simulation
from smartcity.visualizer import WINDOW, Camera, make_views


@pytest.fixture
def screen():
    pygame.init()
    surface = pygame.display.set_mode(WINDOW)
    yield surface
    pygame.quit()


def test_camera_projects_lat_lon_to_screen_and_back(iasi_net):
    cam = Camera(pygame.Rect(0, 0, 880, 860), iasi_net)
    lat, lon = iasi_net.proj.lat0, iasi_net.proj.lon0
    sx, sy = cam.latlon_to_screen(lat, lon)
    assert (sx, sy) == (
        pytest.approx(cam.rect.centerx, abs=cam.scale * 5000),
        pytest.approx(cam.rect.centery, abs=cam.scale * 5000),
    )
    # north is up, east is right
    n = cam.latlon_to_screen(lat + 0.001, lon)
    e = cam.latlon_to_screen(lat, lon + 0.001)
    assert n[1] < sy and e[0] > sx
    x, y = iasi_net.nodes[10].x, iasi_net.nodes[10].y
    assert cam.to_world(*cam.to_screen(x, y)) == (pytest.approx(x), pytest.approx(y))
    assert cam.latlon_to_screen(iasi_net.nodes[10].lat, iasi_net.nodes[10].lon) == pytest.approx(
        cam.to_screen(x, y), abs=1e-6
    )


def test_fit_shows_the_whole_map(iasi_net):
    rect = pygame.Rect(0, 0, 880, 860)
    cam = Camera(rect, iasi_net)
    x0, y0, x1, y1 = iasi_net.bounds
    for corner in ((x0, y0), (x1, y1), (x0, y1), (x1, y0)):
        assert rect.collidepoint(cam.to_screen(*corner))


def test_zoom_keeps_the_point_under_the_cursor_fixed_and_pan_moves_the_map(iasi_net):
    cam = Camera(pygame.Rect(0, 0, 880, 860), iasi_net)
    cursor = (300, 200)
    world = cam.to_world(*cursor)
    cam.zoom_at(*cursor, 2.0)
    assert cam.to_screen(*world) == pytest.approx(cursor, abs=1e-6)
    before = cam.to_screen(*world)
    cam.pan_pixels(30, -20)
    after = cam.to_screen(*world)
    assert (after[0] - before[0], after[1] - before[1]) == (pytest.approx(30), pytest.approx(-20))
    for _ in range(40):
        cam.zoom_at(*cursor, 2.0)
    assert cam.scale <= Camera.MAX_SCALE
    cam.fit()
    assert cam.scale < 1.0


@pytest.mark.parametrize("map_name", ["iasi", "grid"])
def test_frame_renders_map_and_dashboard_without_errors(screen, map_name):
    sim = Simulation(Config(), map_name=map_name, incident_at=5.0)
    sim.run(40)
    city, hud = make_views(sim)
    ui = UiState(rows=snapshot_buses(sim))
    assert ui.rows and ui.rows[0].line
    for heat in (0, 1, 2):
        for lines in (True, False):
            ui.heat, ui.show_lines = heat, lines
            screen.fill(BG)
            city.draw(screen, sim, ui)
            hud.draw(screen, sim, ui)
    colours = {tuple(screen.get_at((x, y)))[:3] for x in range(0, WINDOW[0], 7) for y in range(0, WINDOW[1], 7)}
    assert len(colours) > 10 and BG in colours


def test_zoomed_in_view_still_renders(screen, iasi_net):
    sim = Simulation(Config(), net=iasi_net)
    sim.run(30)
    city, hud = make_views(sim)
    ui = UiState(rows=snapshot_buses(sim))
    city.zoom((400, 400), 6.0)
    city.drag((50, 20))
    city.draw(screen, sim, ui)
    city.fit()
    city.draw(screen, sim, ui)


def test_interactive_loop_handles_keys_and_mouse_and_quits(screen):
    sim = Simulation(Config(), map_name="grid")
    keys = [pygame.K_b, pygame.K_i, pygame.K_h, pygame.K_l, pygame.K_4, pygame.K_SPACE, pygame.K_SPACE, pygame.K_f,
            pygame.K_PLUS, pygame.K_MINUS, pygame.K_LEFT, pygame.K_UP, pygame.K_r]  # fmt: skip
    for k in keys:
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k))
    pygame.event.post(pygame.event.Event(pygame.MOUSEWHEEL, x=0, y=2))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(200, 200)))
    pygame.event.post(pygame.event.Event(pygame.MOUSEMOTION, pos=(220, 210), rel=(20, 10), buttons=(1, 0, 0)))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(220, 210)))
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    run_window(sim, None, 0)  # returns when QUIT is processed


def test_screenshot_mode_writes_a_png(tmp_path):
    out = tmp_path / "frame.png"
    run_window(Simulation(Config(), map_name="grid"), str(out), 20)
    assert out.stat().st_size > 10_000
