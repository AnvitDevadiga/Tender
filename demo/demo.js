/**
 * Public landing page. Every figure is read at load time from committed JSON:
 * data/site-summary.json (derived by scripts/build_site_data.py, with a source
 * file and JSON path per figure) and the patch-demo reports. Nothing is
 * executed and no number is typed by hand.
 */

import { esc, renderDiff } from "./dashboard/lib.js";

const THEME_KEY = "tender.theme";
const SVG = "http://www.w3.org/2000/svg";
const numbers = new Intl.NumberFormat("en-US");
const REPO = "https://github.com/AnvitDevadiga/Tender/blob/main/";

/* ------------------------------------------------------------------ theme */

function readTheme() {
  try {
    return window.localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

function applyTheme() {
  const theme = readTheme();
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  const button = document.querySelector("[data-theme-toggle]");
  if (button) {
    button.textContent = theme === "dark" ? "Light" : "Dark";
    button.setAttribute("aria-pressed", String(theme === "dark"));
    button.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  }
}

function toggleTheme() {
  try {
    window.localStorage.setItem(THEME_KEY, readTheme() === "dark" ? "light" : "dark");
  } catch {
    /* preference is optional */
  }
  applyTheme();
}

/* ------------------------------------------------------------- navigation */

function setupMenu() {
  const toggle = document.querySelector("[data-menu-toggle]");
  const nav = document.getElementById("site-nav");
  if (!(toggle instanceof HTMLElement) || !nav) return;
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
      toggle.focus();
    }
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

/* ---------------------------------------------------------------- helpers */

const GLYPH = { passed: "✓", accepted: "✓", failed: "✕", rejected: "✕", error: "!" };
const NAMES = {
  accepted: "Accepted",
  rejected: "Rejected",
  passed: "Passed",
  failed: "Failed",
  error: "Error",
  inconclusive: "Inconclusive",
  not_configured: "Not configured",
  not_run: "Not run",
};
const LABELS = { recorded: "Recorded", derived: "Derived", unknown: "Unknown", verified: "Verified" };

/** @param {string} status @param {string} [text] */
function mark(status, text) {
  const key = status || "unknown";
  return `<span class="status ${esc(key)}"><span class="mark" aria-hidden="true">${GLYPH[key] || ""}</span><span class="text">${esc(
    text || NAMES[key] || key,
  )}</span></span>`;
}

/** @param {string} kind */
function label(kind) {
  return `<span class="label ${esc(kind)}">${LABELS[kind] || esc(kind)}</span>`;
}

/** @param {string} path */
function sourceLink(path) {
  return `<a class="source" href="${REPO}${esc(path)}" target="_blank" rel="noreferrer">${esc(path)}</a>`;
}

/** @param {string} url */
async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

/**
 * @param {string} name
 * @param {Record<string, string|number>} [attributes]
 * @param {string} [text]
 */
function svgEl(name, attributes = {}, text) {
  const element = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}

/** Focusable, linked mark with a native tooltip. */
function linked(parent, href, title, ariaLabel) {
  const anchor = svgEl("a", { href, "aria-label": ariaLabel });
  anchor.append(svgEl("title", {}, title));
  parent.append(anchor);
  return anchor;
}

/** @param {number} max */
function niceMax(max) {
  if (max <= 0) return 1;
  const step = tickStep(max, 4);
  return Math.ceil(max / step) * step;
}

/** Round tick step (1, 2, 2.5, or 5 times a power of ten) for about `target` ticks. */
function tickStep(max, target) {
  const raw = max / Math.max(1, target);
  const power = 10 ** Math.floor(Math.log10(raw));
  const multiple = [1, 2, 2.5, 5, 10].find((candidate) => candidate * power >= raw) || 10;
  return multiple * power;
}

function axis(svg, x0, x1, y, max, ticks, unit) {
  svg.append(svgEl("line", { x1: x0, x2: x1, y1: y, y2: y, class: "axis" }));
  const step = tickStep(max, ticks);
  for (let value = 0; value <= max + step / 1000; value += step) {
    const x = x0 + ((x1 - x0) * value) / max;
    svg.append(svgEl("line", { x1: x, x2: x, y1: 8, y2: y, class: "grid" }));
    svg.append(svgEl("text", { x, y: y + 16, "text-anchor": "middle" }, `${numbers.format(Number(value.toFixed(3)))}${unit}`));
  }
}

function chartFrame(container, { title, subtitle, provenance, source, note }) {
  container.innerHTML = `
    <div class="chart-head"><h3>${esc(title)}</h3>${label(provenance)}</div>
    <p class="chart-sub">${esc(subtitle)}</p>
    <div class="chart-legend-slot"></div>
    <div class="chart-svg"></div>
    <p class="chart-note">${esc(note || "")}</p>
    <div class="chart-meta">Source ${sourceLink(source)}</div>
    <details class="chart-data"><summary>Data table</summary><div class="table-wrap"></div></details>`;
  return {
    svgSlot: /** @type {HTMLElement} */ (container.querySelector(".chart-svg")),
    legend: /** @type {HTMLElement} */ (container.querySelector(".chart-legend-slot")),
    table: /** @type {HTMLElement} */ (container.querySelector(".chart-data .table-wrap")),
  };
}

/** @param {string[]} headers @param {(string|number)[][]} rows @param {number[]} [numeric] */
function table(headers, rows, numeric = []) {
  return `<table class="data-table"><thead><tr>${headers
    .map((header, index) => `<th class="${numeric.includes(index) ? "num" : ""}">${esc(header)}</th>`)
    .join("")}</tr></thead><tbody>${rows
    .map(
      (row) =>
        `<tr>${row.map((cell, index) => `<td class="${numeric.includes(index) ? "num" : ""}">${esc(cell)}</td>`).join("")}</tr>`,
    )
    .join("")}</tbody></table>`;
}

/* ---------------------------------------------------------------- charts */

/** Outcome counts per dataset, each bar a share of that dataset's own trials. */
function renderOutcomes(container, data) {
  const labels = Object.fromEntries(data.categories.map((item) => [item.key, item.label]));
  const frame = chartFrame(container, {
    title: "Evaluation outcomes",
    subtitle: "Counts per dataset. Bars show the share of that dataset's trials; datasets are not combined.",
    provenance: "derived",
    source: "demo/data/site-summary.json",
    note: data.outcomes.map((item) => `${item.label}: ${item.note}`).join(" "),
  });
  const draw = () => {
    const width = Math.max(280, frame.svgSlot.clientWidth);
    const labelWidth = Math.min(230, Math.round(width * 0.42));
    const valueWidth = 64;
    const barX = labelWidth + 8;
    const barMax = Math.max(40, width - barX - valueWidth);
    let y = 0;
    const svg = svgEl("svg", { class: "chart", role: "group", "aria-label": "Evaluation outcomes by dataset" });
    for (const dataset of data.outcomes) {
      y += 22;
      svg.append(svgEl("text", { x: 0, y, class: "group-label" }, `${dataset.label} · ${dataset.trials} trials`));
      y += 8;
      for (const [key, count] of Object.entries(dataset.counts)) {
        if (!count) continue;
        y += 24;
        const share = count / dataset.trials;
        const href =
          dataset.id === "patch-demo"
            ? "replay/#/reports"
            : dataset.id === "retry-ablation"
              ? "replay/#/runs?benchmark=groq-retry-ablation-3tasks-v1"
              : `${REPO}${dataset.source}`;
        const anchor = linked(
          svg,
          href,
          `${dataset.label}: ${labels[key]} — ${count} of ${dataset.trials} (${Math.round(share * 100)}%). Source: ${dataset.source}`,
          `${dataset.label}, ${labels[key]}: ${count} of ${dataset.trials}`,
        );
        anchor.append(svgEl("text", { x: 0, y: y - 4 }, labels[key]));
        anchor.append(svgEl("rect", { x: barX, y: y - 15, width: Math.max(2, barMax * share), height: 14, class: `mark ${key === "verified_pass" ? "" : "secondary"}` }));
        anchor.append(svgEl("text", { x: barX + Math.max(2, barMax * share) + 6, y: y - 4, class: "value" }, `${count} of ${dataset.trials}`));
      }
      y += 10;
    }
    svg.setAttribute("viewBox", `0 0 ${width} ${y + 4}`);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(y + 4));
    frame.svgSlot.replaceChildren(svg);
  };
  frame.table.innerHTML = table(
    ["Dataset", ...data.categories.map((item) => item.label), "Trials", "Source"],
    data.outcomes.map((dataset) => [
      dataset.label,
      ...data.categories.map((item) => dataset.counts[item.key]),
      dataset.trials,
      dataset.source,
    ]),
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
  );
  return draw;
}

/** Recorded duration of every trial, grouped by task, with the median marked. */
function renderDurations(container, data) {
  const durations = data.durations;
  const frame = chartFrame(container, {
    title: "Run duration by task",
    subtitle: `${durations.experiment} · ${durations.model}. Each dot is one recorded trial; the bar is the median.`,
    provenance: "recorded",
    source: durations.source,
    note: durations.note,
  });
  frame.legend.innerHTML = `<div class="chart-legend"><span class="dark">Trial (links to its recorded run)</span><span class="bar">Median</span></div>`;
  const all = durations.tasks.flatMap((task) => task.runs.map((run) => run.seconds));
  const max = niceMax(Math.max(...all));
  const draw = () => {
    const width = Math.max(280, frame.svgSlot.clientWidth);
    const left = 104;
    const right = width - 12;
    const rowHeight = 40;
    const height = durations.tasks.length * rowHeight + 34;
    const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Recorded run durations by task" });
    axis(svg, left, right, height - 24, max, width < 420 ? 3 : 4, " s");
    durations.tasks.forEach((task, index) => {
      const y = 14 + index * rowHeight + rowHeight / 2;
      svg.append(svgEl("text", { x: 0, y: y + 4 }, task.task_id));
      const median = left + ((right - left) * task.median_seconds) / max;
      svg.append(svgEl("line", { x1: median, x2: median, y1: y - 11, y2: y + 11, class: "median" }));
      for (const run of task.runs) {
        const x = left + ((right - left) * run.seconds) / max;
        const cost = run.estimated_cost_usd === null ? "unknown" : `$${run.estimated_cost_usd.toFixed(5)} (published-rate estimate)`;
        const anchor = linked(
          svg,
          `replay/#/runs/${run.run_id}`,
          `${task.title}\n${run.seconds} s · ${numbers.format(run.tokens)} tokens · cost ${cost}\nretry budget ${run.retry_budget}, ${run.attempts} attempt · run ${run.run_id.slice(0, 8)}`,
          `${task.task_id} trial: ${run.seconds} seconds`,
        );
        anchor.append(svgEl("circle", { cx: x, cy: y, r: 5, class: "mark", "fill-opacity": 0.85 }));
      }
    });
    frame.svgSlot.replaceChildren(svg);
  };
  frame.table.innerHTML = table(
    ["Task", "Trial seconds", "Median", "Tokens", "Estimated cost (USD)"],
    durations.tasks.flatMap((task) =>
      task.runs.map((run) => [task.task_id, run.seconds, task.median_seconds, run.tokens, run.estimated_cost_usd ?? "unknown"]),
    ),
    [1, 2, 3, 4],
  );
  return draw;
}

/** Matched task pairs: retrieval cap 12 against cap 6. */
function renderPaired(container, data) {
  const paired = data.paired_latency;
  const frame = chartFrame(container, {
    title: "Matched pairs: retrieval cap 12 vs 6",
    subtitle: `${paired.experiment} · ${paired.model}. ${paired.pairs.length} of ${paired.planned_pairs} planned pairs are usable.`,
    provenance: "recorded",
    source: paired.source,
    note: paired.note,
  });
  frame.legend.innerHTML = `<div class="chart-legend"><span class="dark">Cap 12</span><span class="hollow">Cap 6</span></div>`;
  const max = niceMax(Math.max(...paired.pairs.flatMap((pair) => [pair.cap_12.seconds, pair.cap_6.seconds])));
  const draw = () => {
    const width = Math.max(280, frame.svgSlot.clientWidth);
    const left = 124;
    const right = width - 12;
    const rowHeight = 32;
    const height = paired.pairs.length * rowHeight + 34;
    const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Paired run durations" });
    axis(svg, left, right, height - 24, max, width < 420 ? 3 : 4, " s");
    paired.pairs.forEach((pair, index) => {
      const y = 12 + index * rowHeight + rowHeight / 2;
      const x12 = left + ((right - left) * pair.cap_12.seconds) / max;
      const x6 = left + ((right - left) * pair.cap_6.seconds) / max;
      svg.append(svgEl("text", { x: 0, y: y + 4 }, pair.task_id));
      svg.append(svgEl("line", { x1: x12, x2: x6, y1: y, y2: y, class: "connector" }));
      const solved = (/** @type {any} */ run) => (run.passed ? "solved" : "not solved");
      const anchor = linked(
        svg,
        `${REPO}${paired.source}`,
        `${pair.task_id}\ncap 12: ${pair.cap_12.seconds} s, ${solved(pair.cap_12)}\ncap 6: ${pair.cap_6.seconds} s, ${solved(pair.cap_6)}`,
        `${pair.task_id}: cap 12 ${pair.cap_12.seconds} seconds, cap 6 ${pair.cap_6.seconds} seconds`,
      );
      anchor.append(svgEl("circle", { cx: x12, cy: y, r: 5, class: "mark" }));
      anchor.append(svgEl("circle", { cx: x6, cy: y, r: 5, class: "mark hollow" }));
    });
    frame.svgSlot.replaceChildren(svg);
  };
  frame.table.innerHTML = table(
    ["Task", "Cap 12 (s)", "Solved", "Cap 6 (s)", "Solved"],
    paired.pairs.map((pair) => [pair.task_id, pair.cap_12.seconds, pair.cap_12.passed ? "yes" : "no", pair.cap_6.seconds, pair.cap_6.passed ? "yes" : "no"]),
    [1, 3],
  );
  return draw;
}

/** Recorded workflow-state timestamps of one run, drawn as stages on a time axis. */
function renderTimeline(container, data) {
  const timeline = data.timeline;
  const frame = chartFrame(container, {
    title: "Execution timeline of one recorded run",
    subtitle: `“${timeline.title}” · ${timeline.model} · ${timeline.sandbox} sandbox · ${timeline.total_seconds.toFixed(1)} s end to end.`,
    provenance: "recorded",
    source: timeline.source,
    note: timeline.note,
  });
  const max = niceMax(timeline.total_seconds);
  const draw = () => {
    const width = Math.max(280, frame.svgSlot.clientWidth);
    const left = width < 520 ? 118 : 150;
    const right = width - 12;
    const rowHeight = 22;
    const height = timeline.stages.length * rowHeight + 34;
    const svg = svgEl("svg", { class: "chart", width, height, viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Workflow stages over time" });
    axis(svg, left, right, height - 24, max, width < 420 ? 4 : 8, " s");
    timeline.stages.forEach((stage, index) => {
      const y = 6 + index * rowHeight;
      const x0 = left + ((right - left) * stage.start_seconds) / max;
      const x1 = left + ((right - left) * stage.end_seconds) / max;
      const name = stage.state.replaceAll("_", " ");
      const seconds = (stage.end_seconds - stage.start_seconds).toFixed(3);
      svg.append(svgEl("text", { x: 0, y: y + 12 }, name));
      const anchor = linked(
        svg,
        `replay/#/runs/${timeline.run_id}`,
        `${name}: ${stage.start_seconds.toFixed(3)} s → ${stage.end_seconds.toFixed(3)} s (${seconds} s, derived from recorded timestamps)`,
        `${name}, ${seconds} seconds`,
      );
      anchor.append(svgEl("rect", { x: x0, y: y + 3, width: Math.max(2, x1 - x0), height: 12, class: "mark" }));
    });
    frame.svgSlot.replaceChildren(svg);
  };
  frame.table.innerHTML = table(
    ["Stage", "Start (s)", "End (s)", "Duration (s)"],
    timeline.stages.map((stage) => [
      stage.state.replaceAll("_", " "),
      stage.start_seconds,
      stage.end_seconds,
      (stage.end_seconds - stage.start_seconds).toFixed(3),
    ]),
    [1, 2, 3],
  );
  return draw;
}

function renderModels(data) {
  const note = document.getElementById("models-note");
  const container = document.getElementById("models-table");
  if (!note || !container) return;
  note.textContent = data.models.note;
  container.innerHTML = `<table class="data-table"><thead><tr>
      <th>Model</th><th>Publisher</th><th>Provider</th><th>Experiment</th>
      <th class="num">Tasks</th><th class="num">Trials</th><th class="num">Passed</th>
      <th class="num">Provider failures</th><th class="num">Median duration</th><th class="num">Estimated cost</th><th>Source</th>
    </tr></thead><tbody>${data.models.rows
      .map(
        (row) => `<tr>
          <td class="mono">${esc(row.model)}</td>
          <td>${esc(row.model_publisher ?? "unknown")}</td>
          <td>${esc(row.provider)}</td>
          <td class="mono">${esc(row.experiment)}</td>
          <td class="num">${row.unique_tasks}</td>
          <td class="num">${row.trials}</td>
          <td class="num">${row.passed}</td>
          <td class="num">${row.provider_failures}</td>
          <td class="num">${row.median_seconds_excluding_provider_failures === null ? "unknown" : `${row.median_seconds_excluding_provider_failures} s`}</td>
          <td class="num" title="${esc(row.cost_basis)}">${row.estimated_cost_usd === null ? "unknown" : `$${row.estimated_cost_usd.toFixed(4)}*`}</td>
          <td>${sourceLink(row.source)}</td>
        </tr>`,
      )
      .join("")}</tbody></table>`;
  container.insertAdjacentHTML(
    "afterend",
    `<p class="fineprint">* Estimated from published token prices (${esc(data.models.rows[0]?.cost_basis || "")}). Tender has no billing record. Median duration excludes provider failures.</p>`,
  );
}

function renderPreview(data) {
  const root = document.getElementById("report-preview");
  const report = data.hero_report;
  if (!root || !report) return;
  const tests = report.tests
    .map(
      (test) =>
        `<li><span class="group">${esc(test.group)}</span><span class="status ${esc(test.outcome)}"><span class="mark" aria-hidden="true">${GLYPH[test.outcome] || ""}</span><span class="visually-hidden">${esc(NAMES[test.outcome] || test.outcome)}</span></span><span class="id">${esc(test.id)}</span></li>`,
    )
    .join("");
  root.innerHTML = `
    <div class="preview-head">
      <span class="title">Patch verification report</span>
      ${mark(report.overall)} ${report.independently_verified ? label("verified") : ""}
    </div>
    <dl class="preview-facts">
      <dt>Task</dt><dd>${esc(report.task)}</dd>
      <dt>Repository</dt><dd class="mono">${esc(report.repository)} @ ${esc(report.base_commit.slice(0, 12))}</dd>
      <dt>Patch</dt><dd class="mono">${esc(report.patch_file)} · sha256 ${esc(report.patch_sha256.slice(0, 12))}</dd>
      <dt>Executed in</dt><dd>${esc(report.sandbox)} sandbox, fresh workspace per check</dd>
      <dt>Required tests</dt><dd>from ${esc(report.coverage_provenance.replaceAll("_", " "))}</dd>
    </dl>
    <ul class="preview-tests" aria-label="Tests executed">${tests}</ul>
    <div class="preview-foot">
      <span class="mono">evidence ${esc(report.evidence_manifest_sha256.slice(0, 16))}…</span>
      <a href="replay/#/reports/${esc(report.report_id)}">Open the full report</a>
    </div>`;
  root.setAttribute("aria-label", `Recorded patch verification report: ${report.overall}`);
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
  const executed = (group.checks || []).flatMap((check) => check.coverage?.executed || []);
  const expected = (group.checks || []).flatMap((check) => check.coverage?.expected || []);
  const detail = expected.length ? `${executed.length} of ${expected.length} required tests ran` : group.note || "";
  return `<div class="check"><div class="check-head">${mark(group.status)}<span class="what">${esc(name)}</span><span class="facts-inline">${esc(detail)}</span></div></div>`;
}

function renderPatchDemo(reports, active) {
  const root = document.getElementById("patch-demo");
  if (!root) return;
  const report = reports[active];
  const switches = reports
    .map((item, index) => `<button type="button" aria-pressed="${index === active}" data-candidate="${index}">${esc(item.demo_label || `Candidate ${index + 1}`)}</button>`)
    .join("");
  const failed = failingTests(report);
  const why = (report.explanations || []).map((line) => `<li>${esc(line)}</li>`).join("");
  root.innerHTML = `
    <div class="demo-switch"><div class="segmented" role="group" aria-label="Candidate patch">${switches}</div>
      <a class="btn small ghost" href="replay/#/reports/${esc(report.report_id)}">Inspect the full report</a></div>
    <div class="demo-grid">
      <div class="demo-panel">
        <h4>Verdict</h4>
        <div class="verdict ${report.outcome?.overall === "accepted" ? "accepted" : ""}">${mark(report.outcome?.overall)}
          <p>${esc(report.summary || "")}</p></div>
        <h4>Checks, each in a fresh workspace at <span class="mono">${esc(String(report.repository?.base_commit || "").slice(0, 10))}</span></h4>
        <div class="checks">
          <div class="check"><div class="check-head">${mark(report.validation?.passed ? "passed" : "failed")}<span class="what">Patch validation</span></div></div>
          <div class="check"><div class="check-head">${mark(report.integrity?.passed ? "passed" : "failed")}<span class="what">Integrity</span></div></div>
          ${groupRow(report.baseline, "Baseline (before the patch)")}
          ${groupRow(report.target, "Target checks")}
          ${groupRow(report.regression, "Regression checks")}
        </div>
        ${failed.length ? `<p class="callout">Failing test: <code>${esc(failed.join(", "))}</code></p>` : ""}
        ${why ? `<h4>Why</h4><ul class="prose">${why}</ul>` : ""}
      </div>
      <div class="demo-panel"><h4>Patch</h4>${renderDiff(report.patch?.diff || "", { copy: false })}</div>
    </div>`;
}

async function loadPatchDemo() {
  const root = document.getElementById("patch-demo");
  if (!root) return;
  try {
    const index = await getJson("data/patch-demo/index.json");
    const reports = await Promise.all(
      index.reports.map((/** @type {any} */ entry) => getJson(`data/patch-demo/${entry.file}`).then((report) => ({ ...report, demo_label: entry.label }))),
    );
    let active = Math.max(0, reports.findIndex((report) => report.outcome?.overall !== "accepted"));
    renderPatchDemo(reports, active);
    root.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-candidate]") : null;
      if (!button) return;
      active = Number(button.getAttribute("data-candidate"));
      renderPatchDemo(reports, active);
      /** @type {HTMLElement|null} */ (root.querySelector(`[data-candidate="${active}"]`))?.focus();
    });
  } catch {
    root.innerHTML = `<p class="fineprint">The demonstration reports could not be loaded. Generate them with <code>python scripts/run_patch_demo.py</code>.</p>`;
  }
}

