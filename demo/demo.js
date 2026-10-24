/**
 * Public landing page behaviour. Every number and outcome shown is read from
 * committed JSON at load time: the patch-verification demo reports and the
 * published experiment records. Nothing is executed and nothing is typed by hand.
 */

import { esc, renderDiff } from "./dashboard/lib.js";

const THEME_KEY = "tender.theme";
const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
const numbers = new Intl.NumberFormat("en-US");

/** @param {string} key */
function readStore(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** @param {string} key @param {string} value */
function writeStore(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* optional preference */
  }
}

function theme() {
  const stored = readStore(THEME_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme() {
  const stored = readStore(THEME_KEY);
  if (stored === "light" || stored === "dark") document.documentElement.dataset.theme = stored;
  else delete document.documentElement.dataset.theme;
  const button = document.querySelector("[data-theme-toggle]");
  button?.setAttribute("aria-label", `Switch to ${theme() === "dark" ? "light" : "dark"} theme`);
}

/* ------------------------------------------------------------ navigation */

function setupMenu() {
  const toggle = document.querySelector("[data-menu-toggle]");
  const nav = document.getElementById("site-nav");
  if (!toggle || !nav) return;
  const setOpen = (/** @type {boolean} */ open) => {
    nav.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Close" : "Menu";
  };
  toggle.addEventListener("click", () => setOpen(!nav.classList.contains("open")));
  nav.addEventListener("click", (event) => {
    if (event.target instanceof HTMLAnchorElement) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && nav.classList.contains("open")) {
      setOpen(false);
      /** @type {HTMLElement} */ (toggle).focus();
    }
  });
  window.matchMedia("(min-width: 769px)").addEventListener?.("change", (query) => {
    if (query.matches) setOpen(false);
  });
}

function setupScrollSpy() {
  if (!("IntersectionObserver" in window)) return;
  const links = new Map(
    [...document.querySelectorAll('.site-nav a[href^="#"]')].map((link) => [link.getAttribute("href")?.slice(1), link]),
  );
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        for (const [id, link] of links) {
          if (id === entry.target.id) link.setAttribute("aria-current", "true");
          else link.removeAttribute("aria-current");
        }
      }
    },
    { rootMargin: "-40% 0px -55% 0px" },
  );
  for (const id of links.keys()) {
    const section = id ? document.getElementById(id) : null;
    if (section) observer.observe(section);
  }
}

function setupReveal() {
  if (reducedMotion || !("IntersectionObserver" in window)) return;
  const sections = [...document.querySelectorAll(".band")];
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      }
    },
    { rootMargin: "0px 0px -10% 0px" },
  );
  for (const section of sections) {
    const box = section.getBoundingClientRect();
    if (box.top < window.innerHeight) continue; // already visible: never hide it
    section.classList.add("reveal");
    observer.observe(section);
  }
}

/* ------------------------------------------------------ status helpers */

const GLYPH = { passed: "✓", accepted: "✓", failed: "✕", rejected: "✕", violated: "✕", error: "!" };
const NAMES = {
  accepted: "Accepted",
  rejected: "Rejected",
  passed: "Passed",
  failed: "Failed",
  not_configured: "Not configured",
  not_run: "Not run",
  error: "Error",
  inconclusive: "Inconclusive",
  unknown: "Unknown",
};

/** @param {string} status @param {string} [text] */
function mark(status, text) {
  const key = status || "unknown";
  return `<span class="status ${esc(key)}"><span class="mark" aria-hidden="true">${GLYPH[key] || ""}</span><span class="text">${esc(
    text || NAMES[key] || key,
  )}</span></span>`;
}

/** @param {number|null|undefined} value */
function seconds(value) {
  return value === null || value === undefined ? "—" : `${(value / 1000).toFixed(1)} s`;
}

/** @param {string} url */
async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------ patch demo */

/** @param {any} report */
function failingTests(report) {
  const failed = [];
  for (const group of [report.target, report.regression]) {
    for (const check of group?.checks || []) {
      for (const test of check.tests || []) if (test.outcome !== "passed") failed.push(test.failing_cases || test.id);
    }
  }
  return failed;
}

