# Retrieval cap experiment v1

**Status: inconclusive. Do not use this run to claim an efficiency gain.**

## Setup

- Compared the existing BM25 retrieval cap of 12 snippets with a cap of 6.
- Used the same model, single-agent workflow, tests, retry budget (zero), and pinned task revisions for both variants.
- Planned 30 matched tasks across five local utility repositories, for 60 trials total.
- A trial counted as solved only when the agent's tests and independent verification both passed.

## What happened

The run recorded all 60 trials, but the provider's free-tier token quota interrupted the experiment. There were 46 rate-limit rejections and three invalid tool-call responses. Those are provider/interface failures, not valid measurements of the agent's task-solving ability. Only five task pairs completed without provider errors, too few for a resume performance claim.

| Metric on the five clean matched pairs | Cap 12 baseline | Cap 6 | Change |
| --- | ---: | ---: | ---: |
| Verified solves | 3/5 (60%) | 3/5 (60%) | No change |
| Mean input tokens | 5,647 | 6,243 | +10.6% |
| Mean output tokens | 645 | 998 | +54.7% |
| Mean total tokens | 6,292 | 7,241 | +15.1% |
| Mean latency | 33.9 s | 37.7 s | +11.1% |
| Estimated token cost per task | $0.000617 | $0.000768 | +24.4% |

Token costs use the rate snapshot in the experiment manifest and are estimates, not provider billing data. These five pairs show no benefit from lowering the cap. They are also too small and synthetic to support a general performance conclusion.

## Conclusion and next run

There is no defensible efficiency-improvement percentage from this experiment. The raw 60-trial pass rate is contaminated by provider errors, and the clean subset is only five pairs. The benchmark reporter now distinguishes provider failures from solve failures and warns when the sample covers fewer than ten repositories. Re-run after provider quota is available, with quota-aware stopping, more repetitions, and a broader task set. Report a percentage only if the paired results show a repeatable reduction while maintaining solve rate and regression checks.
