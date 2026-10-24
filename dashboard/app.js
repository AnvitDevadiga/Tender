/**
 * Tender dashboard — zero-build ES module.
 *
 * One module renders the whole shell, so the local dashboard (/dashboard/) and
 * the static replay (demo/replay/) share identical markup. The replay installs
 * a fetch shim (replay.js) before this module loads; nothing else differs.
 *
 *   state   → a plain object, the single source of truth
 *   api     → fetch client for the local Tender API (or the replay shim)
 *   render* → functions that rebuild one region of the DOM from state
 *   events  → delegated listeners and keyboard shortcuts
 *
 * Strict CSP: no inline handlers or styles. All dynamic text is escaped.
 */

import { esc, parseDiff, renderDiff } from "./lib.js";

/* ============================================================== constants */

const STATUSES = ["queued", "running", "awaiting_approval", "passed", "failed", "cancelled"];
const STATUS_LABELS = {
  queued: "Queued",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  passed: "Passed",
  failed: "Failed",
  cancelled: "Cancelled",
  accepted: "Accepted",
  rejected: "Rejected",
  error: "Error",
  inconclusive: "Inconclusive",
  pending_approval: "Pending approval",
  not_run: "Not run",
  not_configured: "Not configured",
  unknown: "Unknown",
  ok: "OK",
  violated: "Violated",
  not_checked: "Not checked",
  setup_failed: "Setup failed",
  sandbox_unavailable: "Sandbox unavailable",
  not_used: "Not used",
};
const STATUS_GLYPHS = {
  passed: "✓",
  accepted: "✓",
  ok: "✓",
  failed: "✕",
  rejected: "✕",
  violated: "✕",
  error: "!",
  setup_failed: "!",
  sandbox_unavailable: "!",
};
const SORT_OPTIONS = [
  ["newest", "Newest first"],
  ["oldest", "Oldest first"],
  ["latency", "Slowest first"],
  ["tokens", "Most tokens"],
  ["cost", "Highest reported cost"],
];
const POLL_FAST_MS = 4000;
const POLL_SLOW_MS = 15000;
const MAX_LIMIT = 1000;
const PAGE_SIZE = 100;
const MAX_COMPARE = 4;
const REPO_URL = "https://github.com/AnvitDevadiga/Tender";

const ROOT = /** @type {HTMLElement} */ (document.getElementById("root"));
const REPLAY_MODE =
  Boolean(/** @type {any} */ (window).TENDER_REPLAY) || ROOT?.dataset.mode === "replay";
const HOME_HREF = ROOT?.dataset.home || null;
const STORE_KEYS = { theme: "tender.theme", filters: "tender.filters" };

/* ================================================================== state */

const state = {
  view: "runs",
  /** @type {any[]} */ runs: [],
  total: 0,
  /** @type {any|null} */ metrics: null,
  filters: { q: "", status: "all", model: "all", benchmark: "all", category: "all", sort: "newest" },
  /** @type {Set<string>} */ compare: new Set(),
  /** @type {string|null} */ selectedId: null,
  limit: PAGE_SIZE,
  loading: false,
  loadedOnce: false,
  apiOk: true,
  /** @type {string|null} */ apiError: null,
  runsVersion: 0,
  filtersOpen: false,
  inspector: {
    /** @type {"run"|"report"|null} */ kind: null,
    /** @type {string|null} */ id: null,
    tab: "overview",
    /** @type {any} */ data: null,
    /** @type {any[]|null} */ events: null,
    /** @type {any} */ artifact: null,
    loading: false,
    /** @type {string|null} */ error: null,
  },
  /** @type {any[]} */ reports: [],
  reportsLoaded: false,
  /** @type {string|null} */ reportsError: null,
  playback: { index: -1, timer: 0 },
  palette: { index: 0, /** @type {any[]} */ items: [] },
  /** @type {{id: number, message: string, tone: string}[]} */ toasts: [],
};

let toastSeq = 0;
let derivedRows = { key: "", rows: /** @type {any[]} */ ([]) };
let pollTimer = 0;
let refreshSeq = 0;
/** @type {{ element: HTMLElement, returnFocus: Element|null, returnKey: string|null, onClose?: () => void }[]} */
const overlays = [];

/* ================================================================ helpers */

/** @param {string} selector @param {ParentNode} [scope] */
function $(selector, scope = document) {
  return /** @type {HTMLElement|null} */ (scope.querySelector(selector));
}

const numbers = new Intl.NumberFormat("en-US");

/** @param {number|null|undefined} value */
function fmtInt(value) {
  return value === null || value === undefined || Number.isNaN(value) ? "—" : numbers.format(value);
}

/** @param {number|null|undefined} value */
function fmtTokens(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 100000) return `${Math.round(value / 1000)}k`;
  if (value >= 10000) return `${(value / 1000).toFixed(1)}k`;
  return numbers.format(value);
}

/** @param {number|null|undefined} value */
function fmtMs(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 60000) return `${(value / 60000).toFixed(1)} min`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} s`;
  return `${Math.round(value)} ms`;
}

/** @param {number|null|undefined} value */
function fmtCost(value) {
  if (value === null || value === undefined) return "unknown";
  return `$${value < 0.01 ? value.toFixed(5) : value.toFixed(4)}`;
}

/** @param {number|null|undefined} value */
function fmtPct(value) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

/** @param {string|null|undefined} iso */
function fmtDate(iso) {
  if (!iso) return "unknown";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** @param {string|null|undefined} iso */
function relTime(iso) {
  const then = new Date(iso || "").getTime();
  if (Number.isNaN(then)) return "unknown";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 172800) return "yesterday";
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** @param {string|null|undefined} value */
function short(value, length = 8) {
  return value ? String(value).slice(0, length) : "—";
}

/** @param {any} run */
function runTokens(run) {
  return (run.input_tokens || 0) + (run.output_tokens || 0);
}

/** @param {string} status @param {string} [text] @param {string} [extra] */
function statusMark(status, text, extra = "") {
  const key = String(status || "unknown");
  const glyph = STATUS_GLYPHS[key] || "";
  return `<span class="status ${esc(key)} ${extra}"><span class="mark" aria-hidden="true">${glyph}</span><span class="text">${esc(
    text || STATUS_LABELS[key] || key,
  )}</span></span>`;
}

const LABEL_TEXT = { recorded: "Recorded", derived: "Derived", unknown: "Unknown", verified: "Verified" };

/** @param {"recorded"|"derived"|"unknown"|"verified"} kind @param {string} [title] */
function label(kind, title = "") {
  return `<span class="label ${kind}"${title ? ` title="${esc(title)}"` : ""}>${LABEL_TEXT[kind] || kind}</span>`;
}

/** @param {any} run */
function provenanceOf(run) {
  return run?.provenance || null;
}

/** @param {unknown} value */
function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** @param {string} value */
function storageGet(value) {
  try {
    return window.localStorage.getItem(value);
  } catch {
    return null;
  }
}

/** @param {string} key @param {string} value */
function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage can be unavailable (private mode); preferences are optional */
  }
}

function prefersReducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

/* ==================================================================== api */

class ApiError extends Error {
  /** @param {number} status @param {string} detail */
  constructor(status, detail) {
    super(detail || `request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

const api = {
  /** @param {string} path @param {RequestInit} [options] */
  async request(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new ApiError(0, "API unreachable. Start it with: uvicorn tender.api:app");
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const payload = await response.json();
        if (payload && typeof payload.detail === "string") detail = payload.detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(response.status, detail);
    }
    return response.status === 204 ? null : response.json();
  },

  /** @param {{limit?: number, signal?: AbortSignal}} params */
  async listRuns(params = {}) {
    const query = new URLSearchParams({ summary: "true", limit: String(params.limit || PAGE_SIZE) });
    let response;
    try {
      response = await fetch(`/runs?${query}`, { signal: params.signal });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError(0, "API unreachable. Start it with: uvicorn tender.api:app");
    }
    if (!response.ok) throw new ApiError(response.status, response.statusText);
    const runs = await response.json();
    const total = Number(response.headers.get("X-Total-Count"));
    return { runs, total: Number.isFinite(total) ? total : runs.length };
  },
  /** @param {string} id */
  getRun(id) {
    return this.request(`/runs/${encodeURIComponent(id)}`);
  },
  /** @param {string} id */
  getEvents(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/events?limit=20000`);
  },
  /** @param {string} id */
  getArtifact(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/artifact`);
  },
  /** @param {string} id */
  cancelRun(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  },
  getMetrics() {
    return this.request("/metrics");
  },
  listReports() {
    return this.request("/reports");
  },
  /** @param {string} id */
  getReport(id) {
    return this.request(`/reports/${encodeURIComponent(id)}`);
  },
};

/** @param {unknown} error */
function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Not authorized: this API requires TENDER_API_TOKEN.";
    if (error.status === 409) return error.detail || "The run already changed state.";
    if (error.status === 429) return error.detail || "Rate limited; retry shortly.";
    if (error.status === 0) return error.detail;
    return `${error.status}: ${error.detail}`;
  }
  return error instanceof Error ? error.message : String(error);
}

/* ================================================================= shell */

function renderShell() {
  const home = HOME_HREF ? `<a class="btn ghost small hide-xs" href="${esc(HOME_HREF)}">Overview</a>` : "";
  ROOT.innerHTML = `
    <a class="skip-link" href="#main">Skip to content</a>
    <div class="app" id="app">
      <header class="topbar">
        <div class="topbar-inner">
          <a class="wordmark" href="#/runs" aria-label="Tender runs">tender<span>.</span><small>${
            REPLAY_MODE ? "replay" : "local"
          }</small></a>
          <nav class="nav" aria-label="Views">
            <a href="#/runs" data-view="runs">Runs</a>
            <a href="#/reports" data-view="reports">Reports</a>
          </nav>
          <span class="spacer"></span>
          <div class="top-actions">
            <span class="connection" id="connection" role="status"><span class="dot" aria-hidden="true"></span><span class="label-text">connecting</span></span>
            ${home}
            <button class="btn ghost small hide-xs" type="button" data-action="open-palette" aria-label="Command palette" title="Command palette (⌘K)"><kbd>⌘K</kbd></button>
            <button class="btn ghost small" type="button" data-action="toggle-theme" id="theme-btn" aria-label="Switch color theme" title="Theme (t)">Dark</button>
            <button class="btn ghost small" type="button" data-action="help" aria-label="Help and shortcuts">Help</button>
          </div>
        </div>
      </header>
      <div id="notices"></div>
      <main class="main" id="main" tabindex="-1"></main>
      <footer class="footer">
        <div class="footer-inner">
          <span>Tender ${REPLAY_MODE ? "recorded replay · read-only" : "local dashboard"}</span>
          <span>Status reflects recorded evidence only. Unknown values stay unknown.</span>
          <span class="grow"></span>
          <a href="${REPO_URL}" target="_blank" rel="noreferrer">Source</a>
        </div>
      </footer>
    </div>
    <div class="scrim" id="scrim" hidden></div>
    <section class="inspector" id="inspector" role="dialog" aria-modal="true" aria-labelledby="inspector-title" hidden></section>
    <section class="dialog" id="help-dialog" role="dialog" aria-modal="true" aria-labelledby="help-title" hidden></section>
    <section class="dialog" id="setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title" hidden></section>
    <section class="dialog wide" id="compare-dialog" role="dialog" aria-modal="true" aria-labelledby="compare-title" hidden></section>
    <section class="dialog palette" id="palette" role="dialog" aria-modal="true" aria-label="Command palette" hidden>
      <input id="palette-input" type="text" placeholder="Search runs and actions" aria-label="Search runs and actions" aria-controls="palette-list" autocomplete="off" spellcheck="false">
      <div class="palette-list" id="palette-list" role="listbox" aria-label="Results"></div>
      <div class="palette-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> select</span><span><kbd>esc</kbd> close</span></div>
    </section>
    <div class="toasts" id="toasts" aria-live="polite"></div>`;
}

