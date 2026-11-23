"""Entry point: interactive demo, headless comparison, multi-episode training, or a single screenshot.

python -m smartcity.main                              # interactive window (Iași, default slot)
python -m smartcity.main --slot rush_hour_exp         # load or create named save slot data/slots/rush_hour_exp.json
python -m smartcity.main --slot slot_1 --reset-slot   # reset slot_1 to clean defaults and train from scratch
python -m smartcity.main --incident-at 60             # stall a vehicle on the busiest bus corridor after 60 s
python -m smartcity.main --map grid                   # the small synthetic city instead
python -m smartcity.main --traffic 300                # three times the normal cars and buses (0 to 1000)
python -m smartcity.main --rush "Podu Roș"            # start with a key point under rush-hour pressure
python -m smartcity.main --compare                    # headless fixed-timer vs adaptive table
python -m smartcity.main --train 3 --slot slot_1      # run 3 RL training episodes into slot_1
python -m smartcity.main --screenshot out.png --seconds 200 --incident-at 60
"""

from __future__ import annotations

import argparse
import math
import os
import time

from .config import Config
from .data import build_city
from .learning import LEARNED_KINDS, merge_memories, merge_predictor_states, train_policy
from .simulation import Simulation
from .slots import DEFAULT_SLOT_NAME, SlotManager, SlotRuntime


def compare(
    seconds: float,
    seeds: int,
    map_name: str,
    demand: float | None = None,
    incident_at: float | None = None,
    traffic: float | None = None,
    rush: str | None = None,
    slot_name: str = DEFAULT_SLOT_NAME,
    from_clean: bool = False,
    reset_slot: bool = False,
    manager: SlotManager | None = None,
) -> None:
    mgr = manager or SlotManager()
    safe_slot = mgr.sanitize_name(slot_name)
    if reset_slot:
        slot_data = mgr.reset_slot(safe_slot, map_name=map_name)
    elif from_clean:
        slot_data = mgr.clean_state(safe_slot, map_name=map_name)
    else:
        slot_data = mgr.load_slot(safe_slot, map_name=map_name, fallback_to_default=True)

    cfg = Config() if demand is None else Config(gate_demand=demand)
    inc = 120.0 if incident_at is None else incident_at
    net = build_city(map_name, cfg)  # the network is shared; each run builds its own world
    extras = []
    if safe_slot != DEFAULT_SLOT_NAME or from_clean or reset_slot:
        extras.append(f"slot {safe_slot}{' (clean)' if (from_clean or reset_slot) else ''}")
    if traffic is not None:
        extras.append(f"traffic {traffic:g}%")
    if rush is not None:
        extras.append(f"rush hour at {rush}")
    extra_str = f" ({', '.join(extras)})" if extras else ""
    print(
        f"{map_name}{extra_str}: a vehicle stalls on the busiest bus corridor at t={inc:.0f} s; "
        f"{seeds} seeds x {seconds:.0f} s each.\n"
    )
    print(
        f"{'mode':<10} {'bus time lost':>14} {'car speed':>10} {'jam edge-s':>11} {'cars exited':>12} {'adjustments':>12}"
    )
    memories: list[dict] = []
    pred_states: list[dict] = []
    dec = wst = mis = 0
    for adaptive in (False, True):
        lost = speed = jam = thr = ovr = 0.0
        for seed in range(1, seeds + 1):
            run_cfg = Config(seed=seed) if demand is None else Config(seed=seed, gate_demand=demand)
            sim = Simulation(
                run_cfg,
                adaptive=adaptive,
                incident_at=inc,
                net=net,
                map_name=map_name,
                memory=slot_data if adaptive else None,
            )
            apply_start_options(sim, traffic, rush)
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
            if adaptive and sim.coord.learner is not None:
                lrn = sim.coord.learner
                memories.append(lrn.memory())
                pred_states.append(sim.predictor.export_state())
                for kind in LEARNED_KINDS:
                    dec += lrn.outcomes[kind]["decisive"]
                    wst += lrn.outcomes[kind]["wasted"]
                    mis += lrn.outcomes[kind]["missed"]
        n = seeds
        name = "adaptive" if adaptive else "fixed"
        print(f"{name:<10} {lost / n:>14.1%} {speed / n:>10.0%} {jam / n:>11.0f} {thr / n:>12.0f} {ovr / n:>12.0f}")
    if memories and map_name == "iasi" and seconds >= 120.0:
        merged_pol = merge_memories(memories)
        slot_data["policy"] = merged_pol
        slot_data["predictor"] = merge_predictor_states(pred_states)
        slot_data["metadata"]["episodes"] = int(merged_pol.get("episodes", slot_data["metadata"].get("episodes", 0)))
        slot_data["metadata"]["sim_seconds_trained"] = round(
            float(slot_data["metadata"].get("sim_seconds_trained", 0.0)) + seeds * seconds,
            2,
        )
        mgr.save_slot(safe_slot, slot_data)
        print(
            f"\nlearned [slot '{safe_slot}', {slot_data['metadata']['episodes']} episodes]: "
            f"{dec} decisive, {wst} wasted, {mis} missed extensions judged this run."
        )


