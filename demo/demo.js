const $ = (selector) => document.querySelector(selector);
const list = $("#trace-list");
const detail = $("#trace-detail");
const caveat = $("#trace-caveat");
let activeTimer = null;
let runData = null;

function renderMetrics(data) {
  const metrics = [
    [data.status, "final result"],
    [`${(data.latency_ms / 1000).toFixed(2)}s`, "workflow latency"],
    [`${data.input_tokens.toLocaleString()} / ${data.output_tokens.toLocaleString()}`, "input / output tokens"],
    [data.reported_cost_usd === null ? "Unknown" : `$${data.reported_cost_usd}`, "reported cost"],
  ];
  const root = $("#trace-metrics");
  for (const [value, label] of metrics) {
    const card = document.createElement("div");
    card.className = "trace-metric";
    const number = document.createElement("b");
    number.textContent = value;
    const caption = document.createElement("span");
    caption.textContent = label;
    card.append(number, caption);
    root.append(card);
  }
  caveat.textContent = `Run ${data.run_id} · ${data.recorded_at} · ${data.attempts} attempt · one pinned task. A successful smoke run does not establish a solve rate or model comparison.`;
}

function showStep(index) {
  if (!runData || index < 0 || index >= runData.steps.length) return;
  for (const [buttonIndex, button] of [...list.children].entries()) {
    button.setAttribute("aria-current", String(buttonIndex === index));
  }
  const step = runData.steps[index];
  detail.replaceChildren();
  const state = document.createElement("span");
  state.className = "status";
  state.textContent = step.state;
  const heading = document.createElement("h3");
  heading.textContent = step.title;
  const summary = document.createElement("p");
  summary.textContent = step.summary;
  const evidence = document.createElement("p");
  evidence.textContent = step.evidence;
  detail.append(state, heading, summary, evidence);
}

function renderSteps(data) {
  for (const [index, step] of data.steps.entries()) {
    const button = document.createElement("button");
    button.className = "trace-step";
    button.type = "button";
    button.textContent = `${String(index + 1).padStart(2, "0")}  ${step.title}`;
    button.setAttribute("aria-current", "false");
    button.addEventListener("click", () => {
      stopPlayback();
      showStep(index);
    });
    list.append(button);
  }
  showStep(0);
}

function stopPlayback() {
  if (activeTimer !== null) window.clearInterval(activeTimer);
  activeTimer = null;
}

$("#play-trace").addEventListener("click", () => {
  if (!runData) return;
  stopPlayback();
  let index = 0;
  showStep(index);
  activeTimer = window.setInterval(() => {
    index += 1;
    if (index >= runData.steps.length) {
      stopPlayback();
      return;
    }
    showStep(index);
  }, 1100);
});

$("#stop-trace").addEventListener("click", stopPlayback);
$("#show-patch").addEventListener("click", () => {
  if (!runData) return;
  stopPlayback();
  detail.replaceChildren();
  const state = document.createElement("span");
  state.className = "status";
  state.textContent = "Verified patch · one file";
  const heading = document.createElement("h3");
  heading.textContent = "Percentage calculation";
  const summary = document.createElement("p");
  summary.textContent = "The agent changed the requested behavior and left the other fixture functions untouched.";
  const code = document.createElement("pre");
  code.className = "patch-view";
  code.textContent = runData.patch;
  detail.append(state, heading, summary, code);
  for (const button of list.children) button.setAttribute("aria-current", "false");
});

fetch("data/verified-run.json")
  .then((response) => {
    if (!response.ok) throw new Error("Run record could not be loaded.");
    return response.json();
  })
  .then((data) => {
    runData = data;
    renderMetrics(data);
    renderSteps(data);
  })
  .catch(() => {
    detail.textContent = "The recorded run is unavailable right now. Try reloading the page.";
    caveat.textContent = "No live backend is contacted by this page.";
  });
