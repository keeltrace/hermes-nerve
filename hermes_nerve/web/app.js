"use strict";

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value === null || value === undefined ? "" : value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const labels = {jev:"Jev (hosted)",laya:"Laya (local)",openjev:"OpenJev (local)",
  systemone:"System One",d1:"Liquid D1 (local)",shadow:"Shadow comparison"};
const about = {jev:"Hermes uses your existing provider setup.",
  laya:"Run a Laya sidecar on this computer.",
  openjev:"Connect an OpenJev local helper.",
  systemone:"Connect a compatible local System One server.",
  d1:"Run LiquidAI D1 from a local server.",
  shadow:"Jev decides; a second backend runs for comparison."};
const localDefaults = {laya:"http://127.0.0.1:8765",openjev:"http://127.0.0.1:3000",
  systemone:"http://127.0.0.1:8080",d1:"http://127.0.0.1:8080"};

const state = {
  token:"", info:null, step:0, profile:"operator", overrides:{}, advanced:{},
  resolved:null, preview:null, busy:false, screen:"setup", dirty:false, saved:false,
  showAdvanced:false, advancedQuery:"", checking:null, installPreview:null, lastError:"",
  backend:"jev", provider:"openrouter", clearAdvanced:new Set(),
  checkingKey:null, checkingBusy:false, showBackends:false,
  setupPlan:null, setupJob:null, setupPollTimer:null,
};

