# Current state

Updated 2026-10-24 on branch `feature/tender-platform-v2` (version 0.2.0). The
previous snapshot of this file (2026-10-16) is in Git history.

## Implemented and tested

- Trusted repository boundary, commit-identity enforcement, pristine
  per-check verification, dispatch-time tool authorization, evaluator integrity
  (protected/restored paths, tool shadowing, hidden assets), redirect-safe
  credentialed HTTP.
- Lease-fenced events, artifacts, and final results; expired leases never
  renewed; atomic finalization; whole-database metrics with coverage.
- Separate evaluation outcomes; stage timing and retry counters; run-wide
  deadlines with process-group cancellation; evidence bundles with a verifier.
- Deterministic, committed replay inputs (587 recorded events), rebuilt
  byte-for-byte; provenance labels throughout.
- Experiment runner: trial ordering, exclusions, quota-aware stopping,
  live-provider budget and authorization, Wilson intervals, matched comparisons.
- `tender verify-patch` with reports, Markdown export, evidence, API and
  dashboard views; deterministic failure-and-repair demo.
- Monochrome dashboard, replay, and landing page; keyboard navigation and
  dialog focus management; responsive from 320px to 1440px.
- Packaged dashboard in the wheel; `tender` console script.

## Verified in this environment (2026-10-24)

- Full offline suite, Ruff lint and format, Node unit tests and syntax checks.
- Docker Desktop 29.7.2: sandbox isolation flags and limits, timeout and
  cancellation cleanup, patch evaluation and an agent workflow inside Docker.
  The committed demo reports were produced in Docker.
- Built wheel installed in a clean virtual environment serves `/dashboard/`,
  its assets, and the API from site-packages. The API Docker image (source
  removed after install) serves the same endpoints; the worker image builds and
  runs the CLI.
- API and worker as real processes against the Docker sandbox: a run with a
  mismatched `base_commit` was rejected before execution; a run with the
  correct commit was claimed, executed in Docker with the offline provider
  (which fails at planning, so the outcome is "rejected"), finalized under its
  lease, and published a verifiable evidence bundle; no containers remained.
- Headless Chrome 155: landing page, replay, inspector tabs, reports, compare,
  palette, help, theme persistence, mobile menu, reduced motion; light and dark
  at 320, 390, 768, 1024, and 1440px, with no horizontal overflow.

## Not verified or not implemented

- No live model run was made in this cycle, so no paid API was called.
  The workflow with a real provider was last exercised in the published
  experiments, before these changes.
- `docker compose up` itself was not run; the images and the API + worker
  processes were verified separately (above).
- The GitHub Pages deployment of these changes has not been published.
- No GitHub pull-request workflow, remote repository sources, multi-user
  authentication, signed evidence, or durable workflow resume.
- No project license has been chosen.

## Open risks

- The code under test runs in the same process as the test runner; forged
  results remain possible for an adversarial patch built for that purpose.
- The Docker boundary depends on a privileged daemon; the Compose worker
  mounts the Docker socket (local development only).
