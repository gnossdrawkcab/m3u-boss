/* ═══════════════════════════════════════════════════════════════════════
   M3U Boss — Frontend Application
   Professional UI with toast notifications, loading states, smooth UX
   ═══════════════════════════════════════════════════════════════════════ */

const $ = (s, p) => (p || document).querySelector(s);
const $$ = (s, p) => [...(p || document).querySelectorAll(s)];
const esc = (s) => { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; };
function timeAgo(iso) {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/* ── Shift-click range select helper ──────────────────────────────────── */
const _shiftState = {};
function wireShiftSelect(selector, container, onChange) {
  const key = selector + (container ? "_scoped" : "");
  if (!_shiftState[key]) _shiftState[key] = { last: null };
  const state = _shiftState[key];
  const boxes = $$(selector, container || document);
  boxes.forEach(cb => {
    cb.addEventListener("click", (e) => {
      if (e.shiftKey && state.last) {
        const all = $$(selector, container || document);
        const from = all.indexOf(state.last);
        const to = all.indexOf(cb);
        if (from !== -1 && to !== -1) {
          const [lo, hi] = from < to ? [from, to] : [to, from];
          for (let i = lo; i <= hi; i++) all[i].checked = cb.checked;
        }
      }
      state.last = cb;
      if (onChange) onChange(cb);
    });
  });
}

/* ── SVG Icons ────────────────────────────────────────────────────────── */
const ICO = {
  check:  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>',
  x:      '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  info:   '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
  grip:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg>',
  chev:   '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>',
  pin:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16h14v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1h1V3H7v3h1a1 1 0 0 1 1 1z"/></svg>',
  trash:  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V3h6v3"/></svg>',
  up:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
};

/* ── State ────────────────────────────────────────────────────────────── */
let currentPage = "dashboard";
let expandedGroups = new Set();
let channelCache = {};
let groupsData = [];
let categoriesData = [];
let groupOrderHealth = null;
let groupOrderHealthById = new Map();
let searchResults = [];
let searchSelected = new Set();
let toastId = 0;
const INITIAL_PAGE = location.hash.replace("#", "");
const _scriptLoads = new Map();
const _apiGetInflight = new Map();
const _pageLoadedAt = {};

function ensureScript(src, globalName) {
  if (globalName && window[globalName]) return Promise.resolve();
  if (_scriptLoads.has(src)) return _scriptLoads.get(src);
  const promise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Could not load ${globalName || src}`));
    document.head.appendChild(script);
  });
  _scriptLoads.set(src, promise);
  return promise;
}

/* ── Fetch categories from settings ──────────────────────────────────── */
async function fetchCategories() {
  try { categoriesData = await api("/api/settings/group-categories"); } catch(_) { categoriesData = []; }
  return categoriesData;
}

/* ── Fetch groups and unpack from {groups, total_channels} envelope ──── */
async function fetchGroups() {
  const d = await api("/api/groups");
  // Handle both envelope {groups:[...], total_channels:N} and bare array [...]
  groupsData = Array.isArray(d) ? d : (d.groups || []);
  const total = Array.isArray(d) ? null : d.total_channels;
  if (total != null) {
    const el = $("#ch-count-label"); if (el) el.textContent = `${total.toLocaleString()} channels`;
    const nb = $("#nav-ch-count"); if (nb) nb.textContent = total;
  }
  return groupsData;
}

async function fetchGroupOrderHealth() {
  groupOrderHealth = await api("/api/groups/order-health");
  groupOrderHealthById = new Map((groupOrderHealth?.groups || []).map(row => [row.id, row]));
  renderGroupOrderHealth();
  return groupOrderHealth;
}

/* ═══════════════════════════════════════════════════════════════════════
   TOAST SYSTEM — non-blocking notifications
   ═══════════════════════════════════════════════════════════════════════ */
function toast(type, title, msg, duration = 4000) {
  const id = ++toastId;
  const iconMap = { success: ICO.check, error: ICO.x, info: ICO.info, loading: "" };
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.dataset.id = id;
  el.innerHTML = `
    <div class="toast-icon">${iconMap[type] || ""}</div>
    <div class="toast-body">
      <div class="toast-title">${esc(title)}</div>
      ${msg ? `<div class="toast-msg">${esc(msg)}</div>` : ""}
    </div>
    <button class="toast-close" onclick="dismissToast(${id})">&times;</button>`;
  $("#toast-container").appendChild(el);
  if (type !== "loading" && duration > 0) {
    setTimeout(() => dismissToast(id), duration);
  }
  return id;
}

function dismissToast(id) {
  const el = $(`.toast[data-id="${id}"]`);
  if (!el) return;
  el.classList.add("removing");
  setTimeout(() => el.remove(), 200);
}

function updateToast(id, type, title, msg, duration = 3000) {
  const el = $(`.toast[data-id="${id}"]`);
  if (!el) return;
  const iconMap = { success: ICO.check, error: ICO.x, info: ICO.info };
  el.className = `toast ${type}`;
  el.querySelector(".toast-icon").innerHTML = iconMap[type] || "";
  el.querySelector(".toast-title").textContent = title;
  const bodyMsg = el.querySelector(".toast-msg");
  if (msg) {
    if (bodyMsg) bodyMsg.textContent = msg;
    else el.querySelector(".toast-body").innerHTML += `<div class="toast-msg">${esc(msg)}</div>`;
  } else if (bodyMsg) { bodyMsg.remove(); }
  if (duration > 0) setTimeout(() => dismissToast(id), duration);
}

window.dismissToast = dismissToast;

/* ═══════════════════════════════════════════════════════════════════════
   CONFIRM DIALOG — replaces browser confirm()
   ═══════════════════════════════════════════════════════════════════════ */
function confirmDialog(title, msg, dangerLabel = "Delete") {
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    overlay.innerHTML = `
      <div class="dialog">
        <div class="dialog-title">${esc(title)}</div>
        <div class="dialog-msg">${esc(msg)}</div>
        <div class="dialog-actions">
          <button class="btn" id="dlg-cancel">Cancel</button>
          <button class="btn btn-danger" id="dlg-confirm">${esc(dangerLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector("#dlg-cancel").onclick = () => { overlay.remove(); resolve(false); };
    overlay.querySelector("#dlg-confirm").onclick = () => { overlay.remove(); resolve(true); };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) { overlay.remove(); resolve(false); } });
  });
}

/* ═══════════════════════════════════════════════════════════════════════
   BUTTON LOADING STATES
   ═══════════════════════════════════════════════════════════════════════ */
function btnLoad(btn, text) {
  if (!btn) return;
  btn._origHTML = btn.innerHTML;
  btn.innerHTML = `<span class="spinner"></span> ${esc(text || "Loading...")}`;
  btn.disabled = true;
  btn.classList.add("loading");
}
function btnDone(btn) {
  if (!btn || !btn._origHTML) return;
  btn.innerHTML = btn._origHTML;
  btn.disabled = false;
  btn.classList.remove("loading");
}

/* ═══════════════════════════════════════════════════════════════════════
   API HELPER — with toast feedback
   ═══════════════════════════════════════════════════════════════════════ */
async function api(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  if (method === "GET" && _apiGetInflight.has(url)) return _apiGetInflight.get(url);
  const request = (async () => {
    const controller = new AbortController();
    const timeoutMs = opts.timeout || (method === "GET" ? 45000 : 1200000);
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const r = await fetch(url, {
        headers: { "Content-Type": "application/json", ...opts.headers },
        ...opts,
        signal: opts.signal || controller.signal,
      });
      const raw = await r.text();
      let body = null;
      if (raw) {
        try { body = JSON.parse(raw); } catch (_) { body = null; }
      }
      if (!r.ok) {
        const err = body || { detail: raw || r.statusText };
        throw new Error(err.detail || r.statusText);
      }
      if (method !== "GET") Object.keys(_pageLoadedAt).forEach(k => delete _pageLoadedAt[k]);
      return body;
    } catch (err) {
      if (err?.name === "AbortError") throw new Error("Request timed out");
      throw err;
    } finally {
      clearTimeout(timeout);
      if (method === "GET") _apiGetInflight.delete(url);
    }
  })();
  if (method === "GET") _apiGetInflight.set(url, request);
  return request;
}

/* ═══════════════════════════════════════════════════════════════════════
   NAVIGATION
   ═══════════════════════════════════════════════════════════════════════ */
$$(".nav-link").forEach(link => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    navigate(link.dataset.page);
    // Close mobile sidebar
    document.getElementById("sidebar").classList.remove("open");
    document.getElementById("mobile-overlay").classList.remove("open");
  });
});

// Mobile hamburger toggle
document.getElementById("mobile-nav-toggle").addEventListener("click", () => {
  document.getElementById("sidebar").classList.toggle("open");
  document.getElementById("mobile-overlay").classList.toggle("open");
});
document.getElementById("mobile-overlay").addEventListener("click", () => {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("mobile-overlay").classList.remove("open");
});

function navigate(page, force = false) {
  if (!pageLoaders[page]) page = "dashboard";
  currentPage = page;
  if (location.hash !== `#${page}`) history.pushState(null, "", `#${page}`);
  $$(".nav-link").forEach(l => l.classList.toggle("active", l.dataset.page === page));
  $$(".page").forEach(p => p.classList.toggle("active", p.id === `page-${page}`));
  const age = Date.now() - (_pageLoadedAt[page] || 0);
  const ttl = ({ dashboard: 60000, export: 60000, guide: GUIDE_FRESH_MS })[page] || 300000;
  if (force || !_pageLoadedAt[page] || age > ttl) {
    Promise.resolve(pageLoaders[page]?.()).then(() => { _pageLoadedAt[page] = Date.now(); });
  }
}

const pageLoaders = {
  dashboard: loadDashboard,
  sources: loadSources,
  editor: loadEditor,
  favorites: loadFavorites,
  epg: loadEPG,
  guide: loadGuide,
  export: loadExportPage,
  teamarr: loadTeamarr,
  safety: () => window.M3UBossFeatures?.loadSafetyCenter(),
  settings: loadRefreshSettings,
};

// Handle browser back/forward and initial hash on page load
window.addEventListener("hashchange", () => {
  const page = location.hash.replace("#", "") || "dashboard";
  if (page !== currentPage) navigate(page);
});

/* ═══════════════════════════════════════════════════════════════════════
   DASHBOARD
   ═══════════════════════════════════════════════════════════════════════ */
const DASHBOARD_CACHE_KEY = "m3u.dashboard.snapshot.v1";
let _dashboardLoadToken = 0;

function _readDashboardSnapshot() {
  try {
    const raw = localStorage.getItem(DASHBOARD_CACHE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

function _writeDashboardSnapshot(data) {
  try {
    localStorage.setItem(DASHBOARD_CACHE_KEY, JSON.stringify({ ...data, cachedAt: new Date().toISOString() }));
  } catch (_) {
    // Ignore quota/storage failures.
  }
}

function _renderDashboardShell() {
  const el = $("#dashboard-stats");
  const setLoading = (id) => { const n = $(id); if (n) n.innerHTML = '<span class="text-muted">Loading...</span>'; };
  el.innerHTML = Array(6).fill('<div class="stat-card"><div class="skeleton" style="height:42px;width:60px;margin-bottom:6px"></div><div class="skeleton" style="height:12px;width:80px"></div></div>').join("");
  setLoading("#dashboard-coverage");
  setLoading("#dashboard-reliability");
  setLoading("#dashboard-trend");
  setLoading("#dashboard-alerts");
}

function _renderDashboardPrimary(d, history = [], sysErrors = { errors: [] }, coverage = null, reliability = [], epgDash = null) {
  const el = $("#dashboard-stats");
  const s = d.stats || {};
  el.innerHTML = [
    statCard(s.total_channels ?? 0, "Total Channels", ""),
    statCard(s.enabled_channels ?? 0, "Enabled", "success"),
    statCard(s.favorite_channels ?? 0, "Favorites", "warning"),
    statCard(s.total_groups ?? 0, "Groups", ""),
    statCard(d.sources?.length ?? 0, "Sources", "accent"),
    statCard(d.teamarr_enabled ? "ON" : "OFF", "Teamarr", d.teamarr_enabled ? "success" : ""),
  ].join("");

  const nb1 = $("#nav-src-count"); if (nb1) nb1.textContent = d.sources?.length ?? 0;
  const nb2 = $("#nav-ch-count"); if (nb2) nb2.textContent = s.total_channels ?? 0;

  const h = d.health || {};
  const syncAgo = h.last_sync ? _timeAgo(new Date(h.last_sync)) : "Never";
  const nextSync = h.next_sync_seconds ? _fmtDuration(h.next_sync_seconds) : "—";
  const exportState = h.export_status || "idle";
  const coveragePct = coverage?.coverage_pct ?? 0;
  $("#dash-title").textContent = !(d.sources || []).length
    ? "Welcome to M3U Boss"
    : exportState === "error"
    ? "Export pipeline needs attention"
    : coverage
      ? (coveragePct >= 85 ? "EPG quality is in a strong state" : "Monitoring quality and sync drift")
      : "Loading pipeline health";
  $("#dash-sub").textContent = !(d.sources || []).length
    ? "Start with one M3U or Xtream source; integrations can wait until the core lineup works."
    : `Last sync ${syncAgo} • next refresh in ${nextSync} • ${s.enabled_channels ?? 0} enabled channels`;
  $("#dashboard-health-chips").innerHTML = [
    `<span class="dash-chip ${exportState === "ok" ? "ok" : exportState === "error" ? "bad" : "warn"}">Export: ${esc(String(exportState).toUpperCase())}</span>`,
    `<span class="dash-chip ${coverage ? (coveragePct >= 85 ? "ok" : coveragePct >= 60 ? "warn" : "bad") : "warn"}">Coverage: ${coverage ? `${coveragePct}%` : "Loading"}</span>`,
    `<span class="dash-chip ${Array.isArray(reliability) && reliability.length ? "ok" : "warn"}">Sources: ${(d.sources || []).length}</span>`,
    `<span class="dash-chip ${sysErrors?.errors?.length ? "bad" : "ok"}">Errors: ${(sysErrors?.errors || []).length}</span>`,
  ].join("");

  _renderDashboardTrend(history || []);
  _renderDashboardAlerts(d, coverage, epgDash, sysErrors || { errors: [] });
  renderDashboardSource(d);
  _wireDashboardActions();
}

async function loadDashboard() {
  const token = ++_dashboardLoadToken;
  const el = $("#dashboard-stats");
  _renderDashboardShell();
  const snapshot = _readDashboardSnapshot();
  if (snapshot?.dashboard) {
    _renderDashboardPrimary(
      snapshot.dashboard,
      snapshot.history || [],
      snapshot.sysErrors || { errors: [] },
      snapshot.coverage || null,
      snapshot.reliability || [],
      snapshot.epgDash || null,
    );
    if (snapshot.coverage) _renderDashboardCoverage(snapshot.coverage, snapshot.epgDash || null);
    if (snapshot.reliability) _renderDashboardReliability(snapshot.reliability || []);
  }
  try {
    const [d, history, sysErrors] = await api("/api/bootstrap").then(b => {
      const srcBadge = $("#nav-src-count");
      if (srcBadge) srcBadge.textContent = b.source_count ?? (b.dashboard?.sources || []).length;
      const chBadge = $("#nav-ch-count");
      if (chBadge) chBadge.textContent = b.channel_count ?? (b.dashboard?.stats?.total_channels || 0);
      return [b.dashboard, b.history || [], b.errors || { errors: [] }];
    }).catch(() => Promise.all([
      api("/api/dashboard"),
      api("/api/export/history?limit=20").catch(() => []),
      api("/api/system/errors?limit=30").catch(() => ({ errors: [] })),
    ]));
    if (token !== _dashboardLoadToken) return;
    _renderDashboardPrimary(d, history, sysErrors);
    _writeDashboardSnapshot({ dashboard: d, history, sysErrors });

    Promise.all([
      api("/api/epg/coverage").catch(() => null),
      api("/api/epg/dashboard").catch(() => null),
      api("/api/sources/reliability").catch(() => []),
    ]).then(([coverage, epgDash, reliability]) => {
      if (token !== _dashboardLoadToken) return;
      _renderDashboardPrimary(d, history, sysErrors, coverage, reliability, epgDash);
      _renderDashboardCoverage(coverage, epgDash);
      _renderDashboardReliability(reliability);
      _writeDashboardSnapshot({ dashboard: d, history, sysErrors, coverage, epgDash, reliability });
    });
  } catch (e) {
    if (snapshot?.dashboard) return;
    el.innerHTML = `<div class="card"><div class="card-empty"><div class="card-empty-text">Failed to load dashboard: ${esc(e.message)}</div></div></div>`;
  }
}

function _wireDashboardActions() {
  $("#dash-refresh-now")?.addEventListener("click", async () => {
    const btn = $("#dash-refresh-now");
    btnLoad(btn, "Refreshing...");
    const tid = toast("loading", "Refreshing sources", "Reimporting active sources and rebuilding exports...");
    try {
      await api("/api/refresh-all", { method: "POST" });
      updateToast(tid, "success", "Sources refreshed");
      loadDashboard();
    } catch (e) {
      updateToast(tid, "error", "Refresh failed", e.message);
    } finally { btnDone(btn); }
  }, { once: true });

  $("#dash-run-coverage")?.addEventListener("click", async () => {
    navigate("epg");
    setTimeout(() => runEpgCoverage(), 120);
  }, { once: true });

  $("#dash-open-guide")?.addEventListener("click", () => navigate("guide"), { once: true });
}

function _renderDashboardCoverage(coverage, epgDash) {
  const el = $("#dashboard-coverage");
  if (!el) return;
  if (!coverage) {
    el.innerHTML = '<span class="text-muted">Coverage data unavailable.</span>';
    return;
  }
  const pct = coverage.coverage_pct ?? 0;
  const color = pct >= 85 ? "var(--success)" : pct >= 60 ? "var(--warning)" : "var(--danger)";
  const worst = (epgDash?.groups || []).slice(0, 5);
  el.innerHTML = `
    <div class="dash-meter">
      <div class="dash-meter-bar"><span style="width:${pct}%;background:${color}"></span></div>
      <div class="dash-meter-label"><b>${pct}%</b> real guide coverage</div>
    </div>
    <div class="dash-mini-grid">
      <div><span class="text-muted">Real</span><b>${(coverage.matched || 0).toLocaleString()}</b></div>
      <div><span class="text-muted">Dummy</span><b>${(coverage.dummy || 0).toLocaleString()}</b></div>
      <div><span class="text-muted">No EPG</span><b>${(coverage.unmatched || 0).toLocaleString()}</b></div>
      <div><span class="text-muted">Missing XML</span><b>${(coverage.missing_in_xml || 0).toLocaleString()}</b></div>
    </div>
    ${worst.length ? `<div class="dash-list">${worst.map(g => `<div class="dash-list-row"><span>${esc(g.group)}</span><b>${g.score}%</b></div>`).join("")}</div>` : ""}
  `;
}

function _renderDashboardReliability(rows) {
  const el = $("#dashboard-reliability");
  if (!el) return;
  if (!rows || !rows.length) {
    el.innerHTML = '<span class="text-muted">No source reliability data yet.</span>';
    return;
  }
  const top = rows.slice(0, 8);
  el.innerHTML = `<div class="dash-list">${top.map(r => {
    const cls = r.score >= 80 ? "ok" : r.score >= 55 ? "warn" : "bad";
    return `<div class="dash-list-row"><span>${esc(r.name || "Source")}</span><div style="display:flex;align-items:center;gap:8px"><span class="dash-pill ${cls}">${r.score}</span><span class="text-muted">${r.coverage}%</span></div></div>`;
  }).join("")}</div>`;
}

function _renderDashboardTrend(history) {
  const el = $("#dashboard-trend");
  if (!el) return;
  if (!history || !history.length) {
    el.innerHTML = '<span class="text-muted">No export history available yet.</span>';
    return;
  }
  const rows = [...history].reverse().slice(-12);
  const maxCh = Math.max(1, ...rows.map(r => r.channel_count || 0));
  const maxXml = Math.max(1, ...rows.map(r => r.xml_size || 0));
  const bars = rows.map(r => {
    const h = Math.max(8, Math.round(((r.channel_count || 0) / maxCh) * 72));
    const o = Math.max(0.2, (r.xml_size || 0) / maxXml);
    const tt = `${r.exported_at || ""} • ${(r.channel_count || 0).toLocaleString()} channels`;
    return `<span class="dash-bar" style="height:${h}px;opacity:${o}" title="${esc(tt)}"></span>`;
  }).join("");
  const latest = history[0] || {};
  el.innerHTML = `
    <div class="dash-bars">${bars}</div>
    <div class="dash-mini-grid" style="margin-top:10px">
      <div><span class="text-muted">Last Export</span><b>${latest.exported_at ? _timeAgo(new Date(latest.exported_at + "Z")) : "—"}</b></div>
      <div><span class="text-muted">Channels</span><b>${(latest.channel_count || 0).toLocaleString()}</b></div>
      <div><span class="text-muted">Groups</span><b>${(latest.group_count || 0).toLocaleString()}</b></div>
      <div><span class="text-muted">XML Size</span><b>${_fmtBytes(latest.xml_size || 0)}</b></div>
    </div>
  `;
}

function _renderDashboardAlerts(d, coverage, epgDash, sysErrors) {
  const el = $("#dashboard-alerts");
  if (!el) return;
  const alerts = [];
  if ((d.health?.export_status || "") === "error") alerts.push({ lvl: "bad", msg: `Export error: ${d.health?.export_error || "Unknown"}` });
  if ((coverage?.issues || []).length) alerts.push(...coverage.issues.slice(0, 4).map(x => ({ lvl: "warn", msg: x })));
  if ((sysErrors?.errors || []).length) alerts.push(...sysErrors.errors.slice(0, 3).map(x => ({ lvl: "bad", msg: `${x.context}: ${x.error}` })));
  if (!alerts.length) alerts.push({ lvl: "ok", msg: "No critical signals. Pipeline is stable." });
  el.innerHTML = `<div class="dash-alerts">${alerts.slice(0, 8).map(a => `<div class="dash-alert ${a.lvl}">${esc(a.msg)}</div>`).join("")}</div>`;
}

function _fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${u[i]}`;
}

function _timeAgo(date) {
  const seconds = Math.floor((new Date() - date) / 1000);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function _fmtDuration(secs) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

function statCard(value, label, accent) {
  return `<div class="stat-card ${accent}"><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(String(value))}</div></div>`;
}

function renderDashboardSource(d) {
  const info = $("#dashboard-source-info");
  const sources = d.sources || [];
  if (!sources.length) {
    info.innerHTML = `<div class="card"><div class="card-header"><span class="card-title">First-run checklist</span></div><div class="card-body">
      <ol class="text-dim" style="line-height:1.9;margin-top:0">
        <li>Add one M3U or Xtream live-TV source.</li>
        <li>Review groups, then disable anything you do not want to export.</li>
        <li>Add EPG sources and check coverage before creating organization rules.</li>
        <li>Copy the token-protected player URLs from Export.</li>
      </ol>
      <button class="btn btn-primary" onclick="navigate('sources')">Add your first source</button>
    </div></div>`;
    return;
  }
  info.innerHTML = `<div class="card">
    <div class="card-header"><span class="card-title">All Sources</span></div>
    ${sources.map(s => {
      const isActive = s.is_active;
      let badges = isActive ? `<span class="badge badge-success">Active</span>` : `<span class="badge badge-neutral">Inactive</span>`;
      badges += `<span class="badge badge-neutral">${(s.type || "").toUpperCase()}</span>`;
      if (s.is_expired) {
        badges += `<span class="badge badge-danger">Expired</span>`;
      } else if (s.days_until_expiry != null) {
        const cls = s.days_until_expiry < 7 ? "badge-danger" : s.days_until_expiry < 30 ? "badge-warning" : "badge-success";
        badges += `<span class="badge ${cls}">${s.days_until_expiry}d left</span>`;
      }
      if (s.max_connections != null) {
        badges += `<span class="badge badge-neutral">${s.active_connections ?? 0}/${s.max_connections} conn</span>`;
      }
      const epg = s.epg_url_configured ? `<div class="text-muted" style="margin-top:2px;font-size:11px">EPG: configured (hidden)</div>` : "";
      let link = "";
      if (s.type === "xc" && s.xc_server) {
        link = `<div class="text-muted" style="margin-top:2px;font-size:11px">Server: ${esc(s.xc_server)}</div>`;
      } else if (s.type === "m3u" && s.m3u_url_configured) {
        link = `<div class="text-muted" style="margin-top:2px;font-size:11px">URL: configured (hidden)</div>`;
      }
      return `<div class="source-item${isActive ? " row-active" : ""}">
        <div class="source-icon ${s.type || "xc"}">${s.type === "m3u" ? "M3U" : "XC"}</div>
        <div class="source-info">
          <div class="source-name" style="font-weight:600;font-size:14px">${esc(s.name)}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">${badges}</div>
          ${link}${epg}
        </div>
      </div>`;
    }).join("")}
  </div>`;
}

/* ═══════════════════════════════════════════════════════════════════════
   SOURCES
   ═══════════════════════════════════════════════════════════════════════ */

// Tab switching
$$(".tab-btn").forEach(tab => {
  tab.addEventListener("click", () => {
    const group = tab.closest(".card");
    $$(".tab-btn", group).forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    $$(".tab-panel", group).forEach(c => c.classList.remove("active"));
    $(`#tab-${tab.dataset.tab}`, group).classList.add("active");
  });
});

// XC import
$("#import-xc-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#xc-submit-btn");
  btnLoad(btn, "Importing...");
  const tid = toast("loading", "Importing source", "Connecting to Xtream Codes server...");
  try {
    const result = await api("/api/import/xc", {
      method: "POST",
      body: JSON.stringify({
        name: $("#xc-name").value, server: $("#xc-server").value,
        username: $("#xc-user").value, password: $("#xc-pass").value,
        output: $("#xc-output").value, epg_url: $("#xc-epg").value || null,
      }),
    });
    updateToast(tid, "success", "Source imported", `${result.channels ?? ""} channels loaded`);
    e.target.reset();
    loadSources();
  } catch (err) {
    updateToast(tid, "error", "Import failed", err.message);
  } finally { btnDone(btn); }
});

// M3U import
$("#import-m3u-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#m3u-submit-btn");
  btnLoad(btn, "Importing...");
  const tid = toast("loading", "Importing M3U", "Downloading and parsing playlist...");
  try {
    const result = await api("/api/import/m3u", {
      method: "POST",
      body: JSON.stringify({
        name: $("#m3u-name").value, m3u_url: $("#m3u-url").value,
        epg_url: $("#m3u-epg").value || null,
      }),
    });
    updateToast(tid, "success", "M3U imported", `${result.channels ?? ""} channels loaded`);
    e.target.reset();
    loadSources();
  } catch (err) {
    updateToast(tid, "error", "Import failed", err.message);
  } finally { btnDone(btn); }
});

