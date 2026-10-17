"""All tunable parameters in one frozen dataclass (see ARCHITECTURE.md sections 4-6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- city grid -------------------------------------------------------
    rows: int = 4
    cols: int = 4
    block_length: float = 200.0  # m between adjacent intersections
    seed: int = 7

    # --- simulation clock ------------------------------------------------
    dt: float = 0.1  # s per simulation tick (10 Hz API)
    decision_period: float = 0.5  # s between controller decisions / coordinator refresh

    # --- hidden traffic model (macroscopic) ------------------------------
    free_speed: float = 12.0  # m/s
    min_speed: float = 0.4  # m/s floor, even at jam density
    jam_density: float = 0.2  # veh/m (two lanes)
    sat_flow: float = 0.7  # veh/s discharged per green approach
    base_demand: float = 0.09  # veh/s entering each segment from side streets
    demand_jitter: float = 0.25  # +- fraction of per-segment demand heterogeneity
    initial_fill: float = 0.15  # initial density as a fraction of jam density
    turn_straight: float = 0.70
    turn_left: float = 0.15
    turn_right: float = 0.15

    # --- buses -----------------------------------------------------------
    buses_per_route: int = 2
    bus_gap: float = 14.0  # m minimum spacing between buses on a segment
    bus_accel: float = 1.5  # m/s^2
    bus_brake: float = 2.5  # m/s^2
    lane_offset: float = 5.0  # m lateral offset so opposite directions don't overlap

    # --- signals ---------------------------------------------------------
    base_green: float = 28.0  # s, the "standard timer"
    yellow: float = 2.0
    min_green: float = 10.0
    max_green: float = 50.0

    # --- congestion estimator -------------------------------------------
    est_alpha: float = 0.3  # EWMA smoothing
    est_skip_start: float = 30.0  # m: ignore samples while still accelerating off the line
    est_skip_end: float = 40.0  # m: ignore samples in the stop-line braking zone
    est_stale_tau: float = 90.0  # s: confidence decay time constant
    est_prior_load: float = 0.25  # load assumed for segments no bus has seen recently

    # --- local optimisation ---------------------------------------------
    v_slow_frac: float = 0.5  # bus slower than this fraction of free speed => slow
    d_trigger: float = 120.0  # m from stop line within which a slow bus counts
    ext_gain: float = 10.0  # s: extension = gain * (1 - speed / v_slow)
    ext_remaining: float = 6.0  # only extend when this little green remains
    ext_cooldown: float = 3.0
    early_switch_margin: float = 0.3  # score gap that justifies ending a green early

    # --- global optimisation --------------------------------------------
    rho_gate: float = 0.55  # downstream load above which releases are metered
    rho_block: float = 0.85  # downstream load at which the movement is not served
    w_pressure: float = 0.35
    w_bus_delay: float = 0.25
    w_forecast: float = 0.20
    w_age: float = 0.10
    w_downstream: float = 0.30
    age_full: float = 60.0  # s of red at which the age term saturates
    split_eps: float = 0.15  # keeps splits at the base timer when scores are all low
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
    pred_hop2_decay: float = 0.5  # confidence multiplier for the second hop
    pred_decay_tau: float = 40.0  # s: how quickly a forecast's weight fades with ETA
    lead_window: float = 10.0  # s before ETA in which the target pre-adjusts
    pred_density_min: float = 0.45  # predicted density that justifies pre-adjusting
    pred_ext: float = 7.0  # s of predictive green extension

    # --- incident -------------------------------------------------------
    incident_outflow_mult: float = 0.25  # blocked lane: discharge multiplier
    incident_demand_mult: float = 2.0
    incident_duration: float = 240.0  # s
