/**
 * Tender dashboard — zero-build ES module.
 *
 * Architecture:
 *   state  → plain object, the single source of truth
 *   api    → typed fetch client for the local Tender API
 *   render → pure-ish functions rebuilding DOM sections from state
 *   events → delegated listeners + keyboard shortcuts
 *
 * No inline event handlers or inline styles (strict CSP on /dashboard).
 */

/* ==========================================================================
   Types
   ========================================================================== */

/**
 * @typedef {Object} RunIssue
 * @property {string} title
 * @property {string} repository
 * @property {string} body
 * @property {string|null} base_commit
 */

/**
 * @typedef {Object} RunConfig
 * @property {string} model
 * @property {string} architecture
 * @property {string} retrieval
 * @property {number} retrieval_limit
 * @property {string} sandbox
 */

/**
 * @typedef {Object} Run
 * @property {string} id
 * @property {string} status
 * @property {string} failure_category
 * @property {string|null} current_state
 * @property {string} benchmark_id
 * @property {string} created_at
 * @property {number} attempts
 * @property {number} tool_calls
 * @property {number} input_tokens
 * @property {number} output_tokens
 * @property {number|null} cost_usd
 * @property {number|null} latency_ms
 * @property {string|null} patch
 * @property {string|null} test_output
 * @property {RunIssue} issue
 * @property {RunConfig} config
 * @property {Record<string, unknown>} metadata
 */

/**
 * @typedef {Object} RunEvent
 * @property {string} run_id
 * @property {string} name
 * @property {string} timestamp
 * @property {Record<string, unknown>} payload
 */

/**
 * @typedef {Object} Metrics
 * @property {number} runs
 * @property {number} passed
 * @property {number} pass_rate
 * @property {number} completed_runs
 * @property {number} queued_runs
 * @property {number} running_runs
 * @property {number} awaiting_approval
 * @property {number} cancelled_runs
 * @property {number|null} average_latency_ms
 * @property {number|null} latency_p50_ms
 * @property {number|null} latency_p95_ms
 * @property {number|null} total_cost_usd
 * @property {number} cost_observations
 */

/**
 * @typedef {Object} Filters
 * @property {string} q
 * @property {string} status
 * @property {string} model
 * @property {string} benchmark
 * @property {string} sort
 */

/**
 * @typedef {Object} PaletteItem
 * @property {string} group
 * @property {string} label
 * @property {string} hint
 * @property {() => void} run
 */

/* ==========================================================================
   Constants
   ========================================================================== */

const STATUSES = ["queued", "running", "awaiting_approval", "passed", "failed", "cancelled"];
const SORT_OPTIONS = [
  ["newest", "Newest first"],
  ["oldest", "Oldest first"],
  ["latency", "Slowest first"],
  ["tokens", "Most tokens"],
  ["cost", "Costliest first"],
];
const POLL_FAST_MS = 4000;
const POLL_SLOW_MS = 15000;
const MAX_LIMIT = 1000;
const PAGE_SIZE = 100;
const PATCH_RENDER_LIMIT = 4000;

/* The static replay page (demo/replay/) sets this before the module loads; the
   live dashboard never does. Replay mode only changes what the chrome claims. */
const REPLAY_MODE = Boolean(/** @type {any} */ (window).TENDER_REPLAY);

const STORE_KEYS = {
  theme: "tender.theme",
  filters: "tender.filters",
  onboarding: "tender.onboarding.dismissed",
};

/* ==========================================================================
   State
   ========================================================================== */

const state = {
  /** @type {Run[]} */ runs: [],
  /** @type {number} */ total: 0,
  /** @type {Metrics|null} */ metrics: null,
  /** @type {Filters} */ filters: { q: "", status: "all", model: "all", benchmark: "all", sort: "newest" },
  /** @type {Set<string>} */ compare: new Set(),
  /** @type {string|null} */ selectedId: null,
  /** @type {string|null} */ openRunId: null,
  /** @type {number} */ limit: PAGE_SIZE,
  /** @type {boolean} */ loading: false,
  /** @type {boolean} */ loadedOnce: false,
  /** @type {boolean} */ apiOk: true,
  /** @type {string|null} */ apiError: null,
  /** @type {number} */ runsVersion: 0,
  /** @type {number} */ lastLoaded: 0,
  /** @type {string} */ theme: "light",
  /** @type {boolean} */ refreshing: false,
  /** @type {{ tab: string, run: Run|null, events: RunEvent[]|null, artifact: string[]|null, loading: boolean, error: string|null }} */
  drawer: { tab: "overview", run: null, events: null, artifact: null, loading: false, error: null },
  /** @type {{ index: number, items: PaletteItem[] }} */
  palette: { index: 0, items: [] },
  /** @type {Array<{ id: number, message: string, tone: string, action: (() => void)|null }>} */ toasts: [],
};

let toastSeq = 0;
let listDerived = { key: "", rows: /** @type {Run[]} */ ([]) };

/* ==========================================================================
   DOM & formatting helpers
   ========================================================================== */

/**
 * @param {string} selector
 * @returns {HTMLElement}
 */
function $(selector) {
  const element = document.querySelector(selector);
  if (!(element instanceof HTMLElement)) throw new Error(`missing element: ${selector}`);
  return element;
}

/** @type {Record<string, string>} */
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/**
 * Escape a value for safe interpolation into HTML.
 * @param {unknown} value
 * @returns {string}
 */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ESCAPES[character] || character);
}

const numberFormat = new Intl.NumberFormat("en-US");

/** @param {number|null|undefined} value */
function fmtInt(value) {
  if (value === null || value === undefined) return "—";
  return numberFormat.format(value);
}

/** @param {number|null|undefined} value */
function fmtTokens(value) {
  if (!value) return "0";
  if (value >= 100000) return `${(value / 1000).toFixed(0)}k`;
  if (value >= 10000) return `${(value / 1000).toFixed(1)}k`;
  return numberFormat.format(value);
}