function renderChrome() {
  for (const link of ROOT.querySelectorAll(".nav [data-view]")) {
    if (link.getAttribute("data-view") === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const connection = $("#connection");
  if (connection) {
    connection.className = `connection ${state.apiOk ? "ok" : ""}`;
    const text = REPLAY_MODE ? "static snapshot" : state.apiOk ? "API connected" : "API offline";
    connection.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="label-text">${esc(text)}</span>`;
    connection.title = text;
  }
  const notices = $("#notices");
  if (!notices) return;
  const parts = [];
  if (REPLAY_MODE) {
    parts.push(`<div class="notice"><div class="notice-inner">
      <strong>Recorded replay.</strong>
      <span>Runs, events, and reports below were recorded by Tender and are served from a static snapshot. Nothing executes on this page; actions that would change state are refused.</span>
      <span>${label("recorded")} ${label("derived")} ${label("unknown")} ${label("verified")} mark where each value comes from.</span>
    </div></div>`);
  }
  if (!state.apiOk && state.apiError) {
    parts.push(`<div class="notice alert" role="alert"><div class="notice-inner">
      <strong>Cannot reach the Tender API.</strong><span>${esc(state.apiError)}</span>
      <button class="btn small" type="button" data-action="refresh">Retry</button>
    </div></div>`);
  }
  notices.innerHTML = parts.join("");
}

/* ================================================================ routing */

function parseRoute() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = hash.split("?");
  const segments = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  return { view: segments[0] === "reports" ? "reports" : "runs", id: segments[1] || null, params };
}

let renderedView = "";

function applyRoute() {
  const route = parseRoute();
  const viewChanged = route.view !== renderedView;
  state.view = route.view;
  let filtersChanged = false;
  for (const key of /** @type {const} */ (["q", "status", "model", "benchmark", "category", "sort"])) {
    const value = route.params.get(key);
    if (value !== null && value !== state.filters[key]) {
      state.filters[key] = value;
      filtersChanged = true;
    }
  }
  renderChrome();
  // Opening or closing the inspector must not rebuild the list behind it, or the
  // element that should receive focus back would be destroyed.
  if (viewChanged || filtersChanged) {
    renderMain();
    if (renderedView) $("#main")?.focus({ preventScroll: true });
    renderedView = route.view;
  }
  if (route.view === "reports" && !state.reportsLoaded) loadReports();
  if (route.id) {
    const kind = route.view === "reports" ? "report" : "run";
    if (state.inspector.id !== route.id || state.inspector.kind !== kind) openInspector(kind, route.id);
  } else if (state.inspector.id) {
    closeInspectorNow();
  }
}

/** @type {Element|null} */
let pendingReturnFocus = null;

/** @param {string} hash */
function navigate(hash) {
  pendingReturnFocus = document.activeElement;
  if (window.location.hash !== hash) window.location.hash = hash;
  else applyRoute();
}

function filterQuery() {
  const params = new URLSearchParams();
  const defaults = { q: "", status: "all", model: "all", benchmark: "all", category: "all", sort: "newest" };
  for (const [key, value] of Object.entries(state.filters)) {
    if (value !== /** @type {any} */ (defaults)[key]) params.set(key, value);
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function syncFilters() {
  storageSet(STORE_KEYS.filters, JSON.stringify(state.filters));
  if (state.view !== "runs" || state.inspector.id) return;
  const target = `#/runs${filterQuery()}`;
  if (window.location.hash !== target) history.replaceState(null, "", target);
}

/* ============================================================== runs view */

const OUTCOME_LABELS = {
  passed: "Passed",
  awaiting: "Queued, running, or awaiting",
  test: "Test failure",
  integrity_violation: "Integrity violation",
  policy_rejection: "Policy rejection",
  provider_error: "Provider error",
  environment_setup: "Environment error",
  timeout: "Timeout",
  planning: "Agent stopped before verification",
  patch: "Agent stopped before verification (patch)",
  tooling: "Tooling error",
  cancelled: "Cancelled",
  other: "Other failure",
};

/** One outcome bucket per run, from recorded status and failure category. */
function outcomeKey(run) {
  if (run.status === "passed") return "passed";
  if (run.status === "cancelled") return "cancelled";
  if (run.status !== "failed") return "awaiting";
  return run.failure_category in OUTCOME_LABELS ? run.failure_category : "other";
}

function renderBreakdown() {
  const m = state.metrics;
  if (!m || !m.runs) return "";
  const rows = [["passed", m.passed]];
  for (const [category, count] of Object.entries(m.failure_categories || {})) {
    rows.push([category in OUTCOME_LABELS ? category : "other", count]);
  }
  const unfinished = (m.queued_runs || 0) + (m.running_runs || 0) + (m.awaiting_approval || 0);
  if (unfinished) rows.push(["awaiting", unfinished]);
  if (m.cancelled_runs) rows.push(["cancelled", m.cancelled_runs]);
  const max = Math.max(1, ...rows.map(([, count]) => Number(count)));
  /** @param {number} value @param {number} total */
  const bar = (value, total) =>
    `<svg class="mini-bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true"><rect x="0" y="0" width="100" height="8" class="track"></rect><rect x="0" y="0" width="${Math.max(1.5, (100 * value) / total)}" height="8" class="fill"></rect></svg>`;
  const outcomes = rows
    .filter(([, count]) => Number(count) > 0)
    .map(
      ([key, count]) => `<button type="button" class="bar-row ${state.filters.category === key ? "active" : ""}" data-action="set-filter" data-group="category" data-value="${state.filters.category === key ? "all" : esc(key)}" aria-pressed="${state.filters.category === key}">
        <span class="name">${esc(OUTCOME_LABELS[key] || key)}</span>${bar(Number(count), max)}<span class="count">${fmtInt(Number(count))}</span></button>`,
    )
    .join("");
  const models = Object.entries(m.by_model || {})
    .map(
      ([model, row]) => `<tr><td><button type="button" class="link-button mono" data-action="set-filter" data-group="model" data-value="${
        state.filters.model === model ? "all" : esc(model)
      }" aria-pressed="${state.filters.model === model}">${esc(model)}</button></td>
        <td class="num">${fmtInt(row.runs)}</td><td class="num">${fmtInt(row.passed)}</td>
        <td class="num">${fmtPct(row.pass_rate)}</td><td class="num">${fmtInt(row.provider_failures ?? 0)}</td></tr>`,
    )
    .join("");
  return `<section class="breakdown" aria-label="Outcomes and models">
      <div class="breakdown-panel">
        <h2>Outcomes ${label("derived", "Counted from recorded run status and failure category")}</h2>
        <p class="hint">Select a row to show its runs.</p>
        <div class="bar-list">${outcomes}</div>
      </div>
      <div class="breakdown-panel">
        <h2>Models ${label("derived", "Aggregated from recorded runs")}</h2>
        <p class="hint">Model as recorded (provider/model). Select one to filter.</p>
        <div class="table-wrap"><table class="kv-table models-table"><thead><tr><th>Model</th><th class="num">Runs</th><th class="num">Passed</th><th class="num">Pass rate</th><th class="num">Provider failures</th></tr></thead><tbody>${models}</tbody></table></div>
      </div>
    </section>`;
}

function visibleRuns() {
  const key = `${state.runsVersion}|${JSON.stringify(state.filters)}`;
  if (derivedRows.key === key) return derivedRows.rows;
  const filters = state.filters;
  const query = filters.q.trim().toLowerCase();
  let rows = state.runs.filter((run) => {
    if (filters.status !== "all" && run.status !== filters.status) return false;
    if (filters.model !== "all" && run.config?.model !== filters.model) return false;
    if (filters.benchmark !== "all" && run.benchmark_id !== filters.benchmark) return false;
    if (filters.category !== "all" && outcomeKey(run) !== filters.category) return false;
    if (!query) return true;
    return [
      run.issue?.title,
      run.issue?.repository,
      run.config?.model,
      run.benchmark_id,
      run.status,
      run.failure_category,
      run.outcome?.overall,
      run.id,
    ]
      .join(" ")
      .toLowerCase()
      .includes(query);
  });
  /** @type {Record<string, (a: any, b: any) => number>} */
  const sorters = {
    newest: (a, b) => String(b.created_at).localeCompare(String(a.created_at)),
    oldest: (a, b) => String(a.created_at).localeCompare(String(b.created_at)),
    latency: (a, b) => (b.latency_ms ?? -1) - (a.latency_ms ?? -1),
    tokens: (a, b) => runTokens(b) - runTokens(a),
    cost: (a, b) => (b.cost_usd ?? -1) - (a.cost_usd ?? -1),
  };
  rows = [...rows].sort(sorters[filters.sort] || sorters.newest);
  derivedRows = { key, rows };
  return rows;
}

function statusCounts() {
  /** @type {Record<string, number>} */
  const counts = { all: state.runs.length };
  for (const status of STATUSES) counts[status] = 0;
  for (const run of state.runs) counts[run.status] = (counts[run.status] || 0) + 1;
  return counts;
}

function renderMain() {
  const main = $("#main");
  if (!main) return;
  if (state.view === "reports") {
    main.innerHTML = renderReportsView();
    return;
  }
  const searchValue = state.filters.q;
  const focused = document.activeElement?.id;
  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Runs</h1>
        <p>${
          REPLAY_MODE
            ? "Recorded evaluations from the published experiments."
            : "Evaluations recorded by this Tender instance."
        }</p>
      </div>
      <div class="actions-row">
        <button class="btn small" type="button" data-action="setup">Run locally</button>
        <button class="btn small" type="button" data-action="refresh">Refresh</button>
        <button class="btn small" type="button" data-action="export-json" title="Export the current view as JSON">Export JSON</button>
        <button class="btn small" type="button" data-action="export-csv" title="Export the current view as CSV">Export CSV</button>
      </div>
    </div>
    <dl class="summary" id="summary" aria-label="Workspace metrics">${renderSummary()}</dl>
    <div id="breakdown">${renderBreakdown()}</div>
    <div class="toolbar" role="search">
      <div class="search">
        <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2.2"></circle><line x1="16.5" y1="16.5" x2="21" y2="21" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"></line></svg>
        <input class="input" id="search" type="search" placeholder="Search title, repository, model, status" aria-label="Search runs" autocomplete="off" value="${esc(searchValue)}">
        <kbd aria-hidden="true">/</kbd>
      </div>
      <button class="btn small filters-toggle" type="button" data-action="toggle-filters" aria-expanded="${state.filtersOpen}" aria-controls="filters">Filters${activeFilterCount() ? ` · ${activeFilterCount()}` : ""}</button>
      <div class="filters ${state.filtersOpen ? "open" : ""}" id="filters">${renderFilters()}</div>
      <span class="grow"></span>
      <span class="view-count" id="view-count"></span>
    </div>
    <div id="compare-bar"></div>
    <div id="run-list" aria-live="polite"></div>`;
  renderList();
  if (focused === "search") {
    const search = /** @type {HTMLInputElement|null} */ ($("#search"));
    search?.focus();
    search?.setSelectionRange(search.value.length, search.value.length);
  }
}

function activeFilterCount() {
  const { status, model, benchmark, category, sort } = state.filters;
  return [status !== "all", model !== "all", benchmark !== "all", category !== "all", sort !== "newest"].filter(Boolean).length;
}

function renderFilters() {
  const counts = statusCounts();
  const statusButtons = ["all", ...STATUSES]
    .filter((value) => value === "all" || counts[value] > 0 || state.filters.status === value)
    .map(
      (value) =>
        `<button type="button" data-action="set-filter" data-group="status" data-value="${value}" aria-pressed="${
          state.filters.status === value
        }">${value === "all" ? "All" : STATUS_LABELS[value]}<span class="count">${counts[value] ?? 0}</span></button>`,
    )
    .join("");
  const models = [...new Set(state.runs.map((run) => run.config?.model).filter(Boolean))].sort();
  const benchmarks = [...new Set(state.runs.map((run) => run.benchmark_id).filter(Boolean))].sort();
  /** @param {string} group @param {string} current @param {string[]} values @param {string} allLabel */
  const select = (group, current, values, allLabel) => `
    <select class="select" data-action="select-filter" data-group="${group}" aria-label="Filter by ${group}">
      <option value="all">${allLabel}</option>
      ${values.map((value) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(value)}</option>`).join("")}
    </select>`;
  return `
    <div class="segmented" role="group" aria-label="Status">${statusButtons}</div>
    ${select("model", state.filters.model, models, "All models")}
    ${select("benchmark", state.filters.benchmark, benchmarks, "All benchmarks")}
    <select class="select" data-action="select-filter" data-group="category" aria-label="Filter by outcome">
      <option value="all">All outcomes</option>
      ${Object.entries(OUTCOME_LABELS)
        .map(([value, text]) => `<option value="${value}" ${value === state.filters.category ? "selected" : ""}>${esc(text)}</option>`)
        .join("")}
    </select>
    <select class="select" data-action="select-filter" data-group="sort" aria-label="Sort runs">
      ${SORT_OPTIONS.map(([value, text]) => `<option value="${value}" ${value === state.filters.sort ? "selected" : ""}>${text}</option>`).join("")}
    </select>
    ${activeFilterCount() || state.filters.q ? `<button class="btn ghost small" type="button" data-action="clear-filters">Clear</button>` : ""}`;
}

function renderSummary() {
  const m = state.metrics;
  if (!m) {
    return ["Runs", "Passed", "Failed", "Provider failures", "Latency", "Tokens"]
      .map((name) => `<div><dt>${name}</dt><dd>—</dd><span class="foot">&nbsp;</span></div>`)
      .join("");
  }
  const failed = Math.max(0, (m.completed_runs || 0) - (m.passed || 0));
  const coverage = m.coverage
    ? m.coverage.truncated
      ? `${fmtInt(m.coverage.runs_counted)} of ${fmtInt(m.coverage.total_runs)} counted`
      : "all runs counted"
    : "coverage not reported";
  const cost =
    m.cost_observations > 0
      ? `${fmtCost(m.total_cost_usd)} reported by ${fmtInt(m.cost_observations)} runs`
      : "no provider-reported cost";
  const cells = [
    ["Runs", fmtInt(m.runs), coverage],
    ["Passed", fmtInt(m.passed), `${fmtPct(m.pass_rate)} of ${fmtInt(m.completed_runs)} finished`],
    ["Failed", fmtInt(failed), `${fmtInt(m.queued_runs + m.running_runs)} queued or running`],
    [
      "Provider failures",
      fmtInt(m.provider_failures ?? 0),
      m.pass_rate_excluding_provider_failures === null || m.pass_rate_excluding_provider_failures === undefined
        ? "not coding failures"
        : `${fmtPct(m.pass_rate_excluding_provider_failures)} pass rate without them`,
    ],
    ["Latency p50", fmtMs(m.latency_p50_ms), `p95 ${fmtMs(m.latency_p95_ms)}`],
    ["Tokens", fmtTokens((m.total_input_tokens || 0) + (m.total_output_tokens || 0)), cost],
  ];
  return cells
    .map(
      ([name, value, foot]) =>
        `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd><span class="foot" title="${esc(foot)}">${esc(foot)}</span></div>`,
    )
    .join("");
}

/** @param {any} run */
function verificationCell(run) {
  if (run.outcome?.overall) {
    const overall = run.outcome.overall;
    return statusMark(overall, STATUS_LABELS[overall], "");
  }
  const provenance = provenanceOf(run);
  if (provenance?.independent_verification === "verified") return label("verified", "Recorded clean-copy verification passed");
  if (provenance) return label("unknown", "Not captured by this legacy record");
  return `<span class="muted">${run.status === "passed" || run.status === "failed" ? "legacy record" : "—"}</span>`;
}

function renderList() {
  const container = $("#run-list");
  if (!container) return;
  const rows = visibleRuns();
  const countLabel = $("#view-count");
  if (countLabel) {
    countLabel.textContent = state.loadedOnce
      ? `${fmtInt(rows.length)} shown · ${fmtInt(state.total)} total`
      : "";
  }
  renderCompareBar();
  if (!state.loadedOnce && state.loading) {
    container.innerHTML = `<div class="table-wrap"><table class="runs" aria-label="Loading runs"><tbody>${Array.from(
      { length: 6 },
      () => `<tr class="skeleton-row"><td></td><td></td><td></td><td></td></tr>`,
    ).join("")}</tbody></table></div>`;
    return;
  }
  if (!state.apiOk && !state.runs.length) {
    container.innerHTML = `<div class="empty"><h2>No connection to the Tender API</h2>
      <p>${esc(state.apiError || "")}</p>
      <div class="actions"><button class="btn primary" type="button" data-action="refresh">Retry</button><button class="btn" type="button" data-action="setup">Setup commands</button></div></div>`;
    return;
  }
  if (!rows.length) {
    const filtered = state.runs.length > 0;
    container.innerHTML = `<div class="empty"><h2>${filtered ? "No runs match these filters" : "No runs recorded yet"}</h2>
      <p>${
        filtered
          ? "Clear the filters or change the search to see more runs."
          : "Run the pinned fixture benchmark or queue a run through the API. Tender only shows what it recorded."
      }</p>
      <div class="actions">${
        filtered
          ? `<button class="btn primary" type="button" data-action="clear-filters">Clear filters</button>`
          : `<button class="btn primary" type="button" data-action="setup">Setup commands</button>`
      }</div></div>`;
    return;
  }
  const body = rows
    .map((run) => {
      const title = run.issue?.title || "(untitled)";
      const commit = run.metadata?.executed_base_commit || run.issue?.base_commit || run.metadata?.base_commit;
      const picked = state.compare.has(run.id);
      const selected = state.selectedId === run.id ? "selected" : "";
      return `<tr class="${selected}" data-action="open-run" data-id="${esc(run.id)}">
        <td class="pick-cell"><label title="Select for comparison"><input type="checkbox" data-action="pick" data-id="${esc(
          run.id,
        )}" ${picked ? "checked" : ""} aria-label="Compare ${esc(title)}"></label></td>
        <td class="col-status">${statusMark(run.status)}</td>
        <td class="task"><a class="title" href="#/runs/${esc(run.id)}" title="${esc(title)}">${esc(title)}</a>
          <span class="sub mono" title="${esc(run.benchmark_id)}">${esc(run.issue?.repository || "")} · ${esc(short(commit, 10))} · ${esc(run.benchmark_id)}</span></td>
        <td class="col-model mono">${esc(run.config?.model || "—")}</td>
        <td class="col-attempts num">${fmtInt(run.attempts)}</td>
        <td class="col-duration num">${fmtMs(run.latency_ms)}</td>
        <td class="col-tokens num">${fmtTokens(runTokens(run))}</td>
        <td class="col-cost num ${run.cost_usd === null || run.cost_usd === undefined ? "muted" : ""}">${fmtCost(run.cost_usd)}</td>
        <td class="col-verify">${verificationCell(run)}</td>
        <td class="col-created muted" title="${esc(fmtDate(run.created_at))}">${esc(relTime(run.created_at))}</td>
        <td class="mobile-meta">${esc(run.config?.model || "")} · ${fmtInt(run.attempts)} attempt${
          run.attempts === 1 ? "" : "s"
        } · ${esc(relTime(run.created_at))}</td>
      </tr>`;
    })
    .join("");
  const more =
    state.runs.length < Math.min(state.total, MAX_LIMIT)
      ? `<div class="load-more"><button class="btn" type="button" data-action="load-more">Load more (${fmtInt(state.runs.length)} of ${fmtInt(
          state.total,
        )})</button></div>`
      : state.total > MAX_LIMIT
        ? `<p class="view-count load-more">The list shows the newest ${fmtInt(MAX_LIMIT)} runs; metrics above cover all ${fmtInt(state.total)}.</p>`
        : "";
  container.innerHTML = `<div class="table-wrap"><table class="runs">
    <caption class="visually-hidden">Recorded runs</caption>
    <thead><tr>
      <th class="pick-cell"><span class="visually-hidden">Compare</span></th>
      <th class="col-status">Status</th><th>Task</th><th class="col-model">Model</th>
      <th class="col-attempts num">Attempts</th><th class="col-duration num">Duration</th><th class="col-tokens num">Tokens</th>
      <th class="col-cost num">Cost</th><th class="col-verify">Verification</th><th class="col-created">Created</th>
    </tr></thead><tbody>${body}</tbody></table></div>${more}`;
}

function renderCompareBar() {
  const bar = $("#compare-bar");
  if (!bar) return;
  if (!state.compare.size) {
    bar.innerHTML = "";
    return;
  }
  bar.innerHTML = `<div class="compare-bar" role="region" aria-label="Comparison selection">
    <span>${state.compare.size} of ${MAX_COMPARE} runs selected</span><span class="grow"></span>
    <button class="btn small primary" type="button" data-action="compare-open" ${state.compare.size < 2 ? "disabled" : ""}>Compare</button>
    <button class="btn small ghost" type="button" data-action="compare-clear">Clear</button></div>`;
}

/* ============================================================ data loads */

/** @param {{ quiet?: boolean }} [_options] */
async function refresh(_options = {}) {
  const sequence = ++refreshSeq;
  state.loading = true;
  if (!state.loadedOnce) renderList();
  try {
    const [{ runs, total }, metrics] = await Promise.all([
      api.listRuns({ limit: Math.min(state.limit, MAX_LIMIT) }),
      api.getMetrics(),
    ]);
    if (sequence !== refreshSeq) return;
    state.runs = runs;
    state.total = total;
    state.metrics = metrics;
    state.apiOk = true;
    state.apiError = null;
    state.runsVersion += 1;
  } catch (error) {
    if (sequence !== refreshSeq) return;
    state.apiOk = false;
    state.apiError = describeError(error);
  } finally {
    if (sequence === refreshSeq) {
      state.loading = false;
      state.loadedOnce = true;
      renderChrome();
      if (state.view === "runs") {
        const summary = $("#summary");
        if (summary) summary.innerHTML = renderSummary();
        const breakdown = $("#breakdown");
        if (breakdown && !breakdown.contains(document.activeElement)) breakdown.innerHTML = renderBreakdown();
        const filters = $("#filters");
        if (filters && !filters.contains(document.activeElement)) filters.innerHTML = renderFilters();
        renderList();
      }
      schedulePoll();
    }
  }
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (REPLAY_MODE || document.hidden) return;
  const active = state.runs.some((run) => run.status === "queued" || run.status === "running");
  pollTimer = window.setTimeout(() => refresh({ quiet: true }), active ? POLL_FAST_MS : POLL_SLOW_MS);
}

async function loadMore() {
  state.limit = Math.min(state.limit + PAGE_SIZE * 2, MAX_LIMIT);
  await refresh();
}

async function loadReports() {
  try {
    const payload = await api.listReports();
    state.reports = Array.isArray(payload) ? payload : payload?.reports || [];
    state.reportsError = null;
  } catch (error) {
    state.reports = [];
    state.reportsError =
      error instanceof ApiError && error.status === 404
        ? "This API does not serve reports yet. Set TENDER_REPORT_ROOT to a directory of patch evaluation reports."
        : describeError(error);
  } finally {
    state.reportsLoaded = true;
    if (state.view === "reports") renderMain();
  }
}

/** @param {string} id */
async function cancelRun(id) {
  try {
    await api.cancelRun(id);
    toast("Run cancelled.");
    await refresh();
    if (state.inspector.kind === "run" && state.inspector.id === id) openInspector("run", id);
  } catch (error) {
    toast(describeError(error), "error");
  }
}

/* ============================================================== inspector */

const RUN_TABS = [
  ["overview", "Overview"],
  ["timeline", "Timeline"],
  ["patch", "Patch"],
  ["verification", "Verification"],
  ["tools", "Tool activity"],
  ["artifacts", "Artifacts"],
  ["provenance", "Provenance"],
];
const REPORT_TABS = [
  ["overview", "Summary"],
  ["checks", "Checks"],
  ["patch", "Patch"],
  ["integrity", "Integrity"],
  ["evidence", "Evidence"],
];

/** @param {"run"|"report"} kind @param {string} id */
async function openInspector(kind, id) {
  const inspector = state.inspector;
  const sameTarget = inspector.kind === kind && inspector.id === id;
  Object.assign(inspector, {
    kind,
    id,
    tab: sameTarget ? inspector.tab : "overview",
    data: sameTarget ? inspector.data : null,
    events: sameTarget ? inspector.events : null,
    artifact: sameTarget ? inspector.artifact : null,
    loading: true,
    error: null,
  });
  stopPlayback();
  state.selectedId = kind === "run" ? id : state.selectedId;
  const element = $("#inspector");
  if (element && element.hidden) {
    openOverlay(element, { onClose: () => closeInspector(), returnFocus: pendingReturnFocus });
  }
  pendingReturnFocus = null;
  renderInspector();
  try {
    if (kind === "run") {
      const [run, events, artifact] = await Promise.all([
        api.getRun(id),
        api.getEvents(id).catch(() => null),
        api.getArtifact(id).catch(() => null),
      ]);
      if (inspector.id !== id) return;
      Object.assign(inspector, { data: run, events, artifact });
    } else {
      const report = state.reports.find((item) => item.report_id === id) || (await api.getReport(id));
      if (inspector.id !== id) return;
      inspector.data = report;
    }
  } catch (error) {
    if (inspector.id === id) inspector.error = describeError(error);
  } finally {
    if (inspector.id === id) {
      inspector.loading = false;
      renderInspector();
      const focusTarget = $("#inspector-close");
      if (overlays.at(-1)?.element.id === "inspector" && !$("#inspector")?.contains(document.activeElement)) {
        focusTarget?.focus();
      }
    }
  }
}

/** Ask the router to close the inspector; the hash is the single source of truth. */
function closeInspector() {
  const kind = state.inspector.kind;
  if (!kind) return;
  navigate(kind === "report" ? "#/reports" : `#/runs${filterQuery()}`);
}