async function loadSources() {
  const el = $("#sources-list");
  try {
    const sources = await api("/api/sources?refresh_account=1");
    const nb = $("#nav-src-count"); if (nb) nb.textContent = sources.length;
    if (!sources.length) {
      el.innerHTML = `<div class="card-empty"><div class="card-empty-icon">📡</div><div class="card-empty-text">No sources yet. Add one above to get started.</div></div>`;
      return;
    }
    el.innerHTML = sources.map(s => {
      const isActive = s.is_active;
      let statusBadge = isActive ? `<span class="badge badge-success">Active</span>` : `<span class="badge badge-neutral">Inactive</span>`;
      if (s.is_expired) statusBadge = `<span class="badge badge-danger">Expired</span>`;
      let meta = [];
      if (s.max_connections != null) meta.push(`<span>${s.active_connections ?? 0}/${s.max_connections} conn</span>`);
      if (s.days_until_expiry != null && !s.is_expired) meta.push(`<span>${s.days_until_expiry}d left</span>`);
      if (s.last_refreshed) {
        const ago = timeAgo(s.last_refreshed);
        meta.push(`<span title="${esc(s.last_refreshed)}">Synced ${ago}</span>`);
      }
      return `
        <div class="source-item ${isActive ? "row-active" : ""}">
          <div class="source-icon ${s.type || "xc"}">${s.type === "m3u" ? "M3U" : "XC"}</div>
          <div class="source-info">
            <input class="source-name-input" value="${esc(s.name)}" data-sid="${s.id}" />
            <div class="source-meta">${statusBadge}${meta.join("")}</div>
            ${s.epg_url_configured ? `<div class="source-epg"><span class="text-muted">EPG: configured (hidden)</span></div>` : `<div class="source-epg"><span class="text-muted">EPG: none</span></div>`}
            ${s.type === "m3u" && s.m3u_url_configured ? `<div class="source-epg"><span class="text-muted">M3U: configured (hidden)</span></div>` : ''}
            ${s.type === "xc" && s.xc_server ? `<div class="source-epg"><span class="text-muted">Server:</span> <code class="cell-mono text-muted">${esc(s.xc_server)}</code></div>` : ''}
            <div class="source-epg" style="display:flex;align-items:center;gap:6px;margin-top:2px">
              <span class="text-muted" style="font-size:11px;white-space:nowrap">Payment:</span>
              <input type="url" class="source-payment-input" value="${esc(s.payment_url || '')}" data-sid="${s.id}" placeholder="https://your-provider.com/renew" style="flex:1;font-size:11px;padding:2px 6px;max-width:260px" />
              ${s.payment_url ? `<a href="${esc(s.payment_url)}" target="_blank" rel="noopener" class="btn btn-sm" style="font-size:10px;padding:2px 8px">Open</a>` : ''}
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:4px;padding:0 8px">
            <label class="text-muted" style="font-size:11px;white-space:nowrap" title="Lower number = higher priority. Channels from higher-priority sources appear first in shared groups.">Priority</label>
            <input type="number" class="source-priority-input" value="${s.priority ?? 10}" data-sid="${s.id}" min="1" max="99" style="width:48px;text-align:center;padding:4px;font-size:13px" />
          </div>
          <div class="btn-row">
            <button class="btn btn-sm ${isActive ? "btn-warning" : "btn-success"}" onclick="toggleSource(${s.id})">${isActive ? "Deactivate" : "Activate"}</button>
            ${s.type === "xc" ? `<button class="btn btn-sm" onclick="refreshSource(${s.id}, this)">Refresh</button>` : ""}
            <button class="btn btn-sm" onclick="syncSource(${s.id}, this)">Sync</button>
            ${s.last_import_diff ? `<button class="btn btn-sm btn-ghost" onclick="showImportDiff(${s.id}, '${esc(s.name).replace(/'/g, "\\'")}')" title="View last import changes">Last Import</button>` : ""}
            <button class="btn btn-sm btn-danger" onclick="deleteSourceConfirm(${s.id}, '${esc(s.name).replace(/'/g, "\\'")}')">Delete</button>
          </div>
        </div>`;
    }).join("");
    // Wire source name edits
    $$(".source-name-input").forEach(inp => {
      inp.addEventListener("change", async () => {
        try {
          await api(`/api/sources/${inp.dataset.sid}`, { method: "PATCH", body: JSON.stringify({ name: inp.value }) });
          toast("success", "Source renamed");
        } catch (e) { toast("error", "Rename failed", e.message); }
      });
    });
    // Wire source payment URL edits
    $$(".source-payment-input").forEach(inp => {
      inp.addEventListener("change", async () => {
        try {
          await api(`/api/sources/${inp.dataset.sid}`, { method: "PATCH", body: JSON.stringify({ payment_url: inp.value.trim() }) });
          toast("success", "Payment link saved");
          loadSources();
        } catch (e) { toast("error", "Save failed", e.message); }
      });
    });
    // Wire source priority edits
    $$(".source-priority-input").forEach(inp => {
      inp.addEventListener("change", async () => {
        const val = parseInt(inp.value) || 10;
        inp.value = val;
        try {
          await api(`/api/sources/${inp.dataset.sid}`, { method: "PATCH", body: JSON.stringify({ priority: val }) });
          toast("success", "Priority updated", `Lower number = channels appear first`);
        } catch (e) { toast("error", "Update failed", e.message); }
      });
    });
  } catch (e) {
    el.innerHTML = `<div class="card-empty"><div class="card-empty-text">Failed to load sources: ${esc(e.message)}</div></div>`;
  }
}

window.toggleSource = async (id) => {
  const tid = toast("loading", "Toggling source", "Updating active sources...");
  try {
    const r = await api(`/api/sources/${id}/activate`, { method: "POST" });
    updateToast(tid, "success", r.active ? "Source activated" : "Source deactivated");
    loadSources();
  } catch (e) { updateToast(tid, "error", "Toggle failed", e.message); }
};

window.refreshSource = async (id, btn) => {
  btnLoad(btn, "Refreshing...");
  const tid = toast("loading", "Refreshing source", "Fetching latest channel data...");
  try {
    await api(`/api/sources/${id}/refresh`, { method: "POST" });
    updateToast(tid, "success", "Source refreshed");
    loadSources();
  } catch (e) { updateToast(tid, "error", "Refresh failed", e.message); }
  finally { btnDone(btn); }
};

window.syncSource = async (id, btn) => {
  btnLoad(btn, "Syncing...");
  const tid = toast("loading", "Syncing source", "Detecting channel changes...");
  try {
    const r = await api(`/api/sources/${id}/refresh`, { method: "POST" });
    const parts = [];
    if (r.added > 0) parts.push(`+${r.added} added`);
    if (r.removed > 0) parts.push(`-${r.removed} removed`);
    if (r.updated > 0) parts.push(`${r.updated} updated`);
    const msg = parts.length ? parts.join(", ") : "No changes";
    updateToast(tid, "success", "Sync complete", msg);
    loadSources(); fetchGroups().then(() => renderGroups());
  } catch (e) { updateToast(tid, "error", "Sync failed", e.message); }
  finally { btnDone(btn); }
};

window.deleteSourceConfirm = async (id, name) => {
  const ok = await confirmDialog("Delete Source", `Are you sure you want to delete "${name}"? This will remove all channels from this source.`, "Delete Source");
  if (!ok) return;
  const tid = toast("loading", "Deleting source");
  try {
    await api(`/api/sources/${id}`, { method: "DELETE" });
    updateToast(tid, "success", "Source deleted");
    loadSources();
  } catch (e) { updateToast(tid, "error", "Delete failed", e.message); }
};

/* ═══════════════════════════════════════════════════════════════════════
   EDITOR
   ═══════════════════════════════════════════════════════════════════════ */
async function loadEditor() {
  const el = $("#groups-list");
  if (!groupsData.length) el.innerHTML = '<div style="padding:20px;text-align:center"><span class="text-muted">Loading groups...</span></div>';
  try {
    await Promise.all([
      ensureScript("/static/vendor/sortable.min.js", "Sortable"),
      fetchGroups(), fetchCategories(), fetchGroupOrderHealth(),
    ]);
    renderGroups();
  } catch (e) { console.error(e); }
}

let groupFilterText = "";
let groupViewMode = localStorage.getItem("m3uBoss.groupView") || "active";
let groupCategoryFilter = localStorage.getItem("m3uBoss.groupCategory") || "";

function syncGroupFilterControls() {
  const view = $("#group-view-filter");
  if (view) view.value = groupViewMode;
  const cat = $("#group-category-filter");
  if (cat) {
    cat.innerHTML = `<option value="">All categories</option>` + categoriesData.map(c =>
      `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
    cat.value = groupCategoryFilter;
    if (cat.value !== groupCategoryFilter) groupCategoryFilter = "";
  }
}

function getFilteredGroups() {
  return groupsData.filter(g => {
    const count = Number(g.channel_count || 0);
    const viewMatch = groupViewMode === "all" ||
      (groupViewMode === "active" && !!g.enabled && count > 0 && !g.archived_at) ||
      (groupViewMode === "enabled" && !!g.enabled) ||
      (groupViewMode === "disabled" && !g.enabled && !g.archived_at) ||
      (groupViewMode === "empty" && count === 0) ||
      (groupViewMode === "archived" && (!!g.archived_at || g.category === "Archive"));
    return viewMatch && (!groupCategoryFilter || g.category === groupCategoryFilter) &&
      (!groupFilterText || (g.name || "").toLowerCase().includes(groupFilterText));
  });
}

function renderGroups() {
  syncGroupFilterControls();
  const el = $("#groups-list");
  if (!groupsData.length) {
    el.innerHTML = `<div class="card"><div class="card-empty"><div class="card-empty-icon">📂</div><div class="card-empty-text">No groups yet. Import a source or add a group to get started.</div></div></div>`;
    return;
  }
  const filtered = getFilteredGroups();
  if (!filtered.length) {
    el.innerHTML = `<div class="card"><div class="card-empty"><div class="card-empty-text">No groups match "<b>${esc(groupFilterText)}</b>"</div></div></div>`;
    return;
  }
  // Build category lookup for color on position badges
  const catMap = {};
  categoriesData.forEach(c => { catMap[c.name] = c; });

  let html = "";
  let lastCat = null;
  filtered.forEach((g, idx) => {
    const cat = g.category || "";
    // When a category is selected, label the scoped result once. In the full
    // lineup categories remain color labels/filters so the visible order stays
    // identical to the exported player order.
    if (groupCategoryFilter && cat !== lastCat) {
      const nextCat = cat && catMap[cat] ? catMap[cat] : null;
      const divColor = nextCat ? nextCat.color : "var(--border)";
      const divLabel = nextCat ? nextCat.name : "Uncategorized";
      html += `<div class="category-divider" style="border-color:${esc(divColor)}"><span class="category-divider-label">${esc(divLabel)}</span></div>`;
      lastCat = cat;
    }
    const isOpen = expandedGroups.has(g.id);
    const numberRow = groupOrderHealthById.get(g.id);
    const isPinned = !!g.pinned;
    const isDisabled = !g.enabled;
    const isTeamarr = !!g.teamarr;
    const isNameEpg = !!g.name_epg;
    const catInfo = cat && catMap[cat] ? catMap[cat] : null;
    // Position badge: use category color as background, or default accent
    const badgeBg = catInfo ? esc(catInfo.color) : "";
    const badgeStyle = badgeBg ? `background:${badgeBg};color:#fff` : "";
    // Distinguish "coverage not computed yet" (null/undefined -> show "--")
    // from a genuine 0%. Note: Number(null) === 0, so the null check must
    // come first or uncomputed groups falsely render as a red "0% EPG".
    const _rawCov = g.epg_coverage_pct;
    const coveragePct = (_rawCov === null || _rawCov === undefined || !Number.isFinite(Number(_rawCov)))
      ? null : Number(_rawCov);
    // Disabled groups aren't in the export, so their coverage is estimated
    // from how many channels are mapped to a known guide id.
    const covEstimated = !!g.epg_coverage_estimated;
    const coverageCls = coveragePct == null ? "" : coveragePct >= 85 ? "ok" : coveragePct >= 60 ? "warn" : "bad";
    const _covSpan = g.epg_channels ? ` across ${g.epg_channels} channel${g.epg_channels === 1 ? "" : "s"}` : "";
    const coverageTitle = coveragePct == null
      ? "EPG coverage unavailable"
      : covEstimated
        ? `~${coveragePct}% of channels mapped to a guide${_covSpan} — estimated (group disabled; enable it to verify real programme data)`
        : `${coveragePct}% real guide coverage${_covSpan}`;
    // Category submenu for the ⋮ menu
    let catMenuHtml = '<div class="group-menu-sub">';
    catMenuHtml += `<div class="group-menu-item grp-cat-set" data-gid="${g.id}" data-cat="" style="font-size:12px;opacity:${!cat ? 1 : 0.6}">${!cat ? "✓ " : ""}No Category</div>`;
    categoriesData.forEach(c => {
      const isCurrent = cat === c.name;
      catMenuHtml += `<div class="group-menu-item grp-cat-set" data-gid="${g.id}" data-cat="${esc(c.name)}" style="font-size:12px;opacity:${isCurrent ? 1 : 0.6}"><span class="category-dot" style="background:${esc(c.color)}"></span>${isCurrent ? "✓ " : ""}${esc(c.name)}</div>`;
    });
    catMenuHtml += '</div>';

    html += `
    <div class="group-card${isPinned ? " pinned" : ""}${isDisabled ? " disabled" : ""}${isOpen ? " expanded" : ""}" data-gid="${g.id}">
      <div class="group-header" data-gid="${g.id}">
        <input type="checkbox" class="grp-sel" data-gid="${g.id}" onclick="event.stopPropagation();updateGroupSelBar()" />
        <span class="drag-handle">${ICO.grip}</span>
        <span class="group-order" data-gid="${g.id}" data-pos="${idx + 1}" title="Click to move to position"${badgeStyle ? ` style="${badgeStyle}"` : ""}>${idx + 1}</span>
        <span class="group-chevron${isOpen ? " open" : ""}">${ICO.chev}</span>
        <span class="group-name-display" title="${esc(g.name)}">${esc(g.name)}</span>
        <div class="group-metrics">
          <span class="group-count" title="${g.enabled_count ?? 0} enabled / ${g.channel_count ?? 0} total">${g.enabled_count ?? 0}/${g.channel_count ?? 0}</span>
          ${numberRow ? `<span class="chno-pill${numberRow.aligned ? "" : " warn"}" title="Player-visible provider number range">#${numberRow.start}–${numberRow.end}</span>` : ""}
          <span class="dash-pill ${coverageCls}" title="${esc(coverageTitle)}">${coveragePct == null ? "--" : `${covEstimated ? "~" : ""}${coveragePct}% EPG`}</span>
        </div>
        <div class="group-actions">
          <button class="btn-icon-only" onclick="event.stopPropagation();pinGroup('${g.id}',${!isPinned})" title="${isPinned ? "Unpin" : "Pin to top"}" style="${isPinned ? "color:var(--warning)" : ""}">${ICO.pin}</button>
          <button class="btn-icon-only" onclick="event.stopPropagation();moveGroupTop('${g.id}')" title="Move to top">${ICO.up}</button>
          <label class="toggle" title="Enable/disable group" onclick="event.stopPropagation()">
            <input type="checkbox" class="grp-toggle" data-gid="${g.id}" ${g.enabled ? "checked" : ""} />
            <span class="toggle-track"></span>
          </label>
          <div class="group-menu-wrap" onclick="event.stopPropagation()">
            <button class="btn-icon-only group-menu-btn" title="More actions">⋮</button>
            <div class="group-menu">
              <div class="group-menu-item" onclick="event.stopPropagation();renameGroup('${g.id}')">✏️ Rename</div>
              <label class="group-menu-item" onclick="event.stopPropagation()"><input type="checkbox" class="grp-teamarr" data-gid="${g.id}" ${isTeamarr ? "checked" : ""} /> Teamarr</label>
              <label class="group-menu-item" onclick="event.stopPropagation()"><input type="checkbox" class="grp-name-epg" data-gid="${g.id}" ${isNameEpg ? "checked" : ""} /> Dummy EPG</label>
              <div class="group-menu-item" onclick="event.stopPropagation();setGroupTag('${g.id}','${esc(g.export_tag||'').replace(/'/g,"\\'")}')">🏷 Export Tag${g.export_tag ? ` <span class="cell-mono" style="font-size:10px;opacity:.7">[${esc(g.export_tag)}]</span>` : ""}</div>
              <div class="group-menu-item" onclick="event.stopPropagation();setGroupParent('${g.id}')">🔗 Set Parent</div>
              <div class="group-menu-item" onclick="event.stopPropagation();openTemplatesFor('${g.id}')">📋 Templates</div>
              <div class="group-menu-item" onclick="event.stopPropagation();openHealthModal('${g.id}','${esc(g.name).replace(/'/g,"\\'")}')">🩺 Health Check</div>
              <div class="group-menu-item" onclick="event.stopPropagation();sortSxmGroup('${g.id}')">📻 Sort SiriusXM</div>
              <div class="group-menu-item group-menu-danger" onclick="event.stopPropagation();deleteGroupConfirm('${g.id}','${esc(g.name).replace(/'/g, "\\'")}')">🗑 Delete</div>
              ${catMenuHtml}
            </div>
          </div>
        </div>
      </div>
      ${isOpen ? renderGroupBody(g) : ""}
    </div>`;
  });
  el.innerHTML = html;
  wireGroupEvents();
  wireSortableGroups();
  // Update count label when filtering
  const countLabel = $("#ch-count-label");
  if (countLabel) countLabel.textContent = `${getFilteredGroups().length} of ${groupsData.length} groups`;
}

function renderGroupOrderHealth() {
  const bar = $("#group-order-health");
  if (!bar || !groupOrderHealth) return;
  bar.hidden = false;
  bar.classList.toggle("ok", !!groupOrderHealth.aligned);
  $("#group-order-health-title").textContent = groupOrderHealth.aligned
    ? "Player order matches M3U Boss"
    : `${groupOrderHealth.mismatch_count} groups need player-order sync`;
  $("#group-order-health-detail").textContent = groupOrderHealth.aligned
    ? "Provider numbers and configured group order agree."
    : "M3U order is correct, but historical provider numbers can place groups elsewhere in Dispatcharr.";
  $("#sync-player-order-btn").hidden = !!groupOrderHealth.aligned;
}

$("#sync-player-order-btn")?.addEventListener("click", async () => {
  const ok = await confirmDialog(
    "Sync Player Order",
    "Reassign all exported provider channel numbers once so every group follows the order shown in M3U Boss. Dispatcharr will refresh automatically. Stable ordering resumes from the new baseline.",
    "Sync Order"
  );
  if (!ok) return;
  const btn = $("#sync-player-order-btn");
  btnLoad(btn, "Syncing…");
  const tid = toast("loading", "Syncing player order", "Rebuilding provider channel numbers…");
  try {
    const result = await api("/api/groups/renumber-all", { method: "POST" });
    await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
    renderGroups();
    updateToast(tid, "success", "Player order synchronized", `${result.assigned.toLocaleString()} channels aligned`);
    startExportPoll();
  } catch (err) {
    updateToast(tid, "error", "Order sync failed", err.message);
  } finally { btnDone(btn); }
});

function renderGroupBody(g) {
  const rules = g.match_rules || [];
  const cache = channelCache[g.id];
  const fieldLabels = { source_group: "Source Group", channel_name: "Name", any: "Any" };
  const matchLabels = { contains: "contains", starts_with: "starts with", regex: "regex", exact: "exact" };

  // Rules section
  let rulesHtml = `<div class="rules-section"><div class="rules-title">Match Rules</div><div class="rule-chips">`;
  rulesHtml += rules.map(r => `
    <span class="rule-chip">
      <span class="rule-chip-field">${esc(fieldLabels[r.field] || r.field)}</span>
      ${esc(matchLabels[r.match_type] || r.match_type)}
      "<b>${esc(r.pattern)}</b>"
      <span class="rule-chip-edit" data-gid="${g.id}" data-rid="${r.id}" data-field="${esc(r.field)}" data-match="${esc(r.match_type)}" data-pattern="${esc(r.pattern)}" onclick="editRule(this.dataset.gid,this.dataset.rid,this.dataset.field,this.dataset.match,this.dataset.pattern)" title="Edit rule">✏</span>
      <span class="rule-chip-close" onclick="deleteRule('${g.id}','${r.id}')">&times;</span>
    </span>`).join("");
  if (!rules.length) rulesHtml += `<span class="text-muted">No rules — channels assigned from source groups</span>`;
  rulesHtml += `</div>
    <div class="rule-add-row">
      <select class="rule-field" style="min-width:110px"><option value="source_group">Source Group</option><option value="channel_name">Channel Name</option><option value="any">Any Field</option></select>
      <select class="rule-match"><option value="contains">contains</option><option value="starts_with">starts with</option><option value="regex">regex</option><option value="exact">exact</option></select>
      <input class="rule-pattern" placeholder="Pattern..." style="flex:1;min-width:120px" />
      <button class="btn btn-sm btn-primary" onclick="addRule('${g.id}', this)">Add Rule</button>
      <span class="rule-preview-count" data-gid="${g.id}"></span>
    </div></div>`;

  // Channels
  let channelsHtml = "";
  if (!cache) {
    channelsHtml = `<div style="padding:16px;text-align:center"><span class="text-muted">Loading channels...</span></div>`;
  } else if (!cache.channels.length) {
    channelsHtml = `<div class="card-empty" style="padding:20px"><div class="card-empty-text">No channels in this group</div></div>`;
  } else {
    const moveOpts = groupsData.filter(o => o.id !== g.id)
      .map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join("");

    channelsHtml = `
      <div class="ch-toolbar">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;font-weight:600">
          <input type="checkbox" class="grp-sel-all" data-gid="${g.id}" /> All
        </label>
        <span class="sel-count" data-gid="${g.id}">0 selected</span>
        <div style="flex:1"></div>
        <button class="btn btn-sm" onclick="editChannelPriorities('${g.id}')" title="Edit channel priority keywords">⚡ Priorities</button>
        <button class="btn btn-sm" onclick="sortChannelsByPriority('${g.id}')" title="Sort channels by priority keywords, then A–Z">↕ Sort</button>
        <button class="btn btn-sm" onclick="sortChannelsAlpha('${g.id}')" title="Sort channels A–Z (natural order)">A–Z</button>
        <button class="btn btn-sm" onclick="sortChannelsBySource('${g.id}')" title="Sort by source priority, then channel number within each source">🔀 Source</button>
        <button class="btn btn-sm" onclick="bulkMoveChannelsTop('${g.id}')" title="Move selected to top">⬆ Top</button>
        <select class="grp-bulk-move" data-gid="${g.id}" style="font-size:12px"><option value="">Move/Copy to...</option>${moveOpts}</select>
        <button class="btn btn-sm" onclick="bulkMoveGroup('${g.id}')">Move</button>
        <button class="btn btn-sm" onclick="bulkCopyGroup('${g.id}')">Copy</button>
        <button class="btn btn-sm btn-success" onclick="bulkToggleGroup('${g.id}',true)">Enable</button>
        <button class="btn btn-sm btn-ghost" onclick="bulkToggleGroup('${g.id}',false)">Disable</button>
        <button class="btn btn-sm" onclick="bulkLockGroup('${g.id}',true)" title="Prevent rules and refreshes from relocating selected stations">🔒 Lock</button>
        <button class="btn btn-sm btn-ghost" onclick="bulkLockGroup('${g.id}',false)" title="Allow rules to relocate selected stations">Unlock</button>
        <button class="btn btn-sm" style="color:var(--warning)" onclick="bulkFavGroup('${g.id}',true)">★ Fav</button>
        <select class="grp-bulk-epg" data-gid="${g.id}" style="font-size:12px" onchange="bulkEpgGroup('${g.id}',this.value);this.selectedIndex=0">
          <option value="">EPG...</option>
          <option value="name">Dummy EPG</option>
          <option value="unset">Unset EPG</option>
          <option value="restore">Restore EPG</option>
        </select>
        <select class="grp-bulk-logo" data-gid="${g.id}" style="font-size:12px" onchange="bulkLogoGroup('${g.id}',this.value);this.selectedIndex=0">
          <option value="">Logo...</option>
          <option value="clear">Clear Logos</option>
        </select>
        <button class="btn btn-sm btn-danger" onclick="bulkDeleteGroup('${g.id}')">Delete</button>
      </div>
      <div class="table-wrap"><table class="data-table"><thead><tr>
        <th style="width:32px"><input type="checkbox" class="grp-sel-all-hdr" data-gid="${g.id}" /></th>
        <th style="width:24px"></th>
        <th style="width:24px"></th>
        <th style="width:24px"></th>
        <th>Name</th><th style="min-width:220px">Now</th><th>Source Group</th><th>TVG-ID</th><th>TVG-Name</th><th style="width:48px">On</th><th style="width:32px">★</th><th style="width:38px" title="Protect this channel from automatic rules">🔒</th><th style="width:70px">EPG</th><th style="width:28px">🖼</th><th>Move</th><th style="width:32px"></th>
      </tr></thead><tbody>
      ${cache.channels.map(ch => {
        const isDummy = (ch.tvg_id || "").startsWith("dummy.");
        const hasOriginal = !!(ch.original_tvg_id);
        const epgState = isDummy ? "name" : (ch.tvg_id ? "matched" : "none");
        const hasLogo = !!(ch.logo);
        const listing = cache.listings?.[ch.id]?.current || null;
        const nextListing = cache.listings?.[ch.id]?.next || null;
        const listingDetail = listing ? [listing.episode_num, listing.sub_title].filter(Boolean).join(" · ") : "";
        const listingStop = listing?.stop ? new Date(listing.stop).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}) : "";
        const listingTitle = listing
          ? `${listing.title}${listingDetail ? ` — ${listingDetail}` : ""}${nextListing ? `\nNext: ${nextListing.title}` : ""}`
          : (nextListing ? `Next: ${nextListing.title}` : "No current guide listing");
        return `
        <tr data-cid="${ch.id}">
          <td><input type="checkbox" class="ch-sel" data-cid="${ch.id}" data-gid="${g.id}" /></td>
          <td class="drag-handle" style="cursor:grab;text-align:center;color:var(--text-dim)" title="Drag to reorder">⠿</td>
          <td><button class="btn-icon-only ch-top-btn" data-cid="${ch.id}" data-gid="${g.id}" title="Move to top" style="width:20px;height:20px">${ICO.up}</button></td>
          <td><button class="btn-icon-only ch-play-btn" data-cid="${ch.id}" data-gid="${g.id}" title="Play channel" style="width:20px;height:20px">▶</button></td>
          <td><input type="text" class="ch-name" value="${esc(ch.name)}" data-cid="${ch.id}" /></td>
          <td class="ch-current-listing" title="${esc(listingTitle)}">
            ${listing ? `<div class="ch-current-title">${esc(listing.title)}</div><div class="ch-current-meta">${listingDetail ? `${esc(listingDetail)} · ` : ""}until ${esc(listingStop)}${listing.quality ? ` · ${esc(listing.quality)}` : ""}</div>`
              : `<span class="text-muted">${nextListing ? `Next: ${esc(nextListing.title)}` : "No current listing"}</span>`}
          </td>
          <td class="cell-dim">${esc(ch.source_group || "")}</td>
          <td><input type="text" class="ch-tvgid cell-mono" value="${esc(ch.tvg_id || "")}" data-cid="${ch.id}" style="width:calc(100% - 26px);display:inline-block" /><button class="btn-icon-only epg-search-btn" data-cid="${ch.id}" title="Search EPG" style="width:22px;height:22px;vertical-align:middle;margin-left:2px">🔍</button></td>
          <td><input type="text" class="ch-tvgname" value="${esc(ch.tvg_name || "")}" data-cid="${ch.id}" /></td>
          <td><label class="toggle"><input type="checkbox" class="ch-toggle" data-cid="${ch.id}" ${ch.enabled ? "checked" : ""} /><span class="toggle-track"></span></label></td>
          <td><input type="checkbox" class="ch-fav" data-cid="${ch.id}" ${ch.favorite ? "checked" : ""} style="accent-color:var(--warning)" /></td>
          <td><input type="checkbox" class="ch-lock" data-cid="${ch.id}" ${ch.placement_locked ? "checked" : ""} title="Keep this channel in its current group" /></td>
          <td><select class="ch-epg-action" data-cid="${ch.id}" style="font-size:11px;padding:2px 4px;width:68px;${isDummy ? 'color:var(--accent)' : ''}">
            <option value="" selected>${isDummy ? "Dummy" : (ch.tvg_id ? "Match" : "None")}</option>
            <option value="search">Search</option>
            <option value="name">Dummy EPG</option>
            ${hasOriginal ? `<option value="restore">Restore</option>` : ""}
            <option value="unset">Unset</option>
          </select></td>
          <td style="text-align:center">${hasLogo ? `<img src="${esc(ch.logo)}" style="width:20px;height:20px;object-fit:contain;border-radius:2px;cursor:pointer" onclick="editChannelLogo('${ch.id}')" title="${esc(ch.logo)}" onerror="this.style.display='none'" />` : `<button class="btn-icon-only" style="width:20px;height:20px;opacity:.4" onclick="editChannelLogo('${ch.id}')" title="No logo — click to set">+</button>`}</td>
          <td><select class="ch-move" data-cid="${ch.id}" style="font-size:12px"><option value="">Move...</option>${moveOpts}</select></td>
          <td><select class="ch-copy" data-cid="${ch.id}" style="font-size:12px"><option value="">Copy...</option>${moveOpts}</select></td>
          <td><button class="btn btn-sm btn-danger" style="padding:2px 6px;font-size:11px" onclick="deleteChannel('${ch.id}','${g.id}')">✕</button></td>
        </tr>`;}).join("")}
      </tbody></table></div>`;

    // Pagination
    if (cache.total > cache.channels.length) {
      channelsHtml += `<div class="pagination"><span>Showing ${cache.channels.length.toLocaleString()} of ${cache.total.toLocaleString()}</span><button class="btn btn-sm" onclick="loadMoreChannels('${g.id}')">Load More</button></div>`;
    } else {
      channelsHtml += `<div class="pagination"><span>Showing all ${cache.total.toLocaleString()} channels</span></div>`;
    }
  }
  return `<div class="group-body">${rulesHtml}${channelsHtml}</div>`;
}

function wireGroupEvents() {
  // Expand/collapse — update only the clicked group, not the full list
  $$(".group-header").forEach(hdr => {
    hdr.addEventListener("click", async (e) => {
      if (e.target.closest(".group-actions") || e.target.closest(".toggle")
          || e.target.classList.contains("grp-sel") || e.target.closest(".group-menu-wrap")) return;
      const gid = hdr.dataset.gid;
      const card = hdr.closest(".group-card");
      const chevron = hdr.querySelector(".group-chevron");

      if (expandedGroups.has(gid)) {
        // Collapse: just remove the body
        expandedGroups.delete(gid);
        delete channelCache[gid];
        const body = card.querySelector(".group-body");
        if (body) body.remove();
        if (chevron) chevron.classList.remove("open");
        card.classList.remove("expanded");
      } else {
        // Expand: load channels and insert body
        expandedGroups.add(gid);
        if (chevron) chevron.classList.add("open");
        card.classList.add("expanded");
        // Show loading placeholder
        const placeholder = document.createElement("div");
        placeholder.className = "group-body";
        placeholder.innerHTML = '<div style="padding:16px;text-align:center"><span class="text-muted">Loading channels...</span></div>';
        card.appendChild(placeholder);
        try {
          await loadGroupChannels(gid, 0);
          const g = groupsData.find(g => g.id === gid);
          if (g) {
            if (placeholder.parentNode) {
              const wrap = document.createElement("div");
              wrap.innerHTML = renderGroupBody(g);
              const nextBody = wrap.firstElementChild;
              if (nextBody) placeholder.replaceWith(nextBody);
            }
            wireGroupBodyEvents(card, g);
          }
        } catch (err) {
          const msg = err?.message || "Unable to load channels for this group.";
          placeholder.innerHTML = `<div class="card-empty" style="padding:16px"><div class="card-empty-text">${esc(msg)}</div></div>`;
          toast("error", "Channel load failed", msg);
        }
      }
    });
  });

  // Group ⋮ menu toggle
  $$(".group-menu-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const menu = btn.closest(".group-menu-wrap").querySelector(".group-menu");
      const isOpen = menu.classList.contains("open");
      // Close all menus first
      $$(".group-menu.open").forEach(m => m.classList.remove("open"));
      if (!isOpen) menu.classList.add("open");
    });
  });
  // Menu items: stop propagation so document click doesn't fire
  $$(".group-menu-item").forEach(item => {
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      // Close the menu after click (except for checkbox labels)
      if (!item.querySelector("input[type=checkbox]")) {
        $$(".group-menu.open").forEach(m => m.classList.remove("open"));
      }
    });
  });

  // Group toggle
  $$(".grp-toggle").forEach(cb => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/groups/${cb.dataset.gid}`, { method: "PATCH", body: JSON.stringify({ enabled: cb.checked }) });
        toast("success", cb.checked ? "Group enabled" : "Group disabled");
        const card = cb.closest(".group-card");
        if (card) card.classList.toggle("disabled", !cb.checked);
      } catch (e) { toast("error", "Toggle failed", e.message); }
    });
  });

  // Teamarr toggle
  $$(".grp-teamarr").forEach(cb => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/groups/${cb.dataset.gid}`, { method: "PATCH", body: JSON.stringify({ teamarr: cb.checked }) });
        toast("success", cb.checked ? "Added to Teamarr" : "Removed from Teamarr");
      } catch (e) { toast("error", "Toggle failed", e.message); cb.checked = !cb.checked; }
    });
  });

  // Dummy EPG toggle
  $$(".grp-name-epg").forEach(cb => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/groups/${cb.dataset.gid}`, { method: "PATCH", body: JSON.stringify({ name_epg: cb.checked }) });
        toast("success", cb.checked ? "Dummy EPG enabled" : "Dummy EPG disabled");
      } catch (e) { toast("error", "Toggle failed", e.message); cb.checked = !cb.checked; }
    });
  });

  // Wire body events for any already-expanded groups
  $$(".group-card").forEach(card => {
    if (card.querySelector(".group-body")) {
      const gid = card.dataset.gid;
      const g = groupsData.find(g => g.id === gid);
      if (g) wireGroupBodyEvents(card, g);
    }
  });

  // Shift-click range select for group checkboxes
  wireShiftSelect(".grp-sel", null, () => updateGroupSelBar());

  // Clickable position number — click to edit, Enter/blur to move
  $$(".group-order").forEach(badge => {
    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      const gid = badge.dataset.gid;
      const pos = badge.dataset.pos;
      const input = document.createElement("input");
      input.type = "number";
      input.className = "group-order-input";
      input.value = pos;
      input.min = 1;
      input.max = groupsData.length;
      badge.replaceWith(input);
      input.focus();
      input.select();
      const commit = async () => {
        const newPos = parseInt(input.value, 10);
        if (newPos && newPos !== parseInt(pos, 10)) {
          try {
            await api(`/api/groups/${gid}/move-to-position`, { method: "POST", body: JSON.stringify({ position: newPos }) });
            await fetchGroups();
            renderGroups();
          } catch (err) { toast("error", "Move failed", err.message); }
        } else {
          // Revert without reload
          input.replaceWith(badge);
        }
      };
      input.addEventListener("blur", commit);
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
        if (ev.key === "Escape") { input.removeEventListener("blur", commit); input.replaceWith(badge); }
      });
    });
  });

  // Category assignment from group menu
  $$(".grp-cat-set").forEach(item => {
    item.addEventListener("click", async (e) => {
      e.stopPropagation();
      const gid = item.dataset.gid;
      const cat = item.dataset.cat;
      try {
        await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ category: cat }) });
        $$(".group-menu.open").forEach(m => m.classList.remove("open"));
        toast("success", cat ? `Assigned to ${cat}` : "Category removed");
        await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
        renderGroups();
      } catch (err) { toast("error", "Category update failed", err.message); }
    });
  });
}

