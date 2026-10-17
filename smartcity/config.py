"""All tunable parameters in one frozen dataclass (see ARCHITECTURE.md sections 4-6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- road network ----------------------------------------------------
    box_half: float = 14.0  # m from an intersection centre to its stop lines (half the junction box)
    min_approaches: int = 3  # a node with at least this many incoming roads gets signals
    gate_band: float = 150.0  # m: nodes this close to the map edge can be gates where cars enter and leave
    max_gates: int = 10
    seed: int = 7

    # --- simulation clock ------------------------------------------------
    dt: float = 0.1  # s per simulation tick (10 Hz API)
    decision_period: float = 0.5  # s between controller decisions / coordinator refresh

    # --- vehicles --------------------------------------------------------
    car_speed_var: float = 0.08
    car_length: float = 4.5
    bus_length: float = 11.0
    min_gap: float = 2.0  # m bumper-to-bumper when stopped
    accel: float = 1.8  # m/s^2
    comfort_brake: float = 3.0  # m/s^2 used to plan stops and following distance
    max_brake: float = 6.0  # m/s^2 emergency limit
    sat_flow: float = 0.5  # veh/s one lane discharges; sets the headway when throttled
    lane_offset: float = 3.0  # m lateral offset so opposite directions don't overlap

    # --- cars ------------------------------------------------------------
    gate_demand: float = 0.08  # veh/s entering at each gate
    initial_cars: int = 150
    spawn_backlog_max: int = 40  # cars waiting outside a blocked gate before new ones are dropped

    # --- buses -----------------------------------------------------------
    bus_headway_m: float = 3500.0  # one bus per this much route length (at least 2 per line)
    max_buses_per_line: int = 5

    # --- signals (SUMO-style safety timings) ----------------------------------
    # Opposite streets are green together; yellow then all-red clears the box between phases.
    base_green: float = 20.0  # s, the "standard timer" per phase
    min_green: float = 7.0  # never shorter: prevents rapid flickering
    yellow: float = 3.0
    all_red: float = 2.0  # clearance before the conflicting phase starts
    max_green: float = 45.0

    # --- congestion estimator -------------------------------------------
    est_alpha: float = 0.3  # EWMA smoothing
    est_skip_start: float = 30.0  # m: ignore samples while still accelerating off the line
    est_skip_end: float = 40.0  # m: ignore samples in the stop-line braking zone
    est_stale_tau: float = 90.0  # s: confidence decay time constant
    est_prior_load: float = 0.25  # load assumed for segments no bus has seen recently

    # --- local optimisation ---------------------------------------------
    v_slow_frac: float = 0.5  # bus slower than this fraction of free speed => slow
    loop_windows: tuple[float, float, float] = (50.0, 150.0, 300.0)  # m before the stop line: virtual detection loops
    d_trigger: float = 150.0  # m: a slow bus inside this window counts as congestion at the line
    ext_gain: float = 8.0  # s: extension = gain * (1 - speed / v_slow)
    ext_remaining: float = 4.0  # only extend when this little green remains
    ext_cooldown: float = 3.0
    early_switch_margin: float = 0.3  # score gap that justifies ending a green early

    # --- global optimisation --------------------------------------------
    rho_gate: float = 0.65  # downstream load above which releases are metered
    rho_block: float = 0.85  # downstream load at which an approach is released at zero rate
    w_pressure: float = 0.35
    w_bus_delay: float = 0.25
    w_forecast: float = 0.20
    w_age: float = 0.10
    w_downstream: float = 0.30
    age_full: float = 60.0  # s of red at which the age term saturates
    split_eps: float = 0.15  # keeps splits at the base timer when scores are all low
    split_deadband: float = 0.15  # score share must differ from an even split by this much to change greens
    budget_window: float = 60.0  # s
    budget_seconds: float = 90.0  # max total extension seconds per window across the grid
    neighbour_window: float = 20.0
    neighbour_damping: float = 0.5

    # --- prediction -----------------------------------------------------
    pred_history: int = 3  # segments of bus history used as features
    pred_window: int = 200  # ridge training window
    pred_min_samples: int = 25  # switch from EWMA to ridge after this many outcomes
    pred_ridge_lambda: float = 1.0
    pred_refit_every: int = 10
    pred_ttl_margin: float = 25.0
    pred_hops: int = 3  # links ahead along the bus's route that each forecast covers
    pred_hop_decay: float = 0.6  # confidence multiplier per extra hop
    pred_decay_tau: float = 40.0  # s: how quickly a forecast's weight fades with ETA
    lead_window: float = 10.0  # s before ETA in which the target pre-adjusts
    pred_density_min: float = 0.2  # predicted density that justifies pre-adjusting
    pred_ext: float = 6.0  # s of predictive green extension

    # --- incident -------------------------------------------------------
    incident_duration: float = 150.0  # s a stalled vehicle blocks the lane
