// Run with: node --test tests/js/diff.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { esc, parseDiff, renderDiff } from "../../dashboard/lib.js";

test("hunk bodies are delimited by header counts", () => {
  const files = parseDiff(
    [
      "diff --git a/app.sql b/app.sql",
      "--- a/app.sql",
      "+++ b/app.sql",
      "@@ -1,2 +1,2 @@",
      "--- old comment",
      "+-- new comment",
      " select 1;",
      "--- /dev/null",
      "+++ b/new.txt",
      "@@ -0,0 +1 @@",
      "+hello",
      "",
    ].join("\n"),
  );
  assert.equal(files.length, 2);
  assert.deepEqual(files[0].hunks[0].rows.map((row) => [row.type, row.old, row.new, row.text]), [
    ["del", 1, "", "-- old comment"],
    ["add", "", 1, "-- new comment"],
    ["ctx", 2, 2, "select 1;"],
  ]);
  assert.equal(files[1].status, "added");
  assert.equal(files[1].newPath, "new.txt");
});

test("rendered diff escapes code and keeps explicit markers without blank rows", () => {
  const html = renderDiff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-<b>\n+&\n", { copy: false });
  assert.ok(html.includes("&lt;b&gt;") && html.includes("&amp;"));
  assert.ok(!html.includes("<b>"));
  assert.equal((html.match(/<tr class="(add|del|ctx)"/g) || []).length, 2);
  assert.ok(!html.includes('data-action="copy-diff"'));
  assert.equal(esc(`"'`), "&quot;&#39;");
});