/**
 * Wire event handlers ONLY inside a single group's body (channels, rules, etc).
 * Called per-group on expand or full re-render.  `container` = the .group-card element.
 */
function wireGroupBodyEvents(container) {
  // Channel drag-to-reorder (with multi-drag support)
  const tbody = container.querySelector(".data-table tbody");
  if (tbody) {
    const gid = container.dataset?.gid || container.querySelector(".group-header")?.dataset?.gid;
    new Sortable(tbody, {
      handle: ".drag-handle",
      animation: 150,
      ghostClass: "sortable-ghost",
      scroll: true,
      scrollSensitivity: 100,
      scrollSpeed: 20,
      bubbleScroll: true,
      onStart: (evt) => {
        if (!gid) return;
        const draggedCid = evt.item.dataset.cid;
        const selectedCids = getSelectedIds(gid);
        if (selectedCids.length > 1 && selectedCids.includes(draggedCid)) {
          evt.item._multiDragIds = selectedCids;
          tbody.querySelectorAll("tr[data-cid]").forEach(r => {
            if (r !== evt.item && selectedCids.includes(r.dataset.cid)) {
              r.classList.add("multi-drag-hidden");
            }
          });
        }
      },
      onEnd: async (evt) => {
        const multiIds = evt.item._multiDragIds;
        delete evt.item._multiDragIds;

        if (multiIds && multiIds.length > 1) {
          tbody.querySelectorAll(".multi-drag-hidden").forEach(r => r.classList.remove("multi-drag-hidden"));
          const others = multiIds.filter(id => id !== evt.item.dataset.cid);
          const otherEls = others.map(id => tbody.querySelector(`tr[data-cid="${id}"]`)).filter(Boolean);
          otherEls.forEach(r => r.remove());
          let insertAfter = evt.item;
          for (const r of otherEls) {
            insertAfter.after(r);
            insertAfter = r;
          }
        }

        const rows = tbody.querySelectorAll("tr[data-cid]");
        const ids = Array.from(rows).map(r => r.dataset.cid);
        if (!gid) return;
        try {
          await api(`/api/groups/${gid}/reorder-channels`, { method: "POST", body: JSON.stringify({ channel_ids: ids }) });
        } catch (e) { toast("error", "Reorder failed", e.message); }
      }
    });
  }

  // Channel inline edits
  $$(".ch-name, .ch-tvgid, .ch-tvgname", container).forEach(inp => {
    inp.addEventListener("change", async () => {
      const field = inp.classList.contains("ch-name") ? "name" :
                    inp.classList.contains("ch-tvgid") ? "tvg_id" : "tvg_name";
      try {
        await api(`/api/channels/${inp.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ [field]: inp.value }) });
      } catch (e) { toast("error", "Update failed", e.message); }
    });
  });

  // Channel play button — opens the preview player for this channel
  $$(".ch-play-btn", container).forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      previewChannelFromEditor(btn.dataset.cid, btn.dataset.gid);
    });
  });

  // Channel move-to-top button
  $$(".ch-top-btn", container).forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const cid = btn.dataset.cid;
      const gid = btn.dataset.gid;
      // If part of a multi-selection, move all selected to top
      const selectedIds = getSelectedIds(gid);
      if (selectedIds.length > 1 && selectedIds.includes(cid)) {
        try {
          await api(`/api/groups/${gid}/channels/bulk-move-to-top`, { method: "POST", body: JSON.stringify({ channel_ids: selectedIds }) });
          toast("success", `${selectedIds.length} channels moved to top`);
          await loadGroupChannels(gid, 0);
          renderGroups();
        } catch (e) { toast("error", "Move failed", e.message); }
      } else {
        try {
          await api(`/api/groups/${gid}/channels/${cid}/move-to-top`, { method: "POST" });
          toast("success", "Channel moved to top");
          await loadGroupChannels(gid, 0);
          renderGroups();
        } catch (e) { toast("error", "Move failed", e.message); }
      }
    });
  });

  // Channel toggle
  $$(".ch-toggle", container).forEach(cb => {
    cb.addEventListener("change", async () => {
      try { await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ enabled: cb.checked }) }); }
      catch (e) { toast("error", "Toggle failed", e.message); cb.checked = !cb.checked; }
    });
  });

  // Channel favorite
  $$(".ch-fav", container).forEach(cb => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ favorite: cb.checked }) });
      } catch (e) { toast("error", "Update failed", e.message); cb.checked = !cb.checked; }
    });
  });

  // Placement lock: rules may never move a locked station.
  $$(".ch-lock", container).forEach(cb => {
    cb.addEventListener("change", async () => {
      try {
        await api(`/api/channels/${cb.dataset.cid}`, {
          method: "PATCH", body: JSON.stringify({ placement_locked: cb.checked })
        });
      } catch (e) { toast("error", "Lock update failed", e.message); cb.checked = !cb.checked; }
    });
  });

  // Channel move (per-row dropdown moves all selected if multi-selected)
  $$(".ch-move", container).forEach(sel => {
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      const toGid = sel.value;
      const cid = sel.dataset.cid;
      const gid = container.dataset?.gid || container.querySelector(".group-header")?.dataset?.gid;
      const selectedIds = gid ? getSelectedIds(gid) : [];
      // If this channel is part of a multi-selection, move all selected
      if (selectedIds.length > 1 && selectedIds.includes(cid)) {
        try {
          await api("/api/channels/bulk-move", { method: "POST", body: JSON.stringify({ channel_ids: selectedIds, to_group_id: toGid }) });
          toast("success", `${selectedIds.length} channels moved`);
          sel.value = "";
          await refreshOpenGroups();
        } catch (e) { toast("error", "Move failed", e.message); sel.value = ""; }
      } else {
        try {
          await api(`/api/channels/${cid}/move`, { method: "POST", body: JSON.stringify({ to_group_id: toGid }) });
          toast("success", "Channel moved");
          sel.value = "";
          await refreshOpenGroups();
        } catch (e) { toast("error", "Move failed", e.message); sel.value = ""; }
      }
    });
  });

  // Channel copy (per-row dropdown copies all selected if multi-selected)
  $$(".ch-copy", container).forEach(sel => {
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      const toGid = sel.value;
      const cid = sel.dataset.cid;
      const gid = container.dataset?.gid || container.querySelector(".group-header")?.dataset?.gid;
      const selectedIds = gid ? getSelectedIds(gid) : [];
      if (selectedIds.length > 1 && selectedIds.includes(cid)) {
        try {
          await api("/api/channels/bulk-copy", { method: "POST", body: JSON.stringify({ channel_ids: selectedIds, to_group_id: toGid }) });
          toast("success", `${selectedIds.length} channels copied`);
          sel.value = "";
          await refreshOpenGroups();
        } catch (e) { toast("error", "Copy failed", e.message); sel.value = ""; }
      } else {
        try {
          await api("/api/channels/bulk-copy", { method: "POST", body: JSON.stringify({ channel_ids: [cid], to_group_id: toGid }) });
          toast("success", "Channel copied");
          sel.value = "";
          await refreshOpenGroups();
        } catch (e) { toast("error", "Copy failed", e.message); sel.value = ""; }
      }
    });
  });

  // EPG action dropdown
  $$(".ch-epg-action", container).forEach(sel => {
    sel.addEventListener("change", () => {
      const action = sel.value;
      if (action) handleEpgAction(sel.dataset.cid, action);
    });
  });

  // Select all
  $$(".grp-sel-all, .grp-sel-all-hdr", container).forEach(cb => {
    cb.addEventListener("change", () => {
      const gid = cb.dataset.gid;
      $$(`.ch-sel[data-gid="${gid}"]`, container).forEach(c => c.checked = cb.checked);
      updateSelCount(gid);
    });
  });
  $$(".ch-sel", container).forEach(cb => {
    cb.addEventListener("change", () => updateSelCount(cb.dataset.gid));
  });

  // Shift-click range select for channel checkboxes within this group
  const gidForShift = container.dataset?.gid || container.querySelector(".group-header")?.dataset?.gid;
  if (gidForShift) {
    wireShiftSelect(`.ch-sel[data-gid="${gidForShift}"]`, container, (cb) => updateSelCount(gidForShift));
  }

  // Rule preview (debounced)
  $$(".rule-pattern", container).forEach(inp => {
    let t;
    inp.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const row = inp.closest(".rule-add-row");
        const gid = row.querySelector(".rule-preview-count")?.dataset.gid;
        if (!gid || !inp.value.trim()) {
          if (gid) $(`.rule-preview-count[data-gid="${gid}"]`).textContent = "";
          return;
        }
        try {
          const r = await api(`/api/groups/${gid}/rules/preview`, {
            method: "POST",
            body: JSON.stringify({
              field: row.querySelector(".rule-field").value,
              match_type: row.querySelector(".rule-match").value,
              pattern: inp.value,
            }),
          });
          $(`.rule-preview-count[data-gid="${gid}"]`).textContent = `${r.match_count} match${r.match_count !== 1 ? "es" : ""}`;
        } catch { /* ignore */ }
      }, 400);
    });
  });
}

function updateSelCount(gid) {
  const count = $$(`.ch-sel[data-gid="${gid}"]:checked`).length;
  const el = $(`.sel-count[data-gid="${gid}"]`);
  if (el) el.textContent = `${count} selected`;
}

function getSelectedIds(gid) {
  return $$(`.ch-sel[data-gid="${gid}"]:checked`).map(c => c.dataset.cid);
}

let groupSortable;
function wireSortableGroups() {
  const el = $("#groups-list");
  if (groupSortable) groupSortable.destroy();
  if (!el.children.length || groupFilterText || groupViewMode !== "all" || groupCategoryFilter) return;
  groupSortable = new Sortable(el, {
    animation: 200,
    handle: ".drag-handle",
    draggable: ".group-card",
    filter: ".category-divider",
    ghostClass: "sortable-ghost",
    chosenClass: "sortable-chosen",
    scroll: true,
    scrollSensitivity: 100,
    scrollSpeed: 20,
    bubbleScroll: true,
    onStart: (evt) => {
      // If dragged item is selected, tag all selected for multi-drag
      const draggedGid = evt.item.dataset.gid;
      const selectedGids = getSelectedGroupIds();
      if (selectedGids.length > 1 && selectedGids.includes(draggedGid)) {
        evt.item._multiDragIds = selectedGids;
        // Hide other selected items (they'll be moved on end)
        $$(".group-card", el).forEach(c => {
          if (c !== evt.item && selectedGids.includes(c.dataset.gid)) {
            c.classList.add("multi-drag-hidden");
          }
        });
      }
    },
    onEnd: async (evt) => {
      const multiIds = evt.item._multiDragIds;
      delete evt.item._multiDragIds;

      if (multiIds && multiIds.length > 1) {
        // Remove hidden class
        $$(".multi-drag-hidden", el).forEach(c => c.classList.remove("multi-drag-hidden"));

        // Get current DOM order, then move all selected items to right after the dragged item
        const allCards = $$(".group-card", el);
        const draggedIdx = allCards.indexOf(evt.item);
        const others = multiIds.filter(id => id !== evt.item.dataset.gid);

        // Remove other selected from DOM
        const otherEls = others.map(id => el.querySelector(`.group-card[data-gid="${id}"]`)).filter(Boolean);
        otherEls.forEach(c => c.remove());

        // Insert them after the dragged item
        let insertAfter = evt.item;
        for (const c of otherEls) {
          insertAfter.after(c);
          insertAfter = c;
        }
      }

      // Read final order from DOM
      const ids = $$(".group-card", el).map(c => c.dataset.gid);
      try {
        await api("/api/groups/reorder", { method: "POST", body: JSON.stringify({ group_ids: ids }) });
        // Refresh data and re-render to update numbers
        await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
        await syncPrioritiesToPinned();
        renderGroups();
      } catch (e) { toast("error", "Reorder failed", e.message); }
    },
  });
}

async function loadGroupChannels(gid, offset) {
  const encoded = encodeURIComponent(gid);
  const [data, listingData] = await Promise.all([
    api(`/api/groups/${encoded}/channels?offset=${offset}&limit=200`),
    offset === 0
      ? api(`/api/groups/${encoded}/listings/current`).catch(() => ({ listings: {}, now: null }))
      : Promise.resolve(null),
  ]);
  const channels = Array.isArray(data?.channels) ? data.channels : [];
  const total = Number.isFinite(Number(data?.total)) ? Number(data.total) : channels.length;
  if (!channelCache[gid] || offset === 0) {
    channelCache[gid] = {
      channels, total,
      listings: listingData?.listings || {},
      listingsNow: listingData?.now || null,
    };
  } else {
    channelCache[gid].channels.push(...channels);
    channelCache[gid].total = total;
  }
}

window.loadMoreChannels = async (gid) => {
  const cache = channelCache[gid];
  if (!cache) return;
  await loadGroupChannels(gid, cache.channels.length);
  // Re-render just this group's body
  const card = document.querySelector(`.group-card[data-gid="${gid}"]`);
  const g = groupsData.find(g => g.id === gid);
  if (card && g) {
    const oldBody = card.querySelector(".group-body");
    if (oldBody && oldBody.parentNode) {
      const wrap = document.createElement("div");
      wrap.innerHTML = renderGroupBody(g);
      const nextBody = wrap.firstElementChild;
      if (nextBody) oldBody.replaceWith(nextBody);
    }
    wireGroupBodyEvents(card);
  }
};

async function refreshOpenGroups() {
  // Parallel: fetch all expanded groups + group list concurrently
  await Promise.all([
    ...Array.from(expandedGroups).map(gid => loadGroupChannels(gid, 0)),
    fetchGroups(),
  ]);
  renderGroups();
}

// Group actions
window.renameGroup = async (gid) => {
  const g = groupsData.find(g => g.id === gid);
  const name = prompt("Rename group:", g?.name || "");
  if (!name?.trim() || name.trim() === g?.name) return;
  try {
    await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
    toast("success", "Group renamed");
    await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
    renderGroups();
  } catch (e) { toast("error", "Rename failed", e.message); }
};

window.sortSxmGroup = async (gid) => {
  try {
    const res = await api(`/api/groups/${gid}/sort-sxm`, { method: "POST" });
    toast("success", "SiriusXM sort applied", `${res.sorted} channels reordered`);
    // Reload the group channels if expanded
    if (expandedGroups.has(gid)) {
      await loadGroupChannels(gid, 0);
      const g = groupsData.find(x => x.id === gid);
      const card = document.querySelector(`.group-card[data-gid="${gid}"]`);
      if (g && card) {
        const body = card.querySelector(".group-body");
        if (body && body.parentNode) {
          const wrap = document.createElement("div");
          wrap.innerHTML = renderGroupBody(g);
          const nextBody = wrap.firstElementChild;
          if (nextBody) body.replaceWith(nextBody);
        }
        wireGroupBodyEvents(card, g);
      }
    }
  } catch (e) { toast("error", "SXM sort failed", e.message); }
};

async function syncPrioritiesToPinned() {
  try {
    const pinned = groupsData.filter(g => g.pinned).map(g => g.name);
    await api("/api/settings/group-sort-priorities", { method: "PUT", body: JSON.stringify(pinned) });
  } catch (_) {}
}

window.pinGroup = async (gid, pin) => {
  try {
    await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ pinned: pin }) });
    await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
    await syncPrioritiesToPinned();
    toast("success", pin ? "Group pinned" : "Group unpinned");
    renderGroups();
  } catch (e) { toast("error", "Failed", e.message); }
};

window.moveGroupTop = async (gid) => {
  try {
    await api(`/api/groups/${gid}/move-to-top`, { method: "POST" });
    toast("success", "Moved to top");
    await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
    renderGroups();
  } catch (e) { toast("error", "Failed", e.message); }
};

window.deleteGroupConfirm = async (gid, name) => {
  const ok = await confirmDialog("Delete Group", `Delete "${name}"? Channels will be moved to Unmatched.`, "Delete Group");
  if (!ok) return;
  expandedGroups.delete(gid);
  delete channelCache[gid];
  try {
    await api(`/api/groups/${gid}`, { method: "DELETE" });
    toast("success", "Group deleted");
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

// ── Group multi-select ──────────────────────────────────────────
function getSelectedGroupIds() {
  return $$(".grp-sel:checked").map(c => c.dataset.gid);
}

function updateGroupSelBar() {
  const ids = getSelectedGroupIds();
  let bar = $("#group-sel-bar");
  if (!ids.length) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "group-sel-bar";
    bar.className = "group-sel-bar";
    document.body.appendChild(bar);
  }
  // Build "Move after" dropdown from unselected groups
  const unselected = groupsData.filter(g => !ids.includes(g.id));
  const moveOpts = unselected.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  // Build category options for bulk assignment
  const catOpts = categoriesData.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
  bar.innerHTML = `
    <span class="group-sel-count">${ids.length} group${ids.length > 1 ? "s" : ""} selected</span>
    <button class="btn btn-sm" onclick="bulkPinGroups(true)" title="Pin selected">📌 Pin</button>
    <button class="btn btn-sm" onclick="bulkPinGroups(false)" title="Unpin selected">Unpin</button>
    <button class="btn btn-sm" onclick="bulkMoveGroupsTop()">⬆ Top</button>
    <button class="btn btn-sm" onclick="bulkMoveGroupsBottom()">⬇ Bottom</button>
    <select id="group-move-after" class="group-move-after-select">
      <option value="">Move after...</option>${moveOpts}
    </select>
    <button class="btn btn-sm" onclick="bulkMoveGroupsAfter()">Move</button>
    <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
    <select id="group-cat-assign" style="font-size:12px" onchange="bulkAssignCategory(this.value);this.selectedIndex=0">
      <option value="">Category...</option>
      ${catOpts}
      <option value="__new__">+ New Category</option>
      <option value="__none__">Remove Category</option>
    </select>
    <span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>
    <button class="btn btn-sm btn-success" onclick="bulkToggleGroups(true)">Enable</button>
    <button class="btn btn-sm btn-ghost" onclick="bulkToggleGroups(false)">Disable</button>
    <button class="btn btn-sm btn-danger" onclick="bulkDeleteGroups()">Delete</button>
    <button class="btn btn-sm" onclick="clearGroupSel()">✕</button>
  `;
}

window.clearGroupSel = () => {
  $$(".grp-sel").forEach(c => c.checked = false);
  updateGroupSelBar();
};

async function reorderAndRefresh(newIds) {
  try {
    await api("/api/groups/reorder", { method: "POST", body: JSON.stringify({ group_ids: newIds }) });
    await Promise.all([fetchGroups(), fetchGroupOrderHealth()]);
    await syncPrioritiesToPinned();
    renderGroups();
    updateGroupSelBar();
  } catch (e) { toast("error", "Reorder failed", e.message); }
}

window.bulkMoveGroupsTop = async () => {
  const sel = getSelectedGroupIds();
  if (!sel.length) return;
  const rest = groupsData.filter(g => !sel.includes(g.id)).map(g => g.id);
  await reorderAndRefresh([...sel, ...rest]);
  toast("success", `${sel.length} group(s) moved to top`);
};

window.bulkPinGroups = async (pin) => {
  const sel = getSelectedGroupIds();
  if (!sel.length) return;
  try {
    for (const gid of sel) {
      await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ pinned: pin }) });
    }
    await fetchGroups();
    await syncPrioritiesToPinned();
    toast("success", `${sel.length} group(s) ${pin ? "pinned" : "unpinned"}`);
    renderGroups();
    updateGroupSelBar();
  } catch (e) { toast("error", "Failed", e.message); }
};

window.bulkMoveGroupsBottom = async () => {
  const sel = getSelectedGroupIds();
  if (!sel.length) return;
  const rest = groupsData.filter(g => !sel.includes(g.id)).map(g => g.id);
  await reorderAndRefresh([...rest, ...sel]);
  toast("success", `${sel.length} group(s) moved to bottom`);
};

window.bulkMoveGroupsAfter = async () => {
  const sel = getSelectedGroupIds();
  const afterGid = $("#group-move-after")?.value;
  if (!sel.length || !afterGid) { toast("info", "Select a target group"); return; }
  const rest = groupsData.filter(g => !sel.includes(g.id)).map(g => g.id);
  const idx = rest.indexOf(afterGid);
  if (idx === -1) return;
  rest.splice(idx + 1, 0, ...sel);
  await reorderAndRefresh(rest);
  toast("success", `${sel.length} group(s) moved`);
};

window.bulkToggleGroups = async (enabled) => {
  const sel = getSelectedGroupIds();
  if (!sel.length) return;
  try {
    for (const gid of sel) {
      await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
    }
    toast("success", `${sel.length} group(s) ${enabled ? "enabled" : "disabled"}`);
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Failed", e.message); }
};

window.bulkDeleteGroups = async () => {
  const sel = getSelectedGroupIds();
  if (!sel.length) return;
  const ok = await confirmDialog("Delete Groups", `Delete ${sel.length} selected group(s)? Channels will be moved to Unmatched.`, "Delete");
  if (!ok) return;
  try {
    for (const gid of sel) {
      expandedGroups.delete(gid);
      delete channelCache[gid];
      await api(`/api/groups/${gid}`, { method: "DELETE" });
    }
    toast("success", `${sel.length} group(s) deleted`);
    await fetchGroups();
    renderGroups();
    updateGroupSelBar();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

window.bulkAssignCategory = async (value) => {
  if (!value) return;
  const sel = getSelectedGroupIds();
  if (!sel.length) { toast("info", "No groups selected"); return; }

  let catName = "";
  if (value === "__new__") {
    // Prompt for new category name and color
    const name = prompt("New category name:");
    if (!name || !name.trim()) return;
    catName = name.trim();
    // Check if already exists
    if (!categoriesData.some(c => c.name.toLowerCase() === catName.toLowerCase())) {
      // Pick a default color from a palette
      const palette = ["#3b82f6","#ef4444","#22c55e","#f59e0b","#8b5cf6","#ec4899","#06b6d4","#f97316"];
      const color = palette[categoriesData.length % palette.length];
      categoriesData.push({ name: catName, color });
      await saveCategories();
    }
  } else if (value === "__none__") {
    catName = "";
  } else {
    catName = value;
  }

  try {
    for (const gid of sel) {
      await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ category: catName }) });
    }
    toast("success", catName ? `${sel.length} group(s) → ${catName}` : `Category removed from ${sel.length} group(s)`);
    await fetchGroups();
    renderGroups();
    updateGroupSelBar();
  } catch (e) { toast("error", "Category assign failed", e.message); }
};

window.setChannelNameEpg = async (cid) => {
  try {
    await api(`/api/channels/${cid}/set-name-epg`, { method: "POST" });
    toast("success", "Dummy EPG set");
    const inp = document.querySelector(`.ch-tvgid[data-cid="${cid}"]`);
    if (inp) {
      const ch = await api(`/api/channels/${cid}`);
      if (ch) inp.value = ch.tvg_id || "";
    }
  } catch (e) { toast("error", "Failed", e.message); }
};

window.handleEpgAction = async (cid, action) => {
  if (!action) return;
  if (action === "search") {
    openEpgSearch(cid);
    // Reset dropdown
    const sel = document.querySelector(`.ch-epg-action[data-cid="${cid}"]`);
    if (sel) sel.selectedIndex = 0;
    return;
  }
  const urlMap = { name: "set-name-epg", unset: "unset-epg", restore: "restore-epg" };
  const labelMap = { name: "Dummy EPG set", unset: "EPG cleared", restore: "Original EPG restored" };
  try {
    await api(`/api/channels/${cid}/${urlMap[action]}`, { method: "POST" });
    toast("success", labelMap[action]);
    const ch = await api(`/api/channels/${cid}`);
    const inp = document.querySelector(`.ch-tvgid[data-cid="${cid}"]`);
    if (inp && ch) inp.value = ch.tvg_id || "";
    // Reset the select back to display current state
    const sel = document.querySelector(`.ch-epg-action[data-cid="${cid}"]`);
    if (sel && ch) {
      const isDummy = (ch.tvg_id || "").startsWith("dummy.");
      sel.options[0].textContent = isDummy ? "Dummy" : (ch.tvg_id ? "Match" : "None");
      sel.style.color = isDummy ? "var(--accent)" : "";
      sel.selectedIndex = 0;
    }
  } catch (e) { toast("error", "EPG action failed", e.message); }
};

// Rules
window.addRule = async (gid, btn) => {
  const row = btn.closest(".rule-add-row");
  const pattern = row.querySelector(".rule-pattern").value.trim();
  if (!pattern) return;
  btnLoad(btn, "Adding...");
  try {
    await api(`/api/groups/${gid}/rules`, {
      method: "POST",
      body: JSON.stringify({
        field: row.querySelector(".rule-field").value,
        match_type: row.querySelector(".rule-match").value,
        pattern,
      }),
    });
    row.querySelector(".rule-pattern").value = "";
    row.querySelector(".rule-preview-count").textContent = "";
    toast("success", "Rule added");
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Failed to add rule", e.message); }
  finally { btnDone(btn); }
};

window.deleteRule = async (gid, rid) => {
  try {
    await api(`/api/groups/${gid}/rules/${rid}`, { method: "DELETE" });
    toast("success", "Rule removed");
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Failed to delete rule", e.message); }
};

window.editRule = async (gid, rid, field, matchType, pattern) => {
  const newPattern = prompt("Edit pattern:", pattern);
  if (newPattern === null || newPattern === pattern) return;
  if (!newPattern.trim()) { toast("error", "Pattern cannot be empty"); return; }
  try {
    await api(`/api/groups/${gid}/rules/${rid}`, {
      method: "PATCH",
      body: JSON.stringify({ field, match_type: matchType, pattern: newPattern.trim() })
    });
    toast("success", "Rule updated");
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Failed to update rule", e.message); }
};

// Bulk ops
window.bulkMoveGroup = async (gid) => {
  const sel = $(`.grp-bulk-move[data-gid="${gid}"]`);
  if (!sel?.value) return;
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  const tid = toast("loading", "Moving channels", `${ids.length} channel${ids.length > 1 ? "s" : ""}...`);
  try {
    await api("/api/channels/bulk-move", { method: "POST", body: JSON.stringify({ channel_ids: ids, to_group_id: sel.value }) });
    sel.value = "";
    updateToast(tid, "success", "Channels moved");
    await refreshOpenGroups();
  } catch (e) { updateToast(tid, "error", "Move failed", e.message); }
};

window.bulkCopyGroup = async (gid) => {
  const sel = $(`.grp-bulk-move[data-gid="${gid}"]`);
  if (!sel?.value) return;
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  const tid = toast("loading", "Copying channels", `${ids.length} channel${ids.length > 1 ? "s" : ""}...`);
  try {
    await api("/api/channels/bulk-copy", { method: "POST", body: JSON.stringify({ channel_ids: ids, to_group_id: sel.value }) });
    sel.value = "";
    updateToast(tid, "success", "Channels copied");
    await refreshOpenGroups();
  } catch (e) { updateToast(tid, "error", "Copy failed", e.message); }
};

window.bulkToggleGroup = async (gid, enabled) => {
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  try {
    await api("/api/channels/bulk-toggle", { method: "POST", body: JSON.stringify({ channel_ids: ids, enabled }) });
    toast("success", enabled ? "Channels enabled" : "Channels disabled", `${ids.length} updated`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Toggle failed", e.message); }
};

window.bulkFavGroup = async (gid, fav) => {
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  try {
    await api("/api/channels/bulk-favorite", { method: "POST", body: JSON.stringify({ channel_ids: ids, favorite: fav }) });
    toast("success", "Favorites updated", `${ids.length} channel${ids.length > 1 ? "s" : ""}`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Update failed", e.message); }
};

window.bulkLockGroup = async (gid, locked) => {
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  try {
    await api("/api/channels/bulk-lock", { method: "POST", body: JSON.stringify({ channel_ids: ids, locked }) });
    toast("success", locked ? "Station placement locked" : "Station placement unlocked", `${ids.length} updated`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Lock update failed", e.message); }
};

window.bulkEpgGroup = async (gid, action) => {
  if (!action) return;
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  const labels = { name: "Dummy EPG applied", unset: "EPG cleared", restore: "Original EPG restored" };
  try {
    await api("/api/channels/bulk-epg", { method: "POST", body: JSON.stringify({ channel_ids: ids, action }) });
    toast("success", labels[action] || "EPG updated", `${ids.length} channel${ids.length > 1 ? "s" : ""}`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "EPG action failed", e.message); }
};

window.deleteChannel = async (cid, gid) => {
  try {
    await api(`/api/channels/${cid}`, { method: "DELETE" });
    toast("success", "Channel deleted");
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

window.bulkDeleteGroup = async (gid) => {
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  const ok = await confirmDialog("Delete Channels", `Delete ${ids.length} selected channel(s)? This cannot be undone.`, "Delete");
  if (!ok) return;
  try {
    await api("/api/channels/bulk-delete", { method: "POST", body: JSON.stringify({ channel_ids: ids }) });
    toast("success", "Channels deleted", `${ids.length} removed`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

window.bulkMoveChannelsTop = async (gid) => {
  const ids = getSelectedIds(gid);
  if (!ids.length) { toast("info", "No channels selected"); return; }
  try {
    await api(`/api/groups/${gid}/channels/bulk-move-to-top`, { method: "POST", body: JSON.stringify({ channel_ids: ids }) });
    toast("success", `${ids.length} channel(s) moved to top`);
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Move failed", e.message); }
};

window.editChannelPriorities = async (gid) => {
  let prios = [];
  try { prios = await api(`/api/groups/${gid}/channel-priorities`); } catch (_) {}
  const val = prompt("Channel priority keywords (comma-separated).\nChannels matching earlier keywords sort first:", prios.join(", "));
  if (val === null) return;
  const newPrios = val.split(",").map(s => s.trim()).filter(Boolean);
  try {
    await api(`/api/groups/${gid}/channel-priorities`, { method: "PUT", body: JSON.stringify(newPrios) });
    toast("success", `${newPrios.length} priority keyword(s) saved`);
  } catch (e) { toast("error", "Failed", e.message); }
};

window.sortChannelsByPriority = async (gid) => {
  try {
    const r = await api(`/api/groups/${gid}/sort-channels-by-priority`, { method: "POST" });
    toast("success", "Channels sorted by priority");
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Sort failed", e.message); }
};

window.sortChannelsAlpha = async (gid) => {
  try {
    await api(`/api/groups/${gid}/sort-alpha`, { method: "POST" });
    toast("success", "Channels sorted A–Z");
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Sort failed", e.message); }
};

window.sortChannelsBySource = async (gid) => {
  try {
    await api(`/api/groups/${gid}/sort-by-source`, { method: "POST" });
    toast("success", "Channels sorted by source priority");
    await loadGroupChannels(gid, 0);
    renderGroups();
  } catch (e) { toast("error", "Sort failed", e.message); }
};

// Add group
$("#add-group-btn").addEventListener("click", async () => {
  const name = prompt("Group name:");
  if (!name?.trim()) return;
  try {
    await api("/api/groups", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
    toast("success", "Group created", name.trim());
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Failed to create group", e.message); }
});

// Sort A-Z
$("#sort-alpha-btn").addEventListener("click", async () => {
  const ok = await confirmDialog("Sort Groups A-Z", "This will reorder all groups alphabetically (with your priority keywords at top). Your current manual order will be lost.", "Sort A-Z");
  if (!ok) return;
  const btn = $("#sort-alpha-btn");
  btnLoad(btn, "Sorting...");
  try {
    await api("/api/groups/sort-alpha", { method: "POST" });
    await fetchGroups();
    renderGroups();
    toast("success", "Groups sorted alphabetically");
  } catch (e) { toast("error", "Sort failed", e.message); }
  finally { btnDone(btn); }
});

// Reset Source
$("#reset-source-btn").addEventListener("click", async () => {
  const ok = await confirmDialog(
    "Reset Source",
    "This will DELETE all groups, rules, and channel assignments, then reimport everything from the active source from scratch. Your priority and dummy EPG keywords will be reapplied automatically. This cannot be undone.",
    "Reset Everything"
  );
  if (!ok) return;
  const btn = $("#reset-source-btn");
  btnLoad(btn, "Resetting...");
  const tid = toast("loading", "Resetting source", "Wiping groups and reimporting...");
  try {
    const r = await api("/api/sources/reset", { method: "POST" });
    channelCache = {};
    expandedGroups.clear();
    await fetchGroups();
    renderGroups();
    updateToast(tid, "success", "Source reset", `Reimported ${r.channels ?? 0} channels`);
  } catch (e) { updateToast(tid, "error", "Reset failed", e.message); }
  finally { btnDone(btn); }
});

// Priorities panel
let prioritiesData = [];

$("#priorities-btn").addEventListener("click", async () => {
  const panel = $("#priorities-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadPriorities();
});
$("#priorities-close").addEventListener("click", () => { $("#priorities-panel").hidden = true; });

async function loadPriorities() {
  try {
    prioritiesData = await api("/api/settings/group-sort-priorities");
    renderPriorities();
  } catch (e) { console.error(e); prioritiesData = []; renderPriorities(); }
}

function renderPriorities() {
  const el = $("#priorities-list");
  if (!prioritiesData.length) {
    el.innerHTML = `<div class="text-muted" style="padding:8px 0">No priority keywords set. Groups will be sorted alphabetically.</div>`;
    return;
  }
  el.innerHTML = prioritiesData.map((kw, i) => `
    <div class="priority-item" data-idx="${i}">
      <span class="drag-handle">${ICO.grip}</span>
      <span class="priority-rank">${i + 1}</span>
      <span class="priority-keyword">${esc(kw)}</span>
      <button class="btn-icon-only" onclick="removePriority(${i})" title="Remove" style="color:var(--danger)">${ICO.x}</button>
    </div>`).join("");
  wirePrioritySortable();
}

let prioritySortable;
function wirePrioritySortable() {
  const el = $("#priorities-list");
  if (prioritySortable) prioritySortable.destroy();
  if (!el.children.length) return;
  prioritySortable = new Sortable(el, {
    animation: 200,
    handle: ".drag-handle",
    ghostClass: "sortable-ghost",
    onEnd: async () => {
      const items = $$(".priority-item", el);
      prioritiesData = items.map(item => {
        const idx = parseInt(item.dataset.idx);
        return prioritiesData[idx];
      });
      // Re-read from DOM order since drag changed it
      prioritiesData = items.map(item => item.querySelector(".priority-keyword").textContent);
      await savePriorities();
      renderPriorities();
    },
  });
}

async function savePriorities() {
  try {
    await api("/api/settings/group-sort-priorities", {
      method: "PUT",
      body: JSON.stringify(prioritiesData),
    });
  } catch (e) { toast("error", "Save failed", e.message); }
}

window.removePriority = async (idx) => {
  prioritiesData.splice(idx, 1);
  await savePriorities();
  renderPriorities();
  toast("info", "Priority removed");
};

$("#add-priority-btn").addEventListener("click", async () => {
  const inp = $("#new-priority-input");
  const kw = inp.value.trim();
  if (!kw) return;
  if (prioritiesData.some(p => p.toLowerCase() === kw.toLowerCase())) {
    toast("info", "Keyword already exists");
    return;
  }
  prioritiesData.push(kw);
  inp.value = "";
  await savePriorities();
  renderPriorities();
  toast("success", "Priority added", kw);
});
$("#new-priority-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#add-priority-btn").click(); }
});

// Categories panel
$("#categories-btn").addEventListener("click", async () => {
  const panel = $("#categories-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) { panel.scrollIntoView({ behavior: "smooth", block: "start" }); await loadCategoriesPanel(); }
});
$("#categories-close").addEventListener("click", () => { $("#categories-panel").hidden = true; });

async function loadCategoriesPanel() {
  await fetchCategories();
  renderCategoriesPanel();
}

function renderCategoriesPanel() {
  const el = $("#categories-list");
  if (!categoriesData.length) {
    el.innerHTML = `<div class="text-muted" style="padding:8px 0">No categories yet. Add one above to start organizing groups into color-coded bands.</div>`;
    return;
  }
  // Count groups per category
  const counts = {};
  groupsData.forEach(g => { const c = g.category || ""; if (c) counts[c] = (counts[c] || 0) + 1; });
  el.innerHTML = categoriesData.map((c, i) => `
    <div class="cat-list-item" data-idx="${i}">
      <span class="drag-handle">${ICO.grip}</span>
      <span class="cat-swatch" style="background:${esc(c.color)}"></span>
      <span class="cat-name">${esc(c.name)}</span>
      <span class="cat-group-count">${counts[c.name] || 0} groups</span>
      <input type="color" value="${esc(c.color)}" class="cat-color-pick" data-idx="${i}" style="width:24px;height:24px;padding:0;border:none;cursor:pointer;background:none" title="Change color" />
      <button class="btn-icon-only" onclick="removeCategory(${i})" title="Remove" style="color:var(--danger)">${ICO.x}</button>
    </div>`).join("");
  wireCategorySortable();
  // Color picker change
  $$(".cat-color-pick").forEach(inp => {
    inp.addEventListener("change", async () => {
      const idx = parseInt(inp.dataset.idx);
      categoriesData[idx].color = inp.value;
      await saveCategories();
      renderCategoriesPanel();
      renderGroups();
    });
  });
}

let categorySortable;
function wireCategorySortable() {
  const el = $("#categories-list");
  if (categorySortable) categorySortable.destroy();
  if (!el.children.length) return;
  categorySortable = new Sortable(el, {
    animation: 200,
    handle: ".drag-handle",
    ghostClass: "sortable-ghost",
    onEnd: async () => {
      const items = $$(".cat-list-item", el);
      categoriesData = items.map(item => {
        const idx = parseInt(item.dataset.idx);
        return categoriesData[idx];
      });
      // Re-read from DOM order
      categoriesData = items.map(item => {
        const name = item.querySelector(".cat-name").textContent;
        const color = item.querySelector(".cat-swatch").style.background;
        return { name, color };
      });
      await saveCategories();
      renderCategoriesPanel();
      // Re-sort groups to reflect new band order
      await fetchGroups();
      renderGroups();
    },
  });
}

async function saveCategories() {
  try {
    categoriesData = await api("/api/settings/group-categories", {
      method: "PUT",
      body: JSON.stringify(categoriesData),
    });
  } catch (e) { toast("error", "Save failed", e.message); }
}

window.removeCategory = async (idx) => {
  const name = categoriesData[idx].name;
  categoriesData.splice(idx, 1);
  await saveCategories();
  // Unassign groups that had this category
  const affected = groupsData.filter(g => g.category === name);
  for (const g of affected) {
    try { await api(`/api/groups/${g.id}`, { method: "PATCH", body: JSON.stringify({ category: "" }) }); } catch(_) {}
  }
  if (affected.length) { await fetchGroups(); renderGroups(); }
  renderCategoriesPanel();
  toast("info", `Category "${name}" removed`);
};

$("#add-cat-btn").addEventListener("click", async () => {
  const nameInp = $("#new-cat-name");
  const colorInp = $("#new-cat-color");
  const name = nameInp.value.trim();
  if (!name) return;
  if (categoriesData.some(c => c.name.toLowerCase() === name.toLowerCase())) {
    toast("info", "Category already exists");
    return;
  }
  categoriesData.push({ name, color: colorInp.value });
  nameInp.value = "";
  await saveCategories();
  renderCategoriesPanel();
  toast("success", "Category added", name);
});
$("#new-cat-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#add-cat-btn").click(); }
});

// Dummy EPG keywords panel
let nameEpgData = [];

$("#name-epg-btn").addEventListener("click", async () => {
  const panel = $("#name-epg-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadNameEpgKeywords();
});
$("#name-epg-close").addEventListener("click", () => { $("#name-epg-panel").hidden = true; });

async function loadNameEpgKeywords() {
  try {
    nameEpgData = await api("/api/settings/name-epg-keywords");
    renderNameEpgKeywords();
  } catch (e) { console.error(e); nameEpgData = []; renderNameEpgKeywords(); }
}

function renderNameEpgKeywords() {
  const el = $("#name-epg-list");
  if (!nameEpgData.length) {
    el.innerHTML = `<div class="text-muted" style="padding:8px 0">No keywords set. Groups matching keywords will auto-get name-based EPG on import.</div>`;
    return;
  }
  el.innerHTML = nameEpgData.map((kw, i) => `
    <div class="priority-item" data-idx="${i}">
      <span class="priority-rank" style="background:rgba(34,197,94,.15);color:#4ade80">${i + 1}</span>
      <span class="priority-keyword">${esc(kw)}</span>
      <button class="btn-icon-only" onclick="removeNameEpgKw(${i})" title="Remove" style="color:var(--danger)">${ICO.x}</button>
    </div>`).join("");
}

async function saveNameEpgKeywords() {
  try {
    await api("/api/settings/name-epg-keywords", { method: "PUT", body: JSON.stringify(nameEpgData) });
  } catch (e) { toast("error", "Save failed", e.message); }
}

window.removeNameEpgKw = async (idx) => {
  nameEpgData.splice(idx, 1);
  await saveNameEpgKeywords();
  renderNameEpgKeywords();
  toast("info", "Keyword removed");
};

$("#add-name-epg-btn").addEventListener("click", async () => {
  const inp = $("#new-name-epg-input");
  const kw = inp.value.trim();
  if (!kw) return;
  if (nameEpgData.some(p => p.toLowerCase() === kw.toLowerCase())) {
    toast("info", "Keyword already exists");
    return;
  }
  nameEpgData.push(kw);
  inp.value = "";
  await saveNameEpgKeywords();
  renderNameEpgKeywords();
  toast("success", "Name EPG keyword added", kw);
});
$("#new-name-epg-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#add-name-epg-btn").click(); }
});

/* ── My Teams ──────────────────────────────────────────────────── */
let myTeamsData = [];

$("#my-teams-btn").addEventListener("click", async () => {
  const panel = $("#my-teams-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadMyTeams();
});
$("#my-teams-close").addEventListener("click", () => { $("#my-teams-panel").hidden = true; });

async function loadMyTeams() {
  try {
    myTeamsData = await api("/api/settings/favorite-team-keywords");
    renderMyTeams();
  } catch (e) { console.error(e); myTeamsData = []; renderMyTeams(); }
}

function renderMyTeams() {
  const el = $("#my-teams-list");
  if (!myTeamsData.length) {
    el.innerHTML = `<div class="text-muted" style="padding:8px 0">No team keywords set. Add your favorite teams to boost their channels to the top of each group.</div>`;
    return;
  }
  el.innerHTML = myTeamsData.map((kw, i) => `
    <div class="priority-item" data-idx="${i}">
      <span class="priority-rank" style="background:rgba(250,204,21,.15);color:#facc15">${i + 1}</span>
      <span class="priority-keyword">${esc(kw)}</span>
      <button class="btn-icon-only" onclick="removeTeam(${i})" title="Remove">${ICO.x}</button>
    </div>`).join("");
}

async function saveMyTeams() {
  try { await api("/api/settings/favorite-team-keywords", { method: "PUT", body: JSON.stringify(myTeamsData) }); }
  catch (e) { toast("error", "Save failed", e.message); }
}

window.removeTeam = async (idx) => {
  const removed = myTeamsData.splice(idx, 1)[0];
  await saveMyTeams();
  renderMyTeams();
  toast("success", "Team keyword removed", removed);
};

$("#add-team-btn").addEventListener("click", async () => {
  const inp = $("#new-team-input");
  const kw = inp.value.trim();
  if (!kw) return;
  if (myTeamsData.some(t => t.toLowerCase() === kw.toLowerCase())) {
    toast("info", "Keyword already exists");
    return;
  }
  myTeamsData.push(kw);
  inp.value = "";
  await saveMyTeams();
  renderMyTeams();
  toast("success", "Team keyword added", `Channels matching "${kw}" will sort to top`);
});
$("#new-team-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#add-team-btn").click(); }
});

// Apply rules
$("#apply-rules-btn").addEventListener("click", async () => {
  const btn = $("#apply-rules-btn");
  btnLoad(btn, "Checking...");
  let preview;
  try {
    preview = await api("/api/apply-rules/preview");
  } catch (e) {
    toast("error", "Rule preview failed", e.message); btnDone(btn); return;
  }
  if (!preview.moves) {
    toast("success", "No station moves needed",
      `${preview.protected || 0} protected · ${preview.unchanged || 0} unchanged`);
    btnDone(btn); return;
  }
  const examples = (preview.sample || []).slice(0, 5).map(x =>
    `${x.name} → ${x.to_group}`).join("\n");
  const ok = await confirmDialog(
    "Review Rule Changes",
    `${preview.moves} station${preview.moves === 1 ? "" : "s"} will move. ${preview.protected || 0} are protected and ${preview.unchanged || 0} stay put.${examples ? `\n\nExamples:\n${examples}` : ""}`,
    `Move ${preview.moves}`
  );
  if (!ok) { btnDone(btn); return; }
  btnLoad(btn, "Applying...");
  const tid = toast("loading", "Applying rules", `Moving ${preview.moves} station${preview.moves === 1 ? "" : "s"}...`);
  try {
    const r = await api("/api/apply-rules", { method: "POST" });
    channelCache = {};
    await fetchGroups();
    for (const gid of expandedGroups) await loadGroupChannels(gid, 0);
    renderGroups();
    updateToast(tid, "success", "Rules applied", `${r.moves || 0} moved · ${r.protected || 0} protected`);
  } catch (e) { updateToast(tid, "error", "Apply failed", e.message); }
  finally { btnDone(btn); }
  startExportPoll();
});

/* ── Search ───────────────────────────────────────────────────────────── */
let searchTimer;
let groupFilterTimer;

// Group name filter
$("#group-filter").addEventListener("input", (e) => {
  clearTimeout(groupFilterTimer);
  groupFilterTimer = setTimeout(() => {
    groupFilterText = e.target.value.trim().toLowerCase();
    renderGroups();
  }, 200);
});
$("#group-filter").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; groupFilterText = ""; renderGroups(); }
});

// Channel search
$("#editor-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  if (!e.target.value.trim()) { hideSearch(); return; }
  searchTimer = setTimeout(() => doSearch(e.target.value.trim()), 350);
});
$("#editor-search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; hideSearch(); }
});
$("#search-close").addEventListener("click", () => { $("#editor-search").value = ""; hideSearch(); });

let _searchQuery = "";
let _searchOffset = 0;
let _searchTotal = 0;
let _searchFilters = {}; // {no_epg, favorites, group_id}
let _searchRequestToken = 0;

async function doSearch(q, append = false) {
  if (!append) { _searchOffset = 0; searchResults = []; _searchRequestToken += 1; }
  const requestToken = _searchRequestToken;
  _searchQuery = q;
  try {
    const params = new URLSearchParams({q, limit: "100", offset: String(_searchOffset)});
    if (_searchFilters.no_epg) params.set("no_epg", "true");
    if (_searchFilters.favorites) params.set("favorites", "true");
    if (_searchFilters.group_id) params.set("group_id", _searchFilters.group_id);
    const d = await api(`/api/channels/search?${params}`);
    if (requestToken !== _searchRequestToken || q !== _searchQuery) return;
    if (append) { searchResults = searchResults.concat(d.results || []); }
    else { searchResults = d.results || []; searchSelected.clear(); }
    _searchTotal = d.total || 0;
    renderSearchResults();
    $("#search-results").hidden = false;
  } catch (e) { console.error(e); }
}

function hideSearch() {
  $("#search-results").hidden = true;
  searchResults = [];
  searchSelected.clear();
  _searchFilters = {};
  _searchQuery = "";
  _searchTotal = 0;
  _searchOffset = 0;
  // Reset filter buttons
  const noEpgBtn = $("#no-epg-btn"); if (noEpgBtn) noEpgBtn.classList.remove("active");
  const favsBtn = $("#favs-filter-btn"); if (favsBtn) favsBtn.classList.remove("active");
}

function renderSearchResults() {
  const moveOpts = groupsData.map(g => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
  $("#search-body").innerHTML = searchResults.map(ch => `
    <tr data-cid="${ch.id}">
      <td><input type="checkbox" class="srch-sel" data-cid="${ch.id}" ${searchSelected.has(ch.id) ? "checked" : ""} /></td>
      <td>${esc(ch.name)}</td>
      <td class="cell-dim">${esc(ch.source_group || "")}</td>
      <td class="cell-dim">${esc(ch.group_name || "")}</td>
      <td class="cell-dim cell-mono">${esc(ch.tvg_id || "")}</td>
      <td><label class="toggle"><input type="checkbox" class="srch-ch-toggle" data-cid="${ch.id}" ${ch.enabled ? "checked" : ""} /><span class="toggle-track"></span></label></td>
      <td><input type="checkbox" class="srch-ch-fav" data-cid="${ch.id}" ${ch.favorite ? "checked" : ""} style="accent-color:var(--warning)" /></td>
      <td><select class="srch-ch-move" data-cid="${ch.id}" style="font-size:12px"><option value="">Move...</option>${moveOpts}</select></td>
      <td><select class="srch-ch-copy" data-cid="${ch.id}" style="font-size:12px"><option value="">Copy...</option>${moveOpts}</select></td>
    </tr>`).join("");
  $("#search-bulk-group").innerHTML = `<option value="">Move/Copy to group...</option>${moveOpts}`;
  // Pagination
  const pagEl = $("#search-pagination");
  if (pagEl) {
    if (_searchTotal > searchResults.length) {
      pagEl.hidden = false;
      $("#search-page-info").textContent = `Showing ${searchResults.length} of ${_searchTotal}`;
    } else {
      pagEl.hidden = searchResults.length === 0;
      if (!pagEl.hidden) $("#search-page-info").textContent = `Showing all ${_searchTotal}`;
    }
    const moreBtn = $("#search-load-more");
    if (moreBtn) moreBtn.hidden = _searchTotal <= searchResults.length;
  }
  wireSearchEvents();
  updateSearchBulk();
}

function wireSearchEvents() {
  $$(".srch-sel").forEach(cb => {
    cb.addEventListener("change", () => {
      cb.checked ? searchSelected.add(cb.dataset.cid) : searchSelected.delete(cb.dataset.cid);
      updateSearchBulk();
    });
  });
  $$(".srch-ch-toggle").forEach(cb => {
    cb.addEventListener("change", async () => {
      try { await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ enabled: cb.checked }) }); }
      catch (e) { toast("error", "Toggle failed", e.message); cb.checked = !cb.checked; }
    });
  });
  $$(".srch-ch-fav").forEach(cb => {
    cb.addEventListener("change", async () => {
      try { await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ favorite: cb.checked }) }); }
      catch (e) { toast("error", "Update failed", e.message); cb.checked = !cb.checked; }
    });
  });
  $$(".srch-ch-move").forEach(sel => {
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      try {
        await api(`/api/channels/${sel.dataset.cid}/move`, { method: "POST", body: JSON.stringify({ to_group_id: sel.value }) });
        toast("success", "Channel moved");
        doSearch($("#editor-search").value.trim());
        refreshOpenGroups();
      } catch (e) { toast("error", "Move failed", e.message); }
      sel.value = "";
    });
  });
  $$(".srch-ch-copy").forEach(sel => {
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      try {
        await api("/api/channels/bulk-copy", { method: "POST", body: JSON.stringify({ channel_ids: [sel.dataset.cid], to_group_id: sel.value }) });
        toast("success", "Channel copied");
        doSearch($("#editor-search").value.trim());
        refreshOpenGroups();
      } catch (e) { toast("error", "Copy failed", e.message); }
      sel.value = "";
    });
  });
}

function updateSearchBulk() {
  const count = searchSelected.size;
  $("#search-bulk-bar").hidden = count === 0;
  $("#search-selected-count").textContent = `${count} selected`;
}

$("#search-select-all").addEventListener("change", (e) => {
  if (e.target.checked) searchResults.forEach(r => searchSelected.add(r.id));
  else searchSelected.clear();
  $$(".srch-sel").forEach(cb => cb.checked = e.target.checked);
  updateSearchBulk();
});
$("#search-sel-hdr").addEventListener("change", (e) => {
  $("#search-select-all").checked = e.target.checked;
  $("#search-select-all").dispatchEvent(new Event("change"));
});

$("#search-bulk-move-btn").addEventListener("click", async () => {
  const to = $("#search-bulk-group").value;
  if (!to || !searchSelected.size) return;
  const tid = toast("loading", "Moving channels");
  try {
    await api("/api/channels/bulk-move", { method: "POST", body: JSON.stringify({ channel_ids: [...searchSelected], to_group_id: to }) });
    updateToast(tid, "success", "Channels moved");
    doSearch($("#editor-search").value.trim());
    refreshOpenGroups();
  } catch (e) { updateToast(tid, "error", "Move failed", e.message); }
});
$("#search-bulk-copy-btn").addEventListener("click", async () => {
  const to = $("#search-bulk-group").value;
  if (!to || !searchSelected.size) return;
  const tid = toast("loading", "Copying channels");
  try {
    await api("/api/channels/bulk-copy", { method: "POST", body: JSON.stringify({ channel_ids: [...searchSelected], to_group_id: to }) });
    updateToast(tid, "success", "Channels copied");
    doSearch($("#editor-search").value.trim());
    refreshOpenGroups();
  } catch (e) { updateToast(tid, "error", "Copy failed", e.message); }
});
$("#search-bulk-enable-btn").addEventListener("click", async () => {
  if (!searchSelected.size) return;
  try {
    await api("/api/channels/bulk-toggle", { method: "POST", body: JSON.stringify({ channel_ids: [...searchSelected], enabled: true }) });
    toast("success", "Channels enabled");
    doSearch($("#editor-search").value.trim());
  } catch (e) { toast("error", "Enable failed", e.message); }
});
$("#search-bulk-disable-btn").addEventListener("click", async () => {
  if (!searchSelected.size) return;
  try {
    await api("/api/channels/bulk-toggle", { method: "POST", body: JSON.stringify({ channel_ids: [...searchSelected], enabled: false }) });
    toast("success", "Channels disabled");
    doSearch($("#editor-search").value.trim());
  } catch (e) { toast("error", "Disable failed", e.message); }
});
$("#search-bulk-fav-btn").addEventListener("click", async () => {
  if (!searchSelected.size) return;
  try {
    await api("/api/channels/bulk-favorite", { method: "POST", body: JSON.stringify({ channel_ids: [...searchSelected], favorite: true }) });
    toast("success", "Favorites updated");
    doSearch($("#editor-search").value.trim());
  } catch (e) { toast("error", "Update failed", e.message); }
});

// Search pagination - Load More
{
  const _searchLoadMoreEl = $("#search-load-more");
  if (_searchLoadMoreEl) {
    _searchLoadMoreEl.addEventListener("click", () => {
      _searchOffset = searchResults.length;
      doSearch(_searchQuery, true);
    });
  }
}

// Filter: No EPG
$("#no-epg-btn").addEventListener("click", () => {
  const btn = $("#no-epg-btn");
  const isActive = btn.classList.toggle("active");
  _searchFilters.no_epg = isActive;
  if (isActive) {
    doSearch(_searchQuery || "");
  } else if (!_searchQuery && !Object.values(_searchFilters).some(Boolean)) {
    hideSearch();
  } else {
    doSearch(_searchQuery);
  }
});

// Filter: Favorites only
$("#favs-filter-btn").addEventListener("click", () => {
  const btn = $("#favs-filter-btn");
  const isActive = btn.classList.toggle("active");
  _searchFilters.favorites = isActive;
  if (isActive) {
    doSearch(_searchQuery || "");
  } else if (!_searchQuery && !Object.values(_searchFilters).some(Boolean)) {
    hideSearch();
  } else {
    doSearch(_searchQuery);
  }
});

// Bulk Rename Panel
$("#bulk-rename-btn").addEventListener("click", () => {
  const panel = $("#bulk-rename-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) { panel.scrollIntoView({ behavior: "smooth", block: "start" }); $("#rename-find").focus(); }
});
$("#bulk-rename-close").addEventListener("click", () => { $("#bulk-rename-panel").hidden = true; });

$("#rename-preview-btn").addEventListener("click", async () => {
  const find = $("#rename-find").value;
  const replace = $("#rename-replace").value;
  const isRegex = $("#rename-regex").checked;
  if (!find) return;
  try {
    const d = await api("/api/channels/bulk-rename/preview", {
      method: "POST",
      body: JSON.stringify({ pattern: find, replacement: replace, is_regex: isRegex })
    });
    let html = `<b>${d.count}</b> channel(s) would be renamed`;
    if (d.examples?.length) {
      html += `<div style="margin-top:8px">` + d.examples.map(ex =>
        `<div style="font-size:11px;margin:2px 0"><span style="text-decoration:line-through;color:var(--danger)">${esc(ex.old)}</span> → <span style="color:var(--success)">${esc(ex.new)}</span></div>`
      ).join("") + `</div>`;
    }
    $("#rename-preview-results").innerHTML = html;
  } catch (e) { toast("error", "Preview failed", e.message); }
});

$("#rename-apply-btn").addEventListener("click", async () => {
  const find = $("#rename-find").value;
  const replace = $("#rename-replace").value;
  const isRegex = $("#rename-regex").checked;
  if (!find) return;
  const ok = await confirmDialog("Bulk Rename", `This will rename all channels matching "${find}". This cannot be easily undone.`, "Rename");
  if (!ok) return;
  const tid = toast("loading", "Renaming channels...");
  try {
    const d = await api("/api/channels/bulk-rename", {
      method: "POST",
      body: JSON.stringify({ pattern: find, replacement: replace, is_regex: isRegex })
    });
    updateToast(tid, "success", `${d.modified} channel(s) renamed`);
    refreshOpenGroups();
    startExportPoll();
    if (_searchQuery) doSearch(_searchQuery);
  } catch (e) { updateToast(tid, "error", "Rename failed", e.message); }
});

// Duplicates Panel
$("#duplicates-btn").addEventListener("click", async () => {
  const panel = $("#duplicates-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) { panel.scrollIntoView({ behavior: "smooth", block: "start" }); await loadDuplicates(); }
});
$("#duplicates-close").addEventListener("click", () => { $("#duplicates-panel").hidden = true; });

async function loadDuplicates() {
  const body = $("#duplicates-body");
  body.innerHTML = '<span class="text-muted">Scanning for duplicates...</span>';
  try {
    const d = await api("/api/channels/duplicates");
    if (!d.duplicates?.length) {
      body.innerHTML = '<div class="card-empty"><div class="card-empty-icon">✓</div><div class="card-empty-text">No duplicate channels found!</div></div>';
      return;
    }
    body.innerHTML = `<p class="text-muted" style="margin-bottom:12px"><b>${d.total}</b> URLs shared by multiple channels. Click delete to remove a duplicate.</p>` +
      d.duplicates.map(dup => `
        <div style="margin-bottom:12px;padding:10px;border:1px solid var(--border);border-radius:var(--radius)">
          <div class="text-muted" style="font-size:11px;margin-bottom:6px;word-break:break-all">${esc(dup.url)} (${dup.cnt} copies)</div>
          ${dup.channels.map(ch => `
            <div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:13px">
              <span style="flex:1">${esc(ch.name)} <span class="cell-dim">(${esc(ch.group)})</span></span>
              <button class="btn btn-sm btn-danger" style="padding:1px 6px;font-size:11px" onclick="deleteDuplicateChannel('${ch.id}')">Delete</button>
            </div>`).join("")}
        </div>`).join("");
  } catch (e) { body.innerHTML = `<div class="text-muted">Failed: ${esc(e.message)}</div>`; }
}

window.deleteDuplicateChannel = async (cid) => {
  try {
    await api(`/api/channels/${cid}`, { method: "DELETE" });
    toast("success", "Channel deleted");
    loadDuplicates();
    refreshOpenGroups();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

// Export status polling — start after any mutation
let _exportPollTimer;
function startExportPoll() {
  if (_exportPollTimer) return;
  _exportPollTimer = setInterval(async () => {
    try {
      const s = await api("/api/export-status");
      if (s.status === "ok" && s.time) {
        toast("success", `Export complete — ${s.channels} channels`);
        clearInterval(_exportPollTimer);
        _exportPollTimer = null;
      } else if (s.status === "error") {
        toast("error", "Export failed", s.error || "Unknown error");
        clearInterval(_exportPollTimer);
        _exportPollTimer = null;
      }
    } catch { /* ignore */ }
  }, 3000);
  setTimeout(() => { if (_exportPollTimer) { clearInterval(_exportPollTimer); _exportPollTimer = null; } }, 60000);
}

/* ═══════════════════════════════════════════════════════════════════════
   FAVORITES
   ═══════════════════════════════════════════════════════════════════════ */
async function loadFavorites() {
  try {
    const favs = await api("/api/favorites");
    if (!favs.length) {
      $("#favorites-body").innerHTML = `<tr><td colspan="5"><div class="card-empty"><div class="card-empty-icon">⭐</div><div class="card-empty-text">No favorites yet. Star channels in the Editor.</div></div></td></tr>`;
      return;
    }
    $("#favorites-body").innerHTML = favs.map(ch => `
      <tr>
        <td>${esc(ch.name)}</td>
        <td class="cell-dim">${esc(ch.group_name || "")}</td>
        <td class="cell-dim cell-mono">${esc(ch.tvg_id || "")}</td>
        <td><label class="toggle"><input type="checkbox" class="fav-toggle" data-cid="${ch.id}" ${ch.enabled ? "checked" : ""} /><span class="toggle-track"></span></label></td>
        <td><input type="checkbox" class="fav-star" data-cid="${ch.id}" checked style="accent-color:var(--warning)" /></td>
      </tr>`).join("");

    $$(".fav-toggle").forEach(cb => {
      cb.addEventListener("change", async () => {
        try { await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ enabled: cb.checked }) }); }
        catch (e) { toast("error", "Toggle failed", e.message); cb.checked = !cb.checked; }
      });
    });
    $$(".fav-star").forEach(cb => {
      cb.addEventListener("change", async () => {
        try {
          await api(`/api/channels/${cb.dataset.cid}`, { method: "PATCH", body: JSON.stringify({ favorite: cb.checked }) });
          if (!cb.checked) { toast("info", "Removed from favorites"); loadFavorites(); }
        } catch (e) { toast("error", "Update failed", e.message); cb.checked = !cb.checked; }
      });
    });
  } catch (e) {
    $("#favorites-body").innerHTML = `<tr><td colspan="5" class="text-muted" style="padding:16px">Error: ${esc(e.message)}</td></tr>`;
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   EPG
   ═══════════════════════════════════════════════════════════════════════ */
async function loadEPG() {
  try {
    const active = await api("/api/sources").then(s => s.find(x => x.is_active));
    if (active) {
      $("#epg-url").value = "";
      $("#epg-url").dataset.configured = active.epg_url_configured ? "1" : "0";
      $("#epg-url").placeholder = active.epg_url_configured ? "Configured — enter a new URL only to replace it" : "EPG URL";
    }
    const [epgs, status] = await Promise.all([
      api("/api/epg-sources"),
      api("/api/epg-sources/status").catch(() => null),
    ]);
    const statusMap = status?.sources || {};
    if (!epgs.length) {
      $("#epg-list").innerHTML = `<div class="card-empty" style="padding:16px"><div class="card-empty-text">No additional EPG sources configured.</div></div>`;
    } else {
      $("#epg-list").innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>Name</th><th>URL</th><th>Status</th><th style="width:80px"></th></tr></thead><tbody>
        ${epgs.map(e => {
          const s = statusMap[e.url];
          let badge = '<span style="font-size:11px;padding:2px 8px;border-radius:8px;background:rgba(148,163,184,.15);color:var(--text-dim)">Pending</span>';
          if (s) {
            if (s.status === 'ok') {
              const ago = _fmtAgo(s.ago_seconds);
              badge = `<span style="font-size:11px;padding:2px 8px;border-radius:8px;background:rgba(34,197,94,.15);color:#4ade80" title="Fetched ${ago}">${s.channels.toLocaleString()} ch &middot; ${ago}</span>`;
            } else if (s.status === 'error') {
              badge = `<span style="font-size:11px;padding:2px 8px;border-radius:8px;background:rgba(239,68,68,.15);color:#f87171" title="${esc(s.error || 'Error')}">Error</span>`;
            }
          }
          return `<tr><td>${esc(e.name)}</td><td class="cell-dim cell-mono" style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.url)}</td>
          <td>${badge}</td>
          <td><button class="btn btn-sm btn-danger" onclick="deleteEpgSource(${e.id})">Delete</button></td></tr>`;
        }).join("")}
      </tbody></table></div>`;
    }
    // Also show active source EPG status
    if (active?.epg_url_configured) {
      const as = null;
      let label = '';
      if (as?.status === 'ok') label = `<span class="text-muted" style="font-size:11px;margin-left:8px">${as.channels.toLocaleString()} channels · cached ${_fmtAgo(as.ago_seconds)}</span>`;
      else if (as?.status === 'error') label = `<span style="font-size:11px;color:#f87171;margin-left:8px">Fetch error</span>`;
      const existing = document.getElementById('active-epg-status');
      if (existing) existing.remove();
      const span = document.createElement('span');
      span.id = 'active-epg-status';
      span.innerHTML = label;
      $("#epg-form").appendChild(span);
    }
    loadSuggestedEpg();
  } catch (e) { console.error(e); }
}

function _fmtAgo(seconds) {
  if (seconds == null) return 'never';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

$("#epg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const value = $("#epg-url").value.trim();
    if (!value && $("#epg-url").dataset.configured === "1") {
      toast("info", "EPG URL unchanged");
      return;
    }
    await api("/api/epg", { method: "PATCH", body: JSON.stringify({ epg_url: value }) });
    toast("success", "EPG URL saved");
  } catch (err) { toast("error", "Save failed", err.message); }
});

$("#add-epg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/epg-sources", { method: "POST", body: JSON.stringify({ name: $("#new-epg-name").value, url: $("#new-epg-url").value }) });
    toast("success", "EPG source added");
    $("#new-epg-name").value = "";
    $("#new-epg-url").value = "";
    loadEPG();
  } catch (err) { toast("error", "Failed to add EPG source", err.message); }
});

