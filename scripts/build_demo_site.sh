#!/usr/bin/env bash
# Assemble the static portfolio site exactly as GitHub Pages serves it.
#
# The deploy workflow runs this against _site/; local previews run it against a
# throwaway directory, then serve that directory directly:
#
#     bash scripts/build_demo_site.sh /tmp/tender-site
#     python3 -m http.server 8000 --directory /tmp/tender-site
#
# Layout (site root, prefix-agnostic — Pages hosts this at /Tender/):
#   index.html, styles.css, demo.js     landing page copied from demo/
#   replay/                             read-only dashboard replay (replay.js + records.json)
#   dashboard/app.js, lib.js, theme.js, styles.css  the live dashboard bundle
#   data/                               recorded demo inputs (patch-demo reports, replay inputs)
#   docs/experiments/*.json             published experiment records read by the landing page
set -euo pipefail

target="${1:?usage: build_demo_site.sh <target-dir>}"
rm -rf "$target"
mkdir -p "$target"

cp -R demo/. "$target/"

# The landing page and the replay reuse the live dashboard bundle; keep exactly one copy.
mkdir -p "$target/dashboard"
cp dashboard/app.js dashboard/lib.js dashboard/theme.js dashboard/styles.css "$target/dashboard/"

mkdir -p "$target/docs/experiments"
cp docs/experiments/groq-retry-ablation-3tasks-v1.json "$target/docs/experiments/"
cp docs/experiments/retrieval-cap-ab-v1.json "$target/docs/experiments/"
cp docs/PROVIDER_TRIALS.md "$target/docs/PROVIDER_TRIALS.md"

# GitHub Pages must not run Jekyll over the data files.
touch "$target/.nojekyll"

echo "built static site: $target"
