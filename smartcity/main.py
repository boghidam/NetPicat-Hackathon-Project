"""Entry point: interactive demo, headless comparison, or a single screenshot.

python -m smartcity.main                       # interactive window (Iași, central transit spine)
python -m smartcity.main --incident-at 60      # stall a vehicle on the busiest bus corridor after 60 s
python -m smartcity.main --map grid            # the small synthetic city instead
python -m smartcity.main --compare             # headless fixed-timer vs adaptive table
python -m smartcity.main --screenshot out.png --seconds 200 --incident-at 60
"""

from __future__ import annotations

import argparse
import os
import time

from .config import Config
from .data import build_city
from .simulation import Simulation


def compare(seconds: float, seeds: int, map_name: str) -> None:
    cfg = Config()
    net = build_city(map_name, cfg)  # the network is shared; each run builds its own world
    print(
        f"{map_name}: a vehicle stalls on the busiest bus corridor at t=120 s; {seeds} seeds x {seconds:.0f} s each.\n"
    )
    print(
        f"{'mode':<10} {'bus time lost':>14} {'car speed':>10} {'jam edge-s':>11} {'cars exited':>12} {'adjustments':>12}"
    )
    for adaptive in (False, True):
        lost = speed = jam = thr = ovr = 0.0
        for seed in range(1, seeds + 1):
            sim = Simulation(Config(seed=seed), adaptive=adaptive, incident_at=120.0, net=net)
            for _ in range(int(seconds / 5)):
                sim.run(5)
                jam += 5 * sum(sim.model.density(s.id) > 0.5 for s in net.segments)
            m = sim.metrics()
            lost, speed, thr, ovr = (
                lost + m["bus_delay_ratio"],
                speed + m["car_speed"],
                thr + m["throughput"],
                ovr + m["overrides"],
            )
        n = seeds
        name = "adaptive" if adaptive else "fixed"
        print(f"{name:<10} {lost / n:>14.1%} {speed / n:>10.0%} {jam / n:>11.0f} {thr / n:>12.0f} {ovr / n:>12.0f}")


def run_window(sim: Simulation, screenshot: str | None, seconds: float) -> None:
    import pygame

    from .hud import UiState, snapshot_buses
    from .visualizer import WINDOW, make_views

    if screenshot:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    screen = pygame.display.set_mode(WINDOW)
    pygame.display.set_caption("Smart City Traffic Demo: Iași")
    city, hud = make_views(sim)
    ui = UiState()

    def draw() -> None:
        screen.fill((11, 13, 19))
        city.draw(screen, sim, ui)
        hud.draw(screen, sim, ui)
        pygame.display.flip()

    if screenshot:
        sim.run(seconds)
        ui.rows = snapshot_buses(sim)
        draw()
        pygame.image.save(screen, screenshot)
        print(f"saved {screenshot} at t={sim.t:.0f}s")
        pygame.quit()
        return

    clock = pygame.time.Clock()
    acc = 0.0
    wall = 0.0
    dragging = False
    running = True
    pan_step = 40
    while running:
        real = min(clock.tick(60) / 1000.0, 0.1)
        wall += real
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.MOUSEWHEEL:
                city.zoom(pygame.mouse.get_pos(), 1.15**ev.y)
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                dragging = city.rect.collidepoint(ev.pos)
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                dragging = False
            elif ev.type == pygame.MOUSEMOTION and dragging:
                city.drag(ev.rel)
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
                elif ev.key == pygame.K_l:
                    ui.show_lines = not ui.show_lines
                elif ev.key == pygame.K_f:
                    city.fit()
                elif ev.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                    city.zoom_centre(1.25)
                elif ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    city.zoom_centre(0.8)
                elif ev.key == pygame.K_LEFT:
                    city.drag((pan_step, 0))
                elif ev.key == pygame.K_RIGHT:
                    city.drag((-pan_step, 0))
                elif ev.key == pygame.K_UP:
                    city.drag((0, pan_step))
                elif ev.key == pygame.K_DOWN:
                    city.drag((0, -pan_step))
                elif ev.key == pygame.K_r:
                    sim = Simulation(sim.cfg, adaptive=sim.adaptive, incident_at=sim.incident_at, net=sim.net)
                    acc = 0.0
                elif ev.key in (pygame.K_1, pygame.K_2, pygame.K_4, pygame.K_8):
                    ui.speed = float(pygame.key.name(ev.key))
        if not ui.paused:
            acc += real * ui.speed
            budget = time.perf_counter() + 0.012  # keep the window responsive at high speed
            while acc >= sim.cfg.dt and time.perf_counter() < budget:
                sim.step()
                acc -= sim.cfg.dt
            acc = min(acc, 0.5)
        if wall >= 0.25:  # the raw stream is hundreds of msgs/s; sample it so the HUD stays readable
            wall = 0.0
            ui.rows = snapshot_buses(sim)
        draw()
    pygame.quit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Smart city adaptive traffic-light demo on the real Iași road network")
    ap.add_argument("--map", choices=("iasi", "grid"), default="iasi", help="road network to simulate")
    ap.add_argument("--fixed", action="store_true", help="start with fixed timers instead of adaptive control")
    ap.add_argument(
        "--incident-at", type=float, default=None, help="sim seconds at which a vehicle stalls on the busiest corridor"
    )
    ap.add_argument("--seed", type=int, default=Config().seed)
    ap.add_argument(
        "--demand",
        type=float,
        default=None,
        help=f"cars per second entering at each gate (default {Config().gate_demand})",
    )
    ap.add_argument("--compare", action="store_true", help="headless fixed vs adaptive comparison table")
    ap.add_argument("--seconds", type=float, default=600.0, help="duration for --compare / --screenshot")
    ap.add_argument("--seeds", type=int, default=4, help="number of seeds for --compare")
    ap.add_argument("--screenshot", metavar="FILE", help="render one frame to FILE after --seconds and exit")
    args = ap.parse_args()

    if args.compare:
        compare(args.seconds, args.seeds, args.map)
        return
    cfg = Config(seed=args.seed) if args.demand is None else Config(seed=args.seed, gate_demand=args.demand)
    sim = Simulation(cfg, adaptive=not args.fixed, incident_at=args.incident_at, map_name=args.map)
    run_window(sim, args.screenshot, args.seconds)


if __name__ == "__main__":
    main()
