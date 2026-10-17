"""All tunable parameters in one frozen dataclass (see ARCHITECTURE.md sections 4-6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- city grid -------------------------------------------------------
    rows: int = 4
    cols: int = 4
    block_length: float = 200.0  # m between adjacent intersection centres
    gate_length: float = 100.0  # m of the entry/exit road beyond the west and east edge nodes
    box_half: float = 14.0  # m from an intersection centre to its stop lines (half the junction box)
    seed: int = 7

    # --- simulation clock ------------------------------------------------
    dt: float = 0.1  # s per simulation tick (10 Hz API)
    decision_period: float = 0.5  # s between controller decisions / coordinator refresh

    # --- vehicles --------------------------------------------------------
    free_speed: float = 12.0  # m/s (buses drive exactly this; cars vary a little)
    car_speed_var: float = 0.08
    car_length: float = 4.5
    bus_length: float = 11.0
    min_gap: float = 2.0  # m bumper-to-bumper when stopped
    accel: float = 1.8  # m/s^2
    comfort_brake: float = 3.0  # m/s^2 used to plan stops and following distance
    max_brake: float = 6.0  # m/s^2 emergency limit
    sat_flow: float = 0.5  # veh/s one lane discharges; sets the headway when throttled
    lane_offset: float = 5.0  # m lateral offset so opposite directions don't overlap

    # --- cars ------------------------------------------------------------
    gate_demand: float = 0.08  # veh/s entering at each gate
    initial_cars: int = 70
    route_follow: float = 0.8  # chance a car takes a turn that gets it closer to its exit
    spawn_backlog_max: int = 40  # cars waiting outside a blocked gate before new ones are dropped

    # --- buses -----------------------------------------------------------
    buses_per_route: int = 2

    # --- signals ---------------------------------------------------------
    # One street (approach) is green at a time, in sequence; yellow then all-red clears the box.
    base_green: float = 14.0  # s, the "standard timer" per approach
    yellow: float = 2.0
    all_red: float = 1.5
    min_green: float = 6.0
    max_green: float = 28.0

    # --- congestion estimator -------------------------------------------
    est_alpha: float = 0.3  # EWMA smoothing
    est_skip_start: float = 30.0  # m: ignore samples while still accelerating off the line
    est_skip_end: float = 40.0  # m: ignore samples in the stop-line braking zone
    est_stale_tau: float = 90.0  # s: confidence decay time constant
    est_prior_load: float = 0.25  # load assumed for segments no bus has seen recently

    # --- local optimisation ---------------------------------------------
    v_slow_frac: float = 0.5  # bus slower than this fraction of free speed => slow
    d_trigger: float = 120.0  # m from stop line within which a slow bus counts
    ext_gain: float = 8.0  # s: extension = gain * (1 - speed / v_slow)
    ext_remaining: float = 4.0  # only extend when this little green remains
    ext_cooldown: float = 3.0
    early_switch_margin: float = 0.3  # score gap that justifies ending a green early

    # --- global optimisation --------------------------------------------
    rho_gate: float = 0.65  # downstream load above which releases are metered
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
    pred_density_min: float = 0.2  # predicted density that justifies pre-adjusting
    pred_ext: float = 6.0  # s of predictive green extension

    # --- incident -------------------------------------------------------
    incident_duration: float = 150.0  # s a stalled vehicle blocks the lane
