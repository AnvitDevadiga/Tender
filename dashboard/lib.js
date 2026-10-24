/**
 * Shared, dependency-free helpers for the Tender dashboard and the public
 * landing page: HTML escaping and a unified-diff parser/renderer.
 */

const DIFF_LINE_LIMIT = 5000;
const numbers = new Intl.NumberFormat("en-US");

/** @type {Record<string, string>} */
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** @param {unknown} value */
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ESCAPES[character] || character);
}


/** @param {number} value */
function fmtInt(value) {
  return numbers.format(value);
}


/**
 * Parse a unified Git diff into files, hunks, and numbered rows.
 *
 * Hunk bodies are delimited by the line counts in their headers, so a removed
 * line whose content starts with "--" is never mistaken for a file header.
 * @param {string} text
 */
export function parseDiff(text) {
  /** @type {any[]} */
  const files = [];
  /** @type {any} */
  let file = null;
  /** @type {any} */
  let hunk = null;
  let oldLine = 0;
  let newLine = 0;
  let oldLeft = 0;
  let newLeft = 0;
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const startFile = () => {
    file = { oldPath: "", newPath: "", status: "modified", binary: false, hunks: [], additions: 0, deletions: 0, raw: [] };
    files.push(file);
    hunk = null;
  };
  for (const line of lines) {
    if (hunk && (oldLeft > 0 || newLeft > 0)) {
      file.raw.push(line);
      if (line.startsWith("+")) {
        hunk.rows.push({ type: "add", old: "", new: newLine++, text: line.slice(1) });
        file.additions += 1;
        newLeft -= 1;
      } else if (line.startsWith("-")) {
        hunk.rows.push({ type: "del", old: oldLine++, new: "", text: line.slice(1) });
        file.deletions += 1;
        oldLeft -= 1;
      } else if (line.startsWith("\\")) {
        hunk.rows.push({ type: "note", old: "", new: "", text: line.slice(2) });
      } else {
        hunk.rows.push({ type: "ctx", old: oldLine++, new: newLine++, text: line.startsWith(" ") ? line.slice(1) : line });
        oldLeft -= 1;
        newLeft -= 1;
      }
      continue;
    }
    if (line.startsWith("\\") && hunk) {
      file.raw.push(line);
      hunk.rows.push({ type: "note", old: "", new: "", text: line.slice(2) });
      continue;
    }
    if (line.startsWith("diff --git ")) {
      startFile();
      const match = /^diff --git a\/(.+) b\/(.+)$/.exec(line);
      file.oldPath = match?.[1] || "";
      file.newPath = match?.[2] || "";
    } else if (line.startsWith("--- ") && (!file || file.hunks.length)) {
      startFile();
    } else if (!file) {
      startFile();
    }
    file.raw.push(line);
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (header) {
      hunk = { header: line, rows: [] };
      file.hunks.push(hunk);
      oldLine = Number(header[1]);
      oldLeft = header[2] === undefined ? 1 : Number(header[2]);
      newLine = Number(header[3]);
      newLeft = header[4] === undefined ? 1 : Number(header[4]);
      continue;
    }
    if (line.startsWith("new file mode")) file.status = "added";
    else if (line.startsWith("deleted file mode")) file.status = "deleted";
    else if (line.startsWith("rename from ")) {
      file.status = "renamed";
      file.oldPath = line.slice(12);
    } else if (line.startsWith("rename to ")) file.newPath = line.slice(10);
    else if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) file.binary = true;
    else if (line.startsWith("--- ")) {
      const path = line.slice(4).replace(/\t.*$/, "");
      if (path === "/dev/null") file.status = "added";
      else file.oldPath = path.replace(/^a\//, "");
    } else if (line.startsWith("+++ ")) {
      const path = line.slice(4).replace(/\t.*$/, "");
      if (path === "/dev/null") file.status = "deleted";
      else file.newPath = path.replace(/^b\//, "");
    }
  }
  return files;
}

/**
 * Render a diff as accessible tables, one per file, with old/new line numbers
 * and explicit +/- markers. Pass ``copy: false`` where no copy handler exists.
 * @param {string} diff @param {{ id?: string, copy?: boolean }} [options]
 */
export function renderDiff(diff, options = {}) {
  const copy = options.copy !== false;
  if (!diff || !diff.trim()) return `<p class="prose">No patch was recorded.</p>`;
  const files = parseDiff(diff);
  if (!files.length) return `<pre class="json">${esc(diff)}</pre>`;
  const additions = files.reduce((sum, file) => sum + file.additions, 0);
  const deletions = files.reduce((sum, file) => sum + file.deletions, 0);
  let budget = DIFF_LINE_LIMIT;
  const body = files
    .map((file, index) => {
      const path = file.status === "deleted" ? file.oldPath : file.newPath || file.oldPath;
      const rename = file.status === "renamed" ? `${esc(file.oldPath)} → ` : "";
      let rows = "";
      if (file.binary) {
        rows = `<tr class="note"><td class="ln"></td><td class="ln"></td><td class="sign"></td><td class="code">Binary change (not rendered)</td></tr>`;
      } else {
        for (const hunk of file.hunks) {
          rows += `<tr class="hunk"><td class="ln"></td><td class="ln"></td><td class="sign"></td><td class="code">${esc(hunk.header)}</td></tr>`;
          for (const row of hunk.rows) {
            if (budget-- <= 0) break;
            const sign = row.type === "add" ? "+" : row.type === "del" ? "-" : "";
            rows += `<tr class="${row.type}"><td class="ln">${row.old}</td><td class="ln">${row.new}</td><td class="sign">${sign}</td><td class="code">${esc(
              row.text,
            )}</td></tr>`;
          }
        }
        if (budget <= 0) {
          rows += `<tr class="note"><td class="ln"></td><td class="ln"></td><td class="sign"></td><td class="code">Diff truncated after ${fmtInt(
            DIFF_LINE_LIMIT,
          )} lines; export the run for the full patch.</td></tr>`;
        }
      }
      return `<section class="diff-file" aria-label="Changes to ${esc(path)}">
        <div class="diff-file-head">
          <span class="label">${esc(file.status)}</span>
          <span class="path">${rename}${esc(path)}</span>
          <span class="stat">+${file.additions} −${file.deletions}</span>
          ${copy ? `<button class="btn ghost small" type="button" data-action="copy-diff-file" data-diff="${esc(options.id || "")}" data-index="${index}">Copy</button>` : ""}
        </div>
        <div class="diff-scroll"><table class="diff-table"><caption class="visually-hidden">Diff of ${esc(path)}: old line, new line, change, code</caption><tbody>${rows}</tbody></table></div>
      </section>`;
    })
    .join("");
  return `<div class="diff-summary">
      <span>${files.length} file${files.length === 1 ? "" : "s"} changed</span>
      <span class="mono">+${additions} −${deletions}</span>
      ${copy ? `<button class="btn small" type="button" data-action="copy-diff" data-diff="${esc(options.id || "")}">Copy patch</button>` : ""}
    </div>${body}`;
}