window.deleteEpgSource = async (id) => {
  try {
    await api(`/api/epg-sources/${id}`, { method: "DELETE" });
    toast("success", "EPG source removed");
    loadEPG();
  } catch (e) { toast("error", "Delete failed", e.message); }
};

/* ── Suggested EPG Sources ──────────────────────────────────────────── */
async function loadSuggestedEpg() {
  try {
    const suggested = await api("/api/epg-sources/suggested");
    const card = $("#suggested-epg-card");
    if (!suggested.length) { card.hidden = true; return; }
    card.hidden = false;
    $("#suggested-epg-list").innerHTML = suggested.map(s => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1">
          <div style="font-weight:500">${esc(s.name)}</div>
          <div class="text-muted" style="font-size:12px">${esc(s.description)}</div>
        </div>
        <button class="btn btn-primary btn-sm add-suggested-epg" data-name="${esc(s.name)}" data-url="${esc(s.url)}">Add</button>
      </div>`).join("");
    $$(".add-suggested-epg").forEach(btn => {
      btn.addEventListener("click", async () => {
        btnLoad(btn, "Adding...");
        try {
          await api("/api/epg-sources", { method: "POST", body: JSON.stringify({ name: btn.dataset.name, url: btn.dataset.url }) });
          toast("success", "EPG source added", btn.dataset.name);
          loadEPG();
        } catch (e) { toast("error", "Failed", e.message); }
        finally { btnDone(btn); }
      });
    });
  } catch (_) {}
}

/* ── EPG Explore / Search ───────────────────────────────────────────── */
let _epgExploreTimer = null;

$("#epg-explore-input").addEventListener("input", (e) => {
  clearTimeout(_epgExploreTimer);
  _epgExploreTimer = setTimeout(() => doEpgExplore(e.target.value.trim()), 300);
});

$("#epg-explore-refresh").addEventListener("click", async () => {
  const btn = $("#epg-explore-refresh");
  btnLoad(btn, "Refreshing...");
  const tid = toast("loading", "Refreshing EPG cache", "Downloading and parsing all EPG sources...");
  try {
    const res = await api("/api/epg-channels/refresh", { method: "POST" });
    updateToast(tid, "success", "EPG cache refreshed", `${res.total.toLocaleString()} channels indexed`);
    $("#epg-explore-input").value = "";
    $("#epg-explore-results").innerHTML = '';
    $("#epg-explore-status").textContent = `${res.total.toLocaleString()} total channels cached`;
    loadEPG();
  } catch (e) { updateToast(tid, "error", "Refresh failed", e.message); }
  finally { btnDone(btn); }
});

async function doEpgExplore(q) {
  const results = $("#epg-explore-results");
  const status = $("#epg-explore-status");
  if (!q) {
    results.innerHTML = '';
    status.textContent = 'Type to search across all configured EPG sources...';
    return;
  }
  try {
    const data = await api(`/api/epg-channels?q=${encodeURIComponent(q)}&limit=100`);
    if (!data.channels.length) {
      results.innerHTML = '<div class="text-muted" style="padding:12px">No EPG channels found matching your search.</div>';
      status.textContent = `0 of ${data.total.toLocaleString()} total EPG channels`;
      return;
    }
    status.textContent = `Showing ${data.channels.length} of ${data.total.toLocaleString()} total EPG channels`;
    results.innerHTML = `<div class="table-wrap" style="max-height:400px;overflow-y:auto"><table class="data-table" style="font-size:13px"><thead><tr>
      <th>Name</th><th>EPG ID</th><th>Source</th>
    </tr></thead><tbody>
    ${data.channels.map(ch => `<tr>
      <td style="display:flex;align-items:center;gap:6px">
        ${ch.icon ? `<img src="${esc(ch.icon)}" style="width:20px;height:20px;border-radius:3px;object-fit:contain" onerror="this.style.display='none'" />` : ''}
        ${esc(ch.name)}
      </td>
      <td class="cell-mono cell-dim">${esc(ch.id)}</td>
      <td class="cell-dim" style="font-size:11px">${esc(ch.source || '')}</td>
    </tr>`).join('')}
    </tbody></table></div>`;
  } catch (e) {
    results.innerHTML = `<div class="text-muted" style="padding:12px">Error: ${esc(e.message)}</div>`;
  }
}

/* ── Auto-Match EPG ─────────────────────────────────────────────────── */
let _autoMatchResults = [];

$("#auto-match-threshold").addEventListener("input", (e) => {
  $("#auto-match-threshold-val").textContent = e.target.value + "%";
});

$("#auto-match-btn").addEventListener("click", async () => {
  const btn = $("#auto-match-btn");
  const threshold = parseInt($("#auto-match-threshold").value) / 100;
  btnLoad(btn, "Scanning...");
  const tid = toast("loading", "Scanning for EPG matches", "Comparing unmatched channels against all EPG sources...");
  try {
    const data = await api(`/api/epg-channels/auto-match?min_score=${threshold}`);
    _autoMatchResults = data.matches || [];
    updateToast(tid, "success", `Found ${_autoMatchResults.length} matches`, `${data.unmatched} channels were unmatched`);
    renderAutoMatchResults();
  } catch (e) {
    updateToast(tid, "error", "Auto-match failed", e.message);
    $("#auto-match-results").innerHTML = "";
  } finally { btnDone(btn); }
});

function renderAutoMatchResults() {
  const el = $("#auto-match-results");
  if (!_autoMatchResults.length) {
    el.innerHTML = '<div class="text-muted" style="padding:8px 0">No matches found. Try lowering the minimum score.</div>';
    return;
  }
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <label style="font-size:12px"><input type="checkbox" id="auto-match-sel-all" checked /> Select All</label>
      <div style="flex:1"></div>
      <button id="auto-match-apply-btn" class="btn btn-primary btn-sm">Apply Selected</button>
    </div>
    <div class="table-wrap" style="max-height:400px;overflow-y:auto"><table class="data-table" style="font-size:12px"><thead><tr>
      <th style="width:28px"></th><th>Channel</th><th>→ EPG Match</th><th>EPG ID</th><th>Source</th><th style="width:50px">Score</th>
    </tr></thead><tbody>
    ${_autoMatchResults.map((m, i) => {
      const pct = Math.round(m.score * 100);
      const color = pct >= 70 ? "var(--success)" : pct >= 50 ? "var(--warning)" : "var(--text-dim)";
      return `<tr>
        <td><input type="checkbox" class="am-sel" data-idx="${i}" checked /></td>
        <td>${esc(m.channel_name)}</td>
        <td>${esc(m.epg_name)}</td>
        <td class="cell-mono cell-dim">${esc(m.epg_id)}</td>
        <td class="cell-dim" style="font-size:11px">${esc(m.epg_source || "")}</td>
        <td style="color:${color};font-weight:600;text-align:center">${pct}%</td>
      </tr>`;}).join("")}
    </tbody></table></div>`;

  $("#auto-match-sel-all").addEventListener("change", (e) => {
    $$(".am-sel").forEach(cb => cb.checked = e.target.checked);
  });

  $("#auto-match-apply-btn").addEventListener("click", applyAutoMatch);
}

async function applyAutoMatch() {
  const selected = $$(".am-sel:checked").map(cb => {
    const m = _autoMatchResults[parseInt(cb.dataset.idx)];
    return { channel_id: m.channel_id, epg_id: m.epg_id };
  });
  if (!selected.length) { toast("info", "No matches selected"); return; }
  const btn = $("#auto-match-apply-btn");
  btnLoad(btn, "Applying...");
  try {
    const res = await api("/api/epg-channels/auto-match", {
      method: "POST", body: JSON.stringify({ matches: selected })
    });
    toast("success", `Applied ${res.applied} EPG matches`);
    _autoMatchResults = [];
    $("#auto-match-results").innerHTML = '<div class="text-muted" style="padding:8px 0">Matches applied. Re-export to update EPG data.</div>';
  } catch (e) { toast("error", "Apply failed", e.message); }
  finally { btnDone(btn); }
}

/* ── EPG Search Modal ───────────────────────────────────────────────── */
let _epgSearchTarget = null; // channel ID we're assigning to
let _epgSearchTimer = null;

function openEpgSearch(cid) {
  _epgSearchTarget = cid;
  const modal = $("#epg-search-modal");
  modal.hidden = false;
  const input = $("#epg-search-input");
  input.value = "";
  $("#epg-search-results").innerHTML = '<div class="text-muted" style="padding:12px">Type to search EPG channels from your configured sources...</div>';
  $("#epg-search-status").textContent = "";
  setTimeout(() => input.focus(), 50);
}

function closeEpgSearch() {
  $("#epg-search-modal").hidden = true;
  _epgSearchTarget = null;
}

$("#epg-search-close").addEventListener("click", closeEpgSearch);
$("#epg-search-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeEpgSearch();
});

$("#epg-search-input").addEventListener("input", (e) => {
  clearTimeout(_epgSearchTimer);
  _epgSearchTimer = setTimeout(() => doEpgSearch(e.target.value.trim()), 300);
});

async function doEpgSearch(q) {
  const results = $("#epg-search-results");
  if (!q) {
    results.innerHTML = '<div class="text-muted" style="padding:12px">Type to search EPG channels from your configured sources...</div>';
    $("#epg-search-status").textContent = "";
    return;
  }
  try {
    const data = await api(`/api/epg-channels?q=${encodeURIComponent(q)}&limit=50`);
    if (!data.channels.length) {
      results.innerHTML = '<div class="text-muted" style="padding:12px">No EPG channels found matching your search.</div>';
      $("#epg-search-status").textContent = `0 of ${data.total} total EPG channels`;
      return;
    }
    $("#epg-search-status").textContent = `Showing ${data.channels.length} of ${data.total} total EPG channels`;
    results.innerHTML = `<table class="data-table" style="font-size:13px"><thead><tr><th>Name</th><th>EPG ID</th><th>Source</th><th style="width:60px"></th></tr></thead><tbody>
      ${data.channels.map(ch => `<tr>
        <td style="display:flex;align-items:center;gap:6px">
          ${ch.icon ? `<img src="${esc(ch.icon)}" style="width:20px;height:20px;border-radius:3px;object-fit:contain" onerror="this.style.display='none'" />` : ""}
          ${esc(ch.name)}
        </td>
        <td class="cell-mono cell-dim">${esc(ch.id)}</td>
        <td class="cell-dim" style="font-size:11px">${esc(ch.source || "")}</td>
        <td><button class="btn btn-sm btn-primary epg-pick-btn" data-epg-id="${esc(ch.id)}">Use</button></td>
      </tr>`).join("")}
    </tbody></table>`;
    $$(".epg-pick-btn", results).forEach(btn => {
      btn.addEventListener("click", () => pickEpgChannel(btn.dataset.epgId));
    });
  } catch (e) {
    results.innerHTML = `<div class="text-muted" style="padding:12px">Error: ${esc(e.message)}</div>`;
  }
}

async function pickEpgChannel(epgId) {
  if (!_epgSearchTarget) return;
  try {
    await api(`/api/channels/${_epgSearchTarget}`, { method: "PATCH", body: JSON.stringify({ tvg_id: epgId }) });
    toast("success", "EPG assigned", epgId);
    // Update the TVG-ID input in the editor
    const inp = document.querySelector(`.ch-tvgid[data-cid="${_epgSearchTarget}"]`);
    if (inp) inp.value = epgId;
    // Update the EPG action dropdown state
    const sel = document.querySelector(`.ch-epg-action[data-cid="${_epgSearchTarget}"]`);
    if (sel) { sel.options[0].textContent = "Match"; sel.style.color = ""; sel.selectedIndex = 0; }
    closeEpgSearch();
  } catch (e) { toast("error", "Failed to assign EPG", e.message); }
}

// Wire up search buttons (delegated)
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".epg-search-btn");
  if (btn) openEpgSearch(btn.dataset.cid);
});