async function loadSummary() {
  let data;
  try {
    data = await getJson("data/site-summary.json");
  } catch {
    const preview = document.getElementById("report-preview");
    if (preview) preview.innerHTML = `<figcaption>The recorded report could not be loaded.</figcaption>`;
    const charts = document.getElementById("chart-outcomes");
    if (charts) charts.innerHTML = `<p class="fineprint">The derived results file could not be loaded. Rebuild it with <code>python scripts/build_site_data.py</code>.</p>`;
    return;
  }
  renderPreview(data);
  renderModels(data);
  const charts = [
    ["chart-outcomes", renderOutcomes],
    ["chart-durations", renderDurations],
    ["chart-paired", renderPaired],
    ["chart-timeline", renderTimeline],
  ];
  const drawers = [];
  for (const [id, render] of charts) {
    const container = document.getElementById(/** @type {string} */ (id));
    if (!container) continue;
    const draw = /** @type {Function} */ (render)(container, data);
    drawers.push([container, draw]);
    draw();
  }
  if ("ResizeObserver" in window) {
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => drawers.forEach(([, draw]) => draw()));
    });
    observer.observe(document.getElementById("results") || document.body);
  }
}

/* ------------------------------------------------------------------ init */

applyTheme();
document.querySelector("[data-theme-toggle]")?.addEventListener("click", toggleTheme);
setupMenu();
setupScrollSpy();
loadSummary();
loadPatchDemo(); // two small reports; loaded eagerly so no content waits on scrolling
