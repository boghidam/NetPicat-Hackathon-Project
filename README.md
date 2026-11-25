# Noodle Traffic Control — Iași Smart Transit & Adaptive Signal AI

**Noodle Traffic Control** is a real-time adaptive traffic-signal control platform and microscopic urban traffic digital twin built on the **real road network of Iași, Romania** (~18 × 15 km, from Breazu & Copou to Ciurea, CUG, Păcurari, and Tătărași).

Instead of requiring expensive street-level camera networks or inductive road loops, the AI observes **only public transit bus GPS telemetry** through virtual detection loops (`50 / 150 / 300 m` ahead of every stop line), forecasts downstream bottlenecks with online Ridge regression, and dynamically holds or extends green phases and meters upstream inflows — cutting bus delay while keeping general traffic flowing and strictly obeying SUMO-style signal safety timings.

![Noodle Traffic Control — Iași Live Dashboard](docs/demo.png)

---

## Two Operating Modes

- **`Aether (Simulation)`** — Full microscopic traffic digital twin of Iași running 51 real CTP Iași bus lines, ~1,500 OD-routed vehicles, 16 Romanian car roundabouts, 14 CTP *rond* transit loops, 115 signalized intersections, and live interactive scenario controls (Weather, 24h Rush Clock, Rush Hour hotspots, Incidents, Urban Double-Parking, and Emergency Ambulance green-wave preemption).
- **`Terra (Real)`** — Live real-world transit monitor streaming real-time CTP Iași bus GPS positions and statuses via the **Tranzy Open Data API** (Agency `#1`), with vectorized GPS map-matching onto the Iași road graph, live AI green-hold advisories, one-click live fleet-count sync back into `Aether`, masked local credential storage, and an extensible `CTPPrivateProvider` stub for direct future AVL partnerships.

---

## Real-World Iași Simulation Fidelity

- **Multi-Lane Mixed-Traffic Road Network (263 Nodes, 667 Directed Segments):**
  Real 1-, 2-, and 3-lane per-direction Iași boulevards (`356` 1-lane, `226` 2-lane, `85` 3-lane segments) with MOBIL overtaking, mandatory turn-lane pre-selection, smooth lateral lane changes, and single-lane incident bypasses. Coincident-centerline detection (`2.5 m` tolerance) accurately distinguishes shared two-way streets from physically separated OSM dual carriageways (`6–67 m` apart). All streets reflect Iași's mixed-traffic carriageways.
- **Complete CTP Iași Bus Fleet (51 Lines, 160 Buses):**
  Includes all **29 urban** (`6B`, `18`, `19`, `20`, `23`, `23b`, `27`, `28`, `29`, `30`, `30b`, `36`, `41`, `42`, `43`, `43c`, `44`, `46`, `47`, `48`, `49`, `50`, `51`, `52`, `53`, `54`) and **22 metropolitan** AMTPI lines (`101–902`) with realistic weekday-peak per-line fleet counts (`IASI_LINE_BUSES`), articulated (`12.6 m`), standard (`11.0 m`), and minibus (`8.4 m`) profiles, **222 deduplicated real CTP bus stops** (curbside & recessed bays with passenger boarding dwell), and realistic terminus layovers (`5–15 min`) plus periodic driver breaks (`15–30 min` every `3–5` terminal arrivals).
- **16 Real Romanian Car Roundabouts (*Sensuri Giratorii*, OUG 195/2002 & HG 130/2023):**
  **3 signalized mega-roundabouts** (Podu Roș, Podu de Piatră, Fundație / Piața Mihai Eminescu) and **13 yield-to-left roundabouts** (Zimbru, Pasaj Octav Băncilă / ANL Zimbru, Păcurari, Agronomie / Copou, Râpa Galbenă / Gării, Bd. Socola / Granit, Ciric / Aeroport, 3× Centura Iași, Aroneanu, Horpaz / Valea Adâncă, CUG / Kaufland–Expo) with circular counterclockwise kinematics, rim-to-rim entry/exit, gap acceptance ($T_c, T_f$), inner-to-outer (*Bandă 2 → Bandă 1*) exit yielding, and HG 130/2023 extra-loop fallback when Bandă 1 is full.