function closeInspectorNow() {
  const inspector = state.inspector;
  if (!inspector.id) return;
  Object.assign(inspector, { kind: null, id: null, data: null, events: null, artifact: null, error: null });
  stopPlayback();
  const element = $("#inspector");
  if (element && !element.hidden) closeOverlay(element);
  for (const row of document.querySelectorAll("tr[data-action='open-run']")) {
    row.classList.toggle("selected", row.getAttribute("data-id") === state.selectedId);
  }
}

function renderInspector() {
  const element = $("#inspector");
  if (!element) return;
  const inspector = state.inspector;
  const tabs = inspector.kind === "report" ? REPORT_TABS : RUN_TABS;
  const data = inspector.data;
  let title = inspector.kind === "report" ? "Patch evaluation report" : "Run";
  let meta = "";
  let actions = "";
  if (data && inspector.kind === "run") {
    title = data.issue?.title || "(untitled)";
    meta = `${statusMark(data.status)}<span class="mono">${esc(data.id)}</span><span>${esc(data.issue?.repository || "")}</span><span>${esc(
      fmtDate(data.created_at),
    )}</span>`;
    actions = `<button class="btn small" type="button" data-action="copy" data-copy="${esc(data.id)}" data-label="Run ID">Copy ID</button>
      <button class="btn small" type="button" data-action="export-run">Export JSON</button>
      ${data.status === "queued" && !REPLAY_MODE ? `<button class="btn small" type="button" data-action="cancel-run" data-id="${esc(data.id)}">Cancel run</button>` : ""}`;
  } else if (data && inspector.kind === "report") {
    title = data.task?.title || "Patch evaluation report";
    meta = `${statusMark(data.outcome?.overall || "unknown")}<span class="mono">${esc(short(data.patch?.sha256, 12))}</span><span>${esc(
      data.repository?.name || "",
    )} @ ${esc(short(data.repository?.base_commit, 10))}</span>`;
    actions = `<button class="btn small" type="button" data-action="export-report" data-format="json">Export JSON</button>
      ${data.markdown ? `<button class="btn small" type="button" data-action="export-report" data-format="md">Export Markdown</button>` : ""}`;
  }
  element.innerHTML = `
    <header class="inspector-head">
      <div class="row">
        <h2 id="inspector-title">${esc(title)}</h2>
        <button class="btn ghost icon" type="button" id="inspector-close" data-action="close-inspector" aria-label="Close inspector">✕</button>
      </div>
      <div class="meta">${meta}</div>
      ${actions ? `<div class="actions-row inspector-actions">${actions}</div>` : ""}
      <div class="tabs" role="tablist" aria-label="Inspector sections">
        ${tabs
          .map(
            ([value, text]) =>
              `<button type="button" role="tab" id="tab-${value}" aria-controls="inspector-panel" aria-selected="${
                inspector.tab === value
              }" tabindex="${inspector.tab === value ? 0 : -1}" data-action="inspector-tab" data-tab="${value}">${text}</button>`,
          )
          .join("")}
      </div>
    </header>
    <div class="inspector-body" id="inspector-panel" role="tabpanel" aria-labelledby="tab-${inspector.tab}" tabindex="0">
      <div class="panel-enter">${renderInspectorPanel()}</div>
    </div>`;
}

function renderInspectorPanel() {
  const inspector = state.inspector;
  if (inspector.loading && !inspector.data) {
    return `<div class="actions-row"><span class="spinner" aria-hidden="true"></span><span>Loading recorded data…</span></div>`;
  }
  if (inspector.error) {
    return `<div class="empty"><h2>Could not load this ${inspector.kind}</h2><p>${esc(inspector.error)}</p>
      <div class="actions"><button class="btn" type="button" data-action="inspector-retry">Retry</button></div></div>`;
  }
  const data = inspector.data;
  if (!data) return "";
  if (inspector.kind === "report") return renderReportPanel(data, inspector.tab);
  switch (inspector.tab) {
    case "timeline":
      return renderTimeline(data, inspector.events);
    case "patch":
      return renderPatchPanel(data);
    case "verification":
      return renderVerification(data, inspector.events);
    case "tools":
      return renderTools(inspector.events);
    case "artifacts":
      return renderArtifacts(data, inspector.artifact);
    case "provenance":
      return renderProvenance(data);
    default:
      return renderOverview(data);
  }
}

/** @param {any} run */
function renderOverview(run) {
  const provenance = provenanceOf(run);
  const recorded = provenance ? label("recorded") : "";
  const requested = run.metadata?.requested_base_commit ?? run.issue?.base_commit ?? null;
  const executed = run.metadata?.executed_base_commit ?? run.metadata?.base_commit ?? null;
  const cost =
    run.cost_usd === null || run.cost_usd === undefined
      ? `unknown ${label("unknown", "The provider did not report cost")}`
      : `${fmtCost(run.cost_usd)} <span class="muted">provider-reported</span>`;
  const estimate = provenance?.published_record?.published_rate_cost_estimate_usd;
  const facts = [
    ["Repository", `<span class="mono">${esc(run.issue?.repository || "—")}</span>`],
    [
      "Base commit",
      `<span class="mono">${esc(short(executed, 12))}</span>${
        requested && executed && requested.toLowerCase() !== executed.toLowerCase() ? " <strong>(mismatch)</strong>" : ""
      }`,
    ],
    ["Requested commit", requested ? `<span class="mono">${esc(short(requested, 12))}</span>` : `<span class="muted">not requested</span>`],
    ["Model", `<span class="mono">${esc(run.config?.model || "—")}</span>`],
    ["Benchmark", esc(run.benchmark_id)],
    ["Sandbox", esc(run.config?.sandbox || "—")],
    ["Retrieval", `${esc(run.config?.retrieval || "—")} · limit ${esc(run.config?.retrieval_limit ?? "—")}`],
    ["Attempts", `${fmtInt(run.attempts)} of ${fmtInt((run.config?.max_retries ?? 0) + 1)} allowed`],
    ["Duration", `${fmtMs(run.latency_ms)} ${recorded}`],
    ["Tokens", `${fmtInt(run.input_tokens)} in · ${fmtInt(run.output_tokens)} out ${recorded}`],
    ["Tool calls", run.tool_calls === null || run.tool_calls === undefined ? `unknown ${label("unknown")}` : `${fmtInt(run.tool_calls)} ${recorded}`],
    ["Cost", cost],
    ...(estimate !== undefined && estimate !== null
      ? [["Published-rate estimate", `${fmtCost(estimate)} ${label("derived", "Estimate from published token prices; not a bill")}`]]
      : []),
    ["Failure category", esc(run.failure_category || "none")],
  ];
  return `
    <div class="section">${renderOutcomes(run)}</div>
    <div class="section">
      <h3>Identity and measurements</h3>
      <dl class="facts">${facts.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${value}</dd></div>`).join("")}</dl>
    </div>
    <div class="section">
      <h3>Task</h3>
      <p class="prose">${esc(run.issue?.body || "No issue body recorded.")}</p>
    </div>`;
}

