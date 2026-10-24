
(function () {
  "use strict";
  var TOKEN_KEY = "nerve-observatory-token";
  var LEVELS = ["fleet", "host", "harness", "agent", "run"];
  var state = { level: "fleet", host: null, harness: null, agent: null, run: null, snapshot: null, events: null, live: null, liveError: null, fleet: null, fleetError: null, liveAgentQuery: "", liveAgentPage: 0, liveSelectedKey: null, liveOpen: false, historyOpen: false, historyRows: [], historyCursor: null, historyCapture: null, historyError: null };
  var token = "";
  try { token = sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { token = ""; }
  var inFlight = false;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }
  function dash(v) { return v === null || v === undefined || v === "" ? "UNRECORDED" : String(v); }
  function ageText(f) {
    if (!f || f.age_seconds === null || f.age_seconds === undefined) return "";
    var s = Math.round(f.age_seconds);
    return s < 90 ? s + "s" : s < 5400 ? Math.round(s / 60) + "m" : Math.round(s / 3600) + "h";
  }
  // Freshness annunciator: fresh=green, stale=amber, unknown=off.
  function freshTile(f) {
    var s = (f && f.state) || "unknown";
    var cls = s === "fresh" ? "ok" : s === "stale" ? "warn" : "off";
    var label = s === "fresh" ? "FRESH " + ageText(f) : s === "stale" ? "STALE " + ageText(f) : "NO TIMESTAMP";
    return el("span", { class: "tile " + cls, text: label.trim() });
  }
  function storeTile(status) {
    var s = status || "missing";
    var cls = s === "ok" ? "ok" : s === "unavailable" ? "alarm" : s === "missing" ? "off" : "warn";
    return el("span", { class: "tile " + cls, text: "STORE " + s.toUpperCase() });
  }
  function kv(pairs) {
    var dl = el("dl", { class: "kv" });
    pairs.forEach(function (p) {
      dl.appendChild(el("dt", { text: p[0] }));
      dl.appendChild(el("dd", { text: dash(p[1]) }));
    });
    return dl;
  }
  // Analog gauge: a real value against a stated scale. Unknown renders an empty dial.
  function gauge(label, value, max, opts) {
    opts = opts || {};
    var known = value !== null && value !== undefined && isFinite(Number(value));
    var v = known ? Math.max(0, Math.min(max, Number(value))) : 0;
    var angle = -120 + (240 * (known && max > 0 ? v / max : 0));
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 80 80");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label + ": " + (known ? Number(value).toFixed(opts.digits || 0) + (opts.unit || "") : "unavailable"));
    var face = document.createElementNS(NS, "circle");
    face.setAttribute("cx", "40"); face.setAttribute("cy", "40"); face.setAttribute("r", "36");
    face.setAttribute("fill", "#0a1220"); face.setAttribute("stroke", "#5d7288"); face.setAttribute("stroke-width", "3");
    svg.appendChild(face);
    for (var t = 0; t <= 10; t++) {
      var a = (-120 + 24 * t) * Math.PI / 180;
      var tick = document.createElementNS(NS, "line");
      tick.setAttribute("x1", String(40 + 28 * Math.sin(a))); tick.setAttribute("y1", String(40 - 28 * Math.cos(a)));
      tick.setAttribute("x2", String(40 + 33 * Math.sin(a))); tick.setAttribute("y2", String(40 - 33 * Math.cos(a)));
      tick.setAttribute("stroke", t > 7 ? "#ff4d4d" : "#8ea3ba"); tick.setAttribute("stroke-width", t % 5 === 0 ? "2" : "1");
      svg.appendChild(tick);
    }
    if (known) {
      var needle = document.createElementNS(NS, "line");
      needle.setAttribute("x1", "40"); needle.setAttribute("y1", "40");
      needle.setAttribute("x2", String(40 + 26 * Math.sin(angle * Math.PI / 180)));
      needle.setAttribute("y2", String(40 - 26 * Math.cos(angle * Math.PI / 180)));
      needle.setAttribute("stroke", "#ffb13b"); needle.setAttribute("stroke-width", "3"); needle.setAttribute("stroke-linecap", "round");
      svg.appendChild(needle);
    }
    var hub = document.createElementNS(NS, "circle");
    hub.setAttribute("cx", "40"); hub.setAttribute("cy", "40"); hub.setAttribute("r", "3"); hub.setAttribute("fill", "#d8e2ee");
    svg.appendChild(hub);
    var status = !known ? "unknown" : (opts.alarmAt !== undefined && v >= opts.alarmAt) ? "alarm" : (opts.warnAt !== undefined && v >= opts.warnAt) ? "warn" : "";
    var wrap = el("div", { class: "gauge " + status });
    wrap.appendChild(svg);
    var txt = el("div", {});
    txt.appendChild(el("div", { class: "glabel", text: label }));
    txt.appendChild(el("div", { class: "gval", text: known ? Number(value).toFixed(opts.digits || 0) + (opts.unit || "") : "UNAVAILABLE" }));
    wrap.appendChild(txt);
    return wrap;
  }
  // A control-wall instrument bay. Keyboard and screen readers get a real button.
  function bay(title, idLine, children, onopen, ariaLabel) {
    var b = el("button", { class: "bay", type: "button", "aria-label": ariaLabel || title }, []);
    b.appendChild(el("h3", { text: title }));
    if (idLine) b.appendChild(el("div", { class: "id", text: idLine }));
    children.forEach(function (c) { if (c) b.appendChild(c); });
    if (onopen) b.addEventListener("click", onopen);
    else b.disabled = true;
    return b;
  }
  function section(title) { return el("div", { class: "section-head", text: title }); }
  function bayTitle(title) { return el("div", { class: "bay-title", text: title }); }
  function notice(msg, kind) { return el("p", { class: "notice " + (kind || ""), role: "note", text: msg }); }
  function annun(tiles) { var d = el("div", { class: "annun" }); tiles.forEach(function (t) { d.appendChild(t); }); return d; }
  function tile(cls, label) { return el("span", { class: "tile " + cls, text: label }); }

  function api(path) {
    var headers = { Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(path, { headers: headers, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" }).then(function (r) {
      if (r.status === 401) { needAuth(); throw new Error("auth"); }
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    });
  }
  function needAuth() {
    document.getElementById("auth").hidden = false;
    setMaster("AUTH REQUIRED", "alarm");
  }
  function setMaster(msg, cls) {
    document.getElementById("master-text").textContent = msg;
    document.getElementById("master-lamp").className = "lamp " + (cls || "off");
  }

  function refresh() {
    if (inFlight) return Promise.resolve();
    inFlight = true;
    return api("/api/snapshot").then(function (snap) {
      state.snapshot = snap;
      // Independent freshness: live sensor failures never erase historical evidence.
      return Promise.all([
        api("/api/live").then(function (live) {
          state.live = live; state.liveError = null;
        }).catch(function (err) {
          state.live = null;
          state.liveError = err && err.message === "auth" ? "AUTH REQUIRED" : "LIVE SENSOR UNAVAILABLE";
        }),
        api("/api/fleet").then(function (fleet) {
          state.fleet = fleet; state.fleetError = null;
        }).catch(function (err) {
          state.fleet = null;
          state.fleetError = err && err.message === "auth" ? "AUTH REQUIRED" : "PROCESS CENSUS UNAVAILABLE";
        })
      ]);
    }).then(function () {
      document.getElementById("updated").textContent = "COLLECTED " + new Date().toLocaleTimeString();
      setMaster("COLLECTOR CONNECTED", "off");
      if (state.level === "run" && state.run) return loadRun().then(render);
      render();
    }).catch(function (err) {
      if (err && err.message !== "auth") setMaster("COLLECTOR UNREACHABLE", "alarm");
      render();
    }).then(function () { inFlight = false; });
  }
  function loadRun() {
    return api("/api/run?task_id=" + encodeURIComponent(state.run.task_id) + "&run_id=" + encodeURIComponent(state.run.run_id))
      .then(function (r) { state.events = r; })
      .catch(function () { state.events = { status: "unavailable" }; });
  }

  // Semantic zoom: each level shows a finer grain of the same real data.
  function zoomTo(target, patch) {
    var p = patch || {};
    if (target === "fleet") { p.host = null; p.harness = null; p.agent = null; p.run = null; }
    if (target === "host") { p.harness = null; p.agent = null; p.run = null; }
    if (target === "harness") { p.agent = null; p.run = null; }
    if (target === "agent") { p.run = null; }
    if (target !== "run") state.events = null;
    Object.keys(p).forEach(function (k) { state[k] = p[k]; });
    state.level = target;
    render();
    document.getElementById("view").focus();
    if (target === "run") loadRun().then(render);
  }
  var quiltMode = true;
  var factoryMode = true;  // Factory is default; verified Atlas remains available.
  function showFactory(on) {
    factoryMode = !!on;
    if (factoryMode) quiltMode = true;
    document.getElementById("factory-toggle").setAttribute("aria-pressed", factoryMode ? "true" : "false");
    render();
  }
  function setMode(on) {
    quiltMode = on;
    if (!on && quilt.unbind) { quilt.unbind(); quilt.unbind = null; }
    var b = document.getElementById("mode");
    b.textContent = on ? "SHOW LEGACY LEVELS" : "SHOW ZOOM QUILT";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    render();
  }
  function up() {
    if (factoryMode && window.NerveFactory) { window.NerveFactory.up(); return; }
    if (quiltMode) { if (quilt.stack.length) quiltGoDepth(quilt.stack.length - 1); else quiltFitRoot(); return; }
    var i = LEVELS.indexOf(state.level);
    if (i > 0) zoomTo(LEVELS[i - 1]);
  }

  // Breadcrumbs: quilt mode shows the district stack (each crumb pops to that depth);
  // legacy mode shows the semantic-zoom path. Rebuilt from scratch each render, so no stale click listeners survive.
  function crumbs() {
    var list = document.getElementById("crumbs");
    list.textContent = "";
    var items;
    if (quiltMode) {
      items = [["FLEET ROOT", function () { quiltGoDepth(0); }]];
      quilt.stack.forEach(function (e, i) { items.push([e.label, function () { quiltGoDepth(i + 1); }]); });
    } else {
      var legacy = [["fleet", "FLEET"]];
      if (state.host) legacy.push(["host", "HOST " + state.host.id.toUpperCase()]);
      if (state.harness) legacy.push(["harness", "SOURCE " + state.harness.source]);
      if (state.agent) legacy.push(["agent", "AGENT " + (state.agent.worker_id || "UNRECORDED")]);
      if (state.run) legacy.push(["run", "RUN " + state.run.task_id + "#" + state.run.run_id]);
      items = legacy.map(function (it) { return [it[1], function () { zoomTo(it[0]); }]; });
    }
    items.forEach(function (it, idx) {
      var li = el("li");
      if (idx === items.length - 1) li.appendChild(el("span", { "aria-current": "page", text: it[0] }));
      else {
        var b = el("button", { type: "button", text: it[0] });
        b.addEventListener("click", it[1]);
        li.appendChild(b);
      }
      list.appendChild(li);
    });
  }

  function render() {
    document.body.classList.toggle("factory-active", factoryMode);
    crumbs();
    var main = document.getElementById("view");
    var factoryRestore = factoryMode && window.NerveFactory &&
      typeof window.NerveFactory.captureUIState === "function" ?
      window.NerveFactory.captureUIState(main) : null;
    main.textContent = "";
    var snap = state.snapshot;
    if (!snap) { main.appendChild(notice("NO SNAPSHOT YET. WAITING FOR COLLECTOR.")); return; }
    var crumbNav = document.getElementById("crumbs").parentNode;
    crumbNav.hidden = false;
    if (factoryMode && window.NerveFactory) {
      return window.NerveFactory.render(main, {snapshot:snap, live:state.live,
        fleet:state.fleet, fleetError:state.fleetError, api:api,
        onAtlas:function () { showFactory(false); },
        onDepth:function () {},restore:factoryRestore});
    }
    if (window.NerveFactory) window.NerveFactory.dispose();
    if (quiltMode) return renderQuilt(main, snap);
    if (state.level === "fleet") return renderFleet(main, snap);
    if (state.level === "host") return renderHost(main, snap);
    if (state.level === "harness") return renderHarness(main, snap);
    if (state.level === "agent") return renderAgent(main, snap);
    if (state.level === "run") return renderRun(main, snap);
  }

  // ---- Zoom quilt: one affine transform (screen = world*s + t), mirrors camera.py ----
  var MIN_S = 0.05, MAX_S = 64;
  // Quilt navigation state. stack entries are {frame, nodeId, cam, label}:
  //   frame  = the district frame entered (global origin, units normalized to width/1000),
  //   nodeId = the district id,
  //   cam    = the camera in the PARENT frame, restored when popping back to it.
  // quilt.cam is always expressed in the current frame (top of stack, or root).
  var quilt = { cam: null, key: null, root: null, world: null, vp: null, stack: [], items: [], directIds: [], frameBox: null, notice: null, searchTerm: "", searchPage: 0, unbind: null,
    miniViewport: null, miniRoot: null, selectedId: null, inspector: null,
    visibleCount: 0, flightTarget: null };
  var ROOT_FRAME = { ox: 0, oy: 0, units: 1 };
  function curFrame() { return quilt.stack.length ? quilt.stack[quilt.stack.length - 1].frame : ROOT_FRAME; }
  function isDistrict(n) { return !!n && Array.isArray(n.children) && n.children.length > 0; }
  function frameOf(n) {
    var b = n && n.bounds;
    if (!Array.isArray(b) || b.length < 4 || typeof b[0] !== "number" || typeof b[1] !== "number" || !isFinite(b[0]) || !isFinite(b[1])) return null;
    var f = { ox: b[0], oy: b[1], units: b[2] / 1000 };
    return localBounds(b, f) ? f : null;
  }
  function nodeById(root, id) {
    var all = flattenQ(root, 0, []);
    for (var i = 0; i < all.length; i++) if (all[i][0].id === id) return all[i][0];
    return null;
  }
  // Convert scale AND translation when a measured frame changes after refresh.
  // This uses the same geometry as the tested Python/JS enterFrame pair.
  function shiftCam(c, oldF, newF) {
    if (!c) return c;
    return enterFrame(c, oldF, newF);
  }
  function quiltRerender() { render(); if (quiltMode && quilt.vp) quilt.vp.focus(); }
  function quiltRefuse(msg) { quilt.notice = msg; quiltRerender(); }
  // Enter a direct-child district through enterFrame. Every check runs before the single stack/camera
  // assignment, so the stack and camera change together or not at all. Child frame = district origin, same units.
  function enterDistrict(n) {
    if (!isDistrict(n) || quilt.directIds.indexOf(n.id) === -1) return;
    var child = frameOf(n);
    if (!child) return quiltRefuse("DISTRICT " + dash(n.id) + " HAS NO VALID BOUNDS. NOT ENTERED.");
    var next = enterFrame(quilt.cam, curFrame(), child);
    if (!next) return quiltRefuse("CANNOT ENTER " + dash(n.id) + ": FRAME OR SCALE OUT OF RANGE.");
    var box = localBounds(n.bounds, child);
    var vp = quilt.vp;
    var fit = (vp && box) ? fitBounds(vp.clientWidth, vp.clientHeight, [0, 0, box[2], box[3]]) : null;
    var entry = { frame: child, nodeId: n.id, cam: quilt.cam, label: String(n.kind).toUpperCase() + " " + dash(n.label) };
    quilt.stack = quilt.stack.concat([entry]);
    quilt.cam = next; // exact same picture, re-addressed in the child's frame
    quilt.flightTarget = fit || null; // next paint glides into the district
    quilt.notice = null;
    quiltRerender();
  }
  // Pops to depth d (0 = fleet root) and restores the camera saved at that depth.
  function quiltGoDepth(d) {
    if (!(d >= 0 && d < quilt.stack.length)) return;
    var cam = quilt.stack[d].cam;
    quilt.stack = quilt.stack.slice(0, d);
    quilt.cam = cam;
    quilt.notice = null;
    quiltRerender();
  }
  function quiltHome() {
    quilt.stack = [];
    quilt.notice = null;
    quiltFitRoot();
    quiltRerender();
  }
  // Hit-test a viewport point (screen px) against enterable districts (direct children of the current frame), topmost first.
  function hitDistrictScreen(sx, sy) {
    var c = quilt.cam;
    if (!c || !c.s) return null;
    var px = (sx - c.tx) / c.s, py = (sy - c.ty) / c.s;
    for (var i = quilt.items.length - 1; i >= 0; i--) {
      var it = quilt.items[i];
      if (!it.direct || !isDistrict(it.node)) continue;
      var lb = it.lb;
      if (px >= lb[0] && px <= lb[0] + lb[2] && py >= lb[1] && py <= lb[1] + lb[3]) return it.node;
    }
    return null;
  }
  // Separate traversal and DOM budgets. Breadth-first traversal guarantees
  // every observed sibling agent a chance before opening its long run history.
  var Q_TRAVERSAL_LIMIT = 3000, Q_RENDER_LIMIT = 300, Q_LIST_PAGE_SIZE = 60;
  function boundedQ(root, limit) {
    var queue = [[root, 0]], out = [], pos = 0;
    while (pos < queue.length && out.length < limit) {
      var part = queue[pos++], node = part[0], depth = part[1];
      out.push(part);
      (Array.isArray(node.children) ? node.children : []).forEach(function (c) {
        queue.push([c, depth + 1]);
      });
    }
    return { rows: out, truncated: pos < queue.length };
  }
  function quiltLod() {
    if (!quilt.cam) return quilt.stack.length;
    return Math.min(5, quilt.stack.length + (quilt.cam.s >= 1.5 ? 2 : quilt.cam.s >= 0.3 ? 1 : 0));
  }
  // Actual viewport location projected into a small, never-fabricated 2D atlas.
  function updateAtlasMap() {
    if (!quilt.miniViewport || !quilt.frameBox || !quilt.cam || !quilt.vp) return;
    var box = quilt.frameBox, c = quilt.cam;
    var x = -c.tx / c.s, y = -c.ty / c.s;
    var w = quilt.vp.clientWidth / c.s, h = quilt.vp.clientHeight / c.s;
    if (!Number.isFinite(x + y + w + h)) return;
    quilt.miniViewport.setAttribute("x", String(x));
    quilt.miniViewport.setAttribute("y", String(y));
    quilt.miniViewport.setAttribute("width", String(Math.max(0, w)));
    quilt.miniViewport.setAttribute("height", String(Math.max(0, h)));
  }
  function atlasSvgRect(svg, props) {
    var shape = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    Object.keys(props).forEach(function (k) { shape.setAttribute(k, String(props[k])); });
    svg.appendChild(shape);
    return shape;
  }
  function buildTaskNeighborhoods(container, items) {
    var counts = new Map();
    items.forEach(function (it) {
      if (it.node.kind !== "run") return;
      var task = String(it.node.label || "UNRECORDED").split(" #")[0];
      counts.set(task, (counts.get(task) || 0) + 1);
    });
    if (!counts.size) return;
    var box = el("section", { class: "atlas-neighborhoods" });
    box.appendChild(el("div", { class: "atlas-section-label", text: "TASK NEIGHBORHOODS / OBSERVED RUNS" }));
    Array.from(counts).sort(function (a,b) { return b[1] - a[1] || a[0].localeCompare(b[0]); }).slice(0, 12)
      .forEach(function (entry) {
        var row = el("div", { class: "atlas-neighborhood" });
        var chip = el("span", { class: "atlas-colorchip", "aria-hidden": "true" });
        chip.style.background = atlasTaskColor(entry[0]);
        row.appendChild(chip);
        row.appendChild(el("span", { class: "atlas-neighborhood-name", text: entry[0] }));
        row.appendChild(el("strong", { text: String(entry[1]) }));
        box.appendChild(row);
      });
    box.appendChild(el("p", { class: "atlas-footnote", text: "COLOR = RECORDED TASK ID, NOT STATUS OR ACTIVITY" }));
    container.appendChild(box);
  }
  function buildAtlasMap(container, items) {
    var box = quilt.frameBox;
    if (!box) return;
    var card = el("section", { class: "atlas-overview" });
    card.appendChild(el("div", { class: "atlas-section-label", text: "02 / NAVIGATION RADAR" }));
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + box[2] + " " + box[3]);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Spatial minimap of the current recorded district");
    svg.setAttribute("class", "atlas-minimap");
    var limit = 850, stride = Math.max(1, Math.ceil(items.length / limit));
    items.forEach(function (item, i) {
      if (i % stride !== 0 && !item.direct) return;
      var b = item.lb;
      var region = atlasSvgRect(svg, { x: b[0], y: b[1], width: b[2], height: b[3],
        class: "map-" + item.node.kind + (item.direct ? " map-direct" : "") });
      if (item.node.kind === "run")
        region.style.fill = atlasTaskColor(String(item.node.label || "").split(" #")[0]);
    });
    quilt.miniViewport = atlasSvgRect(svg, { x: 0, y: 0, width: box[2], height: box[3], class: "map-viewport" });
    svg.addEventListener("click", function (ev) {
      var r = svg.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0 && quilt.cam && quilt.vp)) return;
      var localX = (ev.clientX - r.left) / r.width * box[2] + box[0];
      var localY = (ev.clientY - r.top) / r.height * box[3] + box[1];
      quilt.cam = { s: quilt.cam.s,
        tx: quilt.vp.clientWidth / 2 - localX * quilt.cam.s,
        ty: quilt.vp.clientHeight / 2 - localY * quilt.cam.s };
      applyCam();
    });
    card.appendChild(svg);
    card.appendChild(el("p", { class: "atlas-footnote", text: "CLICK TO RECENTER · OUTLINE = CAMERA · REAL RECORDS ONLY" }));
    container.appendChild(card);
  }
  function updateAtlasInspector() {
    if (!quilt.inspector || !quilt.root) return;
    var box = quilt.inspector;
    box.textContent = "";
    var chosen = quilt.selectedId && nodeById(quilt.root, quilt.selectedId);
    if (!chosen && quilt.stack.length)
      chosen = nodeById(quilt.root, quilt.stack[quilt.stack.length - 1].nodeId);
    if (!chosen) chosen = quilt.root;
    box.appendChild(el("div", { class: "atlas-section-label", text: "03 / SELECTED RECORD" }));
    box.appendChild(el("h3", { class: "atlas-inspector-title", text: dash(chosen.label) }));
    box.appendChild(kv([
      ["CLASS", String(chosen.kind || "unknown").toUpperCase()],
      ["STATUS", String(chosen.state || "unknown").toUpperCase()],
      ["CHILDREN", Array.isArray(chosen.children) ? chosen.children.length : 0],
      ["IDENTITY", chosen.id]
    ]));
    if (Array.isArray(chosen.children) && chosen.children.length)
      box.appendChild(el("p", { class: "atlas-footnote", text: "Select a district on the atlas or in the index to descend." }));
    else
      box.appendChild(el("p", { class: "atlas-footnote", text: "Recorded leaf. Use wheel or Fit to navigate its surroundings." }));
  }
  function hitAtlasItemScreen(sx, sy) {
    if (!quilt.cam || !quilt.cam.s || !quilt.world) return null;
    var wx = (sx - quilt.cam.tx) / quilt.cam.s;
    var wy = (sy - quilt.cam.ty) / quilt.cam.s;
    for (var i = quilt.items.length - 1; i >= 0; i--) {
      var it = quilt.items[i], b = it.lb;
      if (it.el.parentNode !== quilt.world) continue;
      if (wx >= b[0] && wx <= b[0] + b[2] && wy >= b[1] && wy <= b[1] + b[3]) return it;
    }
    return null;
  }
  function selectAtlasItem(it) {
    if (!it) return;
    quilt.selectedId = it.node.id;
    if (it.direct && isDistrict(it.node)) return enterDistrict(it.node);
    quiltFocusNode(it.node);
    updateAtlasInspector();
  }
  // The map is a true spatial field. Tiny recorded run tiles remain visible as
  // texture; labels emerge as their projected size becomes readable.
  function cullQuilt() {
    if (!quilt.world || !quilt.vp || !quilt.cam) return;
    var c = quilt.cam, vw = quilt.vp.clientWidth, vh = quilt.vp.clientHeight;
    if (!(c.s > 0 && vw > 0 && vh > 0)) return;
    var x0 = -c.tx / c.s, y0 = -c.ty / c.s;
    var x1 = (vw - c.tx) / c.s, y1 = (vh - c.ty) / c.s;
    var count = 0, deepest = quilt.stack.length;
    quilt.items.forEach(function (it) {
      var b = it.lb;
      var intersects = b[0] <= x1 && b[0] + b[2] >= x0 &&
        b[1] <= y1 && b[1] + b[3] >= y0;
      var shortEdge = Math.min(b[2], b[3]) * c.s;
      var group = !!it.node && Array.isArray(it.node.children) && it.node.children.length > 0;
      var show = intersects && (group || shortEdge >= 2) && count < Q_RENDER_LIMIT;
      if (show) {
        count++;
        deepest = Math.max(deepest, it.depth + quilt.stack.length);
        var level = shortEdge >= 130 ? "3" : shortEdge >= 65 ? "2" : shortEdge >= 22 ? "1" : "0";
        if (it.el.getAttribute && it.el.getAttribute("data-lod") !== level)
          it.el.setAttribute("data-lod", level);
      }
      if (show && it.el.parentNode !== quilt.world) quilt.world.appendChild(it.el);
      else if (!show && it.el.parentNode === quilt.world) quilt.world.removeChild(it.el);
    });
    var info = document.getElementById("qinfo");
    if (info) info.textContent = "D" + Math.min(5, deepest) + "  /  " + count +
      " DRAWN  /  " + quilt.items.length + " RECORDED" +
      (quilt.truncated ? "  /  SCAN BUDGET" : "");
    quilt.visibleCount = count;
    if (typeof updateAtlasMap === "function") updateAtlasMap();
  }
  // Refresh: re-derive each stacked frame from the new snapshot. A level whose district vanished (or stopped
  // being a district) is cut off, and the view returns to the surviving parent's saved camera.
  function reconcileStack(q) {
    if (!quilt.stack.length) return;
    var old = [ROOT_FRAME].concat(quilt.stack.map(function (e) { return e.frame; }));
    var kept = [], cut = -1;
    for (var i = 0; i < quilt.stack.length; i++) {
      var e = quilt.stack[i];
      var n = nodeById(q, e.nodeId);
      var f = isDistrict(n) ? frameOf(n) : null;
      if (!f) { cut = i; break; }
      kept.push({ frame: f, nodeId: e.nodeId, cam: e.cam, label: String(n.kind).toUpperCase() + " " + dash(n.label) });
    }
    var neu = [ROOT_FRAME].concat(kept.map(function (k) { return k.frame; }));
    var depth = kept.length;
    // cam of the first dropped entry is the camera in frame depth (the surviving parent); otherwise current cam.
    var cur = cut >= 0 ? quilt.stack[cut].cam : quilt.cam;
    var rebased = kept.map(function (k, idx) {
      return { frame: k.frame, nodeId: k.nodeId, cam: shiftCam(k.cam, old[idx], neu[idx]), label: k.label };
    });
    var next = shiftCam(cur, old[depth], neu[depth]);
    if (!next || rebased.some(function (k) { return !k.cam; })) {
      quilt.stack = [];
      quilt.cam = null;
      quilt.notice = "THE DISTRICT GEOMETRY CHANGED BEYOND A SAFE CAMERA CONVERSION. VIEW RESET.";
      return;
    }
    quilt.stack = rebased;
    quilt.cam = next;
    if (cut >= 0) quilt.notice = "A DISTRICT IN THE PATH IS NO LONGER IN THE SNAPSHOT. RETURNED TO ITS PARENT.";
  }
  function clampS(s) { return Math.min(MAX_S, Math.max(MIN_S, s)); }
  function zoomAt(cam, factor, sx, sy) {
    var wx = (sx - cam.tx) / cam.s, wy = (sy - cam.ty) / cam.s;
    var s = clampS(cam.s * factor);
    return { s: s, tx: sx - wx * s, ty: sy - wy * s };
  }
  function wheelPixels(d, m, l, h) { if (!Number.isFinite(d)) return 0; var u = m === 0 ? 1 : m === 1 ? (Number.isFinite(l) && l > 0 ? l : 16) : m === 2 ? (Number.isFinite(h) && h > 0 ? h : 0) : 0; return Math.max(-200, Math.min(200, d * u)); }
  function quiltIdentity(root) { if (!root || typeof root.id !== 'string' || !root.id) return null; return JSON.stringify([typeof root.kind === 'string' ? root.kind : null, root.id]); }
  function shouldFitQuilt(cam, previousKey, root) { var key = quiltIdentity(root); return !cam || key === null || previousKey !== key; }
  function panBy(cam, dx, dy) { return { s: cam.s, tx: cam.tx + dx, ty: cam.ty + dy }; }
  // Mirrors camera.enter_frame: re-express a camera from parent-frame to child-frame coordinates.
  // Frames are {ox, oy, units}. Returns null (refusal, no clamping) for invalid input or a scale outside [MIN_S, MAX_S].
  function enterFrame(cam, parent, child) {
    function fin(v) { return typeof v === "number" && Number.isFinite(v); }
    function frameOk(f) { return !!f && fin(f.ox) && fin(f.oy) && fin(f.units) && f.units > 0; }
    if (!cam || !fin(cam.s) || !fin(cam.tx) || !fin(cam.ty) || !frameOk(parent) || !frameOk(child)) return null;
    var s = cam.s * (child.units / parent.units);
    var tx = cam.tx + (child.ox - parent.ox) / parent.units * cam.s;
    var ty = cam.ty + (child.oy - parent.oy) / parent.units * cam.s;
    if (!(fin(s) && fin(tx) && fin(ty) && s >= MIN_S && s <= MAX_S)) return null;
    return { s: s, tx: tx, ty: ty };
  }
  function fitBounds(vw, vh, b) {
    if (!(vw > 0 && vh > 0 && b[2] > 0 && b[3] > 0)) return null;
    var s = clampS(Math.min(vw / b[2], vh / b[3]) * 0.9);
    return { s: s, tx: (vw - b[2] * s) / 2 - b[0] * s, ty: (vh - b[3] * s) / 2 - b[1] * s };
  }
  function applyCam() {
    if (!quilt.world || !quilt.cam) return;
    var c = quilt.cam;
    quilt.world.style.transform = "translate(" + c.tx + "px," + c.ty + "px) scale(" + c.s + ")";
    var sl = document.getElementById("qscale");
    if (sl) sl.textContent = "ZOOM " + Math.round(c.s * 100) + "%";
    cullQuilt();
  }
  // Fits the frame currently entered (fleet root when the stack is empty).
  function quiltFitRoot() {
    if (!quilt.vp || !quilt.frameBox) return;
    quilt.cam = fitBounds(quilt.vp.clientWidth, quilt.vp.clientHeight, quilt.frameBox) || quilt.cam;
    applyCam();
  }
  function quiltFocusNode(node) {
    if (!quilt.vp) return;
    var lb = localBounds(node.bounds, curFrame());
    var next = lb && fitBounds(quilt.vp.clientWidth, quilt.vp.clientHeight, lb);
    if (next) {
      var world = quilt.world;
      if (world) world.classList.add("is-flying");
      quilt.cam = next; applyCam();
      if (world) setTimeout(function () { world.classList.remove("is-flying"); }, 440);
    }
  }
  function flattenQ(n, depth, out) {
    out.push([n, depth]);
    n.children.forEach(function (c) { flattenQ(c, depth + 1, out); });
    return out;
  }
  // Converts global bounds [x,y,w,h] into frame-local [x,y,w,h]. Frame is {ox,oy,units}. Returns null on invalid input or non-positive size. Never mutates globalBounds.
  function localBounds(globalBounds, frame) {
    function fin(v) { return typeof v === "number" && Number.isFinite(v); }
    if (!Array.isArray(globalBounds) || globalBounds.length < 4) return null;
    var x = globalBounds[0], y = globalBounds[1], w = globalBounds[2], h = globalBounds[3];
    if (!(fin(x) && fin(y) && fin(w) && fin(h))) return null;
    if (!frame || !(fin(frame.ox) && fin(frame.oy) && fin(frame.units) && frame.units > 0)) return null;
    var lx = (x - frame.ox) / frame.units, ly = (y - frame.oy) / frame.units;
    var lw = w / frame.units, lh = h / frame.units;
    if (!(fin(lx) && fin(ly) && fin(lw) && fin(lh) && lw > 0 && lh > 0)) return null;
    return [lx, ly, lw, lh];
  }
  // These palette colors group observed task identities, never freshness.
  function atlasTaskColor(taskId) {
    var name = String(taskId || "unrecorded"), hash = 2166136261;
    for (var i = 0; i < name.length; i++) hash = Math.imul(hash ^ name.charCodeAt(i), 16777619);
    var colors = ["#e8a459", "#70c6c5", "#b49dec", "#9ccb8c", "#e58eaa",
      "#79acd8", "#f2c277", "#9fdbc3", "#dfa9f4", "#ef9d80", "#a1badf"];
    return colors[(hash >>> 0) % colors.length];
  }
  function qNodeEl(n, frame) {
    var f = (frame === undefined || frame === null) ? { ox: 0, oy: 0, units: 1 } : frame;
    var lb = localBounds(n.bounds, f);
    if (!lb) return null;
    var st = String(n.state || "unknown"), hasChildren = Array.isArray(n.children) && n.children.length > 0;
    var cls = "qnode q-" + n.kind + (hasChildren ? " q-district" : " q-leaf") +
      (st === "stale" ? " s-stale" : (st === "unknown" || st === "missing") ? " s-unknown" : "");
    var node = el("div", { class: cls, "data-id": n.id, "aria-hidden": "true", "data-lod": "0" }, [
      el("span", { class: "qtype", text: String(n.kind).toUpperCase() }),
      el("span", { class: "qlabel", text: dash(n.label) }),
      el("span", { class: "qstate", text: "STATE " + st.toUpperCase() }),
      el("span", { class: "qcount", text: hasChildren ? n.children.length + " RECORDED CHILDREN" : "" })
    ]);
    node.style.left = lb[0] + "px"; node.style.top = lb[1] + "px";
    node.style.width = lb[2] + "px"; node.style.height = lb[3] + "px";
    if (n.kind === "run" && node.style.setProperty && typeof atlasTaskColor === "function") {
      var task = String(n.label || "").split(" #")[0];
      node.style.setProperty("--run-accent", atlasTaskColor(task));
    }
    return node;
  }
  function bindQuilt(vp) {
    if (quilt.unbind) quilt.unbind();
    var controller = new AbortController();
    quilt.unbind = function () { controller.abort(); };
    function vpPoint(ev) { var r = vp.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }
    vp.addEventListener("wheel", function (ev) {
      ev.preventDefault();
      if (quilt.world) quilt.world.classList.remove("is-flying");
      var p = vpPoint(ev);
      var dy = wheelPixels(ev.deltaY, ev.deltaMode, parseFloat(getComputedStyle(vp).lineHeight), vp.clientHeight);
      quilt.cam = zoomAt(quilt.cam, Math.exp(-dy * 0.0015), p[0], p[1]);
      applyCam();
    }, { passive: false });
    var drag = null, moved = 0;
    vp.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      if (quilt.world) quilt.world.classList.remove("is-flying");
      drag = { x: ev.clientX, y: ev.clientY, sx: ev.clientX, sy: ev.clientY };
      moved = 0;
      vp.classList.add("dragging");
      try { vp.setPointerCapture(ev.pointerId); } catch (e) { /* capture unsupported: drag still works inside the viewport */ }
    });
    vp.addEventListener("pointermove", function (ev) {
      if (!drag) return;
      moved = Math.max(moved, Math.abs(ev.clientX - drag.sx) + Math.abs(ev.clientY - drag.sy));
      quilt.cam = panBy(quilt.cam, ev.clientX - drag.x, ev.clientY - drag.y);
      drag = { x: ev.clientX, y: ev.clientY, sx: drag.sx, sy: drag.sy };
      applyCam();
    });
    // A press-and-release that barely moved is a click: enter the district under the pointer, if any.
    function endDrag(ev) {
      var wasClick = !!drag && ev.type === "pointerup" && moved < 4;
      drag = null; vp.classList.remove("dragging");
      if (!wasClick) return;
      var p = vpPoint(ev);
      var hit = hitAtlasItemScreen(p[0], p[1]);
      if (hit) selectAtlasItem(hit);
    }
    vp.addEventListener("pointerup", endDrag);
    vp.addEventListener("pointercancel", endDrag);
    vp.addEventListener("lostpointercapture", endDrag);
    window.addEventListener("blur", endDrag, { signal: controller.signal });
    vp.addEventListener("keydown", function (ev) {
      var c = quilt.cam, cx = vp.clientWidth / 2, cy = vp.clientHeight / 2, k = ev.key, handled = true;
      if (k === "+" || k === "=") quilt.cam = zoomAt(c, 1.25, cx, cy);
      else if (k === "-" || k === "_") quilt.cam = zoomAt(c, 0.8, cx, cy);
      else if (k === "ArrowLeft") quilt.cam = panBy(c, 40, 0);
      else if (k === "ArrowRight") quilt.cam = panBy(c, -40, 0);
      else if (k === "ArrowUp") quilt.cam = panBy(c, 0, 40);
      else if (k === "ArrowDown") quilt.cam = panBy(c, 0, -40);
      else if (k === "Escape") up();
      else if (k === "Home") quiltHome();
      else if (k === "Enter") { var hit = hitDistrictScreen(cx, cy); if (hit) enterDistrict(hit); }
      else handled = false;
      if (handled) { ev.preventDefault(); ev.stopPropagation(); applyCam(); }
    });
  }
  function renderQuilt(main, snap) {
    var q = snap.quilt;
    if (!q) {
      if (quilt.unbind) { quilt.unbind(); quilt.unbind = null; }
      quilt.stack = []; quilt.items = []; quilt.vp = null; quilt.world = null;
      main.appendChild(notice("HIERARCHY NOT PROVIDED BY COLLECTOR.", "missing")); return;
    }
    var fresh = shouldFitQuilt(quilt.cam, quilt.key, q);
    if (fresh) quilt.stack = [];
    else reconcileStack(q);
    quilt.key = quiltIdentity(q); quilt.root = q;
    var frame = curFrame();
    var frameNode = quilt.stack.length ? nodeById(q, quilt.stack[quilt.stack.length - 1].nodeId) : q;
    quilt.frameBox = frameNode ? localBounds(frameNode.bounds, frame) : null;
    quilt.directIds = frameNode && Array.isArray(frameNode.children) ? frameNode.children.map(function (c) { return c.id; }) : [];
    var shell = el("section", { class: "atlas-shell", "aria-label": "Nerve recursive spatial atlas" });
    var stage = el("div", { class: "atlas-stage" });
    var rail = el("aside", { class: "atlas-rail", "aria-label": "Spatial navigation and recorded entity index" });
    shell.appendChild(stage); shell.appendChild(rail);
    main.appendChild(el("div", { class: "atlas-topline" }, [
      el("div", { class: "atlas-eyebrow", text: "NERVE / RECURSIVE SYSTEMS ATLAS  ·  VERIFIED GEOMETRY" }),
      el("p", { class: "atlas-intro", text: "A spatial field of actual hosts, sources, workers and runs. Zoom into a district or traverse it from the record index." })
    ]));
    if (quilt.notice) main.appendChild(notice(quilt.notice, "missing"));
    main.appendChild(shell);
    var vp = el("div", { class: "qvp", tabindex: "0", role: "application",
      "aria-label": "Zoom quilt of recorded fleet, hosts, sources, agents and runs" });
    var world = el("div", { class: "qworld" });
    // Items are the frame's own subtree. Nodes are built here but attached only when visible (see cullQuilt).
    var items = [];
    var scope = boundedQ(frameNode || q, Q_TRAVERSAL_LIMIT);
    quilt.truncated = scope.truncated;
    scope.rows.forEach(function (p) {
      var n = p[0];
      if (quilt.stack.length && n === frameNode) return;
      var lb = localBounds(n.bounds, frame);
      var ne = lb && qNodeEl(n, frame);
      if (!ne) return;
      items.push({ node: n, lb: lb, el: ne, depth: p[1], direct: quilt.directIds.indexOf(n.id) !== -1 });
    });
    quilt.items = items; quilt.world = world; quilt.vp = vp;
    var stats = { host: 0, source: 0, agent: 0, run: 0 };
    items.forEach(function (it) { if (Object.prototype.hasOwnProperty.call(stats, it.node.kind)) stats[it.node.kind]++; });
    var header = el("div", { class: "atlas-stage-header" });
    var titleGroup = el("div", { class: "atlas-stage-title" });
    titleGroup.appendChild(el("div", { class: "atlas-section-label",
      text: "01 / ACTIVE DISTRICT  ·  LEVEL " + quilt.stack.length }));
    titleGroup.appendChild(el("h2", { text: quilt.stack.length ?
      String(frameNode.kind || "DISTRICT").toUpperCase() + " / " + dash(frameNode.label) :
      "FLEET / SPATIAL OVERVIEW" }));
    titleGroup.appendChild(el("div", { class: "atlas-stats", text:
      stats.host + " HOSTS   ·   " + stats.source + " SOURCES   ·   " +
      stats.agent + " AGENTS   ·   " + stats.run + " RUNS" }));
    header.appendChild(titleGroup);
    var actions = el("div", { class: "atlas-actions" });
    function atlasButton(label, action, disabled) {
      var button = el("button", { type: "button", class: "atlas-action", text: label });
      button.disabled = !!disabled;
      button.addEventListener("click", action);
      return button;
    }
    actions.appendChild(atlasButton("OUT", function () {
      quilt.cam = zoomAt(quilt.cam, .78, quilt.vp.clientWidth / 2, quilt.vp.clientHeight / 2); applyCam();
    }));
    actions.appendChild(atlasButton("IN", function () {
      quilt.cam = zoomAt(quilt.cam, 1.28, quilt.vp.clientWidth / 2, quilt.vp.clientHeight / 2); applyCam();
    }));
    actions.appendChild(atlasButton("FIT", quiltFitRoot));
    actions.appendChild(atlasButton("↑ UP", up, !quilt.stack.length));
    header.appendChild(actions); stage.appendChild(header);
    vp.appendChild(world);
    vp.appendChild(el("span", { class: "qzoom tile off", id: "qscale", text: "ZOOM" }));
    vp.appendChild(el("span", { class: "qinfo tile off", id: "qinfo", text: "LOD" }));
    stage.appendChild(vp);
    stage.appendChild(el("div", { class: "atlas-stage-footer" }, [
      el("span", { text: "WHEEL / ZOOM  ·  DRAG / PAN  ·  CLICK / EXPLORE" }),
      el("span", { text: "ESC / BACK  ·  HOME / FLEET  ·  + / − / SCALE" })
    ]));
    var tools = el("div", { class: "qfind" });
    var search = el("input", { type: "search", "aria-label": "Search recorded quilt entities",
      placeholder: "Search recorded ID, type, label or state" });
    search.value = quilt.searchTerm;
    var indexBox = el("section", { class: "atlas-index" });
    indexBox.appendChild(el("div", { class: "atlas-section-label", text: "04 / RECORDED ENTITY INDEX" }));
    tools.appendChild(search);
    indexBox.appendChild(tools);
    rail.appendChild(indexBox);
    var list = el("ul", { class: "qlist", "aria-label": "Quilt entities, searchable recorded items in this frame" });
    var pages = el("div", { class: "qpages" });
    indexBox.appendChild(list); indexBox.appendChild(pages);
    function drawList() {
      list.textContent = ""; pages.textContent = "";
      var term = quilt.searchTerm.trim().toLowerCase();
      var found = items.filter(function (it) {
        var n = it.node;
        return !term || [n.id, n.kind, n.label, n.state].some(function (v) {
          return String(v || "").toLowerCase().indexOf(term) >= 0;
        });
      });
      var last = Math.max(0, Math.ceil(found.length / Q_LIST_PAGE_SIZE) - 1);
      quilt.searchPage = Math.max(0, Math.min(last, quilt.searchPage));
      found.slice(quilt.searchPage * Q_LIST_PAGE_SIZE, (quilt.searchPage + 1) * Q_LIST_PAGE_SIZE).forEach(function (it) {
        var n = it.node, li = el("li", { style: "padding-left:" + (it.depth * 12) + "px" });
        var btn = el("button", { class: "qbtn", type: "button", "data-id": n.id,
          text: String(n.kind).toUpperCase() + " " + dash(n.label) + " - " + String(n.state).toUpperCase() + " - " + n.id });
        btn.addEventListener("click", function () {
          selectAtlasItem(it);
        });
        li.appendChild(btn); list.appendChild(li);
      });
      if (!found.length) list.appendChild(notice("NO MATCHING RECORDED ENTITIES."));
      var prev = el("button", { type: "button", text: "PREVIOUS" }); prev.disabled = quilt.searchPage === 0;
      prev.addEventListener("click", function () { quilt.searchPage--; drawList(); });
      var next = el("button", { type: "button", text: "NEXT" }); next.disabled = quilt.searchPage >= last;
      next.addEventListener("click", function () { quilt.searchPage++; drawList(); });
      pages.appendChild(prev);
      pages.appendChild(el("span", { text: " " + found.length + " RECORDED | PAGE " + (quilt.searchPage + 1) + "/" + (last + 1) +
        (quilt.truncated ? " | SEARCH TRAVERSAL CAPPED" : "") + " " }));
      pages.appendChild(next);
    }
    search.addEventListener("input", function () { quilt.searchTerm = search.value; quilt.searchPage = 0; drawList(); });
    drawList();
    buildAtlasMap(rail, items);
    buildTaskNeighborhoods(rail, items);
    quilt.inspector = el("section", { class: "atlas-inspector" });
    rail.appendChild(quilt.inspector);
    if (quilt.selectedId && !nodeById(q, quilt.selectedId)) quilt.selectedId = frameNode.id;
    updateAtlasInspector();
    bindQuilt(vp);
    if (fresh) quiltFitRoot();
    applyCam();
    if (quilt.flightTarget) {
      var endCamera = quilt.flightTarget, activeWorld = quilt.world;
      quilt.flightTarget = null;
      requestAnimationFrame(function () {
        if (quilt.world !== activeWorld) return;
        activeWorld.classList.add("is-flying");
        requestAnimationFrame(function () {
          if (quilt.world !== activeWorld) return;
          quilt.cam = endCamera;
          applyCam();
          setTimeout(function () { activeWorld.classList.remove("is-flying"); }, 450);
        });
      });
    }
    var lower = el("section", { class: "atlas-telemetry", "aria-label": "Live evidence and recording panels" });
    main.appendChild(lower);
    renderLiveControlRoom(lower);
    renderHistoryPanel(lower);
  }

  // A separate read-only evidence plane. /api/live is authenticated and sanitized.
  // Never inject HTML markup or claim activity without a live process lease.
  function liveNum(v, precision) {
    return typeof v === "number" && Number.isFinite(v) ? v.toFixed(precision || 0) : "UNAVAILABLE";
  }
  function renderLiveControlRoom(main) {
    var live = state.live;
    var agents = live && live.agents && Array.isArray(live.agents.items) ? live.agents.items : [];
    var leases = live && live.agent_feed && Array.isArray(live.agent_feed.leases) ? live.agent_feed.leases : [];
    var active = agents.filter(function (a) { return a.lease_status === "live"; }).length;
    var source = live && live.agent_feed ? String(live.agent_feed.state) : "unknown";
    var box = el("details", { class: "tcontrol", "aria-label": "Hermes and host instrumentation" });
    box.open = state.liveOpen;
    box.addEventListener("toggle", function () { state.liveOpen = box.open; });
    box.appendChild(el("summary", { text: "OBSERVABILITY / HOST INSTRUMENTS | " +
      active + " ACTIVE AGENTS | " + agents.length + " RECORDED | FEED " + source.toUpperCase() }));
    var body = el("div", { class: "tcontrol-body" });
    box.appendChild(body); main.appendChild(box);
    if (!live) { body.appendChild(notice(state.liveError || "LIVE SENSOR NOT CONNECTED.", "missing")); return; }
    body.appendChild(annun([
      tile(active ? "ok" : "off", "ACTIVE " + active),
      tile("off", "RECORDED " + agents.length),
      tile(source === "available" ? "ok" : "off", "FEED " + source.toUpperCase()),
      tile("off", "LEASES " + leases.length)
    ]));
    var services = live.services && Array.isArray(live.services.sources) ? live.services.sources : [];
    var serviceBay = el("div", { class: "tservices", "aria-label": "Verified local service health" });
    services.forEach(function (svc) {
      serviceBay.appendChild(tile(svc.state === "ok" ? "ok" : "off",
        String(svc.label || svc.id).toUpperCase() + " " + String(svc.state || "unknown").toUpperCase()));
    });
    if (!services.length) serviceBay.appendChild(tile("off", "SERVICE PROBES NOT AVAILABLE"));
    body.appendChild(serviceBay);
    if (!active) body.appendChild(el("p", { class: "muted reading",
      text: "No observer is currently reporting live work. Recorded sessions below may be completed, stale or disconnected; that is not an idle assertion." }));
    var hosts = el("div", { class: "thosts" });
    ["j2", "win4060"].forEach(function (id) {
      var m = live.host && live.host.metrics && live.host.metrics[id];
      var g = live.host && live.host.gpu && live.host.gpu[id];
      var gpu = g && Array.isArray(g.gpus) && g.gpus.length ? g.gpus[0] : null;
      var b = el("article", { class: "thost", "aria-label": id + " host facts" });
      b.appendChild(el("h3", { text: id.toUpperCase() + " | " + (m && m.scope_label || "UNKNOWN SCOPE") }));
      b.appendChild(annun([
        tile(m && m.state === "ok" ? "ok" : "off", "CPU/RAM " + String(m && m.state || "unknown").toUpperCase()),
        tile(g && g.state === "ok" ? "ok" : "off", "GPU " + String(g && g.state || "unknown").toUpperCase())
      ]));
      var ram = m && m.state === "ok" && Number.isFinite(m.memory_total_mib) &&
        Number.isFinite(m.memory_available_mib)
        ? liveNum(m.memory_total_mib - m.memory_available_mib) + " / " + liveNum(m.memory_total_mib) + " MiB" : "UNAVAILABLE";
      b.appendChild(kv([
        ["CPU", m && m.state === "ok" ? liveNum(m.cpu_percent, 1) + "%" : "UNAVAILABLE"],
        ["RAM used / total", ram],
        ["GPU", gpu && g.state === "ok" ? dash(gpu.name) + " | " + liveNum(gpu.utilization_percent) + "%" : "UNAVAILABLE"],
        ["GPU used / total", gpu && g.state === "ok" ? liveNum(gpu.memory_used_mib) + " / " + liveNum(gpu.memory_total_mib) + " MiB" : "UNAVAILABLE"],
        ["sample age", m && Number.isFinite(m.age_s) ? liveNum(m.age_s, 1) + "s" : "UNAVAILABLE"],
        ["attribution", "HOST LEVEL; NOT AGENT GPU USE"]
      ]));
      hosts.appendChild(b);
    });
    body.appendChild(hosts);
    var line = el("div", { class: "tline" });
    line.appendChild(el("h3", { text: "HERMES AGENTS / RECORDED EVIDENCE" }));
    var filter = el("input", { type: "search", "aria-label": "Filter recorded agents", placeholder: "Filter agent, model, tool or phase" });
    filter.value = state.liveAgentQuery; line.appendChild(filter);
    body.appendChild(line);
    var cards = el("div", { class: "tagents", "aria-label": "Agent status cards" });
    var paging = el("div", { class: "tpages" });
    var detail = el("div", { class: "tevidence", role: "region", "aria-label": "Selected agent evidence" });
    body.appendChild(cards); body.appendChild(paging); body.appendChild(detail);
    function drawDetail() {
      detail.textContent = "";
      var selected = agents.find(function (a) { return a.key === state.liveSelectedKey; });
      if (!selected) return;
      detail.appendChild(el("h3", { text: "EVIDENCE / " + selected.label }));
      detail.appendChild(kv([
        ["phase", selected.phase], ["lease", selected.lease_status],
        ["model", selected.model], ["provider", selected.provider],
        ["tool calls", selected.tool_calls], ["api requests", selected.api_requests],
        ["last observation", selected.last_ts], ["provenance", selected.origin_ref]
      ]));
      var events = live.events && Array.isArray(live.events.items) ? live.events.items : [];
      var mine = events.filter(function (ev) { return ev.sess === selected.key; }).slice(0, 25);
      if (!mine.length) detail.appendChild(notice("NO RECENT EVENT DETAIL IN THE BOUNDED VIEW."));
      else detail.appendChild(table(["time", "event", "state", "evidence"], mine.map(function (ev) {
        return [ev.ts, ev.kind, ev.status || ev.lease_status || "observed", ev.origin_ref];
      })));
    }
    function drawCards() {
      cards.textContent = ""; paging.textContent = "";
      var term = state.liveAgentQuery.trim().toLowerCase();
      var all = agents.filter(function (a) {
        return !term || [a.label, a.model, a.provider, a.phase, a.last_tool, a.key]
          .some(function (v) { return String(v || "").toLowerCase().indexOf(term) >= 0; });
      });
      var perPage = 20, maxPage = Math.max(0, Math.ceil(all.length / perPage) - 1);
      state.liveAgentPage = Math.max(0, Math.min(state.liveAgentPage, maxPage));
      all.slice(state.liveAgentPage * perPage, (state.liveAgentPage + 1) * perPage).forEach(function (a) {
        var b = el("button", { type: "button", class: "tagent" + (a.key === state.liveSelectedKey ? " selected" : ""),
          "aria-pressed": a.key === state.liveSelectedKey ? "true" : "false" });
        b.appendChild(el("strong", { text: a.label }));
        b.appendChild(el("span", { class: "tstatus " + (a.lease_status === "live" ? "live" : "stale"),
          text: String(a.lease_status || "unknown").toUpperCase() }));
        b.appendChild(el("span", { text: "PHASE " + dash(a.phase) }));
        b.appendChild(el("span", { text: "MODEL " + dash(a.model) }));
        b.appendChild(el("span", { text: "TOOLS " + liveNum(a.tool_calls) + " / API " + liveNum(a.api_requests) }));
        b.addEventListener("click", function () { state.liveSelectedKey = a.key; drawCards(); drawDetail(); });
        cards.appendChild(b);
      });
      if (!all.length) cards.appendChild(notice("NO MATCHING OBSERVED AGENTS."));
      var prev = el("button", { type: "button", text: "PREVIOUS" }); prev.disabled = state.liveAgentPage === 0;
      prev.addEventListener("click", function () { state.liveAgentPage--; drawCards(); });
      var next = el("button", { type: "button", text: "NEXT" }); next.disabled = state.liveAgentPage === maxPage;
      next.addEventListener("click", function () { state.liveAgentPage++; drawCards(); });
      paging.appendChild(prev);
      paging.appendChild(el("span", { text: all.length + " MATCHES | PAGE " + (state.liveAgentPage + 1) + "/" + (maxPage + 1) }));
      paging.appendChild(next);
    }
    filter.addEventListener("input", function () { state.liveAgentQuery = filter.value; state.liveAgentPage = 0; drawCards(); });
    drawCards(); drawDetail();
  }

  // Bounded, paginated derived-history reader. Data are recorded observations,
  // never inferred agent actions. The capture status is shown separately.
  function renderHistoryPanel(main) {
    var group = el("details", { class: "thistory", "aria-label": "Recorded Nerve timeline" });
    group.open = state.historyOpen;
    group.appendChild(el("summary", { text: "RECORDED HISTORY / TIMELINE | " +
      state.historyRows.length + " LOADED | " + String(state.historyCapture || "NOT CHECKED").toUpperCase() }));
    var region = el("div", { class: "thistory-body" });
    group.appendChild(region); main.appendChild(group);
    var busy = false;
    function draw() {
      region.textContent = "";
      var status = state.historyCapture || "UNKNOWN";
      region.appendChild(annun([tile(status === "recording" || status === "running" ? "ok" : "off",
        "CAPTURE " + String(status).toUpperCase()), tile("off", "ROWS LOADED " + state.historyRows.length)]));
      if (state.historyError) region.appendChild(notice(state.historyError, "missing"));
      if (!state.historyRows.length) region.appendChild(notice("NO RECORDED HISTORY ROWS IN THE BOUNDED STORE."));
      else region.appendChild(table(["recorded", "store", "runs", "events", "fresh / stale", "shadow"],
        state.historyRows.map(function (r) {
          var freshness = r.run_freshness || {};
          return [r.recorded_at, r.store_status, r.run_count, r.event_total,
            dash(freshness.fresh) + " / " + dash(freshness.stale),
            String(r.shadow_status || "unknown") + " / " + dash(r.shadow_records) + " records"];
        })));
      var ctl = el("div", { class: "tpages" });
      var reload = el("button", { type: "button", text: "REFRESH HISTORY" });
      reload.disabled = busy;
      reload.addEventListener("click", function () { load(null); });
      var next = el("button", { type: "button", text: "NEXT 50 ROWS" });
      next.disabled = busy || !state.historyCursor || state.historyRows.length >= 400;
      next.addEventListener("click", function () { load(state.historyCursor); });
      ctl.appendChild(reload); ctl.appendChild(next); region.appendChild(ctl);
    }
    function load(cursor) {
      if (busy) return;
      busy = true;
      var path = "/api/history?limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      return Promise.all([api("/api/history/status"), api(path)]).then(function (parts) {
        var capture = parts[0] && parts[0].capture;
        state.historyCapture = capture ? capture.state || (capture.enabled ? "running" : "disabled") : "unknown";
        state.historyRows = cursor ? state.historyRows.concat(parts[1].rows || []).slice(0, 400) : parts[1].rows || [];
        state.historyCursor = parts[1].next_cursor || null;
        state.historyError = null;
      }).catch(function () {
        state.historyError = "HISTORY SOURCE UNREACHABLE; NO ROWS WERE FABRICATED.";
      }).then(function () { busy = false; draw(); });
    }
    group.addEventListener("toggle", function () {
      state.historyOpen = group.open;
      if (group.open && state.historyCapture === null) load(null);
    });
    draw();
  }

  function renderFleet(main, snap) {
    var sh = snap.shadow;
    main.appendChild(annun([storeTile(snap.store.status), freshTile(sh.freshness),
      tile(sh.status === "ok" ? "ok" : sh.status === "unavailable" ? "alarm" : "off", "D1 SHADOW " + String(sh.status).toUpperCase())]));
    main.appendChild(bayTitle("FLEET — LOCAL HOST"));
    main.appendChild(el("p", { class: "muted reading", text: snap.fleet.note }));
    var bays = el("div", { class: "bays" });
    snap.fleet.hosts.forEach(function (h) {
      bays.appendChild(bay("HOST " + h.id, h.hostname, [
        gauge("LOAD 1m", h.loadavg ? h.loadavg[0] : null, 8, { digits: 2, warnAt: 4, alarmAt: 6 }),
        annun([tile("ok", "LIVE OS FACT")]),
        kv([["system", h.system], ["release", h.release], ["python", h.python], ["observed", h.observed_at]]),
      ], function () { zoomTo("host", { host: h }); }, "Host " + h.id + ", open"));
    });
    main.appendChild(bays);
    main.appendChild(bayTitle("SUPERVISION STORE"));
    main.appendChild(annun([storeTile(snap.store.status),
      tile("off", "RUNS " + snap.runs.length),
      tile("off", "EVENTS " + snap.event_total),
      tile("off", "SOURCES " + snap.harnesses.length)]));
    if (snap.store.status === "missing") main.appendChild(notice("SUPERVISION STORE NOT FOUND. NO RUN DATA RECORDED.", "missing"));
    if (snap.store.status === "unavailable") main.appendChild(notice("SUPERVISION STORE UNREADABLE: " + dash(snap.store.error), "missing"));
  }

  function renderHost(main, snap) {
    var h = state.host;
    main.appendChild(bayTitle("HOST " + h.id.toUpperCase() + " — " + dash(h.hostname)));
    main.appendChild(annun([tile("ok", "LIVE OS FACT"), tile("off", "EVENT SOURCES " + snap.harnesses.length)]));
    var dials = el("div", { class: "bays" });
    dials.appendChild(bay("LOAD AVERAGE", "1 / 5 / 15 min", [
      gauge("1 min", h.loadavg ? h.loadavg[0] : null, 8, { digits: 2, warnAt: 4, alarmAt: 6 }),
      gauge("5 min", h.loadavg ? h.loadavg[1] : null, 8, { digits: 2, warnAt: 4, alarmAt: 6 }),
      gauge("15 min", h.loadavg ? h.loadavg[2] : null, 8, { digits: 2, warnAt: 4, alarmAt: 6 }),
    ], null, "Load average"));
    dials.appendChild(bay("HOST FACTS", null, [kv([["hostname", h.hostname], ["system", h.system + " " + dash(h.release)], ["python", h.python], ["observed", h.observed_at]])], null, "Host facts"));
    main.appendChild(dials);
    main.appendChild(bayTitle("EVENT SOURCES (HARNESS)"));
    if (!snap.harnesses.length) main.appendChild(notice("NO EVENT SOURCES RECORDED. NOTHING IS SHOWN AS LIVE WITHOUT A RECORDED EVENT."));
    var bays = el("div", { class: "bays" });
    snap.harnesses.forEach(function (src) {
      bays.appendChild(bay("SOURCE " + src.source, "events " + src.events + " · stale " + src.stale_events, [
        gauge("EVENTS", src.events, Math.max(1, src.events), { digits: 0 }),
        annun([freshTile(src.freshness)]),
      ], function () { zoomTo("harness", { harness: src }); }, "Source " + src.source + ", open"));
    });
    main.appendChild(bays);
    main.appendChild(bayTitle("D1 / SHADOW DECISIONS — AUTHORITY: PRIMARY JEV"));
    main.appendChild(renderShadow(snap.shadow));
    main.appendChild(bayTitle("SUPERVISION DIAGNOSTICS"));
    if (!snap.diagnostics.length) main.appendChild(notice("NO DIAGNOSTICS RECORDED."));
    else main.appendChild(table(["kind", "count", "last", "freshness"], snap.diagnostics.map(function (d) {
      return [d.kind, d.count, d.last_at, freshTile(d.freshness)];
    })));
  }

  function renderShadow(sh) {
    var box = el("div", {});
    var st = sh.status;
    var cls = st === "ok" ? "ok" : st === "unavailable" ? "alarm" : st === "missing" ? "off" : "warn";
    box.appendChild(annun([tile(cls, "SHADOW " + String(st).toUpperCase()), freshTile(sh.freshness)]));
    if (st === "missing") { box.appendChild(notice("NO D1 SHADOW TELEMETRY FILE. D1 IS NOT SHOWN AS OBSERVED.", "missing")); return box; }
    if (st === "unavailable") { box.appendChild(notice("SHADOW TELEMETRY UNREADABLE: " + dash(sh.error), "missing")); return box; }
    box.appendChild(annun([
      tile(sh.shadow_errors ? "alarm" : "ok", "ERRORS " + sh.shadow_errors),
      tile(sh.shadow_timeouts ? "warn" : "ok", "TIMEOUTS " + sh.shadow_timeouts),
      tile("off", "PAIRED " + sh.paired_answers),
    ]));
    var dials = el("div", { class: "bays" });
    var agreePct = sh.agreement_rate === null || sh.agreement_rate === undefined ? null : sh.agreement_rate * 100;
    dials.appendChild(bay("AGREEMENT", "primary vs shadow", [
      gauge("AGREE", agreePct, 100, { digits: 1, unit: "%" }),
    ], null, "Agreement rate"));
    var p95 = sh.shadow_latency_ms ? sh.shadow_latency_ms.p95 : null;
    dials.appendChild(bay("SHADOW LATENCY p95", "ms, finite sample", [
      gauge("P95", p95, Math.max(1, p95 || 1), { digits: 1, unit: "ms" }),
      kv([["p50 ms", sh.shadow_latency_ms && sh.shadow_latency_ms.p50], ["samples", sh.shadow_latency_ms && sh.shadow_latency_ms.count], ["provenance", JSON.stringify(sh.shadow_provenance || {})]]),
    ], null, "Shadow latency"));
    box.appendChild(dials);
    if (sh.recent && sh.recent.length) {
      box.appendChild(table(["time", "shadow provider", "model", "latency ms", "error class", "freshness"], sh.recent.map(function (r) {
        return [r.created_at, r.shadow.provider, r.shadow.model, r.shadow.latency_ms, r.shadow.error_class || "none", freshTile(r.freshness)];
      })));
    }
    return box;
  }

  function renderHarness(main, snap) {
    var src = state.harness;
    main.appendChild(bayTitle("SOURCE " + src.source));
    main.appendChild(annun([freshTile(src.freshness), tile(src.stale_events ? "warn" : "ok", "STALE EVENTS " + src.stale_events)]));
    main.appendChild(kv([["events", src.events], ["last event", src.last_at]]));
    var agents = snap.agents.filter(function (a) { return a.sources.indexOf(src.source) !== -1; });
    main.appendChild(bayTitle("AGENTS / WORKERS"));
    if (!agents.length) main.appendChild(notice("NO AGENTS RECORDED FOR THIS SOURCE."));
    var bays = el("div", { class: "bays" });
    agents.forEach(function (a) {
      bays.appendChild(bay("AGENT " + (a.worker_id || "UNRECORDED"), "runs " + a.runs + " · events " + a.events, [
        annun([freshTile(a.freshness)]),
      ], function () { zoomTo("agent", { agent: a }); }, "Agent " + (a.worker_id || "unrecorded") + ", open"));
    });
    main.appendChild(bays);
  }

  function renderAgent(main, snap) {
    var a = state.agent;
    main.appendChild(bayTitle("AGENT " + (a.worker_id || "UNRECORDED")));
    main.appendChild(annun([freshTile(a.freshness)]));
    main.appendChild(kv([["runs", a.runs], ["events", a.events], ["last event", a.last_at]]));
    var runs = snap.runs.filter(function (r) {
      return (r.worker_id || "unrecorded") === (a.worker_id || "unrecorded") &&
        (!state.harness || r.sources.indexOf(state.harness.source) !== -1);
    });
    main.appendChild(bayTitle("RUNS"));
    if (!runs.length) main.appendChild(notice("NO RUNS RECORDED FOR THIS AGENT."));
    else main.appendChild(table(["task", "run", "events", "stale", "control", "last event", "freshness"], runs.map(function (r) {
      return [r.task_id, r.run_id, r.events, r.stale_events, r.control ? r.control.control + (r.control.acknowledged ? " (ack)" : " (pending)") : "none", r.last_at, freshTile(r.freshness)];
    }), function (row, idx) { return function () { zoomTo("run", { run: runs[idx] }); }; }));
  }

  function renderRun(main, snap) {
    var r = state.run;
    main.appendChild(bayTitle("RUN " + r.task_id + " #" + r.run_id));
    main.appendChild(annun([freshTile(r.freshness), tile(r.control ? "warn" : "off", "CONTROL " + (r.control ? r.control.control : "NONE"))]));
    main.appendChild(kv([["worker", r.worker_id], ["events", r.events], ["stale events", r.stale_events], ["first event", r.first_at], ["last event", r.last_at]]));
    main.appendChild(bayTitle("EVENTS (SANITIZED; PAYLOADS NOT SHOWN)"));
    if (!state.events) { main.appendChild(notice("LOADING EVENTS.")); return; }
    if (state.events.status !== "ok") { main.appendChild(notice("EVENTS " + String(state.events.status).toUpperCase() + (state.events.error ? ": " + state.events.error : ""), "missing")); return; }
    if (!state.events.events.length) { main.appendChild(notice("NO EVENTS RECORDED FOR THIS RUN.")); return; }
    if (state.events.truncated) main.appendChild(notice("SHOWING NEWEST EVENTS ONLY."));
    main.appendChild(table(["seq", "time", "type", "criterion", "source", "stale"], state.events.events.map(function (e) {
      return [e.seq, e.created_at, e.event_type, e.criterion_id, e.source, e.stale ? "yes" : "no"];
    })));
  }

  function table(headers, rows, rowOpen) {
    var wrap = el("div", { class: "wrap" });
    var t = el("table");
    var thead = el("thead"); var tr = el("tr");
    headers.forEach(function (h) { tr.appendChild(el("th", { scope: "col", text: h })); });
    thead.appendChild(tr); t.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (row, idx) {
      var rowEl = el("tr", rowOpen ? { class: "row", tabindex: "0" } : {});
      if (rowOpen) {
        rowEl.addEventListener("click", rowOpen(row, idx));
        rowEl.addEventListener("keydown", function (ev) { if (ev.key === "Enter") rowOpen(row, idx)(); });
      }
      row.forEach(function (cell) {
        var td = el("td");
        if (cell && cell.nodeType) td.appendChild(cell); else td.textContent = dash(cell);
        rowEl.appendChild(td);
      });
      tbody.appendChild(rowEl);
    });
    t.appendChild(tbody); wrap.appendChild(t);
    return wrap;
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.target && /^(INPUT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.defaultPrevented) return;
    if (ev.key === "Backspace" || ev.key === "Escape") { ev.preventDefault(); up(); }
    if (ev.key === "r" || ev.key === "R") refresh();
  });
  document.getElementById("back").addEventListener("click", up);
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("mode").addEventListener("click", function () { setMode(!quiltMode); });
  document.getElementById("factory-toggle").addEventListener("click", function () { showFactory(!factoryMode); });
  // Resizing rechecks visibility but never silently destroys a user's camera.
  window.addEventListener("resize", function () { if (quiltMode && !factoryMode) applyCam(); });
  document.getElementById("token-save").addEventListener("click", function () {
    token = document.getElementById("token").value.trim();
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (e) { /* storage unavailable: token kept in memory only */ }
    document.getElementById("auth").hidden = true;
    refresh();
  });

  render();
  refresh();
  setInterval(refresh, 15000);
})();
