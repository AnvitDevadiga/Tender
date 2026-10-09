# ForgeBench evaluation contract

This document defines what ForgeBench is allowed to claim.

## Required identity

Every run must record:

- issue source and issue text;
- repository and immutable base commit;
- benchmark and dataset version;
- model/provider and configuration;
- agent architecture and retrieval strategy;
- commands executed and their outputs;
- patch artifact;
- timestamps, token counts, tool calls, and cost when available.

An issue without a pinned repository commit is not benchmark-grade. It may be used for local development only.

## Outcome rules

- `passed`: all evaluator-owned tests pass and integrity checks pass.
- `failed`: the run completed but the evaluator did not accept the patch.
- `timeout`: the execution budget expired.
- `tooling`: the environment could not execute the requested evaluation.

Provider output alone never constitutes success. The current offline provider always produces an explicit `tooling` failure because no patch validation has happened.

## Integrity rules

Evaluator-owned tests and benchmark metadata are immutable during agent execution. A patch that edits evaluator tests, disables checks, or changes the benchmark environment is invalid regardless of test output.

## Metrics

Metrics are derived from stored events and artifacts. They must not be manually typed into README files. Any public result must include the experiment manifest, dataset version, raw run artifacts, and the command used to aggregate results. A provider that does not expose pricing must record cost as unknown, never as zero.