/* ═══════════════════════════════════════════════════════════════════════
   EXPORT
   ═══════════════════════════════════════════════════════════════════════ */
async function loadExportPage() {
  const base = window.location.origin;
  let urls = { m3u: `${base}/m3u`, epg: `${base}/epg` };
  try { urls = await api("/api/feed-urls"); } catch (_) {}
  $("#m3u-direct-url").textContent = urls.m3u;
  $("#epg-direct-url").textContent = urls.epg;

  const dlEl = $("#export-download-links");
  if (dlEl) {
    const iconView = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>`;
    const iconDl = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;
    dlEl.innerHTML = `
      <a href="${esc(urls.m3u)}${urls.m3u.includes("?") ? "&" : "?"}view=1" class="btn" target="_blank" rel="noopener">${iconView} View M3U</a>
      <a href="${esc(urls.m3u)}${urls.m3u.includes("?") ? "&" : "?"}download=1" class="btn btn-primary">${iconDl} Download M3U</a>
      <a href="${esc(urls.epg)}${urls.epg.includes("?") ? "&" : "?"}view=1" class="btn" target="_blank" rel="noopener">${iconView} View EPG</a>
      <a href="${esc(urls.epg)}${urls.epg.includes("?") ? "&" : "?"}download=1" class="btn">${iconDl} Download EPG</a>`;
  }

  loadExportHistory();
}

async function loadExportHistory() {
  const el = $("#export-history-body");
  if (!el) return;
  try {
    const history = await api("/api/export/history?limit=20");
    if (!history.length) {
      el.innerHTML = `<p class="text-muted">No export history yet. Exports are recorded automatically.</p>`;
      return;
    }
    const fmtSize = (n) => {
      if (!n) return "—";
      if (n < 1024) return `${n} B`;
      if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
      return `${(n / 1048576).toFixed(1)} MB`;
    };
    el.innerHTML = `<div class="table-wrap"><table class="data-table" style="font-size:12px">
      <thead><tr><th>Time</th><th>Channels</th><th>Groups</th><th>M3U Size</th><th>EPG Size</th></tr></thead>
      <tbody>${history.map(h => `<tr>
        <td class="cell-dim">${new Date(h.exported_at).toLocaleString()}</td>
        <td><b>${(h.channel_count||0).toLocaleString()}</b></td>
        <td>${(h.group_count||0).toLocaleString()}</td>
        <td class="cell-mono">${fmtSize(h.m3u_size)}</td>
        <td class="cell-mono">${fmtSize(h.xml_size)}</td>
      </tr>`).join("")}</tbody>
    </table></div>`;
  } catch (e) {
    el.innerHTML = `<p class="text-muted">Failed to load history.</p>`;
  }
}

document.getElementById("export-history-refresh")?.addEventListener("click", loadExportHistory);

// Copy buttons (delegated)
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const target = document.getElementById(btn.dataset.target);
  if (!target) return;
  navigator.clipboard.writeText(target.textContent).then(() => {
    const orig = btn.innerHTML;
    btn.textContent = "Copied!";
    btn.style.color = "var(--success)";
    setTimeout(() => { btn.innerHTML = orig; btn.style.color = ""; }, 1500);
  });
});

/* ═══════════════════════════════════════════════════════════════════════
   TEAMARR
   ═══════════════════════════════════════════════════════════════════════ */
async function loadTeamarr() {
  try {
    const s = await api("/api/settings");
    $("#ta-enabled").value = s.teamarr_enabled || "0";
    $("#ta-base-url").value = s.teamarr_base_url || "";
    $("#ta-username").value = s.teamarr_username || "";
    $("#ta-password").value = "";
    $("#ta-password").placeholder = s.teamarr_password_configured ? "Saved — enter only to replace" : "Password";
    $("#ta-output").value = s.teamarr_output || "ts";
    updateTeamarrInfo(s);
  } catch (e) { console.error(e); }
}

function updateTeamarrInfo(s) {
  const enabled = s.teamarr_enabled === "1";
  const info = $("#teamarr-info");
  if (!enabled) { info.hidden = true; return; }
  info.hidden = false;
  const base = (s.teamarr_base_url || window.location.origin).replace(/\/$/, "");
  const user = s.teamarr_username || "teamarr";
  const pass = s.teamarr_password_configured ? "Saved (hidden)" : "Not configured";
  $("#ta-server-url").textContent = base;
  $("#ta-show-user").textContent = user;
  $("#ta-show-pass").textContent = pass;
  $("#ta-epg-url").textContent = `${base}/xmltv.php?username=${encodeURIComponent(user)}&password=YOUR_PASSWORD`;
}

$("#teamarr-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tid = toast("loading", "Saving Teamarr settings");
  try {
    const s = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({
        teamarr_enabled: $("#ta-enabled").value,
        teamarr_base_url: $("#ta-base-url").value,
        teamarr_username: $("#ta-username").value,
        teamarr_password: $("#ta-password").value,
        teamarr_output: $("#ta-output").value,
      }),
    });
    updateTeamarrInfo(s);
    updateToast(tid, "success", "Teamarr settings saved");
  } catch (err) { updateToast(tid, "error", "Save failed", err.message); }
});

/* ═══════════════════════════════════════════════════════════════════════
   TV GUIDE — Virtual-scrolled EPG grid with group sidebar
   ═══════════════════════════════════════════════════════════════════════ */
var guideData = null;
var guideLoadedAt = 0;
// Guide data is loaded one group at a time. This preserves the full three-day
// timeline without downloading programmes for thousands of invisible rows.
const GUIDE_MAX_PROGRAMMES = 300;
// How far forward the guide grid extends from "now" (fixed 3-day span).
const GUIDE_FORWARD_HOURS = 72;
// Re-pull guide data if the cached copy is older than this so the guide
// always reflects the current time ("absolute up to date").
const GUIDE_FRESH_MS = 5 * 60 * 1000;
var guideFilter = "";
var guideGroupFilter = "";
var guideOnlyActive = false;
var _guideChannels = [];  // filtered list, cached
var _guideTimeline = null; // cached timeline params
var _guideGroupOffsets = []; // [{id, name, startIdx}] for scroll-to-group

const GUIDE_ROW_H = 56;  // px per channel row
const GUIDE_OVERSCAN = 10; // extra rows above/below viewport
// If the EPG export is older than this when the guide opens, re-pull fresh
// EPG from the providers in the background ("try its best to serve latest").
const GUIDE_STALE_MS = 30 * 60 * 1000;
var _guideFreshening = false;

// Human-friendly age, e.g. "just now", "14 min ago", "3 h ago", "2 d ago".
function relAge(iso) {
  if (!iso) return "unknown";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "just now";
  const min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} h ago`;
  return `${Math.floor(hr / 24)} d ago`;
}

async function fetchGuideData(extra = "") {
  let groups = guideData?.groups || [];
  if (!groups.length) {
    const meta = await api("/api/guide?groups_only=true");
    groups = meta.groups || [];
    const saved = localStorage.getItem("m3u.guide.group") || "";
    if (!guideGroupFilter || !groups.some(g => g.id === guideGroupFilter)) {
      guideGroupFilter = groups.some(g => g.id === saved)
        ? saved
        : (groups.find(g => (g.count || 0) > 0)?.id || "");
    }
    guideData = { ...meta, groups };
  }
  const groupArg = guideGroupFilter ? `&group_id=${encodeURIComponent(guideGroupFilter)}` : "";
  const data = await api(`/api/guide?max_programmes=${GUIDE_MAX_PROGRAMMES}${groupArg}${extra}`);
  guideData = { ...data, groups };
  guideLoadedAt = Date.now();
  return guideData;
}

// If the EPG data is stale, pull a fresh export from the providers in the
// background and re-render when it lands. Safe to call repeatedly — it
// no-ops while a refresh is already running or the data is recent.
async function maybeAutoFreshenGuide() {
  if (_guideFreshening) return;
  const synced = guideData && guideData.epg_synced;
  const ageMs = synced ? (Date.now() - new Date(synced).getTime()) : Infinity;
  if (ageMs <= GUIDE_STALE_MS) return;
  _guideFreshening = true;
  renderGuide();  // repaint the bar so it shows "updating…"
  try {
    await fetchGuideData("&rebuild=true");  // server re-fetches EPG, then rebuilds
    _renderGuideFromData();
  } catch (_) { /* keep showing the stale data */ }
  finally { _guideFreshening = false; }
}

function _renderGuideFromData() {
  populateGuideSidebar();
  _guideChannels = getVisibleChannels();
  _buildGroupOffsets();
  renderGuide();
}

async function loadGuide() {
  const container = $("#guide-container");
  const fresh = guideData && (Date.now() - guideLoadedAt) < GUIDE_FRESH_MS;
  if (fresh) {
    _renderGuideFromData();
    startGuideAutoRefresh();
    maybeAutoFreshenGuide();
    return;
  }
  if (!guideData) {
    container.innerHTML = `<div class="guide-loading"><div class="skeleton" style="height:400px;width:100%"></div></div>`;
  }
  try {
    await fetchGuideData();
    _renderGuideFromData();
    startGuideAutoRefresh();
    maybeAutoFreshenGuide();
  } catch (e) {
    if (!guideData) {
      container.innerHTML = `<div class="card-empty"><div class="card-empty-text">Failed to load guide: ${esc(e.message)}</div></div>`;
    }
  }
}

// Keep the guide tracking real time while the page stays open.
var _guideAutoRefreshTimer = null;
function startGuideAutoRefresh() {
  if (_guideAutoRefreshTimer) return;
  _guideAutoRefreshTimer = setInterval(async () => {
    if (currentPage !== "guide") return;
    try {
      await fetchGuideData();
      _renderGuideFromData();
    } catch (_) {}
  }, GUIDE_FRESH_MS);
}

function populateGuideSidebar() {
  const list = $("#guide-group-list");
  // Use groups from API (preserves editor sort order)
  const groups = guideData?.groups || [];
  const picker = $("#guide-group-select");
  if (picker) {
    picker.innerHTML = groups
      .filter(g => (g.count ?? 1) > 0)
      .map(g => `<option value="${esc(g.id)}">${esc(g.name)} (${g.count ?? "—"})</option>`)
      .join("");
    picker.value = guideGroupFilter;
    picker.onchange = () => selectGuideGroup(picker.value);
  }
  let html = "";
  for (const g of groups) {
    const count = g.count ?? 0;
    if (count === 0) continue;
    html += `<button class="guide-group-btn${guideGroupFilter === g.id ? ' active' : ''}" data-gid="${esc(g.id)}">
      <span class="guide-group-btn-name">${esc(g.name)}</span>
      <span class="guide-group-btn-count">${count}</span>
    </button>`;
  }
  list.innerHTML = html;

  // Click handlers
  $$(".guide-group-btn", list).forEach(btn => {
    btn.addEventListener("click", () => selectGuideGroup(btn.dataset.gid));
  });
}

async function selectGuideGroup(groupId) {
  if (!groupId || guideGroupFilter === groupId) return;
  guideGroupFilter = groupId;
  localStorage.setItem("m3u.guide.group", guideGroupFilter);
  $$(".guide-group-btn", $("#guide-group-list")).forEach(b =>
    b.classList.toggle("active", b.dataset.gid === guideGroupFilter));
  const picker = $("#guide-group-select");
  if (picker) picker.value = guideGroupFilter;
  const container = $("#guide-container");
  container.classList.add("is-loading");
  try {
    await fetchGuideData();
    _renderGuideFromData();
  } catch (e) {
    toast("error", "Guide group failed", e.message);
  } finally {
    container.classList.remove("is-loading");
  }
}

function _buildGroupOffsets() {
  _guideGroupOffsets = [];
  if (!_guideChannels.length) return;
  let lastGroup = null;
  _guideChannels.forEach((ch, i) => {
    if (ch.group !== lastGroup) {
      _guideGroupOffsets.push({ id: ch.group_id, name: ch.group, startIdx: i });
      lastGroup = ch.group;
    }
  });
}

function getVisibleChannels() {
  if (!guideData) return [];
  let chs = guideData.channels;
  if (guideFilter) {
    const q = guideFilter.toLowerCase();
    chs = chs.filter(c => c.name.toLowerCase().includes(q) || c.group.toLowerCase().includes(q));
  }
  if (guideGroupFilter) {
    chs = chs.filter(c => c.group_id === guideGroupFilter);
  }
  if (guideOnlyActive) {
    const now = new Date(guideData?.now || Date.now());
    chs = chs.filter(c => (c.programmes || []).some(p => new Date(p.start) <= now && new Date(p.stop) > now));
  }
  return chs;
}

function _calcTimeline(now) {
  const timeStart = new Date(now.getTime() - 3 * 3600000);
  timeStart.setMinutes(Math.floor(timeStart.getMinutes() / 30) * 30, 0, 0);
  const timeEnd = new Date(now.getTime() + GUIDE_FORWARD_HOURS * 3600000);
  const totalMinutes = (timeEnd - timeStart) / 60000;
  const pxPerMin = 4;
  return { timeStart, timeEnd, totalMinutes, pxPerMin, totalWidth: totalMinutes * pxPerMin };
}

function _renderRow(ch, tl, now, isGroupStart) {
  let progsHtml = '';
  for (const p of (ch.programmes || [])) {
    const pStart = new Date(p.start);
    const pStop = new Date(p.stop);
    const clampStart = Math.max(0, (pStart - tl.timeStart) / 60000);
    const clampEnd = Math.min(tl.totalMinutes, (pStop - tl.timeStart) / 60000);
    if (clampEnd <= clampStart) continue;
    const left = clampStart * tl.pxPerMin;
    const width = (clampEnd - clampStart) * tl.pxPerMin;
    const isNow = pStart <= now && pStop > now;
    const pct = isNow ? Math.round(((now - pStart) / (pStop - pStart)) * 100) : 0;
    const desc = (p.desc || '').toLowerCase();
    const typeCls = desc.includes('no guide data') ? ' dummy' : (desc.includes('schedule inferred') || desc.includes('guide details unavailable') ? ' synthetic' : ' real');
    // Build display title: "Title: Sub-title. SxxExx" (matches TiviMate's format)
    const _ptParts = [p.title || ''];
    if (p.sub_title) _ptParts.push(': ' + p.sub_title);
    if (p.episode_num) _ptParts.push('. ' + p.episode_num);
    const _displayTitle = _ptParts.join('');
    progsHtml += `<div class="guide-prog${isNow ? ' now' : ''}${typeCls}" style="left:${left}px;width:${width}px" data-ch="${ch.id}" data-ps="${p.start}" data-pe="${p.stop}">
      ${isNow ? `<div class="guide-prog-progress" style="width:${pct}%"></div>` : ''}
      <span class="guide-prog-title">${esc(_displayTitle)}</span>
      <span class="guide-prog-time">${pStart.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>
    </div>`;
  }
  const logoSrc = ch.logo || '';
  const groupDivider = isGroupStart ? `<div class="guide-group-divider"><span>${esc(ch.group)}</span></div>` : '';
  return `${groupDivider}<div class="guide-row" data-chid="${ch.id}">
    <div class="guide-ch" onclick="openPreview('${ch.id}')">
      ${logoSrc ? `<img class="guide-ch-logo" src="${esc(logoSrc)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'guide-ch-logo-placeholder',textContent:'TV'}))" />` : `<div class="guide-ch-logo-placeholder">TV</div>`}
      <div class="guide-ch-info">
        <div class="guide-ch-name">${esc(ch.name)}</div>
        <div class="guide-ch-group">${esc(ch.group)}</div>
      </div>
    </div>
    <div class="guide-timeline" style="width:${tl.totalWidth}px">${progsHtml}</div>
  </div>`;
}

function renderGuide() {
  const container = $("#guide-container");
  const channels = _guideChannels;
  const now = new Date(guideData?.now || Date.now());
  const _nowTxt = `Now: ${now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})} — ${now.toLocaleDateString([], {weekday:'long',month:'short',day:'numeric'})} — ${channels.length} channels${guideOnlyActive ? ' (active-only)' : ''}`;
  let _syncHtml = "";
  const _synced = guideData && guideData.epg_synced;
  if (_guideFreshening) {
    _syncHtml = ` · <span style="color:var(--accent)">⟳ refreshing EPG…</span>`;
  } else if (_synced) {
    const _ageMs = Date.now() - new Date(_synced).getTime();
    const _col = _ageMs > GUIDE_STALE_MS ? "var(--warning)" : "var(--success)";
    _syncHtml = ` · <span style="color:${_col}" title="EPG last fetched ${esc(new Date(_synced).toLocaleString())}">EPG synced ${esc(relAge(_synced))}</span>`;
  }
  $("#guide-now-bar").innerHTML = esc(_nowTxt) + _syncHtml;

  if (!channels.length) {
    container.innerHTML = `<div class="card-empty"><div class="card-empty-text">No channels to display.</div></div>`;
    return;
  }

  const tl = _calcTimeline(now);
  _guideTimeline = tl;
  const nowLeft = ((now - tl.timeStart) / 60000) * tl.pxPerMin;

  // Time header
  let timeHtml = '';
  for (let t = new Date(tl.timeStart); t < tl.timeEnd; t = new Date(t.getTime() + 1800000)) {
    const left = ((t - tl.timeStart) / 60000) * tl.pxPerMin;
    const label = t.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    timeHtml += `<div class="guide-time-mark" style="left:${left}px">${label}</div>`;
  }

  // Build a set of indices that are group starts (for divider rendering)
  const groupStartSet = new Set(_guideGroupOffsets.map(g => g.startIdx));
  // Total height: rows + group dividers (28px each)
  const DIVIDER_H = 28;
  const totalH = channels.length * GUIDE_ROW_H + _guideGroupOffsets.length * DIVIDER_H;

  container.innerHTML = `<div class="guide-grid">
    <div class="guide-header">
      <div class="guide-ch-header">Channel</div>
      <div class="guide-time-row" id="guide-time-row" style="width:${tl.totalWidth}px">
        ${timeHtml}
        <div class="guide-now-line" style="left:${nowLeft}px"></div>
      </div>
    </div>
    <div class="guide-body" id="guide-body">
      <div class="guide-scroll-spacer" style="height:${totalH}px;width:${tl.totalWidth + 200}px"></div>
      <div class="guide-viewport" id="guide-viewport"></div>
    </div>
    <div class="guide-xscroll" id="guide-xscroll" aria-label="Guide horizontal scrollbar">
      <div class="guide-xscroll-inner" id="guide-xscroll-inner" style="width:${tl.totalWidth + 200}px"></div>
    </div>
  </div>`;

  const body = $("#guide-body");
  const viewport = $("#guide-viewport");
  const timeRow = $("#guide-time-row");
  const xscroll = $("#guide-xscroll");

  // Precompute Y offset for each row (accounting for dividers)
  const rowTops = [];
  let curY = 0;
  for (let i = 0; i < channels.length; i++) {
    if (groupStartSet.has(i)) curY += DIVIDER_H;
    rowTops.push(curY);
    curY += GUIDE_ROW_H;
  }

  // Virtual scroll: render only visible rows
  let _lastFirst = -1, _lastLast = -1;
  function renderVisible() {
    const scrollTop = body.scrollTop;
    const viewH = body.clientHeight;
    // Binary search for first visible row
    let first = 0, last = channels.length - 1;
    let lo = 0, hi = channels.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (rowTops[mid] + GUIDE_ROW_H < scrollTop - GUIDE_OVERSCAN * GUIDE_ROW_H) lo = mid + 1;
      else hi = mid - 1;
    }
    first = Math.max(0, lo);
    lo = first; hi = channels.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (rowTops[mid] < scrollTop + viewH + GUIDE_OVERSCAN * GUIDE_ROW_H) lo = mid + 1;
      else hi = mid - 1;
    }
    last = Math.min(channels.length - 1, lo);

    if (first === _lastFirst && last === _lastLast) return;
    _lastFirst = first; _lastLast = last;
    let html = '';
    for (let i = first; i <= last; i++) {
      const isGroupStart = groupStartSet.has(i);
      html += _renderRow(channels[i], tl, now, isGroupStart);
    }
    viewport.style.transform = `translateY(${rowTops[first] - (groupStartSet.has(first) ? DIVIDER_H : 0)}px)`;
    viewport.innerHTML = html;
  }

  let _syncingScroll = false;
  body.addEventListener("scroll", () => {
    timeRow.style.transform = `translateX(-${body.scrollLeft}px)`;
    if (xscroll && !_syncingScroll) {
      _syncingScroll = true;
      xscroll.scrollLeft = body.scrollLeft;
      _syncingScroll = false;
    }
    renderVisible();
    _highlightActiveGroup();
  }, { passive: true });

  if (xscroll) {
    xscroll.addEventListener("scroll", () => {
      if (_syncingScroll) return;
      _syncingScroll = true;
      body.scrollLeft = xscroll.scrollLeft;
      timeRow.style.transform = `translateX(-${body.scrollLeft}px)`;
      _syncingScroll = false;
    }, { passive: true });
  }

  // Initial render + scroll to now
  renderVisible();
  body.scrollLeft = Math.max(0, nowLeft - 300);
  if (xscroll) xscroll.scrollLeft = body.scrollLeft;

  // Expose scroll-to-group for sidebar
  window._guideScrollToGroup = (groupId) => {
    const offset = _guideGroupOffsets.find(g => g.id === groupId);
    if (!offset) return;
    body.scrollTop = rowTops[offset.startIdx] - DIVIDER_H;
  };

  // Highlight active group in sidebar based on scroll position
  function _highlightActiveGroup() {
    if (guideGroupFilter) return; // sidebar already has active selection
    const scrollTop = body.scrollTop + 10;
    let activeGid = "";
    for (const g of _guideGroupOffsets) {
      if (rowTops[g.startIdx] <= scrollTop) activeGid = g.id;
    }
    $$(".guide-group-btn", $("#guide-group-list")).forEach(btn => {
      if (!guideGroupFilter) {
        btn.classList.toggle("scrolled", btn.dataset.gid === activeGid && btn.dataset.gid !== "");
      }
    });
  }
}