/** @param {any} group @param {string} name */
function groupRow(group, name) {
  if (!group) return "";
  const tests = (group.checks || []).flatMap((check) => check.tests || []);
  const passed = tests.filter((test) => test.outcome === "passed").length;
  const detail = tests.length ? `${passed} of ${tests.length} named tests passed` : group.note || "";
  return `<div class="check"><div class="check-head">${mark(group.status)}<span class="what">${esc(name)}</span><span class="facts-inline">${esc(detail)}</span></div></div>`;
}

/** @param {any[]} reports @param {number} active */
function renderPatchDemo(reports, active) {
  const root = document.getElementById("patch-demo");
  if (!root) return;
  const report = reports[active];
  const switches = reports
    .map(
      (item, index) => `<button type="button" aria-pressed="${index === active}" data-candidate="${index}">${esc(item.demo_label || `Candidate ${index + 1}`)}</button>`,
    )
    .join("");
  const failed = failingTests(report);
  const why = (report.explanations || []).map((line) => `<li>${esc(line)}</li>`).join("");
  root.innerHTML = `
    <div class="demo-switch"><div class="segmented" role="group" aria-label="Candidate patch">${switches}</div>
      <a class="btn small ghost" href="replay/#/reports/${esc(report.report_id)}">Inspect the full report</a></div>
    <div class="demo-grid">
      <div class="demo-panel">
        <div class="demo-task"><strong>${esc(report.task?.title || "")}</strong>${esc(report.task?.description || "")}</div>
        <h3>Verdict</h3>
        <div class="verdict ${report.outcome?.overall === "accepted" ? "accepted" : ""}">${mark(report.outcome?.overall)}
          <p>${esc(report.summary || "")}</p></div>
        <h3>Checks, each in a fresh workspace at <span class="mono">${esc(String(report.repository?.base_commit || "").slice(0, 10))}</span></h3>
        <div class="checks">
          <div class="check"><div class="check-head">${mark(report.validation?.passed ? "passed" : "failed")}<span class="what">Patch validation</span></div></div>
          <div class="check"><div class="check-head">${mark(report.integrity?.passed ? "passed" : "failed")}<span class="what">Integrity</span></div></div>
          ${groupRow(report.baseline, "Baseline (before the patch)")}
          ${groupRow(report.target, "Target checks")}
          ${groupRow(report.regression, "Regression checks")}
        </div>
        ${failed.length ? `<p class="callout">Failing test: <code>${esc(failed.join(", "))}</code></p>` : ""}
        ${why ? `<h3>Why</h3><ul class="prose">${why}</ul>` : ""}
        <p class="fineprint">Patch <code>${esc(String(report.patch?.sha256 || "").slice(0, 16))}</code> · evidence manifest <code>${esc(
          String(report.evidence?.manifest_sha256 || "").slice(0, 16),
        )}</code> · ${esc(report.environment?.sandbox?.backend || "")} sandbox</p>
      </div>
      <div class="demo-panel"><h3>Patch</h3>${renderDiff(report.patch?.diff || "", { copy: false })}</div>
    </div>`;
}

/** @param {any[]} reports */
function renderHeroProof(reports) {
  const root = document.getElementById("hero-proof");
  if (!root || !reports.length) return;
  root.innerHTML = `<div class="proof" aria-label="Recorded demonstration outcomes">${reports
    .map(
      (report, index) => `<div>
        <span class="what">${esc(report.demo_label || `Candidate ${index + 1}`)} · recorded</span>
        ${mark(report.outcome?.overall)}
        <p>${esc(report.summary || "")}</p>
        <span class="mono">patch ${esc(String(report.patch?.sha256 || "").slice(0, 12))} · base ${esc(String(report.repository?.base_commit || "").slice(0, 10))}</span>
      </div>`,
    )
    .join("")}</div>`;
}

async function loadPatchDemo() {
  const root = document.getElementById("patch-demo");
  try {
    const index = await getJson("data/patch-demo/index.json");
    const reports = await Promise.all(index.reports.map((/** @type {any} */ entry) => getJson(`data/patch-demo/${entry.file}`).then((report) => ({ ...report, demo_label: entry.label }))));
    let active = Math.max(0, reports.findIndex((report) => report.outcome?.overall !== "accepted"));
    renderHeroProof(reports);
    renderPatchDemo(reports, active);
    root?.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-candidate]") : null;
      if (!button) return;
      active = Number(button.getAttribute("data-candidate"));
      renderPatchDemo(reports, active);
      /** @type {HTMLElement|null} */ (root.querySelector(`[data-candidate="${active}"]`))?.focus();
    });
  } catch {
    if (root) root.innerHTML = `<p class="fineprint">The demonstration reports are not available. Generate them with <code>python scripts/run_patch_demo.py</code>.</p>`;
  }
}

