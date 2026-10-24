# Patch verification

`tender verify-patch` evaluates one existing patch against one pinned commit of a
local repository, using acceptance criteria you write before the patch runs.

```bash
tender verify-patch --repo PATH --base COMMIT --patch FILE --criteria FILE --out DIR \
  [--sandbox docker|local] [--trust-local-execution] [--python CMD] [--name NAME]
```

| Exit | Meaning |
| --- | --- |
| 0 | accepted |
| 1 | rejected (a check failed or the patch violated policy) |
| 2 | not evaluated (error, inconclusive, or refused inputs) |

## What happens

1. **Read the repository without trusting it.** Tender copies `.git` as bytes
   into a private directory, replaces its config, drops hooks, and resolves
   `--base` there. Git never runs against your repository's own metadata, and
   nothing in your working tree is written. Uncommitted and ignored files are
   never used.
2. **Load criteria and hidden tests** before looking at the patch. Their digests
   go into the report.
3. **Validate the patch statically:** size, UTF-8, diff syntax and hunk
   counts, file count, traversal and absolute paths, `.git` paths, symlinks,
   submodules, binary policy, and protected, evaluator-owned, or tool-shadowing
   paths. Finally `git apply --check` runs against a fresh copy of the base. Any
   failure rejects the patch and nothing executes.
4. **Baseline:** run the target checks on the unpatched base in a fresh
   workspace. If they already pass, the report says they do not show the patch
   is needed.
5. **Target checks:** apply the patch in a new workspace, reset
   `restored_paths` to the base commit, add hidden tests, run setup and target
   checks.
6. **Regression checks:** the same procedure in another new workspace.
7. **Report:** `report.json`, `report.md`, and `evidence/` (hash manifest).
   Tender re-observes the source repository and records whether it changed.

Every workspace is deleted afterwards. With `--sandbox docker` (the default)
commands run in a network-disabled container (see [Security](SECURITY.md)). Set
`TENDER_SANDBOX_IMAGE=tender-sandbox:local` so `pytest` is available. If the
Docker daemon is unreachable the evaluation **fails closed**: the report records
`environment: sandbox_unavailable` and nothing runs.

`--sandbox local` runs the patch directly on your machine. It is refused
unless you also pass `--trust-local-execution`, and the report then records
`isolation: none`. Use it only for patches you wrote or fully trust.

## Criteria file

```toml
schema_version = 1

[task]
title = "median() is wrong for even-length input"
description = "For an even number of values return the mean of the two middle values."

setup = []                      # optional, e.g. ["python3 -m pip install -e ."] (no network in Docker)

[[target]]                      # at least one
name = "even-length median"
pytest = ["tests/test_median_target.py::test_median_even_length_averages_middle_values"]

[[regression]]                  # optional; without it regression is "not configured"
name = "existing behaviour"
pytest = ["tests/test_core.py"]

[[regression]]
name = "type check"
command = "python3 -m mypy stats"   # a plain command: exit status only

[integrity]
hidden_tests = "hidden"          # directory beside the criteria file, outside the repository
protected_paths = [".tender/**"] # touching these rejects the patch
restored_paths = ["tests/**", "conftest.py", "**/conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"]
reject_tool_shadowing = true

[constraints]
timeout_seconds = 120            # per command
deadline_seconds = 900           # whole evaluation
max_patch_bytes = 1048576
max_files = 100
allow_binary = false
check_baseline = true
python = "python3"
```

**`pytest` checks are stronger than `command` checks.** Tender runs pytest with
`-o addopts=`, writes a JUnit report, and requires every selector to match at
least one test case, with all matched cases passed. A selector that matches
nothing is `missing`, and the check fails even if the exit status was 0. A file
selector lists any failing test cases by name.

## Reading the report

| Field | Meaning |
| --- | --- |
| `outcome.overall` | accepted / rejected / error / inconclusive |
| `outcome.target`, `outcome.regression` | passed / failed / not_configured / not_run / error |
| `outcome.integrity` | passed / violated |
| `outcome.independently_verified` | target checks passed in a pristine workspace at the base commit |
| `baseline.status` | `failed` is what you want: the checks detect the bug without the patch |
| `integrity.restored_from_base`, `removed_patch_additions` | evaluator files the patch changed, reset for verification |
| `explanations` | why the result is what it is, including failing test IDs |
| `unknowns` | what the report does not establish |
| `evidence.manifest_sha256` | digest of `evidence/manifest.json`; check with `tender evidence verify` |

A pass means the configured checks passed. It does not prove the absence of
other defects. The code under test runs inside the same process as pytest, so a
patch built specifically to forge results could still mislead a check. The reset
files, the shadowing rules, and the JUnit requirement close the common vectors.

## Viewing reports

Point the API at a directory of reports and open the dashboard's **Reports** view:

```bash
TENDER_REPORT_ROOT=reports uvicorn tender.api:app
```

`GET /reports` lists summaries (newest first) and `GET /reports/{id}` returns a
full report. `tender report render report.json` prints the Markdown summary.
Command output in reports and evidence is sanitized (secret-like strings and host
paths are redacted). The patch itself is kept byte for byte so its digest
matches, so review it before sharing a report publicly.

## Pull requests (future)

GitHub pull request evaluation is not implemented. A safe path would fetch the
PR's patch as data through the API, without credentials for public repositories.
Tender would verify it with `verify-patch` against a locally cloned, trusted base,
in Docker only, and would never write back to GitHub automatically.
