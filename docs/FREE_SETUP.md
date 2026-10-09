# Free Local Use, API Keys, and Deployment

ForgeBench is the current working project name. It can be explored locally without paying for an AI provider. This guide separates the free engineering checks from real model use and public hosting.

## What this project does

Think of ForgeBench as a workshop with two parts:

- **The robot:** reads a software task, searches a code repository, proposes a patch, and runs tests.
- **The judge and notebook:** checks whether tests pass and records the task, patch, tool activity, time, token use, and cost when available.

The second part matters because a code suggestion by itself does not prove the code works.

## Do I need an API key?

| Activity | API key needed? | What it proves |
|---|---:|---|
| Run the unit tests | No | The ForgeBench code passes its own checks. |
| Validate the pinned fixture dataset | No | The benchmark file and commit pins follow the format. |
| Run the offline deterministic provider | No | The run and failure-recording paths work; it does not show coding ability. |
| Ask Groq to generate a real patch | Yes, `GROQ_API_KEY` | A real model was called. Results depend on model availability and account limits. |
| Import a public GitHub issue | Usually no | Uses the unauthenticated GitHub API limit. |
| Import private issues or get higher GitHub API limits | Yes, `GITHUB_TOKEN` | Gives API access; keep the token private. |

Groq documents creating an API key and placing it in `GROQ_API_KEY` for API calls. Its API documents a free plan with rate limits; the exact limits depend on the account and model and can change. Usage above a free allowance or an upgrade to a paid tier can incur charges. Check the provider console before running experiments, and do not put keys in source files or benchmark repositories. See the [Groq API quickstart](https://console.groq.com/docs/quickstart), [current rate limits](https://console.groq.com/docs/rate-limits), and [billing FAQ](https://console.groq.com/docs/billing-faqs).

## Free local start

Requirements: Python 3.12 or newer, Git, and this repository. Docker is needed only for the API workflow route that runs repository tests in a container. The current machine running the API must have access to a Docker daemon for that route.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python scripts/prepare_fixtures.py
pytest -q
PYTHONPATH=src python3 -m forgebench validate-benchmark \
  --manifest experiments/retry-ablation-v1.toml \
  --dataset benchmarks/tasks.jsonl
```

Those commands do not call Groq and do not create an AI benchmark result. A real provider call is optional and may be subject to provider limits or charges.

## Can I host it for free?

A static portfolio page with empty run states can be hosted without running code tasks. A small API demo may also fit a provider's free web plan, but that is not a safe or complete ForgeBench deployment today.

The local Compose setup includes a separate Docker worker with persistent local SQLite/artifact storage. It mounts the host Docker socket, which is highly privileged and is suitable only for local development. The project still lacks public authentication, quotas, production-grade worker recovery, and stronger isolation. Do not expose anonymous code execution publicly.

Free hosting plans also have limits that conflict with a persistent benchmark service. For example, Render says its free web services sleep after inactivity and lose local filesystem changes on restart/redeploy; its free Postgres database expires after 30 days. That is acceptable for a temporary demo, not reliable long-term run history. Read the [current Render free-plan limits](https://render.com/docs/free) before choosing it. Hosting terms change, so recheck the provider's official limits when deploying.

## Current cost summary

- **Local tests and benchmark-format validation:** no model key and no model usage.
- **Local Docker commands:** no cloud fee, but they use your computer's resources.
- **Real model-generated patches:** require a provider API key and may be limited or billed by the provider.
- **Free public web hosting:** possible for a limited demo; persistent execution, storage, and safe arbitrary-repository tasks are not available as a complete, configured path in the current code.

The project has attempted a real Groq-backed task on the pinned fixture. It reached planning and repository inspection, then stopped at Groq's free-tier token-per-minute limit before producing a patch. Docker images, the sandbox, and the API/worker path have been exercised locally. The current repository suite has 97 passing tests; this test count is not an AI success rate. See [the provider trial record](PROVIDER_TRIALS.md) for the run ID and limits.
