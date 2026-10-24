# Evaluation

The authoritative rules are in the [evaluation contract](evaluation-contract.md);
historical interpretation notes are in the [errata](EVALUATION_ERRATA.md).

## Meaning of success (new runs)

`outcome.overall = accepted` requires that the target checks passed in a fresh
workspace at the validated base commit, that integrity passed, and that
regression checks either passed or were explicitly not configured. The
agent-side gates (agent tests, static analysis, diff review) give the agent
feedback; they are not independent evidence. Legacy `status` stays for
compatibility.

## Measurements

Runs record status and separate outcomes, attempts, stage durations, retry
counters, tokens, provider-reported cost (unknown when not reported), tool calls,
failure category, per-check command results, verifier fingerprints, the exact
patch and its digest, and the event stream. `/metrics` aggregates **every**
stored run with SQL and states its coverage. Provider failures are counted
separately, with a pass rate that excludes them.

Experiment records (schema 3) add trial order, exclusions with reasons,
target and regression outcomes, independent final verification, and the
manifest text. Summaries report Wilson 95% intervals over included trials;
comparisons report matched task-level wins and losses with an exact sign test.
These numbers describe the recorded sample only.

## Legacy records

Schema 2 experiment records and runs without `outcome` predate this contract.
Their `regression_free` field means "the independent target check passed on a
clean copy" (erratum E6), and their retrieval label `bm25` names Tender's
lexical retriever (erratum E7).
