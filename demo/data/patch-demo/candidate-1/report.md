# Tender patch verification: median() is wrong for even-length input

**REJECTED** — Rejected: regression checks failed with the patch applied.

| Property | Result |
| --- | --- |
| Repository | `stats-demo` |
| Base commit | `36c85c7eb48586c4fbd12888858cffbaf77bce94` |
| Patch | `candidate-1.patch` · sha256 `a6916cac33813024` |
| Validation | passed |
| Integrity | passed |
| Baseline | failed |
| Target checks | passed |
| Regression checks | failed |
| Environment | ok (docker) |
| Independently verified | yes |
| Source repository unchanged | True |
| Duration | 3.8 s |

## Why

- Regression check “existing statistics behaviour” failed (failing: tests/test_core.py::test_median_does_not_mutate_input).

## Baseline checks (failed)

- [ ] even-length median (hidden evaluator tests) — exit 1
  - `tests/test_median_target.py::test_median_even_length_averages_middle_values`: failed
  - `tests/test_median_target.py::test_median_even_length_floats`: failed

## Target checks (passed)

- [x] even-length median (hidden evaluator tests) — exit 0
  - `tests/test_median_target.py::test_median_even_length_averages_middle_values`: passed
  - `tests/test_median_target.py::test_median_even_length_floats`: passed

## Regression checks (failed)

- [ ] existing statistics behaviour — exit 1
  - `tests/test_core.py`: failed (tests/test_core.py::test_median_does_not_mutate_input)

## Not established

- Behaviour not exercised by the configured checks. Passing checks do not prove the absence of other defects or regressions.

Evidence manifest sha256 `d8e1395605d909eb44e0fe0cd4fa0495bc55f6b216962b4181d0212edffa4780` (hashed, not signed). Verify with `tender evidence verify <dir>/evidence`.
