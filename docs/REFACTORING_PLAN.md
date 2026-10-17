# Refactoring plan: from the synthetic grid to the real Iași network

## What stays (the core logic)

`congestion.py` (EWMA link load), `predictor.py` (EWMA→ridge feed-forward model), `controller.py` (phase machine, local extension, throttle, score-sized greens, extension budget), `events.py`, `simulation.py` wiring, the HUD structure, and the rule that controllers see only bus telemetry.

## What changes

| Area | Today | After |
|---|---|---|
| Topology | 4x4 grid, N/E/S/W | `RoadNetwork` built from a real graph: arbitrary node degrees and bearings, polyline edges, speed limits, one-way streets |
| Data | none | `scripts/fetch_iasi.py` (osmnx + networkx + shapely) downloads the Iași drive network and CTP bus relations once, crops to the central spine, merges junction clusters, and writes `data/iasi_core.json` and `data/iasi_routes.json`. The app loads those files, so it starts offline and tests are deterministic |
| Phases | pair opposite N/S, E/W sides | pair approaches whose bearings are roughly opposite; an unpaired street gets its own phase; timings follow SUMO's safety rules (7 s minimum green, 3 s yellow, 2 s all-red) |
| Cars | random gates, grid steering | cars spawn at boundary gates of the cropped map and follow shortest-time paths (networkx) to another gate |
| Buses | hand-made loops | one cyclic edge sequence per CTP line, built from the OSM relation (stop order and way members, snapped to the graph); buses follow it strictly |
| Sensing | per-link EWMA, one 120 m trigger | adds **virtual detection loops** at 50 / 150 / 300 m per approach, built from bus telemetry and walking upstream edges across junctions |
| Bottleneck prevention | worst next-link load | also looks at the junction at the end of the next edge, traversing `networkx` successors, so an upstream release is throttled when the *downstream junction* is overloaded |
| Prediction | next link + straight-ahead link | follows the bus's predefined route: forecasts for the next three links, with per-bus predicted extra delay |
| Rendering | fixed grid pixels | lat/lon → local metres → screen projection, mouse pan and zoom, cached road layer, neon-on-dark dashboard, route numbers and speeds in the HUD |

## Order of work (each step tested before the next)

1. **Data pipeline:** fetch, filter, consolidate, map the bus relations to graph edges, write the data files.
2. **Network layer:** `RoadNetwork` plus a synthetic grid with the same interface, so existing logic tests keep a deterministic city.
3. **World, signals, sensing:** generalise `TrafficModel` (polylines, per-edge speed limits, route-following vehicles), phase grouping by bearing, detection loops, graph-aware throttle.
4. **Routes and prediction:** bus fleet from CTP routes; predictor follows routes.
5. **Visualizer and HUD:** projection, camera, dashboard.
6. **Tests, performance check (60 FPS target), docs.**

## Known limits to carry into the docs

- One lane per direction regardless of OSM `lanes`.
- Only primary/secondary/tertiary roads (plus whatever the bus routes need) are simulated.
- Every junction with three or more approaches gets a signal, which is more than Iași really has.
- OSM bus relations are volunteer-maintained and may differ from CTP's current timetable.
