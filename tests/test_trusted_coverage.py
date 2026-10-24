"""Required test coverage must not be controllable by the candidate (found at 1027b54).

Expected pytest identities were collected inside the patched workspace, so a
candidate could shrink a parametrized case list (CASES = [1, 2] -> [1]) and be
accepted without fixing case 2. Required coverage now comes from
evaluator-controlled sources, and JUnit reports must be internally consistent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tender.integrity import IntegrityPolicy
from tender.patch_eval import (
    AcceptanceCriteria,
    CheckSpec,
    _static_test_ids,
    evaluate_patch,
    junit_node_ids,
    reconcile_pytest_results,
)

T = "tests/test_app.py"
MOD = "tests.test_app"


def suite(cases: str, **declared: int) -> str:
    totals = {
        "tests": cases.count("<testcase"),
        "failures": cases.count("<failure"),
        "errors": cases.count("<error"),
        "skipped": cases.count("<skipped"),
    }
    totals.update(declared)
    attributes = " ".join(f'{key}="{value}"' for key, value in totals.items())
    return f"<testsuites><testsuite {attributes}>{cases}</testsuite></testsuites>"


def case(name: str, body: str = "", classname: str = MOD) -> str:
    return f'<testcase classname="{classname}" name="{name}" file="{T}">{body}</testcase>'


# --------------------------------------------------- JUnit report consistency


def test_declared_totals_must_match_the_records_present() -> None:
    """Astra: two declared tests and one failure, but only one passing case."""
    report = suite(case("test_a"), tests=2, failures=1)
    _, problems = junit_node_ids(report)
    assert "declares tests=2 but contains 1" in " ".join(problems)
    assert "declares failures=1 but contains 0" in " ".join(problems)
    outcomes, problems, _ = reconcile_pytest_results(
        [f"{T}::test_a"], [f"{T}::test_a"], 0, report, required_ids=[f"{T}::test_a"]
    )
    assert problems  # the check fails even though the only case shown passed


@pytest.mark.parametrize(
    ("report", "fragment"),
    [
        (suite(case("test_a"), errors=1), "errors=1"),
        (suite(case("test_a"), skipped=1), "skipped=1"),
        ("<testsuites><testsuite>" + case("test_a") + "</testsuite></testsuites>", "test count"),
        (
            '<testsuites tests="3">' + suite(case("test_a")).removeprefix("<testsuites>"),
            "its suites contain 1",
        ),
        ("<testsuites>" + case("test_a") + "</testsuites>", "outside any test suite"),
    ],
)
def test_inconsistent_report_totals_are_rejected(report, fragment) -> None:
    _, problems = junit_node_ids(report)
    assert any(fragment in problem for problem in problems), problems


def test_duplicate_collected_identities_are_rejected_not_deduplicated() -> None:
    node = f"{T}::test_a"
    _, problems, coverage = reconcile_pytest_results(
        [node], [node, node], 0, suite(case("test_a")), required_ids=[node]
    )
    assert "duplicate test IDs" in " ".join(problems)
    assert coverage["duplicate_collected"] == [node]


def test_duplicate_required_identities_are_rejected() -> None:
    node = f"{T}::test_a"
    _, problems, _ = reconcile_pytest_results(
        [node], [node], 0, suite(case("test_a")), required_ids=[node, node]
    )
    assert "duplicate test IDs" in " ".join(problems)


def test_missing_and_skipped_required_tests_fail() -> None:
    required = [f"{T}::test_p[1]", f"{T}::test_p[2]"]
    missing, problems, coverage = reconcile_pytest_results(
        [f"{T}::test_p"], required, 0, suite(case("test_p[1]")), required_ids=required
    )
    assert missing[0]["outcome"] == "missing" and coverage["missing_required"] == [required[1]]
    skipped, _, _ = reconcile_pytest_results(
        [f"{T}::test_p"],
        required,
        0,
        suite(case("test_p[1]") + case("test_p[2]", '<skipped message="x"/>')),
        required_ids=required,
    )
    assert skipped[0]["outcome"] == "skipped"


# ------------------------------------------------ trusted required coverage


def test_candidate_collection_cannot_shrink_required_coverage() -> None:
    required = [f"{T}::test_double[1]", f"{T}::test_double[2]"]
    candidate = [f"{T}::test_double[1]"]
    outcomes, problems, coverage = reconcile_pytest_results(
        [f"{T}::test_double"], candidate, 0, suite(case("test_double[1]")), required_ids=required
    )
    assert outcomes[0]["outcome"] == "missing"
    assert coverage["not_collected_by_candidate"] == [required[1]]
    assert coverage["missing_required"] == [required[1]]
    assert "no longer collects required tests" in " ".join(problems)


def test_extra_candidate_cases_are_recorded_and_must_pass() -> None:
    required = [f"{T}::test_double[1]"]
    candidate = [f"{T}::test_double[1]", f"{T}::test_double[3]"]
    passing = suite(case("test_double[1]") + case("test_double[3]"))
    failing = suite(case("test_double[1]") + case("test_double[3]", '<failure message="x"/>'))
    ok, ok_problems, coverage = reconcile_pytest_results(
        [f"{T}::test_double"], candidate, 0, passing, required_ids=required
    )
    bad, _, _ = reconcile_pytest_results(
        [f"{T}::test_double"], candidate, 0, failing, required_ids=required
    )
    assert ok[0]["outcome"] == "passed" and not ok_problems
    assert coverage["extra_candidate_cases"] == [f"{T}::test_double[3]"]
    assert bad[0]["outcome"] == "failed"


def test_unavailable_trusted_coverage_fails_closed() -> None:
    _, problems, _ = reconcile_pytest_results(
        [f"{T}::test_a"], [f"{T}::test_a"], 0, suite(case("test_a")), required_ids=None
    )
    assert "trusted expected coverage is unavailable" in problems[0]


def test_criteria_reject_overlapping_or_inconsistent_selectors() -> None:
    with pytest.raises(ValidationError, match="overlapping"):
        CheckSpec(name="x", pytest=[T, f"{T}::test_a"])
    with pytest.raises(ValidationError, match="outside its selectors"):
        CheckSpec(name="x", pytest=[f"{T}::TestA"], expected_tests=[f"{T}::TestB::test_a"])
    with pytest.raises(ValidationError, match="duplicate"):
        CheckSpec(name="x", pytest=[T], expected_tests=[f"{T}::test_a", f"{T}::test_a"])
    CheckSpec(name="ok", pytest=[f"{T}::TestA", f"{T}::TestB"], expected_tests=[f"{T}::TestA::t"])


def test_static_enumeration_is_conservative(tmp_path) -> None:
    criteria = AcceptanceCriteria.model_validate(
        {"task": {"title": "t"}, "target": [{"name": "t", "pytest": [T]}]}
    )
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / T
    test_file.write_text(
        "def test_a():\n    pass\n\n\nclass TestB:\n    def test_b(self):\n        pass\n\n"
        "    class TestC:\n        def test_c(self):\n            pass\n\n\n"
        "def helper():\n    pass\n"
    )
    ids, _ = _static_test_ids(tmp_path, [T], criteria, None)
    assert ids == [f"{T}::test_a", f"{T}::TestB::test_b", f"{T}::TestB::TestC::test_c"]

    test_file.write_text(
        "import pytest\n\n\n@pytest.mark.parametrize('n', [1])\ndef test_a(n):\n    pass\n"
    )
    assert _static_test_ids(tmp_path, [T], criteria, None)[0] is None
    test_file.write_text("class TestB(Base):\n    def test_b(self):\n        pass\n")
    assert _static_test_ids(tmp_path, [T], criteria, None)[0] is None
    test_file.write_text("def test_a():\n    pass\n")
    (tmp_path / "conftest.py").write_text("def pytest_generate_tests(metafunc):\n    pass\n")
    assert _static_test_ids(tmp_path, [T], criteria, None)[0] is None
    (tmp_path / "conftest.py").unlink()
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npython_functions = "x*"\n')
    assert _static_test_ids(tmp_path, [T], criteria, None)[0] is None
    (tmp_path / "pyproject.toml").unlink()
    untrusted = criteria.model_copy(
        update={"integrity": criteria.integrity.model_copy(update={"restored_paths": []})}
    )
    assert "not evaluator-owned" in _static_test_ids(tmp_path, [T], untrusted, None)[1]
    assert IntegrityPolicy().restored_paths  # defaults unchanged by this test


# ------------------------------------------------- real repository scenarios


APP = "CASES = [1, 2]\n\n\ndef double(n):\n    return n * 2 if n != 2 else 5\n"
PARAM_TESTS = (
    "import pytest\n\nfrom app import CASES, double\n\n\n"
    "@pytest.mark.parametrize('n', CASES)\ndef test_double(n):\n    assert double(n) == n * 2\n\n\n"
    "class TestPlain:\n    def test_one(self):\n        assert double(1) == 2\n"
)


def build(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    for name, content in {
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["."]\n',
        **files,
    }.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, commit


def patch_for(repo: Path, edits: dict[str, str]) -> str:
    for name, content in edits.items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-N", "."], cwd=repo, check=True)
    diff = subprocess.run(
        ["git", "diff"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    subprocess.run(["git", "reset", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, check=True)
    subprocess.run(["git", "clean", "-qfd"], cwd=repo, check=True)
    return diff


def run_eval(
    repo, commit, patch, checks: str, tmp_path, name, hidden: dict | None = None, backend="local"
):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "fix.patch").write_text(patch, encoding="utf-8")
    integrity = ""
    if hidden:
        for path, content in hidden.items():
            target = folder / "hidden" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        integrity = '[integrity]\nhidden_tests = "hidden"\n'
    (folder / "criteria.toml").write_text(
        f'schema_version = 1\n[task]\ntitle = "{name}"\n{checks}{integrity}', encoding="utf-8"
    )
    return evaluate_patch(
        repo,
        commit,
        folder / "fix.patch",
        folder / "criteria.toml",
        folder / "out",
        sandbox=backend,
        trust_local_execution=backend == "local",
        python=sys.executable if backend == "local" else None,
    )


TARGET_PARAM = f'[[target]]\nname = "double"\npytest = ["{T}::test_double"]\n'


def test_shrinking_parametrized_cases_is_rejected(tmp_path) -> None:
    repo, commit = build(tmp_path, {"app.py": APP, T: PARAM_TESTS})
    shrink = patch_for(repo, {"app.py": APP.replace("[1, 2]", "[1]")})

    report = run_eval(repo, commit, shrink, TARGET_PARAM, tmp_path, "shrink")

    check = report["target"]["checks"][0]
    assert report["outcome"]["overall"] == "rejected"
    assert report["outcome"]["independently_verified"] is False
    assert check["coverage"]["provenance"] == "base_commit_collection"
    assert check["coverage"]["missing_required"] == [f"{T}::test_double[2]"]
    assert report["coverage"]["target[0]"]["expected"] == [
        f"{T}::test_double[1]",
        f"{T}::test_double[2]",
    ]
    assert "removed required tests" in " ".join(report["explanations"])


def test_legitimate_fixes_with_complete_coverage_are_accepted(tmp_path) -> None:
    repo, commit = build(tmp_path, {"app.py": APP, T: PARAM_TESTS})
    fix = patch_for(repo, {"app.py": APP.replace(" if n != 2 else 5", "")})
    grow = patch_for(
        repo, {"app.py": APP.replace("[1, 2]", "[1, 2, 3]").replace(" if n != 2 else 5", "")}
    )
    selectors = (
        f'[[target]]\nname = "file"\npytest = ["{T}"]\n'
        f'[[regression]]\nname = "class"\npytest = ["{T}::TestPlain"]\n'
        f'[[regression]]\nname = "exact"\npytest = ["{T}::test_double[1]"]\n'
    )

    fixed = run_eval(repo, commit, fix, selectors, tmp_path, "fixed")
    grown = run_eval(repo, commit, grow, TARGET_PARAM, tmp_path, "grown")

    assert fixed["outcome"]["overall"] == "accepted"
    assert fixed["outcome"]["independently_verified"] is True
    assert [check["tests"][0]["outcome"] for check in fixed["regression"]["checks"]] == [
        "passed",
        "passed",
    ]
    assert grown["outcome"]["overall"] == "accepted"
    assert grown["target"]["checks"][0]["coverage"]["extra_candidate_cases"] == [
        f"{T}::test_double[3]"
    ]


NEW_FEATURE_TEST = "from app import triple\n\n\ndef test_triple():\n    assert triple(2) == 6\n"
NEW_PARAM_TEST = (
    "import pytest\n\nfrom app import TRIPLES, triple\n\n\n"
    "@pytest.mark.parametrize('n', TRIPLES)\ndef test_triple(n):\n    assert triple(n) == n * 3\n"
)
TRIPLE = APP + "\n\nTRIPLES = [1, 2]\n\n\ndef triple(n):\n    return n * 3\n"


def test_new_feature_tests_use_static_evaluator_coverage(tmp_path) -> None:
    """Base collection fails (triple does not exist yet); evaluator source is enumerated."""
    repo, commit = build(tmp_path, {"app.py": APP})
    patch = patch_for(repo, {"app.py": TRIPLE})
    target = '[[target]]\nname = "triple"\npytest = ["tests/test_triple.py"]\n'

    report = run_eval(
        repo,
        commit,
        patch,
        target,
        tmp_path,
        "static",
        hidden={"tests/test_triple.py": NEW_FEATURE_TEST},
    )

    assert report["outcome"]["overall"] == "accepted"
    coverage = report["target"]["checks"][0]["coverage"]
    assert coverage["provenance"] == "evaluator_source_static"
    assert coverage["expected"] == ["tests/test_triple.py::test_triple"]


def test_untrustworthy_new_parametrized_coverage_fails_closed_or_needs_authorization(
    tmp_path,
) -> None:
    repo, commit = build(tmp_path, {"app.py": APP})
    patch = patch_for(repo, {"app.py": TRIPLE})
    hidden = {"tests/test_triple.py": NEW_PARAM_TEST}
    base = '[[target]]\nname = "triple"\npytest = ["tests/test_triple.py"]\n'
    declared = base + (
        'expected_tests = ["tests/test_triple.py::test_triple[1]", '
        '"tests/test_triple.py::test_triple[2]"]\n'
    )
    authorized = base + 'coverage = "candidate"\n'

    closed = run_eval(repo, commit, patch, base, tmp_path, "closed", hidden=hidden)
    explicit = run_eval(repo, commit, patch, declared, tmp_path, "declared", hidden=hidden)
    trusted_candidate = run_eval(repo, commit, patch, authorized, tmp_path, "auth", hidden=hidden)

    assert closed["outcome"]["overall"] == "error"
    assert closed["outcome"]["independently_verified"] is False
    reason = closed["target"]["checks"][0]["coverage"]["unavailable_reason"]
    assert "base commit cannot be collected" in reason and "expected_tests" in reason
    assert explicit["outcome"]["overall"] == "accepted"
    assert explicit["target"]["checks"][0]["coverage"]["provenance"] == "evaluator_declared"
    # Explicitly authorized candidate coverage is shown, but never presented as an
    # independently verified acceptance.
    assert trusted_candidate["outcome"]["target"] == "passed"
    assert trusted_candidate["outcome"]["independently_verified"] is False
    assert trusted_candidate["outcome"]["overall"] == "inconclusive"
    assert "not established independently" in trusted_candidate["summary"]
    assert any("candidate's own collection" in item for item in trusted_candidate["unknowns"])

    shrunk = patch_for(repo, {"app.py": TRIPLE.replace("TRIPLES = [1, 2]", "TRIPLES = [1]")})
    declared_shrunk = run_eval(
        repo, commit, shrunk, declared, tmp_path, "declared-shrunk", hidden=hidden
    )
    assert declared_shrunk["outcome"]["overall"] == "rejected"


def _report_tamper(fix_totals: bool) -> str:
    """Application code that deletes the failing case from the JUnit file at exit."""
    totals = (
        "        text = re.sub(r'tests=\"\\d+\"', 'tests=\"1\"', text)\n"
        "        text = re.sub(r'failures=\"\\d+\"', 'failures=\"0\"', text)\n"
        if fix_totals
        else ""
    )
    return (
        "import atexit, glob, re\n\n\n"
        "def _hide_failure():\n"
        "    for path in glob.glob('.tender-junit-*.xml'):\n"
        "        text = open(path).read()\n"
        "        pattern = r'<testcase[^>]*test_double\\[2\\].*?</testcase>'\n"
        "        text = re.sub(pattern, '', text, flags=re.S)\n"
        f"{totals}"
        "        open(path, 'w').write(text)\n\n\n"
        "atexit.register(_hide_failure)\n" + APP
    )


@pytest.mark.parametrize("fix_totals", [False, True], ids=["stale-totals", "forged-totals"])
def test_candidate_rewriting_the_junit_report_is_rejected(tmp_path, fix_totals) -> None:
    repo, commit = build(tmp_path, {"app.py": APP, T: PARAM_TESTS})
    tamper = patch_for(repo, {"app.py": _report_tamper(fix_totals)})

    report = run_eval(repo, commit, tamper, TARGET_PARAM, tmp_path, "tamper")

    check = report["target"]["checks"][0]
    assert report["outcome"]["overall"] == "rejected"
    assert check["coverage"]["missing_required"] == [f"{T}::test_double[2]"]
    problems = " ".join(check.get("report_problems", []))
    if not fix_totals:
        assert "declares tests=2 but contains 1" in problems
