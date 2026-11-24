"""All tunable parameters in one frozen dataclass (see ARCHITECTURE.md sections 4-6)."""

from __future__ import annotations

from dataclasses import dataclass

# Global traffic multiplier: 1.0 is 100% traffic. It scales the bus fleet and the car spawn rate together, so
# 2.0 doubles both and 0.0 empties the map. The runtime keys ([ ] and \) start from this value and move it.
TRAFFIC_AMOUNT = 1.0

# Realistic simultaneous weekday-peak bus counts for all 51 active CTP Iași + AMTPI metropolitan bus lines,
# derived from official sctpiasi.ro peak headways and round-trip cycle times (can also be synced live from Terra).
IASI_LINE_BUSES: dict[str, int] = {
    # Urban & Suburban CTP Iași lines (26)
    "30": 10,
    "19": 8,
    "42": 8,
    "43": 8,
    "46": 8,
    "52": 8,
    "20": 7,
    "27": 6,
    "28": 6,
    "44": 6,
    "41": 5,
    "47": 5,
    "36": 4,
    "43c": 4,
    "30b": 3,
    "29": 3,
    "18": 2,
    "50": 2,
    "54": 2,
    "23": 2,
    "6B": 2,
    "23b": 1,
    "48": 1,
    "49": 1,
    "51": 1,
    "53": 1,
    # Metropolitan AMTPI / CTP Iași lines (25)
    "101": 2,
    "102": 2,
    "103": 2,
    "104": 1,
    "201": 2,
    "202": 2,
    "203": 2,
    "204": 1,
    "301": 2,
    "401": 3,
    "501": 2,
    "501b": 1,
    "502": 2,
    "503": 2,
    "503b": 1,
    "601": 2,
    "602": 2,
    "603": 1,
    "701": 2,
    "702": 2,
    "801": 2,
    "802": 2,
    "802b": 1,
    "901": 3,
    "902": 2,
}


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
    # Calibrated to PMUD Iași / Ziarul de Iași peak perimeter entry flow (~4,200-4,600 veh/h across 16 gates)
    # and realistic simultaneous moving cars across the 428 km arterial core (excluding parked cars).
    gate_demand: float = 0.08  # veh/s entering at each gate (~4,600 veh/h across 16 gates)
    initial_cars: int = 1500
    spawn_backlog_max: int = 40  # cars waiting outside a blocked gate before new ones are dropped

    # --- residential OD & urban distribution (real city maps) ------------
    urban_radius_m: float = 4200.0  # m from Iași center defining urban core vs outer ring
    share_city_to_building: float = 0.60  # 60% building-destined city -> city (1-8 km)
    share_outer_to_building: float = 0.20  # 20% outside -> city building (5-20 km)
    share_through_city: float = 0.20  # 20% outside -> far outside gate crossing city (5-20 km)
    city_trip_min_m: float = 1000.0  # 1 km min for city -> city trips
    city_trip_max_m: float = 8000.0  # 8 km max for city -> city trips
    outer_trip_min_m: float = 5000.0  # 5 km min for outside -> city & outside -> outside trips
    outer_trip_max_m: float = 20000.0  # 20 km max for outside -> city & outside -> outside trips
    building_spawn_max_density: float = 0.30  # Wave 2 only spawns from uncrowded streets (<30% occupied)
    through_min_angle_deg: float = 90.0  # minimum angular separation between entry & exit gates

    # --- multi-lane, roundabouts & real-world Iași realism ---------------
    lane_width: float = 3.1  # m lateral spacing between bands in the same direction
    lane_change_cooldown: float = 3.5  # s minimum between lane changes per vehicle
    lane_change_duration: float = 1.4  # s smooth lateral animation duration
    roundabout_speed: float = 7.2  # m/s (~26 km/h) circulating speed inside roundabouts
    roundabout_tc_car: float = 3.6  # s critical gap for cars entering a roundabout
    roundabout_tc_bus: float = 4.8  # s critical gap for buses/trucks entering a roundabout
    roundabout_tf: float = 2.2  # s follow-up headway inside the same accepted circulating gap
    yield_tc: float = 3.4  # s critical gap at unsignalized Cedează T-junctions
    bus_stops_enabled: bool = True  # realistic 8-24s passenger boarding dwell at real CTP stops
    crosswalks_enabled: bool = True  # stochastic pedestrian crossing pulses at major Iași hubs
    rail_crossings_enabled: bool = True  # periodic CFR rail barrier pulses
    urban_friction_enabled: bool = False  # optional curbside double-parking ("5 min pe avarii") on 2-3 lane streets
    weather: str = "clear"  # "clear" | "rain" | "snow"
    day_cycle_enabled: bool = False  # optional 24h simulated city clock (06:00-22:00 rush waves)

    # --- rush hour at a key point (runtime option) ----------------------------
    rush_extra: float = 2.6  # extra car demand at every gate, as a multiple of normal, routed through the key point
    rush_reach: float = 2000.0  # m: the seeded commuters are placed this close (along their trip) to the key point
    rush_seed: int = 340  # cars placed on routes through the key point the moment rush hour starts (scaled for multi-lane boulevards)

    # --- buses -----------------------------------------------------------
    # On Iași, each line uses its realistic weekday-peak count from IASI_LINE_BUSES (all 51 lines).
    # On the synthetic grid, buses_per_line / backbone settings apply.
    buses_per_line: int = 4
    backbone_buses_min: int = 6
    backbone_buses_max: int = 8
    backbone_headway_m: float = 3300.0  # backbone buses = loop length / this, clamped to the range above
    # Terminal scheduling: instead of despawning, buses rest 5-15 min at each terminus and take a 15-30 min
    # driver break every 3-5 terminal arrivals.
    bus_layovers: bool = True
    bus_layover_min: float = 300.0  # s (5 IRL min)
    bus_layover_max: float = 900.0  # s (15 IRL min)
    bus_break_min: float = 900.0  # s (15 IRL min)
    bus_break_max: float = 1800.0  # s (30 IRL min)
    bus_break_every_min: int = 3
    bus_break_every_max: int = 5

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
    hold_horizon: float = 11.5  # s: the longest hold for one arriving bus (0 switches holding off)
    hold_margin: float = 1.0  # s added to the bus's arrival estimate
    hold_min_speed: float = 3.5  # m/s assumed for a bus rolling up in a discharging queue
    hold_window: float = 135.0  # m before the stop line that a bus must be within to be held for
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
        5.0  # extension seconds per signalised junction per window: scaled for 51 active CTP lines
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