/** @param {any} run */
function renderOutcomes(run) {
  const outcome = run.outcome;
  if (!outcome) {
    const provenance = provenanceOf(run);
    const verified = provenance?.independent_verification === "verified";
    return `<div class="verdict">
        ${statusMark(run.status)}
        ${verified ? label("verified", "A recorded clean-copy verification passed") : ""}
        ${label("unknown", "Separate outcomes were not recorded")}
        <p>Legacy record. It stores a single combined status, so target correctness, regression preservation, and integrity cannot be shown separately. Its <code>regression_free</code> field meant “the independent target check passed” (erratum E6).</p>
      </div>`;
  }
  const cells = [
    ["Overall", outcome.overall],
    ["Target checks", outcome.target],
    ["Regression checks", outcome.regression],
    ["Integrity", outcome.integrity],
    ["Environment", outcome.environment],
    ["Provider", outcome.provider],
    ["Policy", outcome.policy],
    ["Timeout", outcome.timed_out ? "error" : "ok"],
  ];
  const reasons = (outcome.reasons || []).join(" · ");
  let note = reasons;
  if (outcome.failed_stage) {
    note = `Stopped at the agent-side ${outcome.failed_stage.replaceAll("_", " ")} stage, before independent verification. ${reasons}`;
  } else if (outcome.overall === "accepted") {
    note =
      outcome.regression === "not_configured"
        ? "Target checks passed in a fresh workspace at the base commit. No regression checks were configured, so regression preservation was not measured."
        : "All required checks passed in a fresh workspace at the base commit. A finite test suite cannot prove the absence of every regression.";
  } else if (!reasons && outcome.target === "not_run") {
    note = "Independent verification did not run.";
  }
  return `<div class="verdict ${outcome.overall === "accepted" ? "accepted" : ""}">
      ${statusMark(outcome.overall)}
      ${outcome.independently_verified ? label("verified", "Target checks passed in a pristine verifier") : ""}
      <span class="muted mono">base ${esc(short(outcome.verification_commit, 12))}</span>
      ${note ? `<p>${esc(note)}</p>` : ""}
    </div>
    <div class="outcomes">${cells
      .map(([name, value]) => {
        const text = name === "Timeout" ? (outcome.timed_out ? "Timed out" : "Within budget") : STATUS_LABELS[value] || value;
        return `<div><span class="name">${esc(name)}</span>${statusMark(value, text)}</div>`;
      })
      .join("")}</div>`;
}

/** @param {any[]|null} events */
function eventsByName(events, ...names) {
  return (events || []).filter((event) => names.includes(event.name));
}

/** @param {any} event */
function describeEvent(event) {
  const p = event.payload || {};
  switch (event.name) {
    case "workflow.state_changed":
      return `${p.from ?? "start"} → ${p.to}${p.summary ? ` · ${p.summary}` : ""}`;
    case "agent_tool.completed":
      return `${p.name} ${p.ok ? "ok" : "failed"} · ${fmtMs(p.duration_ms)}${p.input?.path ? ` · ${p.input.path}` : ""}`;
    case "agent_tool.rejected":
      return `refused ${p.name}: ${p.reason}`;
    case "plan.created":
      return p.summary || "";
    case "retrieval.completed":
      return `${p.hits} hits · ${(p.selected_files || []).join(", ")}`;
    case "provider.completed":
      return `${fmtInt(p.input_tokens)} in · ${fmtInt(p.output_tokens)} out · ${fmtInt(p.provider_calls)} calls`;
    case "grader.completed":
    case "static_analysis.completed":
    case "task_verification.completed":
    case "benchmark.verification.completed":
      return `${p.passed ? "passed" : "failed"}${p.reason ? ` · ${p.reason}` : ""} · ${(p.checks || []).length} checks`;
    case "attempt.started":
      return `attempt ${p.attempt} of ${p.max_attempts}`;
    case "attempt.finished":
      return `attempt ${p.attempt} ${p.passed ? "passed" : "failed"} · ${p.reason}`;
    case "attempt.error":
    case "workflow.retry_stopped":
      return p.reason || "";
    case "workspace.isolated":
      return `${p.snapshot || "disposable copy"} · base ${short(p.base_commit, 10)}`;
    case "workflow.finished":
    case "run.finished":
      return `${p.status}${p.failure_category && p.failure_category !== "none" ? ` · ${p.failure_category}` : ""}`;
    case "worker.claimed":
      return `worker ${short(p.worker_id, 8)}`;
    case "repository.analyzed":
      return `${p.file_count} files · ${Object.keys(p.languages || {}).join(", ")}`;
    case "workflow.rejected":
      return p.reason || "";
    default:
      return "";
  }
}

