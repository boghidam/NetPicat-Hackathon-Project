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


def test_interactive_loop_traffic_and_rush_hour_shortcuts(screen):
    sim = Simulation(Config(), map_name="iasi")
    keys = [pygame.K_RIGHTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_LEFTBRACKET, pygame.K_k, pygame.K_k]
    for k in keys:
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k, mod=0))
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    run_window(sim, None, 0)
    assert sim.traffic_percent == 150  # 100 -> 150 -> 200 -> 150
    assert sim.rush_point == sim.key_points[1]  # K twice: the first key point, then the second


def test_interactive_loop_shift_k_goes_back_and_backslash_resets_to_normal(screen):
    sim = Simulation(Config(), map_name="iasi")
    sim.set_traffic_percent(500)
    for k, mod in ((pygame.K_k, pygame.KMOD_SHIFT), (pygame.K_BACKSLASH, 0)):
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k, mod=mod))
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    run_window(sim, None, 0)
    assert sim.traffic_percent == 100 and sim.rush_point == sim.key_points[-1]


def test_clicking_a_key_point_toggles_rush_hour_but_dragging_does_not(screen):
    sim = Simulation(Config(), map_name="iasi")
    city, _ = make_views(sim)
    name, lat, lon = sim.net.landmarks_ll[3]
    sx, sy = (int(v) for v in city.camera.latlon_to_screen(lat, lon))

    def click(*, drag=0):
        pygame.init()  # run_window shuts pygame down when it returns
        pygame.display.set_mode(WINDOW)
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(sx, sy)))
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(sx + drag, sy)))
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        run_window(sim, None, 0)

    click()
    assert sim.rush_point == name  # a click picks it
    click()
    assert sim.rush_point is None  # clicking it again lifts the pressure
    click(drag=40)
    assert sim.rush_point is None  # a drag is not a click


@pytest.mark.parametrize("percent", [0, 1000])
def test_the_dashboard_draws_at_the_extremes_with_rush_hour_on(screen, percent):
    sim = Simulation(Config(), map_name="iasi")
    sim.set_traffic_percent(percent)
    sim.set_rush_point("Podu Roș")
    sim.run(20)
    city, hud = make_views(sim)
    ui = UiState(rows=snapshot_buses(sim))
    city.draw(screen, sim, ui)
    hud.draw(screen, sim, ui)
    assert (percent == 0) == (not ui.rows)


def test_every_line_in_the_city_has_its_own_colour(iasi_net):
    import itertools
    import math

    from smartcity.hud import LINE_COLORS, line_color

    sim = Simulation(Config(), net=iasi_net)
    colours = [line_color(sim, r.ref) for r in iasi_net.routes]
    assert len(set(colours)) == len(colours) == len(iasi_net.routes)  # no two lines share a colour
    assert min(math.dist(a, b) for a, b in itertools.combinations(LINE_COLORS, 2)) > 40  # and they are tellable apart
    assert line_color(sim, "nope") not in colours


def test_the_legend_lists_every_line_and_only_one_badge_sits_in_a_crowded_spot(screen, iasi_net):
    sim = Simulation(Config(seed=3, traffic_amount=3.0), net=iasi_net)
    sim.run(30)
    city, hud = make_views(sim)
    ui = UiState(rows=snapshot_buses(sim))
    shown = []
    real_chip = city._chip
    city._chip = lambda ref, color: shown.append(ref) or real_chip(ref, color)
    screen.fill(BG)
    city.draw(screen, sim, ui)
    legend = shown[-len(iasi_net.routes) :]
    assert legend == [r.ref for r in iasi_net.routes]  # all 18 lines are in the legend, in route order
    badges = len(shown) - len(legend)
    assert 0 < badges < len(sim.fleet.buses)  # 300 buses, but crowded spots get a single badge


def test_cached_fonts_measure_and_render_once_and_stay_bounded():
    from smartcity.hud import CachedFont, sysfont

    pygame.init()
    try:
        font = sysfont("arial", 12)
        assert isinstance(font, CachedFont)
        assert font.render("L28", True, (255, 255, 255)) is font.render("L28", True, (255, 255, 255))
        assert font.render("L28", True, (255, 255, 255)) is not font.render("L28", True, (0, 0, 0))
        assert font.size("L28") == font._font.size("L28") and font.get_height() == font._font.get_height()
        for i in range(CachedFont.LIMIT + 50):
            font.size(f"n{i}")
            font.render(f"n{i}", True, (1, 2, 3))
        assert len(font._sizes) <= CachedFont.LIMIT and len(font._images) <= CachedFont.LIMIT
    finally:
        pygame.quit()
