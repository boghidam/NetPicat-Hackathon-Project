# Smart City Traffic Demo: Architecture

A visual demo of adaptive traffic-signal control on the **real road network of central Iași, Romania**. Individual cars and buses drive on one-lane roads; the buses follow real CTP Iași lines and double as moving congestion sensors. Each signalised junction reads their telemetry, extends green where buses crawl, and avoids releasing traffic toward an overloaded junction. An AI module forecasts what each bus will meet on the next links of its route and feeds that forward so the junctions ahead adjust before the bus arrives.

**Target:** Python 3.14 (3.14.8), `pygame-ce`, `numpy`, `networkx`, `pytest`; `osmnx` and `shapely` only to rebuild the dataset (see `requirements.txt`).

**Status:** implemented. The earlier grid-based demo was refactored onto the real map (see `docs/REFACTORING_PLAN.md`); section 12 records what was measured. Run it with `python -m smartcity.main --incident-at 60`.

---

## 1. Overview and data flow

The world (cars, buses, and the signals' effect on them) is simulated microscopically. Controllers see **only** bus telemetry plus their own signal state, never the world's vehicle positions. The buses really are the sensors.

```mermaid
flowchart LR
    OSM[(OpenStreetMap<br/>roads + CTP bus relations)] -->|scripts/fetch_iasi.py| DATA[data/*.json]
    DATA --> NET[RoadNetwork<br/>graph, polylines, phases, routes]
    NET --> W[TrafficModel<br/>cars + buses, stop lines, boxes]
    W -->|bus state| BF[BusFleet<br/>mock CTP API]
    BF -->|BusTelemetry| TB[TelemetryBus]
    TB --> CE[CongestionEstimator<br/>EWMA per edge]
    TB --> DL[DetectionLoops<br/>50 / 150 / 300 m]
    TB --> PR[PostIntersectionPredictor<br/>follows the bus's route]
    CE --> NC[NetworkCoordinator<br/>graph-aware scoring and throttling]
    DL --> NC
    PR -->|Forecast| NC
    NC --> IC[IntersectionControllers<br/>SUMO-style phases]
    IC -->|light per approach, car throttle| W
    IC --> EL[EventLog]
    NC --> EL
    PR --> EL
    EL --> VIZ[Visualizer / HUD<br/>read-only, pan and zoom]
    W --> VIZ
    IC --> VIZ
```

One simulation tick (`dt = 0.1 s`):

1. Each `IntersectionController` advances its phase timer.
2. `TrafficModel` moves every vehicle along its edge polyline: car-following, stop lines, crossing junctions, spawning and removing cars at the gates.
3. `BusFleet` publishes one `BusTelemetry` per bus.
4. The estimator, detection loops and predictor consume the telemetry.
5. Every 0.5 s the `NetworkCoordinator` refreshes loads, throttles and scores, and controllers make their adaptive decisions.

Everything except rendering is headless and testable with `pytest`.

---

## 2. The Iași dataset (`scripts/fetch_iasi.py`)

Run once; the output (`data/iasi_core.json`, `data/iasi_routes.json`, about 180 KB) is committed so the app starts offline and tests are deterministic. The raw downloads go to `data/raw/` (git-ignored).

1. **Area.** A box (47.147-47.186 N, 27.553-27.607 E) covering Copou, Păcurari, Bd. Independenței, Piața Unirii, Gara, Palas, Podu Roș and Tudor Vladimirescu. OSMnx downloads its drive network (1,194 nodes, 2,770 edges, 307 km).
2. **Bus lines.** Overpass returns the `route=bus` relations of network "CTP Iași" in the box (stops in order, way members). For each chosen line (19, 28, 36, 41, 20, 42, 43, 47) the stops are snapped to graph nodes and joined by shortest-time paths that strongly prefer the relation's own ways, giving a contiguous edge sequence. The return direction comes from the opposite relation, or from a shortest path back when OSM has none (line 36). The result is a cyclic edge list per line.
3. **Crop for 60 FPS.** Keep primary, secondary and tertiary roads (plus their links) and every edge a bus route needs, then the largest strongly connected component (1,149 edges).
4. **Merge junction clusters.** `osmnx.consolidate_intersections` (25 m tolerance) merges dual carriageways and roundabout nodes into single junctions, which also removes every edge shorter than 30 m. Route edges are mapped through the merge.
5. **Result:** 352 nodes, 710 directed edges (125 km), speed limits from `maxspeed` tags (50 km/h urban default; 20-70 km/h occur), edge polylines, street names, and 8 routes of 6-17 km each.

Map data (c) OpenStreetMap contributors, ODbL.

---

## 3. Module layout

Package `smartcity/`:

| Module | Responsibility |
|---|---|
| `config.py` | Frozen dataclass holding all tunables |
| `network.py` | `RoadNetwork`: graph of `Intersection`s and polyline `Segment`s with speed limits and stop lines; phase grouping by heading; turn options; gate selection; `networkx` routing; lat/lon projection |
| `data.py`, `synthetic.py` | Load the committed Iași data into plain `GraphData`/`RouteData`; a small synthetic grid with the same interface for tests |
| `traffic_model.py` | `TrafficModel` and `Vehicle`: the microscopic world and its physical rules |
| `mock_api.py` | `BusFleet` (places buses on their lines, emits telemetry), `TelemetryBus` (pub/sub), `BusTelemetry` |
| `congestion.py` | `CongestionEstimator`: per-edge EWMA of bus speed ratio, load, confidence |
| `detection.py` | `DetectionLoops`: virtual loops at 50/150/300 m before every stop line, built from bus telemetry |
| `predictor.py` | `PostIntersectionPredictor`: the feed-forward AI module, following each bus's route |
| `controller.py` | `IntersectionController` (phase machine) and `NetworkCoordinator` (graph-aware scoring, throttling, budget) |
| `events.py` | `EventLog` of forecasts, adjustments, throttles, incidents |
| `simulation.py` | Headless fixed-step engine wiring everything together |
| `visualizer.py`, `hud.py` | `Camera` (projection, pan, zoom), city renderer, dashboard |
| `main.py` | Entry point, key and mouse handling, `--compare`, `--screenshot` |

---

## 4. Road network and signals

**Graph.** Every OSM edge is a one-lane directed `Segment` from one intersection centre to the next along its polyline (`length` from OSM, scaled geometry, `speed_limit`, street `name`, headings at both ends). Two-way streets are two segments; one-way streets are one. Vehicles keep to the right-hand lane (3 m off the centre line).

**Stop line and junction box.** Vehicles stop at the *stop line*, 14 m before the centre of the junction they approach (or 40% of the edge, if shorter). The area between the stop lines is the box.

**Signalised junctions.** Every node with at least three incoming roads (and not a gate) gets signals: 76 of 352 in the Iași graph, which is more than the real city has. Nodes with fewer approaches are plain bends or merges with no signal.

**Phases from geometry.** Incoming approaches are grouped by heading: two approaches whose headings are within 40 degrees of opposite share a green (greedy best match), and an unpaired street has a phase of its own. A crossroads gets two phases, a T-junction two (the through road, then the stem), a five-way junction three. A left turn is any turn of more than 35 degrees to the left; turn shares prefer going straight (U-turns only at dead ends).

**Safety timings (SUMO-style, in `config.py`).** A green never lasts less than `min_green` = **7 s** (no flickering; every early end checks it); then **yellow 3 s**; then an **all-red clearance of 2 s** before the conflicting phase starts. The standard green is 20 s, at most 45 s. Phase state is `GREEN → YELLOW → ALLRED → next phase`. Neighbouring junctions start desynchronised.

**Gates.** Boundary nodes of the cropped map with traffic both ways (10, spread by farthest-point sampling) are gates: cars appear on their outgoing edges and leave when they reach their destination gate.

**Routing.** Cars follow the fastest `networkx` path (Dijkstra on free-flow travel time, cached) from a random gate to another. Buses follow their line's cyclic edge list. Both are plain edge sequences: no random walks.

**Projection.** `LocalProjection` maps latitude/longitude to metres east/north of the map centre (equirectangular); `Camera` maps those to screen pixels with pan and zoom (`Camera.latlon_to_screen`).

---

## 5. Vehicle rules (`TrafficModel`)

Identical in fixed and adaptive mode, in plain Python in the spirit of SUMO's safety rules:

- *Red:* a vehicle holds at the stop line; nothing enters the box while its light is red.
- *Yellow:* a vehicle stops if it can still do so with maximum braking; only vehicles too fast to stop continue.
- *Green:* a vehicle crosses only if the start of its next edge has room, so nobody enters the box without room to leave it.
- *Car-following:* a vehicle keeps a safe distance behind the one ahead along the polyline (`v <= sqrt(2 * a_comfort * room)`), with no overtaking, so vehicles never overlap and never jump across nodes.
- *Speed:* each vehicle drives at its edge's speed limit (cars vary by up to 8%), accelerating at 1.8 m/s² and braking at up to 6 m/s².
- *Left turns:* both opposite streets are green together, so a left-turner yields while oncoming traffic is moving toward the box or in it.
- *Merging:* two streams turning into the same edge never share the box.
- *Throttle:* when the coordinator meters an approach, cars (not buses) are released no faster than `sat_flow * g`.
- *Incident:* a stalled vehicle parks mid-edge for 150 s. On a one-lane road nobody can overtake, so the lane is blocked.

---

## 6. Sensing: estimator and virtual detection loops

**Estimator.** EWMA of the bus speed ratio (speed over the edge's limit) per edge from mid-edge samples only (30 m past the previous junction, 40 m before the stop line), because speed near a signal reflects the signal. Confidence decays with staleness (`exp(-age / 90 s)`) and load is blended toward a neutral prior (0.25), so an unvisited edge is *unknown*, not *clear*.

**Detection loops.** Each approach has virtual windows 50, 150 and 300 m before its stop line. A bus counts toward a window when its *road distance* to that stop line is within it, and the windows reach back across junctions: the loops are indexed by walking upstream edges that can turn into the approach. From the buses inside, a window yields counts, speed ratios and a density estimate:

- buses still moving contribute their slowdown (`1 - speed ratio`), weighted 0.5 / 0.3 / 0.2 from near to far;
- a *standing* bus (under 30% of the limit) proves a queue at least as long as its distance, so it sets a floor on how full the approach edge is (a bus 80 m back on a 150 m edge means a half-full edge, not a jam; one standing on the previous street means the queue covers the whole edge);
- a standing bus within 40 m of its own stop line is ignored (it is waiting for its signal or pulling away).

The 150 m window drives the local extension trigger; the 300 m window feeds the bus-delay term of the score; the density feeds the load estimate.

---

## 7. Global optimisation across the graph

1. **Graph-aware downstream gating (throttle).** For each approach the coordinator looks at every edge it can release into *and at the junction at the end of that edge*: the worst of `load(edge)` and `0.85 * junction_load(next node)`, where a junction's load is the mean of its two most loaded approaches. It meters car releases only when that is clearly worse than the edge the cars are leaving (backpressure: do not shift the bottleneck):

   ```
   excess = downstream - max(rho_gate, load_up)
   g      = 1 if excess <= 0 else clamp(1 - excess / (rho_block - rho_gate), 0, 1)     # rho_gate 0.65, rho_block 0.85
   ```

   So releasing onto a perfectly free edge is still throttled when the junction it leads to (say a rush-hour Podu Roș) is overloaded. Metering is waived while a bus is in the 150 m window of the approach, because buses cannot overtake the cars they would be held behind.

2. **Physical spillback.** The world enforces that nobody enters a box without room to leave it, so a queue that reaches a junction blocks that approach on green too.

3. **Dynamic phase score** per approach: `S = w_p * pressure + w_d * bus_delay + w_f * forecast + w_a * age - w_n * downstream_load`. Green is shared in proportion to `max(S, 0) + eps`, only when the shares clearly differ from even (dead-band), and a green ends early if the next phase's score beats the current one by 0.3.

4. **Extension budget.** Total extension seconds across the network per rolling minute are capped, and extensions are damped when a neighbouring junction just extended (`networkx` neighbours).

5. **Anti-starvation and hysteresis.** An age term, `max_green`, a fixed phase order (except a predictive jump), `min_green`, the fixed yellow and all-red intervals, and an override cooldown.

An earlier "spillback guard" that skipped or cut short phases whose exits looked full was removed after measurement: it cost buses a few points and did not reduce jams.

---

## 8. AI post-intersection prediction (feed-forward, along the route)

```mermaid
sequenceDiagram
    participant Bus
    participant I as Junction I
    participant P as Predictor
    participant J as Junctions ahead on the route
    Bus->>I: crosses onto the next edge of its line
    I->>P: BusTelemetry (line, route position, speed history)
    P->>P: features, predict speed on this edge
    P->>J: Forecasts for the next 3 edges of the route (delay, ETA, confidence)
    J->>J: add forecast term to score, pre-extend or serve this street next
    Bus->>J: arrives (eta)
    J->>P: observed speed, error recorded, model updated
```

**Trigger.** A bus's telemetry shows it has moved onto a new edge of its line.

**Features** (over its last 3 edges): mean speed ratio, last ratio, delay rate (fraction of time lost), trend. **Model:** an EWMA baseline at cold start, then an **online ridge regression** (NumPy, window of 200 outcomes) after 25 outcomes, predicting the speed ratio on the edge just entered. Rolling MAE of speed and ETA is shown on the HUD.

**Following the predefined route.** Telemetry carries the bus's line and position along it, and the predictor looks the line up in the public route table. It forecasts the edge the bus just entered and the next two edges **of that route** (wrapping around the loop), each with `predicted_density = 1 - ratio`, an ETA built from edge lengths and limits, a `predicted_delay` (extra seconds over free flow on that edge) and a confidence that falls with each hop. When the bus clears the next junction, the window moves one edge forward and fresh forecasts are issued ahead of it.

**Consumption.** The forecast adds `w_f * density * confidence * decay(eta)` to its approach's score. If the street is green and the bus is `lead` seconds away with little green left, the controller pre-extends (`PREDICTIVE_EXTEND`); if it is red, the current green ends (after `min_green`) and that street is served next, even out of order (`PREDICTIVE_SWITCH`). Each forecast is acted on once. Because the regression shrinks toward the mean, predicted densities are modest, so the trigger threshold is low (0.2) and predictive adjustments are rare.

---

## 9. Mock API (the bus feed)

- In-process, 10 Hz of simulation time, no network. It stands in for a high-rate AVL feed.
- 26 buses on 8 CTP lines (19, 28, 36, 41, 20, 42, 43, 47), spaced evenly along each line's loop, 2-5 per line by length (one per 3.5 km). They share the road with cars, queue at the same lights and obey the same junction rules.
- Each tick, one `BusTelemetry` per bus: ID, line number, position along the line, coordinates, distance to the next stop line, speed, the edge's speed limit, accumulated delay. Consumers `subscribe`; a ring buffer keeps recent records for the HUD.
- Deterministic given a seed. Cars are *not* in the feed.

---

## 10. Visualizer and dashboard

- **Projection and camera.** Latitude/longitude to metres to screen, with mouse-wheel zoom about the cursor, drag to pan, `F` to fit, `+`/`-` and arrow keys. The road layer is cached and only re-rendered when the view settles (while dragging, the old layer is shifted). Road width is exaggerated when zoomed out and true to scale when zoomed in.
- **Look.** Dark theme with asphalt-grey road vectors sized by road class, sleek junction rings, neon stop-line bars for each approach (green `#39FF14`, yellow, red `#FF1744`), cars as small blue rectangles, buses as long rectangles coloured neon red-amber-lime by speed with their line number chip, faint coloured CTP line corridors (`L`), landmark labels (Copou, Păcurari, Gara, Piața Unirii, Palas, Podu Roș, Tudor Vladimirescu), rings where an adjustment fires, cyan diamonds for AI forecasts, a red X on the stalled vehicle, a scale bar.
- **HUD panels.** Live CTP telemetry table (bus ID, line, speed, distance to the line, delay, AI extra delay ahead); AI predictions (model, error, upcoming forecasts with predicted delay); the three loudest virtual detection loops; automated cycle adjustments (every override of the standard timer, with the reason: `EXTEND`, `PRED-EXT`, `PRED-SW`, `EARLY`, `SPLIT`, `THROTTLE`); metrics.
- **Keys.** `B` adaptive/fixed, `I` incident, `Space` pause, `1`/`2`/`4`/`8` speed, `H` heat layer (off / true occupancy / estimated load), `L` bus lines, `R` reset, `Q` quit.

---

## 11. Testing strategy

Everything except the window is headless. 111 tests:

- **Network and geometry:** projection round trip, compass names, polyline position and lane side on a bent road, stop-line set-back; phase grouping on crossroads, T, skewed, five-way and two-approach nodes; turn options without U-turns; gates; routing.
- **Iași dataset:** real streets and landmarks present, lengths and limits plausible, the requested lines (19, 28, 36, 41) present and contiguous, routes run along their streets, every gate reaches every other, every signalised junction has valid phases.
- **World rules:** stop line on red, proceed on green, queue gaps, edge speed limits, wait when the exit is full, yellow, throttle spacing, buses not throttled, left-turn yield, two left turns pass, merge exclusion, stalled vehicle, cars follow gate-to-gate routes.
- **Signals:** opposite lights match at every junction at every tick (300 s of the real map); one phase non-red at a time; every street served; phase order green-yellow-all-red; **7 s minimum green, 3 s yellow and 2 s all-red measured**, including with adaptive overrides.
- **Sensing and controllers:** detection windows including across junctions, standing-bus rules, queue occupancy; local extension, cap, budget, damping; the graph-aware throttle (an overloaded *downstream junction* throttles an upstream release onto a free edge) and its bus waiver; predictive extension and switch; score splits and dead-band.
- **Prediction:** forecasts follow the route for three hops and wrap; the window moves ahead as the bus clears junctions; unknown lines get one hop; error recording, expiry, ridge learning.
- **Whole runs on the real map (both control modes):** no vehicle ever crosses a stop line on red, no two vehicles overlap, the box is clear when a new phase turns green, buses and cars never leave their routes, and adaptive control is no worse than fixed timers.
- **Rendering:** camera projection and zoom invariants, every heat and line mode on both maps, the interactive loop with keys and mouse, screenshots, all under `SDL_VIDEODRIVER=dummy`.

---

## 12. Results, risks and open items

**Measured** (`python -m smartcity.main --compare --seeds 6`: a vehicle stalls on the busiest bus corridor from t=120 s to t=270 s, 600 s per run, default demand of 0.08 cars/s per gate):

| mode | bus time lost | car speed (share of limit) | edge-seconds half-occupied | cars exited | adjustments |
|---|---|---|---|---|---|
| fixed timers | 38.0% | 59% | 328 | 268 | 0 |
| adaptive | 36.3% | 60% | 266 | 270 | 354 |

Other demand levels (4 seeds, same incident; bus time lost / car speed / jam edge-seconds / cars exited, fixed then adaptive):

| demand (cars/s per gate) | seeds | fixed timers | adaptive |
|---|---|---|---|
| 0.045 | 4 | 36.4% / 65% / 36 / 219 | 35.7% / 66% / 76 / 220 |
| 0.06 | 6 | 37.6% / 62% / 107 / 243 | 36.3% / 64% / 129 / 244 |
| 0.08 (default) | 6 | 38.0% / 59% / 328 / 268 | 36.3% / 60% / 266 / 270 |

- **Performance.** A frame at 1x speed costs about 9 ms (map and dashboard 5 ms, six simulation ticks 3.4 ms), so the demo holds 60 FPS; at 4x about 53 FPS; the loop keeps the window responsive at 8x. A 15-minute run simulates in about 6 s headless.
- **The gains on the real map are small and not uniform.** Adaptive control lowers bus time lost by 0.7 to 1.7 points, raises car speed by 1 to 2 points and keeps throughput the same. Edges spend about 19% less time half-occupied at the default demand, but *more* at the two lower demands, where the totals are tiny and within noise. It does not transform the network: Iași's arterials are not saturated at these demands, and with a 20 s standard green and only 76 signalised junctions there is little to optimise.
- **Differences from the request.** CTP's lines as described in the brief (28: Dacia - Podu Roș - Triumf; 41: Dacia - Podu Roș) do not match OpenStreetMap, where line 28 is Dacia - Copou and line 41 is Blocuri Ciurea - Piața Independenței. The dataset uses the OSM relations, since that is what can be queried; line 19 (Canta - CUG) and 36 (Dacia - Breazu) match. Only the part of each line inside the cropped map is simulated, and OSM relations are volunteer-maintained and may differ from CTP's current timetable.
- **Simplifications.** One lane per direction whatever OSM `lanes` says; only primary/secondary/tertiary roads (plus what the routes need), so residential streets are absent; every junction with three or more approaches is signalised, whereas only some are in reality; no pedestrians, lane changes or turn lanes; no collision physics inside the box; unsignalised bends and merges give way only by the room check. Speed limits come from `maxspeed` tags with a 50 km/h urban default.
- **Left turns** are the weak spot of paired phases on one lane: a left-turner waiting for a gap holds up the cars behind it.
- **Predictive adjustments are rare.** The model works but regresses to the mean, so few forecasts are dense enough to act on; most of the benefit is reactive. ETA error is large (roughly 15-25 s) because forecast arrivals ignore waiting at red.
- **Bus-only sensing** means unvisited edges have no fresh data; confidence decays to a neutral prior, so the controllers see the *neighbourhood of the buses*, not the whole city.
- **Rush hour** is approximated by raising demand (`--demand`); there is no time-of-day profile yet. Weights and thresholds are hand-tuned on a few seeds, not optimised.

---

## 13. Running it

```bash
python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python -m smartcity.main --incident-at 60      # interactive demo on Iași (press B to compare with fixed timers)
python -m smartcity.main --map grid            # the small synthetic city
python -m smartcity.main --compare             # headless fixed vs adaptive table
python scripts/fetch_iasi.py --refresh         # rebuild the dataset from OpenStreetMap (needs network)
pytest                                         # tests (headless)
```
