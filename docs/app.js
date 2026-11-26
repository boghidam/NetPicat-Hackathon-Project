/**
 * Noodle Traffic Control — Interactive Web UI (Green & White Civic Theme)
 * Supports both FastAPI WebSocket backend and Standalone GitHub Pages browser mode.
 */
(function () {
  const CFG = window.NOODLE_CONFIG || {};
  const API_BASE = (CFG.API_BASE_URL || "").replace(/\/$/, "");
  const THEME_KEY = "noodle_theme_v1";

  const isMobile = () => window.innerWidth <= 768;

  const state = {
    theme: "light",
    backendOnline: false,
    standalone: null,
    ws: null,
    map: null,
    frame: null,
    showHeat: false,
    showRoutes: true,
    showCars: true,
    showBuildings: true,
    drawerCollapsed: false,
    mobileSnap: "peek", // "peek" | "half" | "full"
    mobileTab: "controls", // "controls" | "impact"
    wasMobile: isMobile(),
    isTouchInspect: false,
    camera: { cx: 0, cy: 0, scale: 0.14 },
    minScale: 0.012,
    dragging: false,
    lastMouse: { x: 0, y: 0 },
    hoverMouse: { x: -1, y: -1 },
    speeds: [1, 2, 4, 8],
    speedIdx: 0,
    trafficSliderDragging: false,
    trafficSliderPendingVal: null,
    trafficSliderHoldUntil: 0,
    trafficSliderLastSent: null,
  };

  // DOM helpers
  const $ = (id) => document.getElementById(id);
  const canvas = $("cityCanvas");
  const ctx = canvas.getContext("2d");
  const workspaceEl = document.querySelector(".workspace");
  const mapControlsEl = document.querySelector(".map-controls");
  const drawerInnerEl = $("drawer") ? $("drawer").querySelector(".drawer-inner") : null;

  function updateTrafficSliderTrack(val) {
    const sl = $("trafficSlider");
    if (!sl) return;
    const pct = Math.max(0, Math.min(100, (Number(val) / 300) * 100));
    sl.style.setProperty("--slider-pct", `${pct}%`);
  }

  function applyTheme(theme) {
    const t = theme === "dark" ? "dark" : "light";
    state.theme = t;
    document.documentElement.dataset.theme = t;
    const moon = $("themeIconMoon");
    const sun = $("themeIconSun");
    if (moon) moon.classList.toggle("hidden", t === "dark");
    if (sun) sun.classList.toggle("hidden", t !== "dark");
    const metaTheme = document.querySelector('meta[name="theme-color"]');
    if (metaTheme) metaTheme.setAttribute("content", t === "dark" ? "#061913" : "#0f3d2e");
    try { localStorage.setItem(THEME_KEY, t); } catch (_) {}
  }

  function setMobileTab(tab) {
    state.mobileTab = tab === "impact" ? "impact" : "controls";
    const drawer = $("drawer");
    drawer.dataset.mobileView = state.mobileTab;
    $("tabMobileControls").classList.toggle("active", state.mobileTab === "controls");
    $("tabMobileControls").setAttribute("aria-selected", String(state.mobileTab === "controls"));
    $("tabMobileImpact").classList.toggle("active", state.mobileTab === "impact");
    $("tabMobileImpact").setAttribute("aria-selected", String(state.mobileTab === "impact"));
    const inner = drawer.querySelector(".drawer-inner");
    if (inner) inner.scrollTop = 0;
  }

  function setMobileSnap(snap, skipReframe = false) {
    const valid = snap === "full" || snap === "half" ? snap : "peek";
    const prev = state.mobileSnap;
    state.mobileSnap = valid;
    state.drawerCollapsed = valid === "peek";
    const drawer = $("drawer");
    drawer.style.height = "";
    drawer.style.transform = "";
    drawer.classList.remove("dragging");
    drawer.dataset.snap = valid;
    drawer.classList.toggle("collapsed", valid === "peek");
    if (workspaceEl) {
      workspaceEl.classList.remove("sheet-dragging");
      workspaceEl.classList.toggle("sheet-half", valid === "half");
      workspaceEl.classList.toggle("sheet-full", valid === "full");
    }
    if (mapControlsEl) {
      mapControlsEl.style.bottom = "";
      mapControlsEl.style.opacity = "";
      mapControlsEl.style.pointerEvents = "";
    }
    const lbl = $("peekCtrlLabel");
    if (lbl) lbl.textContent = valid === "peek" ? "Controls" : "Map";
    if (valid === "peek") {
      $("statTooltip").classList.add("hidden");
    }
    // Smoothly keep the map center framed in the open viewport above the bottom sheet
    if (!skipReframe && isMobile() && state.map && prev !== valid) {
      const vh = window.innerHeight || 700;
      const occ = (s) => (s === "peek" ? 68 : vh * 0.58);
      const deltaPx = (occ(valid) - occ(prev)) * 0.5;
      if (Math.abs(deltaPx) > 1 && state.camera.scale > 0) {
        state.camera.cy -= deltaPx / state.camera.scale;
      }
    }
  }

  function resizeCanvas() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function fitMap() {
    if (!state.map) return;
    const [x0, y0, x1, y1] = state.map.bounds;
    const rect = canvas.parentElement.getBoundingClientRect();
    const mapW = Math.max(1, x1 - x0);
    const mapH = Math.max(1, y1 - y0);

    if (isMobile()) {
      const sheetOcc = state.mobileSnap === "half" ? rect.height * 0.58 : 80;
      const topOcc = 44;
      const availH = Math.max(180, rect.height - sheetOcc - topOcc);
      const sx = (rect.width * 0.93) / mapW;
      const sy = (availH * 0.90) / mapH;
      state.camera.scale = Math.min(sx, sy);
      state.minScale = Math.min(0.012, state.camera.scale * 0.65);
      state.camera.cx = (x0 + x1) / 2;
      // Shift center slightly so Iași sits centered in the open map area above the bottom sheet
      const centerShiftPx = (sheetOcc - topOcc) * 0.5;
      state.camera.cy = (y0 + y1) / 2 - centerShiftPx / state.camera.scale;
      return;
    }

    const drawerOffset = state.drawerCollapsed ? 0 : 340;
    const availW = Math.max(260, rect.width - drawerOffset);
    const sx = (availW * 0.88) / mapW;
    const sy = (rect.height * 0.88) / mapH;
    state.camera.scale = Math.min(sx, sy);
    state.minScale = Math.min(0.018, state.camera.scale * 0.6);
    state.camera.cx = (x0 + x1) / 2 + (drawerOffset * 0.45) / state.camera.scale;
    state.camera.cy = (y0 + y1) / 2;
  }

  function toScreen(wx, wy) {
    const rect = canvas.parentElement.getBoundingClientRect();
    return [
      rect.width / 2 + (wx - state.camera.cx) * state.camera.scale,
      rect.height / 2 - (wy - state.camera.cy) * state.camera.scale,
    ];
  }

  function toWorld(sx, sy) {
    const rect = canvas.parentElement.getBoundingClientRect();
    return [
      state.camera.cx + (sx - rect.width / 2) / state.camera.scale,
      state.camera.cy - (sy - rect.height / 2) / state.camera.scale,
    ];
  }

  function zoomAt(sx, sy, factor) {
    const [wx, wy] = toWorld(sx, sy);
    const minSc = state.minScale || 0.012;
    state.camera.scale = Math.max(minSc, Math.min(6.0, state.camera.scale * factor));
    const rect = canvas.parentElement.getBoundingClientRect();
    state.camera.cx = wx - (sx - rect.width / 2) / state.camera.scale;
    state.camera.cy = wy + (sy - rect.height / 2) / state.camera.scale;
  }

  // ==================== API / STANDALONE BRIDGE ====================
  async function apiFetch(path, opts = {}) {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return await res.json();
  }

  function sendCmd(payload) {
    if (state.backendOnline && state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(payload));
      return;
    }
    const eng = state.standalone;
    if (!eng || !state.map) return;
    if (payload.cmd === "set_mode") eng.setMode(payload.mode);
    else if (payload.cmd === "set_adaptive") eng.adaptive = !!payload.adaptive;
    else if (payload.cmd === "set_paused") eng.paused = !!payload.paused;
    else if (payload.cmd === "set_speed") eng.speed = payload.speed;
    else if (payload.cmd === "set_traffic") { eng.trafficPercent = payload.percent; eng._spawnCars(1500); }
    else if (payload.cmd === "set_rush") eng.rushPoint = payload.rush_point || null;
    else if (payload.cmd === "trigger_incident") {
      const lm = eng.landmarks[Math.floor(Math.random() * eng.landmarks.length)];
      eng.incident = lm ? [lm.x, lm.y] : [0, 0];
    } else if (payload.cmd === "set_weather") {
      eng.setWeather(payload.weather);
    } else if (payload.cmd === "set_day_cycle") {
      eng.setDayCycle(payload.enabled);
    } else if (payload.cmd === "set_urban_friction") {
      eng.setUrbanFriction(payload.enabled);
    } else if (payload.cmd === "dispatch_ambulance") {
      eng.dispatchAmbulance();
    } else if (payload.cmd === "reset_sim") {
      eng.t = 0; eng.incident = null; eng.ambulances = []; eng._spawnFleet(); eng._spawnCars(1500);
    }
    state.frame = eng.getFrame();
    updateUI(state.frame);
  }

  async function refreshSlots() {
    const data = state.backendOnline ? await apiFetch("/api/slots") : state.standalone.listSlots();
    const active = data.slots.find((s) => s.name === data.active_slot) || { name: data.active_slot, episodes: 0 };
    state.activeSlotMeta = active;
    $("slotBtnLabel").textContent = isMobile()
      ? active.name
      : `Slot: ${active.name} (${active.episodes} ep)`;
    const list = $("slotList");
    list.innerHTML = "";
    data.slots.forEach((s) => {
      const btn = document.createElement("button");
      btn.className = "slot-item" + (s.name === data.active_slot ? " active" : "");
      btn.innerHTML = `<span>${s.name}</span><span class="slot-meta">${s.episodes} ep</span>`;
      btn.onclick = async () => {
        $("slotDropdown").classList.add("hidden");
        if (state.backendOnline) await apiFetch("/api/slots/select", { method: "POST", body: JSON.stringify({ slot: s.name, clean: false }) });
        else {
          state.standalone.selectSlot(s.name, false);
          state.frame = state.standalone.getFrame();
          updateUI(state.frame);
        }
        await refreshSlots();
      };
      list.appendChild(btn);
    });
  }

  async function refreshCredentialsBadge() {
    if (state.backendOnline) {
      const c = await apiFetch("/api/credentials");
      $("apiDot").classList.toggle("connected", !!(c.has_key || c.connected));
      $("inputAgencyId").value = c.agency_id || "1";
      $("inputApiKey").placeholder = c.has_key ? `Saved (${c.masked_key})` : "Paste your Tranzy API key";
    } else {
      const has = !!(state.standalone && state.standalone.tranzyKey);
      $("apiDot").classList.toggle("connected", has);
      $("inputAgencyId").value = (state.standalone && state.standalone.tranzyAgency) || "1";
      $("inputApiKey").placeholder = has ? "Pre-configured / saved (masked)" : "Paste your Tranzy API key";
    }
  }

  // ==================== UI UPDATE (EXECUTIVE DRAWER) ====================
  function updateUI(f) {
    if (!f) return;
    const isTerra = f.mode === "terra";
    $("btnModeAether").classList.toggle("active", !isTerra);
    $("btnModeTerra").classList.toggle("active", isTerra);
    $("aetherControls").classList.toggle("hidden", isTerra);
    $("terraControls").classList.toggle("hidden", !isTerra);

    const k = f.kpis;
    const carsTxt = k.cars_count != null ? ` · ${k.cars_count.toLocaleString()} Vehicles` : "";
    const clockTxt = f.day_cycle && f.day_time ? ` · ${f.day_time}` : "";
    const weatherTxt = f.weather && f.weather !== "clear" ? ` · ${f.weather === "rain" ? "Rain" : "Snow"}` : "";
    if (isMobile()) {
      const mCars = k.cars_count != null ? ` · ${k.cars_count.toLocaleString()} veh` : "";
      $("mapClockText").textContent = isTerra
        ? `Terra · ${k.total_buses} Live Buses`
        : `Aether · ${f.t.toFixed(0)}s${clockTxt}${weatherTxt}${mCars}`;
    } else {
      $("mapClockText").textContent = isTerra
        ? `Terra (Real) · ${k.total_buses} Live CTP Buses · Tranzy API`
        : `Aether (Simulation) · t = ${f.t.toFixed(1)}s${clockTxt}${weatherTxt} · ${k.total_routes} Lines${carsTxt}`;
    }

    // Status pill & live ML stage
    const badge = $("statusBadge");
    if (isTerra) {
      badge.className = "status-pill adaptive";
      $("statusBadgeText").textContent = f.terra_status.connected ? "TERRA LIVE" : "AWAITING API KEY";
    } else if (f.adaptive) {
      badge.className = "status-pill adaptive";
      $("statusBadgeText").textContent = k.ml_stage === "cold"
        ? "LEARNING · COLD START"
        : (k.ml_stage === "learning" ? "ADAPTIVE AI LEARNING" : "ADAPTIVE AI ACTIVE");
    } else {
      badge.className = "status-pill fixed";
      $("statusBadgeText").textContent = "FIXED TIMERS";
    }

    if (f.active_slot != null) {
      const ep = Number(f.slot_episodes || 0);
      $("slotBtnLabel").textContent = isMobile() ? f.active_slot : `Slot: ${f.active_slot} (${ep} ep)`;
      state.activeSlotMeta = { name: f.active_slot, episodes: ep };
    }

    // Hero KPI
    const redPct = k.bus_delay_reduction_pct;
    const delayStr = isTerra
      ? `-${Math.abs(redPct).toFixed(1)}%`
      : (f.adaptive ? `-${Math.max(0, redPct).toFixed(1)}%` : "0.0%");
    $("kpiDelayReduction").textContent = delayStr;

    // Secondary KPIs (Strictly honest bus-inferred Predicted Flow!)
    const flowStr = `${Math.round(k.predicted_flow * 100)}%`;
    $("kpiPredictedFlow").textContent = flowStr;

    // Mobile bottom-sheet Peek Bar live summary
    const peekDelay = $("peekDelay");
    if (peekDelay) {
      peekDelay.textContent = delayStr;
      $("peekFlow").textContent = flowStr;
      $("peekDot").classList.toggle("fixed", !isTerra && !f.adaptive);
      const peekPauseBtn = $("btnPeekPause");
      if (peekPauseBtn) {
        peekPauseBtn.classList.toggle("hidden", isTerra);
        const pIcon = $("peekPauseIcon");
        if (pIcon) {
          pIcon.innerHTML = f.paused
            ? '<path d="M5.5 3.5l9 5.5-9 5.5v-11z" fill="currentColor"/>'
            : '<rect x="4.5" y="3.5" width="3" height="11" rx="1" fill="currentColor"/><rect x="10.5" y="3.5" width="3" height="11" rx="1" fill="currentColor"/>';
        }
      }
    }
    $("kpiPrecision").textContent = (k.decisive_holds > 0 || k.ai_precision > 0) ? `${(k.ai_precision * 100).toFixed(1)}%` : "—";
    const jctSuffix = k.learned_junctions ? ` · ${k.learned_junctions} jct` : "";
    $("kpiHoldsSub").textContent = `${Number(k.decisive_holds || 0).toLocaleString()} decisive holds${jctSuffix}`;

    // Municipal Value Bars
    $("barAdaptive").style.height = f.adaptive ? `${Math.min(95, Math.max(48, 60 + Math.abs(redPct) * 1.1))}%` : "54%";
    $("barFixed").style.height = "54%";
    $("kpiPaxHours").textContent = `~${Number(k.passenger_hours_saved_day || 0).toLocaleString()} hrs/day`;
    const perHrs = Number(k.passenger_hours_saved_per_rider || 0).toFixed(3);
    const perMin = Number(k.passenger_mins_saved_per_rider || 0).toFixed(1);
    $("kpiPaxPerRider").innerHTML = `~${perHrs} hrs/day <small>(${perMin} min)</small>`;
    $("kpiCo2").textContent = `-${Math.round(k.idle_co2_cut_pct)}%`;

    // Fleet Breakdown (Driving / Layover / Driver Break)
    $("fleetTitle").textContent = isTerra
      ? `Live CTP Iași Fleet (${k.total_routes} Active Lines)`
      : `CTP Iași Fleet (${k.total_routes} Lines)`;
    $("fleetTotal").textContent = `${k.total_buses} buses`;
    $("cntDriving").textContent = k.buses_driving;
    $("cntLayover").textContent = k.buses_layover;
    $("cntBreak").textContent = k.buses_break;

    // Aether controls sync
    $("btnCtrlAdaptive").classList.toggle("active", f.adaptive);
    $("btnCtrlFixed").classList.toggle("active", !f.adaptive);
    $("btnPlayPause").textContent = f.paused ? "Resume" : "Pause";
    $("btnSpeed").textContent = `${f.speed}× Speed`;
    const simTraffic = Math.min(300, Math.max(0, Math.round(f.traffic_percent)));
    if (state.trafficSliderPendingVal !== null && simTraffic === state.trafficSliderPendingVal) {
      state.trafficSliderPendingVal = null;
      state.trafficSliderHoldUntil = 0;
    }
    const sliderLocked =
      state.trafficSliderDragging ||
      (state.trafficSliderPendingVal !== null && performance.now() < state.trafficSliderHoldUntil);
    if (!sliderLocked) {
      $("trafficVal").textContent = `${simTraffic}%`;
      const sl = $("trafficSlider");
      if (sl && Number(sl.value) !== simTraffic) sl.value = String(simTraffic);
      updateTrafficSliderTrack(simTraffic);
    }
    if (document.activeElement !== $("rushSelect")) $("rushSelect").value = f.rush_point || "";

    // Realism controls sync (Weather, Day Cycle, Urban Friction)
    const curW = f.weather || "clear";
    if ($("btnWeatherClear")) $("btnWeatherClear").classList.toggle("active", curW === "clear");
    if ($("btnWeatherRain")) $("btnWeatherRain").classList.toggle("active", curW === "rain");
    if ($("btnWeatherSnow")) $("btnWeatherSnow").classList.toggle("active", curW === "snow");
    $("btnDayCycle").classList.toggle("active", !!f.day_cycle);
    $("btnDayCycle").textContent = f.day_cycle && f.day_time ? `Clock ${f.day_time}` : "Day Cycle";
    $("btnFriction").classList.toggle("active", !!f.urban_friction);

    // Terra status & advisories
    if (isTerra) {
      const ts = f.terra_status;
      const box = $("terraStatusBox");
      $("apiDot").classList.toggle("connected", !!ts.connected);
      if (ts.connected) {
        box.className = "terra-status ok";
        const ago = ts.last_sync_at ? `${Math.max(0, Math.round(Date.now() / 1000 - ts.last_sync_at))}s ago` : "just now";
        box.textContent = `Connected to Tranzy (Agency #${ts.agency_id}) · ${ts.mapped_vehicles} buses mapped · Synced ${ago}`;
      } else {
        box.className = "terra-status";
        box.textContent = ts.error || "Enter your Tranzy API key to stream real-time CTP Iași buses.";
      }
      const adv = $("advisoriesList");
      if (f.advisories && f.advisories.length) {
        adv.innerHTML = f.advisories.map((a) => `<div class="adv-item"><strong>${a.kind.replace("_", " ")}</strong> · ${a.text}</div>`).join("");
      } else {
        adv.innerHTML = `<div class="adv-empty">${ts.connected ? "Monitoring approaching buses for green-hold opportunities…" : "Waiting for real-time CTP bus telemetry…"}</div>`;
      }
    }
  }

  // ==================== THEME-AWARE GREEN & WHITE / DARK MAP RENDERER ====================
  function drawMap() {
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width, H = rect.height;
    const isDark = state.theme === "dark";
    const weather = (state.frame && state.frame.weather) || "clear";
    ctx.fillStyle = isDark
      ? (weather === "snow" ? "#111e24" : weather === "rain" ? "#0b1817" : "#0a1613")
      : (weather === "snow" ? "#eef4f7" : weather === "rain" ? "#e9f0ed" : "#f2f6f3");
    ctx.fillRect(0, 0, W, H);

    if (!state.map) return;
    const sc = state.camera.scale;

    // Subtle urban grid backdrop texture
    ctx.strokeStyle = isDark ? "rgba(74, 222, 128, 0.045)" : "rgba(20, 83, 45, 0.035)";
    ctx.lineWidth = 1;
    const gridStep = 1000 * sc;
    if (gridStep > 24) {
      const [ox, oy] = toScreen(0, 0);
      for (let x = ox % gridStep; x < W; x += gridStep) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
      for (let y = oy % gridStep; y < H; y += gridStep) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
    }

    // 0.5. Residential Buildings (Apartment Blocks & Detached Houses — civic architectural layer)
    let hoveredBuilding = null;
    if (state.showBuildings && state.map.buildings && state.map.buildings.length) {
      const detailedApt = sc >= 0.22;
      const detailedHouse = sc >= 0.28;
      const checkHover = sc >= 0.18 && state.hoverMouse.x >= 0;
      for (const b of state.map.buildings) {
        const [bid, isApt, isCity, bx, by, bw, bd, ang] = b;
        const [sx, sy] = toScreen(bx, by);
        if (sx < -36 || sx > W + 36 || sy < -36 || sy > H + 36) continue;

        const sw = Math.max(isApt ? 3.2 : 2.2, bw * sc);
        const sd = Math.max(isApt ? 2.0 : 1.8, bd * sc);

        if (checkHover && !hoveredBuilding) {
          const hitR = Math.max(7, Math.max(sw, sd) * 0.55);
          if (Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) <= hitR) {
            hoveredBuilding = b;
          }
        }

        ctx.save();
        ctx.translate(sx, sy);
        ctx.rotate(-ang); // canvas Y points down, world Y points north
        if (isApt) {
          ctx.fillStyle = isDark ? "#132922" : "#d5e4da";
          ctx.strokeStyle = isDark ? "rgba(74, 222, 128, 0.22)" : "rgba(20, 83, 45, 0.30)";
          ctx.lineWidth = sc >= 0.35 ? 1.15 : 0.75;
          ctx.fillRect(-sw / 2, -sd / 2, sw, sd);
          ctx.strokeRect(-sw / 2, -sd / 2, sw, sd);
          if (detailedApt && sw > 7 && sd > 4.5) {
            ctx.fillStyle = isDark ? "#1a352c" : "#eaf3ed";
            ctx.fillRect(-sw * 0.34, -sd * 0.28, sw * 0.68, sd * 0.56);
          }
        } else {
          ctx.fillStyle = isDark ? "#162b24" : "#e8ece0";
          ctx.strokeStyle = isDark ? "rgba(134, 239, 172, 0.24)" : "rgba(22, 101, 52, 0.34)";
          ctx.lineWidth = sc >= 0.35 ? 1.05 : 0.7;
          ctx.fillRect(-sw / 2, -sd / 2, sw, sd);
          ctx.strokeRect(-sw / 2, -sd / 2, sw, sd);
          if (detailedHouse && sw > 4.5) {
            ctx.beginPath();
            ctx.moveTo(-sw * 0.38, 0);
            ctx.lineTo(sw * 0.38, 0);
            ctx.strokeStyle = isDark ? "rgba(134, 239, 172, 0.22)" : "rgba(20, 83, 45, 0.32)";
            ctx.lineWidth = 0.9;
            ctx.stroke();
          }
        }
        ctx.restore();
      }
    }

    // 1. Multi-Lane Road Casings, Surfaces & Paved Junction Aprons
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    const roadCasing = isDark ? "#2d4b40" : "#bccdc2";
    const roadSurface = isDark ? "#1a2f28" : "#ffffff";
    const exag = 1.0 + 1.05 * Math.max(0, Math.min(1, (0.55 - sc) / 0.45));
    for (const pass of [0, 1]) {
      for (const road of state.map.roads) {
        const lanesDir = road.lanes || 1;
        const totalLanes = road.total_lanes || (road.oneway ? lanesDir : lanesDir * 2);
        const minW = road.oneway
          ? (road.hwy.includes("primary") ? 7.0 : road.hwy.includes("secondary") ? 5.8 : 5.0)
          : (road.hwy.includes("primary") ? 10.5 : road.hwy.includes("secondary") ? 8.8 : 8.4);
        const baseW = Math.max(
          minW,
          road.oneway ? (totalLanes * 3.35 + 1.5) : (totalLanes * 3.5 + 2.4)
        );
        const w = Math.max(2.2, exag * baseW * sc);
        ctx.beginPath();
        road.pts.forEach(([x, y], idx) => {
          const [sx, sy] = toScreen(x, y);
          if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
        });
        ctx.strokeStyle = pass === 0 ? roadCasing : roadSurface;
        ctx.lineWidth = pass === 0 ? w + 2.8 : w;
        ctx.stroke();
      }
      if (state.map.junctions && state.map.junctions.length) {
        ctx.fillStyle = pass === 0 ? roadCasing : roadSurface;
        for (const jn of state.map.junctions) {
          const [sx, sy] = toScreen(jn[2], jn[3]);
          if (sx < -45 || sx > W + 45 || sy < -45 || sy > H + 45) continue;
          const apronR = Math.max(2.6, exag * jn[5] * sc);
          ctx.beginPath();
          ctx.arc(sx, sy, pass === 0 ? apronR + 1.4 : apronR, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    // 1.2. Crisp Center Medians & Dashed Multi-Lane Markings (zoomed in)
    if (sc >= 0.24) {
      for (const road of state.map.roads) {
        const lanesDir = road.lanes || 1;
        if (lanesDir < 2 && road.oneway) continue;
        // Center divider separating opposing directions on 2-way roads
        if (!road.oneway) {
          ctx.beginPath();
          road.pts.forEach(([x, y], idx) => {
            const [sx, sy] = toScreen(x, y);
            if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
          });
          ctx.setLineDash(lanesDir >= 2 ? [] : [4, 5]);
          ctx.strokeStyle = isDark
            ? (lanesDir >= 2 ? "rgba(134, 239, 172, 0.28)" : "rgba(134, 239, 172, 0.16)")
            : (lanesDir >= 2 ? "rgba(20, 83, 45, 0.22)" : "rgba(20, 83, 45, 0.12)");
          ctx.lineWidth = lanesDir >= 2 ? Math.max(1.0, 1.3 * sc) : 0.85;
          ctx.stroke();
          ctx.setLineDash([]);
        }
        // Subtle dashed band dividers inside 2-lane and 3-lane directions
        if (lanesDir >= 2 && sc >= 0.32 && road.pts.length >= 2) {
          ctx.setLineDash([5, 6]);
          ctx.strokeStyle = isDark ? "rgba(148, 196, 172, 0.26)" : "rgba(100, 125, 110, 0.28)";
          ctx.lineWidth = 0.85;
          const offsets = road.oneway
            ? (lanesDir === 2 ? [0] : [-1.55, 1.55])
            : (lanesDir === 2 ? [3.45, -3.45] : [3.45, 6.55, -3.45, -6.55]);
          for (const off of offsets) {
            ctx.beginPath();
            for (let i = 0; i < road.pts.length; i++) {
              const p = road.pts[i];
              const q = road.pts[Math.min(road.pts.length - 1, i + 1)];
              const prev = road.pts[Math.max(0, i - 1)];
              let dx = q[0] - prev[0], dy = q[1] - prev[1];
              const mag = Math.hypot(dx, dy) || 1;
              dx /= mag; dy /= mag;
              const [sx, sy] = toScreen(p[0] + dy * off, p[1] - dx * off);
              if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
            }
            ctx.stroke();
          }
          ctx.setLineDash([]);
        }
      }
    }

    // 1.3. Clean Junction Interior Pads, Signalized Decks & Romanian Cedează Yield Markings
    let hoveredJunction = null;
    if (state.map.junctions && state.map.junctions.length) {
      const hitJ = state.hoverMouse.x >= 0;
      for (const jn of state.map.junctions) {
        const [, , jx, jy, jkind, radM, yieldBars] = jn;
        const [sx, sy] = toScreen(jx, jy);
        if (sx < -45 || sx > W + 45 || sy < -45 || sy > H + 45) continue;
        const apronR = Math.max(2.6, exag * radM * sc);

        if (hitJ && sc >= 0.14) {
          const jHitR = state.isTouchInspect ? Math.max(16, apronR + 6) : Math.max(10, apronR + 4);
          if (Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) <= jHitR) {
            hoveredJunction = jn;
          }
        }

        // Mask lane dividers inside junction interior so they stop cleanly at the apron threshold
        if (sc >= 0.18) {
          ctx.beginPath();
          ctx.arc(sx, sy, Math.max(2.2, apronR * 0.92), 0, Math.PI * 2);
          ctx.fillStyle = roadSurface;
          ctx.fill();
        }

        if (jkind === "signalized" && sc >= 0.22) {
          ctx.beginPath();
          ctx.arc(sx, sy, Math.max(2.4, apronR * 0.80), 0, Math.PI * 2);
          ctx.fillStyle = isDark ? "#1e372e" : "#f7fbf8";
          ctx.fill();
          ctx.strokeStyle = isDark ? "rgba(134, 239, 172, 0.24)" : "rgba(20, 83, 45, 0.16)";
          ctx.lineWidth = 0.9;
          ctx.stroke();
          if (sc >= 0.36 && apronR >= 4.5) {
            const ch = Math.min(3.5, apronR * 0.30);
            ctx.beginPath();
            ctx.moveTo(sx - ch, sy); ctx.lineTo(sx + ch, sy);
            ctx.moveTo(sx, sy - ch); ctx.lineTo(sx, sy + ch);
            ctx.strokeStyle = isDark ? "rgba(134, 239, 172, 0.25)" : "rgba(20, 83, 45, 0.18)";
            ctx.lineWidth = 0.9;
            ctx.stroke();
          }
        } else if (jkind === "yield" && sc >= 0.18) {
          ctx.strokeStyle = "#b45309";
          ctx.lineWidth = sc > 0.35 ? 2.2 : 1.5;
          ctx.setLineDash([2.5, 2.2]);
          for (const yb of yieldBars || []) {
            const [ax, ay] = toScreen(yb[0], yb[1]);
            const [bx, by] = toScreen(yb[2], yb[3]);
            ctx.beginPath();
            ctx.moveTo(ax, ay);
            ctx.lineTo(bx, by);
            ctx.stroke();
          }
          ctx.setLineDash([]);
          if (sc >= 0.26) {
            for (const yb of yieldBars || []) {
              const mx = (yb[0] + yb[2]) * 0.5 - (yb[4] || 0) * 4.6;
              const my = (yb[1] + yb[3]) * 0.5 - (yb[5] || 0) * 4.6;
              const [tx, ty] = toScreen(mx, my);
              ctx.save();
              ctx.translate(tx, ty);
              ctx.rotate(Math.atan2(-(yb[5] || 0), yb[4] || 1));
              ctx.beginPath();
              ctx.moveTo(3.8, 0);
              ctx.lineTo(-3.0, -3.3);
              ctx.lineTo(-3.0, 3.3);
              ctx.closePath();
              ctx.fillStyle = "#ffffff";
              ctx.fill();
              ctx.strokeStyle = "#dc2626";
              ctx.lineWidth = 1.25;
              ctx.stroke();
              ctx.restore();
            }
          }
        }
      }
    }

    // 1.5. Real Romanian Circular Roundabouts (Snow White ring + dashed HG 130/2023 lane + Forest Green island)
    let hoveredRoundabout = null;
    if (state.map.roundabouts && state.map.roundabouts.length) {
      for (const rb of state.map.roundabouts) {
        const [, , rx, ry, radM, ringLanes, isSig] = rb;
        const [sx, sy] = toScreen(rx, ry);
        if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;
        const outerR = Math.max(7.5, (radM + 2.5) * sc);
        const ringW = Math.max(3.5, (ringLanes >= 2 ? 7.2 : 4.6) * sc);
        const islandR = Math.max(2.6, outerR - ringW);
        const rbHitR = state.isTouchInspect ? outerR + 10 : outerR + 5;

        if (state.hoverMouse.x >= 0 && Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) <= rbHitR) {
          hoveredRoundabout = rb;
        }

        // Outer road casing & circular asphalt ring
        ctx.beginPath();
        ctx.arc(sx, sy, outerR, 0, Math.PI * 2);
        ctx.fillStyle = roadSurface;
        ctx.fill();
        ctx.strokeStyle = isDark
          ? (isSig ? "rgba(74, 222, 128, 0.55)" : "#375a4e")
          : (isSig ? "rgba(20, 83, 45, 0.42)" : "#b9cbc0");
        ctx.lineWidth = 1.6;
        ctx.stroke();

        // Dashed concentric ring lane divider for 2-lane roundabouts (HG 130/2023)
        if (ringLanes >= 2 && sc >= 0.22) {
          ctx.beginPath();
          ctx.arc(sx, sy, islandR + ringW * 0.5, 0, Math.PI * 2);
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = isDark ? "rgba(148, 196, 172, 0.38)" : "rgba(100, 125, 110, 0.38)";
          ctx.lineWidth = 0.9;
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }

    // 1.6. Real-World CTP Iași "Rond" Transit Turnaround Loops (spur connector + paved tram/bus bay ring)
    let hoveredRondLoop = null;
    if (state.map.rond_loops && state.map.rond_loops.length) {
      for (const rl of state.map.rond_loops) {
        const [, , lx, ly, radM, lkind, , nx, ny] = rl;
        const [sx, sy] = toScreen(lx, ly);
        if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;
        const outerR = Math.max(5.8, (radM + 1.8) * sc);
        const ringW = Math.max(2.8, 4.4 * sc);
        const islandR = Math.max(2.1, outerR - ringW);
        const rlHitR = state.isTouchInspect ? outerR + 10 : outerR + 5;

        if (state.hoverMouse.x >= 0 && Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) <= rlHitR) {
          hoveredRondLoop = rl;
        }

        // Paved connector spur linking anchor node into the transit turnaround loop
        const [nsx, nsy] = toScreen(nx, ny);
        const distPx = Math.hypot(sx - nsx, sy - nsy);
        if (distPx > 2.5) {
          ctx.beginPath();
          ctx.moveTo(nsx, nsy);
          ctx.lineTo(sx, sy);
          ctx.strokeStyle = isDark ? "#2f6b4f" : "#9ecab1";
          ctx.lineWidth = ringW + 2.4;
          ctx.stroke();
          ctx.strokeStyle = roadSurface;
          ctx.lineWidth = ringW;
          ctx.stroke();
        }

        // Paved turnaround ring
        ctx.beginPath();
        ctx.arc(sx, sy, outerR, 0, Math.PI * 2);
        ctx.fillStyle = roadSurface;
        ctx.fill();
        ctx.strokeStyle = lkind === "tram_bus"
          ? (isDark ? "rgba(74, 222, 128, 0.65)" : "rgba(21, 128, 61, 0.56)")
          : (isDark ? "rgba(56, 189, 248, 0.62)" : "rgba(14, 116, 144, 0.52)");
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // Inner tram track dashed guide inside tram+bus rond loops
        if (lkind === "tram_bus" && sc >= 0.18) {
          ctx.beginPath();
          ctx.arc(sx, sy, islandR + ringW * 0.5, 0, Math.PI * 2);
          ctx.setLineDash([2.8, 2.8]);
          ctx.strokeStyle = "rgba(21, 128, 61, 0.52)";
          ctx.lineWidth = 1.0;
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
    }

    // 1.7. Real CTP Bus Stops & Zebra Crosswalks & CFR Rail Crossings (zoomed in)
    if (sc >= 0.20 && state.map.bus_stops) {
      ctx.fillStyle = "#16a34a";
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.0;
      for (const st of state.map.bus_stops) {
        const [, bx, by, hasBay] = st;
        const [sx, sy] = toScreen(bx, by);
        if (sx < -12 || sx > W + 12 || sy < -12 || sy > H + 12) continue;
        ctx.beginPath();
        ctx.arc(sx, sy, hasBay ? 2.7 : 2.1, 0, Math.PI * 2);
        ctx.fill();
        if (sc >= 0.35) ctx.stroke();
      }
    }
    const f = state.frame;
    const activeCwSet = new Set((f && f.active_crosswalks) || []);
    if (sc >= 0.18 && state.map.crosswalks) {
      for (const cw of state.map.crosswalks) {
        const [cwid, cx, cy, dx, dy, lanes] = cw;
        const [sx, sy] = toScreen(cx, cy);
        if (sx < -20 || sx > W + 20 || sy < -20 || sy > H + 20) continue;
        const active = activeCwSet.has(cwid);
        const span = Math.max(4.5, (lanes * 3.2 + 2.0) * sc);
        ctx.save();
        ctx.translate(sx, sy);
        ctx.rotate(Math.atan2(-dy, dx));
        ctx.strokeStyle = active ? "rgba(245, 158, 11, 0.92)" : "rgba(20, 83, 45, 0.26)";
        ctx.lineWidth = active ? 2.4 : 1.3;
        ctx.setLineDash([2.2, 2.2]);
        ctx.beginPath();
        ctx.moveTo(0, -span);
        ctx.lineTo(0, span);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.restore();
      }
    }
    const activeRailSet = new Set((f && f.active_rails) || []);
    if (state.map.rail_crossings) {
      for (const rc of state.map.rail_crossings) {
        const [segId, rx, ry, dx, dy] = rc;
        const [sx, sy] = toScreen(rx, ry);
        if (sx < -20 || sx > W + 20 || sy < -20 || sy > H + 20) continue;
        const closed = activeRailSet.has(segId);
        if (closed || sc >= 0.20) {
          ctx.save();
          ctx.translate(sx, sy);
          ctx.rotate(Math.atan2(-dy, dx));
          ctx.strokeStyle = closed ? "#dc2626" : "rgba(71, 85, 105, 0.45)";
          ctx.lineWidth = closed ? 3.2 : 1.6;
          ctx.beginPath();
          ctx.moveTo(0, -9);
          ctx.lineTo(0, 9);
          ctx.stroke();
          ctx.restore();
        }
      }
    }

    // 2. Transit corridors (clipped cleanly at roundabout rims + wrapped counterclockwise around roundabout rings)
    if (!state.map._corridorCache && state.map.seg_geoms) {
      const rbs = (state.map.roundabouts || []).map((rb) => ({
        node: rb[0], x: rb[2], y: rb[3], radM: rb[4], ringLanes: rb[5],
        rRing: rb[5] >= 2 ? (rb[4] - 1.1) : (rb[4] + 0.2),
      }));
      const findRbAt = (pt) => rbs.find((rb) => Math.hypot(pt[0] - rb.x, pt[1] - rb.y) <= 2.0) || null;
      const crossCircle = (ax, ay, bx, by, rb) => {
        const dx = bx - ax, dy = by - ay;
        const fx = ax - rb.x, fy = ay - rb.y;
        const a = dx * dx + dy * dy;
        if (a < 1e-9) return null;
        const b = 2 * (fx * dx + fy * dy);
        const c = fx * fx + fy * fy - rb.rRing * rb.rRing;
        const disc = b * b - 4 * a * c;
        if (disc < 0) return null;
        const sq = Math.sqrt(disc);
        const t1 = (-b - sq) / (2 * a);
        const t2 = (-b + sq) / (2 * a);
        for (const t of [t2, t1]) {
          if (t >= 0 && t <= 1) return [ax + t * dx, ay + t * dy];
        }
        return null;
      };
      const segTrim = {};
      for (const [sid, rawPts] of Object.entries(state.map.seg_geoms)) {
        if (!rawPts || rawPts.length < 2) continue;
        let pts = rawPts;
        let startRb = null, startAng = 0, endRb = null, endAng = 0;
        const rb0 = findRbAt(pts[0]);
        if (rb0) {
          let i = 0;
          while (i < pts.length - 1 && Math.hypot(pts[i + 1][0] - rb0.x, pts[i + 1][1] - rb0.y) < rb0.rRing) i++;
          if (i < pts.length - 1) {
            const hit = crossCircle(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], rb0) || pts[i + 1];
            pts = [hit, ...pts.slice(i + 1)];
            startRb = rb0;
            startAng = Math.atan2(hit[1] - rb0.y, hit[0] - rb0.x);
          }
        }
        const rb1 = findRbAt(pts[pts.length - 1]);
        if (rb1 && pts.length >= 2) {
          let i = pts.length - 1;
          while (i > 0 && Math.hypot(pts[i - 1][0] - rb1.x, pts[i - 1][1] - rb1.y) < rb1.rRing) i--;
          if (i > 0) {
            const hit = crossCircle(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1], rb1) || pts[i - 1];
            pts = [...pts.slice(0, i), hit];
            endRb = rb1;
            endAng = Math.atan2(hit[1] - rb1.y, hit[0] - rb1.x);
          }
        }
        segTrim[sid] = { pts, startRb, startAng, endRb, endAng };
      }
      const routeArcs = (state.map.routes || []).map((r) => {
        const arcs = [];
        const es = r.edges || [];
        for (let i = 0; i < es.length; i++) {
          const cur = segTrim[es[i]];
          const nxt = segTrim[es[(i + 1) % es.length]];
          if (cur && nxt && cur.endRb && nxt.startRb && cur.endRb.node === nxt.startRb.node) {
            arcs.push({ rb: cur.endRb, aIn: cur.endAng, aOut: nxt.startAng });
          }
        }
        return arcs;
      });
      state.map._corridorCache = { segTrim, routeArcs };
    }

    if (state.showRoutes) {
      const { segTrim, routeArcs } = state.map._corridorCache;
      ctx.lineWidth = Math.max(1.2, 2.0 * sc);
      state.map.routes.forEach((r, rIdx) => {
        ctx.strokeStyle = r.backbone
          ? "rgba(22, 163, 74, 0.34)"
          : (r.metropolitan ? "rgba(132, 204, 22, 0.24)" : "rgba(20, 83, 45, 0.20)");
        ctx.beginPath();
        for (const sid of r.edges) {
          const info = segTrim[sid];
          if (!info || info.pts.length < 2) continue;
          info.pts.forEach(([x, y], idx) => {
            const [sx, sy] = toScreen(x, y);
            if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
          });
        }
        for (const arc of routeArcs[rIdx] || []) {
          const [rx, ry] = toScreen(arc.rb.x, arc.rb.y);
          const outerR = Math.max(7.5, (arc.rb.radM + 2.5) * sc);
          const ringW = Math.max(3.5, (arc.rb.ringLanes >= 2 ? 7.2 : 4.6) * sc);
          const rr = outerR - ringW * 0.5;
          const ccwArc = (((arc.aOut - arc.aIn) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
          ctx.moveTo(rx + rr * Math.cos(-arc.aIn), ry + rr * Math.sin(-arc.aIn));
          if (ccwArc < 0.25 || ccwArc > Math.PI * 2 - 0.10) {
            ctx.arc(rx, ry, rr, -arc.aIn, -arc.aIn + Math.PI * 2, false);
          } else {
            ctx.arc(rx, ry, rr, -arc.aIn, -arc.aOut, true);
          }
        }
        ctx.stroke();
      });
    }

    if (f) {
      // 3. Optional bus-inferred congestion heat overlay
      if (state.showHeat && f.heat) {
        const segTrim = state.map._corridorCache ? state.map._corridorCache.segTrim : null;
        for (const [sid, load] of f.heat) {
          const pts = (segTrim && segTrim[sid] && segTrim[sid].pts) || state.map.seg_geoms[sid];
          if (!pts) continue;
          ctx.beginPath();
          pts.forEach(([x, y], i) => {
            const [sx, sy] = toScreen(x, y);
            if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
          });
          ctx.strokeStyle = load > 0.65 ? "rgba(220, 38, 38, 0.72)" : load > 0.35 ? "rgba(245, 158, 11, 0.65)" : "rgba(132, 204, 22, 0.55)";
          ctx.lineWidth = Math.max(3, 5.5 * sc);
          ctx.stroke();
        }
      }
    }

    // 3.5. Landscaped Roundabout Center Islands (drawn above corridor/heat overlays so islands stay pristine)
    if (state.map.roundabouts && state.map.roundabouts.length) {
      for (const rb of state.map.roundabouts) {
        const [, , rx, ry, radM, ringLanes] = rb;
        const [sx, sy] = toScreen(rx, ry);
        if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;
        const outerR = Math.max(7.5, (radM + 2.5) * sc);
        const ringW = Math.max(3.5, (ringLanes >= 2 ? 7.2 : 4.6) * sc);
        const islandR = Math.max(2.6, outerR - ringW);
        ctx.beginPath();
        ctx.arc(sx, sy, islandR, 0, Math.PI * 2);
        ctx.fillStyle = isDark ? "#0e3325" : "#c9e6d2";
        ctx.fill();
        ctx.strokeStyle = isDark ? "rgba(74, 222, 128, 0.42)" : "rgba(20, 83, 45, 0.35)";
        ctx.lineWidth = 1.1;
        ctx.stroke();
      }
    }

    // 3.6. CTP Iași "Rond" Loop Center Islands
    if (state.map.rond_loops && state.map.rond_loops.length) {
      for (const rl of state.map.rond_loops) {
        const [, , lx, ly, radM, lkind] = rl;
        const [sx, sy] = toScreen(lx, ly);
        if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;
        const outerR = Math.max(5.8, (radM + 1.8) * sc);
        const ringW = Math.max(2.8, 4.4 * sc);
        const islandR = Math.max(2.1, outerR - ringW);
        ctx.beginPath();
        ctx.arc(sx, sy, islandR, 0, Math.PI * 2);
        ctx.fillStyle = isDark
          ? (lkind === "tram_bus" ? "#113b29" : "#0e3142")
          : (lkind === "tram_bus" ? "#dcf5e4" : "#e0f2fe");
        ctx.fill();
        ctx.strokeStyle = isDark
          ? (lkind === "tram_bus" ? "rgba(74, 222, 128, 0.55)" : "rgba(56, 189, 248, 0.55)")
          : (lkind === "tram_bus" ? "rgba(21, 128, 61, 0.45)" : "rgba(14, 116, 144, 0.42)");
        ctx.lineWidth = 1.1;
        ctx.stroke();
        if (sc >= 0.28 && islandR >= 5.2) {
          ctx.font = `700 ${Math.min(10, Math.max(7, Math.round(islandR * 1.1)))}px Inter, sans-serif`;
          ctx.fillStyle = isDark
            ? (lkind === "tram_bus" ? "#86efac" : "#7dd3fc")
            : (lkind === "tram_bus" ? "#14532d" : "#0e7490");
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText("R", sx, sy + 0.5);
          ctx.textAlign = "start";
          ctx.textBaseline = "alphabetic";
        }
      }
    }

    if (f) {

      // 4. Signal Stop Lines + Curb Signal Lamp Dots
      if (sc > 0.08 && f.signals) {
        const sigColor = { G: "#16a34a", Y: "#f59e0b", R: "#dc2626" };
        const barW = sc > 0.35 ? 3.4 : 2.2;
        for (const [sid, st] of f.signals) {
          const sl = state.map.stop_lines[sid];
          if (!sl) continue;
          const [ax, ay] = toScreen(sl[0], sl[1]);
          if (ax < -20 || ax > W + 20 || ay < -20 || ay > H + 20) continue;
          const [bx, by] = toScreen(sl[2], sl[3]);
          const col = sigColor[st] || "#16a34a";
          ctx.strokeStyle = isDark ? "rgba(10, 22, 19, 0.85)" : "rgba(255, 255, 255, 0.88)";
          ctx.lineWidth = barW + 2.0;
          ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
          ctx.strokeStyle = col;
          ctx.lineWidth = barW;
          ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
          if (sc >= 0.28) {
            ctx.beginPath();
            ctx.arc(bx, by, sc >= 0.45 ? 2.5 : 2.0, 0, Math.PI * 2);
            ctx.fillStyle = col;
            ctx.fill();
            ctx.strokeStyle = "#0f2a1f";
            ctx.lineWidth = 0.85;
            ctx.stroke();
          }
        }
      }

      // 5. Pulsing Adaptive Signal-Hold Rings (Lime Green!)
      const pulse = 0.5 + 0.5 * Math.sin(performance.now() * 0.006);
      for (const [x, y, kind, age] of f.rings || []) {
        const [sx, sy] = toScreen(x, y);
        const r = 14 + 8 * pulse + 22 * age;
        const alpha = Math.max(0, 0.75 * (1 - age));
        ctx.beginPath();
        ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.fillStyle = kind === "BUS_HOLD" ? `rgba(132, 204, 22, ${alpha * 0.25})` : `rgba(22, 163, 74, ${alpha * 0.2})`;
        ctx.fill();
        ctx.strokeStyle = kind === "BUS_HOLD" ? `rgba(132, 204, 22, ${alpha})` : `rgba(22, 163, 74, ${alpha})`;
        ctx.lineWidth = 2.2;
        ctx.stroke();
      }

      // 6. Rush Hour Marker
      if (f.rush_xy) {
        const [rx, ry] = toScreen(f.rush_xy[0], f.rush_xy[1]);
        const phase = (performance.now() * 0.0008) % 1;
        for (let k = 0; k < 3; k++) {
          const prog = (phase + k / 3) % 1;
          ctx.beginPath();
          ctx.arc(rx, ry, 14 + 44 * prog, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(249, 115, 22, ${(1 - prog) * 0.85})`;
          ctx.lineWidth = 2.2;
          ctx.stroke();
        }
      }

      // 7. Visual Vehicles by Class (0=car, 1=van, 2=truck, 3=ambulance — physics always active!)
      if (state.showCars && f.cars && f.cars.length) {
        const blink = Math.floor(performance.now() / 160) % 2 === 0;
        for (const c of f.cars) {
          const [x, y, dx, dy, cls = 0] = c;
          const [sx, sy] = toScreen(x, y);
          if (sx < -18 || sx > W + 18 || sy < -18 || sy > H + 18) continue;
          ctx.save();
          ctx.translate(sx, sy);
          ctx.rotate(Math.atan2(-dy, dx));
          if (cls === 3) {
            // Emergency Ambulance: Snow White + red/blue flashing beacon halo!
            const al = Math.max(7.5, 6.4 * sc), aw = Math.max(3.8, 2.7 * sc);
            ctx.beginPath();
            ctx.arc(0, 0, Math.max(7.5, 8.5 * sc), 0, Math.PI * 2);
            ctx.fillStyle = blink ? "rgba(239, 68, 68, 0.25)" : "rgba(59, 130, 246, 0.25)";
            ctx.fill();
            ctx.fillStyle = "#ffffff";
            ctx.strokeStyle = "#dc2626";
            ctx.lineWidth = 1.4;
            ctx.fillRect(-al / 2, -aw / 2, al, aw);
            ctx.strokeRect(-al / 2, -aw / 2, al, aw);
            ctx.fillStyle = blink ? "#ef4444" : "#2563eb";
            ctx.fillRect(-1.5, -aw * 0.35, 3, aw * 0.7);
          } else if (cls === 2) {
            // Heavy Truck / TIR
            const tl = Math.max(8.5, 10.6 * sc), tw = Math.max(3.2, 2.7 * sc);
            ctx.fillStyle = isDark ? "#94b8a8" : "#627b6e";
            ctx.fillRect(-tl / 2, -tw / 2, tl, tw);
          } else if (cls === 1) {
            // Delivery Van / SUV
            const vl = Math.max(5.5, 5.8 * sc), vw = Math.max(2.8, 2.4 * sc);
            ctx.fillStyle = isDark ? "#a9c8ba" : "#7e998b";
            ctx.fillRect(-vl / 2, -vw / 2, vl, vw);
          } else {
            // Passenger Car
            const cl = Math.max(4.4, 4.5 * sc), cw = Math.max(2.4, 2.2 * sc);
            ctx.fillStyle = isDark ? "#b8d6c8" : "#9ab0a4";
            ctx.fillRect(-cl / 2, -cw / 2, cl, cw);
          }
          ctx.restore();
        }
      }

      // 7.5. Curbside Double-Parked Friction Vans ("5 min pe avarii" blinking amber hazard lights)
      if (f.friction && f.friction.length) {
        const haz = Math.floor(performance.now() / 320) % 2 === 0;
        for (const [fx, fy] of f.friction) {
          const [sx, sy] = toScreen(fx, fy);
          if (sx < -15 || sx > W + 15 || sy < -15 || sy > H + 15) continue;
          ctx.beginPath();
          ctx.arc(sx, sy, 5.5, 0, Math.PI * 2);
          ctx.fillStyle = haz ? "rgba(245, 158, 11, 0.32)" : "rgba(245, 158, 11, 0.10)";
          ctx.fill();
          ctx.fillStyle = "#d97706";
          ctx.fillRect(sx - 2.6, sy - 2.6, 5.2, 5.2);
        }
      }

      // 8. Incident Marker
      if (f.incident) {
        const [ix, iy] = toScreen(f.incident[0], f.incident[1]);
        ctx.beginPath();
        ctx.arc(ix, iy, 12, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(220, 38, 38, 0.18)";
        ctx.fill();
        ctx.strokeStyle = "#dc2626";
        ctx.lineWidth = 2.5;
        ctx.stroke();
      }

      // 9. Buses (articulated 16m, standard 12m, minibus 8.5m; boarding ring at CTP stops; soft Sage at terminus)
      let hoveredBus = null;
      const chipGrid = new Set();

      for (const b of f.buses || []) {
        const [id, line, x, y, dx, dy, spdRatio, status, restMin, atStop = 0, bLenM = 11.5] = b;
        const [sx, sy] = toScreen(x, y);
        if (sx < -25 || sx > W + 25 || sy < -25 || sy > H + 25) continue;

        const busHitR = state.isTouchInspect ? 24 : 14;
        if (state.hoverMouse.x >= 0 && Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) < busHitR) {
          hoveredBus = b;
        }

        if (status !== "DRIVING") {
          ctx.beginPath();
          ctx.arc(sx, sy, 5.5, 0, Math.PI * 2);
          ctx.fillStyle = status === "DRIVER_BREAK" ? "#e0f2fe" : "#e6f7d2";
          ctx.fill();
          ctx.strokeStyle = status === "DRIVER_BREAK" ? "#0284c7" : "#14532d";
          ctx.lineWidth = 1.6;
          ctx.stroke();
          continue;
        }

        const bl = Math.max(bLenM >= 14 ? 13.5 : (bLenM <= 9 ? 9.5 : 11), bLenM * sc);
        const bw = Math.max(5.5, 3.2 * sc);

        if (atStop) {
          ctx.beginPath();
          ctx.arc(sx, sy, 9.5, 0, Math.PI * 2);
          ctx.strokeStyle = "rgba(22, 163, 74, 0.55)";
          ctx.lineWidth = 1.6;
          ctx.stroke();
        }

        // Driving or boarding bus body
        const col = atStop ? "#15803d" : (spdRatio > 0.65 ? "#16a34a" : spdRatio > 0.3 ? "#84cc16" : "#f59e0b");
        ctx.save();
        ctx.translate(sx, sy);
        ctx.rotate(Math.atan2(-dy, dx));
        ctx.fillStyle = col;
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.roundRect(-bl / 2, -bw / 2, bl, bw, 2.5);
        ctx.fill();
        ctx.stroke();
        ctx.restore();

        const cellW = isMobile() ? (sc < 0.08 ? 58 : 44) : 38;
        const cellH = isMobile() ? (sc < 0.08 ? 30 : 24) : 22;
        const cell = `${Math.floor(sx / cellW)}:${Math.floor(sy / cellH)}`;
        if (line && !chipGrid.has(cell)) {
          chipGrid.add(cell);
          ctx.font = "700 10px Inter, sans-serif";
          const tw = ctx.measureText(line).width + 8;
          ctx.fillStyle = "#14532d";
          ctx.beginPath();
          ctx.roundRect(sx + 6, sy - 14, tw, 15, 4);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.fillText(line, sx + 10, sy - 3);
        }
      }

      // Hover / tap tooltip (bus -> roundabout -> rond loop -> junction -> building), clamped inside viewport bounds
      const tip = $("mapTooltip");
      let tipText = "";
      if (hoveredBus) {
        const [id, line, , , , , spdRatio, status, restMin, atStop = 0] = hoveredBus;
        tipText = status === "DRIVING"
          ? (atStop ? `Line ${line} (${id}) · Boarding Passengers at CTP Stop` : `Line ${line} (${id}) · Driving · ${Math.round(spdRatio * 50)} km/h`)
          : `Line ${line} (${id}) · ${status === "DRIVER_BREAK" ? "Driver Break" : "Terminus Layover"} (${restMin}m left)`;
      } else if (hoveredRoundabout) {
        const [, rname, , , , rlanes, isSig] = hoveredRoundabout;
        tipText = `${rname} · ${rlanes}-Lane ${isSig ? "Signalized Roundabout" : "Yield-to-Left (HG 130/2023)"}`;
      } else if (hoveredRondLoop) {
        const [, lname, , , , lkind, llines] = hoveredRondLoop;
        const lType = lkind === "tram_bus" ? "CTP Tram & Bus Loop" : "CTP Bus Loop";
        const lList = llines && llines.length ? ` · Lines ${llines.slice(0, 6).join(", ")}${llines.length > 6 ? "…" : ""}` : "";
        tipText = `${lname} · ${lType}${lList}`;
      } else if (hoveredJunction) {
        const [, jname, , , jkind, , , jarms] = hoveredJunction;
        const jLabel = jkind === "signalized" ? "Signalized Junction" : (jkind === "yield" ? "Yield Junction (Cedează)" : "Road Junction");
        tipText = `${jname} · ${jLabel}${jarms ? ` (${jarms} arms)` : ""}`;
      } else if (hoveredBuilding) {
        const [bid, isApt, isCity] = hoveredBuilding;
        tipText = `${isApt ? "Apartment Block" : "House"} #${bid} · ${isCity ? "Iași City Core" : "Metropolitan / Outer"}`;
      }
      if (tipText) {
        tip.textContent = tipText;
        tip.classList.remove("hidden");
        const tw = tip.offsetWidth || 180;
        const th = tip.offsetHeight || 30;
        const pad = 10;
        const maxBottom = isMobile() ? (H - 96) : (H - pad);
        let lx = state.hoverMouse.x + 14;
        if (lx + tw > W - pad) lx = Math.max(pad, state.hoverMouse.x - tw - 14);
        if (lx + tw > W - pad) lx = Math.max(pad, W - tw - pad);
        let ty = state.hoverMouse.y - th - 12;
        if (ty < 50) ty = Math.min(maxBottom - th, state.hoverMouse.y + 18);
        ty = Math.max(pad, Math.min(maxBottom - th, ty));
        tip.style.left = `${Math.round(lx)}px`;
        tip.style.top = `${Math.round(ty)}px`;
      } else {
        tip.classList.add("hidden");
      }

      // 9.5. Subtle Weather Atmospheric Overlay (Rain streaks / Winter snow)
      if (weather === "rain") {
        const tms = performance.now() * 0.25;
        ctx.strokeStyle = "rgba(59, 130, 168, 0.18)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let i = 0; i < 65; i++) {
          const rx = ((i * 137 + tms * 0.35) % (W + 40)) - 20;
          const ry = ((i * 89 + tms * 1.4) % (H + 40)) - 20;
          ctx.moveTo(rx, ry);
          ctx.lineTo(rx - 4, ry + 13);
        }
        ctx.stroke();
      } else if (weather === "snow") {
        const tms = performance.now() * 0.04;
        ctx.fillStyle = "rgba(255, 255, 255, 0.78)";
        for (let i = 0; i < 75; i++) {
          const rx = ((i * 151 + Math.sin(tms * 0.05 + i) * 28) % W + W) % W;
          const ry = ((i * 97 + tms * 0.9) % H + H) % H;
          ctx.beginPath();
          ctx.arc(rx, ry, (i % 2) + 1.4, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    // 10. Landmarks (clean civic map labels with overlap avoidance on compact viewports)
    const mobileView = isMobile();
    ctx.font = mobileView ? "600 10.5px Inter, sans-serif" : "600 11.5px Inter, sans-serif";
    const placedLabels = [];
    for (const lm of state.map.landmarks) {
      const [sx, sy] = toScreen(lm.x, lm.y);
      if (sx < -60 || sx > W + 60 || sy < -30 || sy > H + 30) continue;
      const tw = ctx.measureText(lm.name).width;
      const padX = mobileView ? 5 : 7;
      const boxW = tw + padX * 2;
      const boxH = mobileView ? 18 : 20;
      const bx0 = sx - boxW / 2;
      const by0 = sy - boxH / 2;
      if (mobileView && sc < 0.14) {
        const margin = 4;
        const overlaps = placedLabels.some(
          ([x0, y0, x1, y1]) => bx0 - margin < x1 && bx0 + boxW + margin > x0 && by0 - margin < y1 && by0 + boxH + margin > y0
        );
        if (overlaps) continue;
        placedLabels.push([bx0, by0, bx0 + boxW, by0 + boxH]);
      }
      ctx.fillStyle = isDark ? "rgba(12, 28, 23, 0.90)" : "rgba(255, 255, 255, 0.90)";
      ctx.beginPath();
      ctx.roundRect(bx0, by0, boxW, boxH, 6);
      ctx.fill();
      ctx.strokeStyle = isDark ? "rgba(134, 239, 172, 0.25)" : "rgba(20, 83, 45, 0.16)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = isDark ? "#e6f5ee" : "#0f2a1f";
      ctx.fillText(lm.name, sx - tw / 2, sy + 3.5);
    }
  }

  // ==================== EVENT BINDINGS ====================
  function bindEvents() {
    const onViewportResize = () => {
      resizeCanvas();
      const nowMobile = isMobile();
      if (nowMobile !== state.wasMobile) {
        state.wasMobile = nowMobile;
        if (nowMobile) {
          setMobileSnap("peek", true);
        } else {
          state.drawerCollapsed = false;
          const drawer = $("drawer");
          drawer.style.height = "";
          drawer.style.transform = "";
          drawer.classList.remove("collapsed", "dragging");
          if (workspaceEl) workspaceEl.classList.remove("sheet-half", "sheet-full");
        }
        if (state.activeSlotMeta) {
          $("slotBtnLabel").textContent = nowMobile
            ? state.activeSlotMeta.name
            : `Slot: ${state.activeSlotMeta.name} (${state.activeSlotMeta.episodes} ep)`;
        }
        if (state.frame) updateUI(state.frame);
        fitMap();
      }
    };
    window.addEventListener("resize", onViewportResize);
    window.addEventListener("orientationchange", () => setTimeout(() => { resizeCanvas(); fitMap(); }, 180));

    // Desktop Mouse Pan & Zoom
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.18 : 0.85);
    }, { passive: false });

    canvas.addEventListener("mousedown", (e) => {
      state.isTouchInspect = false;
      state.dragging = true;
      state.lastMouse = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener("mouseup", () => { state.dragging = false; });
    canvas.addEventListener("mousemove", (e) => {
      state.isTouchInspect = false;
      const rect = canvas.getBoundingClientRect();
      state.hoverMouse = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      if (!state.dragging) return;
      const dx = e.clientX - state.lastMouse.x;
      const dy = e.clientY - state.lastMouse.y;
      state.lastMouse = { x: e.clientX, y: e.clientY };
      state.camera.cx -= dx / state.camera.scale;
      state.camera.cy += dy / state.camera.scale;
    });
    canvas.addEventListener("mouseleave", () => { state.hoverMouse = { x: -1, y: -1 }; });

    // Mobile Multi-Touch Map Navigation (1-finger pan, 2-finger pinch-zoom + pan, double-tap zoom, tap inspect)
    let tLast = null;
    let tStart = null;
    let tMoved = false;
    let pinchDist = 0;
    let pinchMid = null;
    let lastTap = { time: 0, x: 0, y: 0 };

    const touchDist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    const touchMid = (a, b, rect) => ({
      x: (a.clientX + b.clientX) * 0.5 - rect.left,
      y: (a.clientY + b.clientY) * 0.5 - rect.top,
    });

    canvas.addEventListener("touchstart", (e) => {
      if (e.touches.length === 0) return;
      e.preventDefault();
      $("slotDropdown").classList.add("hidden");
      $("statTooltip").classList.add("hidden");
      const rect = canvas.getBoundingClientRect();
      if (e.touches.length === 1) {
        const t = e.touches[0];
        tLast = { x: t.clientX, y: t.clientY };
        tStart = { x: t.clientX, y: t.clientY, cx: t.clientX - rect.left, cy: t.clientY - rect.top };
        tMoved = false;
        pinchDist = 0;
        pinchMid = null;
      } else if (e.touches.length >= 2) {
        tMoved = true;
        state.hoverMouse = { x: -1, y: -1 };
        pinchDist = touchDist(e.touches[0], e.touches[1]);
        pinchMid = touchMid(e.touches[0], e.touches[1], rect);
      }
    }, { passive: false });

    canvas.addEventListener("touchmove", (e) => {
      if (e.touches.length === 0) return;
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      if (e.touches.length === 1 && tLast) {
        const t = e.touches[0];
        const dx = t.clientX - tLast.x;
        const dy = t.clientY - tLast.y;
        tLast = { x: t.clientX, y: t.clientY };
        if (tStart && Math.hypot(t.clientX - tStart.x, t.clientY - tStart.y) > 7) {
          tMoved = true;
          state.hoverMouse = { x: -1, y: -1 };
        }
        state.camera.cx -= dx / state.camera.scale;
        state.camera.cy += dy / state.camera.scale;
      } else if (e.touches.length >= 2) {
        const newDist = touchDist(e.touches[0], e.touches[1]);
        const newMid = touchMid(e.touches[0], e.touches[1], rect);
        if (pinchDist > 8 && pinchMid) {
          state.camera.cx -= (newMid.x - pinchMid.x) / state.camera.scale;
          state.camera.cy += (newMid.y - pinchMid.y) / state.camera.scale;
          const factor = Math.max(0.75, Math.min(1.35, newDist / pinchDist));
          zoomAt(newMid.x, newMid.y, factor);
        }
        pinchDist = newDist;
        pinchMid = newMid;
        tMoved = true;
      }
    }, { passive: false });

    canvas.addEventListener("touchend", (e) => {
      if (e.touches.length === 1) {
        // Smooth handoff from 2-finger pinch to 1-finger pan without jumping
        tLast = { x: e.touches[0].clientX, y: e.touches[0].clientY };
        pinchDist = 0;
        pinchMid = null;
        tMoved = true;
        return;
      }
      if (e.touches.length === 0) {
        if (!tMoved && tStart) {
          const now = Date.now();
          if (now - lastTap.time < 300 && Math.hypot(tStart.cx - lastTap.x, tStart.cy - lastTap.y) < 32) {
            // Double-tap zooms cleanly into tapped location
            zoomAt(tStart.cx, tStart.cy, 1.85);
            state.hoverMouse = { x: -1, y: -1 };
            lastTap.time = 0;
          } else {
            // Single-tap inspects nearest bus / roundabout / building (or clears tooltip on empty map)
            state.isTouchInspect = true;
            state.hoverMouse = { x: tStart.cx, y: tStart.cy };
            lastTap = { time: now, x: tStart.cx, y: tStart.cy };
          }
        }
        tLast = null;
        tStart = null;
        pinchDist = 0;
        pinchMid = null;
      }
    });

    canvas.addEventListener("touchcancel", () => {
      tLast = null;
      tStart = null;
      pinchDist = 0;
      pinchMid = null;
    });

    $("btnZoomIn").onclick = () => { const r = canvas.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.28); };
    $("btnZoomOut").onclick = () => { const r = canvas.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 0.78); };
    $("btnFitMap").onclick = fitMap;
    $("btnHeat").onclick = () => { state.showHeat = !state.showHeat; $("btnHeat").classList.toggle("active", state.showHeat); };
    $("btnRoutes").onclick = () => { state.showRoutes = !state.showRoutes; $("btnRoutes").classList.toggle("active", state.showRoutes); };
    $("btnShowCars").onclick = () => { state.showCars = !state.showCars; $("btnShowCars").classList.toggle("active", state.showCars); };
    $("btnBuildings").onclick = () => { state.showBuildings = !state.showBuildings; $("btnBuildings").classList.toggle("active", state.showBuildings); };

    // Compact structured hover tooltips for every Executive Drawer metric
    const STAT_TIPS = {
      delay_reduced: {
        title: "Bus Delay Reduced",
        what: "Share of transit waiting time eliminated compared to standard fixed traffic-light timers.",
        how: "Compares live bus travel-time loss against Iași's measured fixed-timer baseline.",
        impact: "Faster, more punctual CTP schedules without adding new buses or widening roads.",
      },
      predicted_flow: {
        title: "Predicted Traffic Flow",
        what: "Estimated arterial movement speed across the city (100% = free-flowing traffic).",
        how: "Inferred strictly from live bus GPS telemetry — Noodle never requires street cameras or car sensors.",
        impact: "Proves Noodle sees city-wide congestion using only existing transit AVL data.",
      },
      ai_precision: {
        title: "AI Decision Precision",
        what: "Percentage of green-light holds that were decisive (the bus cleared the junction only thanks to the extra green).",
        how: "Self-graded after every yellow light using bus odometer history; failed holds lower that junction's trust.",
        impact: "Prevents wasted green time so side-street car traffic keeps moving smoothly.",
      },
      municipal_bars: {
        title: "Adaptive AI vs. Fixed Baseline",
        what: "Side-by-side transit efficiency comparison between Noodle AI and traditional fixed timers.",
        how: "Evaluated on paired identical traffic seeds across all 51 CTP Iași lines.",
        impact: "Higher bar = smoother corridor flow and lower passenger waiting time.",
      },
      pax_total: {
        title: "All Passengers Saved (City-Wide)",
        what: "Estimated combined hours returned every day across all CTP Iași bus riders.",
        how: "Active driving buses × ~28 avg riders × 16 service hrs/day × share of ride time saved.",
        impact: "Quantifies total daily economic & productivity gain for the entire municipality.",
      },
      pax_per_rider: {
        title: "Time Saved Per Passenger",
        what: "Average daily hours (and minutes) saved by one regular CTP bus commuter.",
        how: "Based on an average ~1.0 hr/day round-trip bus commute × measured travel-time reduction.",
        impact: "Adds up to ~10–20 hours saved per commuter every year — directly boosting ridership.",
      },
      co2_cut: {
        title: "Estimated Idle CO₂ Cut",
        what: "Projected reduction in stop-and-go bus and corridor idling emissions.",
        how: "Modeled proportionally from fewer red-light stops and reduced diesel/queue idling.",
        impact: "Supports EU Green Deal & PMUD Iași sustainable urban mobility targets.",
      },
      fleet_status: {
        title: "CTP Iași Fleet Status (51 Lines)",
        what: "Real-time breakdown of buses actively driving vs. resting at route termini.",
        how: "Buses take realistic 5–15 min terminal layovers and 15–30 min driver breaks every 3–5 stops (never despawning).",
        impact: "Resting buses are excluded from congestion sensors so parked vehicles never trigger false green holds.",
      },
      car_traffic_load: {
        title: "Car Traffic Load (Cars Only)",
        what: "Scales the volume of private cars in Iași traffic (100% ≈ 1,500 simultaneous moving cars on the arterial core).",
        how: "Changes only car count & entry rate — the 51-line CTP bus fleet remains fixed to its real schedule.",
        impact: "Lets you stress-test how Noodle protects buses during light traffic or severe rush hour.",
      },
    };

    const tipEl = $("statTooltip");
    const showStatTip = (el) => {
      const cfg = STAT_TIPS[el.dataset.tip];
      if (!cfg) return;
      $("stTitle").textContent = cfg.title;
      $("stWhat").textContent = cfg.what;
      $("stHow").textContent = cfg.how;
      $("stImpact").textContent = cfg.impact;
      tipEl.classList.remove("hidden");
      if (!isMobile()) {
        const r = el.getBoundingClientRect();
        const th = tipEl.offsetHeight || 130;
        const left = Math.max(12, r.left - 288);
        const top = Math.max(66, Math.min(window.innerHeight - th - 14, r.top + (r.height - th) / 2));
        tipEl.style.left = `${left}px`;
        tipEl.style.top = `${top}px`;
      }
    };

    document.querySelectorAll(".tip-target").forEach((el) => {
      el.addEventListener("mouseenter", () => {
        if (!isMobile()) showStatTip(el);
      });
      el.addEventListener("mouseleave", () => {
        if (!isMobile()) tipEl.classList.add("hidden");
      });
      el.addEventListener("click", (e) => {
        if (!isMobile()) return;
        if (e.target.closest("input, select, button, .slider-row")) return;
        e.stopPropagation();
        if (!tipEl.classList.contains("hidden") && tipEl.dataset.activeKey === el.dataset.tip) {
          tipEl.classList.add("hidden");
          tipEl.dataset.activeKey = "";
        } else {
          tipEl.dataset.activeKey = el.dataset.tip;
          showStatTip(el);
        }
      });
    });

    const btnCloseTip = $("btnCloseStatTip");
    if (btnCloseTip) {
      btnCloseTip.onclick = (e) => {
        e.stopPropagation();
        tipEl.classList.add("hidden");
        tipEl.dataset.activeKey = "";
      };
    }

    // Collapsible Right Drawer (Desktop) & Swipeable Bottom Sheet (Mobile)
    const toggleDrawer = () => {
      if (isMobile()) {
        setMobileSnap(state.mobileSnap === "peek" ? "half" : "peek");
        return;
      }
      state.drawerCollapsed = !state.drawerCollapsed;
      $("drawer").classList.toggle("collapsed", state.drawerCollapsed);
    };
    $("btnDrawerTab").onclick = toggleDrawer;
    $("btnToggleDrawer").onclick = toggleDrawer;
    const btnTheme = $("btnThemeToggle");
    if (btnTheme) {
      btnTheme.onclick = () => applyTheme(state.theme === "dark" ? "light" : "dark");
    }

    // Mobile Bottom Sheet Buttons & Section Tabs
    $("btnPeekImpact").onclick = () => {
      if (state.mobileSnap !== "peek" && state.mobileTab === "impact") {
        setMobileSnap("peek");
      } else {
        setMobileTab("impact");
        setMobileSnap("full");
      }
    };
    $("btnPeekControls").onclick = () => {
      if (state.mobileSnap === "peek") {
        setMobileTab("controls");
        setMobileSnap("half");
      } else {
        setMobileSnap("peek");
      }
    };
    $("btnPeekPause").onclick = (e) => {
      e.stopPropagation();
      sendCmd({ cmd: "set_paused", paused: !(state.frame && state.frame.paused) });
    };
    $("tabMobileControls").onclick = () => {
      setMobileTab("controls");
      if (state.mobileSnap === "peek") setMobileSnap("half");
    };
    $("tabMobileImpact").onclick = () => {
      setMobileTab("impact");
      if (state.mobileSnap === "peek") setMobileSnap("full");
    };

    // Drag-to-Resize on Mobile Bottom Sheet (works via touch OR pointer/mouse anywhere on #sheetHeader, plus pull-down from top of .drawer-inner)
    const sheetHeader = $("sheetHeader");
    const drawerEl = $("drawer");
    let sheetDrag = null;
    let suppressClickUntil = 0;
    let lastTouchMs = 0;

    const snapHeights = () => {
      const vh = window.innerHeight || 700;
      const peekH = 68;
      const halfH = Math.round(vh * 0.58);
      const fullH = Math.min(Math.round(vh * 0.84), vh - 58);
      return { peek: peekH, half: halfH, full: fullH };
    };

    const syncMapControlsDuringDrag = (h, hs) => {
      if (!mapControlsEl) return;
      if (h <= hs.half) {
        mapControlsEl.style.bottom = `${Math.round(h + 10)}px`;
        mapControlsEl.style.opacity = "1";
        mapControlsEl.style.pointerEvents = "";
      } else {
        const span = Math.max(40, hs.full - hs.half);
        const fade = Math.max(0, 1 - (h - hs.half) / span);
        mapControlsEl.style.bottom = `${Math.round(hs.half + 10)}px`;
        mapControlsEl.style.opacity = fade.toFixed(2);
        mapControlsEl.style.pointerEvents = fade < 0.2 ? "none" : "";
      }
    };

    const beginSheetDrag = (x, y, startedOnButton, fromInner = false) => {
      const hs = snapHeights();
      sheetDrag = {
        startX: x,
        startY: y,
        startH: drawerEl.offsetHeight || hs[state.mobileSnap] || hs.peek,
        lastY: y,
        lastT: Date.now(),
        vel: 0,
        moved: false,
        cancelled: false,
        startedOnButton,
        fromInner,
      };
    };

    const moveSheetDrag = (x, y, ev) => {
      if (!sheetDrag || sheetDrag.cancelled) return;
      const dx = x - sheetDrag.startX;
      const dy = y - sheetDrag.startY;
      if (!sheetDrag.moved) {
        if (sheetDrag.fromInner) {
          // Only hijack inner scroll when user pulls down at scrollTop <= 0
          if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) { sheetDrag.cancelled = true; return; }
          if (dy < -6 || (drawerInnerEl && drawerInnerEl.scrollTop > 0)) { sheetDrag.cancelled = true; return; }
          if (dy <= 10) return;
        } else {
          if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.2) { sheetDrag.cancelled = true; return; }
          if (Math.abs(dy) <= 8 || Math.abs(dy) < Math.abs(dx)) return;
        }
        sheetDrag.moved = true;
        drawerEl.classList.add("dragging");
        if (workspaceEl) workspaceEl.classList.add("sheet-dragging");
      }
      if (ev && ev.cancelable) ev.preventDefault();
      const now = Date.now();
      const dt = Math.max(1, now - sheetDrag.lastT);
      sheetDrag.vel = (y - sheetDrag.lastY) / dt;
      sheetDrag.lastY = y;
      sheetDrag.lastT = now;
      const hs = snapHeights();
      const nextH = Math.max(hs.peek, Math.min(hs.full, sheetDrag.startH - dy));
      drawerEl.style.height = `${Math.round(nextH)}px`;
      syncMapControlsDuringDrag(nextH, hs);
    };

    const finishSheetDrag = () => {
      if (!sheetDrag) return;
      const drag = sheetDrag;
      sheetDrag = null;
      drawerEl.classList.remove("dragging");
      if (workspaceEl) workspaceEl.classList.remove("sheet-dragging");
      if (drag.cancelled) return;
      if (!drag.moved) {
        // Let button taps trigger their own onclick; tap on non-button drag zone toggles peek/half
        if (!drag.startedOnButton && !drag.fromInner) {
          const next = state.mobileSnap === "peek" ? "half" : (state.mobileSnap === "half" ? "full" : "peek");
          setMobileSnap(next);
        }
        return;
      }
      suppressClickUntil = Date.now() + 350;
      const hs = snapHeights();
      const curH = Math.max(hs.peek, Math.min(hs.full, drag.startH - (drag.lastY - drag.startY)));
      if (drag.vel < -0.35) {
        setMobileSnap(curH > (hs.half + hs.full) * 0.5 || state.mobileSnap === "half" ? "full" : "half");
      } else if (drag.vel > 0.35) {
        setMobileSnap(curH < (hs.peek + hs.half) * 0.5 || state.mobileSnap === "half" ? "peek" : "half");
      } else {
        const choices = [
          ["full", Math.abs(curH - hs.full)],
          ["half", Math.abs(curH - hs.half)],
          ["peek", Math.abs(curH - hs.peek)],
        ];
        choices.sort((a, b) => a[1] - b[1]);
        setMobileSnap(choices[0][0]);
      }
    };

    // Suppress trailing synthetic click after a finger drag started on a header button/tab
    sheetHeader.addEventListener("click", (e) => {
      if (Date.now() < suppressClickUntil) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);

    sheetHeader.addEventListener("touchstart", (e) => {
      if (!isMobile() || e.touches.length !== 1) return;
      lastTouchMs = Date.now();
      const t = e.touches[0];
      beginSheetDrag(t.clientX, t.clientY, !!e.target.closest("button"), false);
    }, { passive: true });
    sheetHeader.addEventListener("touchmove", (e) => {
      if (!sheetDrag || e.touches.length !== 1) return;
      moveSheetDrag(e.touches[0].clientX, e.touches[0].clientY, e);
    }, { passive: false });
    sheetHeader.addEventListener("touchend", finishSheetDrag);
    sheetHeader.addEventListener("touchcancel", finishSheetDrag);

    // Mouse drag fallback on #sheetHeader for mobile emulation & touch-capable laptops
    sheetHeader.addEventListener("mousedown", (e) => {
      if (!isMobile() || e.button !== 0 || Date.now() - lastTouchMs < 500) return;
      beginSheetDrag(e.clientX, e.clientY, !!e.target.closest("button"), false);
    });
    window.addEventListener("mousemove", (e) => {
      if (sheetDrag) moveSheetDrag(e.clientX, e.clientY, e);
    });
    window.addEventListener("mouseup", () => {
      if (sheetDrag) finishSheetDrag();
    });

    // Pull down on .drawer-inner when scrolled to top to collapse half/full sheet naturally
    if (drawerInnerEl) {
      drawerInnerEl.addEventListener("touchstart", (e) => {
        if (!isMobile() || state.mobileSnap === "peek" || !e.touches || e.touches.length !== 1) return;
        if (e.target.closest("input, select, option, .slider-row")) return;
        if (drawerInnerEl.scrollTop <= 0) {
          lastTouchMs = Date.now();
          beginSheetDrag(e.touches[0].clientX, e.touches[0].clientY, !!e.target.closest("button"), true);
        }
      }, { passive: true });
      drawerInnerEl.addEventListener("touchmove", (e) => {
        if (!sheetDrag || !sheetDrag.fromInner || e.touches.length !== 1) return;
        moveSheetDrag(e.touches[0].clientX, e.touches[0].clientY, e);
      }, { passive: false });
      drawerInnerEl.addEventListener("touchend", () => {
        if (sheetDrag && sheetDrag.fromInner) finishSheetDrag();
      });
      drawerInnerEl.addEventListener("touchcancel", () => {
        if (sheetDrag && sheetDrag.fromInner) finishSheetDrag();
      });
    }

    // Mode Switcher: Aether (Simulation) | Terra (Real)
    const switchMode = async (mode) => {
      if (state.backendOnline) {
        await apiFetch("/api/mode", { method: "POST", body: JSON.stringify({ mode }) });
      }
      sendCmd({ cmd: "set_mode", mode });
    };
    $("btnModeAether").onclick = () => switchMode("aether");
    $("btnModeTerra").onclick = () => switchMode("terra");

    // Slot Dropdown & New Slot Modal
    $("btnSlotMenu").onclick = (e) => {
      e.stopPropagation();
      $("slotDropdown").classList.toggle("hidden");
    };
    document.addEventListener("click", (e) => {
      if (!$("slotDropdown").contains(e.target) && !$("btnSlotMenu").contains(e.target)) {
        $("slotDropdown").classList.add("hidden");
      }
      if (isMobile() && !$("statTooltip").contains(e.target) && !e.target.closest(".tip-target")) {
        $("statTooltip").classList.add("hidden");
        $("statTooltip").dataset.activeKey = "";
      }
    });

    $("btnResetActiveSlot").onclick = async () => {
      $("slotDropdown").classList.add("hidden");
      const active = (state.frame && state.frame.active_slot) || "default";
      if (state.backendOnline) await apiFetch("/api/slots/reset", { method: "POST", body: JSON.stringify({ slot: active }) });
      else {
        state.standalone.resetSlot(active);
        state.frame = state.standalone.getFrame();
        updateUI(state.frame);
      }
      await refreshSlots();
    };

    const openNewSlotModal = () => {
      $("slotDropdown").classList.add("hidden");
      $("inputNewSlotName").value = "";
      $("newSlotError").classList.add("hidden");
      $("newSlotModal").classList.remove("hidden");
      $("inputNewSlotName").focus();
    };
    $("btnNewSlot").onclick = openNewSlotModal;
    const btnNewMobile = $("btnNewSlotMobile");
    if (btnNewMobile) btnNewMobile.onclick = openNewSlotModal;

    $("btnConfirmNewSlot").onclick = async () => {
      const name = $("inputNewSlotName").value.trim();
      try {
        if (state.backendOnline) await apiFetch("/api/slots/select", { method: "POST", body: JSON.stringify({ slot: name, clean: true }) });
        else {
          state.standalone.selectSlot(name, true);
          state.frame = state.standalone.getFrame();
          updateUI(state.frame);
        }
        $("newSlotModal").classList.add("hidden");
        await refreshSlots();
      } catch (err) {
        $("newSlotError").textContent = err.message;
        $("newSlotError").classList.remove("hidden");
      }
    };

    // API Key Modal
    const openApiModal = async () => {
      $("apiTestMsg").classList.add("hidden");
      $("inputApiKey").value = "";
      await refreshCredentialsBadge();
      $("apiKeyModal").classList.remove("hidden");
      $("inputApiKey").focus();
    };
    $("btnApiKey").onclick = openApiModal;
    $("btnOpenApiModal2").onclick = openApiModal;

    $("btnSaveApiKey").onclick = async () => {
      const key = $("inputApiKey").value.trim();
      const agency = $("inputAgencyId").value.trim() || "1";
      const msgEl = $("apiTestMsg");
      msgEl.className = "field-msg";
      msgEl.textContent = "Testing connection to api.tranzy.ai…";
      msgEl.classList.remove("hidden");
      try {
        const res = state.backendOnline
          ? await apiFetch("/api/credentials", { method: "POST", body: JSON.stringify({ api_key: key, agency_id: agency, test: true }) })
          : await state.standalone.saveCredentials(key, agency);
        msgEl.textContent = res.message || (res.ok ? "Connected!" : "Connection failed.");
        msgEl.classList.toggle("error", !res.ok);
        await refreshCredentialsBadge();
      } catch (err) {
        msgEl.textContent = err.message;
        msgEl.classList.add("error");
      }
    };

    // Sync Fleet Counts from Terra -> Aether
    $("btnSyncFleet").onclick = async () => {
      const res = state.backendOnline
        ? await apiFetch("/api/sync-fleet", { method: "POST" })
        : state.standalone.syncFleetFromTerra();
      $("terraStatusBox").textContent = res.synced
        ? `Synced ${res.routes_updated} live CTP route counts into Aether (Simulation)!`
        : (res.message || "Connect to Terra first to read live fleet counts.");
    };

    // Close modals (buttons & backdrop tap)
    document.querySelectorAll("[data-close]").forEach((btn) => {
      btn.onclick = () => $(btn.dataset.close).classList.add("hidden");
    });
    document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) backdrop.classList.add("hidden");
      });
    });

    // Aether Quick Controls
    $("btnCtrlAdaptive").onclick = () => sendCmd({ cmd: "set_adaptive", adaptive: true });
    $("btnCtrlFixed").onclick = () => sendCmd({ cmd: "set_adaptive", adaptive: false });
    const trafficSlider = $("trafficSlider");
    let trafficSendTimer = null;
    const commitTraffic = (rawVal, immediate = false) => {
      const val = Math.min(300, Math.max(0, Math.round(Number(rawVal) / 10) * 10));
      $("trafficVal").textContent = `${val}%`;
      updateTrafficSliderTrack(val);
      state.trafficSliderPendingVal = val;
      state.trafficSliderHoldUntil = performance.now() + 800;
      const dispatch = () => {
        trafficSendTimer = null;
        if (state.trafficSliderLastSent === val && !immediate) return;
        state.trafficSliderLastSent = val;
        sendCmd({ cmd: "set_traffic", percent: val });
      };
      if (immediate) {
        if (trafficSendTimer) {
          clearTimeout(trafficSendTimer);
          trafficSendTimer = null;
        }
        dispatch();
      } else if (!trafficSendTimer) {
        trafficSendTimer = setTimeout(dispatch, 90);
      }
    };
    const startTrafficDrag = () => {
      state.trafficSliderDragging = true;
      state.trafficSliderHoldUntil = performance.now() + 800;
    };
    const endTrafficDrag = () => {
      if (!state.trafficSliderDragging) return;
      state.trafficSliderDragging = false;
      commitTraffic(trafficSlider.value, true);
    };
    trafficSlider.addEventListener("pointerdown", startTrafficDrag);
    trafficSlider.addEventListener("mousedown", startTrafficDrag);
    trafficSlider.addEventListener("touchstart", startTrafficDrag, { passive: true });
    window.addEventListener("pointerup", endTrafficDrag);
    window.addEventListener("pointercancel", endTrafficDrag);
    window.addEventListener("mouseup", endTrafficDrag);
    window.addEventListener("touchend", endTrafficDrag, { passive: true });
    window.addEventListener("touchcancel", endTrafficDrag, { passive: true });
    trafficSlider.oninput = (e) => commitTraffic(e.target.value, false);
    trafficSlider.onchange = (e) => {
      state.trafficSliderDragging = false;
      commitTraffic(e.target.value, true);
    };
    updateTrafficSliderTrack(trafficSlider.value);

    $("rushSelect").onchange = (e) => sendCmd({ cmd: "set_rush", rush_point: e.target.value || null });
    $("btnPlayPause").onclick = () => sendCmd({ cmd: "set_paused", paused: !(state.frame && state.frame.paused) });
    $("btnSpeed").onclick = () => {
      state.speedIdx = (state.speedIdx + 1) % state.speeds.length;
      sendCmd({ cmd: "set_speed", speed: state.speeds[state.speedIdx] });
    };
    $("btnIncident").onclick = () => sendCmd({ cmd: "trigger_incident" });
    $("btnResetSim").onclick = () => {
      state.trafficSliderDragging = false;
      state.trafficSliderPendingVal = null;
      state.trafficSliderHoldUntil = 0;
      state.trafficSliderLastSent = null;
      sendCmd({ cmd: "reset_sim" });
    };

    // Realism & Scenarios Controls
    ["btnWeatherClear", "btnWeatherRain", "btnWeatherSnow"].forEach((id) => {
      const btn = $(id);
      if (btn) btn.onclick = () => sendCmd({ cmd: "set_weather", weather: btn.dataset.weather });
    });
    $("btnDayCycle").onclick = () => sendCmd({ cmd: "set_day_cycle", enabled: !(state.frame && state.frame.day_cycle) });
    $("btnFriction").onclick = () => sendCmd({ cmd: "set_urban_friction", enabled: !(state.frame && state.frame.urban_friction) });
    $("btnAmbulance").onclick = () => sendCmd({ cmd: "dispatch_ambulance" });
  }

  // ==================== BOOTSTRAP (AUTO-DETECT FASTAPI vs GITHUB PAGES) ====================
  async function boot() {
    const urlTheme = new URLSearchParams(location.search).get("theme");
    let savedTheme = urlTheme === "dark" || urlTheme === "light" ? urlTheme : "light";
    if (!urlTheme) {
      try { savedTheme = localStorage.getItem(THEME_KEY) || "light"; } catch (_) {}
    }
    applyTheme(savedTheme);
    if (isMobile()) {
      setMobileTab("controls");
      setMobileSnap("peek", true);
    }
    resizeCanvas();
    bindEvents();

    // Detect if local/cloud FastAPI backend is reachable
    try {
      const health = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(1500) });
      if (health.ok) state.backendOnline = true;
    } catch (_) {
      state.backendOnline = false;
    }

    if (state.backendOnline) {
      $("runtimeBadge").textContent = "Python ML";
      state.map = await apiFetch("/api/map");
      fitMap();
      await refreshSlots();
      await refreshCredentialsBadge();

      const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
      const wsBase = API_BASE ? API_BASE.replace(/^http/, "ws") : `${wsProto}//${location.host}`;
      state.ws = new WebSocket(`${wsBase}/ws`);
      state.ws.onmessage = (ev) => {
        state.frame = JSON.parse(ev.data);
        updateUI(state.frame);
      };
    } else {
      $("runtimeBadge").textContent = "Browser Mode";
      state.standalone = new window.NoodleStandaloneEngine();
      state.map = await state.standalone.init();
      fitMap();
      await refreshSlots();
      await refreshCredentialsBadge();

      let last = performance.now();
      setInterval(() => {
        const now = performance.now();
        const dt = Math.min(0.15, (now - last) / 1000);
        last = now;
        state.standalone.tick(dt);
        state.frame = state.standalone.getFrame();
        updateUI(state.frame);
      }, 60);
    }

    const renderLoop = () => {
      drawMap();
      requestAnimationFrame(renderLoop);
    };
    requestAnimationFrame(renderLoop);
  }

  boot();
})();
