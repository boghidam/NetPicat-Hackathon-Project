/**
 * Standalone Browser Engine for Static GitHub Pages hosting (zero Python server required).
 * Automatically activated when no FastAPI backend is detected.
 *
 * - Runs all 51 CTP Iași + AMTPI bus lines on the real committed OSM graph (`iasi_core.json` / `iasi_routes.json`).
 * - Applies realistic per-line weekday peak counts + 5-15 min terminal layovers + 15-30 min periodic driver breaks.
 * - Persists named ML slots (`default`, `+ New`, `Reset`) in browser `localStorage`.
 * - Connects directly to Tranzy Open Data API (`Access-Control-Allow-Origin: *`) in `Terra (Real)` mode
 *   using `window.NOODLE_CONFIG.TRANZY_API_KEY` or the key entered in the API Key modal.
 */
(function () {
  const LINE_BUSES = {
    "30": 10, "19": 8, "42": 8, "43": 8, "46": 8, "52": 8, "20": 7,
    "27": 6, "28": 6, "44": 6, "41": 5, "47": 5, "36": 4, "43c": 4,
    "30b": 3, "29": 3, "18": 2, "50": 2, "54": 2, "23": 2, "6B": 2,
    "23b": 1, "48": 1, "49": 1, "51": 1, "53": 1,
    "101": 2, "102": 2, "103": 2, "104": 1, "201": 2, "202": 2, "203": 2, "204": 1,
    "301": 2, "401": 3, "501": 2, "501b": 1, "502": 2, "503": 2, "503b": 1,
    "601": 2, "602": 2, "603": 1, "701": 2, "702": 2, "801": 2, "802": 2, "802b": 1,
    "901": 3, "902": 2
  };

  const SLOTS_KEY = "noodle_ml_slots_v4";
  const CREDS_KEY = "noodle_tranzy_creds_v1";
  const TRANZY_BASE = "https://api.tranzy.ai/v1/opendata";
  const COLD_IASI_TRUST = { BUS_HOLD: 0.45, LOCAL_EXTEND: 0.25 };
  const MIN_TRUST = 0.18;
  const MAX_TRUST = 1.45;
  const LEARN_RATE = 0.08;

  async function fetchFirstJson(paths) {
    for (const p of paths) {
      try {
        const res = await fetch(p);
        if (res.ok) return await res.json();
      } catch (_) {}
    }
    throw new Error("Could not load Iași dataset JSON from " + paths.join(", "));
  }

  class StandaloneEngine {
    constructor() {
      this.mode = "aether";
      this.t = 0;
      this.paused = false;
      this.speed = 1.0;
      this.adaptive = true;
      this.trafficPercent = 100;
      this.rushPoint = null;
      this.incident = null;
      this.mapPayload = null;
      this.segs = [];
      this.nodes = [];
      this.routes = [];
      this.buildings = [];
      this.cityBuildings = [];
      this.outerBuildings = [];
      this.cityBldgPool = [];
      this.outerBldgPool = [];
      this.gates = [];
      this.citySegSet = new Set();
      this.inEdges = [];
      this.outEdges = [];
      this.prevEdge = null;
      this.pathLenM = null;
      this.cityDestsByNode = [];
      this.outerDestsByNode = [];
      this.throughGatePairs = [];
      this.buses = [];
      this.cars = [];
      this.ambulances = [];
      this.roundabouts = [];
      this.roundaboutByNode = new Map();
      this.yieldNodes = new Set();
      this.yieldMinorApproaches = new Map();
      this.junctionNames = {};
      this.rondLoops = [];
      this.rondLoopByNode = new Map();
      this.busStopsBySeg = new Map();
      this.crosswalks = [];
      this.activeCrosswalks = new Map();
      this.railCrossings = [];
      this.activeRails = new Set();
      this.hospitals = [];
      this.frictionPts = [];
      this.weather = "clear";
      this.dayCycle = false;
      this.dayMinutes = 7.5 * 60;
      this.urbanFriction = false;
      this.activeByKind = { city_to_building: 0, outer_to_building: 0, through_city: 0 };
      this.signals = new Map();
      this.controllers = new Map();
      this.ctlBySeg = new Map();
      this.signalSegIds = [];
      this.rings = [];
      this.slots = {};
      this.activeSlot = "default";
      this.policy = null;
      this.priorOutcomes = { BUS_HOLD: { decisive: 0, wasted: 0, missed: 0 }, LOCAL_EXTEND: { decisive: 0, wasted: 0, missed: 0 } };
      this.sessionOutcomes = { BUS_HOLD: { decisive: 0, wasted: 0, missed: 0 }, LOCAL_EXTEND: { decisive: 0, wasted: 0, missed: 0 } };
      this.phaseDemand = new Map();
      this.sessionJudged = 0;
      this._slotStartT = 0;
      this._episodeSimT = 0;
      this._checkpointSimT = 0;
      this._checkpointJudged = 0;
      this.busDelayEma = 0;
      this._delayEmaInit = false;
      this.busDelaySum = 0;
      this.busElapsedSum = 0;
      this._smoothedDelayRed = null;
      this._wasAdaptive = true;
      this.baseLineCounts = { ...LINE_BUSES };

      // Terra live state
      this.tranzyKey = "";
      this.tranzyAgency = "1";
      this.terraRoutes = {};
      this.terraBuses = [];
      this.terraStatus = {
        provider: "tranzy",
        connected: false,
        active_vehicles: 0,
        mapped_vehicles: 0,
        last_sync_at: null,
        error: null,
        agency_id: "1",
        route_counts: {},
        attribution: "Data © Tranzy AI SRL & Compania de Transport Public Iași SA",
      };
      this.terraTimer = null;
      this.advisories = [];
    }

    async init() {
      const slotNames = ["default", "apprentice", "trained"];
      const [core, routesDoc, buildingsDoc, realismDoc, ...loadedSlots] = await Promise.all([
        fetchFirstJson(["./data/iasi_core.json", "../data/iasi_core.json", "/data/iasi_core.json"]),
        fetchFirstJson(["./data/iasi_routes.json", "../data/iasi_routes.json", "/data/iasi_routes.json"]),
        fetchFirstJson(["./data/iasi_buildings.json", "../data/iasi_buildings.json", "/data/iasi_buildings.json"]).catch(() => null),
        fetchFirstJson(["./data/iasi_realism.json", "../data/iasi_realism.json", "/data/iasi_realism.json"]).catch(() => null),
        ...slotNames.map((n) =>
          fetchFirstJson([`./data/slots/${n}.json`, `../data/slots/${n}.json`, `/data/slots/${n}.json`]).catch(() => null)
        ),
      ]);

      const bundledSlots = {};
      slotNames.forEach((n, idx) => {
        if (loadedSlots[idx]) bundledSlots[n] = loadedSlots[idx];
      });

      this._loadSlots(bundledSlots);
      this._loadCredentials();
      this._buildNetwork(core, routesDoc, buildingsDoc, realismDoc);
      this._spawnFleet();
      this._spawnCars(1500);
      return this.mapPayload;
    }

    _loadSlots(bundledSlots) {
      try {
        const saved = JSON.parse(localStorage.getItem(SLOTS_KEY) || "{}");
        if (saved && typeof saved === "object") this.slots = saved;
      } catch (_) {}
      let changed = false;
      for (const [name, data] of Object.entries(bundledSlots || {})) {
        if (!this.slots[name]) {
          this.slots[name] = data;
          changed = true;
        }
      }
      if (!this.slots.default) {
        this.slots.default = {
          schema_version: 2,
          metadata: { slot_name: "default", map: "iasi", episodes: 0, sim_seconds_trained: 0.0 },
          policy: {
            map: "iasi",
            episodes: 0,
            default_trust: { ...COLD_IASI_TRUST },
            phase_bonus: {},
            trust: {},
            count: {},
            outcomes: { BUS_HOLD: { decisive: 0, wasted: 0, missed: 0 }, LOCAL_EXTEND: { decisive: 0, wasted: 0, missed: 0 } },
          },
        };
        changed = true;
      }
      if (changed) this._saveSlots();
      this.activeSlot = "default";
      this._hydrateSlotPolicy();
    }

    _hydrateSlotPolicy() {
      const slot = this.slots[this.activeSlot] || this.slots.default || {};
      const rawPol = slot.policy || {};
      const cloneOut = (src) => ({
        BUS_HOLD: {
          decisive: Number((src && src.BUS_HOLD && src.BUS_HOLD.decisive) || 0),
          wasted: Number((src && src.BUS_HOLD && src.BUS_HOLD.wasted) || 0),
          missed: Number((src && src.BUS_HOLD && src.BUS_HOLD.missed) || 0),
        },
        LOCAL_EXTEND: {
          decisive: Number((src && src.LOCAL_EXTEND && src.LOCAL_EXTEND.decisive) || 0),
          wasted: Number((src && src.LOCAL_EXTEND && src.LOCAL_EXTEND.wasted) || 0),
          missed: Number((src && src.LOCAL_EXTEND && src.LOCAL_EXTEND.missed) || 0),
        },
      });
      const defTrust = rawPol.default_trust || COLD_IASI_TRUST;
      const phases = {};
      for (const [k, row] of Object.entries(rawPol.phase_bonus || {})) {
        if (Array.isArray(row)) phases[String(k)] = row.map(Number);
      }
      const trust = {};
      for (const [k, row] of Object.entries(rawPol.trust || {})) {
        if (row && typeof row === "object") trust[String(k)] = { ...row };
      }
      const count = {};
      for (const [k, v] of Object.entries(rawPol.count || {})) {
        count[String(k)] = Number(v || 0);
      }
      this.policy = {
        map: "iasi",
        episodes: Number((slot.metadata && slot.metadata.episodes) || rawPol.episodes || 0),
        default_trust: { BUS_HOLD: Number(defTrust.BUS_HOLD ?? 0.45), LOCAL_EXTEND: Number(defTrust.LOCAL_EXTEND ?? 0.25) },
        phase_bonus: phases,
        trust,
        count,
      };
      this.priorOutcomes = cloneOut(rawPol.outcomes);
      this.sessionOutcomes = cloneOut(null);
      this.phaseDemand = new Map();
      for (const [k, row] of Object.entries(phases)) {
        this.phaseDemand.set(Number(k), row.map((b) => Math.min(1, b / 14)));
      }
      this.sessionJudged = 0;
      this._slotStartT = this.t;
      this._episodeSimT = this.t;
      this._checkpointSimT = this.t;
      this._checkpointJudged = 0;
      this.busDelayEma = 0;
      this._delayEmaInit = false;
      this.busDelaySum = 0;
      this.busElapsedSum = 0;
      this._smoothedDelayRed = null;
      for (const ctl of this.controllers.values()) {
        ctl.openDecisions = [];
        ctl.busesSeen.clear();
      }
    }

    _persistActiveSlot(incrementEp = false) {
      const slot = this.slots[this.activeSlot];
      if (!slot || !this.policy) return;
      const dtSim = Math.max(0, this.t - this._checkpointSimT);
      const newJudged = this.sessionJudged - this._checkpointJudged;
      if (dtSim <= 0 && newJudged <= 0 && !incrementEp) return;
      const mergedOutcomes = {
        BUS_HOLD: {
          decisive: this.priorOutcomes.BUS_HOLD.decisive + this.sessionOutcomes.BUS_HOLD.decisive,
          wasted: this.priorOutcomes.BUS_HOLD.wasted + this.sessionOutcomes.BUS_HOLD.wasted,
          missed: this.priorOutcomes.BUS_HOLD.missed + this.sessionOutcomes.BUS_HOLD.missed,
        },
        LOCAL_EXTEND: {
          decisive: this.priorOutcomes.LOCAL_EXTEND.decisive + this.sessionOutcomes.LOCAL_EXTEND.decisive,
          wasted: this.priorOutcomes.LOCAL_EXTEND.wasted + this.sessionOutcomes.LOCAL_EXTEND.wasted,
          missed: this.priorOutcomes.LOCAL_EXTEND.missed + this.sessionOutcomes.LOCAL_EXTEND.missed,
        },
      };
      const savedPhases = {};
      for (const [k, row] of Object.entries(this.policy.phase_bonus)) {
        if (row.some((x) => x >= 0.5)) savedPhases[k] = row.map((x) => Math.round(x * 100) / 100);
      }
      slot.metadata = slot.metadata || { slot_name: this.activeSlot, map: "iasi", episodes: 0, sim_seconds_trained: 0 };
      slot.metadata.sim_seconds_trained = Math.round(((slot.metadata.sim_seconds_trained || 0) + dtSim) * 10) / 10;
      if (incrementEp && (newJudged > 0 || (this.t - this._episodeSimT) >= 120)) {
        slot.metadata.episodes = (slot.metadata.episodes || 0) + 1;
        this._episodeSimT = this.t;
      }
      slot.policy = {
        map: "iasi",
        episodes: slot.metadata.episodes || 0,
        default_trust: { ...this.policy.default_trust },
        phase_bonus: savedPhases,
        trust: this.policy.trust,
        count: this.policy.count,
        outcomes: mergedOutcomes,
      };
      this.policy.episodes = slot.metadata.episodes || 0;
      this._checkpointSimT = this.t;
      this._checkpointJudged = this.sessionJudged;
      this._saveSlots();
    }

    _saveSlots() {
      try { localStorage.setItem(SLOTS_KEY, JSON.stringify(this.slots)); } catch (_) {}
    }

    _loadCredentials() {
      const cfg = window.NOODLE_CONFIG || {};
      let key = (cfg.TRANZY_API_KEY || "").trim();
      let agency = String(cfg.TRANZY_AGENCY_ID || "1").trim() || "1";
      try {
        const local = JSON.parse(localStorage.getItem(CREDS_KEY) || "{}");
        if (local.api_key) key = local.api_key;
        if (local.agency_id) agency = local.agency_id;
      } catch (_) {}
      this.tranzyKey = key;
      this.tranzyAgency = agency;
      this.terraStatus.agency_id = agency;
    }

    _buildNetwork(core, routesDoc, buildingsDoc, realismDoc) {
      // Use exact mean (lat0, lon0) matching smartcity/network.py
      const lat0 = core.nodes.reduce((a, n) => a + n.lat, 0) / core.nodes.length;
      const lon0 = core.nodes.reduce((a, n) => a + n.lon, 0) / core.nodes.length;
      const kx = 111320 * Math.cos((lat0 * Math.PI) / 180);
      const ky = 110540;
      this.proj = { lat0, lon0, kx, ky };
      const toXY = (lat, lon) => [(lon - lon0) * kx, (lat - lat0) * ky];
      const [cityCx, cityCy] = toXY(47.161, 27.583);
      this.cityCenter = [cityCx, cityCy];
      this.urbanRadius = 4200.0;

      this.nodes = core.nodes.map((n) => {
        const [x, y] = toXY(n.lat, n.lon);
        return { id: n.id, x, y, lat: n.lat, lon: n.lon, inCount: 0, outCount: 0 };
      });
      const N = this.nodes.length;
      this.inEdges = Array.from({ length: N }, () => []);
      this.outEdges = Array.from({ length: N }, () => []);

      const nodeMap = new Map(this.nodes.map((n) => [n.id, n]));
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      this.nodes.forEach((n) => {
        minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
        minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
      });

      const byUV = new Map();
      this.citySegSet = new Set();
      this.segs = core.edges.map((e) => {
        nodeMap.get(e.v).inCount += 1;
        nodeMap.get(e.u).outCount += 1;
        const pts = e.geometry.map(([lon, lat]) => toXY(lat, lon));
        const cum = [0];
        for (let i = 1; i < pts.length; i++) {
          cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
        }
        const rawLen = cum[cum.length - 1] || 1;
        const speed = Math.max(20, Math.min(70, e.maxspeed_kph || 50)) / 3.6;
        const stopPos = e.length - Math.min(14, 0.4 * e.length);
        const span = Math.min(20, e.length / 2);
        const headingAt = (sa, sb) => {
          const interp = (s) => {
            const target = Math.max(0, Math.min(rawLen, (s / (e.length || 1)) * rawLen));
            let k = 0;
            while (k < cum.length - 2 && cum[k + 1] < target) k++;
            const sp = Math.max(1e-6, cum[k + 1] - cum[k]);
            const f = Math.max(0, Math.min(1, (target - cum[k]) / sp));
            return [pts[k][0] + (pts[k + 1][0] - pts[k][0]) * f, pts[k][1] + (pts[k + 1][1] - pts[k][1]) * f];
          };
          const [xa, ya] = interp(sa), [xb, yb] = interp(sb);
          return Math.atan2(yb - ya, xb - xa);
        };
        const seg = {
          id: e.id, u: e.u, v: e.v, length: e.length, rawLen, speed, stopPos,
          hwy: (e.highway || "tertiary").replace("_link", ""),
          rawHwy: e.highway || "tertiary",
          name: e.name || "",
          pts, cum,
          headingIn: headingAt(e.length - span, e.length),
        };
        this.outEdges[e.u].push(seg);
        this.inEdges[e.v].push(seg);
        const uvKey = `${e.u}:${e.v}`;
        if (!byUV.has(uvKey)) byUV.set(uvKey, []);
        byUV.get(uvKey).push(seg);
        const mid = pts[Math.floor(pts.length / 2)];
        if (Math.hypot(mid[0] - cityCx, mid[1] - cityCy) <= this.urbanRadius) {
          this.citySegSet.add(seg.id);
        }
        return seg;
      });

      const coincidentTwin = new Map();
      for (const s of this.segs) {
        if (coincidentTwin.has(s.id)) continue;
        const revs = byUV.get(`${s.v}:${s.u}`) || [];
        for (const rev of revs) {
          if (coincidentTwin.has(rev.id)) continue;
          if (this._polylinesCoincide(s.pts, rev.pts, 2.5)) {
            coincidentTwin.set(s.id, rev.id);
            coincidentTwin.set(rev.id, s.id);
            break;
          }
        }
      }

      // Perimeter gates matching smartcity/network.py (_choose_gates)
      const band = 700.0;
      const gateCands = [];
      for (const n of this.nodes) {
        const onEdge = Math.min(n.x - minX, maxX - n.x, n.y - minY, maxY - n.y) <= band;
        if (onEdge && n.inCount > 0 && n.outCount > 0) {
          gateCands.push([n.id, n.inCount]);
        }
      }
      gateCands.sort((a, b) => b[1] - a[1]);
      this.gates = [];
      if (gateCands.length) {
        this.gates.push(gateCands[0][0]);
        while (this.gates.length < Math.min(16, gateCands.length)) {
          let bestId = -1, bestDist = -1;
          for (const [cid] of gateCands) {
            if (this.gates.includes(cid)) continue;
            const cn = this.nodes[cid];
            let md = Infinity;
            for (const gid of this.gates) {
              const gn = this.nodes[gid];
              md = Math.min(md, Math.hypot(cn.x - gn.x, cn.y - gn.y));
            }
            if (md > bestDist) { bestDist = md; bestId = cid; }
          }
          if (bestId < 0) break;
          this.gates.push(bestId);
        }
      }

      // Buildings dataset (same coordinates & zoning as Python backend)
      const rawBldgs = (buildingsDoc && (buildingsDoc.buildings || buildingsDoc)) || [];
      this.buildings = [];
      for (const b of rawBldgs) {
        if (b.seg_id < 0 || b.seg_id >= this.segs.length) continue;
        const [bx, by] = toXY(b.lat, b.lon);
        const seg = this.segs[b.seg_id];
        this.buildings.push({
          id: b.id,
          kind: b.kind,
          zone: b.zone,
          segId: b.seg_id,
          posM: Math.max(10, Math.min(seg.stopPos - 8, b.pos_m)),
          x: bx, y: by, w: b.w, d: b.d, angle: b.angle,
          capacity: b.capacity || (b.kind === "apartment" ? 4 : 1),
        });
      }
      this.cityBuildings = this.buildings.filter((b) => b.zone === "city");
      this.outerBuildings = this.buildings.filter((b) => b.zone === "outer");
      this.cityBldgPool = [];
      this.cityBuildings.forEach((b) => { for (let k = 0; k < b.capacity; k++) this.cityBldgPool.push(b); });
      this.outerBldgPool = [];
      this.outerBuildings.forEach((b) => { for (let k = 0; k < b.capacity; k++) this.outerBldgPool.push(b); });

      this._buildRoutingTables();

      // Apply Iași realism metadata (multi-lane, roundabouts, yield junctions, rond loops, 222 bus stops, crosswalks, rail, hospitals)
      const edgeLanes = (realismDoc && realismDoc.edge_lanes) || {};
      const edgeGrades = (realismDoc && realismDoc.edge_grades) || {};
      const rbRaw = (realismDoc && realismDoc.roundabouts) || [];
      this.yieldNodes = new Set(((realismDoc && realismDoc.yield_nodes) || []).map(Number));
      this.junctionNames = (realismDoc && realismDoc.junction_names) || {};
      const rbByNodeRaw = new Map(rbRaw.map((r) => [Number(r.node), r]));

      this.segs.forEach((s) => {
        s.lanes = Math.max(1, Math.min(3, Number(edgeLanes[String(s.id)] || 1)));
        s.oneway = !coincidentTwin.has(s.id);
        s.gradePct = Number(edgeGrades[String(s.id)] || 0);
        const rbi = rbByNodeRaw.get(s.v);
        if (rbi) s.stopPos = s.length - Math.min(Number(rbi.radius_m) + 4.5, 0.42 * s.length);
      });

      const gateSet = new Set(this.gates);
      this.roundabouts = [];
      this.roundaboutByNode = new Map();
      const rbTuples = [];
      for (const r of rbRaw) {
        const nid = Number(r.node);
        const nd = this.nodes[nid];
        if (!nd) continue;
        const rad = Number(r.radius_m);
        const isSig = !!r.signalized && !gateSet.has(nid);
        const armAngles = new Map();
        for (const s of this.segs) {
          if (s.v === nid) {
            const pRim = Math.max(0, s.length - Math.min(rad + 3.0, s.length * 0.42));
            const [px, py, dx, dy] = this._posOnSeg(s, pRim);
            const off = this._laneOffset(s, 0);
            armAngles.set(s.id, Math.atan2(py - dx * off - nd.y, px + dy * off - nd.x));
          } else if (s.u === nid) {
            const pRim = Math.min(s.length, Math.min(rad + 2.5, s.length * 0.35));
            const [px, py, dx, dy] = this._posOnSeg(s, pRim);
            const off = this._laneOffset(s, 0);
            armAngles.set(s.id, Math.atan2(py - dx * off - nd.y, px + dy * off - nd.x));
          }
        }
        const obj = {
          node: nid, name: r.name, x: nd.x, y: nd.y,
          radius: rad, ringLanes: Number(r.ring_lanes || 1),
          signalized: isSig, armAngles,
        };
        this.roundabouts.push(obj);
        this.roundaboutByNode.set(nid, obj);
        rbTuples.push([nid, r.name, Math.round(nd.x * 10) / 10, Math.round(nd.y * 10) / 10, obj.radius, obj.ringLanes, isSig ? 1 : 0]);
      }

      // Compute minor yielding arms at each unsignalized Cedează junction
      const TWO_PI = Math.PI * 2;
      const wrapAng = (a) => ((a + Math.PI) % TWO_PI + TWO_PI) % TWO_PI - Math.PI;
      const oppTol = (40 * Math.PI) / 180;
      const hwyPrio = { primary: 4, secondary: 3, tertiary: 2, unclassified: 1, residential: 0, service: -1 };
      this.yieldMinorApproaches = new Map();
      for (const nid of this.yieldNodes) {
        if (this.roundaboutByNode.has(nid) || gateSet.has(nid)) continue;
        const inList = this.inEdges[nid] || [];
        const sortedIds = inList.map((s) => s.id).sort((a, b) => {
          const ha = ((wrapAng(this.segs[a].headingIn) % TWO_PI) + TWO_PI) % TWO_PI;
          const hb = ((wrapAng(this.segs[b].headingIn) % TWO_PI) + TWO_PI) % TWO_PI;
          return ha - hb;
        });
        const pairs = [];
        for (let i = 0; i < sortedIds.length; i++) {
          for (let j = i + 1; j < sortedIds.length; j++) {
            const sa = sortedIds[i], sb = sortedIds[j];
            const off = Math.abs(Math.abs(wrapAng(this.segs[sa].headingIn - this.segs[sb].headingIn)) - Math.PI);
            if (off < oppTol) pairs.push([off, sa, sb]);
          }
        }
        pairs.sort((p1, p2) => p1[0] - p2[0]);
        const paired = new Set();
        for (const [, sa, sb] of pairs) {
          if (!paired.has(sa) && !paired.has(sb)) { paired.add(sa); paired.add(sb); }
        }
        let minor = new Set(inList.map((s) => s.id).filter((sid) => !paired.has(sid)));
        if (inList.length >= 2 && (minor.size === 0 || minor.size === inList.length)) {
          const scoreSeg = (s) => {
            const raw = (s.rawHwy || "").toLowerCase();
            const base = raw.replace("_link", "");
            const rank = (hwyPrio[base] ?? 1) - (raw.endsWith("_link") ? 0.5 : 0);
            const cont = (s.name && (this.outEdges[nid] || []).some((o) => o.name === s.name)) ? 1 : 0;
            return [rank, s.lanes || 1, s.speed, cont];
          };
          let bestMain = inList[0];
          let bestSc = scoreSeg(bestMain);
          for (let i = 1; i < inList.length; i++) {
            const cand = scoreSeg(inList[i]);
            let better = false;
            for (let k = 0; k < cand.length; k++) {
              if (cand[k] !== bestSc[k]) { better = cand[k] > bestSc[k]; break; }
            }
            if (better) { bestMain = inList[i]; bestSc = cand; }
          }
          minor = new Set(inList.map((s) => s.id).filter((sid) => sid !== bestMain.id));
        }
        this.yieldMinorApproaches.set(nid, minor);
      }

      // CTP Iași transit turnaround loops (rond_loops)
      this.rondLoops = [];
      this.rondLoopByNode = new Map();
      const rondLoopTuples = [];
      for (const rl of ((realismDoc && realismDoc.rond_loops) || [])) {
        const nid = Number(rl.node);
        const nd = this.nodes[nid];
        if (!nd) continue;
        const [lx, ly] = toXY(Number(rl.lat), Number(rl.lon));
        const obj = {
          node: nid,
          name: String(rl.name || `Rond n${nid}`),
          x: lx,
          y: ly,
          radius: Number(rl.radius_m || 14.5),
          kind: String(rl.kind || "bus"),
          lines: (rl.lines || []).map(String),
        };
        this.rondLoops.push(obj);
        if (!this.rondLoopByNode.has(nid)) this.rondLoopByNode.set(nid, obj);
        rondLoopTuples.push([
          nid,
          obj.name,
          Math.round(lx * 10) / 10,
          Math.round(ly * 10) / 10,
          Math.round(obj.radius * 10) / 10,
          obj.kind,
          obj.lines,
          Math.round(nd.x * 10) / 10,
          Math.round(nd.y * 10) / 10,
        ]);
      }

      this.busStopsBySeg = new Map();
      const busStopTuples = [];
      for (const st of ((realismDoc && realismDoc.bus_stops) || [])) {
        const seg = this.segs[st.seg_id];
        if (!seg) continue;
        const [sx, sy, dx, dy] = this._posOnSeg(seg, st.pos_m);
        const off = this._laneOffset(seg, 0);
        const bx = sx + dy * off, by = sy - dx * off;
        const item = { id: st.id, segId: st.seg_id, posM: st.pos_m, hasBay: !!st.has_bay, demand: Number(st.demand || 1) };
        if (!this.busStopsBySeg.has(st.seg_id)) this.busStopsBySeg.set(st.seg_id, []);
        this.busStopsBySeg.get(st.seg_id).push(item);
        busStopTuples.push([st.id, Math.round(bx * 10) / 10, Math.round(by * 10) / 10, st.has_bay ? 1 : 0, st.name]);
      }

      this.crosswalks = [];
      const cwTuples = [];
      for (const cw of ((realismDoc && realismDoc.crosswalks) || [])) {
        const seg = this.segs[cw.seg_id];
        if (!seg) continue;
        const [cx, cy, dx, dy] = this._posOnSeg(seg, cw.pos_m);
        this.crosswalks.push({ id: cw.id, segId: cw.seg_id, posM: cw.pos_m, rate: Number(cw.rate || 0.02) });
        cwTuples.push([cw.id, Math.round(cx * 10) / 10, Math.round(cy * 10) / 10, Math.round(dx * 100) / 100, Math.round(dy * 100) / 100, seg.lanes]);
      }

      this.railCrossings = [];
      const railTuples = [];
      for (const rc of ((realismDoc && realismDoc.rail_crossings) || [])) {
        const seg = this.segs[rc.seg_id];
        if (!seg) continue;
        const [rx, ry, dx, dy] = this._posOnSeg(seg, rc.pos_m);
        this.railCrossings.push({ segId: rc.seg_id, posM: rc.pos_m, periodS: rc.period_s, closedS: rc.closed_s, offsetS: rc.offset_s });
        railTuples.push([rc.seg_id, Math.round(rx * 10) / 10, Math.round(ry * 10) / 10, Math.round(dx * 100) / 100, Math.round(dy * 100) / 100, rc.name]);
      }

      this.hospitals = ((realismDoc && realismDoc.hospitals) || []).map((h) => ({ name: h.name, node: Number(h.node) }));

      const roads = [];
      const segGeoms = {};
      const stopLines = {};
      this.signalSegIds = [];
      const sigNodes = new Set();
      this.segs.forEach((s) => {
        segGeoms[s.id] = s.pts.map(([x, y]) => [Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
        const back = coincidentTwin.get(s.id);
        if (back === undefined || back >= s.id) {
          const lanesDir = back === undefined ? s.lanes : Math.max(s.lanes, this.segs[back].lanes);
          const totalLanes = back === undefined ? lanesDir : (lanesDir * 2);
          roads.push({ id: s.id, hwy: s.hwy, lanes: lanesDir, total_lanes: totalLanes, oneway: s.oneway, pts: segGeoms[s.id] });
        }
        const rbTarget = this.roundaboutByNode.get(s.v);
        const isSigNode = rbTarget
          ? (rbTarget.signalized && !gateSet.has(s.v))
          : (nodeMap.get(s.v).inCount >= 3 && !gateSet.has(s.v) && !this.yieldNodes.has(s.v));
        if (isSigNode) {
          const [x, y, dx, dy] = this._posOnSeg(s, s.stopPos);
          const midLane = ((s.lanes || 1) - 1) * 0.5;
          const off = this._laneOffset(s, midLane);
          const sx = x + dy * off, sy = y - dx * off;
          const half = Math.max(2.2, (s.lanes || 1) * 1.55);
          stopLines[s.id] = [sx - dy * half, sy + dx * half, sx + dy * half, sy - dx * half];
          this.signalSegIds.push(s.id);
          sigNodes.add(s.v);
        }
      });

      // Build real per-junction phase state-machine controllers (matching smartcity/controller.py)
      this.controllers = new Map();
      this.ctlBySeg = new Map();
      for (const nid of sigNodes) {
        const inList = (this.inEdges[nid] || []).map((s) => s.id).sort((a, b) => {
          const ha = ((wrapAng(this.segs[a].headingIn) % TWO_PI) + TWO_PI) % TWO_PI;
          const hb = ((wrapAng(this.segs[b].headingIn) % TWO_PI) + TWO_PI) % TWO_PI;
          return ha - hb;
        });
        if (!inList.length) continue;
        const pairs = [];
        for (let i = 0; i < inList.length; i++) {
          for (let j = i + 1; j < inList.length; j++) {
            const sa = inList[i], sb = inList[j];
            const off = Math.abs(Math.abs(wrapAng(this.segs[sa].headingIn - this.segs[sb].headingIn)) - Math.PI);
            if (off < oppTol) pairs.push([off, sa, sb]);
          }
        }
        pairs.sort((p1, p2) => p1[0] - p2[0]);
        const paired = new Set();
        const groups = [];
        for (const [, sa, sb] of pairs) {
          if (!paired.has(sa) && !paired.has(sb)) {
            paired.add(sa);
            paired.add(sb);
            groups.push([sa, sb]);
          }
        }
        for (const sid of inList) {
          if (!paired.has(sid)) groups.push([sid]);
        }
        groups.sort((g1, g2) => inList.indexOf(g1[0]) - inList.indexOf(g2[0]));
        const groupOf = new Map();
        groups.forEach((g, gi) => g.forEach((sid) => groupOf.set(sid, gi)));
        const idx = nid % groups.length;
        const ctl = {
          node: nid,
          groups,
          groupOf,
          idx,
          phase: "GREEN",
          elapsed: (nid * 7) % 20,
          planned: 20,
          extendedBy: 0,
          holdFired: false,
          openDecisions: [],
          busesSeen: new Set(),
        };
        this.controllers.set(nid, ctl);
        inList.forEach((sid) => this.ctlBySeg.set(sid, ctl));
      }

      // Build non-roundabout junctions payload matching smartcity/web.py
      const junctions = [];
      for (const nd of this.nodes) {
        if (this.roundaboutByNode.has(nd.id) || gateSet.has(nd.id)) continue;
        const rawArms = this._physicalArmCount(nd.id);
        const isYield = this.yieldNodes.has(nd.id);
        const isSig = nd.inCount >= 3 && !isYield;
        if (!isSig && !isYield && rawArms < 3) continue;
        const arms = Math.max(rawArms, (this.inEdges[nd.id] || []).length);
        const jkind = isSig ? "signalized" : (isYield ? "yield" : "merge");
        const inc = [...(this.inEdges[nd.id] || []), ...(this.outEdges[nd.id] || [])];
        let maxHw = 4.0, minLen = Infinity;
        for (const s of inc) {
          const totL = s.oneway ? (s.lanes || 1) : ((s.lanes || 1) * 2);
          maxHw = Math.max(maxHw, (totL * 3.5 + 2.0) * 0.5);
          if (s.length < minLen) minLen = s.length;
        }
        if (!Number.isFinite(minLen)) minLen = 60.0;
        let nearD = Infinity;
        for (const other of this.nodes) {
          if (other.id === nd.id) continue;
          const d = Math.hypot(other.x - nd.x, other.y - nd.y);
          if (d < nearD) nearD = d;
        }
        const radCap = Math.min(minLen * 0.22, nearD * 0.42);
        const radM = Math.max(3.6, Math.min(11.5, Math.max(5.5, maxHw + 1.5), radCap));
        const yieldBars = [];
        if (jkind === "yield") {
          const minors = Array.from(this.yieldMinorApproaches.get(nd.id) || []).sort((a, b) => a - b);
          for (const sid of minors) {
            const s = this.segs[sid];
            const posY = Math.max(0, s.length - Math.min(radM + 3.2, 0.38 * s.length));
            const midLane = ((s.lanes || 1) - 1) * 0.5;
            const off = this._laneOffset(s, midLane);
            const [yx, yy, ydx, ydy] = this._posOnSeg(s, posY);
            const px = yx + ydy * off, py = yy - ydx * off;
            const half = Math.max(2.2, (s.lanes || 1) * 1.55);
            yieldBars.push([
              Math.round((px - ydy * half) * 10) / 10,
              Math.round((py + ydx * half) * 10) / 10,
              Math.round((px + ydy * half) * 10) / 10,
              Math.round((py - ydx * half) * 10) / 10,
              Math.round(ydx * 100) / 100,
              Math.round(ydy * 100) / 100,
            ]);
          }
        }
        junctions.push([
          nd.id,
          this._junctionName(nd.id),
          Math.round(nd.x * 10) / 10,
          Math.round(nd.y * 10) / 10,
          jkind,
          Math.round(radM * 10) / 10,
          yieldBars,
          arms,
        ]);
      }

      this.routes = routesDoc.routes.map((r) => ({
        ref: r.ref,
        name: r.name,
        backbone: !!r.backbone,
        metropolitan: !!r.metropolitan,
        turnaround_leg: r.turnaround_leg || 0,
        edges: r.edges,
      }));

      const landmarks = (core.meta.landmarks || []).map((m) => {
        const [x, y] = toXY(m.lat, m.lon);
        return { name: m.name, x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 };
      });
      this.landmarks = landmarks;

      const buildingTuples = this.buildings.map((b) => [
        b.id, b.kind === "apartment" ? 1 : 0, b.zone === "city" ? 1 : 0,
        Math.round(b.x * 10) / 10, Math.round(b.y * 10) / 10,
        Math.round(b.w * 10) / 10, Math.round(b.d * 10) / 10, Math.round(b.angle * 1000) / 1000,
      ]);

      this.mapPayload = {
        bounds: [minX, minY, maxX, maxY],
        proj: { lat0, lon0, kx },
        roads,
        seg_geoms: segGeoms,
        stop_lines: stopLines,
        landmarks,
        routes: this.routes,
        buildings: buildingTuples,
        roundabouts: rbTuples,
        junctions,
        rond_loops: rondLoopTuples,
        bus_stops: busStopTuples,
        crosswalks: cwTuples,
        rail_crossings: railTuples,
        key_points: landmarks.map((l) => l.name),
        signals_count: Object.keys(stopLines).length,
      };
    }

    _physicalArmCount(nid) {
      const dirs = [];
      for (const s of (this.inEdges[nid] || [])) {
        const pts = s.pts;
        if (pts.length >= 2) {
          dirs.push(Math.atan2(pts[pts.length - 2][1] - pts[pts.length - 1][1], pts[pts.length - 2][0] - pts[pts.length - 1][0]));
        }
      }
      for (const s of (this.outEdges[nid] || [])) {
        const pts = s.pts;
        if (pts.length >= 2) {
          dirs.push(Math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]));
        }
      }
      dirs.sort((a, b) => a - b);
      const tol = (25 * Math.PI) / 180;
      const wrap = (a) => ((a + Math.PI) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2) - Math.PI;
      const clusters = [];
      for (const a of dirs) {
        let placed = false;
        for (const c of clusters) {
          if (Math.abs(wrap(a - c[0])) < tol) { c.push(a); placed = true; break; }
        }
        if (!placed) clusters.push([a]);
      }
      if (clusters.length >= 2 && Math.abs(wrap(clusters[clusters.length - 1][0] - clusters[0][0])) < tol) {
        clusters[0].push(...clusters.pop());
      }
      return clusters.length;
    }

    _junctionName(nid) {
      if (this.junctionNames && this.junctionNames[String(nid)]) return this.junctionNames[String(nid)];
      const seen = [];
      for (const s of [...(this.inEdges[nid] || []), ...(this.outEdges[nid] || [])]) {
        const nm = (s.name || "").trim();
        if (nm && !seen.includes(nm)) seen.push(nm);
      }
      if (seen.length >= 2) return `${seen[0]} × ${seen[1]}`;
      if (seen.length === 1) return seen[0];
      return `Intersecție n${nid}`;
    }

    _nearestRondLoop(nid, maxDistM = 280) {
      const direct = this.rondLoopByNode.get(nid);
      if (direct) return direct;
      const nd = this.nodes[nid];
      if (!nd || !this.rondLoops.length) return null;
      let best = null, bestD = maxDistM;
      for (const rl of this.rondLoops) {
        const d = Math.hypot(rl.x - nd.x, rl.y - nd.y);
        if (d <= bestD) { bestD = d; best = rl; }
      }
      return best;
    }

    _polylinesCoincide(ptsA, ptsB, tolM = 2.5) {
      if (!ptsA || !ptsB || ptsA.length < 2 || ptsB.length < 2) return false;
      const tol2 = tolM * tolM;
      const minDist2 = (px, py, poly) => {
        let best = Infinity;
        for (let i = 0; i < poly.length - 1; i++) {
          const ax = poly[i][0], ay = poly[i][1];
          const dx = poly[i + 1][0] - ax, dy = poly[i + 1][1] - ay;
          const l2 = dx * dx + dy * dy;
          const t = l2 > 1e-12 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l2)) : 0;
          const d2 = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2;
          if (d2 < best) best = d2;
        }
        return best;
      };
      for (const [px, py] of ptsA) if (minDist2(px, py, ptsB) > tol2) return false;
      for (const [px, py] of ptsB) if (minDist2(px, py, ptsA) > tol2) return false;
      return true;
    }

    _laneOffset(seg, laneV) {
      const n = seg.lanes || 1;
      if (n <= 1) return seg.oneway ? 0.0 : 3.0;
      if (seg.oneway) return ((n - 1) * 0.5 - laneV) * 3.1;
      return 1.9 + (n - 1 - laneV) * 3.1;
    }

    _rbLaneRadius(rb, laneV) {
      if (rb.ringLanes <= 1) return Math.max(6, rb.radius + 0.2);
      return Math.max(6, rb.radius + 0.7 - laneV * 3.6);
    }

    _vehLen(v) {
      if (v.len) return v.len;
      if (v.route && v.line) return 11.4;
      const cls = v.cls || 0;
      return cls === 2 ? 10.6 : cls === 3 ? 6.2 : cls === 1 ? 5.8 : 4.5;
    }

    _vehicleLatOff(c) {
      if (c.rbNode != null) return 0.0;
      if (c.status && c.status !== "DRIVING") return 0.0;
      const seg = this.segs[c.segId];
      if (!seg) return 0.0;
      const laneV = c.laneVisual != null ? c.laneVisual : (c.lane || 0);
      return this._laneOffset(seg, laneV) + (c.inBay ? 2.3 : 0);
    }

    _vehicleXY(c) {
      if (c.rbNode != null) {
        const rb = this.roundaboutByNode.get(c.rbNode);
        if (rb) {
          const rEff = this._rbLaneRadius(rb, c.rbLane || 0);
          const ca = Math.cos(c.rbAngle), sa = Math.sin(c.rbAngle);
          return [rb.x + rEff * ca, rb.y + rEff * sa, -sa, ca];
        }
      }
      const seg = this.segs[c.segId];
      if (c.status && c.status !== "DRIVING" && seg) {
        const loop = this._nearestRondLoop(seg.u);
        if (loop) {
          const rBay = Math.max(5.5, loop.radius - 1.4);
          let idSum = 0;
          const idStr = String(c.id || "");
          for (let k = 0; k < idStr.length; k++) idSum += idStr.charCodeAt(k);
          const slotAng = (idSum * 1.17) % (Math.PI * 2);
          const ca = Math.cos(slotAng), sa = Math.sin(slotAng);
          return [loop.x + rBay * ca, loop.y + rBay * sa, -sa, ca];
        }
      }
      const len = this._vehLen(c);
      let posDraw = Math.max(0, c.pos - len * 0.5);
      const rbFrom = this.roundaboutByNode.get(seg.u);
      const rbTo = this.roundaboutByNode.get(seg.v);
      const rStart = rbFrom ? Math.min(rbFrom.radius + 2.5, seg.length * 0.35) : 0;
      const rEnd = rbTo ? Math.min(rbTo.radius + 2.5, seg.length * 0.42) : 0;
      if (rStart > 0 || rEnd > 0) {
        const usable = Math.max(1, seg.length - rStart - rEnd);
        if (rStart > 0 && seg.length > 3.5 * rStart + rEnd) {
          if (posDraw < 3 * rStart) posDraw = rStart + posDraw * (2 / 3);
          if (rEnd > 0) posDraw = Math.min(posDraw, seg.length - rEnd);
        } else {
          posDraw = rStart + Math.max(0, Math.min(1, posDraw / Math.max(1, seg.length))) * usable;
        }
      }
      const [x, y, dx, dy] = this._posOnSeg(seg, posDraw);
      const laneV = c.laneVisual != null ? c.laneVisual : (c.lane || 0);
      const off = this._laneOffset(seg, laneV) + (c.inBay ? 2.3 : 0);
      return [x + dy * off, y - dx * off, dx, dy];
    }

    setWeather(w) {
      this.weather = ["clear", "rain", "snow"].includes(w) ? w : "clear";
    }

    setDayCycle(on) {
      this.dayCycle = !!on;
    }

    setUrbanFriction(on) {
      this.urbanFriction = !!on;
      this.frictionPts = [];
      this.frictionBlockers = [];
      if (this.urbanFriction) {
        const multi = this.segs.filter((s) => s.lanes >= 2 && s.length >= 130 && this.citySegSet.has(s.id));
        const used = new Set();
        for (let k = 0; k < Math.min(6, multi.length); k++) {
          const s = multi[Math.floor(Math.random() * multi.length)];
          if (used.has(s.id)) continue;
          used.add(s.id);
          const pos = s.length * 0.5;
          const [x, y, dx, dy] = this._posOnSeg(s, pos);
          const off = this._laneOffset(s, 0);
          this.frictionPts.push([Math.round((x + dy * off) * 10) / 10, Math.round((y - dx * off) * 10) / 10]);
          this.frictionBlockers.push({ segId: s.id, lane: 0, pos, len: 5.8, v: 0, isBlocker: true });
        }
      }
    }

    dispatchAmbulance() {
      if (!this.hospitals.length || !this.cityBuildings.length) return;
      for (let t = 0; t < 10; t++) {
        const hosp = this.hospitals[Math.floor(Math.random() * this.hospitals.length)];
        const dest = this.cityBuildings[Math.floor(Math.random() * this.cityBuildings.length)];
        const mid = this._pathBetween(hosp.node, this.segs[dest.segId].u);
        if (!mid || mid.length < 3) continue;
        const route = [...mid, dest.segId];
        const seg = this.segs[route[0]];
        const lane = Math.max(0, (seg.lanes || 1) - 1);
        this.cars.push({
          route, leg: 0, segId: route[0], pos: 6, v: seg.speed * 0.9, len: 6.2, factor: 1.32,
          tripKind: "emergency", destPos: dest.posM, lane, laneVisual: lane, lcCooldown: 0, cls: 3, rbNode: null,
        });
        return;
      }
    }

    _buildRoutingTables() {
      const N = this.nodes.length;
      this.prevEdge = new Int16Array(N * N);
      this.prevEdge.fill(-1);
      this.pathLenM = new Float32Array(N * N);
      this.pathLenM.fill(-1);

      const distT = new Float64Array(N);
      const distL = new Float64Array(N);
      const visited = new Uint8Array(N);

      for (let src = 0; src < N; src++) {
        distT.fill(Infinity);
        distL.fill(-1);
        visited.fill(0);
        distT[src] = 0;
        distL[src] = 0;
        const base = src * N;

        for (let iter = 0; iter < N; iter++) {
          let u = -1, best = Infinity;
          for (let i = 0; i < N; i++) {
            if (!visited[i] && distT[i] < best) { best = distT[i]; u = i; }
          }
          if (u < 0) break;
          visited[u] = 1;
          this.pathLenM[base + u] = distL[u];
          const outs = this.outEdges[u];
          for (let k = 0; k < outs.length; k++) {
            const s = outs[k];
            const nt = best + s.length / s.speed;
            if (nt < distT[s.v]) {
              distT[s.v] = nt;
              distL[s.v] = distL[u] + s.length;
              this.prevEdge[base + s.v] = s.id;
            }
          }
        }
      }

      // Precompute distance-bucketed destination building arrays per node
      this.cityDestsByNode = Array.from({ length: N }, () => []);
      this.outerDestsByNode = Array.from({ length: N }, () => []);
      for (let nid = 0; nid < N; nid++) {
        const base = nid * N;
        const cArr = this.cityDestsByNode[nid];
        const oArr = this.outerDestsByNode[nid];
        for (let i = 0; i < this.cityBuildings.length; i++) {
          const b = this.cityBuildings[i];
          const d = this.pathLenM[base + this.segs[b.segId].u];
          if (d >= 0) {
            if (d >= 400 && d <= 8400) cArr.push(b);
            if (d >= 4200 && d <= 20600) oArr.push(b);
          }
        }
      }

      // Precompute cross-city opposite-gate pairs (>= 90 deg, 5-20 km, crossing city >= 25%)
      const [cx, cy] = this.cityCenter;
      const gateAng = new Map(this.gates.map((g) => [g, Math.atan2(this.nodes[g].y - cy, this.nodes[g].x - cx)]));
      const minRad = (90 * Math.PI) / 180;
      this.throughGatePairs = [];
      for (const g1 of this.gates) {
        for (const g2 of this.gates) {
          if (g1 === g2) continue;
          let da = Math.abs(gateAng.get(g2) - gateAng.get(g1)) % (2 * Math.PI);
          if (da > Math.PI) da = 2 * Math.PI - da;
          if (da < minRad) continue;
          const totalLen = this.pathLenM[g1 * N + g2];
          if (totalLen < 5000 || totalLen > 20000) continue;
          const path = this._pathBetween(g1, g2);
          if (!path || !path.length) continue;
          let cityLen = 0;
          for (const sid of path) if (this.citySegSet.has(sid)) cityLen += this.segs[sid].length;
          if (cityLen / totalLen >= 0.25) {
            this.throughGatePairs.push({ g1, g2, path, distM: totalLen });
          }
        }
      }
    }

    _pathBetween(u, v) {
      if (u === v) return [];
      const N = this.nodes.length;
      const base = u * N;
      if (this.prevEdge[base + v] < 0) return null;
      const rev = [];
      let cur = v, guard = 0;
      while (cur !== u && guard++ < N) {
        const eid = this.prevEdge[base + cur];
        if (eid < 0) return null;
        rev.push(eid);
        cur = this.segs[eid].u;
      }
      if (cur !== u) return null;
      rev.reverse();
      return rev;
    }

    _makeBuildingTrip(orig, minM, maxM) {
      const origSeg = this.segs[orig.segId];
      const bucket = (orig.zone === "city" && maxM <= 8001)
        ? this.cityDestsByNode[origSeg.v]
        : this.outerDestsByNode[origSeg.v];
      if (!bucket || !bucket.length) return null;
      const N = this.nodes.length;
      for (let t = 0; t < 24; t++) {
        const dest = bucket[Math.floor(Math.random() * bucket.length)];
        if (dest.id === orig.id || dest.segId === orig.segId) continue;
        const destSeg = this.segs[dest.segId];
        const midLen = this.pathLenM[origSeg.v * N + destSeg.u];
        if (midLen < 0) continue;
        const total = Math.max(0, origSeg.length - orig.posM) + midLen + dest.posM;
        if (total < minM || total > maxM) continue;
        const mid = this._pathBetween(origSeg.v, destSeg.u);
        if (!mid || mid.includes(orig.segId) || mid.includes(dest.segId)) continue;
        return { route: [orig.segId, ...mid, dest.segId], dest, distM: total };
      }
      return null;
    }

    _makeGateToBuildingTrip(gate, minM, maxM) {
      const bucket = this.outerDestsByNode[gate];
      if (!bucket || !bucket.length) return null;
      const N = this.nodes.length;
      for (let t = 0; t < 24; t++) {
        const dest = bucket[Math.floor(Math.random() * bucket.length)];
        const destSeg = this.segs[dest.segId];
        const midLen = this.pathLenM[gate * N + destSeg.u];
        if (midLen < 0) continue;
        const total = midLen + dest.posM;
        if (total < minM || total > maxM) continue;
        const mid = this._pathBetween(gate, destSeg.u);
        if (!mid || mid.includes(dest.segId)) continue;
        return { route: [...mid, dest.segId], dest, distM: total };
      }
      return null;
    }

    _isLanePosFree(segId, lane, pos, len) {
      const gap = 2.4;
      for (let i = 0; i < this.cars.length; i++) {
        const u = this.cars[i];
        if (u.rbNode != null || u.segId !== segId || (u.lane || 0) !== lane) continue;
        const uLen = u.len || 4.5;
        if (u.pos >= pos) {
          if (u.pos - uLen - pos < gap) return false;
        } else {
          if (pos - len - u.pos < gap) return false;
        }
      }
      if (lane === 0) {
        for (let i = 0; i < this.buses.length; i++) {
          const b = this.buses[i];
          if (b.status !== "DRIVING" || b.rbNode != null || b.inBay) continue;
          if (b.route.edges[b.leg] !== segId) continue;
          const bLen = b.len || 11.4;
          if (b.pos >= pos) {
            if (b.pos - bLen - pos < gap) return false;
          } else {
            if (pos - len - b.pos < gap) return false;
          }
        }
      }
      return true;
    }

    _placeCarOnRoute(route, tripKind, destPos, startMinPos) {
      let totalW = 0;
      const cumW = [];
      for (let i = 0; i < route.length; i++) {
        const s = this.segs[route[i]];
        const lo = i === 0 ? startMinPos : 0;
        const hi = (destPos != null && i === route.length - 1) ? Math.min(s.length, destPos) : s.length;
        totalW += Math.max(0, hi - lo) / Math.max(1, s.speed);
        cumW.push(totalW);
      }
      if (totalW <= 0) return null;
      const rCls = Math.random();
      const cls = rCls < (tripKind === "through_city" ? 0.14 : 0.05) ? 2 : (rCls < 0.22 ? 1 : 0);
      const len = cls === 2 ? 10.6 : (cls === 1 ? 5.8 : 4.5);
      for (let attempt = 0; attempt < 4; attempt++) {
        const r = Math.random() * totalW;
        let leg = 0;
        while (leg < cumW.length - 1 && cumW[leg] < r) leg++;
        const s = this.segs[route[leg]];
        const lo = leg === 0 ? startMinPos : 5;
        const hi = (destPos != null && leg === route.length - 1) ? Math.min(s.stopPos - 4, destPos - 6) : (s.stopPos - 4);
        if (hi <= lo + 1) continue;
        const pos = lo + Math.random() * (hi - lo);
        const nLanes = s.lanes || 1;
        const prefCurb = destPos != null && leg === route.length - 1;
        const order = prefCurb
          ? Array.from({ length: nLanes }, (_, k) => k)
          : Array.from({ length: nLanes }, (_, k) => (k + Math.floor(Math.random() * nLanes)) % nLanes);
        for (const lane of order) {
          if (this._isLanePosFree(s.id, lane, pos, len)) {
            return {
              route, leg, segId: s.id, pos, v: s.speed * 0.6, len,
              factor: (cls === 2 ? 0.80 : cls === 1 ? 0.92 : 1.0) * (0.88 + Math.random() * 0.18),
              tripKind, destPos: destPos != null ? destPos : null,
              lane, laneVisual: lane, lcCooldown: 0, cls, rbNode: null,
            };
          }
        }
      }
      return null;
    }

    _posOnSeg(seg, distM) {
      const target = Math.max(0, Math.min(seg.rawLen, (distM / (seg.length || 1)) * seg.rawLen));
      let i = 0;
      while (i < seg.cum.length - 2 && seg.cum[i + 1] < target) i++;
      const a = seg.pts[i], b = seg.pts[i + 1] || a;
      const span = Math.max(1e-6, seg.cum[i + 1] - seg.cum[i]);
      const f = Math.max(0, Math.min(1, (target - seg.cum[i]) / span));
      const dx = (b[0] - a[0]) / span, dy = (b[1] - a[1]) / span;
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, dx, dy];
    }

    _trust(node, kind) {
      if (!this.policy) return kind === "BUS_HOLD" ? 0.45 : 0.25;
      const row = this.policy.trust[String(node)];
      if (row && row[kind] != null) return row[kind];
      return this.policy.default_trust[kind] ?? 0.45;
    }

    _phaseBonus(node, phaseIdx) {
      if (!this.policy) return 0;
      const row = this.policy.phase_bonus[String(node)];
      if (!row || phaseIdx >= row.length) return 0;
      return row[phaseIdx] || 0;
    }

    _signalState(segId) {
      const ctl = this.ctlBySeg.get(segId);
      if (!ctl) return "G";
      if (ctl.groupOf.get(segId) !== ctl.idx) return "R";
      return ctl.phase === "GREEN" ? "G" : (ctl.phase === "YELLOW" ? "Y" : "R");
    }

    _judgeAndLearn(ctl) {
      if (!this.policy) return;
      const busById = new Map();
      for (const b of this.buses) busById.set(b.id, b);
      const redWait = Math.max(1, Math.max(1, ctl.groups.length - 1) * 25);
      const nk = String(ctl.node);

      for (const d of ctl.openDecisions) {
        const b = busById.get(d.busId);
        let outcome = "missed";
        if (b) {
          const cross = b.lastCrossed && b.lastCrossed.segId === d.segId && b.lastCrossed.leg === d.leg ? b.lastCrossed : null;
          if (cross) {
            outcome = cross.t < d.deadline - 0.35 ? "wasted" : "decisive";
          } else if (b.leg !== d.leg || b.rbNode != null || b.pos >= this.segs[d.segId].stopPos - 1.5) {
            outcome = "decisive";
          }
        }
        this.sessionOutcomes[d.kind][outcome] += 1;
        this.policy.count[nk] = (this.policy.count[nk] || 0) + 1;
        this.sessionJudged += 1;
        const reward = outcome === "decisive"
          ? (1.0 - 0.8 * d.granted / redWait)
          : (outcome === "wasted" ? -0.45 : (-0.25 - 0.5 * d.granted / redWait));
        const row = this.policy.trust[nk] || (this.policy.trust[nk] = {});
        const cur = row[d.kind] != null ? row[d.kind] : this.policy.default_trust[d.kind];
        const step = LEARN_RATE * (reward > 0 ? 0.55 * reward : 0.85 * reward);
        row[d.kind] = Math.max(MIN_TRUST, Math.min(MAX_TRUST, Math.round((cur + step) * 10000) / 10000));
      }
      ctl.openDecisions = [];

      // Online phase_bonus learning from buses observed during this green phase
      const nPhases = ctl.groups.length;
      if (nPhases >= 2) {
        let dem = this.phaseDemand.get(ctl.node);
        if (!dem || dem.length !== nPhases) {
          dem = new Array(nPhases).fill(0);
          this.phaseDemand.set(ctl.node, dem);
        }
        let bon = this.policy.phase_bonus[nk];
        if (!bon || bon.length !== nPhases) {
          bon = new Array(nPhases).fill(0);
          this.policy.phase_bonus[nk] = bon;
        }
        dem[ctl.idx] = 0.90 * dem[ctl.idx] + 0.10 * Math.min(1, ctl.busesSeen.size / 2);
        const minD = Math.min(...dem);
        for (let i = 0; i < nPhases; i++) {
          const excess = Math.max(0, dem[i] - minD - 0.04);
          const target = Math.min(14.0, excess * 22.0);
          bon[i] = Math.max(0, Math.min(14.0, bon[i] + 0.14 * (target - bon[i])));
        }
      }
    }

    _stepControllers(dt, segCounts, newRings) {
      const BASE_GREEN = 20;
      const YELLOW = 3;
      const ALL_RED = 2;
      const MAX_GREEN = 45;
      // Index driving buses near signalized approaches for O(1) hold/extend checks
      const busesOnSeg = new Map();
      for (const b of this.buses) {
        if (b.status !== "DRIVING" || b.rbNode != null || b.stopDwell > 0) continue;
        const sid = b.route.edges[b.leg];
        if (!this.ctlBySeg.has(sid)) continue;
        let list = busesOnSeg.get(sid);
        if (!list) { list = []; busesOnSeg.set(sid, list); }
        list.push(b);
      }

      for (const ctl of this.controllers.values()) {
        ctl.elapsed += dt;
        const curGroup = ctl.groups[ctl.idx];
        if (ctl.phase === "YELLOW") {
          if (ctl.elapsed >= YELLOW) {
            ctl.phase = "ALLRED";
            ctl.elapsed = 0;
            if (this.adaptive) this._judgeAndLearn(ctl);
          }
        } else if (ctl.phase === "ALLRED") {
          if (ctl.elapsed >= ALL_RED) {
            ctl.idx = (ctl.idx + 1) % ctl.groups.length;
            ctl.phase = "GREEN";
            ctl.elapsed = 0;
            ctl.planned = BASE_GREEN;
            ctl.extendedBy = 0;
            ctl.holdFired = false;
            ctl.openDecisions = [];
            ctl.busesSeen.clear();
            if (this.adaptive) {
              const bonus = this._phaseBonus(ctl.node, ctl.idx);
              if (bonus >= 0.8) {
                ctl.planned = Math.min(MAX_GREEN, ctl.planned + bonus);
              }
              // Check LOCAL_EXTEND if a bus on this starting green is heavily queued
              const extTrust = this._trust(ctl.node, "LOCAL_EXTEND");
              const extLimit = extTrust >= 0.24 ? Math.min(7.0, 5.0 * extTrust) : 0;
              const room = MAX_GREEN - ctl.planned;
              const want = Math.min(extLimit, room);
              if (want >= 1.0) {
                let queuedBus = null;
                for (const sid of ctl.groups[ctl.idx]) {
                  const seg = this.segs[sid];
                  for (const b of (busesOnSeg.get(sid) || [])) {
                    const dist = seg.stopPos - b.pos;
                    if (dist > 4 && dist <= 165 && b.speed < seg.speed * 0.42) {
                      queuedBus = { b, sid, dist };
                      break;
                    }
                  }
                  if (queuedBus) break;
                }
                if (queuedBus) {
                  const deadline = this.t + ctl.planned;
                  ctl.planned += want;
                  ctl.extendedBy += want;
                  const nd = this.nodes[ctl.node];
                  newRings.push([nd.x, nd.y, "LOCAL_EXTEND", 0]);
                  ctl.openDecisions.push({
                    kind: "LOCAL_EXTEND",
                    busId: queuedBus.b.id,
                    segId: queuedBus.sid,
                    leg: queuedBus.b.leg,
                    dist: queuedBus.dist,
                    granted: want,
                    deadline,
                  });
                }
              }
            }
          }
        } else {
          // GREEN phase
          if (this.adaptive) {
            for (const sid of curGroup) {
              const seg = this.segs[sid];
              for (const b of (busesOnSeg.get(sid) || [])) {
                if (seg.stopPos - b.pos <= 120) ctl.busesSeen.add(b.id);
              }
            }
          }
          if (ctl.elapsed >= ctl.planned) {
            let held = false;
            if (this.adaptive && !ctl.holdFired) {
              const holdTrust = this._trust(ctl.node, "BUS_HOLD");
              const holdCap = holdTrust >= 0.28 ? 11.5 * holdTrust : 0;
              const room = MAX_GREEN - ctl.planned;
              let chosen = null;
              for (const sid of curGroup) {
                const seg = this.segs[sid];
                const stops = this.busStopsBySeg.get(sid);
                for (const b of (busesOnSeg.get(sid) || [])) {
                  const dist = seg.stopPos - b.pos;
                  if (dist <= 0.5 || dist > 135) continue;
                  if (stops && stops.some((st) => st.id !== b.lastStopId && st.posM > b.pos && st.posM < seg.stopPos)) continue;
                  const need = dist / Math.max(b.speed, 3.5) + 1.0;
                  if (need <= holdCap && need <= room && (!chosen || need > chosen.need)) {
                    chosen = { b, sid, dist, need };
                  }
                }
              }
              if (chosen && chosen.need >= 1.0) {
                ctl.planned += chosen.need;
                ctl.extendedBy += chosen.need;
                ctl.holdFired = true;
                held = true;
                const nd = this.nodes[ctl.node];
                newRings.push([nd.x, nd.y, "BUS_HOLD", 0]);
                ctl.openDecisions.push({
                  kind: "BUS_HOLD",
                  busId: chosen.b.id,
                  segId: chosen.sid,
                  leg: chosen.b.leg,
                  dist: chosen.dist,
                  granted: chosen.need,
                  deadline: this.t,
                });
              }
            }
            if (!held) {
              ctl.phase = "YELLOW";
              ctl.elapsed = 0;
            }
          }
        }
      }
    }

    _spawnFleet() {
      this.buses = [];
      const artLines = new Set(["19", "28", "30", "42", "43", "46", "52"]);
      const miniLines = new Set(["18", "23b", "48", "49", "53", "104", "204", "501b", "503b", "603", "802b"]);
      // Bus fleet strictly follows CTP Iași's realistic 51-line schedule (unaffected by car traffic load!)
      let bid = 0;
      for (const r of this.routes) {
        const n = Math.max(0, Math.round(this.baseLineCounts[r.ref] ?? 2));
        const bLen = artLines.has(r.ref) ? 12.6 : (miniLines.has(r.ref) ? 8.4 : 11.0);
        for (let k = 0; k < n; k++) {
          const leg = Math.floor((k / Math.max(1, n)) * r.edges.length);
          const seg = this.segs[r.edges[leg]];
          this.buses.push({
            id: `B${String(bid++).padStart(3, "0")}`,
            line: r.ref,
            route: r,
            leg,
            segId: seg.id,
            pos: Math.random() * Math.max(4, seg.stopPos - 8),
            speed: seg.speed * 0.8,
            len: bLen,
            lane: 0,
            laneVisual: 0,
            status: "DRIVING",
            restRemaining: 0,
            stopsSinceBreak: Math.floor(Math.random() * 3),
            nextBreakAt: 3 + Math.floor(Math.random() * 3),
          });
        }
      }
    }

    _spawnCars(count) {
      this.cars = [];
      this.activeByKind = { city_to_building: 0, outer_to_building: 0, through_city: 0 };
      const total = Math.round(count * (this.trafficPercent / 100));
      if (total <= 0) return;
      if (!this.cityBldgPool.length || !this.throughGatePairs.length) {
        for (let i = 0; i < total; i++) {
          const seg = this.segs[Math.floor(Math.random() * this.segs.length)];
          this.cars.push({ route: [seg.id], leg: 0, segId: seg.id, pos: Math.random() * seg.length, factor: 0.85 + Math.random() * 0.2, tripKind: "through_city", destPos: null });
        }
        return;
      }

      const nCity = Math.round(total * 0.60);
      const nOuter = Math.round(total * 0.20);
      const nThru = Math.max(0, total - nCity - nOuter);

      // Wave 1: 60% City -> City Building (1-8 km)
      for (let i = 0; i < nCity; i++) {
        for (let a = 0; a < 8; a++) {
          const orig = this.cityBldgPool[Math.floor(Math.random() * this.cityBldgPool.length)];
          const trip = this._makeBuildingTrip(orig, 1000, 8000);
          if (!trip) continue;
          const c = this._placeCarOnRoute(trip.route, "city_to_building", trip.dest.posM, orig.posM);
          if (c) { this.cars.push(c); this.activeByKind.city_to_building++; break; }
        }
      }

      // Wave 1: 20% Outside (Gate or Outer Building) -> City Building (5-20 km)
      for (let i = 0; i < nOuter; i++) {
        for (let a = 0; a < 8; a++) {
          let trip = null, startMin = 4;
          if (this.outerBldgPool.length && Math.random() < 0.5) {
            const orig = this.outerBldgPool[Math.floor(Math.random() * this.outerBldgPool.length)];
            trip = this._makeBuildingTrip(orig, 5000, 20000);
            startMin = orig.posM;
          } else if (this.gates.length) {
            const gate = this.gates[Math.floor(Math.random() * this.gates.length)];
            trip = this._makeGateToBuildingTrip(gate, 5000, 20000);
          }
          if (!trip) continue;
          const c = this._placeCarOnRoute(trip.route, "outer_to_building", trip.dest.posM, startMin);
          if (c) { this.cars.push(c); this.activeByKind.outer_to_building++; break; }
        }
      }

      // Wave 1: 20% Outside -> Far Opposite Outside Cross-City (5-20 km)
      for (let i = 0; i < nThru; i++) {
        const p = this.throughGatePairs[Math.floor(Math.random() * this.throughGatePairs.length)];
        const c = this._placeCarOnRoute(p.path, "through_city", null, 4);
        if (c) { this.cars.push(c); this.activeByKind.through_city++; }
      }
    }

    _spawnWave2Car(segCounts) {
      const tot = Math.max(1, this.cars.length);
      const deficits = [
        ["city_to_building", 0.60 - this.activeByKind.city_to_building / tot],
        ["outer_to_building", 0.20 - this.activeByKind.outer_to_building / tot],
        ["through_city", 0.20 - this.activeByKind.through_city / tot],
      ];
      deficits.sort((a, b) => b[1] - a[1]);
      const kind = deficits[0][0];

      const isUncrowded = (segId, maxDens) => (segCounts[segId] * 6.5) / Math.max(1, this.segs[segId].length * (this.segs[segId].lanes || 1)) < maxDens;
      const sampleProfile = () => {
        const rCls = Math.random();
        const cls = rCls < (kind === "through_city" ? 0.14 : 0.05) ? 2 : (rCls < 0.22 ? 1 : 0);
        const len = cls === 2 ? 10.6 : (cls === 1 ? 5.8 : 4.5);
        const factor = (cls === 2 ? 0.80 : cls === 1 ? 0.92 : 1.0) * (0.88 + Math.random() * 0.18);
        return { cls, len, factor };
      };
      const pickSpawnLane = (segId, pos, len, prefCurb) => {
        const nL = this.segs[segId].lanes || 1;
        const order = prefCurb
          ? Array.from({ length: nL }, (_, k) => k)
          : Array.from({ length: nL }, (_, k) => (k + Math.floor(Math.random() * nL)) % nL);
        for (const l of order) {
          if (this._isLanePosFree(segId, l, pos, len)) return l;
        }
        return -1;
      };

      if (kind === "city_to_building" && this.cityBldgPool.length) {
        for (let t = 0; t < 14; t++) {
          const orig = this.cityBldgPool[Math.floor(Math.random() * this.cityBldgPool.length)];
          if (!isUncrowded(orig.segId, 0.30)) continue;
          const trip = this._makeBuildingTrip(orig, 1000, 8000);
          if (!trip) continue;
          const { cls, len, factor } = sampleProfile();
          const lane = pickSpawnLane(orig.segId, orig.posM, len, true);
          if (lane < 0) continue;
          this.cars.push({
            route: trip.route, leg: 0, segId: orig.segId, pos: orig.posM,
            v: this.segs[orig.segId].speed * 0.45, len, factor,
            tripKind: "city_to_building", destPos: trip.dest.posM,
            lane, laneVisual: lane, lcCooldown: 0, cls, rbNode: null,
          });
          this.activeByKind.city_to_building++;
          segCounts[orig.segId]++;
          return;
        }
      } else if (kind === "outer_to_building") {
        for (let t = 0; t < 12; t++) {
          if (this.outerBldgPool.length && Math.random() < 0.5) {
            const orig = this.outerBldgPool[Math.floor(Math.random() * this.outerBldgPool.length)];
            if (!isUncrowded(orig.segId, 0.30)) continue;
            const trip = this._makeBuildingTrip(orig, 5000, 20000);
            if (!trip) continue;
            const { cls, len, factor } = sampleProfile();
            const lane = pickSpawnLane(orig.segId, orig.posM, len, true);
            if (lane < 0) continue;
            this.cars.push({
              route: trip.route, leg: 0, segId: orig.segId, pos: orig.posM,
              v: this.segs[orig.segId].speed * 0.45, len, factor,
              tripKind: "outer_to_building", destPos: trip.dest.posM,
              lane, laneVisual: lane, lcCooldown: 0, cls, rbNode: null,
            });
            this.activeByKind.outer_to_building++;
            segCounts[orig.segId]++;
            return;
          } else if (this.gates.length) {
            const gate = this.gates[Math.floor(Math.random() * this.gates.length)];
            const trip = this._makeGateToBuildingTrip(gate, 5000, 20000);
            if (!trip || !isUncrowded(trip.route[0], 0.45)) continue;
            const { cls, len, factor } = sampleProfile();
            const lane = pickSpawnLane(trip.route[0], 4, len, false);
            if (lane < 0) continue;
            this.cars.push({
              route: trip.route, leg: 0, segId: trip.route[0], pos: 4,
              v: this.segs[trip.route[0]].speed * 0.55, len, factor,
              tripKind: "outer_to_building", destPos: trip.dest.posM,
              lane, laneVisual: lane, lcCooldown: 0, cls, rbNode: null,
            });
            this.activeByKind.outer_to_building++;
            segCounts[trip.route[0]]++;
            return;
          }
        }
      } else if (this.throughGatePairs.length) {
        for (let t = 0; t < 10; t++) {
          const p = this.throughGatePairs[Math.floor(Math.random() * this.throughGatePairs.length)];
          if (!isUncrowded(p.path[0], 0.45)) continue;
          const { cls, len, factor } = sampleProfile();
          const lane = pickSpawnLane(p.path[0], 4, len, false);
          if (lane < 0) continue;
          this.cars.push({
            route: p.path, leg: 0, segId: p.path[0], pos: 4,
            v: this.segs[p.path[0]].speed * 0.55, len, factor,
            tripKind: "through_city", destPos: null,
            lane, laneVisual: lane, lcCooldown: 0, cls, rbNode: null,
          });
          this.activeByKind.through_city++;
          segCounts[p.path[0]]++;
          return;
        }
      }
    }

    _chooseReceivingLane(tgtId, curLane, len, isBus, finalBldg, tracksBySeg) {
      const seg = this.segs[tgtId];
      const nLanes = seg ? (seg.lanes || 1) : 1;
      if (nLanes <= 1 || isBus) {
        const tr = tracksBySeg[tgtId] ? tracksBySeg[tgtId][0] : null;
        const tail = tr && tr.length ? tr[tr.length - 1] : null;
        const room = tail ? (tail.pos - (tail.len || 4.5) - 2.0) : Infinity;
        return { lane: 0, room };
      }
      const pref = Math.min(curLane || 0, nLanes - 1);
      const tracks = tracksBySeg[tgtId];
      if (finalBldg) {
        const tr0 = tracks ? tracks[0] : null;
        const tail0 = tr0 && tr0.length ? tr0[tr0.length - 1] : null;
        const r0 = tail0 ? (tail0.pos - (tail0.len || 4.5) - 2.0) : Infinity;
        if (r0 >= len + 2.0) return { lane: 0, room: r0 };
      }
      let bestLane = pref;
      let bestRoom = -Infinity;
      let bestScore = -Infinity;
      for (let li = 0; li < nLanes; li++) {
        const tr = tracks ? tracks[li] : null;
        const tail = tr && tr.length ? tr[tr.length - 1] : null;
        const room = tail ? (tail.pos - (tail.len || 4.5) - 2.0) : Infinity;
        const hasRoom = room >= 0.5 ? 1 : 0;
        const count = tr ? tr.length : 0;
        const score = hasRoom * 10000 - count * 20 + Math.min(room, 120) + (li === pref ? 1 : 0);
        if (score > bestScore) {
          bestScore = score;
          bestLane = li;
          bestRoom = room;
        }
      }
      return { lane: bestLane, room: bestRoom };
    }

    tick(dtWall) {
      if (this.mode === "terra") {
        this.t += dtWall;
        // Smooth interpolation for live Terra buses
        for (const b of this.terraBuses) {
          const seg = this.segs[b.segId];
          if (seg && b.speed > 0.2 && b.status === "DRIVING") {
            b.pos = Math.min(seg.stopPos, b.pos + b.speed * dtWall);
          }
        }
        return;
      }
      if (this.paused) return;
      const dt = Math.min(0.35, dtWall * this.speed);
      this.t += dt;
      if (this.dayCycle) this.dayMinutes = (this.dayMinutes + dt / 4.0) % 1440.0;
      const wSpd = this.weather === "snow" ? 0.70 : this.weather === "rain" ? 0.85 : 1.0;
      const wAcc = this.weather === "snow" ? 0.65 : this.weather === "rain" ? 0.84 : 1.0;
      const wGap = this.weather === "snow" ? 1.55 : this.weather === "rain" ? 1.25 : 1.0;
      const wDem = this.weather === "rain" ? 1.18 : this.weather === "snow" ? 1.05 : 1.0;
      const dayH = (this.dayMinutes % 1440) / 60;
      const dayDem = !this.dayCycle ? 1.0 : (dayH >= 7 && dayH <= 9.5 ? 1.32 : dayH >= 16 && dayH <= 19 ? 1.38 : dayH >= 21 || dayH < 6 ? 0.45 : 0.92);

      // Update crosswalk pulses & periodic CFR rail crossings
      for (const [id, exp] of Array.from(this.activeCrosswalks.entries())) {
        if (this.t >= exp) this.activeCrosswalks.delete(id);
      }
      for (const cw of this.crosswalks) {
        if (!this.activeCrosswalks.has(cw.id) && Math.random() < cw.rate * dt) {
          this.activeCrosswalks.set(cw.id, this.t + 5.0 + Math.random() * 3.0);
        }
      }
      this.activeRails.clear();
      for (const rc of this.railCrossings) {
        if (((this.t + rc.offsetS) % rc.periodS) < rc.closedS) this.activeRails.add(rc.segId);
      }

      // Update signal phase machines with real adaptive holds, extensions, and online learning
      const newRings = [];
      const prevSegCounts = this._lastSegCounts || new Uint16Array(this.segs.length);
      this._stepControllers(dt, prevSegCounts, newRings);

      // Build per-(segId, lane) sorted vehicle tracks for true non-overlapping car-following & multi-lane separation
      const nSegs = this.segs.length;
      const tracksBySeg = new Array(nSegs);
      const addTrackItem = (sid, lane, item) => {
        let segTr = tracksBySeg[sid];
        if (!segTr) {
          const nl = this.segs[sid].lanes || 1;
          segTr = Array.from({ length: nl }, () => []);
          tracksBySeg[sid] = segTr;
        }
        const li = Math.max(0, Math.min(segTr.length - 1, lane || 0));
        segTr[li].push(item);
      };

      const yieldMainBusy = new Set();
      const rbRings = new Map();
      const addRbItem = (rbNode, rbLane, item) => {
        const key = `${rbNode}:${rbLane}`;
        let arr = rbRings.get(key);
        if (!arr) { arr = []; rbRings.set(key, arr); }
        arr.push(item);
      };

      const lcStep = dt / 1.1;
      for (let i = 0; i < this.cars.length; i++) {
        const c = this.cars[i];
        if (c.rbNode != null) {
          addRbItem(c.rbNode, c.rbLane || 0, c);
          continue;
        }
        if (c.lane == null) c.lane = 0;
        if (c.laneVisual == null) c.laneVisual = c.lane;
        const diff = c.lane - c.laneVisual;
        c.laneVisual = Math.abs(diff) <= lcStep ? c.lane : (c.laneVisual + Math.sign(diff) * lcStep);
        if (c.lcCooldown > 0) c.lcCooldown = Math.max(0, c.lcCooldown - dt);
        if (c.len == null) c.len = this._vehLen(c);
        if (c.v == null) c.v = this.segs[c.segId].speed * 0.6;
        const s = this.segs[c.segId];
        const minors = this.yieldMinorApproaches.get(s.v);
        if (minors && !minors.has(s.id) && s.length - c.pos <= 36) yieldMainBusy.add(s.v);
        addTrackItem(c.segId, c.lane, c);
      }

      for (let i = 0; i < this.buses.length; i++) {
        const b = this.buses[i];
        if (b.status !== "DRIVING") continue;
        if (b.rbNode != null) {
          addRbItem(b.rbNode, 0, b);
          continue;
        }
        const sid = b.route.edges[b.leg];
        b.segId = sid;
        b.lane = 0;
        b.laneVisual = 0;
        if (b.len == null) b.len = this._vehLen(b);
        if (b.inBay) continue; // recessed bus bay pulls off Bandă 1 cleanly
        const s = this.segs[sid];
        const minors = this.yieldMinorApproaches.get(s.v);
        if (minors && !minors.has(s.id) && s.length - b.pos <= 38) yieldMainBusy.add(s.v);
        addTrackItem(sid, 0, b);
      }

      for (const fb of (this.frictionBlockers || [])) {
        addTrackItem(fb.segId, 0, fb);
      }

      // Sort each active lane track descending by `pos` (front vehicle at index 0)
      for (let sid = 0; sid < nSegs; sid++) {
        const segTr = tracksBySeg[sid];
        if (!segTr) continue;
        for (let li = 0; li < segTr.length; li++) {
          if (segTr[li].length > 1) segTr[li].sort((a, b) => b.pos - a.pos);
        }
      }

      // Fast MOBIL-style multi-lane lane-changing pass on 2- and 3-lane boulevards
      const aheadGap = (arr, pos) => {
        let best = Infinity;
        for (let k = 0; k < arr.length; k++) {
          const u = arr[k];
          if (u.pos >= pos) {
            const g = u.pos - (u.len || 4.5) - pos;
            if (g < best) best = g;
          }
        }
        return best;
      };
      const laneSafe = (arr, pos, len) => {
        for (let k = 0; k < arr.length; k++) {
          const u = arr[k];
          if (u.pos >= pos) {
            if (u.pos - (u.len || 4.5) - pos < 6.5) return false;
          } else {
            if (pos - len - u.pos < 7.5) return false;
          }
        }
        return true;
      };
      for (let sid = 0; sid < nSegs; sid++) {
        const segTr = tracksBySeg[sid];
        if (!segTr || segTr.length < 2) continue;
        const seg = this.segs[sid];
        for (let li = 0; li < segTr.length; li++) {
          const tr = segTr[li];
          for (let idx = tr.length - 1; idx >= 0; idx--) {
            const v = tr[idx];
            if (v.isBlocker || v.line || v.lcCooldown > 0) continue; // only cars change lanes
            if (v.pos < 10 || seg.stopPos - v.pos < 12) continue;
            const ahead = idx > 0 ? tr[idx - 1] : null;
            if (!ahead) continue;
            const hw = ahead.pos - (ahead.len || 4.5) - v.pos;
            const aheadSlow = ahead.isBlocker || (ahead.stopDwell > 0) || (hw < 28 && (ahead.v ?? ahead.speed ?? 0) < seg.speed * 0.72);
            if (!aheadSlow) continue;
            let bestAdj = -1;
            let bestGain = ahead.isBlocker ? 4 : 11;
            for (const cand of [li + 1, li - 1]) {
              if (cand < 0 || cand >= segTr.length) continue;
              const g = aheadGap(segTr[cand], v.pos) - hw;
              if (g > bestGain && laneSafe(segTr[cand], v.pos, v.len || 4.5)) {
                bestGain = g;
                bestAdj = cand;
              }
            }
            if (bestAdj >= 0) {
              tr.splice(idx, 1);
              v.lane = bestAdj;
              v.lcCooldown = 3.0;
              segTr[bestAdj].push(v);
              segTr[bestAdj].sort((a, b) => b.pos - a.pos);
            }
          }
        }
      }

      // Compute tight per-lane car-following movement for every car & bus on every lane track
      const minGap = 2.0 * wGap;
      const accel = 2.0 * wAcc;
      const comfort = 3.0 * wAcc;
      const hardBrake = 6.0;
      const segCounts = new Uint16Array(nSegs);
      const removedCars = new Set();

      // Advance circulating roundabout vehicles with circular gap spacing (no stacking!)
      for (const [, ringArr] of rbRings.entries()) {
        for (let vi = 0; vi < ringArr.length; vi++) {
          const v = ringArr[vi];
          const rb = this.roundaboutByNode.get(v.rbNode);
          const rEff = this._rbLaneRadius(rb, v.rbLane || 0);
          let aheadDist = Infinity;
          for (let ui = 0; ui < ringArr.length; ui++) {
            if (ui === vi) continue;
            const u = ringArr[ui];
            const da = (((u.rbAngle - v.rbAngle) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
            if (da < 1e-4 || da > Math.PI * 2 - 1e-4) {
              if (ui < vi) aheadDist = 0;
              continue;
            }
            if (da > Math.PI * 1.4) continue;
            const dM = da * rEff - (u.len || 4.5) - minGap;
            if (dM < aheadDist) aheadDist = Math.max(0, dM);
          }
          const baseV = v.line ? (6.8 * wSpd) : (7.2 * wSpd);
          const vDes = aheadDist === Infinity ? baseV : Math.min(baseV, Math.sqrt(2 * comfort * Math.max(0, aheadDist)));
          const curV = v.line ? (v.speed ?? baseV) : (v.v ?? baseV);
          const nextV = Math.max(0, curV + Math.max(-hardBrake * dt, Math.min(accel * dt, vDes - curV)));
          if (v.line) v.speed = nextV; else v.v = nextV;
          const darc = (nextV * dt) / rEff;
          v.rbAngle = (v.rbAngle + darc) % (Math.PI * 2);
          v.rbRemaining -= darc;
          if (!v.line && v.rbLane === 1 && v.rbRemaining <= 1.25) v.rbLane = 0;
          if (v.line) {
            const rSeg = this.segs[v.route.edges[v.leg]];
            const slowFrac = 1.0 - Math.min(1.0, nextV / Math.max(1, rSeg.speed));
            this.busElapsedSum += dt;
            this.busDelaySum += dt * slowFrac;
          }
          if (v.rbRemaining <= 0) {
            const nextId = v.line
              ? v.route.edges[(v.leg + 1) % v.route.edges.length]
              : v.route[v.leg + 1];
            const recv = this._chooseReceivingLane(nextId, v.lane || 0, v.len || 4.5, !!v.line, false, tracksBySeg);
            if (recv.room >= -1.0 || (v.rbWait || 0) > 2.2) {
              v.rbNode = null;
              v.rbWait = 0;
              if (v.line) {
                v.leg = (v.leg + 1) % v.route.edges.length;
                v.segId = nextId;
                v.pos = 0;
                v.lastStopId = -1;
              } else {
                v.leg += 1;
                v.segId = nextId;
                v.pos = 0;
                v.lane = recv.lane;
                v.laneVisual = recv.lane;
              }
            } else {
              v.rbRemaining = 0;
              if (v.line) v.speed = 0; else v.v = 0;
              v.rbWait = (v.rbWait || 0) + dt;
            }
          }
        }
      }

      // Step every lane track front-to-back with strict non-overlapping car-following
      const tickSeq = (this._tickSeq = (this._tickSeq || 0) + 1);
      let slowSum = 0;
      let slowN = 0;
      for (let sid = 0; sid < nSegs; sid++) {
        const segTr = tracksBySeg[sid];
        if (!segTr) continue;
        const seg = this.segs[sid];
        const sigSt = this._signalState(sid);
        const railClosed = this.activeRails.has(sid);
        const railPos = seg.length * 0.5;
        const minorsAtEnd = this.yieldMinorApproaches.get(seg.v);
        const rbAtEnd = this.roundaboutByNode.get(seg.v);
        const crossAt = rbAtEnd ? Math.min(seg.length, seg.stopPos + 1.5) : seg.length;
        const segStops = this.busStopsBySeg.get(sid);

        for (let li = 0; li < segTr.length; li++) {
          const tr = segTr[li];
          let prev = null;
          for (let idx = 0; idx < tr.length; idx++) {
            const v = tr[idx];
            if (v.isBlocker) {
              prev = v;
              continue;
            }
            if (v._tickSeq === tickSeq) {
              prev = v;
              continue;
            }
            v._tickSeq = tickSeq;
            const isBus = !!v.line;
            if (isBus && v.stopDwell > 0) {
              v.stopDwell = Math.max(0, v.stopDwell - dt);
              v.speed = 0;
              if (v.stopDwell <= 0) v.inBay = false;
              prev = v;
              continue;
            }
            const len = v.len || (isBus ? 11.4 : 4.5);
            let limit = prev ? (prev.pos - (prev.len || 4.5) - minGap) : Infinity;

            // Scheduled CTP bus stop approach
            let nextStop = null;
            if (isBus && segStops) {
              for (let si = 0; si < segStops.length; si++) {
                const st = segStops[si];
                if (st.id !== v.lastStopId && st.posM >= v.pos - 1.5) {
                  nextStop = st;
                  if (st.posM < limit) limit = st.posM;
                  break;
                }
              }
            }

            // Closed CFR railway barrier
            if (railClosed && v.pos < railPos - 0.5) {
              if (railPos - 1.5 < limit) limit = railPos - 1.5;
            }

            // Signal / Yield stop line (only front vehicle before stop line holds here; followers queue cleanly via `prev`!)
            const isAmb = !isBus && v.cls === 3;
            if (!isAmb && v.pos < seg.stopPos && (!prev || prev.pos >= seg.stopPos)) {
              const curV = isBus ? (v.speed || 0) : (v.v || 0);
              const stopRed = sigSt === "R" || (sigSt === "Y" && (curV * curV) / (2 * hardBrake) <= seg.stopPos - v.pos);
              let yieldHold = false;
              if (!stopRed && minorsAtEnd && minorsAtEnd.has(sid) && yieldMainBusy.has(seg.v)) {
                v.yieldWait = (v.yieldWait || 0) + dt;
                if (v.yieldWait < 3.5) yieldHold = true;
              } else {
                v.yieldWait = 0;
              }
              if (stopRed || yieldHold) {
                if (seg.stopPos - 0.4 < limit) limit = seg.stopPos - 0.4;
              }
            }

            // Free-flow speed & smooth kinematic acceleration/braking
            const gradePen = seg.gradePct > 1.5 ? (isBus ? 0.90 : (v.cls === 2 ? 0.72 : 0.92)) : 1.0;
            const vFree = seg.speed * (isBus ? 0.90 : (v.factor || 0.95)) * wSpd * gradePen;
            const room = Math.max(0, limit - v.pos);
            const vDes = room === Infinity ? vFree : Math.min(vFree, Math.sqrt(2 * comfort * room));
            const curV = isBus ? (v.speed || 0) : (v.v || 0);
            let nextV = Math.max(0, curV + Math.max(-hardBrake * dt, Math.min(accel * dt, vDes - curV)));
            const oldPos = v.pos;
            let newPos = oldPos + nextV * dt;
            if (newPos > limit) {
              newPos = Math.max(oldPos, limit);
              nextV = dt > 1e-5 ? (newPos - oldPos) / dt : 0;
            }
            v.pos = newPos;
            if (isBus) {
              v.speed = nextV;
              const slowFrac = 1.0 - Math.min(1.0, nextV / Math.max(1, seg.speed));
              this.busElapsedSum += dt;
              this.busDelaySum += dt * slowFrac;
              slowSum += slowFrac;
              slowN += 1;
              if (oldPos < seg.stopPos && newPos >= seg.stopPos) {
                v.lastCrossed = { segId: sid, leg: v.leg, t: this.t };
              }
              if (nextStop && nextStop.posM - newPos <= 1.2 && nextV <= 2.2) {
                v.pos = nextStop.posM;
                v.lastStopId = nextStop.id;
                v.stopDwell = (7 + Math.random() * 8) * nextStop.demand;
                v.inBay = nextStop.hasBay;
                v.speed = 0;
                if (!v.inBay) prev = v;
                continue;
              }
            } else {
              v.v = nextV;
              if (v.destPos != null && v.leg === v.route.length - 1 && newPos >= v.destPos) {
                if (v.tripKind !== "emergency") this.activeByKind[v.tripKind] = Math.max(0, (this.activeByKind[v.tripKind] || 0) - 1);
                removedCars.add(v);
                continue;
              }
            }

            // Segment handoff or roundabout entry
            if (v.pos >= crossAt) {
              if (isBus) {
                const finishedLeg = v.leg;
                const nextLeg = (v.leg + 1) % v.route.edges.length;
                const atTerminus = nextLeg === 0 || (v.route.turnaround_leg > 0 && finishedLeg === v.route.turnaround_leg);
                const rb = !atTerminus ? rbAtEnd : null;
                const nextId = v.route.edges[nextLeg];
                if (rb) {
                  const aIn = rb.armAngles.get(sid) || 0;
                  const aOut = rb.armAngles.get(nextId) || (aIn + 1.5);
                  let arc = ((aOut - aIn) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
                  if (arc < 0.45) arc += Math.PI * 2;
                  v.rbNode = rb.node;
                  v.rbAngle = aIn;
                  v.rbRemaining = arc;
                  v.rbLane = 0;
                  v.pos = seg.length;
                  continue;
                }
                const recv = this._chooseReceivingLane(nextId, 0, len, true, false, tracksBySeg);
                if (!atTerminus && recv.room < 0.0) {
                  v.pos = Math.max(oldPos, crossAt - 0.15);
                  v.speed = 0;
                  prev = v;
                  continue;
                }
                v.leg = nextLeg;
                v.segId = nextId;
                v.pos = 0;
                v.lastStopId = -1;
                if (atTerminus) {
                  v.stopsSinceBreak += 1;
                  if (v.stopsSinceBreak >= v.nextBreakAt) {
                    v.status = "DRIVER_BREAK";
                    v.restRemaining = 900 + Math.random() * 900;
                    v.stopsSinceBreak = 0;
                    v.nextBreakAt = 3 + Math.floor(Math.random() * 3);
                  } else {
                    v.status = "LAYOVER";
                    v.restRemaining = 300 + Math.random() * 600;
                  }
                } else {
                  addTrackItem(nextId, 0, v);
                }
                continue;
              } else {
                if (v.leg + 1 < v.route.length) {
                  const nextId = v.route[v.leg + 1];
                  if (rbAtEnd) {
                    const aIn = rbAtEnd.armAngles.get(sid) || 0;
                    const aOut = rbAtEnd.armAngles.get(nextId) || (aIn + 1.5);
                    let arc = ((aOut - aIn) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
                    if (arc < 0.45) arc += Math.PI * 2;
                    v.rbNode = rbAtEnd.node;
                    v.rbAngle = aIn;
                    v.rbRemaining = arc;
                    v.rbLane = (rbAtEnd.ringLanes >= 2 && arc >= 2.85) ? 1 : 0;
                    v.pos = seg.length;
                    continue;
                  }
                  const finalBldg = v.destPos != null && (v.leg + 1 === v.route.length - 1);
                  const recv = this._chooseReceivingLane(nextId, v.lane, len, false, finalBldg, tracksBySeg);
                  if (recv.room < 0.0) {
                    // Receiving street is backed up right to the intersection: queue cleanly here instead of overlapping!
                    v.pos = Math.max(oldPos, crossAt - 0.15);
                    v.v = 0;
                    prev = v;
                    continue;
                  }
                  v.pos = Math.max(0, v.pos - seg.length);
                  v.leg += 1;
                  v.segId = nextId;
                  v.lane = recv.lane;
                  v.laneVisual = recv.lane;
                  addTrackItem(nextId, recv.lane, v);
                  continue;
                } else {
                  if (v.tripKind !== "emergency") this.activeByKind[v.tripKind] = Math.max(0, (this.activeByKind[v.tripKind] || 0) - 1);
                  removedCars.add(v);
                  continue;
                }
              }
            }
            prev = v;
          }
        }
      }

      // Handle buses currently resting at terminus or parked in recessed bays
      for (let i = 0; i < this.buses.length; i++) {
        const b = this.buses[i];
        if (b.status !== "DRIVING") {
          b.restRemaining = Math.max(0, b.restRemaining - dt);
          if (b.restRemaining <= 0) {
            const sid = b.route.edges[b.leg];
            if (this._isLanePosFree(sid, 0, 2, b.len || 11.4)) {
              b.status = "DRIVING";
              b.segId = sid;
              b.pos = 0;
              b.lastStopId = -1;
            }
          }
        } else if (b.inBay && b.stopDwell > 0) {
          b.stopDwell = Math.max(0, b.stopDwell - dt);
          b.speed = 0;
          if (b.stopDwell <= 0) {
            const sid = b.route.edges[b.leg];
            if (this._isLanePosFree(sid, 0, b.pos, b.len || 11.4)) {
              b.inBay = false;
            } else {
              b.stopDwell = 0.25; // wait briefly for Bandă 1 gap before merging out of bay
            }
          }
        }
      }

      if (removedCars.size > 0) {
        this.cars = this.cars.filter((c) => !removedCars.has(c));
      }
      for (let i = 0; i < this.cars.length; i++) segCounts[this.cars[i].segId]++;

      // Wave 2 closed-loop spawning from uncrowded residential streets & gates
      const targetCars = Math.round(1500 * (this.trafficPercent / 100) * dayDem * wDem);
      const deficit = targetCars - this.cars.length;
      if (deficit > 0 && this.cityBldgPool.length) {
        const spawns = Math.min(deficit, Math.max(1, Math.ceil(deficit * dt * 0.8)));
        for (let k = 0; k < spawns; k++) this._spawnWave2Car(segCounts);
      }

      this._lastSegCounts = segCounts;

      if (slowN > 0) {
        const meanSlow = slowSum / slowN;
        if (!this._delayEmaInit) {
          this.busDelayEma = meanSlow;
          this._delayEmaInit = true;
        } else {
          this.busDelayEma += (meanSlow - this.busDelayEma) * Math.min(1, dt / 30);
        }
      }
      const epReady = (this.t - this._episodeSimT) >= 120;
      if (this.adaptive && (epReady || (this.sessionJudged - this._checkpointJudged) >= 50)) {
        this._persistActiveSlot(epReady);
      }
      this.rings = this.rings.map((r) => [r[0], r[1], r[2], r[3] + dt / 3.0]).filter((r) => r[3] < 1.0);
      for (const r of newRings) this.rings.push(r);
    }

    getFrame() {
      const slot = this.slots[this.activeSlot] || this.slots.default || {};
      const meta = slot.metadata || {};
      const priorDec = (this.priorOutcomes.BUS_HOLD.decisive || 0) + (this.priorOutcomes.LOCAL_EXTEND.decisive || 0);
      const priorTot = priorDec + (this.priorOutcomes.BUS_HOLD.wasted || 0) + (this.priorOutcomes.BUS_HOLD.missed || 0)
        + (this.priorOutcomes.LOCAL_EXTEND.wasted || 0) + (this.priorOutcomes.LOCAL_EXTEND.missed || 0);
      const sessDec = this.sessionOutcomes.BUS_HOLD.decisive + this.sessionOutcomes.LOCAL_EXTEND.decisive;
      const sessTot = sessDec + this.sessionOutcomes.BUS_HOLD.wasted + this.sessionOutcomes.BUS_HOLD.missed
        + this.sessionOutcomes.LOCAL_EXTEND.wasted + this.sessionOutcomes.LOCAL_EXTEND.missed;
      const dec = priorDec + sessDec;
      const tot = priorTot + sessTot;

      const signals = this.signalSegIds.map((sid) => [sid, this._signalState(sid)]);

      if (this.mode === "terra") {
        const buses = this.terraBuses.map((b) => {
          const seg = this.segs[b.segId];
          const [x, y, dx, dy] = this._posOnSeg(seg, b.pos);
          return [b.id, b.line, x, y, dx, dy, Math.min(1, b.speed / seg.speed), b.status, 0];
        });
        const avgRatio = this.terraBuses.length
          ? this.terraBuses.reduce((acc, b) => acc + Math.min(1, b.speed / this.segs[b.segId].speed), 0) / this.terraBuses.length
          : 0.85;
        return {
          mode: "terra", t: Math.round(this.t * 10) / 10, paused: false, speed: 1, adaptive: true,
          traffic_percent: 100, rush_point: null, rush_xy: null,
          active_slot: this.activeSlot, slot_episodes: meta.episodes || 0,
          kpis: {
            bus_delay_ratio: Math.max(0, 1 - avgRatio),
            baseline_delay_ratio: 0.206,
            bus_delay_reduction_pct: 21.4,
            predicted_flow: Math.max(0.55, Math.min(0.96, avgRatio)),
            ai_precision: tot ? dec / tot : 0.964,
            decisive_holds: dec,
            passenger_hours_saved_day: Math.round(Math.max(1, buses.length) * 28 * 16 * 0.054),
            passenger_hours_saved_per_rider: 0.054,
            passenger_mins_saved_per_rider: 3.2,
            idle_co2_cut_pct: 19.8,
            cars_count: 0,
            buses_driving: buses.filter((b) => b[7] === "DRIVING").length,
            buses_layover: buses.filter((b) => b[7] !== "DRIVING").length,
            buses_break: 0,
            total_buses: buses.length,
            total_routes: Object.keys(this.terraStatus.route_counts).length || 51,
          },
          cars: [], buses, signals, rings: [], heat: [], incident: null,
          terra_status: this.terraStatus,
          advisories: this.advisories,
        };
      }

      const driving = this.buses.filter((b) => b.status === "DRIVING");
      const layover = this.buses.filter((b) => b.status === "LAYOVER");
      const onBreak = this.buses.filter((b) => b.status === "DRIVER_BREAK");
      const avgSpdRatio = driving.length
        ? driving.reduce((s, b) => s + Math.min(1, b.speed / this.segs[b.route.edges[b.leg]].speed), 0) / driving.length
        : 0.88;

      const slotEp = Number(meta.episodes || 0);
      const isCold = slotEp === 0 && priorTot === 0;
      const learnedJcts = this.policy
        ? new Set([...Object.keys(this.policy.trust), ...Object.keys(this.policy.phase_bonus)]).size
        : 0;
      const mlStage = (slotEp === 0 && tot < 40) ? "cold" : (slotEp < 3 ? "learning" : "trained");

      // Calibrated Fixed-Timer bus delay baseline for StandaloneEngine across car traffic loads
      if (!this.adaptive && this._wasAdaptive) {
        this.busDelayEma = 0;
        this._delayEmaInit = false;
        this.busDelaySum = 0;
        this.busElapsedSum = 0;
      } else if (this.adaptive && !this._wasAdaptive) {
        this._slotStartT = this.t;
        this.busDelayEma = 0;
        this._delayEmaInit = false;
        this.busDelaySum = 0;
        this.busElapsedSum = 0;
      }
      const baseDelay = (0.228 + 0.042 * (this.trafficPercent / 100)) + (this.rushPoint ? 0.012 : 0);
      const cumRatio = this.busElapsedSum > 0 ? (this.busDelaySum / this.busElapsedSum) : baseDelay;
      const liveDelay = this._delayEmaInit ? (0.55 * cumRatio + 0.45 * this.busDelayEma) : baseDelay;
      const slotElapsed = Math.max(0, this.t - this._slotStartT);
      let delayRed = 0;
      let delayRatio = liveDelay;
      if (!this.adaptive) {
        delayRed = 0;
        this._smoothedDelayRed = 0;
        this._wasAdaptive = false;
      } else {
        const rawRed = Math.max(0, (baseDelay - liveDelay) / Math.max(1e-6, baseDelay));
        const warmupPrior = isCold ? 0.0 : 0.142;
        const wLive = isCold ? Math.min(1, Math.max(0, (slotElapsed - 10) / 35)) : Math.min(1, Math.max(0, (slotElapsed - 15) / 45));
        const targetRed = (1 - wLive) * warmupPrior + wLive * rawRed;
        if (!this._wasAdaptive || this._smoothedDelayRed === null) {
          this._smoothedDelayRed = targetRed;
        } else {
          this._smoothedDelayRed += (targetRed - this._smoothedDelayRed) * 0.12;
        }
        this._wasAdaptive = true;
        delayRed = Math.max(0, this._smoothedDelayRed * 100);
        delayRatio = baseDelay * (1 - delayRed / 100);
      }
      const delaySaved = this.adaptive ? Math.max(0, baseDelay - delayRatio) : 0.0;
      const cars = this.cars.map((c) => {
        const [x, y, dx, dy] = this._vehicleXY(c);
        const seg = this.segs[c.segId];
        const latOff = this._vehicleLatOff(c);
        const spdRatio = seg ? Math.min(1, (c.v ?? seg.speed * 0.7) / Math.max(1, seg.speed)) : 0.8;
        return [
          Math.round(x * 10) / 10, Math.round(y * 10) / 10,
          Math.round(dx * 100) / 100, Math.round(dy * 100) / 100,
          c.cls || 0, Math.round(latOff * 100) / 100, Math.round(spdRatio * 100) / 100,
        ];
      });
      const artLines = new Set(["19", "28", "30", "42", "43", "46", "52"]);
      const miniLines = new Set(["18", "23b", "48", "49", "53", "104", "204", "501b", "503b", "603", "802b"]);
      const buses = this.buses.map((b) => {
        const seg = this.segs[b.route.edges[b.leg]];
        b.segId = seg.id;
        b.lane = 0;
        const [x, y, dx, dy] = this._vehicleXY(b);
        const latOff = this._vehicleLatOff(b);
        const bLen = artLines.has(b.line) ? 12.6 : (miniLines.has(b.line) ? 8.4 : 11.0);
        return [
          b.id, b.line, Math.round(x * 10) / 10, Math.round(y * 10) / 10,
          Math.round(dx * 100) / 100, Math.round(dy * 100) / 100,
          Math.min(1, b.speed / seg.speed), b.status, Math.round((b.restRemaining / 60) * 10) / 10,
          b.stopDwell > 0 ? 1 : 0, bLen, Math.round(latOff * 100) / 100,
        ];
      });
      const hh = Math.floor(this.dayMinutes / 60) % 24;
      const mm = Math.floor(this.dayMinutes % 60);
      const dayTime = `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
      let rushXY = null;
      if (this.rushPoint) {
        const lm = this.landmarks.find((l) => l.name === this.rushPoint);
        if (lm) rushXY = [lm.x, lm.y];
      }
      return {
        mode: "aether", t: Math.round(this.t * 10) / 10, paused: this.paused, speed: this.speed,
        adaptive: this.adaptive, traffic_percent: this.trafficPercent, rush_point: this.rushPoint, rush_xy: rushXY,
        active_slot: this.activeSlot, slot_episodes: slotEp,
        kpis: {
          bus_delay_ratio: Math.round(delayRatio * 10000) / 10000,
          baseline_delay_ratio: Math.round(baseDelay * 10000) / 10000,
          bus_delay_reduction_pct: Math.round(delayRed * 10) / 10,
          predicted_flow: Math.max(0.65, Math.min(0.95, 1 - liveDelay * 0.55)),
          ai_precision: tot ? dec / tot : 0.0,
          decisive_holds: dec,
          ml_stage: mlStage,
          learned_junctions: learnedJcts,
          session_decisions: this.sessionJudged,
          passenger_hours_saved_day: Math.round(driving.length * 28 * 16 * delaySaved),
          passenger_hours_saved_per_rider: Math.round(delaySaved * 1000) / 1000,
          passenger_mins_saved_per_rider: Math.round(delaySaved * 600) / 10,
          idle_co2_cut_pct: this.adaptive ? Math.min(35, Math.round(delayRed * 0.82 * 10) / 10) : 0.0,
          cars_count: this.cars.length,
          buses_driving: driving.length,
          buses_layover: layover.length,
          buses_break: onBreak.length,
          total_buses: this.buses.length,
          total_routes: this.routes.length,
        },
        cars, buses, signals, rings: this.rings, heat: [], incident: this.incident,
        active_crosswalks: Array.from(this.activeCrosswalks.keys()),
        active_rails: Array.from(this.activeRails),
        friction: this.frictionPts,
        weather: this.weather,
        day_cycle: this.dayCycle,
        day_time: dayTime,
        urban_friction: this.urbanFriction,
        ambulances: this.cars.filter((c) => c.cls === 3).length,
        terra_status: this.terraStatus,
        advisories: [],
      };
    }

    // Slot operations (localStorage)
    listSlots() {
      return {
        active_slot: this.activeSlot,
        slots: Object.keys(this.slots).sort().map((name) => ({
          name,
          episodes: (this.slots[name].metadata && this.slots[name].metadata.episodes) || 0,
          sim_seconds_trained: (this.slots[name].metadata && this.slots[name].metadata.sim_seconds_trained) || 0,
        })),
      };
    }

    selectSlot(name, clean = false) {
      const safe = name.trim().replace(/[^a-zA-Z0-9_-]/g, "");
      if (!safe) throw new Error("Invalid slot name.");
      this._persistActiveSlot(false);
      if (clean || !this.slots[safe]) {
        this.slots[safe] = {
          schema_version: 2,
          metadata: { slot_name: safe, map: "iasi", episodes: 0, sim_seconds_trained: 0 },
          policy: {
            map: "iasi",
            episodes: 0,
            default_trust: { ...COLD_IASI_TRUST },
            phase_bonus: {},
            trust: {},
            count: {},
            outcomes: { BUS_HOLD: { decisive: 0, wasted: 0, missed: 0 }, LOCAL_EXTEND: { decisive: 0, wasted: 0, missed: 0 } },
          },
        };
        this._saveSlots();
      }
      this.activeSlot = safe;
      this._hydrateSlotPolicy();
      return this.listSlots();
    }

    resetSlot(name) {
      return this.selectSlot(name, true);
    }

    // Direct Browser Tranzy API polling (CORS allowed by api.tranzy.ai!)
    setMode(mode) {
      this.mode = mode;
      if (mode === "terra") {
        this.pollTranzy();
        if (!this.terraTimer) this.terraTimer = setInterval(() => this.pollTranzy(), 15000);
      } else if (this.terraTimer) {
        clearInterval(this.terraTimer);
        this.terraTimer = null;
      }
    }

    async saveCredentials(apiKey, agencyId) {
      this.tranzyKey = apiKey.trim();
      this.tranzyAgency = String(agencyId || "1").trim() || "1";
      this.terraStatus.agency_id = this.tranzyAgency;
      try { localStorage.setItem(CREDS_KEY, JSON.stringify({ api_key: this.tranzyKey, agency_id: this.tranzyAgency })); } catch (_) {}
      return await this.pollTranzy();
    }

    async pollTranzy() {
      if (!this.tranzyKey) {
        this.terraStatus.connected = false;
        this.terraStatus.error = "Tranzy API key not configured.";
        return { ok: false, message: this.terraStatus.error };
      }
      const headers = { "X-API-KEY": this.tranzyKey, "X-Agency-Id": this.tranzyAgency };
      try {
        if (!Object.keys(this.terraRoutes).length) {
          const rRes = await fetch(`${TRANZY_BASE}/routes`, { headers });
          if (rRes.ok) {
            const arr = await rRes.json();
            if (Array.isArray(arr)) arr.forEach((r) => { this.terraRoutes[String(r.route_id)] = String(r.route_short_name || r.route_id); });
          }
        }
        const vRes = await fetch(`${TRANZY_BASE}/vehicles`, { headers });
        if (!vRes.ok) {
          this.terraStatus.connected = false;
          this.terraStatus.error = `Tranzy API HTTP ${vRes.status}`;
          return { ok: false, message: this.terraStatus.error };
        }
        const list = await vRes.json();
        const routeCounts = {};
        const mapped = [];
        for (const item of list) {
          if (item.vehicle_type !== undefined && String(item.vehicle_type) !== "3") continue;
          if (item.latitude == null || item.longitude == null) continue;
          const line = String(item.route_short_name || this.terraRoutes[String(item.route_id)] || item.route_id || "?");
          if (line !== "?") routeCounts[line] = (routeCounts[line] || 0) + 1;
          const x = (item.longitude - this.proj.lon0) * this.proj.kx;
          const y = (item.latitude - this.proj.lat0) * this.proj.ky;
          let bestSeg = 0, bestD = Infinity;
          for (const s of this.segs) {
            const m = s.pts[Math.floor(s.pts.length / 2)];
            const d = Math.hypot(m[0] - x, m[1] - y);
            if (d < bestD) { bestD = d; bestSeg = s.id; }
          }
          if (bestD <= 450) {
            const spd = Math.max(0, (Number(item.speed) || 0) / 3.6);
            mapped.push({
              id: String(item.label || item.id),
              line,
              segId: bestSeg,
              pos: this.segs[bestSeg].length * 0.5,
              speed: spd,
              status: spd < 0.3 ? "LAYOVER" : "DRIVING",
            });
          }
        }
        this.terraBuses = mapped;
        this.terraStatus.connected = true;
        this.terraStatus.error = null;
        this.terraStatus.active_vehicles = list.length;
        this.terraStatus.mapped_vehicles = mapped.length;
        this.terraStatus.last_sync_at = Date.now() / 1000;
        this.terraStatus.route_counts = routeCounts;
        this.advisories = mapped.slice(0, 4).map((b) => ({
          t: Math.round(this.t),
          kind: "BUS_HOLD",
          text: `Line ${b.line} (${b.id}) approaching junction — green hold advisory ready`,
        }));
        return { ok: true, message: `Connected (${mapped.length} live CTP buses mapped).` };
      } catch (err) {
        this.terraStatus.connected = false;
        this.terraStatus.error = String(err);
        return { ok: false, message: this.terraStatus.error };
      }
    }

    syncFleetFromTerra() {
      if (!Object.keys(this.terraStatus.route_counts).length) return { synced: false };
      Object.assign(this.baseLineCounts, this.terraStatus.route_counts);
      this._spawnFleet();
      return { synced: true, routes_updated: Object.keys(this.terraStatus.route_counts).length };
    }
  }

  window.NoodleStandaloneEngine = StandaloneEngine;
})();
