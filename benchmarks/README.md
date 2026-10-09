# ForgeBench local benchmark

This is a reproducible local fixture, not a claim about a public GitHub benchmark. Tasks use schema version 2 and pin a full base commit, setup commands, agent test commands, verification commands, and a timeout.

It contains:

- `fixtures/forgecalc`: a small MIT-licensed Git repository;
- `tasks.jsonl`: three issue records with evaluator commands;
- pinned commit fields, replaced after the fixture repository is committed.

The fixture tests are expected to fail at the pinned base commit. They are the evaluator signal the agent must repair; do not run them as part of ForgeBench's own unit-test suite.

Run its tests:

```bash
python3 -m pytest -q benchmarks/fixtures/forgecalc/tests
```

Run ForgeBench against it after installing the package:

```bash
python -m forgebench run-experiment \
  --manifest experiments/retry-ablation-v1.toml \
  --dataset benchmarks/tasks.jsonl \
  --output results/fixture-retry-ablation.json
```