/** @param {number|null|undefined} value */
function fmtMs(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`;
  return `${Math.round(value)} ms`;
}

/** @param {number|null|undefined} value */
function fmtCost(value) {
  if (value === null || value === undefined) return "—";
  return `$${value.toFixed(4)}`;
}

/** @param {string} iso */
function fmtDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

/** @param {string} iso */
function relTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 172800) return "yesterday";
  return new Date(then).toLocaleDateString();
}

/** @param {Run} run */
function runTokens(run) {
  return (run.input_tokens || 0) + (run.output_tokens || 0);
}

/** @param {string} status */
function statusClass(status) {
  return STATUSES.includes(status) ? `status-${status}` : "";
}

/* ==========================================================================
   API client
   ========================================================================== */

class ApiError extends Error {
  /** @param {number} status @param {string} detail */
  constructor(status, detail) {
    super(detail || `request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

const api = {
  /**
   * @param {string} path
   * @param {RequestInit} [options]
   */
  async request(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new ApiError(0, "API unreachable — is the Tender server running?");
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
    if (response.status === 204) return null;
    return response.json();
  },

  /**
   * @param {{ limit?: number, offset?: number, status?: string, signal?: AbortSignal }} [params]
   * @returns {Promise<{ runs: Run[], total: number }>}
   */
  async listRuns(params = {}) {
    const query = new URLSearchParams({ summary: "true" });
    if (params.limit) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    if (params.status && params.status !== "all") query.set("status", params.status);
    let response;
    try {
      response = await fetch(`/runs?${query.toString()}`, { signal: params.signal });
    } catch {
      throw new ApiError(0, "API unreachable — is the Tender server running?");
    }
    if (!response.ok) throw new ApiError(response.status, response.statusText);
    const runs = await response.json();
    const totalHeader = Number(response.headers.get("X-Total-Count"));
    return { runs, total: Number.isFinite(totalHeader) ? totalHeader : runs.length };
  },

  /** @param {string} id @returns {Promise<Run>} */
  getRun(id) {
    return this.request(`/runs/${encodeURIComponent(id)}`);
  },

  /** @param {string} id @returns {Promise<RunEvent[]>} */
  getEvents(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/events?limit=2000`);
  },

  /** @param {string} id @returns {Promise<{available: boolean, files: string[]}>} */
  getArtifact(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/artifact`);
  },

  /** @param {string} id @returns {Promise<Run>} */
  cancelRun(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  },

  /** @returns {Promise<Metrics>} */
  getMetrics() {
    return this.request("/metrics");
  },
};

/**
 * @param {unknown} error
 * @returns {string}
 */
function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Not authorized — set TENDER_API_TOKEN and include it in requests.";
    if (error.status === 409) return error.detail || "The run already changed state.";
    if (error.status === 413) return "Request body exceeded the configured cap.";
    if (error.status === 429) return error.detail || "Rate limited — slow down and retry shortly.";
    if (error.status === 0) return error.detail;
    return `${error.status}: ${error.detail}`;
  }
  return error instanceof Error ? error.message : String(error);
}

/* ==========================================================================
   Toasts
   ========================================================================== */

/**
 * @param {string} message
 * @param {{ tone?: "ok"|"error", action?: (() => void)|null, sticky?: boolean }} [options]
 */
function toast(message, options = {}) {
  const entry = { id: ++toastSeq, message, tone: options.tone || "ok", action: options.action || null };
  state.toasts.push(entry);
  renderToasts();
  if (!options.sticky) {
    setTimeout(() => {
      state.toasts = state.toasts.filter((item) => item.id !== entry.id);
      renderToasts();
    }, 3600);
  }
}

function renderToasts() {
  const container = $("#toasts");
  container.innerHTML = state.toasts
    .map(
      (item) => `
      <div class="toast ${item.tone === "error" ? "error" : ""} ${item.action ? "actionable" : ""}"
           ${item.action ? `data-action="toast-action" data-toast-id="${item.id}"` : ""}
           role="status">
        <span>${esc(item.message)}</span>
      </div>`,
    )
    .join("");
}

/* ==========================================================================
   Filters & derived rows
   ========================================================================== */