/** @param {any} run @param {any[]|null} events */
function renderTimeline(run, events) {
  if (!events) return `<p class="prose">Events could not be loaded for this run.</p>`;
  if (!events.length) return `<p class="prose">No events were recorded for this run.</p>`;
  const start = new Date(events[0].timestamp).getTime();
  const index = state.playback.index;
  const items = events
    .map((event, position) => {
      const at = new Date(event.timestamp).getTime() - start;
      const kind = event.provenance === "derived" ? "derived" : "recorded";
      const classes = [
        event.name === "workflow.state_changed" ? "state" : "",
        index >= 0 && position > index ? "dim" : "",
        position === index ? "active" : "",
      ].join(" ");
      const detail = describeEvent(event);
      return `<li class="${classes}" data-position="${position}">
        <span class="at" title="${esc(event.timestamp)}">+${(at / 1000).toFixed(2)}s</span>
        <span class="rail" aria-hidden="true"></span>
        <div class="body">
          <div class="name">${esc(event.name)} ${label(kind)}</div>
          ${detail ? `<div class="detail">${esc(detail)}</div>` : ""}
          <details><summary>Payload</summary><pre class="json">${esc(prettyJson(event.payload))}</pre></details>
        </div></li>`;
    })
    .join("");
  const progress = index < 0 ? 100 : Math.round(((index + 1) / events.length) * 100);
  return `
    <div class="playback">
      <button class="btn small" type="button" data-action="playback-toggle">${state.playback.timer ? "Pause" : index >= 0 && index < events.length - 1 ? "Resume" : "Play timeline"}</button>
      <button class="btn small ghost" type="button" data-action="playback-step" aria-label="Next event">Step</button>
      <button class="btn small ghost" type="button" data-action="playback-reset">Show all</button>
      <progress class="progress" max="100" value="${progress}" aria-hidden="true"></progress>
      <span class="view-count">${index < 0 ? events.length : index + 1} / ${events.length} events</span>
    </div>
    <ol class="timeline" aria-label="Recorded events">${items}</ol>`;
}

function stopPlayback() {
  window.clearInterval(state.playback.timer);
  state.playback.timer = 0;
}