- **14 CTP *Rond* Turnaround Loops, 35 *Cedează* Yield Junctions & 21 Curated Junction Names:**
  Separately maps all **14 real-world CTP Iași *rond* transit turnaround loops** (`R` island markers for Dacia, Canta, CUG/Tehnopolis, Vechi CUG, Piața Gării, Târgu Cucu, Tătărași Nord/Sud, Baza 3, Copou/Agronomie, Triumf, Tudor Vladimirescu, Dancu, Bucium), **35 unsignalized *Cedează* yield junctions** (minor 3-arm T-junctions and 2-in merges prioritized by road hierarchy and street continuity), unprotected left-turn yielding, and hover/tap inspection cards for buses, roundabouts, *rond* loops, named junctions, and buildings.
- **Residential Origin-Destination Traffic (1,183 Apartment Blocks & Houses):**
  Cars follow door-to-door trips (`60%` city-to-building 1–8 km, `20%` outer-to-building 5–20 km, `20%` cross-city through-traffic 5–20 km), keeping ~70–75% of active vehicles inside Iași's urban core and spawning follow-up trips from uncrowded residential streets (`< 30%` occupancy). Car traffic load scales cars independently without altering the scheduled CTP bus fleet.
- **Everyday Iași Urban Dynamics & Interactive Scenarios:**
  Heterogeneous vehicle mix (cars, delivery vans, heavy trucks, CTP buses, emergency ambulances), **Iași's 7 hills** (`249` sloped segments affecting truck/bus climb acceleration), **34 pedestrian zebra crosswalks**, **2 CFR railway crossings**, **4 hospitals** (Sf. Spiridon, C.I. Parhon, Sf. Maria, Neurochirurgie) for emergency **Ambulance** dispatch with automatic signal green-wave preemption, optional **Urban Friction** double-parking (*"5 min pe avarii"*), **Weather** presets (`Clear`, `Rain`, `Snow`), and a **24h Day-Cycle** rush clock.

---

## How the Adaptive AI Works

1. **Safety-First Signal Timing:** Opposite approaches share a phase (N+S, then E+W) with SUMO-compliant rules — at least `7 s` green (`base_green = 20 s`, `max_green = 45 s`), `3 s` yellow, and `2 s` all-red clearance. Green phases are **never cut short**; the controller only holds or extends green when bus evidence justifies it.
2. **Bus-Only Virtual Detection Loops & Stop Awareness:** Buses publish telemetry (`speed`, `segment`, `position`, `at_stop`). Stationary passenger boarding at CTP stops and resting buses at termini are explicitly excluded so parked or boarding buses never trigger false congestion or wasted green holds.
3. **Bus Green Holds, Counterfactual RL & Persistent ML Save Slots:** Approaching buses within `135 m` trigger real-time **Bus Green Holds** (up to `11.5 s`, gated by downstream openness), supplemented by per-cycle progress checks (up to `7.0 s`), turn-share-weighted upstream backpressure throttling, and an online Ridge regression route predictor (`PostIntersectionPredictor`). Every decision is self-graded after yellow via the bus odometer (`decisive` / `wasted` / `missed`) to update per-junction trust (`JunctionLearner`), persisted atomically in versioned schema-v2 JSON save slots (`data/slots/*.json`).
4. **Responsive Desktop & Mobile Dashboard:** Desktop uses a collapsible floating Executive Impact drawer; mobile switches automatically to a swipeable 3-snap bottom sheet (`peek` / `half` / `full`) with `Controls` and `Impact & Fleet` tabs, touch pan/pinch-zoom, and tap-to-inspect map and KPI tooltips.

---

## Quick Start

