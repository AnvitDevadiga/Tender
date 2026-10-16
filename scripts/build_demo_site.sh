#!/usr/bin/env bash
# Assemble the static portfolio site exactly as GitHub Pages serves it.
#
# The deploy workflow runs this against _site/; local previews run it against a
# throwaway directory, then serve that directory directly:
#
#     bash scripts/build_demo_site.sh /tmp/tender-site
#     python3 -m http.server 8000 --directory /tmp/tender-site
#
# Layout (site root, prefix-agnostic — Pages hosts this at /tender/):
#   index.html, styles.css, ...        portfolio page copied from demo/
#   replay/                            recorded console replay (replay.js + records.json)
#   dashboard/app.js, styles.css       the live dashboard bundle, single source of truth
#   assets/dashboard-*.jpg             screenshots embedded by the portfolio page
#   docs/experiments/..., docs/PROVIDER_TRIALS.md
set -euo pipefail

target="${1:?usage: build_demo_site.sh <target-dir>}"
rm -rf "$target"
mkdir -p "$target"

cp -R demo/. "$target/"

# The replay page reuses the live dashboard bundle; keep exactly one copy in the repo.
mkdir -p "$target/dashboard"
cp dashboard/app.js dashboard/styles.css "$target/dashboard/"

mkdir -p "$target/docs/experiments"
cp docs/experiments/groq-retry-ablation-3tasks-v1.json "$target/docs/experiments/"
cp docs/PROVIDER_TRIALS.md "$target/docs/PROVIDER_TRIALS.md"

echo "built static site: $target"