function playbackStep() {
  const events = state.inspector.events || [];
  if (!events.length) return;
  state.playback.index = Math.min(events.length - 1, state.playback.index + 1);
  if (state.playback.index >= events.length - 1) stopPlayback();
  rerenderPanel();
  const active = $(`.timeline li[data-position="${state.playback.index}"]`);
  active?.scrollIntoView({ block: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

function togglePlayback() {
  const events = state.inspector.events || [];
  if (state.playback.timer) {
    stopPlayback();
    rerenderPanel();
    return;
  }
  if (state.playback.index < 0 || state.playback.index >= events.length - 1) state.playback.index = -1;
  if (prefersReducedMotion()) {
    playbackStep();
    return;
  }
  state.playback.timer = window.setInterval(playbackStep, 240);
  playbackStep();
}

function rerenderPanel() {
  const panel = $("#inspector-panel");
  if (panel) panel.innerHTML = renderInspectorPanel();
}

/** @param {any} run */
function renderPatchPanel(run) {
  return `<div class="section">${renderDiff(run.patch || "", { id: "run" })}</div>
    ${run.patch ? `<p class="view-count">Final cumulative patch from the base commit. Markers: <strong>+</strong> added, <strong>-</strong> removed.</p>` : ""}`;
}

/** @param {any} check */
function renderCommandCheck(check) {
  const outcome = check.timed_out ? "error" : check.passed ? "passed" : "failed";
  const text = check.timed_out ? "Timed out" : check.passed ? "Passed" : "Failed";
  const output = [check.stdout, check.stderr].filter((part) => part && part !== "[withheld]").join("\n").trim();
  const withheld = check.stdout === "[withheld]";
  return `<div class="check">
      <div class="check-head">${statusMark(outcome, text)}<span class="what">${esc(check.kind || check.name || "command")}</span>
        <span class="facts-inline">exit ${esc(check.return_code ?? "—")} · ${fmtMs(check.duration_ms)}</span></div>
      <code class="command">${esc(check.command || "")}</code>
      ${withheld ? `<p class="view-count">Output withheld: evaluator-owned hidden checks.</p>` : output ? `<pre class="output">${esc(output)}</pre>` : ""}
    </div>`;
}

/** @param {any} run @param {any[]|null} events */
function renderVerification(run, events) {
  const verifications = eventsByName(events, "task_verification.completed");
  const sections = [];
  for (const event of verifications) {
    const p = event.payload || {};
    const verifiers = (p.verifiers || [])
      .filter((record) => record.kind !== "integrity")
      .map((record) => {
        const restored = [...(record.restored_from_base || []), ...(record.removed_patch_additions || [])];
        return `<div class="check"><div class="check-head">
            ${statusMark(record.passed ? "passed" : "failed", `${record.kind} verifier`)}
            ${record.pristine ? label("verified", "Fresh workspace at the base commit, clean before the patch") : label("unknown")}
            <span class="facts-inline">base ${esc(short(record.base_commit, 10))} · patch ${esc(short(record.patch_sha256, 10))}</span>
          </div>
          ${restored.length ? `<p class="view-count">Evaluator files reset to the base commit: ${esc(restored.join(", "))}</p>` : ""}
        </div>`;
      })
      .join("");
    sections.push(`<div class="section"><h3>Attempt ${esc(p.attempt)} · task verification <span class="hint">${esc(p.reason || "")}</span> ${label(
      "recorded",
    )}</h3><div class="checks">${verifiers}${(p.checks || []).map(renderCommandCheck).join("")}</div></div>`);
  }
  const final = run.metadata?.verification_results;
  if (Array.isArray(final) && final.length) {
    sections.push(`<div class="section"><h3>Independent final verification ${label("verified")}</h3>
      <div class="checks">${final.map((check) => renderCommandCheck(check)).join("")}</div></div>`);
  }
  const grader = eventsByName(events, "grader.completed", "static_analysis.completed");
  if (grader.length) {
    sections.push(`<div class="section"><h3>Agent-workspace gates <span class="hint">feedback for the agent; not independent</span></h3>
      <div class="checks">${grader
        .map((event) => (event.payload.checks || []).map((check) => renderCommandCheck({ ...check, kind: `${event.name.split(".")[0]} · attempt ${event.payload.attempt}` })).join(""))
        .join("")}</div></div>`);
  }
  if (!sections.length) return `<p class="prose">No verification events were recorded for this run.</p>`;
  return sections.join("");
}

/** @param {any[]|null} events */
function renderTools(events) {
  const tools = eventsByName(events, "agent_tool.completed", "agent_tool.rejected");
  if (!events) return `<p class="prose">Events could not be loaded.</p>`;
  if (!tools.length) return `<p class="prose">No agent tool calls were recorded. The agent may have returned a unified diff directly.</p>`;
  const rows = tools
    .map((event, index) => {
      const p = event.payload || {};
      const rejected = event.name === "agent_tool.rejected";
      return `<tr><td class="num">${index + 1}</td><td class="mono">${esc(p.name)}</td>
        <td>${rejected ? statusMark("rejected", "Refused") : statusMark(p.ok ? "passed" : "failed", p.ok ? "OK" : "Error")}</td>
        <td class="num">${rejected ? "—" : fmtMs(p.duration_ms)}</td>
        <td class="mono">${esc(rejected ? p.reason : p.input?.path || p.input?.query || "")}</td>
        <td class="mono">${esc((p.changed_files || []).join(", "))}</td></tr>`;
    })
    .join("");
  return `<div class="section"><h3>Tool calls ${label("recorded")}</h3><div class="table-wrap"><table class="kv-table">
    <thead><tr><th class="num">#</th><th>Tool</th><th>Result</th><th class="num">Time</th><th>Input</th><th>Changed</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <p class="view-count">Inputs are recorded as paths and content digests, not file contents.</p></div>`;
}

/** @param {any} run @param {any} artifact */
function renderArtifacts(run, artifact) {
  if (!artifact) return `<p class="prose">The artifact inventory could not be loaded.</p>`;
  if (!artifact.available) {
    return `<div class="section"><h3>Artifacts ${label(artifact.provenance === "unknown" ? "unknown" : "unknown")}</h3>
      <p class="prose">${esc(artifact.note || "No artifact directory is available for this run on this host.")}</p></div>`;
  }
  const files = artifact.files || [];
  const evidence = files.filter((file) => file.startsWith("evidence/"));
  return `<div class="section"><h3>Evidence bundle ${evidence.length ? label("recorded") : label("unknown")}</h3>
      <p class="prose">${
        evidence.length
          ? `${evidence.length} files sealed by <code>evidence/manifest.json</code>. Check them with <code>tender evidence verify ${esc(run.metadata?.artifact_path || "")}/evidence</code>. The bundle is hashed, not signed.`
          : "This run predates evidence bundles."
      }</p></div>
    <div class="section"><h3>Files</h3><pre class="json">${esc(files.join("\n"))}</pre>
    <div class="actions-row"><button class="btn small" type="button" data-action="copy" data-copy="${esc(files.join("\n"))}" data-label="File list">Copy file list</button></div></div>`;
}

/** @param {any} run */
function renderProvenance(run) {
  const provenance = provenanceOf(run);
  const timing = run.metadata?.timing_ms;
  const counters = run.metadata?.counters;
  const parts = [];
  if (provenance) {
    const rows = Object.entries(provenance.fields || {})
      .map(([field, value]) => {
        const kind = String(value).startsWith("unknown") ? "unknown" : String(value).startsWith("derived") ? "derived" : "recorded";
        return `<tr><th class="mono">${esc(field)}</th><td>${label(kind)}</td><td>${esc(value)}</td></tr>`;
      })
      .join("");
    parts.push(`<div class="section"><h3>Source</h3><dl class="facts">
      <div><dt>Record</dt><dd>${label("recorded")} ${esc(provenance.source)}</dd></div>
      <div><dt>Events</dt><dd>${label("recorded")} copied from the run database</dd></div>
      <div><dt>Input file</dt><dd class="mono">${esc(provenance.input)}</dd></div>
      <div><dt>Clean-copy check</dt><dd>${label(provenance.independent_verification === "verified" ? "verified" : "unknown")}</dd></div>
    </dl></div>
    <div class="section"><h3>Field labels</h3><div class="table-wrap"><table class="kv-table"><tbody>${rows}</tbody></table></div></div>`);
  } else {
    parts.push(`<div class="section"><h3>Source</h3><p class="prose">${label("recorded")} Every value here was recorded by this Tender instance while the run executed.</p></div>`);
  }
  if (timing && Object.keys(timing).length) {
    parts.push(`<div class="section"><h3>Stage durations ${label("recorded")}</h3><table class="kv-table"><tbody>${Object.entries(timing)
      .map(([stage, value]) => `<tr><th>${esc(stage.replaceAll("_", " "))}</th><td class="num">${fmtMs(Number(value))}</td></tr>`)
      .join("")}</tbody></table><p class="view-count">Stages that did not run are absent rather than zero.</p></div>`);
  } else {
    parts.push(`<div class="section"><h3>Stage durations ${label("unknown")}</h3><p class="prose">This record predates per-stage timing; only the end-to-end duration is known.</p></div>`);
  }
  if (counters && Object.keys(counters).length) {
    parts.push(`<div class="section"><h3>Retries and calls ${label("recorded")}</h3><table class="kv-table"><tbody>${Object.entries(counters)
      .map(([name, value]) => `<tr><th>${esc(name.replaceAll("_", " "))}</th><td class="num">${fmtInt(Number(value))}</td></tr>`)
      .join("")}</tbody></table></div>`);
  }
  if (run.metadata?.cost) {
    parts.push(`<div class="section"><h3>Cost basis</h3><p class="prose">${esc(run.metadata.cost.note || "")}</p></div>`);
  }
  return parts.join("");
}

/* ================================================================ reports */

function renderReportsView() {
  const intro = `<div class="page-head"><div><h1>Patch verification reports</h1>
      <p>Each report checks one patch against a pinned commit, in fresh workspaces, with criteria fixed before the patch ran.</p></div>
      <div class="actions-row"><button class="btn small" type="button" data-action="setup">Verify a patch</button></div></div>
    <div class="flow section" aria-label="How a patch is verified">
      ${[
        ["01", "Pin", "Resolve the base commit from a read-only, sanitized copy of the repository."],
        ["02", "Validate", "Check patch syntax, paths, sizes, binaries, symlinks, and protected files."],
        ["03", "Isolate", "Apply the patch to a fresh workspace per check; reset evaluator files."],
        ["04", "Verify", "Run baseline, target, and regression checks in the sandbox."],
        ["05", "Record", "Seal commands, outputs, digests, and unknowns into an evidence bundle."],
      ]
        .map(([step, name, text]) => `<div><span class="step">${step}</span><strong>${name}</strong><p class="view-count">${text}</p></div>`)
        .join("")}
    </div>`;
  if (!state.reportsLoaded) return `${intro}<div class="actions-row"><span class="spinner" aria-hidden="true"></span><span>Loading reports…</span></div>`;
  if (state.reportsError) {
    return `${intro}<div class="empty"><h2>No reports available</h2><p>${esc(state.reportsError)}</p>
      <div class="actions"><button class="btn" type="button" data-action="setup">Show the verify-patch command</button></div></div>`;
  }
  if (!state.reports.length) {
    return `${intro}<div class="empty"><h2>No reports yet</h2><p>Run <code>tender verify-patch</code> to evaluate a patch; reports appear here when the API's report directory contains them.</p>
      <div class="actions"><button class="btn primary" type="button" data-action="setup">Show the command</button></div></div>`;
  }
  const rows = state.reports
    .map(
      (report) => `<button class="report-row" type="button" data-action="open-report" data-id="${esc(report.report_id)}">
        ${statusMark(report.outcome?.overall || "unknown")}
        <span><span class="title">${esc(report.task?.title || report.report_id)}</span>
        <span class="sub mono">${esc(report.repository?.name || "")} @ ${esc(short(report.repository?.base_commit, 10))} · patch ${esc(
          short(report.patch?.sha256, 10),
        )} · ${esc(report.patch?.file || "")}</span></span>
        <span class="view-count">${esc(fmtDate(report.created_at))}</span>
      </button>`,
    )
    .join("");
  return `${intro}<div class="report-list">${rows}</div>`;
}

/** @param {any} report @param {string} tab */
function renderReportPanel(report, tab) {
  if (tab === "patch") return renderDiff(report.patch?.diff || "", { id: "report" });
  if (tab === "checks") return renderReportChecks(report);
  if (tab === "integrity") return renderReportIntegrity(report);
  if (tab === "evidence") return renderReportEvidence(report);
  const outcome = report.outcome || {};
  const facts = [
    ["Repository", `<span class="mono">${esc(report.repository?.name)}</span>`],
    ["Base commit", `<span class="mono">${esc(short(report.repository?.base_commit, 12))}</span> ${label("verified", "Resolved and materialized from Git objects")}`],
    ["Source unchanged", report.repository?.source_unchanged === true ? `yes ${label("verified")}` : report.repository?.source_unchanged === false ? "<strong>no</strong>" : label("unknown")],
    ["Patch", `<span class="mono">${esc(report.patch?.file || "")}</span>`],
    ["Patch SHA-256", `<span class="mono">${esc(short(report.patch?.sha256, 16))}</span>`],
    ["Files changed", `${fmtInt(report.patch?.files?.length)} · +${fmtInt(report.patch?.additions)} −${fmtInt(report.patch?.deletions)}`],
    ["Sandbox", esc(report.environment?.sandbox?.backend || "—")],
    ["Isolation", esc(report.environment?.sandbox?.isolation || "—")],
    ["Duration", fmtMs(report.timing_ms?.total_wall_clock)],
    ["Criteria", `<span class="mono">${esc(short(report.criteria?.sha256, 12))}</span>`],
  ];
  const unknowns = (report.unknowns || []).map((item) => `<li>${label("unknown")} ${esc(item)}</li>`).join("");
  const explanations = (report.explanations || []).map((item) => `<li>${esc(item)}</li>`).join("");
  return `
    <div class="section">
      <div class="verdict ${outcome.overall === "accepted" ? "accepted" : ""}">
        ${statusMark(outcome.overall || "unknown")}
        ${outcome.independently_verified ? label("verified") : ""}
        <p>${esc(report.summary || "")}</p>
      </div>
      <div class="outcomes">${[
        ["Validation", report.validation?.passed ? "passed" : "failed"],
        ["Integrity", outcome.integrity],
        ["Baseline", report.baseline?.status || "not_run"],
        ["Target", outcome.target],
        ["Regression", outcome.regression],
        ["Environment", outcome.environment],
        ["Policy", outcome.policy],
        ["Timeout", outcome.timed_out ? "error" : "ok"],
      ]
        .map(([name, value]) => `<div><span class="name">${esc(name)}</span>${statusMark(value, name === "Timeout" ? (outcome.timed_out ? "Timed out" : "Within budget") : undefined)}</div>`)
        .join("")}</div>
    </div>
    ${explanations ? `<div class="section"><h3>Why</h3><ul class="prose">${explanations}</ul></div>` : ""}
    <div class="section"><h3>Task</h3><p class="prose">${esc(report.task?.description || "")}</p></div>
    <div class="section"><h3>Identity</h3><dl class="facts">${facts.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${value}</dd></div>`).join("")}</dl></div>
    ${unknowns ? `<div class="section"><h3>Not established by this report</h3><ul class="tests">${unknowns}</ul></div>` : ""}`;
}

const COVERAGE_SOURCES = {
  evaluator_declared: "Declared by the evaluator (expected_tests)",
  base_commit_collection: "Collected at the base commit, before the patch",
  evaluator_source_static: "Read from evaluator-owned test source",
  candidate_collection_authorized: "Candidate's own collection (evaluator-authorized; not independent)",
  unavailable: "Could not be established; the check failed closed",
};

/** Which tests were required, by whom, and which actually ran. */
function renderCoverage(coverage) {
  if (!coverage) return "";
  const trusted = coverage.provenance !== "candidate_collection_authorized" && coverage.provenance !== "unavailable";
  /** @param {string} name @param {string[]} ids */
  const list = (name, ids) =>
    ids?.length ? `<details><summary>${esc(name)} (${ids.length})</summary><pre class="json">${esc(ids.join("\n"))}</pre></details>` : "";
  return `<div class="check coverage">
      <div class="check-head">${label(trusted ? "verified" : "unknown")}<span class="what">Required tests: ${esc(
        COVERAGE_SOURCES[coverage.provenance] || coverage.provenance,
      )}</span>
        <span class="facts-inline">${fmtInt(coverage.executed?.length ?? 0)} of ${fmtInt(coverage.expected?.length ?? 0)} required ran</span></div>
      ${coverage.unavailable_reason ? `<p class="view-count">${esc(coverage.unavailable_reason)}</p>` : ""}
      ${coverage.missing_required?.length ? `<p class="callout">Required tests that did not run: <code>${esc(coverage.missing_required.join(", "))}</code></p>` : ""}
      ${list("Expected", coverage.expected)}${list("Collected in the candidate workspace", coverage.collected)}${list("Executed", coverage.executed)}${list(
        "Extra cases added by the candidate",
        coverage.extra_candidate_cases,
      )}
    </div>`;
}

/** @param {any} group @param {string} title */
function renderCheckGroup(group, title) {
  if (!group) return "";
  const checks = (group.checks || [])
    .map((check) => {
      const tests = (check.tests || [])
        .map((test) => `<li>${statusMark(test.outcome === "passed" ? "passed" : test.outcome === "missing" ? "unknown" : "failed", test.outcome)} <span>${esc(test.id)}${test.failing_cases ? ` — failing: ${esc(test.failing_cases)}` : ""}</span></li>`)
        .join("");
      return `${renderCommandCheck({ ...check, kind: check.name })}${tests ? `<div class="check"><ul class="tests" aria-label="Per-test outcomes">${tests}</ul></div>` : ""}${renderCoverage(check.coverage)}`;
    })
    .join("");
  return `<div class="section"><h3>${esc(title)} ${statusMark(group.status || "unknown")}</h3>
    ${group.note ? `<p class="view-count">${esc(group.note)}</p>` : ""}
    <div class="checks">${checks || `<div class="check"><span class="muted">${esc(group.status === "not_configured" ? "Not configured." : "Not run.")}</span></div>`}</div></div>`;
}

/** @param {any} report */
function renderReportChecks(report) {
  const validation = (report.validation?.checks || [])
    .map((check) => `<div class="check"><div class="check-head">${statusMark(check.passed ? "passed" : "failed")}<span class="what">${esc(check.name)}</span></div>${
      check.detail ? `<p class="view-count">${esc(check.detail)}</p>` : ""
    }</div>`)
    .join("");
  return `<div class="section"><h3>Patch validation ${statusMark(report.validation?.passed ? "passed" : "failed")}</h3><div class="checks">${validation}</div></div>
    ${renderCheckGroup(report.baseline, "Baseline: target checks before the patch")}
    ${renderCheckGroup(report.target, "Target checks with the patch")}
    ${renderCheckGroup(report.regression, "Regression checks with the patch")}`;
}

/** @param {any} report */
function renderReportIntegrity(report) {
  const integrity = report.integrity || {};
  const violations = (integrity.violations || []).map((item) => `<li>${statusMark("violated", item.rule)} <span class="mono">${esc(item.path)}</span></li>`).join("");
  const restored = [...(integrity.restored_from_base || []), ...(integrity.removed_patch_additions || [])];
  return `<div class="section"><h3>Integrity ${statusMark(integrity.passed === false ? "violated" : integrity.passed ? "passed" : "not_checked")}</h3>
      ${violations ? `<ul class="tests">${violations}</ul>` : `<p class="prose">No protected paths, evaluator assets, or test-tool shadowing were touched.</p>`}</div>
    <div class="section"><h3>Evaluator-owned files reset in each verifier</h3><p class="prose">${
      restored.length ? esc(restored.join(", ")) : "The patch changed no evaluator-owned files."
    }</p></div>
    <div class="section"><h3>Policy</h3><pre class="json">${esc(prettyJson(integrity.policy || {}))}</pre></div>
    ${(integrity.evaluator_assets || []).length ? `<div class="section"><h3>Hidden evaluator assets</h3><pre class="json">${esc(prettyJson(integrity.evaluator_assets))}</pre></div>` : ""}`;
}

/** @param {any} report */
function renderReportEvidence(report) {
  const evidence = report.evidence || {};
  const files = (evidence.files || []).map((file) => `<tr><td class="mono">${esc(file.path)}</td><td>${label(file.kind === "derived" ? "derived" : "recorded")}</td><td class="num">${fmtInt(file.bytes)}</td><td class="mono">${esc(short(file.sha256, 16))}</td></tr>`).join("");
  const timing = Object.entries(report.timing_ms || {}).map(([stage, value]) => `<tr><th>${esc(stage.replaceAll("_", " "))}</th><td class="num">${fmtMs(Number(value))}</td></tr>`).join("");
  return `<div class="section"><h3>Evidence bundle</h3><p class="prose">Manifest SHA-256 <code>${esc(evidence.manifest_sha256 || "unknown")}</code>. Hashed and self-consistent; not signed.</p>
      ${files ? `<div class="table-wrap"><table class="kv-table"><thead><tr><th>File</th><th>Kind</th><th class="num">Bytes</th><th>SHA-256</th></tr></thead><tbody>${files}</tbody></table></div>` : ""}</div>
    ${timing ? `<div class="section"><h3>Stage durations</h3><table class="kv-table"><tbody>${timing}</tbody></table></div>` : ""}
    <div class="section"><h3>Environment</h3><pre class="json">${esc(prettyJson(report.environment || {}))}</pre></div>`;
}

/* ================================================================ compare */

function openCompare() {
  const runs = [...state.compare].map((id) => state.runs.find((run) => run.id === id)).filter(Boolean);
  if (runs.length < 2) {
    toast("Select at least two runs to compare.");
    return;
  }
  const fields = [
    ["Status", (run) => STATUS_LABELS[run.status] || run.status],
    ["Overall outcome", (run) => run.outcome?.overall ?? "unknown (legacy)"],
    ["Task", (run) => run.issue?.title],
    ["Repository", (run) => run.issue?.repository],
    ["Base commit", (run) => short(run.metadata?.executed_base_commit || run.issue?.base_commit, 12)],
    ["Model", (run) => run.config?.model],
    ["Benchmark", (run) => run.benchmark_id],
    ["Retrieval", (run) => `${run.config?.retrieval} · ${run.config?.retrieval_limit}`],
    ["Sandbox", (run) => run.config?.sandbox],
    ["Retry budget", (run) => run.config?.max_retries],
    ["Attempts", (run) => fmtInt(run.attempts)],
    ["Duration", (run) => fmtMs(run.latency_ms)],
    ["Input tokens", (run) => fmtInt(run.input_tokens)],
    ["Output tokens", (run) => fmtInt(run.output_tokens)],
    ["Tool calls", (run) => fmtInt(run.tool_calls)],
    ["Cost", (run) => fmtCost(run.cost_usd)],
    ["Failure category", (run) => run.failure_category],
    ["Created", (run) => fmtDate(run.created_at)],
  ];
  const rows = fields
    .map(([name, read]) => {
      const values = runs.map((run) => String(/** @type {any} */ (read)(run) ?? "—"));
      return `<tr><th>${esc(name)}</th>${values
        .map((value, index) => `<td class="${index > 0 && value !== values[0] ? "diff-value" : ""}">${esc(value)}</td>`)
        .join("")}</tr>`;
    })
    .join("");
  const dialog = $("#compare-dialog");
  if (!dialog) return;
  dialog.innerHTML = `<header class="dialog-head"><h2 id="compare-title">Compare ${runs.length} runs</h2>
      <button class="btn ghost icon" type="button" data-action="close-dialog" aria-label="Close comparison">✕</button></header>
    <div class="dialog-body"><p class="view-count">Recorded fields side by side. Values that differ from the first run are bold. No composite score is computed.</p>
      <div class="table-wrap"><table class="kv-table"><thead><tr><th></th>${runs
        .map((run) => `<th class="mono">${esc(short(run.id))}</th>`)
        .join("")}</tr></thead><tbody>${rows}</tbody></table></div></div>
    <footer class="dialog-foot"><button class="btn" type="button" data-action="compare-export">Export comparison JSON</button>
      <button class="btn primary" type="button" data-action="close-dialog">Done</button></footer>`;
  openOverlay(dialog);
}

/* ================================================================ dialogs */

function openHelp() {
  const dialog = $("#help-dialog");
  if (!dialog) return;
  const shortcuts = [
    ["Command palette", "⌘K / Ctrl K"],
    ["Search runs", "/"],
    ["Move selection", "j  k"],
    ["Open selected run", "↵"],
    ["Select for comparison", "x"],
    ["Refresh", "r"],
    ["Switch theme", "t"],
    ["Close", "esc"],
  ];
  dialog.innerHTML = `<header class="dialog-head"><h2 id="help-title">Help</h2>
      <button class="btn ghost icon" type="button" data-action="close-dialog" aria-label="Close help">✕</button></header>
    <div class="dialog-body">
      <h3>Shortcuts</h3>
      <div class="shortcuts">${shortcuts.map(([name, keys]) => `<div><span>${esc(name)}</span><span>${keys
        .split("  ")
        .map((key) => `<kbd>${esc(key)}</kbd>`)
        .join(" ")}</span></div>`).join("")}</div>
      <h3>Labels</h3>
      <p class="prose">${label("recorded")} written by Tender while the run executed · ${label("derived")} computed from recorded values or sanitized · ${label(
        "unknown",
      )} never captured, shown as unknown rather than zero · ${label("verified")} re-checked in a fresh workspace at the base commit.</p>
      <h3>API used by this page</h3>
      <pre>GET  /runs?summary=true     list (X-Total-Count header)
GET  /runs/{id}             full run: patch, outcome, metadata
GET  /runs/{id}/events      recorded event stream
GET  /runs/{id}/artifact    artifact and evidence inventory
POST /runs/{id}/cancel      cancel a queued run
GET  /metrics               aggregates over every run
GET  /reports               patch evaluation reports</pre>
    </div>
    <footer class="dialog-foot"><a class="btn" href="${REPO_URL}" target="_blank" rel="noreferrer">Repository</a><button class="btn primary" type="button" data-action="close-dialog">Close</button></footer>`;
  openOverlay(dialog);
}

function openSetup() {
  const dialog = $("#setup-dialog");
  if (!dialog) return;
  dialog.innerHTML = `<header class="dialog-head"><h2 id="setup-title">Run Tender locally</h2>
      <button class="btn ghost icon" type="button" data-action="close-dialog" aria-label="Close">✕</button></header>
    <div class="dialog-body">
      <h3>Verify a patch against a pinned commit</h3>
      <pre id="cmd-verify">tender verify-patch \\
  --repo ~/code/my-project \\
  --base 3f2c1e0 \\
  --patch fix.patch \\
  --criteria tender-criteria.toml \\
  --out reports/fix-1</pre>
      <div class="actions-row"><button class="btn small" type="button" data-action="copy-code" data-id="cmd-verify">Copy</button></div>
      <h3>Run the deterministic failure-and-repair demo</h3>
      <pre id="cmd-demo">python scripts/run_patch_demo.py</pre>
      <div class="actions-row"><button class="btn small" type="button" data-action="copy-code" data-id="cmd-demo">Copy</button></div>
      <h3>Serve this dashboard</h3>
      <pre id="cmd-serve">TENDER_REPORT_ROOT=reports uvicorn tender.api:app --port 8000
open http://127.0.0.1:8000/dashboard/</pre>
      <div class="actions-row"><button class="btn small" type="button" data-action="copy-code" data-id="cmd-serve">Copy</button></div>
      <p class="callout">Untrusted patches run only in the Docker sandbox. The local backend executes on your machine and requires <code>--trust-local-execution</code>.</p>
    </div>
    <footer class="dialog-foot"><button class="btn primary" type="button" data-action="close-dialog">Done</button></footer>`;
  openOverlay(dialog);
}

/** Describe the focused element so an equivalent can be found after re-rendering. */
function focusKey(element) {
  if (!(element instanceof HTMLElement) || element === document.body) return null;
  if (element.id) return `#${CSS.escape(element.id)}`;
  const href = element.getAttribute("href");
  if (href) return `[href="${CSS.escape(href)}"]`;
  const action = element.getAttribute("data-action");
  const id = element.getAttribute("data-id");
  if (action && id) return `[data-action="${CSS.escape(action)}"][data-id="${CSS.escape(id)}"]`;
  if (action) return `[data-action="${CSS.escape(action)}"]`;
  return null;
}

/** @param {HTMLElement} element @param {{ onClose?: () => void, returnFocus?: Element|null }} [options] */
function openOverlay(element, options = {}) {
  const returnFocus = options.returnFocus ?? document.activeElement;
  overlays.push({ element, returnFocus, returnKey: focusKey(returnFocus), onClose: options.onClose });
  const app = $("#app");
  app?.setAttribute("inert", "");
  app?.setAttribute("aria-hidden", "true");
  const scrim = $("#scrim");
  if (scrim) {
    scrim.hidden = false;
    requestAnimationFrame(() => scrim.classList.add("open"));
  }
  element.hidden = false;
  requestAnimationFrame(() => {
    element.classList.add("open");
    const first = /** @type {HTMLElement|null} */ (
      element.querySelector("input, [data-action='close-dialog'], #inspector-close, button, [tabindex='0']")
    );
    first?.focus();
  });
}

/** @param {HTMLElement} element */
function closeOverlay(element) {
  const index = overlays.findIndex((entry) => entry.element === element);
  if (index < 0) return;
  const [entry] = overlays.splice(index, 1);
  element.classList.remove("open");
  const finish = () => {
    if (!element.classList.contains("open")) element.hidden = true;
  };
  if (prefersReducedMotion()) finish();
  else window.setTimeout(finish, 240);
  if (!overlays.length) {
    const app = $("#app");
    app?.removeAttribute("inert");
    app?.removeAttribute("aria-hidden");
    const scrim = $("#scrim");
    if (scrim) {
      scrim.classList.remove("open");
      window.setTimeout(() => {
        if (!overlays.length) scrim.hidden = true;
      }, prefersReducedMotion() ? 0 : 240);
    }
  }
  let target = /** @type {HTMLElement|null} */ (entry.returnFocus);
  if (!target || target === document.body || !document.contains(target)) {
    target = entry.returnKey ? /** @type {HTMLElement|null} */ (document.querySelector(entry.returnKey)) : null;
  }
  // Never leave focus inside the closing overlay.
  (target && document.contains(target) ? target : $("#main"))?.focus({ preventScroll: true });
}

function closeTopOverlay() {
  const top = overlays.at(-1);
  if (!top) return false;
  if (top.onClose) top.onClose();
  else closeOverlay(top.element);
  return true;
}

/** @param {KeyboardEvent} event */
function trapFocus(event) {
  const top = overlays.at(-1);
  if (!top || event.key !== "Tab") return;
  const focusable = /** @type {HTMLElement[]} */ (
    [...top.element.querySelectorAll("a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex='-1'])")]
  ).filter((node) => node.offsetParent !== null || node === document.activeElement);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !top.element.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/* ================================================================ palette */

function paletteItems(query = "") {
  const q = query.trim().toLowerCase();
  const actions = [
    { group: "Actions", label: "Go to runs", hint: "", run: () => navigate("#/runs") },
    { group: "Actions", label: "Go to reports", hint: "", run: () => navigate("#/reports") },
    { group: "Actions", label: "Refresh", hint: "r", run: () => refresh() },
    { group: "Actions", label: "Switch theme", hint: "t", run: () => toggleTheme() },
    { group: "Actions", label: "Export current view as JSON", hint: "", run: () => exportRows("json") },
    { group: "Actions", label: "Export current view as CSV", hint: "", run: () => exportRows("csv") },
    { group: "Actions", label: "Clear filters", hint: "", run: () => clearFilters() },
    { group: "Actions", label: "Run Tender locally", hint: "", run: () => openSetup() },
    ...STATUSES.map((status) => ({
      group: "Filter",
      label: `Status: ${STATUS_LABELS[status]}`,
      hint: "",
      run: () => setFilter("status", status),
    })),
  ];
  const runs = state.runs.slice(0, 200).map((run) => ({
    group: "Runs",
    label: run.issue?.title || run.id,
    hint: `${STATUS_LABELS[run.status] || run.status} · ${short(run.id)}`,
    run: () => navigate(`#/runs/${run.id}`),
  }));
  return [...actions, ...runs]
    .filter((item) => !q || `${item.label} ${item.hint} ${item.group}`.toLowerCase().includes(q))
    .slice(0, 40);
}

function openPalette() {
  const palette = $("#palette");
  const input = /** @type {HTMLInputElement|null} */ ($("#palette-input"));
  if (!palette || !input) return;
  input.value = "";
  state.palette = { index: 0, items: paletteItems() };
  renderPalette();
  openOverlay(palette);
}

function renderPalette() {
  const list = $("#palette-list");
  if (!list) return;
  let group = "";
  list.innerHTML = state.palette.items.length
    ? state.palette.items
        .map((item, index) => {
          const heading = item.group !== group ? `<div class="palette-group">${esc(item.group)}</div>` : "";
          group = item.group;
          return `${heading}<button class="palette-item" type="button" role="option" id="palette-${index}" aria-selected="${
            index === state.palette.index
          }" data-action="palette-run" data-index="${index}"><span>${esc(item.label)}</span><span class="hint">${esc(item.hint)}</span></button>`;
        })
        .join("")
    : `<div class="palette-group">No matches</div>`;
  $("#palette-input")?.setAttribute("aria-activedescendant", `palette-${state.palette.index}`);
  $(`#palette-${state.palette.index}`)?.scrollIntoView({ block: "nearest" });
}

/** @param {number} index */
function runPalette(index) {
  const item = state.palette.items[index];
  const palette = $("#palette");
  if (palette) closeOverlay(palette);
  item?.run();
}

/* ======================================================= filters & export */

/** @param {"status"|"model"|"benchmark"|"category"|"sort"} group @param {string} value */
function setFilter(group, value) {
  state.filters[group] = value;
  syncFilters();
  if (state.view !== "runs") navigate(`#/runs${filterQuery()}`);
  else {
    const filters = $("#filters");
    if (filters) filters.innerHTML = renderFilters();
    const breakdown = $("#breakdown");
    if (breakdown) {
      const focusedKey = document.activeElement?.closest?.("#breakdown") ? document.activeElement.getAttribute("data-group") : null;
      breakdown.innerHTML = renderBreakdown();
      if (focusedKey) /** @type {HTMLElement|null} */ (breakdown.querySelector(`[data-group="${focusedKey}"][aria-pressed="true"], [data-group="${focusedKey}"]`))?.focus();
    }
    const toggle = $("[data-action='toggle-filters']");
    if (toggle) toggle.textContent = `Filters${activeFilterCount() ? ` · ${activeFilterCount()}` : ""}`;
    renderList();
  }
}

function clearFilters() {
  state.filters = { q: "", status: "all", model: "all", benchmark: "all", category: "all", sort: "newest" };
  syncFilters();
  renderMain();
}

/** @param {string} filename @param {string} content @param {string} type */
function download(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** @param {"json"|"csv"} format */
function exportRows(format) {
  const rows = visibleRuns();
  if (!rows.length) {
    toast("Nothing to export in the current view.");
    return;
  }
  const stamp = new Date().toISOString().slice(0, 10);
  if (format === "json") {
    download(`tender-runs-${stamp}.json`, `${JSON.stringify(rows, null, 2)}\n`, "application/json");
  } else {
    const columns = [
      ["id", (run) => run.id],
      ["status", (run) => run.status],
      ["overall_outcome", (run) => run.outcome?.overall ?? ""],
      ["title", (run) => run.issue?.title],
      ["repository", (run) => run.issue?.repository],
      ["base_commit", (run) => run.metadata?.executed_base_commit || run.issue?.base_commit || ""],
      ["model", (run) => run.config?.model],
      ["benchmark", (run) => run.benchmark_id],
      ["attempts", (run) => run.attempts],
      ["latency_ms", (run) => run.latency_ms ?? ""],
      ["input_tokens", (run) => run.input_tokens],
      ["output_tokens", (run) => run.output_tokens],
      ["cost_usd", (run) => run.cost_usd ?? ""],
      ["failure_category", (run) => run.failure_category],
      ["created_at", (run) => run.created_at],
    ];
    /** @param {unknown} value */
    const cell = (value) => {
      let text = String(value ?? "");
      if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`; // neutralize spreadsheet formulas
      return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    };
    const csv = [columns.map(([name]) => name).join(","), ...rows.map((run) => columns.map(([, read]) => cell(/** @type {any} */ (read)(run))).join(","))].join("\n");
    download(`tender-runs-${stamp}.csv`, `${csv}\n`, "text/csv");
  }
  toast(`Exported ${rows.length} runs as ${format.toUpperCase()}.`);
}

/** @param {string} text @param {string} [what] */
async function copyText(text, what = "Text") {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied.`);
  } catch {
    toast("Clipboard is unavailable in this context.", "error");
  }
}

/** @param {string} message @param {"ok"|"error"} [tone] */
function toast(message, tone = "ok") {
  const entry = { id: ++toastSeq, message, tone };
  state.toasts.push(entry);
  renderToasts();
  window.setTimeout(() => {
    state.toasts = state.toasts.filter((item) => item.id !== entry.id);
    renderToasts();
  }, 3600);
}

function renderToasts() {
  const container = $("#toasts");
  if (!container) return;
  container.innerHTML = state.toasts
    .map((item) => `<div class="toast ${item.tone === "error" ? "error" : ""}" role="status">${esc(item.message)}</div>`)
    .join("");
}

/* ================================================================== theme */

/** Light unless the visitor explicitly chose dark; the OS preference is not used. */
function currentTheme() {
  return storageGet(STORE_KEYS.theme) === "dark" ? "dark" : "light";
}

function applyTheme() {
  const theme = currentTheme();
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  const button = $("#theme-btn");
  if (button) {
    const next = theme === "dark" ? "light" : "dark";
    button.textContent = theme === "dark" ? "Light" : "Dark";
    button.setAttribute("aria-label", `Switch to ${next} theme`);
    button.setAttribute("aria-pressed", String(theme === "dark"));
    button.title = `Switch to ${next} theme (t)`;
  }
}

function toggleTheme() {
  storageSet(STORE_KEYS.theme, currentTheme() === "dark" ? "light" : "dark");
  applyTheme();
}

/* ================================================================= events */

/** @param {Event} event */
function handleClick(event) {
  const target = event.target instanceof Element ? event.target.closest("[data-action]") : null;
  if (!target) {
    if (event.target instanceof Element && event.target.id === "scrim") closeTopOverlay();
    return;
  }
  const action = target.getAttribute("data-action");
  const id = target.getAttribute("data-id") || "";
  switch (action) {
    case "open-run":
      if (event.target instanceof Element && event.target.closest("a, input, label")) return;
      navigate(`#/runs/${id}`);
      break;
    case "pick":
      return;
    case "open-report":
      navigate(`#/reports/${id}`);
      break;
    case "close-inspector":
      closeInspector();
      break;
    case "inspector-retry":
      if (state.inspector.kind && state.inspector.id) openInspector(state.inspector.kind, state.inspector.id);
      break;
    case "inspector-tab":
      selectTab(target.getAttribute("data-tab") || "overview");
      break;
    case "set-filter":
      setFilter(/** @type {any} */ (target.getAttribute("data-group")), target.getAttribute("data-value") || "all");
      break;
    case "clear-filters":
      clearFilters();
      break;
    case "toggle-filters":
      state.filtersOpen = !state.filtersOpen;
      $("#filters")?.classList.toggle("open", state.filtersOpen);
      target.setAttribute("aria-expanded", String(state.filtersOpen));
      break;
    case "refresh":
      refresh();
      if (state.view === "reports") {
        state.reportsLoaded = false;
        renderMain();
        loadReports();
      }
      break;
    case "load-more":
      loadMore();
      break;
    case "cancel-run":
      cancelRun(id);
      break;
    case "copy":
      copyText(target.getAttribute("data-copy") || "", target.getAttribute("data-label") || "Text");
      break;
    case "copy-code":
      copyText($(`#${target.getAttribute("data-id")}`)?.textContent || "", "Command");
      break;
    case "copy-diff":
    case "copy-diff-file": {
      const source = target.getAttribute("data-diff") === "report" ? state.inspector.data?.patch?.diff : state.inspector.data?.patch;
      if (action === "copy-diff") copyText(source || "", "Patch");
      else {
        const file = parseDiff(source || "")[Number(target.getAttribute("data-index"))];
        copyText(file ? `${file.raw.join("\n")}\n` : "", "File diff");
      }
      break;
    }
    case "export-json":
      exportRows("json");
      break;
    case "export-csv":
      exportRows("csv");
      break;
    case "export-run": {
      const run = state.inspector.data;
      if (run) download(`tender-run-${short(run.id)}.json`, `${JSON.stringify({ run, events: state.inspector.events }, null, 2)}\n`, "application/json");
      break;
    }
    case "export-report": {
      const report = state.inspector.data;
      if (!report) break;
      if (target.getAttribute("data-format") === "md") download(`tender-report-${short(report.report_id)}.md`, report.markdown, "text/markdown");
      else download(`tender-report-${short(report.report_id)}.json`, `${JSON.stringify(report, null, 2)}\n`, "application/json");
      break;
    }
    case "compare-open":
      openCompare();
      break;
    case "compare-clear":
      state.compare.clear();
      renderList();
      break;
    case "compare-export": {
      const runs = [...state.compare].map((runId) => state.runs.find((run) => run.id === runId)).filter(Boolean);
      download(`tender-comparison-${new Date().toISOString().slice(0, 10)}.json`, `${JSON.stringify(runs, null, 2)}\n`, "application/json");
      break;
    }
    case "close-dialog": {
      const dialog = target.closest(".dialog");
      if (dialog instanceof HTMLElement) closeOverlay(dialog);
      break;
    }
    case "open-palette":
      openPalette();
      break;
    case "palette-run":
      runPalette(Number(target.getAttribute("data-index")));
      break;
    case "toggle-theme":
      toggleTheme();
      break;
    case "help":
      openHelp();
      break;
    case "setup":
      openSetup();
      break;
    case "playback-toggle":
      togglePlayback();
      break;
    case "playback-step":
      stopPlayback();
      playbackStep();
      break;
    case "playback-reset":
      stopPlayback();
      state.playback.index = -1;
      rerenderPanel();
      break;
    default:
      break;
  }
}

/** @param {string} tab */
function selectTab(tab) {
  stopPlayback();
  state.playback.index = -1;
  state.inspector.tab = tab;
  renderInspector();
  $(`#tab-${tab}`)?.focus();
}

/** @param {Event} event */
function handleChange(event) {
  const target = event.target;
  if (target instanceof HTMLInputElement && target.getAttribute("data-action") === "pick") {
    const id = target.getAttribute("data-id") || "";
    if (target.checked) {
      if (state.compare.size >= MAX_COMPARE) {
        target.checked = false;
        toast(`Compare up to ${MAX_COMPARE} runs at a time.`);
        return;
      }
      state.compare.add(id);
    } else state.compare.delete(id);
    renderCompareBar();
    return;
  }
  if (target instanceof HTMLSelectElement && target.getAttribute("data-action") === "select-filter") {
    setFilter(/** @type {any} */ (target.getAttribute("data-group")), target.value);
  }
}

let searchTimer = 0;
/** @param {Event} event */
function handleInput(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (target.id === "search") {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.filters.q = target.value;
      syncFilters();
      renderList();
    }, 120);
  } else if (target.id === "palette-input") {
    state.palette = { index: 0, items: paletteItems(target.value) };
    renderPalette();
  }
}

/** @param {KeyboardEvent} event */
function handleKeydown(event) {
  trapFocus(event);
  const target = event.target;
  const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (overlays.at(-1)?.element.id === "palette") closeTopOverlay();
    else openPalette();
    return;
  }
  if (event.key === "Escape") {
    if (closeTopOverlay()) event.preventDefault();
    else if (typing && target instanceof HTMLInputElement) target.blur();
    return;
  }
  const top = overlays.at(-1);
  if (top?.element.id === "palette") {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const count = state.palette.items.length;
      if (!count) return;
      state.palette.index = (state.palette.index + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      renderPalette();
    } else if (event.key === "Enter") {
      event.preventDefault();
      runPalette(state.palette.index);
    }
    return;
  }
  if (top?.element.id === "inspector" && target instanceof HTMLElement && target.getAttribute("role") === "tab") {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const tabs = (state.inspector.kind === "report" ? REPORT_TABS : RUN_TABS).map(([value]) => value);
      const index = tabs.indexOf(state.inspector.tab);
      selectTab(tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]);
    }
    return;
  }
  if (typing || top || event.metaKey || event.ctrlKey || event.altKey) return;
  if (target instanceof HTMLElement && target.closest("tr[data-action='open-run']") && event.key === "Enter" && !(target instanceof HTMLAnchorElement)) {
    const row = target.closest("tr[data-action='open-run']");
    navigate(`#/runs/${row?.getAttribute("data-id")}`);
    return;
  }
  const rows = visibleRuns();
  const index = rows.findIndex((run) => run.id === state.selectedId);
  switch (event.key) {
    case "/":
      if (state.view !== "runs") break;
      event.preventDefault();
      $("#search")?.focus();
      break;
    case "j":
    case "k": {
      if (state.view !== "runs" || !rows.length) break;
      const next = event.key === "j" ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1);
      state.selectedId = rows[next].id;
      renderList();
      const row = $(`tr[data-id="${CSS.escape(state.selectedId)}"]`);
      row?.scrollIntoView({ block: "nearest" });
      /** @type {HTMLElement|null} */ (row?.querySelector("a.title") || null)?.focus({ preventScroll: true });
      break;
    }
    case "Enter":
      // Links and buttons activate themselves; only act when nothing focusable is focused.
      if (target instanceof HTMLAnchorElement || target instanceof HTMLButtonElement) break;
      if (state.selectedId && state.view === "runs") navigate(`#/runs/${state.selectedId}`);
      break;
    case "x":
      if (state.selectedId) {
        const box = /** @type {HTMLInputElement|null} */ ($(`input[data-action="pick"][data-id="${CSS.escape(state.selectedId)}"]`));
        if (box) {
          box.checked = !box.checked;
          box.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }
      break;
    case "r":
      refresh();
      break;
    case "t":
      toggleTheme();
      break;
    case "?":
      openHelp();
      break;
    default:
      break;
  }
}

function bindEvents() {
  document.addEventListener("click", handleClick);
  document.addEventListener("change", handleChange);
  document.addEventListener("input", handleInput);
  document.addEventListener("keydown", handleKeydown);
  window.addEventListener("hashchange", applyRoute);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh({ quiet: true });
    else window.clearTimeout(pollTimer);
  });
}

function loadStoredFilters() {
  const stored = storageGet(STORE_KEYS.filters);
  if (!stored) return;
  try {
    const parsed = JSON.parse(stored);
    for (const key of Object.keys(state.filters)) {
      if (typeof parsed?.[key] === "string") /** @type {any} */ (state.filters)[key] = parsed[key];
    }
  } catch {
    /* ignore corrupt preferences */
  }
}

function init() {
  if (!ROOT) return;
  renderShell();
  applyTheme();
  loadStoredFilters();
  bindEvents();
  applyRoute();
  refresh();
}

init();
