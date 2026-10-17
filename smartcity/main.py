"""Entry point: interactive demo, headless comparison, or a single screenshot.

python -m smartcity.main                       # interactive window
python -m smartcity.main --incident-at 60      # block the central lane after 60 s
python -m smartcity.main --compare             # headless fixed-timer vs adaptive table
python -m smartcity.main --screenshot out.png --seconds 200 --incident-at 60
"""

from __future__ import annotations

import argparse
import os

from .config import Config
from .simulation import Simulation


def compare(seconds: float, seeds: int) -> None:
    print(f"Incident on the central link at t=120 s; {seeds} seeds x {seconds:.0f} s each.\n")
    print(f"{'mode':<10} {'bus time lost':>14} {'jam link-s':>11} {'exited veh':>11} {'overrides':>10}")
    for adaptive in (False, True):
        lost = jam = thr = ovr = 0.0
        for seed in range(1, seeds + 1):
            sim = Simulation(Config(seed=seed), adaptive=adaptive, incident_at=120.0)
            for _ in range(int(seconds / 5)):
                sim.run(5)
                jam += 5 * sum(sim.model.density(i) > sim.cfg.rho_block for i in range(len(sim.grid.segments)))
            m = sim.metrics()
            lost, thr, ovr = lost + m["bus_delay_ratio"], thr + m["throughput"], ovr + m["overrides"]
        n = seeds
        name = "adaptive" if adaptive else "fixed"
        print(f"{name:<10} {lost / n:>14.1%} {jam / n:>11.0f} {thr / n:>11.0f} {ovr / n:>10.0f}")


def run_window(sim: Simulation, screenshot: str | None, seconds: float) -> None:
    import pygame

    from .hud import UiState
    from .visualizer import WINDOW, make_views

    if screenshot:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    screen = pygame.display.set_mode(WINDOW)
    pygame.display.set_caption("Smart City Traffic Demo")
    city, hud = make_views(sim)
    ui = UiState()

    def refresh_api() -> None:
        ui.api_snapshot = sim.api.recent(len(sim.fleet.buses))[-8:]

    def draw() -> None:
        city.draw(screen, sim, ui)
        hud.draw(screen, sim, ui)
        pygame.display.flip()

    if screenshot:
        sim.run(seconds)
        refresh_api()
        draw()
        pygame.image.save(screen, screenshot)
        print(f"saved {screenshot} at t={sim.t:.0f}s")
        pygame.quit()
        return

    clock = pygame.time.Clock()
    acc = 0.0
    wall = 0.0
    running = True
    while running:
        real = min(clock.tick(60) / 1000.0, 0.1)
        wall += real
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif ev.key == pygame.K_SPACE:
                    ui.paused = not ui.paused
                elif ev.key == pygame.K_b:
                    sim.set_adaptive(not sim.adaptive)
                elif ev.key == pygame.K_i:
                    sim.trigger_incident()
                elif ev.key == pygame.K_h:
                    ui.heat = (ui.heat + 1) % 3
                elif ev.key == pygame.K_r:
                    sim = Simulation(sim.cfg, adaptive=sim.adaptive, incident_at=sim.incident_at)
                    acc = 0.0
                elif ev.key in (pygame.K_1, pygame.K_2, pygame.K_4, pygame.K_8):
                    ui.speed = float(pygame.key.name(ev.key))
        if not ui.paused:
            acc += real * ui.speed
            steps = 0
            while acc >= sim.cfg.dt and steps < 120:
                sim.step()
                acc -= sim.cfg.dt
                steps += 1
        if wall >= 0.25:  # the raw stream is 160 msg/s; sample it so the HUD stays readable
            wall = 0.0
            refresh_api()
        draw()
    pygame.quit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Smart city adaptive traffic-light demo")
    ap.add_argument("--fixed", action="store_true", help="start with fixed timers instead of adaptive control")
    ap.add_argument("--incident-at", type=float, default=None, help="sim seconds at which the central lane is blocked")
    ap.add_argument("--seed", type=int, default=Config().seed)
    ap.add_argument("--compare", action="store_true", help="headless fixed vs adaptive comparison table")
    ap.add_argument("--seconds", type=float, default=600.0, help="duration for --compare / --screenshot")
    ap.add_argument("--seeds", type=int, default=4, help="number of seeds for --compare")
    ap.add_argument("--screenshot", metavar="FILE", help="render one frame to FILE after --seconds and exit")
    args = ap.parse_args()

    if args.compare:
        compare(args.seconds, args.seeds)
        return
    sim = Simulation(Config(seed=args.seed), adaptive=not args.fixed, incident_at=args.incident_at)
    run_window(sim, args.screenshot, args.seconds)


if __name__ == "__main__":
    main()
