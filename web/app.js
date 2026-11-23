/**
 * Noodle Traffic Control — Interactive Web UI (Green & White Civic Theme)
 * Supports both FastAPI WebSocket backend and Standalone GitHub Pages browser mode.
 */
(function () {
  const CFG = window.NOODLE_CONFIG || {};
  const API_BASE = (CFG.API_BASE_URL || "").replace(/\/$/, "");

  const state = {
    backendOnline: false,
    standalone: null,
    ws: null,
    map: null,
    frame: null,
    showHeat: false,
    showRoutes: true,
    drawerCollapsed: false,
    camera: { cx: 0, cy: 0, scale: 0.14 },
    dragging: false,
    lastMouse: { x: 0, y: 0 },
    hoverMouse: { x: -1, y: -1 },
    speeds: [1, 2, 4, 8],
    speedIdx: 0,
  };

  // DOM helpers
  const $ = (id) => document.getElementById(id);
  const canvas = $("cityCanvas");
  const ctx = canvas.getContext("2d");

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function fitMap() {
    if (!state.map) return;
    const [x0, y0, x1, y1] = state.map.bounds;
    const rect = canvas.parentElement.getBoundingClientRect();
    const drawerOffset = state.drawerCollapsed ? 0 : 340;
    const availW = Math.max(260, rect.width - drawerOffset);
    state.camera.cx = (x0 + x1) / 2 + (drawerOffset / 2) / Math.max(0.05, state.camera.scale);
    state.camera.cy = (y0 + y1) / 2;
    const sx = (availW * 0.88) / Math.max(1, x1 - x0);
    const sy = (rect.height * 0.88) / Math.max(1, y1 - y0);
    state.camera.scale = Math.min(sx, sy);
    state.camera.cx = (x0 + x1) / 2 + (drawerOffset * 0.45) / state.camera.scale;
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
    state.camera.scale = Math.max(0.03, Math.min(6.0, state.camera.scale * factor));
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
    if (!eng) return;
    if (payload.cmd === "set_mode") eng.setMode(payload.mode);
    else if (payload.cmd === "set_adaptive") eng.adaptive = !!payload.adaptive;
    else if (payload.cmd === "set_paused") eng.paused = !!payload.paused;
    else if (payload.cmd === "set_speed") eng.speed = payload.speed;
    else if (payload.cmd === "set_traffic") { eng.trafficPercent = payload.percent; eng._spawnFleet(); eng._spawnCars(280); }
    else if (payload.cmd === "set_rush") eng.rushPoint = payload.rush_point || null;
    else if (payload.cmd === "trigger_incident") {
      const lm = eng.landmarks[Math.floor(Math.random() * eng.landmarks.length)];
      eng.incident = lm ? [lm.x, lm.y] : [0, 0];
    } else if (payload.cmd === "reset_sim") {
      eng.t = 0; eng.incident = null; eng._spawnFleet(); eng._spawnCars(280);
    }
    state.frame = eng.getFrame();
    updateUI(state.frame);
  }

  async function refreshSlots() {
    const data = state.backendOnline ? await apiFetch("/api/slots") : state.standalone.listSlots();
    const active = data.slots.find((s) => s.name === data.active_slot) || { name: data.active_slot, episodes: 0 };
    $("slotBtnLabel").textContent = `Slot: ${active.name} (${active.episodes} ep)`;
    const list = $("slotList");
    list.innerHTML = "";
    data.slots.forEach((s) => {
      const btn = document.createElement("button");
      btn.className = "slot-item" + (s.name === data.active_slot ? " active" : "");
      btn.innerHTML = `<span>${s.name}</span><span class="slot-meta">${s.episodes} ep</span>`;
      btn.onclick = async () => {
        $("slotDropdown").classList.add("hidden");
        if (state.backendOnline) await apiFetch("/api/slots/select", { method: "POST", body: JSON.stringify({ slot: s.name, clean: false }) });
        else state.standalone.selectSlot(s.name, false);
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
    $("mapClockText").textContent = isTerra
      ? `Terra (Real) · ${k.total_buses} Live CTP Buses · Tranzy API`
      : `Aether (Simulation) · t = ${f.t.toFixed(1)}s · ${k.total_routes} CTP Lines`;

    // Status pill
    const badge = $("statusBadge");
    if (isTerra) {
      badge.className = "status-pill adaptive";
      $("statusBadgeText").textContent = f.terra_status.connected ? "TERRA LIVE" : "AWAITING API KEY";
    } else if (f.adaptive) {
      badge.className = "status-pill adaptive";
      $("statusBadgeText").textContent = "ADAPTIVE AI ACTIVE";
    } else {
      badge.className = "status-pill fixed";
      $("statusBadgeText").textContent = "FIXED TIMERS";
    }

    // Hero KPI
    const redPct = k.bus_delay_reduction_pct;
    $("kpiDelayReduction").textContent = isTerra
      ? `-${Math.abs(redPct).toFixed(1)}%`
      : (f.adaptive ? `-${Math.max(0, redPct).toFixed(1)}%` : "0.0%");
    $("kpiDelaySub").textContent = isTerra
      ? `Est. corridor gain on live CTP fleet`
      : `${(k.bus_delay_ratio * 100).toFixed(1)}% vs ${(k.baseline_delay_ratio * 100).toFixed(1)}% Fixed Baseline`;

    // Secondary KPIs (Strictly honest bus-inferred Predicted Flow!)
    $("kpiPredictedFlow").textContent = `${Math.round(k.predicted_flow * 100)}%`;
    $("kpiPrecision").textContent = `${(k.ai_precision * 100).toFixed(1)}%`;
    $("kpiHoldsSub").textContent = `${k.decisive_holds} decisive holds`;

    // Municipal Value Bars
    $("barAdaptive").style.height = f.adaptive ? `${Math.min(95, Math.max(48, 60 + Math.abs(redPct) * 1.1))}%` : "54%";
    $("barFixed").style.height = "54%";
    $("kpiPaxHours").textContent = `~${k.passenger_hours_saved_day} hrs/day`;
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
    $("trafficVal").textContent = `${Math.round(f.traffic_percent)}%`;
    if (document.activeElement !== $("trafficSlider")) $("trafficSlider").value = Math.min(500, Math.round(f.traffic_percent));
    if (document.activeElement !== $("rushSelect")) $("rushSelect").value = f.rush_point || "";

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

  // ==================== LIGHT-MODE GREEN & WHITE MAP RENDERER ====================
  function drawMap() {
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width, H = rect.height;
    ctx.fillStyle = "#f2f6f3"; // Pearl White canvas
    ctx.fillRect(0, 0, W, H);

    if (!state.map) return;
    const sc = state.camera.scale;

    // Subtle urban grid backdrop texture
    ctx.strokeStyle = "rgba(20, 83, 45, 0.035)";
    ctx.lineWidth = 1;
    const gridStep = 1000 * sc;
    if (gridStep > 24) {
      const [ox, oy] = toScreen(0, 0);
      for (let x = ox % gridStep; x < W; x += gridStep) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
      for (let y = oy % gridStep; y < H; y += gridStep) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
    }

    // 1. Road casings (Bone/Sage outline) & Snow White surfaces
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    const exag = 1.0 + 1.05 * Math.max(0, Math.min(1, (0.55 - sc) / 0.45));
    for (const pass of [0, 1]) {
      for (const road of state.map.roads) {
        const baseW = road.hwy.includes("primary") ? 11 : road.hwy.includes("secondary") ? 8.5 : 6.2;
        const w = Math.max(2.2, exag * baseW * sc);
        ctx.beginPath();
        road.pts.forEach(([x, y], idx) => {
          const [sx, sy] = toScreen(x, y);
          if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
        });
        ctx.strokeStyle = pass === 0 ? "#d6e0d9" : "#ffffff";
        ctx.lineWidth = pass === 0 ? w + 2.2 : w;
        ctx.stroke();
      }
    }

    // 2. Transit corridors (Green palette!)
    if (state.showRoutes) {
      ctx.lineWidth = Math.max(1.2, 2.0 * sc);
      for (const r of state.map.routes) {
        ctx.strokeStyle = r.backbone
          ? "rgba(22, 163, 74, 0.34)"
          : (r.metropolitan ? "rgba(132, 204, 22, 0.24)" : "rgba(20, 83, 45, 0.20)");
        ctx.beginPath();
        for (const sid of r.edges) {
          const pts = state.map.seg_geoms[sid];
          if (!pts || pts.length < 2) continue;
          pts.forEach(([x, y], idx) => {
            const [sx, sy] = toScreen(x, y);
            if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
          });
        }
        ctx.stroke();
      }
    }

    const f = state.frame;
    if (f) {
      // 3. Optional bus-inferred congestion heat overlay
      if (state.showHeat && f.heat) {
        for (const [sid, load] of f.heat) {
          const pts = state.map.seg_geoms[sid];
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

      // 4. Signal Stop Lines
      if (sc > 0.08 && f.signals) {
        const sigColor = { G: "#16a34a", Y: "#f59e0b", R: "#dc2626" };
        ctx.lineWidth = sc > 0.35 ? 3.5 : 2.2;
        for (const [sid, st] of f.signals) {
          const sl = state.map.stop_lines[sid];
          if (!sl) continue;
          const [ax, ay] = toScreen(sl[0], sl[1]);
          if (ax < -20 || ax > W + 20 || ay < -20 || ay > H + 20) continue;
          const [bx, by] = toScreen(sl[2], sl[3]);
          ctx.strokeStyle = sigColor[st] || "#16a34a";
          ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
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

      // 7. Visual Cars (subtle muted sage-slate in Aether)
      if (f.cars && f.cars.length) {
        const cl = Math.max(4.5, 4.5 * sc), cw = Math.max(2.6, 2.3 * sc);
        ctx.fillStyle = "#9fb3a8";
        for (const [x, y, dx, dy] of f.cars) {
          const [sx, sy] = toScreen(x, y);
          if (sx < -15 || sx > W + 15 || sy < -15 || sy > H + 15) continue;
          ctx.save();
          ctx.translate(sx, sy);
          ctx.rotate(Math.atan2(-dy, dx));
          ctx.fillRect(-cl / 2, -cw / 2, cl, cw);
          ctx.restore();
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

      // 9. Buses (Kelly/Lime Green for driving, soft Sage circle for terminus Layover / Driver Break!)
      let hoveredBus = null;
      const chipGrid = new Set();
      const bl = Math.max(11, 11 * sc), bw = Math.max(5.5, 3.2 * sc);

      for (const b of f.buses || []) {
        const [id, line, x, y, dx, dy, spdRatio, status, restMin] = b;
        const [sx, sy] = toScreen(x, y);
        if (sx < -25 || sx > W + 25 || sy < -25 || sy > H + 25) continue;

        if (Math.hypot(sx - state.hoverMouse.x, sy - state.hoverMouse.y) < 14) {
          hoveredBus = b;
        }

        if (status !== "DRIVING") {
          // Resting cleanly in terminus bay
          ctx.beginPath();
          ctx.arc(sx, sy, 5.5, 0, Math.PI * 2);
          ctx.fillStyle = status === "DRIVER_BREAK" ? "#e0f2fe" : "#e6f7d2";
          ctx.fill();
          ctx.strokeStyle = status === "DRIVER_BREAK" ? "#0284c7" : "#14532d";
          ctx.lineWidth = 1.6;
          ctx.stroke();
          continue;
        }

        // Driving bus body
        const col = spdRatio > 0.65 ? "#16a34a" : spdRatio > 0.3 ? "#84cc16" : "#f59e0b";
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

        // Crisp route pill (collision-culled so map stays airy & readable)
        const cell = `${Math.floor(sx / 38)}:${Math.floor(sy / 22)}`;
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

      // Hover tooltip
      const tip = $("mapTooltip");
      if (hoveredBus) {
        const [id, line, , , , , spdRatio, status, restMin] = hoveredBus;
        tip.classList.remove("hidden");
        tip.style.left = `${state.hoverMouse.x + 14}px`;
        tip.style.top = `${state.hoverMouse.y - 12}px`;
        tip.textContent = status === "DRIVING"
          ? `Line ${line} (${id}) · Driving · ${Math.round(spdRatio * 50)} km/h`
          : `Line ${line} (${id}) · ${status === "DRIVER_BREAK" ? "Driver Break" : "Terminus Layover"} (${restMin}m left)`;
      } else {
        tip.classList.add("hidden");
      }
    }

    // 10. Landmarks (clean civic map labels)
    ctx.font = "600 11.5px Inter, sans-serif";
    for (const lm of state.map.landmarks) {
      const [sx, sy] = toScreen(lm.x, lm.y);
      if (sx < -60 || sx > W + 60 || sy < -30 || sy > H + 30) continue;
      const tw = ctx.measureText(lm.name).width;
      ctx.fillStyle = "rgba(255, 255, 255, 0.88)";
      ctx.beginPath();
      ctx.roundRect(sx - tw / 2 - 7, sy - 10, tw + 14, 20, 6);
      ctx.fill();
      ctx.strokeStyle = "rgba(20, 83, 45, 0.14)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = "#0f2a1f";
      ctx.fillText(lm.name, sx - tw / 2, sy + 4);
    }
  }

  // ==================== EVENT BINDINGS ====================
  function bindEvents() {
    window.addEventListener("resize", () => { resizeCanvas(); });

    // Map Pan & Zoom
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.18 : 0.85);
    }, { passive: false });

    canvas.addEventListener("mousedown", (e) => {
      state.dragging = true;
      state.lastMouse = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener("mouseup", () => { state.dragging = false; });
    canvas.addEventListener("mousemove", (e) => {
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

    $("btnZoomIn").onclick = () => { const r = canvas.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.28); };
    $("btnZoomOut").onclick = () => { const r = canvas.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 0.78); };
    $("btnFitMap").onclick = fitMap;
    $("btnHeat").onclick = () => { state.showHeat = !state.showHeat; $("btnHeat").classList.toggle("active", state.showHeat); };
    $("btnRoutes").onclick = () => { state.showRoutes = !state.showRoutes; $("btnRoutes").classList.toggle("active", state.showRoutes); };

    // Collapsible Right Drawer
    const toggleDrawer = () => {
      state.drawerCollapsed = !state.drawerCollapsed;
      $("drawer").classList.toggle("collapsed", state.drawerCollapsed);
    };
    $("btnDrawerTab").onclick = toggleDrawer;
    $("btnToggleDrawer").onclick = toggleDrawer;

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
      if (!$("slotDropdown").contains(e.target) && e.target !== $("btnSlotMenu")) {
        $("slotDropdown").classList.add("hidden");
      }
    });

    $("btnResetActiveSlot").onclick = async () => {
      $("slotDropdown").classList.add("hidden");
      const active = (state.frame && state.frame.active_slot) || "default";
      if (state.backendOnline) await apiFetch("/api/slots/reset", { method: "POST", body: JSON.stringify({ slot: active }) });
      else state.standalone.resetSlot(active);
      await refreshSlots();
    };

    $("btnNewSlot").onclick = () => {
      $("inputNewSlotName").value = "";
      $("newSlotError").classList.add("hidden");
      $("newSlotModal").classList.remove("hidden");
      $("inputNewSlotName").focus();
    };

    $("btnConfirmNewSlot").onclick = async () => {
      const name = $("inputNewSlotName").value.trim();
      try {
        if (state.backendOnline) await apiFetch("/api/slots/select", { method: "POST", body: JSON.stringify({ slot: name, clean: true }) });
        else state.standalone.selectSlot(name, true);
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

    // Close modals
    document.querySelectorAll("[data-close]").forEach((btn) => {
      btn.onclick = () => $(btn.dataset.close).classList.add("hidden");
    });

    // Aether Quick Controls
    $("btnCtrlAdaptive").onclick = () => sendCmd({ cmd: "set_adaptive", adaptive: true });
    $("btnCtrlFixed").onclick = () => sendCmd({ cmd: "set_adaptive", adaptive: false });
    $("trafficSlider").oninput = (e) => { $("trafficVal").textContent = `${e.target.value}%`; };
    $("trafficSlider").onchange = (e) => sendCmd({ cmd: "set_traffic", percent: Number(e.target.value) });
    $("rushSelect").onchange = (e) => sendCmd({ cmd: "set_rush", rush_point: e.target.value || null });
    $("btnPlayPause").onclick = () => sendCmd({ cmd: "set_paused", paused: !(state.frame && state.frame.paused) });
    $("btnSpeed").onclick = () => {
      state.speedIdx = (state.speedIdx + 1) % state.speeds.length;
      sendCmd({ cmd: "set_speed", speed: state.speeds[state.speedIdx] });
    };
    $("btnIncident").onclick = () => sendCmd({ cmd: "trigger_incident" });
    $("btnResetSim").onclick = () => sendCmd({ cmd: "reset_sim" });
  }

  // ==================== BOOTSTRAP (AUTO-DETECT FASTAPI vs GITHUB PAGES) ====================
  async function boot() {
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
