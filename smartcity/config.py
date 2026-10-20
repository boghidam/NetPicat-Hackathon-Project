"""All tunable parameters in one frozen dataclass (see ARCHITECTURE.md sections 4-6)."""

from __future__ import annotations

from dataclasses import dataclass

# Global traffic multiplier: 1.0 is 100% traffic. It scales the bus fleet and the car spawn rate together, so
# 2.0 doubles both and 0.0 empties the map. The runtime keys ([ ] and \) start from this value and move it.
TRAFFIC_AMOUNT = 1.0


@dataclass(frozen=True)
class Config:
    traffic_amount: float = TRAFFIC_AMOUNT

    # --- road network ----------------------------------------------------
    box_half: float = 14.0  # m from an intersection centre to its stop lines (half the junction box)
    min_approaches: int = 3  # a node with at least this many incoming roads gets signals
    gate_band: float = 700.0  # m: nodes this close to the map edge can be gates where cars enter and leave
    max_gates: int = 16
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
    gate_demand: float = 0.03  # veh/s entering at each gate
    initial_cars: int = 450
    spawn_backlog_max: int = 40  # cars waiting outside a blocked gate before new ones are dropped

    # --- rush hour at a key point (runtime option) ----------------------------
    rush_extra: float = 2.0  # extra car demand at every gate, as a multiple of normal, routed through the key point
    rush_reach: float = 2000.0  # m: the seeded commuters are placed this close (along their trip) to the key point
    rush_seed: int = 120  # cars placed on routes through the key point the moment rush hour starts (at 100% traffic)

    # --- buses -----------------------------------------------------------
    # At 100% traffic: 4 buses active on each standard line; the backbone lines carry more (6 to 8, the longer
    # the line the more), always spaced evenly along their loop so they never bunch up.
    buses_per_line: int = 4
    backbone_buses_min: int = 6
    backbone_buses_max: int = 8
    backbone_headway_m: float = 3300.0  # backbone buses = loop length / this, clamped to the range above

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

    # --- sensing and green extension ------------------------------------------
    # Only buses are observed. Greens are never shortened; they are extended only when a bus that is
    # approaching a light covered very little distance during the light's last red-green cycle.
    loop_windows: tuple[float, float, float] = (50.0, 150.0, 300.0)  # m before the stop line: virtual detection loops
    cycle_window: float = 300.0  # m: buses within this road distance of a stop line are checked each cycle
    cycle_low_progress: float = 0.3  # "low": less than this share of the free-flow distance per cycle
    cycle_ext_max: float = 7.0  # s: the longest extension one cycle check can add (scaled by the shortfall)
    min_cycle: float = 20.0  # s: shorter gaps between greens are not judged

    # --- bus green hold (classic transit signal priority) -----------------------
    # When a green is about to end and a moving bus on it would reach the stop line a few seconds later, the green
    # is held just long enough for it to clear (never shortened).
    hold_horizon: float = 9.0  # s: the longest hold for one arriving bus (0 switches holding off)
    hold_margin: float = 1.0  # s added to the bus's arrival estimate
    hold_min_speed: float = 3.5  # m/s assumed for a bus rolling up in a discharging queue
    hold_window: float = 95.0  # m before the stop line that a bus must be within to be held for
    hold_min_openness: float = 0.55  # hold only if the streets ahead are at least this open (0..1)

    # --- online learning per junction (counterfactual odometer credit assignment) ---
    learn_rate: float = 0.08  # step size of the per-junction trust update after each extension (0 = no learning)
    learn_cost: float = 0.8  # weight of the seconds taken from the cross streets against a bus that got through

    # --- global optimisation --------------------------------------------
    rho_gate: float = 0.65  # downstream load above which releases are metered
    rho_block: float = 0.85  # downstream load at which an approach is released at zero rate
    hop2_weight: float = 0.95  # how much the second junction downstream counts against lengthening a green
    budget_window: float = 60.0  # s
    budget_per_junction: float = (
        3.5  # extension seconds per signalised junction per window: the cap grows with the network
    )
    budget_min: float = 30.0  # s: the floor for a small network, where 1.2 s per signal would allow almost nothing
    budget_seconds: float | None = None  # an explicit network-wide cap that replaces the per-junction one
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
    pred_horizon: float = 30.0  # s: a forecast for a bus due within this long can extend the green that is starting
    pred_density_min: float = 0.2  # predicted density that counts as predicted low progress
    pred_ext: float = 2.0  # s of predictive green extension

    # --- incident -------------------------------------------------------
    incident_duration: float = 150.0  # s a stalled vehicle blocks the lane
