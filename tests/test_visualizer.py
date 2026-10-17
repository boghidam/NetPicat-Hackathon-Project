"""Smoke tests: the renderer and the interactive loop run under SDL's dummy video driver."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
import pytest

from smartcity.config import Config
from smartcity.hud import BG, UiState
from smartcity.main import run_window
from smartcity.simulation import Simulation
from smartcity.visualizer import WINDOW, make_views


@pytest.fixture
def screen():
    pygame.init()
    surface = pygame.display.set_mode(WINDOW)
    yield surface
    pygame.quit()


def test_frame_renders_map_and_hud_without_errors(screen):
    sim = Simulation(Config(), incident_at=5.0)
    sim.run(40)
    city, hud = make_views(sim)
    ui = UiState(api_snapshot=sim.api.recent(8))
    for heat in (0, 1, 2):
        ui.heat = heat
        city.draw(screen, sim, ui)
        hud.draw(screen, sim, ui)
    colours = {tuple(screen.get_at((x, y)))[:3] for x in range(0, WINDOW[0], 7) for y in range(0, WINDOW[1], 7)}
    assert len(colours) > 8 and BG in colours


def test_interactive_loop_handles_all_keys_and_quits(screen, tmp_path):
    sim = Simulation(Config())
    keys = [pygame.K_b, pygame.K_i, pygame.K_h, pygame.K_4, pygame.K_SPACE, pygame.K_SPACE, pygame.K_r]
    for k in keys:
        pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=k))
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    run_window(sim, None, 0)  # returns when QUIT is processed


def test_screenshot_mode_writes_a_png(tmp_path):
    out = tmp_path / "frame.png"
    run_window(Simulation(Config()), str(out), 20)
    assert out.stat().st_size > 10_000