### 1. Install Dependencies

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
pip install fastapi uvicorn requests
```

*(Note: `fastapi`, `uvicorn`, and `requests` power the Web UI & Tranzy API; `osmnx` and `shapely` are only needed when rebuilding raw OSM datasets.)*

### 2. Run the Interactive Web Dashboard (Recommended)

```bash
python -m smartcity.web
# or specify host, port, and ML save slot:
python -m smartcity.main --web --host 127.0.0.1 --port 8000 --slot default
```

Open **http://127.0.0.1:8000** in your browser (desktop or mobile).

- Switch between **`Aether (Simulation)`** and **`Terra (Real)`**, manage named **ML Save Slots**, or configure your **Tranzy API Key** from the top bar.
- Toggle **`Adaptive AI`** vs **`Fixed Timers`**, adjust **`Car Traffic`**, trigger **`Rush Hour`** at 8 Iași hotspots (Copou, Păcurari, Gara Iași, Piața Unirii, Bd. Independenței, Palas, Podu Roș, Tudor Vladimirescu), test **Weather** (`Clear`/`Rain`/`Snow`), **Day Cycle**, **Friction**, **Incident**, and **Ambulance** green-wave preemption, or toggle map layers (**Heat**, **CTP Routes**, **Cars**, **Buildings**).
- Hover (desktop) or tap (mobile) any bus, roundabout, *rond* loop, named junction, building, or KPI info icon (`i`) for structured explanations.

### 3. Zero-Backend Static Web Bundle (GitHub Pages)

The `docs/` folder bundles the full frontend plus a client-side simulation engine (`web/standalone_sim.js`) and committed Iași JSON datasets so the app runs directly on GitHub Pages with no backend required. Optional demo Tranzy credentials can be set in `web/demo_config.js` before rebuilding:

```bash
python scripts/build_gh_pages.py
```

### 4. Headless Benchmarks, RL Training & Legacy Desktop Visualizer

```bash
# Fixed-timer vs Adaptive AI comparison table
python -m smartcity.main --compare --seconds 240 --seeds 3

# Train adaptive junction policies into a named save slot
python -m smartcity.main --train 5 --slot my_experiment

# List saved ML slots
python -m smartcity.main --list-slots

# Legacy Pygame desktop visualizer
python -m smartcity.main 60
```

Useful CLI flags: `--traffic 200` (`0` to `1000`), `--rush "Podu Roș"`, `--incident-at 60`, `--fixed`, `--map grid` (small synthetic city), `--slot NAME`, `--clean`, `--reset-slot`, `--screenshot out.png --seconds 200`.

---

## Data & Generator Scripts

All Iași datasets are committed in `data/` (and mirrored to `docs/data/` by `scripts/build_gh_pages.py`) so the application runs completely offline:

- `data/iasi_core.json` — Real Iași arterial & transit road graph (`scripts/fetch_iasi.py`).
- `data/iasi_routes.json` — All 51 CTP Iași urban & metropolitan bus routes (`scripts/fetch_iasi.py` + `scripts/expand_ctp_routes.py`).
- `data/iasi_buildings.json` — 1,183 non-overlapping street-aligned apartment blocks & houses (`scripts/generate_iasi_buildings.py`).
- `data/iasi_realism.json` — Multi-lane counts, 16 car roundabouts, 14 CTP *rond* loops, 35 yield junctions, 21 curated junction names, 222 deduplicated CTP stops, 34 crosswalks, hill grades, 2 CFR rail crossings, and 4 hospitals (`scripts/enrich_iasi_realism.py`).
- `data/slots/default.json` — Pre-trained default schema-v2 adaptive signal policy & predictor slot.

Map data © OpenStreetMap contributors (ODbL). Live transit telemetry powered by Tranzy Open Data API (CTP Iași).

---

## Architecture & Tests

- [ARCHITECTURE.md](ARCHITECTURE.md) — Complete system architecture, traffic-flow physics, Romanian roundabout & multi-lane rules, virtual detection loops, adaptive control + counterfactual RL, slot persistence, Terra API, and Web UI design.
- [docs/REFACTORING_PLAN.md](docs/REFACTORING_PLAN.md) & [docs/SCALING_PLAN.md](docs/SCALING_PLAN.md) — Historical design notes recording the earlier grid → 18-line Iași evolution.
- Run the automated headless test suite (**195 tests** across 19 test modules):

```bash
pytest
```