/* --------------------------------------------------------------- results */

/** @param {[string, string, string][]} cells */
function metricRow(cells) {
  return `<dl class="metrics">${cells.map(([name, value, foot]) => `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd><span class="foot">${esc(foot)}</span></div>`).join("")}</dl>`;
}

async function loadResults() {
  const root = document.getElementById("results-data");
  if (!root) return;
  try {
    const [ablation, retrieval] = await Promise.all([
      getJson("docs/experiments/groq-retry-ablation-3tasks-v1.json"),
      getJson("docs/experiments/retrieval-cap-ab-v1.json"),
    ]);
    const a = ablation.summary;
    const firstAttempt = ablation.runs.filter((run) => run.attempts === 1).length;
    const r = retrieval.summary;
    const byTask = new Map();
    for (const run of retrieval.runs) {
      if (!byTask.has(run.task_id)) byTask.set(run.task_id, []);
      byTask.get(run.task_id).push(run);
    }
    const clean = [...byTask.values()].filter((runs) => runs.length === 2 && runs.every((run) => run.failure_category !== "provider_error"));
    const cleanPassed = (/** @type {number} */ limit) => clean.filter((runs) => runs.find((run) => run.retrieval_limit === limit)?.passed).length;
    root.innerHTML = `
      <article class="experiment">
        <header><h3>Retry ablation</h3><span class="mono">${esc(ablation.manifest.model)} · ${esc(ablation.manifest.name)}</span></header>
        ${metricRow([
          ["Trials passed", `${a.passed} / ${a.runs}`, `${a.unique_tasks} synthetic tasks, ${ablation.manifest.repetitions} repeats × ${ablation.manifest.retry_budgets.length} budgets`],
          ["First-attempt passes", `${firstAttempt} / ${a.runs}`, "retries were never exercised"],
          ["Mean run time", seconds(a.average_latency_ms), "end-to-end, recorded"],
          ["Tokens", numbers.format(a.total_input_tokens + a.total_output_tokens), `estimate $${a.total_published_rate_cost_estimate_usd.toFixed(4)} at published rates; no bill`],
        ])}
        <p class="reading">Every trial passed on its first attempt, so this run says nothing about whether retries help. Its independent check re-ran target assertions on a clean copy; regression preservation was not measured separately (<a href="https://github.com/AnvitDevadiga/Tender/blob/main/docs/EVALUATION_ERRATA.md">erratum E6</a>). Repeats of three tasks are not independent samples. <a href="docs/experiments/groq-retry-ablation-3tasks-v1.json">Raw records</a></p>
      </article>
      <article class="experiment">
        <header><h3>Retrieval cap A/B</h3><span class="mono">${esc(retrieval.manifest.model)} · caps ${esc(retrieval.manifest.retrieval_limits.join(" vs "))}</span></header>
        ${metricRow([
          ["Planned trials", numbers.format(r.runs), `${r.unique_tasks} matched tasks, ${r.unique_repositories} repositories`],
          ["Provider failures", numbers.format(r.provider_failures), "quota and tool-call errors; not coding failures"],
          ["Clean matched pairs", numbers.format(clean.length), "both variants free of provider errors"],
          ["Solved in clean pairs", `${cleanPassed(12)} vs ${cleanPassed(6)}`, "cap 12 vs cap 6; no difference"],
        ])}
        <p class="reading">Provider quota dominated this run. Five clean pairs cannot support an efficiency or capability claim, and the published report says so. New experiments stop early on repeated provider failures and exclude them from solve rates. <a href="docs/experiments/retrieval-cap-ab-v1.json">Raw records</a></p>
      </article>`;
  } catch {
    root.innerHTML = `<p class="fineprint">The published experiment files could not be loaded. They are in <code>docs/experiments/</code> in the repository.</p>`;
  }
}

/* ------------------------------------------------------------------ init */

applyTheme();
document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
  writeStore(THEME_KEY, theme() === "dark" ? "light" : "dark");
  applyTheme();
});
setupMenu();
setupScrollSpy();
setupReveal();
loadPatchDemo();
loadResults();
