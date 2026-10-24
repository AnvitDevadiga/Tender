/* Static replay shim for the Tender dashboard bundle.
 *
 * The replay page (demo/replay/) loads the real dashboard app.js and styles
 * with this shim installed ahead of the module. Every read route the dashboard
 * uses is answered from records.json — a snapshot captured from the live API
 * (scripts/record_replay.py). Mutating routes answer 409 with an explicit
 * message so the UI's own error handling shows why nothing changed.
 * Fetches of files next to this page (records.json) still use the network. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const recordsPromise = originalFetch("records.json").then((response) => {
    if (!response.ok) throw new Error(`records.json failed with HTTP ${response.status}`);
    return response.json();
  });

  /**
   * @param {number} status
   * @param {object} body
   * @param {Record<string, string>} [headers]
   */
  const respond = (status, body, headers = {}) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "replay",
    headers: {
      /** @param {string} name */
      get: (name) => headers[String(name).toLowerCase()] ?? null,
    },
    json: async () => body,
  });

  const notFound = () => respond(404, { detail: "not found in this recorded replay" });

  window.TENDER_REPLAY = true;
  window.fetch = async (input, init = {}) => {
    const method = String((init && init.method) || "GET").toUpperCase();
    if (method !== "GET") {
      return respond(409, {
        detail:
          "Recorded replay — mutations are disabled on this static page. " +
          "Run Tender locally to queue, cancel, or approve real runs.",
      });
    }
    const url = new URL(String(input), window.location.href);
    const records = await recordsPromise;

    if (url.pathname === "/metrics") return respond(200, records.metrics);
    if (url.pathname === "/ready") return respond(200, { status: "ready", mode: "replay" });
    if (url.pathname === "/reports") return respond(200, records.reports || []);
    const reportMatch = url.pathname.match(/^\/reports\/([^/]+)$/);
    if (reportMatch) {
      const reportId = decodeURIComponent(reportMatch[1]);
      const report = (records.reports || []).find((candidate) => candidate.report_id === reportId);
      return report ? respond(200, report) : notFound();
    }

    const runMatch = url.pathname.match(/^\/runs\/([^/]+)$/);
    if (runMatch) {
      const runId = decodeURIComponent(runMatch[1]);
      const run = records.runs.find((candidate) => candidate.id === runId);
      return run ? respond(200, run) : notFound();
    }
    const eventsMatch = url.pathname.match(/^\/runs\/([^/]+)\/events$/);
    if (eventsMatch) {
      const runId = decodeURIComponent(eventsMatch[1]);
      return runId in records.events ? respond(200, records.events[runId]) : notFound();
    }
    const artifactMatch = url.pathname.match(/^\/runs\/([^/]+)\/artifact$/);
    if (artifactMatch) {
      const runId = decodeURIComponent(artifactMatch[1]);
      const artifact = records.artifacts[runId] || { available: false, files: [] };
      return respond(200, artifact);
    }
    if (url.pathname === "/runs") {
      let runs = records.summaries.slice();
      const status = url.searchParams.get("status");
      if (status) runs = runs.filter((run) => run.status === status);
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || runs.length);
      return respond(200, runs.slice(offset, offset + limit), {
        "x-total-count": String(runs.length),
      });
    }
    return notFound();
  };
})();
