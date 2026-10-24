# Tender patch verification: median() is wrong for even-length input

**ACCEPTED** — Accepted: target checks passed in a fresh workspace at 36c85c7eb4 and regression checks also passed.

| Property | Result |
| --- | --- |
| Repository | `stats-demo` |
| Base commit | `36c85c7eb48586c4fbd12888858cffbaf77bce94` |
| Patch | `candidate-2.patch` · sha256 `ac0dd22091659827` |
| Validation | passed |
| Integrity | passed |
| Baseline | failed |
| Target checks | passed |
| Regression checks | passed |
| Environment | ok (docker) |
| Independently verified | yes |
| Source repository unchanged | True |
| Duration | 2.6 s |

## Baseline checks (failed)

- [ ] even-length median (hidden evaluator tests) — exit 1
  - `tests/test_median_target.py::test_median_even_length_averages_middle_values`: failed
  - `tests/test_median_target.py::test_median_even_length_floats`: failed

## Target checks (passed)

- [x] even-length median (hidden evaluator tests) — exit 0
  - `tests/test_median_target.py::test_median_even_length_averages_middle_values`: passed
  - `tests/test_median_target.py::test_median_even_length_floats`: passed

## Regression checks (passed)

- [x] existing statistics behaviour — exit 0
  - `tests/test_core.py`: passed

## Not established

- Behaviour not exercised by the configured checks. Passing checks do not prove the absence of other defects or regressions.

Evidence manifest sha256 `804ebbf381edbc44a41c3b81b490cbdb25822320b55bda8f85b0f037de05e6a9` (hashed, not signed). Verify with `tender evidence verify <dir>/evidence`.