def apply_start_options(sim: Simulation, traffic: float | None, rush: str | None) -> None:
    """Start (or restart) a simulation at a given traffic amount and with a key point under rush-hour pressure."""
    if traffic is not None:
        sim.set_traffic_percent(traffic)
    if rush is not None:
        sim.set_rush_point(rush)


def run_window(
    sim: Simulation,
    screenshot: str | None,
    seconds: float,
    runtime: SlotRuntime | None = None,
) -> None:
    import pygame

    from .hud import UiState, snapshot_buses
    from .visualizer import WINDOW, make_views

    if screenshot:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    try:
        screen = pygame.display.set_mode(WINDOW)
        pygame.display.set_caption("Smart City Traffic Demo: Iași")
        city, hud = make_views(sim)
        ui = UiState(rows=snapshot_buses(sim))

        def draw() -> None:
            screen.fill((11, 13, 19))
            city.draw(screen, sim, ui)
            hud.draw(screen, sim, ui)
            pygame.display.flip()

        if screenshot:
            if runtime is not None:
                runtime.run(seconds)
            else:
                sim.run(seconds)
            ui.rows = snapshot_buses(sim)
            draw()
            pygame.image.save(screen, screenshot)
            print(f"saved {screenshot} at t={sim.t:.0f}s")
            return

        speed_keys = {
            pygame.K_1: 1.0,
            pygame.K_2: 2.0,
            pygame.K_4: 4.0,
            pygame.K_8: 8.0,
            pygame.K_KP_1: 1.0,
            pygame.K_KP_2: 2.0,
            pygame.K_KP_4: 4.0,
            pygame.K_KP_8: 8.0,
        }
        clock = pygame.time.Clock()
        acc = 0.0
        wall = 0.0
        dragging = False
        press_pos: tuple[int, int] | None = None
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
                    press_pos = ev.pos
                elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                    dragging = False
                    if press_pos is not None and math.dist(press_pos, ev.pos) < 5:  # a click, not a drag
                        name = city.landmark_at(ev.pos)
                        if name is not None:  # click a key point to put it under rush-hour pressure (again to lift it)
                            sim.set_rush_point(None if sim.rush_point == name else name)
                    press_pos = None
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
                    elif ev.key == pygame.K_RIGHTBRACKET:
                        sim.step_traffic(+1)
                    elif ev.key == pygame.K_LEFTBRACKET:
                        sim.step_traffic(-1)
                    elif ev.key == pygame.K_BACKSLASH:
                        sim.set_traffic_percent(100)
                    elif ev.key == pygame.K_k:
                        sim.cycle_rush_point(-1 if ev.mod & pygame.KMOD_SHIFT else 1)
                    elif ev.key == pygame.K_r:
                        settings = (sim.traffic_percent, sim.rush_point)  # a reset keeps the traffic settings
                        if runtime is not None:
                            runtime.flush()
                        sim = Simulation(
                            sim.cfg,
                            adaptive=sim.adaptive,
                            incident_at=sim.incident_at,
                            net=sim.net,
                            memory=runtime.data if runtime is not None else None,
                        )
                        if runtime is not None:
                            runtime.attach(sim)
                        apply_start_options(sim, *settings)
                        ui.rows = snapshot_buses(sim)
                        acc = 0.0
                        wall = 0.0
                    elif ev.key in speed_keys:
                        ui.speed = speed_keys[ev.key]
            if not ui.paused:
                acc += real * ui.speed
                budget = time.perf_counter() + 0.012  # keep the window responsive at high speed
                while acc >= sim.cfg.dt and time.perf_counter() < budget:
                    sim.step()  # strictly in-memory; zero disk I/O per tick
                    acc -= sim.cfg.dt
                acc = min(acc, 0.5)
                if runtime is not None:
                    runtime.maybe_checkpoint()
            if wall >= 0.25:  # the raw stream is hundreds of msgs/s; sample it so the HUD stays readable
                wall = 0.0
                ui.rows = snapshot_buses(sim)
            draw()
    finally:
        if runtime is not None:
            runtime.close()
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
    ap.add_argument(
        "--traffic",
        type=float,
        default=None,
        metavar="PERCENT",
        help="amount of cars and buses relative to normal: 100 is normal, 0 is none, up to 1000",
    )
    ap.add_argument(
        "--rush",
        default=None,
        metavar="KEY_POINT",
        help="start with a key point under rush-hour pressure (Copou, Păcurari, Gara Iași, Piața Unirii, "
        "Bd. Independenței, Palas, Podu Roș, Tudor Vladimirescu)",
    )
    ap.add_argument(
        "--slot",
        default=DEFAULT_SLOT_NAME,
        metavar="NAME",
        help=f"named ML save slot in data/slots/<NAME>.json (default: {DEFAULT_SLOT_NAME})",
    )
    ap.add_argument(
        "--clean",
        action="store_true",
        help="start from a clean default ML state without deleting existing slot files",
    )
    ap.add_argument(
        "--reset-slot",
        action="store_true",
        help="reset the selected --slot file to clean factory defaults before starting",
    )
    ap.add_argument("--list-slots", action="store_true", help="list saved ML slots in data/slots/ and exit")
    ap.add_argument("--compare", action="store_true", help="headless fixed vs adaptive comparison table")
    ap.add_argument(
        "--train",
        type=int,
        nargs="?",
        const=3,
        default=None,
        metavar="EPISODES",
        help="run multi-episode reinforcement learning into the selected --slot",
    )
    ap.add_argument("--seconds", type=float, default=600.0, help="duration for --compare / --screenshot")
    ap.add_argument("--seeds", type=int, default=4, help="number of seeds for --compare")
    ap.add_argument("--screenshot", metavar="FILE", help="render one frame to FILE after --seconds and exit")
    ap.add_argument("--web", action="store_true", help="launch the Noodle Traffic Control Web UI (FastAPI + WebSocket)")
    ap.add_argument("--host", default="127.0.0.1", help="host address for --web server (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="port for --web server (default: 8000)")
    args = ap.parse_args()

    mgr = SlotManager()
    if args.list_slots:
        slots = mgr.list_slots()
        print("Saved slots:" if slots else "No saved slots yet.")
        for s in slots:
            print(f"  - {s}")
        return

    try:
        safe_slot = mgr.sanitize_name(args.slot)
    except ValueError as err:
        ap.error(str(err))

    if args.traffic is not None and not 0 <= args.traffic <= 1000:
        ap.error("--traffic must be between 0 and 1000")
    if args.web:
        from .web import run_server

        run_server(host=args.host, port=args.port, slot_name=safe_slot)
        return
    if args.train is not None:
        train_policy(
            episodes=max(1, args.train),
            seconds=args.seconds,
            map_name=args.map,
            slot_name=safe_slot,
            from_clean=args.clean,
            reset_slot=args.reset_slot,
            manager=mgr,
        )
        return
    if args.compare:
        try:
            compare(
                args.seconds,
                args.seeds,
                args.map,
                demand=args.demand,
                incident_at=args.incident_at,
                traffic=args.traffic,
                rush=args.rush,
                slot_name=safe_slot,
                from_clean=args.clean,
                reset_slot=args.reset_slot,
                manager=mgr,
            )
        except ValueError as err:
            ap.error(str(err))
        return

    cfg = Config(seed=args.seed) if args.demand is None else Config(seed=args.seed, gate_demand=args.demand)
    runtime = SlotRuntime(
        slot_name=safe_slot,
        map_name=args.map,
        from_clean=args.clean,
        reset_slot=args.reset_slot,
        manager=mgr,
    )
    try:
        sim = Simulation(
            cfg,
            adaptive=not args.fixed,
            incident_at=args.incident_at,
            map_name=args.map,
            memory=runtime.data,
        )
        runtime.attach(sim)
        apply_start_options(sim, args.traffic, args.rush)
    except ValueError as err:
        runtime.close()
        ap.error(str(err))
    run_window(sim, args.screenshot, args.seconds, runtime=runtime)


if __name__ == "__main__":
    main()