// Programme click detail popup
document.addEventListener("click", (e) => {
  const prog = e.target.closest(".guide-prog");
  if (!prog) return;
  const chId = prog.dataset.ch;
  const ps = prog.dataset.ps;
  const pe = prog.dataset.pe;
  if (!chId || !guideData) return;
  const ch = guideData.channels.find(c => c.id === chId);
  if (!ch) return;
  const p = ch.programmes.find(pr => pr.start === ps && pr.stop === pe);
  if (!p) return;
  const s = new Date(p.start);
  const en = new Date(p.stop);
  const dur = Math.round((en - s) / 60000);
  document.querySelectorAll(".guide-popover").forEach(el => el.remove());
  const pop = document.createElement("div");
  pop.className = "guide-popover";
  pop.innerHTML = `<div class="guide-popover-title">${esc(p.title)}</div>
    <div class="guide-popover-time">${s.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})} – ${en.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})} (${dur}min)</div>
    ${p.desc ? `<div class="guide-popover-desc">${esc(p.desc)}</div>` : ''}
    <div class="guide-popover-ch">${esc(ch.name)}</div>`;
  document.body.appendChild(pop);
  const rect = prog.getBoundingClientRect();
  pop.style.top = `${Math.min(rect.bottom + 4, window.innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.min(rect.left, window.innerWidth - pop.offsetWidth - 8)}px`;
  const dismiss = (ev) => { if (!pop.contains(ev.target) && ev.target !== prog) { pop.remove(); document.removeEventListener("click", dismiss, true); } };
  setTimeout(() => document.addEventListener("click", dismiss, true), 0);
});