function probeScope() {
  const local = state.backend === "shadow" ?
    (currentAdvanced("reflex_shadow_backend") || "d1") : state.backend;
  return JSON.stringify([state.backend, state.provider, local, state.profile,
    currentAdvanced("reflex_" + local + "_base_url") || localDefaults[local] || ""]);
}
function clearProbe() {
  state.checking = null;
  state.checkingKey = null;
  const previous = $("backend-result");
  if (previous) previous.remove();
}
function getToken() {
  const match = location.hash.match(/^#token=([A-Za-z0-9_-]+)$/);
  return match ? match[1] : "";
}
async function api(path, data) {
  const options = {method: data === undefined ? "GET" : "POST", headers:{"X-Nerve-Token":state.token}};
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("The local Control Panel did not return JSON."); }
  if (!response.ok) { throw new Error(result.error || "Request failed."); }
  return result;
}
function request() {
  return {profile:state.profile, overrides:state.overrides, advanced:state.advanced, clear_advanced:Array.from(state.clearAdvanced)};
}
function message(text, type) {
  $("messages").innerHTML = "";
  if (!text) return;
  const el = document.createElement("div");
  el.className = "notice " + (type || "");
  el.textContent = text;
  $("messages").appendChild(el);
}
function handleError(error) {
  state.lastError = error.message || String(error);
  message(state.lastError, "error");
}
let previewSeq = 0;
async function refreshPreview() {
  const seq = ++previewSeq;
  try {
    const result = await api("/api/preview", request());
    // Request-sequence guard: drop a stale preview so an older response can
    // never overwrite newer displayed state after rapid setting changes.
    if (seq !== previewSeq) return;
    state.preview = result;
    state.resolved = result.modules;
    state.lastError = "";
    message("");
  } catch(error) {
    if (seq !== previewSeq) return;
    state.preview = null;
    handleError(error);
  }
  render();
}
async function refreshStatus() {
  const info = await api("/api/status");
  state.info = info;
  $("version").textContent = "v" + info.version;
  const identity = info.runtime_identity || {};
  $("runtime-identity").textContent = "CODE: " + (identity.status || "UNKNOWN");
  $("runtime-identity").title = "Panel process identity only; root=" + (identity.plugin_root || "unknown") + " · source_sha256=" + (identity.source_tree_sha256 || "unknown");
  $("hermes-health").textContent = info.installation.hermes_found ? "FOUND" : "MISSING";
  $("hermes-health").className = info.installation.hermes_found ? "ok" : "warn";
  $("plugin-health").textContent = info.installation.detected ? "FOUND" : "NOT INSTALLED";
  $("plugin-health").className = info.installation.detected ? "ok" : "warn";
  state.profile = info.suggested_profile;
  clearProbe();
  state.overrides = info.current.saved_profile === state.profile ? {...info.current.overrides} : {};
  state.advanced = {};
  state.clearAdvanced.clear();
  state.backend = info.current.backend || "jev";
  state.provider = info.current.advanced.jev_provider || "openrouter";
  if (!state.advanced.reflex_backend && info.fresh) {
    state.advanced.reflex_backend = state.backend;
  }
  await refreshPreview();
}
function profileName(name) {
  return (state.info.profiles.find((p) => p.id === name) || {name:name}).name;
}
function currentAdvanced(key) {
  if (state.clearAdvanced.has(key)) return undefined;
  if (Object.prototype.hasOwnProperty.call(state.advanced,key)) return state.advanced[key];
  if (state.info.current.saved_profile === state.profile) return state.info.current.advanced[key];
  return undefined;
}
function featureEnabled(id) {
  return !!(state.resolved && state.resolved[id]);
}
function savedOverride(id) {
  return Object.prototype.hasOwnProperty.call(state.overrides, id) ? state.overrides[id] : null;
}
function switchHtml(id, disabled) {
  const shown = savedOverride(id) === null ? featureEnabled(id) : savedOverride(id);
  return '<input type="checkbox" role="switch" aria-label="Toggle ' + escapeHtml(id) +
    '" data-module="' + escapeHtml(id) + '"' + (shown ? " checked" : "") +
    (disabled ? " disabled" : "") + '>';
}
function toggleProfile(profile) {
  if (state.profile === profile) return;
  state.profile = profile;
  clearProbe();
  if (profile === "custom") {
    const all = {};
    state.info.modules.forEach((m) => { all[m.id] = featureEnabled(m.id); });
    state.overrides = all;
  } else {
    state.overrides = {};
  }
  state.advanced = {reflex_backend:state.backend};
  state.clearAdvanced.clear();
  state.dirty = true;
  refreshPreview();
}
function onToggleModule(id, enabled) {
  state.overrides[id] = enabled;
  state.dirty = true;
  refreshPreview();
}
function updateAdvanced(key, value) {
  if (key.startsWith("reflex_") || key === "jev_provider") clearProbe();
  state.advanced[key] = value;
  state.clearAdvanced.delete(key);
  state.dirty = true;
}
function selectBackend(value) {
  if (state.backend !== value) {
    clearProbe();
    state.setupPlan = null;
    state.setupJob = null;
  }
  state.backend = value;
  updateAdvanced("reflex_backend", value);
  if (value === "shadow") {
    state.overrides.shadow_testing = true;
    state.advanced.reflex_shadow_backend = currentAdvanced("reflex_shadow_backend") || "d1";
  }
  refreshPreview();
}
function createTitle(index, title, desc) {
  return '<div class="section-caption">STEP 0' + (index+1) +
    ' / ' + escapeHtml(["PROFILE", "FEATURES", "REFLEX", "REVIEW"][index]) +
    '</div><h1>' + escapeHtml(title) + '</h1><p class="lead">' + escapeHtml(desc) + '</p>';
}
function profilesPage() {
  let html = createTitle(0, "Choose how Nerve works.", "Start with Operator. Change profiles any time; existing Legacy setups are never switched without your confirmation.");
  html += '<div class="grid-profiles">';
  state.info.profiles.forEach((p) => {
    html += '<button class="profile-card' + (state.profile === p.id ? ' selected' : '') +
      '" data-profile="' + escapeHtml(p.id) + '" aria-pressed="' +
      (state.profile === p.id ? "true" : "false") + '"><div class="profile-top">' +
      escapeHtml(p.name) + (p.id === "operator" ? ' <span class="tag">DEFAULT</span>' : '') +
      '</div><p>' + escapeHtml(p.description) + '</p></button>';
  });
  html += '</div><div class="info-strip"><span>◇</span><p><strong>Your choice stays local.</strong> A profile determines which Nerve tools, checks and background modules are enabled. It does not change the main Hermes language model.</p></div>';
  html += statesHtml();
  if (state.info.current.profile === "legacy" && !state.info.fresh) {
    html += '<p class="feature-footnote">This installation currently uses Legacy. Switching to Operator removes legacy worker-supervision hooks unless you re-enable them in Features.</p>';
  }
  return html;
}
// Profile-specific feature visibility is server-driven (status().views /
// preview().reasons). The UI keeps NO hardcoded per-profile module rules; if
// the server provides no view, every module is shown rather than guessed.
function profileView() {
  return ((state.info || {}).views || {})[state.profile] || null;
}
function visibleModules() {
  const all = state.info.modules;
  // Full Configuration must show every ON and OFF module by default.
  if (state.profile === "custom" || state.showAdvanced) return all;
  const view = profileView();
  if (!view) return all;
  const primary = new Set(view.primary_modules);
  const setup = new Set(view.available_with_setup || []);
  return all.filter((m) => primary.has(m.id) || setup.has(m.id));
}
// "Why" for a module: shown only when the reason is more specific than the
// profile default (overrides, settings, missing dependencies, setup needs).
function moduleWhy(id) {
  const reasons = (state.preview || {}).reasons || {};
  const reason = reasons[id];
  if (!reason) return "";
  if (/( preset|existing configuration)$/.test(reason)) return "";
  return reason;
}
function statesHtml() {
  const s = (state.preview && state.preview.states) || ((state.info || {}).states) || null;
  if (!s) return "";
  const running = s.running || {};
  const runLabel = running.available
    ? "VERIFIED IN RUNNING HERMES"
    : "UNAVAILABLE (panel is a separate process)";
  const pending = s.pending_restart;
  const pendingLabel = (pending === null || pending === undefined)
    ? "UNKNOWN (verify from the in-Hermes panel)"
    : (pending ? "REQUIRED" : "CURRENT");
  return '<div class="states-strip" role="status">' +
    '<span><small>CONFIGURED</small>' + escapeHtml(s.configured.profile || "none saved") + '</span>' +
    '<span><small>EFFECTIVE</small>' + escapeHtml(s.effective.profile) + '</span>' +
    '<span><small>RUNNING</small>' + escapeHtml(runLabel) + '</span>' +
    '<span><small>RESTART</small>' + escapeHtml(pendingLabel) + '</span></div>';
}
function featuresPage() {
  const view = profileView();
  let html = createTitle(1, "Turn on only what you need.", "These are real runtime switches. Disabled modules stop registering their owned tools and hooks where supported.");
  const all = visibleModules();
  html += statesHtml();
  html += '<div class="card-box"><div class="card-heading"><h2>Features for ' +
    escapeHtml(profileName(state.profile)) + '</h2><span class="tag neutral">' +
    all.filter((m) => featureEnabled(m.id)).length + ' SHOWN ON</span></div>';
  if (view && view.emphasis) {
    html += '<p class="small-copy">' + escapeHtml(view.emphasis) + '</p>';
  }
  if (view && (view.available_with_setup || []).length) {
    html += '<p class="feature-footnote">Available after extra setup: ' +
      escapeHtml(view.available_with_setup.join(", ")) +
      '. Enable them from Advanced controls once their setup is complete.</p>';
  }
  for (const m of all) {
    const missing = (m.dependencies || []).filter((dep) => !featureEnabled(dep));
    const disabled = state.profile === "legacy";
    const why = moduleWhy(m.id);
    const forcedOff = state.overrides[m.id] === true && !featureEnabled(m.id);
    html += '<div class="feature-row"><div><h3>' + escapeHtml(m.name) +
      '</h3><p>' + escapeHtml(m.description) + '</p>';
    if (m.dependencies.length) {
      html += '<div class="detail">' + (missing.length ? "Requires: " : "Uses: ") +
        escapeHtml(m.dependencies.join(", ")) + '</div>';
    }
    if (why) {
      html += '<div class="detail">Why: ' + escapeHtml(why) + '</div>';
    }
    if (forcedOff) {
      const detail = (why && why.toLowerCase().indexOf("requires ") === 0)
        ? "Currently off because " + escapeHtml(why) + "."
        : "Currently off at runtime. Saved value: on. " + (why ? "Runtime reason: " + escapeHtml(why) + "." : "");
      html += '<div class="detail">' + detail + '</div>';
    }
    html += '</div><div class="switch-wrap">' + switchHtml(m.id, disabled) + '</div></div>';
  }
  html += '</div>';
  if (state.profile === "legacy") {
    html += '<div class="notice warning">Legacy is a compatibility path. Switch to a named profile to control individual features.</div>';
    html += '<button class="btn secondary small" id="migrate-legacy" type="button">Migrate to Operator profile</button>';
  }
  if (!state.showAdvanced && state.profile !== "custom") {
    html += '<p class="feature-footnote">Use Advanced controls to see all ' + state.info.modules.length + ' modules, including any not shown for this profile.</p>';
  }
  if (state.preview && state.preview.pruned.length) {
    html += '<div class="notice warning">Dependency note: ' +
      escapeHtml(state.preview.pruned.map((p) => p.id + " (" + p.reason + ")").join("; ")) +
      '</div>';
  }
  return html;
}
function optionHtml(value, text, chosen) {
  return '<option value="' + escapeHtml(value) + '"' + (value === chosen ? " selected" : "") + '>' +
    escapeHtml(text) + '</option>';
}
function advancedFields() {
  const keys = Object.keys(state.info.catalog).filter((k) =>
    !["nerve_profile","nerve_modules"].includes(k) &&
    k.toLowerCase().includes(state.advancedQuery.toLowerCase())).sort().slice(0,25);
  let html = '<div class="advanced-results">';
  keys.forEach((key) => {
    const cfg = state.info.catalog[key], typ = cfg.type, val = currentAdvanced(key);
    html += '<div class="advanced-setting"><label for="field-' + escapeHtml(key) + '">' +
      escapeHtml(key) + '</label><small>Type: ' + escapeHtml(typ) +
      '. Leave unchanged to use its current effective default.</small>';
    if (typ === "bool") {
      html += '<select data-advanced="' + escapeHtml(key) + '" data-type="' + escapeHtml(typ) +
        '" id="field-' + escapeHtml(key) + '">' +
        optionHtml("", "Inherit default", val === undefined ? "" : null) +
        optionHtml("true", "Enabled", val === true ? "true" : null) +
        optionHtml("false", "Disabled", val === false ? "false" : null) + '</select>';
    } else {
      const value = val === undefined ? "" : (typeof val === "object" ? JSON.stringify(val) : String(val));
      html += '<input id="field-' + escapeHtml(key) +
        '" type="text" data-advanced="' + escapeHtml(key) + '" data-type="' +
        escapeHtml(typ) + '" placeholder="' + escapeHtml(cfg.default) +
        '" value="' + escapeHtml(value) + '">';
    }
    html += '</div>';
  });
  html += '</div>';
  if (!keys.length) html += '<p class="small-copy">No settings match that search.</p>';
  return html;
}
function connectPage() {
  const isShadow = state.backend === "shadow";
  const localBackend = isShadow ? (currentAdvanced("reflex_shadow_backend") || "d1") : state.backend;
  let html = createTitle(2, "Test your decision engine.",
    "Choose a backend, then run a real typed decision. A health check alone never means inference works.");
  html += '<div class="section-caption">REFLEX BACKEND</div><div class="radio-pills">';
  const visibleBackends = (state.showAdvanced || state.showBackends) ? state.info.backend_options :
    state.info.backend_options.filter((backend) =>
      ["jev","d1","laya"].includes(backend) || backend === state.backend);
  visibleBackends.forEach((backend) => {
    html += '<button class="radio-tile ' + (state.backend === backend ? "selected" : "") +
      '" data-backend="' + escapeHtml(backend) + '" aria-pressed="' +
      (state.backend === backend ? "true" : "false") + '"><strong>' +
      escapeHtml(labels[backend] || backend) + '</strong><small>' +
      escapeHtml(about[backend] || "") + '</small></button>';
  });
  html += '</div>';
  if (!state.showBackends && !state.showAdvanced) {
    html += '<button class="btn secondary small compact-link" type="button" id="all-backends">More backends and shadow comparison</button>';
  }
  html += '<div class="card-box">';
  if (state.backend === "jev" || isShadow) {
    html += '<div class="field"><label for="jev-provider">Jev provider</label><select id="jev-provider">' +
      optionHtml("openrouter","OpenRouter",state.provider) +
      optionHtml("typesafe","TypeSafe",state.provider) +
      optionHtml("opencode","OpenCode",state.provider) +
      '</select><span class="note">Hosted Jev requires your configured credential. A real test may incur usage charges.</span></div>';
  }
  if (isShadow) {
    html += '<div class="field"><label for="shadow-route">Compare Jev with</label><select id="shadow-route">';
    ["laya","openjev","systemone","d1"].forEach((backend) => {
      html += optionHtml(backend, labels[backend], localBackend);
    });
    html += '</select></div>';
  }
  const baseKey = "reflex_" + localBackend + "_base_url";
  if (localDefaults[localBackend]) {
    html += '<div class="field"><label for="sidecar-url">Local backend URL (' + escapeHtml(localBackend) +
      ')</label><input id="sidecar-url" type="text" spellcheck="false" value="' +
      escapeHtml(currentAdvanced(baseKey) || localDefaults[localBackend]) +
      '"><span class="note">Only HTTP loopback addresses can be tested. A health response is not a model test.</span></div>';
  }
  html += '<div class="probe-actions"><button id="check-service" class="btn secondary small" type="button"' +
    (state.checkingBusy ? " disabled" : "") + '>Check service only</button>' +
    '<button id="run-real-test" class="btn primary small" type="button"' +
    (state.checkingBusy ? " disabled" : "") + '>' +
    (state.checkingBusy ? "Running decision..." : "Run real decision test") + '</button></div>';
  if (state.checking && state.checkingKey === probeScope()) {
    const result = state.checking, verified = !!result.verified;
    html += '<div id="backend-result" role="status" class="notice ' +
      (verified ? "" : "warning") + '"><strong>' + (verified ? "REAL INFERENCE PASSED" :
      (result.reachable ? "HTTP ONLY, NOT VERIFIED" : "NOT VERIFIED")) + '</strong><p>' +
      escapeHtml(result.message) + '</p>';
    if (result.comparison) {
      Object.entries(result.comparison).forEach(([name, item]) => {
        html += '<p><strong>' + escapeHtml(name) + ':</strong> ' + escapeHtml(item.message) + '</p>';
      });
    } else if (verified) {
      html += '<p>Reported model: ' + escapeHtml(result.response_model || "unreported") +
        ' · Choice: ' + escapeHtml(result.result?.choice || "-") +
        ' · Confidence: ' + escapeHtml(result.result?.confidence ?? "n/a") +
        ' · Latency: ' + escapeHtml(result.latency_ms ?? "?") + 'ms</p>';
      const tokens = result.usage || {};
      if (tokens.input_tokens !== undefined) html += '<p>Input tokens: ' +
        escapeHtml(tokens.input_tokens) + ' · Output tokens: ' + escapeHtml(tokens.output_tokens ?? "n/a") + '</p>';
    }
    html += '</div>';
  }
  html += '</div>';
  html += '<p class="feature-footnote"><a href="/guide" target="_blank" rel="noopener noreferrer">' +
    'Open the step-by-step local setup tutorial ↗</a></p>';
  html += '<div class="info-strip"><span>◇</span><p><strong>Explicit test, explicit usage.</strong> ' +
    'Check service only makes HTTP GET requests. Run real decision test submits one fixed, non-private ' +
    'typed-choice question to the selected model. Hosted Jev (including Shadow) may bill usage. ' +
    'Tests never change the active Hermes backend or install software.</p></div>';
  if (localDefaults[localBackend] && !isShadow) {
    html += '<div class="card-box"><h2>Get this local backend working</h2>' +
      '<p class="small-copy">1. Review requirements. 2. Install dependencies and start the model (if supported). ' +
      '3. Wait for ready. 4. Run real decision test above. 5. Apply your Nerve profile and restart Hermes.</p>' +
      '<button id="local-setup-preview" type="button" class="btn secondary small">Review one-click setup</button>';
    if (state.setupPlan && state.setupPlan.backend === localBackend) {
      const plan = state.setupPlan;
      html += '<div class="setup-guide"><p>' + escapeHtml(plan.description || plan.message) + '</p>';
      if (!plan.supported) {
        html += '<p class="notice warning">' + escapeHtml(plan.message) + '</p>';
        if (plan.guide_url) html += '<p><a href="' + escapeHtml(plan.guide_url) +
          '" rel="noopener noreferrer" target="_blank">Open official setup guide ↗</a></p>';
      } else {
        html += '<p><strong>Downloads:</strong> ' + escapeHtml(plan.download_note) +
          '</p><p><strong>Packages:</strong> ' + escapeHtml(plan.packages.join(", ")) + '</p>' +
          '<p><strong>Disk:</strong> ' + escapeHtml(plan.free_gb) + ' GB free, ' +
          escapeHtml(plan.min_free_gb) + ' GB minimum · <strong>Python:</strong> ' +
          escapeHtml(plan.python || "not available") + '</p>';
        html += '<p class="notice ' + (plan.can_install ? "" : "warning") +
          '">' + escapeHtml(plan.message) + '</p>';
        if (plan.can_install) {
          html += '<button id="local-setup-install" class="btn primary small" type="button">' +
            'Install dependencies &amp; start ' + escapeHtml(localBackend.toUpperCase()) + '</button>';
        }
      }
      html += '</div>';
    }
    if (state.setupJob && state.setupJob.backend === localBackend) {
      html += '<div class="notice ' + (state.setupJob.phase === "failed" ? "error" : "") +
        '">Setup: ' + escapeHtml(state.setupJob.phase) + ' · ' +
        escapeHtml(state.setupJob.message || "") + '</div>';
      if (state.setupJob.phase === "ready") {
        html += '<button class="btn secondary small" id="local-setup-stop" type="button">Stop this managed local model</button>';
      }
    }
    html += '</div>';
  }
  if (isShadow) {
    html += '<div class="card-box"><h2>Shadow testing</h2><p class="small-copy">' +
      'The real test sends the same typed question to hosted Jev and to the selected local comparator. ' +
      'Both must return valid answers to pass. Configure the local comparator first, then run the test. ' +
      'Shadow mode is a separate opt-in module and may incur hosted Jev charges.</p></div>';
  }
  if (state.showAdvanced) {
    html += '<div class="advanced-pane"><h2>All advanced settings</h2><p class="small-copy">Change provider behavior and precise limits. Values are validated against the plugin manifest.</p>' +
      '<div class="field"><label for="advanced-search">Search setting name</label><input id="advanced-search" value="' +
      escapeHtml(state.advancedQuery) + '" placeholder="e.g. nervous, selective, token"></div>' +
      '<div id="advanced-results">' + advancedFields() + '</div></div>';
  }
  return html;
}
function reviewPage() {
  let html = createTitle(3, "Review before you apply.", "No settings have changed yet. This is a preview of the effective Nerve profile and its module graph.");
  if (!state.preview) {
    return html + '<div class="notice error">Could not preview this configuration. Fix the error shown above before applying.</div>';
  }
  html += '<div class="card-box"><div class="section-caption" >CONFIGURATION SUMMARY</div>' +
    statesHtml() +
    '<div class="split"><div><span class="tiny-label muted">PROFILE</span><h2>' +
    escapeHtml(profileName(state.preview.profile)) + '</h2></div><div><span class="tiny-label muted">REFLEX ENGINE</span><h2>' +
    escapeHtml(labels[state.preview.backend] || state.preview.backend) + '</h2></div></div>' +
    '<p class="small-copy">Profile file: <span class="code-text">' +
    escapeHtml(state.preview.save_path) + '</span></p></div>' + (state.preview.conflict_note ?
    '<div class="notice warning">' + escapeHtml(state.preview.conflict_note) + '</div>' : '');
  if (state.preview.module_changes.length) {
    html += '<div class="section-caption">CHANGES</div><table class="line-table"><thead><tr><th>MODULE</th><th>BEFORE</th><th>AFTER</th></tr></thead><tbody>';
    state.preview.module_changes.forEach((change) => {
      html += '<tr><td>' + escapeHtml(change.name) + '</td><td class="' +
        (change.before ? "ok" : "muted") + '">' + (change.before ? "ON" : "OFF") +
        '</td><td class="' + (change.after ? "ok" : "muted") + '">' +
        (change.after ? "ON" : "OFF") + '</td></tr>';
    });
    html += '</tbody></table>';
  } else {
    html += '<div class="notice">No module activation differences. Saving may still update selected profile or advanced settings.</div>';
  }
  if (state.preview.old_backend !== state.preview.backend) {
    html += '<p class="feature-footnote">Reflex route changes from ' +
      escapeHtml(state.preview.old_backend) + ' to ' +
      escapeHtml(state.preview.backend) + '.</p>';
  }
  if (state.preview.pruned.length) {
    html += '<div class="notice warning">Unavailable dependencies: ' +
      escapeHtml(state.preview.pruned.map((p) => p.id + ": " + p.reason).join("; ")) + '</div>';
  }
  html += '<div class="info-strip"><span>◇</span><p><strong>Safe apply.</strong> The current profile and its recovery backup are preserved before saving. Hermes must restart to activate changes. The installer is a separate explicit action in Diagnostics.</p></div>';
  return html;
}
function successPage() {
  return '<div class="section-caption">SETUP COMPLETE</div><h1 class="success-title">Settings saved.</h1>' +
    '<p class="lead">Your profile configuration is on disk. Restart Hermes to load the changes.</p>' +
    '<div class="check-list"><div class="check-line">Previous configuration backed up</div>' +
    '<div class="check-line">Profile selected: ' + escapeHtml(profileName(state.profile)) + '</div>' +
    '<div class="check-line">Provider calls run only when explicitly tested</div></div>' +
    '<div class="card-box"><h2>Next step</h2><p class="small-copy">If Nerve is not installed in this Hermes home, use Diagnostics &amp; install. Otherwise restart Hermes and reopen this page to verify saved settings.</p>' +
    '<button class="btn secondary small" id="done-diagnostics">Open diagnostics</button></div>';
}
function diagnosticsPage() {
  const info = state.info, install = info.installation;
  let html = '<div class="section-caption">LOCAL HEALTH &amp; INSTALLATION</div><h1>System diagnostics.</h1>' +
    '<p class="lead">Review the Hermes home, installation target, available backups and connection state.</p>' +
    '<div class="card-box"><div class="readout-row"><span>HERMES HOME</span><strong class="code-text">' +
    escapeHtml(info.home) + '</strong></div><div class="readout-row"><span>PLUGIN LOCATION</span><strong class="code-text">' +
    escapeHtml(install.path) + '</strong></div><div class="readout-row"><span>HERMES CLI</span><strong>' +
    (install.hermes_found ? 'Available' : 'Not detected') +
    '</strong></div><div class="readout-row"><span>INSTALLED PLUGIN</span><strong>' +
    (install.detected ? 'Detected on disk' : 'Not detected at expected location') +
    '</strong></div><div class="readout-row"><span>RUNNING STATE</span><strong>Not verified</strong></div></div>';
  info.warnings.forEach((warning) => { html += '<div class="notice warning">' + escapeHtml(warning) + '</div>'; });
  if (info.conflict && info.conflict.message) {
    html += '<div class="notice warning" id="config-conflict">' + escapeHtml(info.conflict.message) + '</div>';
  }
  html += '<div class="card-box"><h2>Install or upgrade Nerve</h2>' +
    '<p class="small-copy">Review source and destination before copying files. Existing files are preserved in a separate backup. Nothing is installed until you press Install.</p>' +
    '<button class="btn secondary small" id="install-preview">Review installation</button>';
  if (state.installPreview) {
    const plan = state.installPreview;
    html += '<div class="notice ' + (plan.already_installed ? "" : "warning") +
      '">Source: ' + escapeHtml(plan.source) +
      ' · Destination: ' + escapeHtml(plan.destination) +
      (plan.already_installed ? " · This is the currently installed plugin. No copy is needed." :
        (plan.replaces_existing ? " · Previous copy will be backed up." :
          " · Fresh install.") +
        (plan.initialize_operator ? " · Operator will be selected." : "")) + '</div>';
    if (!plan.already_installed) {
      html += '<button class="btn primary" id="install-now">Install Nerve into Hermes</button>';
    }
  }
  html += '</div><div class="card-box"><h2>Restore a previous profile</h2>';
  if (!info.backups.length) {
    html += '<p class="small-copy">No Control Panel backups yet. Each Apply operation saves one automatically.</p>';
  } else {
    html += '<p class="small-copy">Restoring saves another backup of the current state. Hermes restart required.</p>';
    info.backups.forEach((backup) => {
      html += '<div class="feature-row"><div class="code-text">' +
        escapeHtml(backup.id) + '</div><button class="btn secondary small" data-restore="' +
        escapeHtml(backup.id) + '">Restore</button></div>';
    });
  }
  html += '</div>';
  return html;
}
function renderSide() {
  $("readout-profile").textContent = profileName(state.profile);
  $("readout-modules").textContent = state.resolved ?
    Object.values(state.resolved).filter(Boolean).length + " / " + state.info.modules.length : "—";
  $("readout-backend").textContent = labels[state.backend] || state.backend;
  $("readout-dirty").textContent = state.saved ? "SAVED" : (state.dirty ? "PREVIEW" : "NONE");
  $("progress").textContent = state.screen === "diagnostics" ? "DIAGNOSTICS" :
    "STEP 0" + (state.step+1) + " / 04";
  document.querySelectorAll("[data-step]").forEach((button) => {
    button.classList.toggle("current", state.screen === "setup" && Number(button.dataset.step) === state.step);
  });
}
function render() {
  if (!state.info) return;
  renderSide();
  $("content").innerHTML = state.screen === "diagnostics" ? diagnosticsPage() :
    state.saved ? successPage() :
    [profilesPage,featuresPage,connectPage,reviewPage][state.step]();
  const bar = $("action-bar");
  bar.hidden = state.screen === "diagnostics" || state.saved;
  $("back").disabled = state.step === 0;
  $("next").textContent = state.step === 3 ? "Apply profile" : "Continue →";
  $("next").disabled = state.busy || (state.step === 3 && !state.preview);
  $("bar-note").textContent = state.busy ? "Working..." : (state.step === 3 ? "Backup created on Apply." : "Nothing saved yet.");
  bindPage();
}
function bindPage() {
  document.querySelectorAll("[data-profile]").forEach((btn) =>
    btn.addEventListener("click", () => toggleProfile(btn.dataset.profile)));
  document.querySelectorAll("[data-module]").forEach((input) =>
    input.addEventListener("change", () => onToggleModule(input.dataset.module, input.checked)));
  document.querySelectorAll("[data-backend]").forEach((btn) =>
    btn.addEventListener("click", () => selectBackend(btn.dataset.backend)));
  const provider = $("jev-provider");
  if (provider) provider.addEventListener("change", () => {
    state.provider = provider.value;
    clearProbe();
    updateAdvanced("jev_provider", provider.value);
    refreshPreview();
  });
  const shadow = $("shadow-route");
  if (shadow) shadow.addEventListener("change", () => {
    clearProbe();
    updateAdvanced("reflex_shadow_backend", shadow.value);
    state.setupPlan = null;
    refreshPreview();
  });
  const local = $("sidecar-url");
  if (local) {
    local.addEventListener("input", () => {
      const target = state.backend === "shadow" ?
        (currentAdvanced("reflex_shadow_backend") || "d1") : state.backend;
      updateAdvanced("reflex_" + target + "_base_url", local.value);
    });
    local.addEventListener("change", refreshPreview);
  }
  const more = $("all-backends");
  if (more) more.addEventListener("click", () => {
    state.showBackends = true;
    render();
  });
  async function runProbe(mode) {
    if (state.checkingBusy) return;
    if (mode === "live" && ["jev","shadow"].includes(state.backend) &&
        !window.confirm("This will make a REAL hosted Jev inference request using " +
          state.provider + ". It may incur provider usage. Continue?")) return;
    const scope = probeScope();
    const comparator = currentAdvanced("reflex_shadow_backend") || "d1";
    const backend = state.backend === "shadow" ? comparator : state.backend;
    const localField = $("sidecar-url");
    const localUrl = localField ? localField.value :
      (currentAdvanced("reflex_" + backend + "_base_url") || localDefaults[backend] || undefined);
    state.checkingBusy = true;
    clearProbe();
    render();
    try {
      const result = await api("/api/test-backend", {
        backend:state.backend, compare_backend:comparator,
        provider:state.provider, base_url:localUrl, mode,
      });
      if (scope === probeScope()) {
        state.checking = result;
        state.checkingKey = scope;
        message(result.message, result.verified ? "" : "warning");
      }
    } catch(error) {
      if (scope === probeScope()) handleError(error);
    } finally {
      state.checkingBusy = false;
      render();
    }
  }
  const checkService = $("check-service");
  if (checkService) checkService.addEventListener("click", () => runProbe("health"));
  const runReal = $("run-real-test");
  if (runReal) runReal.addEventListener("click", () => runProbe("live"));
  const localPreview = $("local-setup-preview");
  if (localPreview) localPreview.addEventListener("click", async () => {
    const target = state.backend;
    try {
      const result = await api("/api/local-setup-preview", {backend:target});
      if (state.backend !== target) return;
      state.setupPlan = result;
      state.setupJob = result.job || null;
      message("");
      render();
    } catch(error) { handleError(error); }
  });
  const localStop = $("local-setup-stop");
  if (localStop) localStop.addEventListener("click", async () => {
    if (!window.confirm("Stop the model started by Nerve on this computer?")) return;
    try {
      const answer = await api("/api/local-setup-stop", {backend:state.backend, confirm_stop:true});
      state.setupJob = {backend:state.backend, phase:"stopped", message:answer.message};
      message(answer.message);
      render();
    } catch(error) {handleError(error);}
  });
  const localInstall = $("local-setup-install");
  if (localInstall) localInstall.addEventListener("click", async () => {
    const plan = state.setupPlan;
    if (!plan || !plan.can_install) return;
    const prompt = "Install " + plan.backend.toUpperCase() + " locally?\n\n" +
      plan.download_note + "\n\nPackages: " + plan.packages.join(", ") +
      "\nDestination: " + plan.runtime_dir +
      "\n\nThis downloads third-party Python dependencies and model weights. " +
      (plan.requires_remote_code_consent ?
        "D1 ALSO executes Python supplied by its Hugging Face model repository. Review its license and code before proceeding. " : "") +
      "No hosted API usage is needed. Continue?";
    if (!window.confirm(prompt)) return;
    try {
      const result = await api("/api/local-setup-install", {
        backend:plan.backend, plan_id:plan.plan_id, approve_downloads:true,
        approve_remote_code:!!plan.requires_remote_code_consent,
      });
      state.setupJob = result;
      state.setupPlan = null;
      message(result.message);
      render();
      pollLocalSetup(plan.backend);
    } catch(error) {handleError(error);}
  });
  const search = $("advanced-search");
  if (search) search.addEventListener("input", () => {
    state.advancedQuery = search.value;
    $("advanced-results").innerHTML = advancedFields();
    bindAdvanced();
  });
  bindAdvanced();
  const installPreview = $("install-preview");
  const migrate = document.getElementById("migrate-legacy");
  if (migrate) migrate.addEventListener("click", () => {
    state.profile = "operator";
    state.overrides = {};
    state.advanced = {reflex_backend:state.backend};
    state.clearAdvanced.clear();
    state.dirty = true;
    state.step = 0;
    refreshPreview();
  });
  if (installPreview) installPreview.addEventListener("click", async () => {
    try { state.installPreview = await api("/api/install-preview", {}); message(""); }
    catch(error){handleError(error);}
    render();
  });
  const installNow = $("install-now");
  if (installNow) installNow.addEventListener("click", async () => {
    if (!state.installPreview) return;
    if (state.installPreview.already_installed) return;
    const prompt = "Install Nerve into " + state.installPreview.destination +
      "? Existing plugin files will be backed up. Hermes must restart.";
    if (!window.confirm(prompt)) return;
    installNow.disabled = true;
    try {
      const done = await api("/api/install", {preview_id:state.installPreview.preview_id});
      state.installPreview = null;
      state.info = await api("/api/status");
      message(done.message, "");
      render();
    } catch(error) {handleError(error);render();}
  });
  document.querySelectorAll("[data-restore]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!window.confirm("Restore backup " + btn.dataset.restore + "? Current settings will also be backed up.")) return;
    try {
      const done = await api("/api/rollback", {id:btn.dataset.restore});
      await refreshStatus();
      message(done.message, "");
      render();
    } catch(error) {handleError(error);}
  }));
  const doneDiag = $("done-diagnostics");
  if (doneDiag) doneDiag.addEventListener("click", () => { state.screen="diagnostics"; render(); });
}
function bindAdvanced() {
  document.querySelectorAll("[data-advanced]").forEach((input) => input.addEventListener("change", () => {
    const key=input.dataset.advanced, kind=input.dataset.type, raw=input.value.trim();
    if (!raw) {
      delete state.advanced[key]; state.clearAdvanced.add(key); refreshPreview(); return;
    }
    let val = raw;
    try {
      if (kind === "bool") val = raw === "true";
      if (kind === "int") {
        if (!/^-?\d+$/.test(raw)) throw new Error("Expected integer.");
        val = Number(raw);
      }
      if (kind === "float") {
        val = Number(raw);
        if (!Number.isFinite(val)) throw new Error("Expected number.");
      }
      if (kind === "dict" || kind === "list") val = JSON.parse(raw);
      updateAdvanced(key,val);
      refreshPreview();
    } catch(error) { handleError(error); }
  }));
}
async function next() {
  if (state.busy) return;
  if (state.step < 3) {
    state.step++;
    state.screen="setup";
    render();
    return;
  }
  state.busy=true; render();
  try {
    const refreshed = await api("/api/preview", request());
    state.preview = refreshed;
    const result = await api("/api/apply", {...request(), preview_id:refreshed.preview_id});
    state.saved=true;
    state.dirty=false;
    message(result.message);
    state.info=await api("/api/status");
  } catch(error) { handleError(error); }
  state.busy=false; render();
}
function showDiagnostics() {
  state.screen = "diagnostics";
  state.saved=false;
  render();
}
function pollLocalSetup(backend) {
  if (state.setupPollTimer) clearTimeout(state.setupPollTimer);
  async function tick() {
    try {
      const job = await api("/api/local-setup-status?backend=" + encodeURIComponent(backend));
      if (state.backend !== backend) return;
      state.setupJob = job;
      render();
      if (["queued","creating_environment","installing_packages","loading_model","verifying_inference"].includes(job.phase)) {
        state.setupPollTimer = setTimeout(tick, 2500);
      } else if (job.phase === "ready") {
        message("Local model is ready and real inference passed. Run your own decision test above.");
      } else if (job.phase === "failed") {
        message(job.message || "Local installation failed. Check logs.", "error");
      }
    } catch(error) {
      handleError(error);
    }
  }
  state.setupPollTimer = setTimeout(tick, 1000);
}
async function start() {
  state.token = getToken();
  if (!state.token) {
    $("content").innerHTML = '<h1>Session link missing.</h1><p class="lead">Open the local URL printed by the Nerve command. Each launch uses a new authorization token.</p>';
    $("action-bar").hidden = true;
    return;
  }
  $("next").addEventListener("click", next);
  $("back").addEventListener("click", () => {if (state.step > 0) {state.step--;render();}});
  $("diagnostics-button").addEventListener("click", showDiagnostics);
  $("diagnostics-shortcut").addEventListener("click", showDiagnostics);
  $("advanced-switch").addEventListener("change", (event) => {
    state.showAdvanced = event.target.checked;
    $("advanced-shortcut").setAttribute("aria-pressed", String(state.showAdvanced));
    render();
  });
  $("advanced-shortcut").addEventListener("click", () => {
    state.showAdvanced = !state.showAdvanced;
    $("advanced-switch").checked = state.showAdvanced;
    $("advanced-shortcut").setAttribute("aria-pressed", String(state.showAdvanced));
    render();
  });
  document.querySelectorAll("[data-step]").forEach((btn) => btn.addEventListener("click", () => {
    state.screen="setup"; state.saved=false; state.step=Number(btn.dataset.step); render();
  }));
  try {
    await refreshStatus();
    state.info.warnings.forEach((warning) => message(warning,"warning"));
    if (state.info.conflict && state.info.conflict.message) message(state.info.conflict.message, "warning");
  } catch(error) {
    handleError(error);
    $("content").innerHTML = '<h1>Unable to read Nerve.</h1><p class="lead">Review the diagnostic error above, and restart this local Control Panel.</p>';
    $("action-bar").hidden=true;
  }
}
window.addEventListener("DOMContentLoaded", start);
