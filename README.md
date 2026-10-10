<div align="center">
  <img src="assets/tender-wordmark-transparent.png" width="360" alt="tender.">
  <p><strong>Give coding agents a task. Keep the proof.</strong></p>
  <p><a href="https://anvitdevadiga.github.io/tender/">Live demo</a> · <a href="https://github.com/AnvitDevadiga/tender">Source</a></p>
</div>

Tender is a local-first platform for evaluating coding agents. It gives an agent a repository task, runs its proposed changes in a controlled workspace, checks the result, and records what happened.

![Tender evaluation flow](assets/tender-architecture-transparent.png)

## What it records

- Whether the task passed and whether existing behavior regressed
- The patch, test results, run timeline, and failure category
- Runtime, token counts, and provider-reported cost when available
- Task, repository revision, and experiment configuration for reproducibility

## Try it locally

Requirements: Python 3.12 or newer. Docker is needed for isolated Docker runs. A model API key is only needed for real hosted-model calls; the offline test suite and benchmark validation work without one.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python3 scripts/prepare_fixtures.py
python -m tender validate-benchmark \
  --manifest experiments/retry-ablation-v1.toml \
  --dataset benchmarks/tasks.jsonl
```

To use Groq, set `GROQ_API_KEY` in your local environment, then run `python -m tender check-groq`. Never commit `.env` or API keys.

Start the local API and run explorer:

```bash
uvicorn tender.api:app --reload
```

Open [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard). For Docker-backed queued runs, see [Local deployment](docs/DEPLOYMENT.md) and [Security](docs/SECURITY.md). The API is a development prototype; it is not a public arbitrary-code execution service.

## Live demo and current evidence

The [public demo](https://anvitdevadiga.github.io/tender/) replays a recorded run. It does not accept tasks or execute submitted code. The current experiment contains 18 trials over three small fixture tasks. It is useful for demonstrating the evaluation pipeline, but too small to support broad solve-rate claims. Cost is a published-rate estimate, not a billing record. The archived result retains the hashes from its original run; the current benchmark metadata was rebranded and repinned, so those historical hashes will differ from today’s task file. Read the [trial notes](docs/PROVIDER_TRIALS.md) and [raw results](docs/experiments/groq-retry-ablation-3tasks-v1.json).

## Project map

- `src/tender/` — API, workflow, providers, sandbox, persistence, and benchmark runner
- `benchmarks/` and `experiments/` — pinned tasks and experiment configurations
- `dashboard/` — local run explorer
- `demo/` — read-only public portfolio demo
- `docs/` — architecture, evaluation contract, security, and operating notes
- `assets/` — monochrome brand and diagram assets (SVG and transparent PNG)

## Checks

```bash
ruff check src tests
ruff format --check src tests
pytest -q
```

Tender is an evolving research prototype. See [the implementation plan](docs/IMPLEMENTATION_PLAN.md) for remaining work and [the evaluation contract](docs/evaluation-contract.md) for what the measurements mean.