/** @returns {Run[]} */
function visibleRuns() {
  const key = `${state.runsVersion}|${JSON.stringify(state.filters)}`;
  if (listDerived.key === key) return listDerived.rows;
  const filters = state.filters;
  const query = filters.q.trim().toLowerCase();
  let rows = state.runs.filter((run) => {
    if (filters.status !== "all" && run.status !== filters.status) return false;
    if (filters.model !== "all" && run.config.model !== filters.model) return false;
    if (filters.benchmark !== "all" && run.benchmark_id !== filters.benchmark) return false;
    if (query) {
      const haystack = [
        run.issue?.title,
        run.issue?.repository,
        run.config?.model,
        run.benchmark_id,
        run.status,
        run.failure_category,
        run.id,
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
  /** @type {Record<string, (a: Run, b: Run) => number>} */
  const sorters = {
    newest: (a, b) => b.created_at.localeCompare(a.created_at),
    oldest: (a, b) => a.created_at.localeCompare(b.created_at),
    latency: (a, b) => (b.latency_ms || 0) - (a.latency_ms || 0),
    tokens: (a, b) => runTokens(b) - runTokens(a),
    cost: (a, b) => (b.cost_usd || 0) - (a.cost_usd || 0),
  };
  rows = [...rows].sort(sorters[filters.sort] || sorters.newest);
  listDerived = { key, rows };
  return rows;
}

/** @param {"status"|"model"|"benchmark"|"sort"} group @param {string} value */
function setFilter(group, value) {
  state.filters[group] = value;
  state.selectedId = null;
  saveFilterPrefs();
  syncUrl();
  render();
}

function clearFilters() {
  state.filters.q = "";
  state.filters.status = "all";
  state.filters.model = "all";
  state.filters.benchmark = "all";
  state.filters.sort = "newest";
  const search = /** @type {HTMLInputElement} */ ($("#search"));
  search.value = "";
  saveFilterPrefs();
  syncUrl();
  render();
}

function statusCounts() {
  /** @type {Record<string, number>} */
  const counts = { all: state.total };
  for (const status of STATUSES) counts[status] = 0;
  for (const run of state.runs) counts[run.status] = (counts[run.status] || 0) + 1;
  return counts;
}

/* ==========================================================================
   Rendering — chrome, KPIs, toolbar, rows
   ========================================================================== */

function render() {
  renderStatusPill();
  renderKpis();
  renderChips();
  renderSelects();
  renderOnboarding();
  renderList();
  renderCompareTray();
  renderBanner();
}

function renderStatusPill() {
  const pill = $("#status-pill");
  pill.classList.toggle("live", state.apiOk && !REPLAY_MODE);
  pill.classList.toggle("offline", !state.apiOk);
  const label = REPLAY_MODE ? "Recorded replay" : state.apiOk ? "API ready" : "API offline";
  const labelNode = pill.querySelector("span.label");
  if (labelNode) labelNode.textContent = label;
  pill.setAttribute("aria-label", label);
}

function renderBanner() {
  const banner = $("#banner");
  if (state.apiOk || !state.loadedOnce) {
    banner.classList.remove("open");
    return;
  }
  $("#banner-text").textContent = state.apiError || "API unreachable.";
  banner.classList.add("open");
}

function renderKpis() {
  const metrics = state.metrics;
  const runsKpi = $("#kpi-runs");
  const passKpi = $("#kpi-pass");
  const passFoot = $("#kpi-pass-foot");
  const latencyKpi = $("#kpi-latency");
  const latencyFoot = $("#kpi-latency-foot");
  const costKpi = $("#kpi-cost");
  const costFoot = $("#kpi-cost-foot");

  if (!state.loadedOnce) {
    runsKpi.textContent = "—";
    passKpi.textContent = "—";
    latencyKpi.textContent = "—";
    costKpi.textContent = "—";
    return;
  }
  if (!metrics) {
    runsKpi.textContent = String(state.total || 0);
    passKpi.textContent = "—";
    passFoot.textContent = "metrics unavailable";
    latencyKpi.textContent = "—";
    latencyFoot.textContent = "";
    costKpi.textContent = "—";
    costFoot.textContent = "";
    return;
  }
  const capped = metrics.runs >= MAX_LIMIT;
  runsKpi.textContent = String(metrics.runs);
  $("#kpi-runs-foot").textContent = capped
    ? `latest ${MAX_LIMIT} runs counted`
    : "recorded evaluations";
  const finished = metrics.completed_runs;
  passKpi.textContent = finished ? `${Math.round(metrics.pass_rate * 100)}%` : "—";
  passFoot.textContent = finished
    ? `${metrics.passed} accepted · ${finished - metrics.passed} need review` +
      (metrics.cancelled_runs ? ` · ${metrics.cancelled_runs} cancelled` : "")
    : "awaiting evidence";
  latencyKpi.textContent = fmtMs(metrics.average_latency_ms);
  latencyFoot.textContent =
    metrics.latency_p50_ms !== null
      ? `p50 ${fmtMs(metrics.latency_p50_ms)} · p95 ${fmtMs(metrics.latency_p95_ms)}`
      : "end-to-end runtime";
  costKpi.textContent = fmtCost(metrics.total_cost_usd);
  costFoot.textContent = metrics.cost_observations
    ? `${metrics.cost_observations} provider-reported value${metrics.cost_observations === 1 ? "" : "s"}`
    : "no provider-reported cost";
}

function renderChips() {
  const counts = statusCounts();
  const chips = [["all", "All"]].concat(STATUSES.map((status) => [status, status.replace("_", " ")]));
  $("#status-chips").innerHTML = chips
    .map(
      ([value, label]) => `
      <button class="chip ${state.filters.status === value ? "active" : ""}"
              data-action="set-filter" data-group="status" data-value="${esc(value)}">
        ${esc(label)}${value === "all" ? "" : ` <span class="count">${counts[value] || 0}</span>`}
      </button>`,
    )
    .join("");
}

function renderSelects() {
  const models = [...new Set(state.runs.map((run) => run.config.model))].sort();
  const benchmarks = [...new Set(state.runs.map((run) => run.benchmark_id))].sort();
  const modelSelect = /** @type {HTMLSelectElement} */ ($("#model-select"));
  const benchmarkSelect = /** @type {HTMLSelectElement} */ ($("#benchmark-select"));
  const sortSelect = /** @type {HTMLSelectElement} */ ($("#sort-select"));

  modelSelect.innerHTML =
    `<option value="all">All models</option>` +
    models
      .map(
        (model) =>
          `<option value="${esc(model)}" ${state.filters.model === model ? "selected" : ""}>${esc(model)}</option>`,
      )
      .join("");
  benchmarkSelect.innerHTML =
    `<option value="all">All benchmarks</option>` +
    benchmarks
      .map(
        (benchmark) =>
          `<option value="${esc(benchmark)}" ${state.filters.benchmark === benchmark ? "selected" : ""}>${esc(benchmark)}</option>`,
      )
      .join("");
  sortSelect.innerHTML = SORT_OPTIONS.map(
    ([value, label]) =>
      `<option value="${value}" ${state.filters.sort === value ? "selected" : ""}>${label}</option>`,
  ).join("");

  const visible = visibleRuns().length;
  $("#view-count").textContent =
    state.total > state.runs.length
      ? `Showing ${visible} of ${state.total} runs (loaded ${state.runs.length})`
      : `Showing ${visible} of ${state.runs.length} run${state.runs.length === 1 ? "" : "s"}`;
}

function renderOnboarding() {
  const card = $("#onboarding");
  const dismissed = localStorage.getItem(STORE_KEYS.onboarding) === "1";
  if (REPLAY_MODE || dismissed) {
    card.classList.remove("open");
    return;
  }
  card.classList.add("open");
  const steps = [
    {
      label: "Server reachable",
      detail: state.apiOk ? "Connected to the local API." : "Start it with <code>uvicorn tender.api:app</code>.",
      done: state.apiOk,
    },
    {
      label: "Record at least one run",
      detail:
        state.total > 0
          ? `${state.total} run${state.total === 1 ? "" : "s"} recorded.`
          : "Run an experiment or queue a run below.",
      done: state.total > 0,
    },
    {
      label: "Know the queue path",
      detail:
        'Queue through the API with the <button class="btn small" data-action="copy-code" data-id="setup-queue" data-label="Queue commands">queue commands</button>.',
      done: false,
    },
  ];
  $("#onboard-steps").innerHTML = steps
    .map(
      (step) => `
      <div class="step ${step.done ? "done" : ""}">
        <span class="tick" aria-hidden="true">✓</span>
        <div><strong>${step.label}</strong><span>${step.detail}</span></div>
      </div>`,
    )
    .join("");
}

function renderList() {
  const list = $("#run-list");
  if (state.loading && !state.loadedOnce) {
    list.innerHTML = Array.from({ length: 5 })
      .map(() => `<div class="skeleton-row"><div class="sk w40"></div><div class="sk w95"></div><div class="sk w70"></div></div>`)
      .join("");
    return;
  }
  if (!state.apiOk && !state.loadedOnce) {
    list.innerHTML = `
      <div class="error-card">
        <h3>Could not reach the API</h3>
        <p>${esc(state.apiError || "Start the server and retry.")}</p>
        <div class="empty-actions">
          <button class="btn primary" data-action="refresh">Retry</button>
          <button class="btn" data-action="new-run">Setup commands</button>
        </div>
      </div>`;
    return;
  }
  const rows = visibleRuns();
  if (!rows.length) {
    const hasAny = state.runs.length > 0;
    list.innerHTML = `
      <div class="empty">
        <h3>${hasAny ? "No runs match these filters" : "No runs recorded yet"}</h3>
        <p>${
          hasAny
            ? "Adjust the search or status filters to widen the view."
            : "Tender only shows recorded evidence. Run the pinned fixture, or queue a run through the API and worker."
        }</p>
        <div class="empty-actions">
          ${
            hasAny
              ? `<button class="btn primary" data-action="clear-filters">Clear filters</button>`
              : `<button class="btn primary" data-action="copy-code" data-id="setup-command" data-label="Fixture command">Copy fixture command</button>
                 <button class="btn" data-action="new-run">Setup guide</button>`
          }
          <button class="btn" data-action="refresh">Refresh</button>
        </div>
      </div>`;
    return;
  }
  const compare = state.compare;
  list.innerHTML =
    rows
      .map(
        (run) => `
      <article class="run-row ${state.selectedId === run.id ? "selected" : ""} ${compare.has(run.id) ? "compare-on" : ""}"
               data-action="open-run" data-id="${esc(run.id)}" tabindex="0" role="button"
               aria-label="Open run: ${esc(run.issue?.title || "untitled")}">
        <input class="pick" type="checkbox" data-action="pick" data-id="${esc(run.id)}"
               ${compare.has(run.id) ? "checked" : ""} aria-label="Select for comparison" />
        <div class="run-main">
          <div class="run-title">${esc(run.issue?.title || "Untitled issue")}</div>
          <div class="run-meta">
            <span class="status-chip ${statusClass(run.status)}">${esc(run.status.replace("_", " "))}</span>
            <span class="mono">${esc(run.config?.model || "unknown model")}</span>
            <span>${esc(run.issue?.repository || "local repository")}</span>
            <span>${esc(run.benchmark_id)}</span>
            <span>${esc(relTime(run.created_at))}</span>
            ${run.failure_category && run.failure_category !== "none" ? `<span>${esc(run.failure_category)}</span>` : ""}
          </div>
        </div>
        <div class="run-metrics">
          <div class="metric"><strong>${esc(fmtMs(run.latency_ms))}</strong>latency</div>
          <div class="metric"><strong>${esc(fmtTokens(runTokens(run)))}</strong>tokens</div>
          <div class="metric"><strong>${esc(fmtCost(run.cost_usd))}</strong>cost</div>
          <div class="metric"><strong>${run.attempts || 0}</strong>attempts</div>
        </div>
        <div class="run-actions">
          ${
            run.status === "queued"
              ? `<button class="btn small" data-action="cancel-run" data-id="${esc(run.id)}">Cancel</button>`
              : ""
          }
        </div>
      </article>`,
      )
      .join("") + renderLoadMore(rows.length);

  const selected = rows.find((run) => run.id === state.selectedId);
  if (state.selectedId && !selected) state.selectedId = rows.length ? rows[0].id : null;
}

/** @param {number} visible */
function renderLoadMore(visible) {
  const loadedAll = state.runs.length >= Math.min(state.total, MAX_LIMIT);
  if (loadedAll) {
    if (state.total > MAX_LIMIT && visible > 0) {
      return `<div class="note">Showing the first ${MAX_LIMIT} runs. Narrow the filters or query the API with <code>offset</code> for the rest.</div>`;
    }
    return "";
  }
  return `
    <div class="empty-actions">
      <button class="btn" data-action="load-more">Load more (${state.runs.length} of ${Math.min(state.total, MAX_LIMIT)})</button>
    </div>`;
}

function renderCompareTray() {
  const tray = $("#compare-tray");
  if (!state.compare.size) {
    tray.classList.remove("open");
    return;
  }
  tray.classList.add("open");
  $("#compare-count").textContent = `${state.compare.size} selected for comparison`;
}

/* ==========================================================================
   Drawer
   ========================================================================== */

/** @param {string} id */
async function openRun(id) {
  state.openRunId = id;
  state.selectedId = id;
  state.drawer = { tab: state.drawer.tab || "overview", run: null, events: null, artifact: null, loading: true, error: null };
  syncUrl();
  renderList();
  renderDrawer();
  const backdrop = $("#drawer-backdrop");
  const drawer = $("#drawer");
  backdrop.classList.add("open");
  drawer.classList.add("open");
  const closeButton = document.querySelector("#drawer-close");
  if (closeButton instanceof HTMLElement) closeButton.focus();

  const summary = state.runs.find((run) => run.id === id) || null;
  try {
    const [run, events, artifact] = await Promise.all([
      api.getRun(id),
      api.getEvents(id).catch(() => null),
      api.getArtifact(id).catch(() => null),
    ]);
    if (state.openRunId !== id) return;
    state.drawer.run = run;
    state.drawer.events = events || [];
    state.drawer.artifact = artifact && artifact.available ? artifact.files : [];
  } catch (error) {
    if (state.openRunId !== id) return;
    state.drawer.run = summary;
    state.drawer.error = describeError(error);
  } finally {
    if (state.openRunId === id) {
      state.drawer.loading = false;
      renderDrawer();
    }
  }
}

function closeDrawer() {
  state.openRunId = null;
  state.drawer = { tab: "overview", run: null, events: null, artifact: null, loading: false, error: null };
  $("#drawer").classList.remove("open");
  $("#drawer-backdrop").classList.remove("open");
  syncUrl();
  renderList();
}

function renderDrawer() {
  const content = $("#drawer-content");
  const drawer = state.drawer;
  if (!state.openRunId) {
    content.innerHTML = "";
    return;
  }
  const summary = state.runs.find((run) => run.id === state.openRunId) || null;
  const run = drawer.run || summary;

  if (drawer.error && !drawer.run) {
    content.innerHTML = `
      <div class="drawer-head">
        <div class="titles"><h2>Run unavailable</h2><div class="sub">${esc(drawer.error)}</div></div>
        <button class="icon-btn" id="drawer-close" data-action="drawer-close" aria-label="Close run details">×</button>
      </div>
      <div class="drawer-body"><div class="error-card"><p>${esc(drawer.error)}</p>
      <button class="btn" data-action="drawer-retry">Retry</button></div></div>`;
    return;
  }
  if (!run) {
    content.innerHTML = `
      <div class="drawer-head">
        <div class="titles"><h2>Loading run…</h2></div>
        <button class="icon-btn" id="drawer-close" data-action="drawer-close" aria-label="Close run details">×</button>
      </div>
      <div class="drawer-body">${skeletonDrawer()}</div>`;
    return;
  }

  const status = run.status;
  const tabs = [
    ["overview", "Overview"],
    ["timeline", `Timeline${drawer.events ? ` · ${drawer.events.length}` : ""}`],
    ["patch", "Patch"],
    ["output", "Output"],
    ["artifacts", `Artifacts${drawer.artifact ? ` · ${drawer.artifact.length}` : ""}`],
  ];
  content.innerHTML = `
    <div class="drawer-head">
      <div class="titles">
        <span class="status-chip ${statusClass(status)}">${esc(status.replace("_", " "))}</span>
        <h2>${esc(run.issue?.title || "Untitled issue")}</h2>
        <div class="sub">${esc(run.issue?.repository || "local repository")} · <span class="mono">${esc(run.id)}</span></div>
      </div>
      <button class="icon-btn" id="drawer-close" data-action="drawer-close" aria-label="Close run details">×</button>
    </div>
    <nav class="drawer-tabs" aria-label="Run detail sections">
      ${tabs
        .map(
          ([value, label]) =>
            `<button class="tab ${drawer.tab === value ? "active" : ""}" data-action="drawer-tab" data-tab="${value}">${esc(label)}</button>`,
        )
        .join("")}
    </nav>
    <div class="drawer-body">${
      drawer.loading
        ? skeletonDrawer()
        : drawer.tab === "overview"
          ? renderOverview(run)
          : drawer.tab === "timeline"
            ? renderTimeline()
            : drawer.tab === "patch"
              ? renderPatch(run)
              : drawer.tab === "output"
                ? renderOutput(run)
                : renderArtifacts(run)
    }</div>`;
}

function skeletonDrawer() {
  return `<div class="skeleton-row"><div class="sk w40"></div><div class="sk w95"></div><div class="sk w70"></div></div>
          <div class="skeleton-row"><div class="sk w70"></div><div class="sk w95"></div><div class="sk w40"></div></div>`;
}

/** @param {Run} run */
function renderOverview(run) {
  const metadata = run.metadata || {};
  const kv = [
    ["Benchmark", run.benchmark_id],
    ["Model", run.config?.model || "—"],
    ["Architecture", run.config?.architecture || "—"],
    ["Retrieval", `${run.config?.retrieval || "—"} · limit ${run.config?.retrieval_limit ?? "—"}`],
    ["Sandbox", run.config?.sandbox || "—"],
    ["Created", fmtDate(run.created_at)],
    ["Latency", fmtMs(run.latency_ms)],
    ["Attempts", String(run.attempts || 0)],
    ["Tool calls", fmtInt(run.tool_calls)],
    ["Input tokens", fmtInt(run.input_tokens)],
    ["Output tokens", fmtInt(run.output_tokens)],
    ["Cost", fmtCost(run.cost_usd)],
    ["Failure category", run.failure_category || "none"],
    ["Workflow state", run.current_state || "—"],
    ["Base commit", run.issue?.base_commit ? run.issue.base_commit.slice(0, 10) : "not pinned"],
    ["Workspace", typeof metadata.workspace === "string" ? metadata.workspace : "—"],
  ];
  const interrupted = metadata.interrupted === true;
  const cancelled = metadata.cancelled === true;

  return `
    ${
      interrupted || cancelled
        ? `<div class="note">${interrupted ? "This run was marked interrupted after a service restart; it was not replayed automatically." : "This run was cancelled by an operator before a worker claimed it."}</div>`
        : ""
    }
    <div class="drawer-section">
      <h3>Evaluation</h3>
      <div class="kv-grid">
        ${kv.map(([key, value]) => `<div class="kv"><div class="k">${esc(key)}</div><div class="v">${esc(value)}</div></div>`).join("")}
      </div>
    </div>
    <div class="drawer-section">
      <h3>Issue</h3>
      <div class="note">${esc(run.issue?.body ? run.issue.body.slice(0, 800) : "No issue body recorded.")}</div>
    </div>
    <div class="drawer-section">
      <h3>Quick actions</h3>
      <div class="actions-row">
        ${run.status === "queued" ? `<button class="btn" data-action="cancel-run" data-id="${esc(run.id)}">Cancel run</button>` : ""}
        <button class="btn" data-action="copy" data-copy="${esc(run.id)}" data-label="Run ID">Copy run ID</button>
        <button class="btn" data-action="copy" data-copy="${esc(`curl -s http://127.0.0.1:8000/runs/${run.id}`)}" data-label="cURL">Copy cURL</button>
        <button class="btn" data-action="export-run" data-id="${esc(run.id)}">Export run JSON</button>
        ${run.config?.model ? `<button class="btn ghost" data-action="set-filter" data-group="model" data-value="${esc(run.config.model)}">Filter by model</button>` : ""}
        <button class="btn ghost" data-action="set-filter" data-group="benchmark" data-value="${esc(run.benchmark_id)}">Filter by benchmark</button>
      </div>
    </div>`;
}

function renderTimeline() {
  const events = state.drawer.events;
  if (!events) return `<div class="note">Event timeline loading…</div>`;
  if (!events.length) return `<div class="note">No events recorded for this run.</div>`;
  return events
    .map(
      (event) => `
      <div class="event">
        <strong>${esc(event.name)}</strong>
        <small>${esc(relTime(event.timestamp))} · ${esc(fmtDate(event.timestamp))}</small>
        ${
          event.payload && Object.keys(event.payload).length
            ? `<details><summary>Payload</summary><pre>${esc(JSON.stringify(event.payload, null, 2))}</pre></details>`
            : ""
        }
      </div>`,
    )
    .join("");
}

/** @param {Run} run */
function renderPatch(run) {
  if (!run.patch) return `<div class="note">No patch was recorded for this run.</div>`;
  const lines = run.patch.split("\n");
  const truncated = lines.length > PATCH_RENDER_LIMIT;
  const shown = truncated ? lines.slice(0, PATCH_RENDER_LIMIT) : lines;
  const body = shown
    .map((line) => {
      let cls = "dl-ctx";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")) cls = "dl-meta";
      else if (line.startsWith("@@")) cls = "dl-hunk";
      else if (line.startsWith("+")) cls = "dl-add";
      else if (line.startsWith("-")) cls = "dl-del";
      return `<span class="diff-line ${cls}">${esc(line) || " "}</span>`;
    })
    .join("\n");
  return `
    <div class="drawer-section">
      <h3>Unified diff</h3>
      <pre class="diff">${body}</pre>
      ${truncated ? `<div class="note">Diff truncated at ${PATCH_RENDER_LIMIT} lines for rendering; the recorded artifact keeps the full patch.</div>` : ""}
    </div>`;
}

/** @param {Run} run */
function renderOutput(run) {
  if (!run.test_output) return `<div class="note">No stored test output for this run.</div>`;
  return `
    <div class="drawer-section">
      <h3>Stored test output (bounded)</h3>
      <pre class="code-block">${esc(run.test_output)}</pre>
      <div class="note">Full gate output remains in the run artifacts.</div>
    </div>`;
}

/** @param {Run} run */
function renderArtifacts(run) {
  const files = state.drawer.artifact;
  const path = run.metadata && typeof run.metadata.artifact_path === "string" ? run.metadata.artifact_path : null;
  if (!files || !files.length) {
    return `<div class="note">No artifact files found${path ? ` under <code>${esc(path)}</code>` : ""}. Artifacts are written when the workflow records them.</div>`;
  }
  return `
    <div class="drawer-section">
      <h3>Recorded files</h3>
      <ul class="artifact-list">${files.map((file) => `<li>${esc(file)}</li>`).join("")}</ul>
      ${path ? `<div class="note">Artifact root: <code>${esc(path)}</code></div>` : ""}
      <div class="actions-row"><button class="btn" data-action="copy" data-copy="${esc(files.join("\n"))}" data-label="File list">Copy file list</button></div>
    </div>`;
}

/* ==========================================================================
   Palette
   ========================================================================== */

function openPalette() {
  $("#palette-backdrop").classList.add("open");
  const input = /** @type {HTMLInputElement} */ ($("#palette-input"));
  input.value = "";
  state.palette.index = 0;
  renderPalette();
  input.focus();
}

function closePalette() {
  $("#palette-backdrop").classList.remove("open");
}

function paletteItems() {
  const query = /** @type {HTMLInputElement} */ ($("#palette-input")).value.trim().toLowerCase();
  /** @type {PaletteItem[]} */
  const items = [];
  const actions = [
    { label: "Refresh runs", hint: "r", run: () => refresh() },
    { label: "Toggle light / dark theme", hint: "t", run: toggleTheme },
    { label: "Export current view as JSON", hint: "", run: () => exportRows("json") },
    { label: "Export current view as CSV", hint: "", run: () => exportRows("csv") },
    { label: "Open setup commands", hint: "", run: () => openDialog("setup-backdrop") },
    { label: "Keyboard shortcuts & help", hint: "?", run: () => openDialog("help-backdrop") },
  ];
  const filters = [
    { label: "Filter: passed runs", run: () => setFilter("status", "passed") },
    { label: "Filter: failed runs", run: () => setFilter("status", "failed") },
    { label: "Filter: running runs", run: () => setFilter("status", "running") },
    { label: "Filter: queued runs", run: () => setFilter("status", "queued") },
    { label: "Clear all filters", run: clearFilters },
  ];
  /** @param {string} label */
  const matches = (label) => !query || label.toLowerCase().includes(query);
  for (const action of actions) {
    if (matches(action.label)) items.push({ group: "Actions", label: action.label, hint: action.hint, run: action.run });
  }
  for (const filter of filters) {
    if (matches(filter.label)) items.push({ group: "Commands", label: filter.label, hint: "", run: filter.run });
  }
  for (const run of state.runs) {
    const label = `${run.issue?.title || "Untitled"} — ${run.status}`;
    const haystack = `${label} ${run.id} ${run.config?.model || ""} ${run.issue?.repository || ""} ${run.benchmark_id}`;
    if (query && !haystack.toLowerCase().includes(query)) continue;
    if (items.filter((item) => item.group === "Runs").length >= 6) break;
    items.push({
      group: "Runs",
      label,
      hint: run.config?.model || "",
      run: () => {
        closePalette();
        openRun(run.id);
      },
    });
  }
  return items;
}

function renderPalette() {
  state.palette.items = paletteItems();
  if (state.palette.index >= state.palette.items.length) state.palette.index = 0;
  /** @type {Array<{ group: string, items: Array<PaletteItem & { index: number }> }>} */
  const groups = [];
  let lastGroup = "";
  state.palette.items.forEach((item, index) => {
    if (item.group !== lastGroup) {
      groups.push({ group: item.group, items: [] });
      lastGroup = item.group;
    }
    groups[groups.length - 1].items.push({ ...item, index });
  });
  const results = $("#palette-results");
  if (!state.palette.items.length) {
    results.innerHTML = `<div class="palette-empty">No matches. Try a run title, model, or status.</div>`;
    return;
  }
  results.innerHTML = groups
    .map(
      (group) => `
      <div class="palette-group">${esc(group.group)}</div>
      ${group.items
        .map(
          (item) => `
        <button class="palette-item ${item.index === state.palette.index ? "active" : ""}"
                data-action="palette-run" data-index="${item.index}" role="option" aria-selected="${item.index === state.palette.index}">
          <span>${esc(item.label)}</span>${item.hint ? `<span class="hint">${esc(item.hint)}</span>` : ""}
        </button>`,
        )
        .join("")}`,
    )
    .join("");
}

/** @param {number} index */
function runPaletteIndex(index) {
  const item = state.palette.items[index];
  if (!item) return;
  item.run();
  if (item.group !== "Runs") closePalette();
}

/* ==========================================================================
   Compare & export
   ========================================================================== */

/** @param {string} id */
function toggleCompare(id) {
  if (state.compare.has(id)) state.compare.delete(id);
  else {
    if (state.compare.size >= 4) {
      toast("Compare supports up to 4 runs at a time.", { tone: "error" });
      return;
    }
    state.compare.add(id);
  }
  renderList();
  renderCompareTray();
}

function openCompare() {
  const runs = state.runs.filter((run) => state.compare.has(run.id));
  if (runs.length < 2) {
    toast("Select at least two runs to compare.", { tone: "error" });
    return;
  }
  /** @type {Array<[string, (run: Run) => string]>} */
  const rows = [
    ["Status", (run) => statusChip(run.status)],
    ["Model", (run) => esc(run.config?.model || "—")],
    ["Benchmark", (run) => esc(run.benchmark_id)],
    ["Repository", (run) => esc(run.issue?.repository || "—")],
    ["Latency", (run) => esc(fmtMs(run.latency_ms))],
    ["Tokens (in/out)", (run) => esc(`${fmtInt(run.input_tokens)} / ${fmtInt(run.output_tokens)}`)],
    ["Cost", (run) => esc(fmtCost(run.cost_usd))],
    ["Attempts", (run) => esc(String(run.attempts || 0))],
    ["Tool calls", (run) => esc(fmtInt(run.tool_calls))],
    ["Failure category", (run) => esc(run.failure_category || "none")],
    ["Created", (run) => esc(fmtDate(run.created_at))],
    ["Run ID", (run) => `<span class="mono">${esc(run.id)}</span>`],
  ];
  $("#compare-content").innerHTML = `
    <div class="compare-grid cols-${runs.length}">
      <div class="head rowlabel">Field</div>
      ${runs.map((run) => `<div class="head">${esc(run.issue?.title || "Untitled")}</div>`).join("")}
      ${rows
        .map(
          ([label, render]) =>
            `<div class="rowlabel">${esc(label)}</div>` +
            runs.map((run) => `<div>${render(run)}</div>`).join(""),
        )
        .join("")}
    </div>`;
  openDialog("compare-backdrop");
}

function exportCompare() {
  const runs = state.runs.filter((run) => state.compare.has(run.id));
  if (runs.length < 2) {
    toast("Select at least two runs to compare.", { tone: "error" });
    return;
  }
  const blob = new Blob([JSON.stringify(runs, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `tender-compare-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  toast(`Exported comparison of ${runs.length} runs.`);
}

/** @param {string} status */
function statusChip(status) {
  return `<span class="status-chip ${statusClass(status)}">${esc(status.replace("_", " "))}</span>`;
}

/** @param {"json"|"csv"} format */
function exportRows(format) {
  const rows = visibleRuns();
  if (!rows.length) {
    toast("Nothing to export for the current filters.", { tone: "error" });
    return;
  }
  const payload = rows.map((run) => ({
    id: run.id,
    status: run.status,
    failure_category: run.failure_category,
    title: run.issue?.title || "",
    repository: run.issue?.repository || "",
    model: run.config?.model || "",
    benchmark_id: run.benchmark_id,
    attempts: run.attempts,
    latency_ms: run.latency_ms,
    input_tokens: run.input_tokens,
    output_tokens: run.output_tokens,
    cost_usd: run.cost_usd,
    created_at: run.created_at,
  }));
  const stamp = new Date().toISOString().slice(0, 10);
  let blob;
  if (format === "json") {
    blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  } else {
    const fields = /** @type {string[]} */ (Object.keys(payload[0]));
    const csv = [
      fields.join(","),
      ...payload.map((row) =>
        fields
          .map((field) => {
            const value = /** @type {Record<string, unknown>} */ (row)[field];
            const text = value === null || value === undefined ? "" : String(value);
            return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
          })
          .join(","),
      ),
    ].join("\n");
    blob = new Blob([csv], { type: "text/csv" });
  }
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `tender-runs-${stamp}.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  toast(`Exported ${rows.length} run${rows.length === 1 ? "" : "s"} as ${format.toUpperCase()}.`);
}

/** @param {string} id */
function exportRun(id) {
  const run = state.drawer.run && state.drawer.run.id === id ? state.drawer.run : state.runs.find((item) => item.id === id);
  if (!run) return;
  const blob = new Blob([JSON.stringify(run, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `tender-run-${id.slice(0, 8)}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  toast("Run exported as JSON.");
}

/* ==========================================================================
   Actions
   ========================================================================== */

/**
 * @param {string} text
 * @param {string} label
 */
async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label} copied to clipboard.`);
    return;
  } catch {
    /* fall back to a temporary textarea for non-secure contexts */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.className = "sr-only";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
    toast(`${label} copied to clipboard.`);
  } catch {
    toast("Clipboard unavailable — select the text manually.", { tone: "error" });
  }
  area.remove();
}

let refreshSequence = 0;

/** @param {{ silent?: boolean }} [options] */
async function refresh(options = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  const sequence = ++refreshSequence;
  if (!state.loadedOnce) {
    state.loading = true;
    renderList();
    renderKpis();
  }
  const [runsResult, metricsResult] = await Promise.allSettled([
    api.listRuns({ limit: state.limit }),
    api.getMetrics(),
  ]);
  if (sequence !== refreshSequence) return; // a newer refresh superseded this one
  if (runsResult.status === "fulfilled") {
    state.runs = runsResult.value.runs;
    state.total = runsResult.value.total;
    state.apiOk = true;
    state.apiError = null;
    state.loadedOnce = true;
    state.runsVersion += 1;
  } else {
    state.apiOk = false;
    state.apiError = describeError(runsResult.reason);
  }
  state.metrics = metricsResult.status === "fulfilled" ? metricsResult.value : state.metrics;
  state.refreshing = false;
  state.loading = false;
  state.lastLoaded = Date.now();
  render();
  if (state.openRunId) {
    const updated = state.runs.find((run) => run.id === state.openRunId);
    if (updated && state.drawer.run && updated.status !== state.drawer.run.status) {
      void openRun(state.openRunId); // status changed: refetch full payloads
    }
  }
}

async function loadMore() {
  state.limit = Math.min(MAX_LIMIT, state.limit + PAGE_SIZE);
  await refresh({ silent: true });
}

/** @param {string} id */
async function cancelRun(id) {
  const run = state.runs.find((item) => item.id === id) || state.drawer.run;
  const title = run ? run.issue?.title || id.slice(0, 8) : id.slice(0, 8);
  if (!window.confirm(`Cancel queued run "${title}"? Only unclaimed runs can be cancelled.`)) return;
  try {
    await api.cancelRun(id);
    toast("Run cancelled.");
    await refresh({ silent: true });
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      toast("This run already started or finished — refreshing.", { tone: "error" });
      await refresh({ silent: true });
      return;
    }
    toast(describeError(error), { tone: "error" });
  }
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  applyTheme();
  localStorage.setItem(STORE_KEYS.theme, state.theme);
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  const button = $("#theme-btn");
  button.textContent = state.theme === "dark" ? "☀" : "☾";
  button.title = state.theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
}

/* ==========================================================================
   Dialogs
   ========================================================================== */

/** @param {string} id */
function openDialog(id) {
  closePalette();
  for (const backdrop of document.querySelectorAll(".dialog-backdrop")) backdrop.classList.remove("open");
  $("#" + id).classList.add("open");
  const focusable = document.querySelector(`#${id} .dialog .dialog-actions button`);
  if (focusable instanceof HTMLElement) focusable.focus();
}

function closeDialogs() {
  for (const backdrop of document.querySelectorAll(".dialog-backdrop")) backdrop.classList.remove("open");
}

/* ==========================================================================
   URL & preferences
   ========================================================================== */

function syncUrl() {
  const query = new URLSearchParams();
  const filters = state.filters;
  if (filters.q) query.set("q", filters.q);
  if (filters.status !== "all") query.set("status", filters.status);
  if (filters.model !== "all") query.set("model", filters.model);
  if (filters.benchmark !== "all") query.set("benchmark", filters.benchmark);
  if (filters.sort !== "newest") query.set("sort", filters.sort);
  if (state.openRunId) query.set("run", state.openRunId);
  const suffix = query.toString();
  history.replaceState(null, "", suffix ? `?${suffix}` : location.pathname);
}

function parseUrlState() {
  const query = new URLSearchParams(location.search);
  const filters = state.filters;
  filters.q = query.get("q") || "";
  const status = query.get("status");
  if (status && STATUSES.includes(status)) filters.status = status;
  filters.model = query.get("model") || "all";
  filters.benchmark = query.get("benchmark") || "all";
  const sort = query.get("sort");
  if (sort && SORT_OPTIONS.some(([value]) => value === sort)) filters.sort = sort;
  const run = query.get("run");
  if (run) state.openRunId = run;
}

function saveFilterPrefs() {
  localStorage.setItem(STORE_KEYS.filters, JSON.stringify(state.filters));
}

function loadStoredPrefs() {
  state.theme =
    localStorage.getItem(STORE_KEYS.theme) ||
    (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  try {
    const stored = JSON.parse(localStorage.getItem(STORE_KEYS.filters) || "{}");
    if (stored && typeof stored === "object") {
      const filters = /** @type {Record<string, string>} */ (state.filters);
      for (const key of ["q", "status", "model", "benchmark", "sort"]) {
        if (typeof stored[key] === "string") filters[key] = stored[key];
      }
    }
  } catch {
    /* ignore malformed preferences */
  }
}

/* ==========================================================================
   Polling
   ========================================================================== */

function startPolling() {
  if (REPLAY_MODE) return; // recorded data never changes; polling would only burn requests
  setInterval(() => {
    if (document.hidden || !state.apiOk) return;
    const active = state.runs.some((run) => ["queued", "running", "awaiting_approval"].includes(run.status));
    const interval = active ? POLL_FAST_MS : POLL_SLOW_MS;
    if (Date.now() - state.lastLoaded >= interval) void refresh({ silent: true });
  }, 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - state.lastLoaded > POLL_SLOW_MS) void refresh({ silent: true });
  });
}

/* ==========================================================================
   Events
   ========================================================================== */

function bindEvents() {
  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-action]") : null;
    if (!target) return;
    const action = target.getAttribute("data-action");
    const id = target.getAttribute("data-id") || "";
    switch (action) {
      case "open-run":
        if (event.target instanceof HTMLInputElement) return;
        event.preventDefault();
        void openRun(id);
        break;
      case "cancel-run":
        event.stopPropagation();
        void cancelRun(id);
        break;
      case "drawer-close":
        closeDrawer();
        break;
      case "drawer-retry":
        if (state.openRunId) void openRun(state.openRunId);
        break;
      case "drawer-tab":
        state.drawer.tab = target.getAttribute("data-tab") || "overview";
        renderDrawer();
        break;
      case "set-filter":
        setFilter(
          /** @type {"status"|"model"|"benchmark"|"sort"} */ (target.getAttribute("data-group") || "status"),
          target.getAttribute("data-value") || "all",
        );
        closeDialogs();
        break;
      case "clear-filters":
        clearFilters();
        break;
      case "refresh":
        void refresh();
        break;
      case "load-more":
        void loadMore();
        break;
      case "copy":
        void copyText(target.getAttribute("data-copy") || "", target.getAttribute("data-label") || "Text");
        break;
      case "copy-code": {
        const source = document.getElementById(id);
        if (source) void copyText(source.textContent || "", target.getAttribute("data-label") || "Command");
        break;
      }
      case "export-json":
        exportRows("json");
        break;
      case "export-csv":
        exportRows("csv");
        break;
      case "export-run":
        exportRun(id);
        break;
      case "compare-open":
        openCompare();
        break;
      case "compare-clear":
        state.compare.clear();
        renderList();
        renderCompareTray();
        break;
      case "compare-close":
        closeDialogs();
        break;
      case "compare-export":
        exportCompare();
        break;
      case "palette-run":
        runPaletteIndex(Number(target.getAttribute("data-index")));
        break;
      case "open-palette":
        openPalette();
        break;
      case "close-dialog":
        closeDialogs();
        break;
      case "toggle-theme":
        toggleTheme();
        break;
      case "help":
        openDialog("help-backdrop");
        break;
      case "new-run":
        openDialog("setup-backdrop");
        break;
      case "dismiss-onboarding":
        localStorage.setItem(STORE_KEYS.onboarding, "1");
        renderOnboarding();
        break;
      case "goto": {
        const section = document.getElementById(id);
        if (section) section.scrollIntoView({ behavior: matchesReducedMotion() ? "auto" : "smooth", block: "start" });
        break;
      }
      case "toast-action": {
        const toastId = Number(target.getAttribute("data-toast-id"));
        const entry = state.toasts.find((item) => item.id === toastId);
        if (entry && entry.action) entry.action();
        state.toasts = state.toasts.filter((item) => item.id !== toastId);
        renderToasts();
        break;
      }
      default:
        break;
    }
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || target.getAttribute("data-action") !== "pick") return;
    toggleCompare(target.getAttribute("data-id") || "");
  });

  const search = /** @type {HTMLInputElement} */ ($("#search"));
  search.value = state.filters.q;
  let searchTimer = 0;
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.filters.q = search.value;
      state.selectedId = null;
      saveFilterPrefs();
      syncUrl();
      renderList();
      renderSelects();
    }, 120);
  });

  const modelSelect = /** @type {HTMLSelectElement} */ ($("#model-select"));
  modelSelect.addEventListener("change", () => setFilter("model", modelSelect.value));
  const benchmarkSelect = /** @type {HTMLSelectElement} */ ($("#benchmark-select"));
  benchmarkSelect.addEventListener("change", () => setFilter("benchmark", benchmarkSelect.value));
  const sortSelect = /** @type {HTMLSelectElement} */ ($("#sort-select"));
  sortSelect.addEventListener("change", () => setFilter("sort", sortSelect.value));

  const paletteInput = /** @type {HTMLInputElement} */ ($("#palette-input"));
  paletteInput.addEventListener("input", () => {
    state.palette.index = 0;
    renderPalette();
  });
  paletteInput.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      state.palette.index = Math.min(state.palette.index + 1, state.palette.items.length - 1);
      renderPalette();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      state.palette.index = Math.max(state.palette.index - 1, 0);
      renderPalette();
    } else if (event.key === "Enter") {
      event.preventDefault();
      runPaletteIndex(state.palette.index);
    }
  });

  for (const backdrop of document.querySelectorAll("[data-close-on-backdrop]")) {
    backdrop.addEventListener("click", (event) => {
      if (event.target !== backdrop) return;
      if (backdrop.id === "drawer-backdrop") closeDrawer();
      else if (backdrop.id === "palette-backdrop") closePalette();
      else backdrop.classList.remove("open");
    });
  }

  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
    const paletteOpen = $("#palette-backdrop").classList.contains("open");

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      paletteOpen ? closePalette() : openPalette();
      return;
    }
    if (event.key === "Escape") {
      if (paletteOpen) closePalette();
      else if (document.querySelector(".dialog-backdrop.open")) closeDialogs();
      else if (state.openRunId) closeDrawer();
      return;
    }
    if (typing || paletteOpen) return;

    const rows = visibleRuns();
    const currentIndex = rows.findIndex((run) => run.id === state.selectedId);
    /** @param {number} delta */
    const move = (delta) => {
      event.preventDefault();
      const next = Math.min(Math.max(currentIndex + delta, 0), rows.length - 1);
      if (!rows.length) return;
      state.selectedId = rows[Math.max(next, 0)].id;
      renderList();
      const element = document.querySelector(`.run-row[data-id="${CSS.escape(state.selectedId)}"]`);
      if (element instanceof HTMLElement) element.scrollIntoView({ block: "nearest" });
    };
    switch (event.key) {
      case "/":
        event.preventDefault();
        /** @type {HTMLInputElement} */ ($("#search")).focus();
        break;
      case "j":
        move(1);
        break;
      case "k":
        move(-1);
        break;
      case "Enter":
        if (state.selectedId) {
          event.preventDefault();
          void openRun(state.selectedId);
        }
        break;
      case "x":
        if (state.selectedId) toggleCompare(state.selectedId);
        break;
      case "c":
        if (state.selectedId) void cancelRun(state.selectedId);
        break;
      case "r":
        void refresh();
        break;
      case "t":
        toggleTheme();
        break;
      case "?":
        openDialog("help-backdrop");
        break;
      default:
        break;
    }
  });

  window.addEventListener("popstate", () => {
    state.filters = { q: "", status: "all", model: "all", benchmark: "all", sort: "newest" };
    state.openRunId = null;
    parseUrlState();
    const search = /** @type {HTMLInputElement} */ ($("#search"));
    search.value = state.filters.q;
    render();
    if (state.openRunId) void openRun(state.openRunId);
  });
}

function matchesReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* ==========================================================================
   Init
   ========================================================================== */

function init() {
  loadStoredPrefs();
  parseUrlState();
  applyTheme();
  bindEvents();
  render();
  void refresh();
  startPolling();
  const openId = state.openRunId;
  if (openId) void openRun(openId);
}

init();