// Filter events (debounced)
let _guideFilterTimer;
$("#guide-search").addEventListener("input", (e) => {
  clearTimeout(_guideFilterTimer);
  _guideFilterTimer = setTimeout(() => {
    guideFilter = e.target.value.trim();
    _guideChannels = getVisibleChannels();
    _buildGroupOffsets();
    renderGuide();
  }, 250);
});
$("#guide-search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; guideFilter = ""; _guideChannels = getVisibleChannels(); _buildGroupOffsets(); renderGuide(); }
});
$("#guide-active-only")?.addEventListener("change", (e) => {
  guideOnlyActive = !!e.target.checked;
  _guideChannels = getVisibleChannels();
  _buildGroupOffsets();
  renderGuide();
});

function _guideJumpHours(hours) {
  const body = $("#guide-body");
  if (!body || !_guideTimeline) return;
  const base = new Date(guideData?.now || Date.now());
  const target = new Date(base.getTime() + (hours * 3600000));
  const left = ((target - _guideTimeline.timeStart) / 60000) * _guideTimeline.pxPerMin;
  const newLeft = Math.max(0, left - 280);
  body.scrollLeft = newLeft;
  const xscroll = $("#guide-xscroll");
  if (xscroll) xscroll.scrollLeft = newLeft;
}

function _guideJumpRelative(hours) {
  const body = $("#guide-body");
  if (!body || !_guideTimeline) return;
  const delta = hours * 3600000;
  const deltaPx = (delta / 60000) * _guideTimeline.pxPerMin;
  const newLeft = Math.max(0, body.scrollLeft + deltaPx);
  body.scrollLeft = newLeft;
  const xscroll = $("#guide-xscroll");
  if (xscroll) xscroll.scrollLeft = newLeft;
}

$("#guide-jump-minus2")?.addEventListener("click", () => _guideJumpHours(-2));
$("#guide-jump-now")?.addEventListener("click", () => _guideJumpHours(0));
$("#guide-jump-plus2")?.addEventListener("click", () => _guideJumpHours(2));
$("#guide-jump-plus6")?.addEventListener("click", () => _guideJumpHours(6));
$("#guide-arrow-left")?.addEventListener("click", () => _guideJumpRelative(-2));
$("#guide-arrow-right")?.addEventListener("click", () => _guideJumpRelative(2));
$("#guide-refresh-btn").addEventListener("click", async () => {
  const btn = $("#guide-refresh-btn");
  btnLoad(btn, "Refreshing...");
  const tid = toast("loading", "Rebuilding guide", "Regenerating export and refreshing guide cache...");
  try {
    await api("/api/export", { method: "POST" });
    await fetchGuideData("&force=true");
    _renderGuideFromData();
    updateToast(tid, "success", "Guide rebuilt");
  } catch (e) { updateToast(tid, "error", "Refresh failed", e.message); }
  finally { btnDone(btn); }
});

// Preview player — open the modal and start playback for a channel object.
// `ch` needs {id, name, url}; `logo` and `programmes` are optional, so this
// works both from the TV guide and from the channel editor.
async function _openPreview(ch) {
  if (!ch) return;
  const modal = $("#preview-modal");
  const player = $("#preview-player");
  const logo = $("#preview-logo");
  const name = $("#preview-ch-name");
  const nowInfo = $("#preview-ch-now");
  const schedule = $("#preview-schedule");

  name.textContent = ch.name;
  logo.src = ch.logo || "";
  logo.style.display = ch.logo ? "" : "none";
  logo.onerror = () => { logo.style.display = "none"; };

  const programmes = ch.programmes || [];
  const now = new Date();
  const current = programmes.find(p => new Date(p.start) <= now && new Date(p.stop) > now);
  nowInfo.textContent = current ? `Now: ${current.title}` : "No current programme info";

  const upcoming = programmes.filter(p => new Date(p.stop) > now).slice(0, 10);
  schedule.innerHTML = upcoming.map(p => {
    const s = new Date(p.start);
    const e = new Date(p.stop);
    const isCurrent = s <= now && e > now;
    return `<div class="preview-prog${isCurrent ? ' current' : ''}">
      <span class="preview-prog-time">${s.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})} - ${e.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>
      <span class="preview-prog-title">${esc(p.title)}</span>
      ${p.desc ? `<div class="preview-prog-desc">${esc(p.desc)}</div>` : ''}
    </div>`;
  }).join('') || '<div class="text-muted" style="padding:8px">No schedule data</div>';

  // Handle stream playback via the server proxy (avoids CORS + redirects).
  // Almost every IPTV channel is a raw MPEG-TS stream, which browsers can't
  // decode natively — those go through mpegts.js. True HLS (.m3u8) channels
  // go through hls.js. The proxy rewrites HLS playlists so segments load too.
  _teardownPlayer(player);
  // Absolute URL: mpegts.js fetches inside a Web Worker, which has no base
  // URL to resolve a relative path against.
  const proxyUrl = `${location.origin}/api/stream/${ch.id}`;
  const srcPath = (ch.url || "").toLowerCase().split("?")[0];
  modal.hidden = false;
  try {
    if (srcPath.endsWith(".m3u8")) {
      await ensureScript("https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js", "Hls");
      _playHls(player, proxyUrl);
    } else {
      await ensureScript("https://cdn.jsdelivr.net/npm/mpegts.js@1.7.3/dist/mpegts.js", "mpegts");
      _playMpegts(player, proxyUrl);
    }
  } catch (err) {
    toast("error", "Player library failed to load", err.message);
  }
}

// TV guide path: look the channel up in the loaded guide data.
window.openPreview = (chId) => {
  if (!guideData) return;
  _openPreview(guideData.channels.find(c => c.id === chId));
};

// Channel-editor path: play a channel straight from its cached editor row.
// Works for disabled channels, which never appear in the TV guide.
window.previewChannelFromEditor = (chId, gid) => {
  const cache = channelCache[gid];
  const ch = cache && cache.channels.find(c => c.id === chId);
  if (ch) _openPreview(ch);
  else toast("error", "Can't play channel", "Channel data not loaded");
};

// Tear down whichever player engine is currently attached.
function _teardownPlayer(player) {
  try { if (player) player.pause(); } catch (_) {}
  if (window._hlsInstance) {
    try { window._hlsInstance.destroy(); } catch (_) {}
    window._hlsInstance = null;
  }
  if (window._mpegtsInstance) {
    try { window._mpegtsInstance.destroy(); } catch (_) {}
    window._mpegtsInstance = null;
  }
}

// Play a raw MPEG-TS stream via mpegts.js (decodes TS through MSE).
// `transcoded` is true on the retry pass through the server-side transcoder.
function _playMpegts(player, src, transcoded) {
  if (typeof mpegts !== "undefined" && mpegts.isSupported()) {
    const p = mpegts.createPlayer(
      { type: "mpegts", isLive: true, url: src },
      {
        enableWorker: true,
        // Don't chase the live edge — it trims the buffer and causes constant
        // re-buffering on a proxied stream. Smooth playback > low latency here.
        liveBufferLatencyChasing: false,
        enableStashBuffer: true,
        stashInitialSize: 1024 * 1024,   // 1MB cushion so playback starts primed
        lazyLoad: false,                 // keep pulling data, don't pause loading
        autoCleanupSourceBuffer: true,   // bound memory over a long watch
      }
    );
    p.attachMediaElement(player);
    p.on(mpegts.Events.ERROR, (type, detail, info) => {
      console.warn("mpegts error", type, detail, info);
      // A media error on the first pass means the browser can't decode the
      // channel's native codecs (OTA channels are MPEG-2 video / AC-3 audio).
      // Retry once through the server-side transcoder, which re-encodes to
      // browser-playable H.264/AAC.
      if (!transcoded && type === mpegts.ErrorTypes.MEDIA_ERROR) {
        _teardownPlayer(player);
        const sep = src.includes("?") ? "&" : "?";
        _playMpegts(player, `${src}${sep}transcode=1`, true);
        return;
      }
      toast("error", "Playback failed",
            transcoded ? "Transcode failed — stream may be offline"
                       : `${detail || type}`);
    });
    p.load();
    p.play().catch(() => {});
    window._mpegtsInstance = p;
  } else {
    // Browser lacks Media Source Extensions — last-ditch native attempt.
    player.src = src;
    player.load();
  }
}

// Play an HLS (.m3u8) stream via hls.js, or native HLS on Safari.
function _playHls(player, src) {
  if (typeof Hls !== "undefined" && Hls.isSupported()) {
    const hls = new Hls({ enableWorker: true, xhrSetup: (xhr) => { xhr.timeout = 20000; } });
    hls.loadSource(src);
    hls.attachMedia(player);
    hls.on(Hls.Events.MANIFEST_PARSED, () => { player.play().catch(() => {}); });
    hls.on(Hls.Events.ERROR, (_e, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      console.warn("hls error", data);
      hls.destroy();
      window._hlsInstance = null;
      toast("error", "Playback failed", data.details || "HLS error");
    });
    window._hlsInstance = hls;
  } else {
    // Safari (native HLS) and everything else.
    player.src = src;
    player.load();
  }
}

function _closePreview() {
  const player = $("#preview-player");
  _teardownPlayer(player);
  player.src = "";
  $("#preview-modal").hidden = true;
}
$("#preview-close").addEventListener("click", _closePreview);
$("#preview-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) _closePreview();
});

/* ═══════════════════════════════════════════════════════════════════════
   SETTINGS — Backup / Restore
   ═══════════════════════════════════════════════════════════════════════ */
$("#restore-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const ok = await confirmDialog("Restore Backup", "This will replace ALL current data (sources, channels, groups, rules, settings). This cannot be undone.", "Restore");
  if (!ok) { e.target.value = ""; return; }
  const tid = toast("loading", "Restoring from backup");
  const text = await file.text();
  try {
    await api("/api/restore", { method: "POST", body: text });
    updateToast(tid, "success", "Restore complete", "All data has been restored.");
    navigate("dashboard");
  } catch (err) {
    updateToast(tid, "error", "Restore failed", err.message);
  }
  e.target.value = "";
});

/* ═══════════════════════════════════════════════════════════════════════
   SETTINGS — Auto Refresh
   ═══════════════════════════════════════════════════════════════════════ */
async function loadRefreshSettings() {
  try {
    const s = await api("/api/settings");
    const sel = $("#refresh-interval");
    sel.value = s.refresh_interval_minutes || "60";
    if (!sel.value) sel.value = "60";
    const wsel = $("#epg-window-days");
    wsel.value = s.epg_window_days || "4";
    if (!wsel.value) wsel.value = "4";
    updateRefreshStatus();
    loadIntegrations(s);
  } catch (e) { console.error(e); }
  loadSmartGroups();
  loadSystemErrors();
}

/* ── Integrations (Dispatcharr auto-refresh + ntfy) ─────────────────── */
async function loadIntegrations(settings = null) {
  if (!$("#integrations-form")) return;
  try {
    const s = settings || await api("/api/settings");
    $("#da-enabled").value = s.dispatcharr_auto_refresh || "0";
    $("#da-url").value = s.dispatcharr_url || "";
    $("#da-username").value = s.dispatcharr_username || "";
    $("#da-password").value = "";
    $("#da-password").placeholder = s.dispatcharr_password_configured ? "Saved — enter only to replace" : "Password";
    $("#da-account").value = s.dispatcharr_m3u_account_id || "";
    if ($("#da-epg")) $("#da-epg").value = s.dispatcharr_epg_source_id || "";
    $("#ntfy-enabled").value = s.ntfy_enabled || "0";
    $("#ntfy-url").value = s.ntfy_url || "";
    $("#ntfy-topic").value = s.ntfy_topic || "";
    $("#ntfy-token").value = "";
    $("#ntfy-token").placeholder = s.ntfy_token_configured ? "Saved — enter only to replace" : "Optional token";
  } catch (e) { console.error(e); }
}

$("#integrations-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PATCH", body: JSON.stringify({
      dispatcharr_auto_refresh: $("#da-enabled").value,
      dispatcharr_url: $("#da-url").value.trim(),
      dispatcharr_username: $("#da-username").value.trim(),
      dispatcharr_password: $("#da-password").value,
      dispatcharr_m3u_account_id: $("#da-account").value.trim(),
      dispatcharr_epg_source_id: ($("#da-epg")?.value || "").trim(),
      ntfy_enabled: $("#ntfy-enabled").value,
      ntfy_url: $("#ntfy-url").value.trim(),
      ntfy_topic: $("#ntfy-topic").value.trim(),
      ntfy_token: $("#ntfy-token").value,
    }) });
    toast("success", "Integrations saved");
  } catch (err) { toast("error", "Save failed", err.message); }
});

/* ── Match logo coverage + override ─────────────────────────────────── */
async function loadLogoCoverage() {
  const body = $("#logo-coverage-body");
  if (!body) return;
  try {
    const d = await api("/api/logos/coverage");
    if (!d || !d.at) {
      body.innerHTML = '<p class="text-muted">No data yet — run an export to populate logo coverage.</p>';
      return;
    }
    const sample = (d.unresolved_sample || []).slice(0, 40).map(u =>
      `<tr><td>${esc(u.away || "")} vs ${esc(u.home || "")}</td><td class="cell-dim">${esc(u.sport || "")}</td><td class="cell-dim">${esc((u.missing || []).join(", "))}</td></tr>`
    ).join("");
    body.innerHTML = `
      <div class="text-muted" style="font-size:13px;margin-bottom:8px">
        <strong>${d.resolved_titles}</strong> matchups resolved · <strong>${d.programmes_tagged}</strong> programmes tagged ·
        <strong>${d.unresolved_count}</strong> unresolved · <span class="cell-dim">${esc(new Date(d.at).toLocaleString())}</span>
      </div>
      ${sample ? `<details><summary style="cursor:pointer;font-size:12px;font-weight:600">Unresolved matchups (need a team badge)</summary>
        <div class="table-wrap" style="margin-top:8px"><table class="data-table" style="font-size:12px">
          <thead><tr><th>Matchup</th><th style="width:140px">Sport</th><th style="width:180px">Missing team(s)</th></tr></thead>
          <tbody>${sample}</tbody></table></div></details>` : ""}`;
  } catch (e) {
    body.innerHTML = `<p class="text-muted">Failed to load: ${esc(e.message)}</p>`;
  }
}

$("#logo-coverage-refresh")?.addEventListener("click", loadLogoCoverage);

$("#logo-override-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const team = $("#lo-team").value.trim(), sport = $("#lo-sport").value.trim(), url = $("#lo-url").value.trim();
  if (!team || !sport || !url) { toast("error", "Team, sport and URL are all required"); return; }
  try {
    await api("/api/logos/override", { method: "POST", body: JSON.stringify({ team, sport, badge_url: url }) });
    toast("success", "Override saved — applies on next export");
    $("#lo-team").value = ""; $("#lo-url").value = "";
  } catch (err) { toast("error", "Override failed", err.message); }
});

$("#logo-clear-neg")?.addEventListener("click", async () => {
  try {
    const r = await api("/api/logos/clear-cache?negatives_only=true", { method: "POST" });
    toast("success", `Cleared ${r.removed} failed lookup(s) — they'll retry next export`);
  } catch (err) { toast("error", "Clear failed", err.message); }
});

/* ── EPG diagnostics ────────────────────────────────────────────────── */
async function loadEpgDiagnostics() {
  const body = $("#epg-diag-body");
  if (!body) return;
  try {
    const d = await api("/api/epg/diagnostics");
    const m = d.match || {};
    const srcRows = (d.sources || []).map(s => {
      const ok = s.last_error ? `<span style="color:var(--danger)">error</span>` :
                 (s.status === "error" ? `<span style="color:var(--danger)">error</span>` : `<span style="color:var(--success)">ok</span>`);
      return `<tr><td class="cell-mono" style="word-break:break-all">${esc(s.url || "")}</td><td>${ok}</td>
        <td class="cell-dim">${s.channels != null ? s.channels : ""}</td>
        <td class="cell-dim">${esc(s.last_error || s.error || "")}</td></tr>`;
    }).join("");
    const sample = (m.unmatched_sample || []).slice(0, 40).map(u =>
      `<tr><td>${esc(u.name || "")}</td><td class="cell-dim">${esc(u.group || "")}</td><td class="cell-mono cell-dim">${esc(u.tvg_id || "")}</td></tr>`
    ).join("");
    body.innerHTML = `
      <div class="text-muted" style="font-size:13px;margin-bottom:8px">
        ${m.at ? `Last export matched <strong>${m.matched}</strong>/<strong>${m.total}</strong> channels (<strong>${m.coverage_pct}%</strong>) · <span class="cell-dim">${esc(new Date(m.at).toLocaleString())}</span>` : "No export data yet."}
      </div>
      ${srcRows ? `<div class="table-wrap"><table class="data-table" style="font-size:12px">
        <thead><tr><th>EPG source</th><th style="width:60px">State</th><th style="width:80px">Channels</th><th>Last error</th></tr></thead>
        <tbody>${srcRows}</tbody></table></div>` : ""}
      ${sample ? `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;font-weight:600">Channels with no guide data</summary>
        <div class="table-wrap" style="margin-top:8px"><table class="data-table" style="font-size:12px">
          <thead><tr><th>Channel</th><th style="width:160px">Group</th><th style="width:160px">tvg-id</th></tr></thead>
          <tbody>${sample}</tbody></table></div></details>` : ""}`;
  } catch (e) {
    body.innerHTML = `<p class="text-muted">Failed to load: ${esc(e.message)}</p>`;
  }
}

$("#epg-diag-refresh")?.addEventListener("click", loadEpgDiagnostics);

async function loadSystemErrors() {
  const body = $("#system-errors-body");
  if (!body) return;
  body.innerHTML = '<p class="text-muted">Loading...</p>';
  try {
    const d = await api("/api/system/errors?limit=100");
    const rows = d.errors || [];
    if (!rows.length) {
      body.innerHTML = '<div class="card-empty"><div class="card-empty-text">No backend errors recorded.</div></div>';
      return;
    }
    body.innerHTML = `<div class="table-wrap"><table class="data-table" style="font-size:12px">
      <thead><tr><th style="width:180px">Time</th><th style="width:220px">Context</th><th>Error</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td class="cell-mono cell-dim">${esc(new Date(r.time).toLocaleString())}</td>
        <td class="cell-mono">${esc(r.context || "")}</td>
        <td>${esc(r.error || "")}</td>
      </tr>`).join("")}</tbody>
    </table></div>`;
  } catch (e) {
    body.innerHTML = `<p class="text-muted">Failed to load errors: ${esc(e.message)}</p>`;
  }
}

$("#system-errors-refresh")?.addEventListener("click", loadSystemErrors);
$("#system-errors-clear")?.addEventListener("click", async () => {
  try {
    await api("/api/system/errors", { method: "DELETE" });
    toast("success", "System errors cleared");
    loadSystemErrors();
  } catch (e) {
    toast("error", "Clear failed", e.message);
  }
});

async function updateRefreshStatus() {
  try {
    const r = await api("/api/refresh-status");
    const el = $("#refresh-status");
    const last = r.last_refresh ? new Date(r.last_refresh).toLocaleTimeString() : "never";
    const nextMin = Math.ceil(r.next_refresh_seconds / 60);
    el.textContent = `Last refresh: ${last} · Next in ~${nextMin} min · Interval: ${r.interval_minutes} min`;
  } catch (_) {}
}

$("#refresh-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ refresh_interval_minutes: $("#refresh-interval").value, epg_window_days: $("#epg-window-days").value }),
    });
    toast("success", "Refresh interval saved");
    updateRefreshStatus();
  } catch (err) { toast("error", "Save failed", err.message); }
});

$("#refresh-now-btn").addEventListener("click", async () => {
  const btn = $("#refresh-now-btn");
  btnLoad(btn, "Refreshing...");
  const tid = toast("loading", "Refreshing sources", "Reimporting all active sources and rebuilding EPG...");
  try {
    await api("/api/refresh-all", { method: "POST" });
    updateToast(tid, "success", "All sources refreshed");
    updateRefreshStatus();
  } catch (err) { updateToast(tid, "error", "Refresh failed", err.message); }
  finally { btnDone(btn); }
});

/* ═══════════════════════════════════════════════════════════════════════
   SMART GROUPS
   ═══════════════════════════════════════════════════════════════════════ */

let smartGroupsConfig = {};
let smartGroupsCities = {};

async function loadSmartGroups() {
  try {
    [smartGroupsConfig, smartGroupsCities] = await Promise.all([
      api("/api/smart-groups"),
      api("/api/smart-groups/cities"),
    ]);
  } catch (_) {
    smartGroupsConfig = {};
    smartGroupsCities = {};
  }
  renderSmartGroupsCities();
  // Restore saved state
  const en = $("#smart-groups-enabled");
  en.checked = !!smartGroupsConfig.enabled;
  $("#sg-window").value = smartGroupsConfig.window_hours || "4";
  $("#sg-custom-keywords").value = (smartGroupsConfig.custom_keywords || []).join(", ");
  $("#sg-locals-enabled").checked = !!smartGroupsConfig.locals_enabled;
  $("#sg-locals-cities").value = (smartGroupsConfig.locals_cities || []).join("\n");
  $("#sg-locals-weather").checked = smartGroupsConfig.locals_include_weather !== false;
  updateSmartGroupsBodyState();
}

function updateSmartGroupsBodyState() {
  const body = $("#smart-groups-body");
  const enabled = $("#smart-groups-enabled").checked;
  body.style.opacity = enabled ? "1" : "0.45";
  body.style.pointerEvents = enabled ? "auto" : "none";
}

$("#smart-groups-enabled").addEventListener("change", updateSmartGroupsBodyState);

function renderSmartGroupsCities() {
  const container = $("#smart-groups-cities");
  const selectedCities = new Set(smartGroupsConfig.cities || []);
  const cityNames = Object.keys(smartGroupsCities).sort();
  container.innerHTML = cityNames.map(city => {
    const teams = smartGroupsCities[city];
    const checked = selectedCities.has(city) ? "checked" : "";
    const teamList = teams.join(", ");
    return `
      <label class="sg-city-item" style="display:flex;align-items:flex-start;gap:6px;cursor:pointer;font-size:13px;padding:4px 0" title="${teamList}">
        <input type="checkbox" class="sg-city-cb" value="${city}" ${checked} style="margin-top:2px" />
        <span>
          <strong>${city}</strong>
          <span class="text-muted" style="font-size:11px;display:block">${teamList}</span>
        </span>
      </label>
    `;
  }).join("");
}

function collectSmartGroupsFromUI() {
  const checked = Array.from($$("#smart-groups-cities .sg-city-cb:checked")).map(cb => cb.value);
  const customKw = $("#sg-custom-keywords").value.split(",").map(k => k.trim()).filter(Boolean);
  const localsCities = $("#sg-locals-cities").value
    .split("\n").map(s => s.trim()).filter(Boolean);
  return {
    enabled: $("#smart-groups-enabled").checked,
    cities: checked,
    window_hours: parseInt($("#sg-window").value) || 4,
    custom_keywords: customKw,
    locals_enabled: $("#sg-locals-enabled").checked,
    locals_cities: localsCities,
    locals_include_weather: $("#sg-locals-weather").checked,
  };
}

$("#save-smart-groups-btn").addEventListener("click", async () => {
  const btn = $("#save-smart-groups-btn");
  btnLoad(btn, "Saving...");
  try {
    const data = collectSmartGroupsFromUI();
    smartGroupsConfig = await api("/api/smart-groups", {
      method: "PUT",
      body: JSON.stringify(data),
    });
    toast("success", "Smart groups saved", data.enabled ? "EPG scan running in background..." : "Feature disabled — shadow channels cleaned up.");
  } catch (err) { toast("error", "Save failed", err.message); }
  finally { btnDone(btn); }
});

$("#refresh-smart-groups-btn").addEventListener("click", async () => {
  const btn = $("#refresh-smart-groups-btn");
  btnLoad(btn, "Scanning...");
  const tid = toast("loading", "Scanning EPG", "Searching for matching programmes...");
  try {
    await api("/api/smart-groups/refresh", { method: "POST" });
    updateToast(tid, "success", "Smart groups refreshed");
  } catch (err) { updateToast(tid, "error", "Scan failed", err.message); }
  finally { btnDone(btn); }
});

/* ═══════════════════════════════════════════════════════════════════════
   EPG COVERAGE
   ═══════════════════════════════════════════════════════════════════════ */
$("#epg-coverage-btn")?.addEventListener("click", runEpgCoverage);

async function runEpgCoverage() {
  const btn = $("#epg-coverage-btn");
  const el = $("#epg-coverage-result");
  btnLoad(btn, "Running...");
  try {
    const d = await api("/api/epg/coverage");
    const pct = d.coverage_pct ?? 0;
    const barColor = pct >= 80 ? "var(--success)" : pct >= 40 ? "var(--warning)" : "var(--danger)";
    const auditColor = d.perfect ? "var(--success)" : d.status === "error" ? "var(--danger)" : "var(--warning)";
    const auditLabel = d.perfect ? "Perfect" : d.status === "error" ? "Broken" : "Needs attention";
    let groupsHtml = "";
    if (d.groups && d.groups.length) {
      groupsHtml = `<details style="margin-top:12px"><summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text-dim)">Groups with lowest coverage</summary>
        <div class="table-wrap" style="margin-top:8px"><table class="data-table" style="font-size:12px">
          <thead><tr><th>Group</th><th>Matched</th><th>Total</th><th>Coverage</th></tr></thead>
          <tbody>${d.groups.map(g => {
            const gp = g.total ? Math.round(100 * g.matched / g.total) : 0;
            return `<tr><td>${esc(g.name)}</td><td>${g.matched}</td><td>${g.total}</td>
              <td><div style="display:flex;align-items:center;gap:6px">
                <div style="flex:1;height:6px;background:var(--border);border-radius:3px">
                  <div style="width:${gp}%;height:6px;background:${gp>=80?"var(--success)":gp>=40?"var(--warning)":"var(--danger)"};border-radius:3px"></div>
                </div><span>${gp}%</span></div></td></tr>`;
          }).join("")}</tbody>
        </table></div></details>`;
    }

    const issuesHtml = d.issues && d.issues.length
      ? `<div style="margin-top:12px;padding:10px 12px;border:1px solid ${auditColor};border-radius:10px;background:color-mix(in srgb, ${auditColor} 10%, transparent)">
          <div style="font-size:13px;font-weight:700;color:${auditColor};margin-bottom:6px">Audit findings</div>
          <ul style="margin:0;padding-left:18px;font-size:12px">${d.issues.slice(0, 8).map(issue => `<li>${esc(issue)}</li>`).join("")}</ul>
        </div>`
      : "";

    const sampleRows = [
      ...(d.sample_missing || []).slice(0, 3).map(x => ({ kind: "No tvg-id", name: x.name, group: x.group })),
      ...(d.sample_missing_in_xml || []).slice(0, 3).map(x => ({ kind: "Missing XML", name: x.name, group: x.group })),
      ...(d.sample_no_programmes || []).slice(0, 3).map(x => ({ kind: "No shows", name: x.name, group: x.group })),
    ].slice(0, 8);

    const samplesHtml = sampleRows.length
      ? `<details style="margin-top:12px"><summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text-dim)">Sample problem channels</summary>
          <div class="table-wrap" style="margin-top:8px"><table class="data-table" style="font-size:12px">
            <thead><tr><th>Issue</th><th>Channel</th><th>Group</th></tr></thead>
            <tbody>${sampleRows.map(row => `<tr><td>${esc(row.kind)}</td><td>${esc(row.name || "Unknown")}</td><td>${esc(row.group || "—")}</td></tr>`).join("")}</tbody>
          </table></div></details>`
      : "";

    el.innerHTML = `
      <div style="margin-bottom:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;flex-wrap:wrap">
          <div style="font-size:14px;font-weight:700;color:${auditColor}">${esc(d.summary || "EPG audit complete")}</div>
          <span style="padding:4px 10px;border-radius:999px;background:${auditColor};color:white;font-size:12px;font-weight:700">${auditLabel}</span>
        </div>
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
          <div style="flex:1;height:10px;background:var(--border);border-radius:5px;overflow:hidden">
            <div style="width:${pct}%;height:10px;background:${barColor};border-radius:5px;transition:width .4s"></div>
          </div>
          <span style="font-size:18px;font-weight:700;color:${barColor}">${pct}%</span>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:13px">
          <div style="text-align:center;padding:8px;background:rgba(34,197,94,.08);border-radius:8px">
            <div style="font-size:20px;font-weight:700;color:var(--success)">${(d.matched||0).toLocaleString()}</div>
            <div class="text-muted">Real EPG</div>
          </div>
          <div style="text-align:center;padding:8px;background:rgba(59,130,246,.08);border-radius:8px">
            <div style="font-size:20px;font-weight:700;color:var(--accent)">${(d.dummy||0).toLocaleString()}</div>
            <div class="text-muted">Dummy EPG</div>
          </div>
          <div style="text-align:center;padding:8px;background:rgba(148,163,184,.08);border-radius:8px">
            <div style="font-size:20px;font-weight:700;color:var(--text-dim)">${(d.unmatched||0).toLocaleString()}</div>
            <div class="text-muted">No EPG</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:13px;margin-top:10px">
          <div style="text-align:center;padding:8px;background:rgba(239,68,68,.08);border-radius:8px">
            <div style="font-size:18px;font-weight:700;color:var(--danger)">${(d.missing_in_xml||0).toLocaleString()}</div>
            <div class="text-muted">Missing in XML</div>
          </div>
          <div style="text-align:center;padding:8px;background:rgba(245,158,11,.08);border-radius:8px">
            <div style="font-size:18px;font-weight:700;color:var(--warning)">${(d.no_programmes||0).toLocaleString()}</div>
            <div class="text-muted">No Shows</div>
          </div>
          <div style="text-align:center;padding:8px;background:rgba(59,130,246,.08);border-radius:8px">
            <div style="font-size:18px;font-weight:700;color:var(--accent)">${(d.collisions||0).toLocaleString()}</div>
            <div class="text-muted">Collisions</div>
          </div>
          <div style="text-align:center;padding:8px;background:rgba(34,197,94,.08);border-radius:8px">
            <div style="font-size:18px;font-weight:700;color:var(--success)">${(d.xml_programmes||0).toLocaleString()}</div>
            <div class="text-muted">Programmes</div>
          </div>
        </div>
        ${issuesHtml}
      </div>${samplesHtml}${groupsHtml}`;
  } catch (e) {
    el.innerHTML = `<p class="text-muted">Failed to load coverage.</p>`;
  } finally { btnDone(btn); }
}

async function runEpgDashboard() {
  const el = $("#epg-coverage-result");
  const tid = toast("loading", "Loading EPG dashboard");
  try {
    const d = await api("/api/epg/dashboard");
    const rows = (d.groups || []).slice(0, 25).map(g => `<tr>
      <td>${esc(g.group)}</td>
      <td>${g.channels}</td>
      <td>${g.with_programmes}</td>
      <td>${g.without_programmes}</td>
      <td>${g.blank_title}</td>
      <td><b>${g.score}%</b></td>
    </tr>`).join("");
    el.innerHTML = `<div style="margin-bottom:10px"><b>${d.totals?.channels || 0}</b> exported IDs · <b>${d.totals?.with_programmes || 0}</b> with programmes</div>
      <div class="table-wrap"><table class="data-table" style="font-size:12px"><thead><tr><th>Group</th><th>Total</th><th>With</th><th>Without</th><th>Blank</th><th>Score</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div style="margin-top:8px;font-size:12px" class="text-muted">Top bad mappings: ${(d.top_bad_mappings || []).length}</div>`;
    updateToast(tid, "success", "EPG dashboard ready");
  } catch (e) {
    updateToast(tid, "error", "Dashboard failed", e.message);
  }
}

async function runEpgDiff() {
  const el = $("#epg-coverage-result");
  const tid = toast("loading", "Calculating export diff");
  try {
    const d = await api("/api/epg/diff");
    if (d.status !== "ok") {
      el.innerHTML = `<div class="text-muted">${esc(d.summary || "No diff available yet")}</div>`;
      updateToast(tid, "info", "No diff available");
      return;
    }
    el.innerHTML = `<div><b>Added IDs:</b> ${d.added_ids} · <b>Removed IDs:</b> ${d.removed_ids} · <b>Changed programme counts:</b> ${d.changed_programme_counts}</div>`;
    updateToast(tid, "success", "Export diff ready");
  } catch (e) {
    updateToast(tid, "error", "Diff failed", e.message);
  }
}

async function runEpgRepairPack() {
  const tid = toast("loading", "Running repair pack");
  try {
    const d = await api("/api/epg/repair-pack", { method: "POST", body: JSON.stringify({
      restore_originals: true,
      clear_dummy: true,
      fix_suspicious: true,
      dry_run: false,
    })});
    updateToast(tid, "success", `Repair complete`, `${d.updated} channel(s) updated`);
    startExportPoll();
  } catch (e) {
    updateToast(tid, "error", "Repair failed", e.message);
  }
}

async function runEpgStale() {
  const el = $("#epg-coverage-result");
  const tid = toast("loading", "Checking for stale guides");
  try {
    const d = await api("/api/epg/stale");
    const kindLabel = { empty: "No shows", synthetic: "Synthetic only", dummy: "Dummy only" };
    const kindColor = { empty: "var(--danger)", synthetic: "var(--warning)", dummy: "var(--warning)" };
    if (!d.total_stale) {
      el.innerHTML = `<div style="padding:10px 12px;border:1px solid var(--success);border-radius:10px;background:color-mix(in srgb,var(--success) 10%,transparent);font-size:13px;font-weight:600;color:var(--success)">All ${d.total_exported} exported channels have real guide data in the next 24h.</div>`;
      updateToast(tid, "success", "No stale guides");
      return;
    }
    const groupsHtml = (d.by_group || []).slice(0, 12)
      .map(([g, n]) => `<tr><td>${esc(g)}</td><td><b>${n}</b></td></tr>`).join("");
    const rows = (d.stale || []).map(s => `<tr>
      <td><span style="color:${kindColor[s.kind] || "var(--text-dim)"}">${esc(kindLabel[s.kind] || s.kind)}</span></td>
      <td>${esc(s.name)}</td>
      <td>${esc(s.group)}</td>
      <td class="cell-mono" style="font-size:11px">${esc(s.tvg_id)}</td>
    </tr>`).join("");
    el.innerHTML = `
      <div style="margin-bottom:10px;font-size:14px;font-weight:700;color:var(--warning)">
        ${d.total_stale} of ${d.total_exported} exported channels have no real guide data in the next 24h.
      </div>
      <details style="margin-bottom:10px"><summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text-dim)">Worst groups</summary>
        <div class="table-wrap" style="margin-top:8px"><table class="data-table" style="font-size:12px">
          <thead><tr><th>Group</th><th>Stale</th></tr></thead><tbody>${groupsHtml}</tbody></table></div>
      </details>
      <div class="table-wrap"><table class="data-table" style="font-size:12px">
        <thead><tr><th>Issue</th><th>Channel</th><th>Group</th><th>tvg-id</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
    updateToast(tid, "success", `${d.total_stale} stale guide${d.total_stale === 1 ? "" : "s"}`);
  } catch (e) {
    updateToast(tid, "error", "Stale check failed", e.message);
  }
}

