# Tender local benchmark

This is a reproducible local fixture, not a claim about a public GitHub benchmark. Tasks use schema version 2 and pin a full base commit, setup commands, agent test commands, verification commands, and a timeout.

It contains:

- `fixtures/forgecalc`: a small MIT-licensed Git repository;
- `tasks.jsonl`: three issue records with evaluator commands;
- a deterministic pinned baseline commit, reproducible from the tracked fixture files by `python scripts/prepare_fixtures.py`.

The fixture tests are expected to fail at the pinned base commit. They are the evaluator signal the agent must repair; do not run them as part of Tender's own unit-test suite.

Prepare the fixture Git repository after a fresh clone (CI does this automatically):

```bash
python scripts/prepare_fixtures.py
```

Run its tests:

```bash
python3 -m pytest -q benchmarks/fixtures/forgecalc/tests
```

Run Tender against it after installing the package:

```bash
python -m tender run-experiment \
  --manifest experiments/retry-ablation-v1.toml \
  --dataset benchmarks/tasks.jsonl \
  --output results/fixture-retry-ablation.json
# This manifest calls Groq: add max_provider_calls to a copy of it and pass
# --authorize-live-provider. Without both, Tender refuses to start.
```
