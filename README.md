# Smart City Traffic Demo: Iași

A visual demo of adaptive traffic-signal control on the **real road network of central Iași, Romania** (Copou, Bd. Independenței, Piața Unirii, Podu Roș, Tudor Vladimirescu, Păcurari), with cars and buses on real streets and **real CTP Iași bus lines** from OpenStreetMap.

Opposite streets share a light (N+S green together, then E+W; at a T-junction the through street, then the stem), with SUMO-style safety timings: at least 7 s of green, a 3 s yellow and a 2 s all-red before the conflicting phase. Vehicles stop at a stop line before the junction on red. The buses stream telemetry; **virtual detection loops** (50 / 150 / 300 m before every stop line) turn it into congestion estimates; only the buses are observed, never the cars, so a green is **never cut short**: once per red-green cycle each junction checks how far the buses heading for it travelled, and lengthens its green only if they covered very little distance. Releases toward overloaded downstream junctions are throttled, and an online-learned forecast of what each bus will meet along the rest of its route can lengthen a green before the bus arrives.

![demo](docs/demo.png)

## Run

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m smartcity.main --incident-at 60     # interactive window
```

| key / mouse | action |
|---|---|
| wheel, drag | zoom about the cursor, pan |
| `F`, `+` `-`, arrows | fit the map, zoom, pan |
| `B` | toggle adaptive control / fixed-timer baseline |
| `I` | stall a vehicle on the busiest bus corridor (incident) |
| `Space` | pause |
| `1` `2` `4` `8` | simulation speed |
| `H` | cycle heat layer: off / true edge occupancy / load estimated from buses |
| `L` | show or hide the CTP line corridors |
| `R` | reset |
| `Q` | quit |

Other options: `--map grid` (a small synthetic city), `--fixed` (start on fixed timers), `--demand 0.08` (cars per second entering at each gate, a crude rush hour), `--compare` (headless fixed vs adaptive table), `--screenshot FILE --seconds N`.

## What you are looking at

- **Map:** asphalt-grey roads, a neon stop-line bar at every approach of a signalised junction (green / yellow / red), small blue cars, buses coloured red-amber-lime by speed with their line number, faint coloured corridors for the 8 simulated CTP lines (19, 28, 36, 41, 20, 42, 43, 47), landmark labels. Rings flash where the algorithm lengthens a green or throttles a release; cyan diamonds mark AI forecasts of delay; a red X marks the stalled vehicle.
- **Dashboard:** live bus telemetry (ID, line, speed, delay, AI-predicted extra delay ahead), AI predictions along each route, the busiest virtual detection loops, and the log of automated cycle adjustments.

## Data

`scripts/fetch_iasi.py` downloads the drive network with OSMnx and the CTP bus relations from Overpass, crops to the central spine, merges junction clusters and writes `data/iasi_core.json` and `data/iasi_routes.json` (committed, so the app runs offline). Rebuild with `python scripts/fetch_iasi.py --refresh`.

Map data (c) OpenStreetMap contributors, ODbL. OSM bus relations are volunteer-maintained and may differ from CTP's current timetable; see the limits in ARCHITECTURE.md.

## Docs and tests

[ARCHITECTURE.md](ARCHITECTURE.md) explains the design, the maths and the measured results, including where it does not help. [docs/REFACTORING_PLAN.md](docs/REFACTORING_PLAN.md) records how the earlier grid demo was moved onto the real map. `pytest` runs the headless test suite.