$("#epg-dashboard-btn")?.addEventListener("click", runEpgDashboard);
$("#epg-stale-btn")?.addEventListener("click", runEpgStale);
$("#epg-diff-btn")?.addEventListener("click", runEpgDiff);
$("#epg-repair-btn")?.addEventListener("click", runEpgRepairPack);


/* ═══════════════════════════════════════════════════════════════════════
   IMPORT DIFF MODAL
   ═══════════════════════════════════════════════════════════════════════ */
window.showImportDiff = async (sid, name) => {
  const modal = $("#import-diff-modal");
  const title = $("#import-diff-title");
  const body = $("#import-diff-body");
  title.textContent = `Last Import — ${name}`;
  // Fetch source to get last_import_diff
  try {
    const sources = await api("/api/sources");
    const src = sources.find(s => s.id === sid);
    const diff = src?.last_import_diff ? JSON.parse(src.last_import_diff) : null;
    if (!diff) {
      body.innerHTML = `<p class="text-muted">No import history recorded yet. Run a sync first.</p>`;
    } else {
      const at = diff.at ? new Date(diff.at).toLocaleString() : "(unknown time)";
      body.innerHTML = `
        <p class="text-muted" style="font-size:12px;margin-bottom:12px">Last synced: ${at}</p>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px">
          <div style="text-align:center;padding:10px;background:rgba(34,197,94,.08);border-radius:8px">
            <div style="font-size:24px;font-weight:700;color:var(--success)">+${diff.added||0}</div>
            <div class="text-muted" style="font-size:12px">Added</div>
          </div>
          <div style="text-align:center;padding:10px;background:rgba(148,163,184,.08);border-radius:8px">
            <div style="font-size:24px;font-weight:700">${diff.updated||0}</div>
            <div class="text-muted" style="font-size:12px">Updated</div>
          </div>
          <div style="text-align:center;padding:10px;background:rgba(239,68,68,.08);border-radius:8px">
            <div style="font-size:24px;font-weight:700;color:var(--danger)">-${diff.removed||0}</div>
            <div class="text-muted" style="font-size:12px">Removed</div>
          </div>
        </div>`;
    }
  } catch (e) {
    body.innerHTML = `<p class="text-muted">Failed to load diff: ${esc(e.message)}</p>`;
  }
  modal.hidden = false;
};
document.getElementById("import-diff-close")?.addEventListener("click", () => {
  $("#import-diff-modal").hidden = true;
});
$("#import-diff-modal")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.hidden = true;
});


/* ═══════════════════════════════════════════════════════════════════════
   GROUP TEMPLATES MODAL
   ═══════════════════════════════════════════════════════════════════════ */
let _templatesForGid = null;

window.openTemplatesFor = async (gid) => {
  _templatesForGid = gid;
  const modal = $("#templates-modal");
  await loadTemplates();
  modal.hidden = false;
};
document.getElementById("templates-modal-close")?.addEventListener("click", () => {
  $("#templates-modal").hidden = true;
});
$("#templates-modal")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.hidden = true;
});

async function loadTemplates() {
  const el = $("#templates-list");
  try {
    const templates = await api("/api/groups/templates");
    const gid = _templatesForGid;
    const grp = groupsData.find(g => g.id === gid);
    let html = "";
    if (grp) {
      html += `<div style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid var(--border)">
        <p style="font-weight:600;margin-bottom:8px;font-size:13px">Save current group as template</p>
        <div style="display:flex;gap:8px">
          <input type="text" id="new-template-name" placeholder="Template name..." style="flex:1" value="${esc(grp.name)}" />
          <button class="btn btn-primary btn-sm" onclick="saveTemplateFrom('${gid}')">Save</button>
        </div>
      </div>`;
    }
    if (!templates.length) {
      html += `<p class="text-muted">No templates saved yet.</p>`;
    } else {
      html += templates.map(t => `
        <div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)">
          <div style="flex:1">
            <div style="font-weight:600">${esc(t.name)}</div>
            <div class="text-muted" style="font-size:12px">${t.rules?.length || 0} rules · ${t.category || "no category"} · ${t.teamarr ? "Teamarr " : ""}${t.name_epg ? "DummyEPG " : ""}${t.export_tag ? `[${esc(t.export_tag)}]` : ""}</div>
          </div>
          ${gid ? `<button class="btn btn-sm btn-primary" onclick="applyTemplate('${gid}','${t.id}')">Apply</button>` : ""}
          <button class="btn btn-sm btn-danger" onclick="deleteTemplate('${t.id}')">Delete</button>
        </div>`).join("");
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<p class="text-muted">Failed to load templates.</p>`;
  }
}

window.saveTemplateFrom = async (gid) => {
  const name = ($("#new-template-name")?.value || "").trim();
  if (!name) { toast("error", "Template name required"); return; }
  try {
    await api("/api/groups/templates", { method: "POST", body: JSON.stringify({ name, group_id: gid }) });
    toast("success", "Template saved", name);
    await loadTemplates();
  } catch (e) { toast("error", "Save failed", e.message); }
};

window.applyTemplate = async (gid, tid) => {
  const ok = await confirmDialog("Apply Template", "This will overwrite the group's rules and settings. Continue?", "Apply");
  if (!ok) return;
  try {
    await api(`/api/groups/${gid}/apply-template`, { method: "POST", body: JSON.stringify({ template_id: tid }) });
    toast("success", "Template applied");
    channelCache = {};
    await fetchGroups();
    renderGroups();
    $("#templates-modal").hidden = true;
  } catch (e) { toast("error", "Apply failed", e.message); }
};

window.deleteTemplate = async (tid) => {
  const ok = await confirmDialog("Delete Template", "Delete this template?", "Delete");
  if (!ok) return;
  try {
    await api(`/api/groups/templates/${tid}`, { method: "DELETE" });
    await loadTemplates();
  } catch (e) { toast("error", "Delete failed", e.message); }
};


/* ═══════════════════════════════════════════════════════════════════════
   PER-GROUP EXPORT TAG
   ═══════════════════════════════════════════════════════════════════════ */
window.setGroupTag = async (gid, currentTag) => {
  const tag = prompt("Export tag for smart-group copies (leave blank to auto-generate):", currentTag || "");
  if (tag === null) return; // cancelled
  try {
    await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ export_tag: tag.trim() }) });
    toast("success", "Export tag saved", tag.trim() ? `[${tag.trim()}]` : "Auto-generated");
    channelCache = {};
    await fetchGroups();
    renderGroups();
  } catch (e) { toast("error", "Save failed", e.message); }
};


/* ═══════════════════════════════════════════════════════════════════════
   SUB-GROUPS (PARENT GROUP)
   ═══════════════════════════════════════════════════════════════════════ */
window.setGroupParent = (gid) => {
  const current = groupsData.find(g => g.id === gid);
  const opts = groupsData.filter(g => g.id !== gid && !g.parent_id);
  const optsHtml = `<option value="">— None (top level) —</option>` +
    opts.map(g => `<option value="${g.id}"${g.id === current?.parent_id ? " selected" : ""}>${esc(g.name)}</option>`).join("");
  return new Promise(resolve => {
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    overlay.innerHTML = `
      <div class="dialog">
        <div class="dialog-title">Set Parent Group</div>
        <div class="dialog-msg" style="margin-bottom:12px">Choose a parent, or clear to make top-level.</div>
        <select id="_pg-sel" style="width:100%;padding:8px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text);margin-bottom:16px">${optsHtml}</select>
        <div class="dialog-actions">
          <button class="btn" id="_pg-cancel">Cancel</button>
          <button class="btn btn-primary" id="_pg-ok">Set Parent</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector("#_pg-cancel").onclick = () => { overlay.remove(); resolve(); };
    overlay.querySelector("#_pg-ok").onclick = async () => {
      const parentId = overlay.querySelector("#_pg-sel").value;
      overlay.remove();
      try {
        await api(`/api/groups/${gid}`, { method: "PATCH", body: JSON.stringify({ parent_id: parentId }) });
        toast("success", parentId ? "Parent group set" : "Group set to top level");
        channelCache = {};
        await fetchGroups();
        renderGroups();
      } catch (e) { toast("error", "Save failed", e.message); }
      resolve();
    };
    overlay.addEventListener("click", e => { if (e.target === overlay) { overlay.remove(); resolve(); } });
  });
};


/* ═══════════════════════════════════════════════════════════════════════
   CHANNEL HEALTH CHECK MODAL
   ═══════════════════════════════════════════════════════════════════════ */
let _healthGid = null;

window.openHealthModal = async (gid, gname) => {
  _healthGid = gid;
  const modal = $("#health-modal");
  $("#health-modal-title").textContent = `Health Check — ${gname}`;
  $("#health-modal-status").textContent = "Load the group first to see channels, then click Run Check.";
  $("#health-modal-body").innerHTML = "";
  modal.hidden = false;
  // Pre-load existing health data
  await refreshHealthDisplay(gid);
};
document.getElementById("health-modal-close")?.addEventListener("click", () => {
  $("#health-modal").hidden = true;
});
$("#health-modal")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.hidden = true;
});
document.getElementById("health-modal-run")?.addEventListener("click", async () => {
  if (!_healthGid) return;
  const btn = $("#health-modal-run");
  btnLoad(btn, "Checking...");
  const status = $("#health-modal-status");
  status.textContent = "Running HEAD checks...";
  try {
    // Get channels from cache or fetch
    let channels = channelCache[_healthGid]?.channels;
    if (!channels) {
      const d = await api(`/api/groups/${_healthGid}/channels?limit=200`);
      channels = d.channels || [];
    }
    const cids = channels.slice(0, 100).map(c => c.id);
    status.textContent = `Checking ${cids.length} channels...`;
    await api("/api/channels/health-check", { method: "POST", body: JSON.stringify({ channel_ids: cids, limit: 100 }) });
    await refreshHealthDisplay(_healthGid);
    status.textContent = `Checked ${cids.length} channels.`;
  } catch (e) { status.textContent = `Error: ${e.message}`; }
  finally { btnDone(btn); }
});
document.getElementById("health-modal-clear")?.addEventListener("click", async () => {
  try {
    await api("/api/channels/health", { method: "DELETE" });
    await refreshHealthDisplay(_healthGid);
    $("#health-modal-status").textContent = "Health data cleared.";
  } catch (e) { toast("error", "Clear failed", e.message); }
});

async function refreshHealthDisplay(gid) {
  const body = $("#health-modal-body");
  if (!gid) return;
  try {
    // Fetch channels and health data in parallel
    const [chanData, health] = await Promise.all([
      api(`/api/groups/${gid}/channels?limit=200`),
      api("/api/channels/health"),
    ]);
    const channels = chanData.channels || [];
    if (!channels.length) {
      body.innerHTML = `<p class="text-muted">No channels in this group.</p>`;
      return;
    }
    const hasAny = channels.some(c => health[c.id]);
    if (!hasAny) {
      body.innerHTML = `<p class="text-muted">No health data yet — click Run Check.</p>`;
      return;
    }
    const rows = channels.map(ch => {
      const h = health[ch.id];
      if (!h) return `<tr><td>${esc(ch.name)}</td><td colspan="3" class="text-muted" style="font-size:12px">Not checked</td></tr>`;
      const code = h.status_code;
      const ok = code && code < 400;
      const badge = code ? `<span class="health-badge health-${ok ? "ok" : "err"}">${code}</span>` :
        `<span class="health-badge health-err">${esc(h.error || "Error")}</span>`;
      const latency = h.latency_ms != null ? `${h.latency_ms}ms` : "—";
      const checked = h.checked_at ? new Date(h.checked_at).toLocaleTimeString() : "—";
      return `<tr><td>${esc(ch.name)}</td><td>${badge}</td><td class="cell-dim">${latency}</td><td class="cell-dim">${checked}</td></tr>`;
    }).join("");
    body.innerHTML = `<div class="table-wrap"><table class="data-table" style="font-size:12px">
      <thead><tr><th>Channel</th><th>Status</th><th>Latency</th><th>Checked</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  } catch (e) { body.innerHTML = `<p class="text-muted">Failed to load health data.</p>`; }
}


/* ═══════════════════════════════════════════════════════════════════════
   LOGO MANAGEMENT
   ═══════════════════════════════════════════════════════════════════════ */
window.editChannelLogo = async (cid) => {
  const ch = await api(`/api/channels/${cid}`).catch(() => null);
  if (!ch) return;
  const url = prompt("Logo URL (leave blank to clear):", ch.logo || "");
  if (url === null) return;
  try {
    await api(`/api/channels/${cid}`, { method: "PATCH", body: JSON.stringify({ logo: url.trim() }) });
    toast("success", "Logo updated");
    // Refresh the group body
    const gid = ch.group_id;
    if (gid) { await loadGroupChannels(gid, 0); const g = groupsData.find(x => x.id === gid); if (g) { const card = document.querySelector(`.group-card[data-gid="${gid}"]`); if (card) { const body = card.querySelector(".group-body"); if (body && body.parentNode) { const wrap = document.createElement("div"); wrap.innerHTML = renderGroupBody(g); const nextBody = wrap.firstElementChild; if (nextBody) body.replaceWith(nextBody); } const pCard = card.closest(".group-card"); if (pCard) wireGroupBodyEvents(pCard, g); } } }
  } catch (e) { toast("error", "Update failed", e.message); }
};

window.bulkLogoGroup = async (gid, action) => {
  if (!action) return;
  const sel = Array.from(document.querySelectorAll(`.ch-sel[data-gid="${gid}"]:checked`)).map(c => c.dataset.cid);
  if (!sel.length) { toast("info", "No channels selected"); return; }
  try {
    await api("/api/channels/bulk-logo", { method: "POST", body: JSON.stringify({ channel_ids: sel, action }) });
    toast("success", action === "clear" ? `Cleared ${sel.length} logos` : `Updated ${sel.length} logos`);
    await loadGroupChannels(gid, 0);
    const g = groupsData.find(x => x.id === gid);
    if (g) { const card = document.querySelector(`.group-card[data-gid="${gid}"]`); if (card) { const body = card.querySelector(".group-body"); if (body && body.parentNode) { const wrap = document.createElement("div"); wrap.innerHTML = renderGroupBody(g); const nextBody = wrap.firstElementChild; if (nextBody) body.replaceWith(nextBody); } wireGroupBodyEvents(card, g); } }
  } catch (e) { toast("error", "Logo bulk action failed", e.message); }
};


/* ═══════════════════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════════════════ */

const COMMANDS = [
  ["Dashboard", "Overview and system health", () => navigate("dashboard")],
  ["Sources", "Manage IPTV providers", () => navigate("sources")],
  ["Channel Editor", "Groups, channels, rules, and ordering", () => navigate("editor")],
  ["Favorites", "Open favorite channels", () => navigate("favorites")],
  ["EPG", "Guide sources and matching", () => navigate("epg")],
  ["TV Guide", "Browse the current schedule", () => navigate("guide")],
  ["Export", "Playlist and XMLTV links", () => navigate("export")],
  ["Settings", "Refresh, integrations, diagnostics", () => navigate("settings")],
  ["Refresh current page", "Reload current data", () => navigate(currentPage, true)],
  ["Sync player order", "Align provider numbers to group order", () => { navigate("editor"); setTimeout(() => $("#sync-player-order-btn")?.click(), 50); }],
];
let _commandMatches = COMMANDS;
let _commandIndex = 0;

function renderCommands(query = "") {
  const q = query.trim().toLowerCase();
  _commandMatches = COMMANDS.filter(([name, detail]) => `${name} ${detail}`.toLowerCase().includes(q));
  _commandIndex = Math.min(_commandIndex, Math.max(0, _commandMatches.length - 1));
  $("#command-results").innerHTML = _commandMatches.length
    ? _commandMatches.map(([name, detail], i) => `<button class="command-item${i === _commandIndex ? " selected" : ""}" data-command-index="${i}"><span>${esc(name)}</span><small>${esc(detail)}</small></button>`).join("")
    : '<div class="card-empty"><div class="card-empty-text">No matching actions</div></div>';
}

function openCommandPalette() {
  $("#command-palette").hidden = false;
  $("#command-search").value = "";
  _commandIndex = 0;
  renderCommands();
  requestAnimationFrame(() => $("#command-search").focus());
}

function closeCommandPalette() { $("#command-palette").hidden = true; }
function runCommand(index = _commandIndex) {
  const command = _commandMatches[index];
  if (!command) return;
  closeCommandPalette();
  command[2]();
}

$("#command-palette-btn")?.addEventListener("click", openCommandPalette);
$("#command-palette")?.addEventListener("click", e => { if (e.target === e.currentTarget) closeCommandPalette(); });
$("#command-search")?.addEventListener("input", e => { _commandIndex = 0; renderCommands(e.target.value); });
$("#command-results")?.addEventListener("click", e => {
  const item = e.target.closest(".command-item");
  if (item) runCommand(Number(item.dataset.commandIndex));
});
$("#group-view-filter").addEventListener("change", (e) => {
  groupViewMode = e.target.value;
  localStorage.setItem("m3uBoss.groupView", groupViewMode);
  renderGroups();
});
$("#group-category-filter").addEventListener("change", (e) => {
  groupCategoryFilter = e.target.value;
  localStorage.setItem("m3uBoss.groupCategory", groupCategoryFilter);
  renderGroups();
});

const compactSaved = localStorage.getItem("m3u.compact") === "1";
document.body.classList.toggle("compact", compactSaved);
$("#density-toggle")?.classList.toggle("active", compactSaved);
$("#density-toggle")?.addEventListener("click", () => {
  const compact = document.body.classList.toggle("compact");
  localStorage.setItem("m3u.compact", compact ? "1" : "0");
  $("#density-toggle").classList.toggle("active", compact);
});

document.addEventListener("keydown", e => {
  const paletteOpen = !$("#command-palette").hidden;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    paletteOpen ? closeCommandPalette() : openCommandPalette();
    return;
  }
  if (paletteOpen) {
    if (e.key === "Escape") { e.preventDefault(); closeCommandPalette(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); _commandIndex = Math.min(_commandIndex + 1, _commandMatches.length - 1); renderCommands($("#command-search").value); }
    else if (e.key === "ArrowUp") { e.preventDefault(); _commandIndex = Math.max(0, _commandIndex - 1); renderCommands($("#command-search").value); }
    else if (e.key === "Enter") { e.preventDefault(); runCommand(); }
    return;
  }
  if (e.key === "/" && currentPage === "editor" && !e.target.matches("input,textarea,select")) {
    e.preventDefault();
    $("#editor-search")?.focus();
  }
});

navigate(pageLoaders[INITIAL_PAGE] ? INITIAL_PAGE : "dashboard");

// Global: close group menus on outside click (registered once)
document.addEventListener("click", (e) => {
  if (!e.target.closest(".group-menu-wrap")) {
    $$(".group-menu.open").forEach(m => m.classList.remove("open"));
  }
});
