# Scaling plan: from 8 bus lines on a central spine to 18 lines across Iași

## What stays

The `RoadNetwork` model and its OSMnx/lat-lon projection, the microscopic world and its rules (stop lines, car-following, no overlap), the SUMO-style phase timings (7 s minimum green, 3 s yellow, 2 s all-red), the virtual detection loops, the route odometer and the per-cycle bus check, the route-following predictor, the throttle, and every runtime control added so far (traffic amount, rush hour).

## What changed

| Area | Before | After |
|---|---|---|
| Dataset | 8 lines on a 4 x 4 km box (352 nodes) | 18 real CTP lines on a city-wide box (about 18 x 15 km, 263 nodes, 667 edges, 428 km). The ten backbone lines 20, 23, 28, 30, 36, 41, 42, 43, 47 and 52 come first, then 19, 29, 44, 46, 50, 6B, 27 and 501. (30B and 43C follow exactly the roads of 30 and 43, and 18 is a loop whose stops snap onto the same nodes both ways, so they were left out.) |
| Route accuracy | stops snapped to the graph, ways preferred 5 to 1 | the graph is built from the arterial roads **plus every way of the chosen relations**, so each bus corridor exists exactly as mapped. Paths join stops or, when OSM lists none (line 52), way-chain waypoints, and prefer the relation's own ways 20 to 1 |
| Fleet | 2-5 buses per line, 26 in all | 4 buses per standard line, 6-8 on each backbone line (longer lines get more): 100 at 100% traffic, spaced evenly along each loop |
| Traffic amount | runtime percentage only | a global `TRAFFIC_AMOUNT` (1.0 = 100%) in `config.py` sets the starting amount and scales buses and car spawning; the runtime keys still move it. New buses go into the biggest gap of their line so headways stay even |
| Safety at scale | extension looks at the downstream edge and junction | it also walks the `networkx` successors one hop further and refuses to lengthen a green into a jammed junction two hops away (predictive extensions too); the extension budget grows with the number of signalised junctions |
| Performance | 60 FPS at 26 buses | one projection per vehicle with precomputed tangents and off-screen culling, cached stop-line geometry, memoised text, one badge per crowded spot, per-tick KPIs, forecasts indexed by edge: 60 FPS at 100% and 300%, about 30 FPS at 1000% |

## Order of work

1. Probe OSM for the relations and the city extent.
2. Rebuild the dataset (`scripts/fetch_iasi.py`) and validate every route.
3. Weighted fleet, `TRAFFIC_AMOUNT`, even headways.
4. Graph-aware extension safety and budget scaling.
5. Profile and fix.
6. Tests and docs.

## Limits to carry into the docs

- OSM relations are volunteer-maintained; some lines are incomplete (52 has no stops) and may differ from CTP's current timetable.
- Only the arterial roads and the roads the chosen lines use are simulated.
- One lane per direction; every junction with three or more approaches is signalised.
