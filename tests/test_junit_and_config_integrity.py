"""Regression tests for two evaluator correctness defects found after 259f0cc.

1. JUnit results were matched by file and bare test name, so a passing
   TestA.test_required could satisfy a selector for TestB.test_required.
2. pyproject.toml was restored as a whole file, discarding candidate
   application configuration: a broken runtime-config patch could be accepted
   and a valid configuration fix rejected.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tender.integrity import EvaluatorConfigError, IntegrityPolicy, restore_evaluator_sections
from tender.models import Issue
from tender.patch_eval import evaluate_patch, junit_node_ids, reconcile_pytest_results
from tender.workflow import AgentWorkflow

F = "tests/test_classes.py"


def xml(*cases: str) -> str:
    return (
        '<?xml version="1.0"?>\n<testsuites><testsuite name="pytest">'
        + "".join(cases)
        + ("</testsuite></testsuites>")
    )


def case(classname: str, name: str, file: str = F, body: str = "") -> str:
    return f'<testcase classname="{classname}" name="{name}" file="{file}">{body}</testcase>'


MOD = "tests.test_classes"


# ------------------------------------------------------- synthetic JUnit XML


def test_result_for_one_class_never_satisfies_another_class() -> None:
    collected = [f"{F}::TestA::test_required", f"{F}::TestB::test_required"]
    report = xml(case(f"{MOD}.TestA", "test_required"))

    outcomes, problems = reconcile_pytest_results(
        [f"{F}::TestB::test_required"], collected, 0, report
    )

    assert outcomes[0]["outcome"] == "missing"
    assert "JUnit report lists tests that were not selected" in problems[0]


def test_class_selector_requires_every_collected_method() -> None:
    collected = [f"{F}::TestA::test_one", f"{F}::TestA::test_two", f"{F}::TestB::test_one"]
    complete = xml(case(f"{MOD}.TestA", "test_one"), case(f"{MOD}.TestA", "test_two"))
    partial = xml(case(f"{MOD}.TestA", "test_one"))

    ok, ok_problems = reconcile_pytest_results([f"{F}::TestA"], collected, 0, complete)
    bad, _ = reconcile_pytest_results([f"{F}::TestA"], collected, 0, partial)

    assert ok == [{"id": f"{F}::TestA", "outcome": "passed", "cases": "2"}] and not ok_problems
    assert bad[0]["outcome"] == "missing"
    assert bad[0]["failing_cases"] == f"{F}::TestA::test_two"


def test_parameter_identity_is_preserved() -> None:
    collected = [f"{F}::test_p[a]", f"{F}::test_p[b]"]
    report = xml(
        case(MOD, "test_p[a]"),
        case(MOD, "test_p[b]", body='<failure message="boom"/>'),
    )

    function, _ = reconcile_pytest_results([f"{F}::test_p"], collected, 0, report)
    exact, _ = reconcile_pytest_results([f"{F}::test_p[a]"], collected, 0, report)

    assert function[0]["outcome"] == "failed"
    assert function[0]["failing_cases"] == f"{F}::test_p[b]"
    assert exact[0]["outcome"] == "passed"


def test_nested_classes_rebuild_full_node_ids() -> None:
    cases, problems = junit_node_ids(xml(case(f"{MOD}.TestOuter.TestInner", "test_x")))
    assert cases == [(f"{F}::TestOuter::TestInner::test_x", "passed")] and not problems


def test_duplicate_results_are_ambiguous() -> None:
    collected = [f"{F}::TestA::test_required"]
    report = xml(case(f"{MOD}.TestA", "test_required"), case(f"{MOD}.TestA", "test_required"))
    outcomes, _ = reconcile_pytest_results([f"{F}::TestA::test_required"], collected, 0, report)
    assert outcomes[0]["outcome"] == "ambiguous"


@pytest.mark.parametrize(
    ("report", "problem"),
    [
        (xml(case("other.module.TestA", "test_required")), "does not belong"),
        (xml('<testcase classname="x" name="test_required"/>'), "without file"),
        ("<testsuites><testsuite>", "not valid XML"),
        ("<html></html>", "unexpected root"),
        (xml(), "no test cases"),
        ("", "no JUnit report"),
    ],
)
def test_inconsistent_malformed_or_empty_reports_fail(report, problem) -> None:
    outcomes, problems = reconcile_pytest_results(
        [f"{F}::TestA::test_required"], [f"{F}::TestA::test_required"], 0, report
    )
    assert any(problem in item for item in problems), problems
    assert outcomes[0]["outcome"] != "passed"


def test_skipped_required_test_is_not_a_pass() -> None:
    report = xml(case(f"{MOD}.TestA", "test_required", body='<skipped message="nope"/>'))
    outcomes, problems = reconcile_pytest_results(
        [f"{F}::TestA"], [f"{F}::TestA::test_required"], 0, report
    )
    assert outcomes[0]["outcome"] == "skipped" and not problems


def test_failed_or_missing_collection_fails_closed() -> None:
    report = xml(case(f"{MOD}.TestA", "test_required"))
    selector = [f"{F}::TestA::test_required"]
    _, missing = reconcile_pytest_results(selector, None, None, report)
    _, failed = reconcile_pytest_results(selector, [selector[0]], 2, report)
    nothing, _ = reconcile_pytest_results(selector, [], 0, report)
    assert "collection output is missing" in missing[0]
    assert "collection failed" in failed[0]
    assert nothing[0]["outcome"] == "missing"


# --------------------------------------------------------- real repositories


def build_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_patch(repo: Path, edits: dict[str, str]) -> str:
    """Produce a real Git diff for edits (path -> new content), then reset the repo."""
    for name, content in edits.items():
        (repo / name).write_text(content, encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, check=True)
    return diff


def evaluate(repo: Path, commit: str, patch: str, criteria: str, tmp_path: Path, name: str):
    (tmp_path / f"{name}.patch").write_text(patch, encoding="utf-8")
    (tmp_path / f"{name}.toml").write_text(criteria, encoding="utf-8")
    return evaluate_patch(
        repo,
        commit,
        tmp_path / f"{name}.patch",
        tmp_path / f"{name}.toml",
        tmp_path / f"{name}-out",
        sandbox="local",
        trust_local_execution=True,
        python=sys.executable,
    )


CLASS_APP = {
    "shapes.py": "def area_a():\n    return 1\n\n\ndef area_b():\n    return 1\n",
    "tests/test_classes.py": (
        "from shapes import area_a, area_b\n\n\n"
        "class TestA:\n    def test_required(self):\n        assert area_a() == 1\n\n\n"
        "class TestB:\n    def test_required(self):\n        assert area_b() == 2\n"
    ),
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["."]\n',
}
CLASS_CRITERIA = (
    'schema_version = 1\n[task]\ntitle = "area_b"\n'
    '[[target]]\nname = "TestB"\npytest = ["tests/test_classes.py::TestB"]\n'
)


def test_class_selector_passes_only_when_that_class_passes(tmp_path) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(repo, CLASS_APP)
    fix_b = make_patch(
        repo,
        {
            "shapes.py": CLASS_APP["shapes.py"]
            .replace("return 1\n", "return 2\n", 2)
            .replace("return 2\n", "return 1\n", 1)
        },
    )
    touch_a = make_patch(
        repo,
        {"shapes.py": CLASS_APP["shapes.py"].replace("return 1\n", "return 1  # unrelated\n", 1)},
    )

    fixed = evaluate(repo, commit, fix_b, CLASS_CRITERIA, tmp_path, "fixed")
    unrelated = evaluate(repo, commit, touch_a, CLASS_CRITERIA, tmp_path, "unrelated")

    assert fixed["outcome"]["overall"] == "accepted"
    tests = fixed["target"]["checks"][0]["tests"]
    assert tests == [{"id": "tests/test_classes.py::TestB", "outcome": "passed", "cases": "1"}]
    assert fixed["baseline"]["status"] == "failed"
    # TestA::test_required passes in both runs; it must not satisfy the TestB selector.
    assert unrelated["outcome"]["overall"] == "rejected"
    assert unrelated["target"]["checks"][0]["tests"][0]["outcome"] == "failed"


CONFIG_APP = {
    "app.py": (
        "import tomllib\nfrom pathlib import Path\n\n\n"
        "def greeting():\n"
        '    data = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())\n'
        '    return data["tool"]["demoapp"]["greeting"]\n\n\n'
        "def shout(text):\n    return text.upper()\n"
    ),
    "pyproject.toml": (
        '[project]\nname = "demoapp"\nversion = "0.1.0"\n\n'
        '[tool.demoapp]\ngreeting = "hi"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
    ),
    "tests/test_app.py": (
        "from app import greeting, shout\n\n\n"
        'def test_greeting_is_hello():\n    assert greeting() == "hello"\n\n\n'
        'def test_greeting_is_unchanged():\n    assert greeting() == "hi"\n\n\n'
        'def test_shout():\n    assert shout("a") == "A"\n'
    ),
}
HELLO_PYPROJECT = CONFIG_APP["pyproject.toml"].replace('greeting = "hi"', 'greeting = "hello"')


def test_valid_runtime_config_fix_is_accepted_not_falsely_rejected(tmp_path) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(repo, CONFIG_APP)
    criteria = (
        'schema_version = 1\n[task]\ntitle = "say hello"\n'
        '[[target]]\nname = "hello"\npytest = ["tests/test_app.py::test_greeting_is_hello"]\n'
        '[[regression]]\nname = "shout"\npytest = ["tests/test_app.py::test_shout"]\n'
    )

    patch = make_patch(repo, {"pyproject.toml": HELLO_PYPROJECT})
    report = evaluate(repo, commit, patch, criteria, tmp_path, "hello")

    assert report["outcome"]["overall"] == "accepted"
    assert report["outcome"]["independently_verified"] is True
    assert report["integrity"]["restored_from_base"] == []
    assert report["integrity"]["tested_as_submitted"] is True
    assert report["integrity"]["evaluator_sections"] == [
        {"path": "pyproject.toml", "section": "tool.pytest", "action": "unchanged"}
    ]


def test_broken_runtime_config_is_rejected_not_falsely_accepted(tmp_path) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(repo, CONFIG_APP)
    criteria = (
        'schema_version = 1\n[task]\ntitle = "keep greeting"\n'
        '[[target]]\nname = "shout"\npytest = ["tests/test_app.py::test_shout"]\n'
        '[[regression]]\nname = "greeting"\n'
        'pytest = ["tests/test_app.py::test_greeting_is_unchanged"]\n'
    )

    patch = make_patch(repo, {"pyproject.toml": HELLO_PYPROJECT})
    report = evaluate(repo, commit, patch, criteria, tmp_path, "broken")

    assert report["outcome"]["target"] == "passed"
    assert report["outcome"]["regression"] == "failed"
    assert report["outcome"]["overall"] == "rejected"


def test_candidate_test_runner_settings_are_reset_but_application_config_is_kept(
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(repo, CONFIG_APP)
    patch = make_patch(
        repo,
        {
            "pyproject.toml": HELLO_PYPROJECT
            + 'addopts = "--deselect tests/test_app.py::test_greeting_is_hello"\n'
        },
    )
    criteria = (
        'schema_version = 1\n[task]\ntitle = "say hello"\n'
        '[[target]]\nname = "hello"\npytest = ["tests/test_app.py::test_greeting_is_hello"]\n'
    )

    report = evaluate(repo, commit, patch, criteria, tmp_path, "addopts")

    assert report["outcome"]["overall"] == "accepted"  # application value tested as submitted
    assert report["integrity"]["tested_as_submitted"] is False
    assert report["integrity"]["evaluator_sections"][0]["action"] == "restored_section"
    assert any("tested as submitted" in line for line in report["explanations"])


def test_unisolatable_evaluator_settings_fail_closed(tmp_path) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(repo, CONFIG_APP)
    dotted = make_patch(
        repo,
        {
            "pyproject.toml": '[tool]\npytest.ini_options.addopts = "-x"\n\n'
            + CONFIG_APP["pyproject.toml"]
        },
    )
    criteria = (
        'schema_version = 1\n[task]\ntitle = "x"\n'
        '[[target]]\nname = "shout"\npytest = ["tests/test_app.py::test_shout"]\n'
    )

    report = evaluate(repo, commit, dotted, criteria, tmp_path, "dotted")

    assert report["outcome"]["integrity"] == "violated"
    assert report["outcome"]["independently_verified"] is False
    assert report["outcome"]["overall"] == "rejected"
    assert "cannot" in report["target"]["integrity_error"]


def test_section_restoration_handles_ini_files_and_deletion(tmp_path) -> None:
    repo = tmp_path / "repo"
    commit = build_repo(
        repo,
        {
            "setup.cfg": "[metadata]\nname = demo\n\n[tool:pytest]\naddopts = -q\n",
            "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n',
        },
    )
    (repo / "setup.cfg").write_text(
        "[metadata]\nname = renamed\n\n[tool:pytest]\naddopts = -k nothing\n", encoding="utf-8"
    )
    actions = restore_evaluator_sections(repo, commit, ["setup.cfg"], IntegrityPolicy())
    text = (repo / "setup.cfg").read_text(encoding="utf-8")
    assert actions == [
        {"path": "setup.cfg", "section": "tool:pytest", "action": "restored_section"}
    ]
    assert "name = renamed" in text and "addopts = -q" in text and "nothing" not in text

    (repo / "pyproject.toml").unlink()
    with pytest.raises(EvaluatorConfigError, match="deletes pyproject.toml"):
        restore_evaluator_sections(repo, commit, ["pyproject.toml"], IntegrityPolicy())


@pytest.mark.asyncio
async def test_agent_workflow_tests_candidate_application_config(tmp_path) -> None:
    source = tmp_path / "source"
    build_repo(source, CONFIG_APP)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    patch = make_patch(source, {"pyproject.toml": HELLO_PYPROJECT})

    class Provider:
        async def generate(self, messages, tools=None):
            if "implementation plan" in messages[0]["content"].lower():
                return __import__("tender.providers", fromlist=["ModelResponse"]).ModelResponse(
                    '{"summary":"s","steps":["e"],"target_files":["pyproject.toml"]}'
                )
            return __import__("tender.providers", fromlist=["ModelResponse"]).ModelResponse(patch)

    check = (
        f"{sys.executable} -m pytest -q -p no:cacheprovider "
        "tests/test_app.py::test_greeting_is_hello"
    )
    result = await AgentWorkflow(Provider()).run(
        Issue(repository="o/r", title="say hello", body="greeting should be hello"),
        agent,
        [check],
        max_retries=0,
        verification_workspace=source,
    )

    assert result.passed is True
    assert result.outcome.independently_verified is True
    target = next(r for r in result.attempts[0].verification if r["kind"] == "target")
    assert target["restored_from_base"] == []


def test_same_named_test_in_another_class_cannot_satisfy_exact_selector(tmp_path) -> None:
    """End to end: TestA::test_required passes, TestB::test_required fails."""
    repo = tmp_path / "repo"
    commit = build_repo(repo, CLASS_APP)
    unrelated = make_patch(
        repo, {"shapes.py": CLASS_APP["shapes.py"].replace("return 1\n", "return 1  # x\n", 1)}
    )
    criteria = CLASS_CRITERIA.replace(
        'pytest = ["tests/test_classes.py::TestB"]',
        'pytest = ["tests/test_classes.py::TestB::test_required"]',
    )

    report = evaluate(repo, commit, unrelated, criteria, tmp_path, "exact")

    assert report["outcome"]["overall"] == "rejected"
    assert report["target"]["checks"][0]["tests"][0]["outcome"] == "failed"
