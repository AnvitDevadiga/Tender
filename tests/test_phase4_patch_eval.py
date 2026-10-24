"""Phase 4: local patch import, validation, independent verification, and reports."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tender.api as api_module
from tender.cli import main as cli_main
from tender.evidence import verify_bundle
from tender.patch_eval import (
    AcceptanceCriteria,
    PatchEvaluationError,
    evaluate_patch,
    parse_patch,
    validate_patch,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "patch-demo"
CRITERIA = EXAMPLE / "criteria.toml"


def load_demo_module():
    spec = importlib.util.spec_from_file_location(
        "run_patch_demo", ROOT / "scripts/run_patch_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fixture_repo(tmp_path) -> tuple[Path, str]:
    repo = tmp_path / "stats-demo"
    commit = load_demo_module().build_fixture(repo)
    return repo, commit


def git_state(repo: Path) -> tuple[str, str, bytes]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    return head, status, (repo / "stats" / "core.py").read_bytes()


def run(repo: Path, commit: str, patch: Path, out: Path, criteria: Path = CRITERIA, **kwargs):
    return evaluate_patch(
        repo,
        commit,
        patch,
        criteria,
        out,
        sandbox="local",
        trust_local_execution=True,
        python=sys.executable,
        **kwargs,
    )


def write_patch(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------- demo scenario


def test_plausible_patch_is_rejected_and_repair_is_accepted(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    before = git_state(repo)

    first = run(repo, commit, EXAMPLE / "candidate-1.patch", tmp_path / "one")
    second = run(repo, commit, EXAMPLE / "candidate-2.patch", tmp_path / "two")

    assert first["outcome"]["overall"] == "rejected"
    assert first["outcome"]["target"] == "passed"
    assert first["outcome"]["regression"] == "failed"
    assert "test_median_does_not_mutate_input" in " ".join(first["explanations"])
    assert first["baseline"]["status"] == "failed"  # the target checks discriminate
    assert second["outcome"]["overall"] == "accepted"
    assert second["outcome"]["independently_verified"] is True
    assert second["repository"]["base_commit"] == commit
    assert second["repository"]["source_unchanged"] is True
    for report, out in ((first, tmp_path / "one"), (second, tmp_path / "two")):
        assert verify_bundle(out / "evidence") == []
        assert (out / "report.md").read_text().startswith("# Tender patch verification")
        saved = json.loads((out / "report.json").read_text())
        assert saved["patch"]["sha256"] == report["patch"]["sha256"]
        assert (out / "evidence" / "source" / "patch.diff").read_bytes() == (
            EXAMPLE / f"candidate-{1 if out.name == 'one' else 2}.patch"
        ).read_bytes()
    assert git_state(repo) == before  # the original repository is never modified


def test_hidden_target_tests_never_enter_the_source_repository(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    run(repo, commit, EXAMPLE / "candidate-2.patch", tmp_path / "out")
    assert not (repo / "tests" / "test_median_target.py").exists()


# ---------------------------------------------------------- tampering cases


def test_weakened_tests_are_restored_before_verification(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    weaken = (
        (EXAMPLE / "candidate-1.patch").read_text()
        + "--- a/tests/test_core.py\n+++ b/tests/test_core.py\n@@ -26,4 +26,4 @@ def "
        "test_median_rejects_empty_input():\n def test_median_does_not_mutate_input():\n"
        "     values = [3, 1, 2]\n     median(values)\n-    assert values == [3, 1, 2]\n"
        "+    assert values\n"
    )
    report = run(repo, commit, write_patch(tmp_path, "weaken.patch", weaken), tmp_path / "out")

    assert report["validation"]["passed"] is True
    assert "tests/test_core.py" in report["integrity"]["restored_from_base"]
    assert report["outcome"]["regression"] == "failed"
    assert report["outcome"]["overall"] == "rejected"


def test_forced_pass_conftest_is_removed_before_verification(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    conftest = (
        "import pytest\n\n\n@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n    outcome = yield\n"
        "    outcome.get_result().outcome = 'passed'\n"
    )
    lines = conftest.splitlines()
    addition = f"--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1,{len(lines)} @@\n" + "".join(
        f"+{line}\n" for line in lines
    )
    patch = (EXAMPLE / "candidate-1.patch").read_text() + addition
    report = run(repo, commit, write_patch(tmp_path, "conftest.patch", patch), tmp_path / "out")

    assert "conftest.py" in report["integrity"]["removed_patch_additions"]
    assert report["outcome"]["overall"] == "rejected"


def test_exit_zero_without_running_tests_is_not_a_pass(fixture_repo, tmp_path) -> None:
    """A patch that hard-exits with status 0 at import leaves no JUnit report."""
    repo, commit = fixture_repo
    patch = (
        "--- a/stats/core.py\n+++ b/stats/core.py\n@@ -1,2 +1,4 @@\n"
        ' """Small statistics helpers used by the Tender patch-verification demo."""\n'
        "+import os\n+os._exit(0)\n \n"
    )
    report = run(repo, commit, write_patch(tmp_path, "exit.patch", patch), tmp_path / "out")

    target = report["target"]["checks"][0]
    assert target["return_code"] == 0
    assert target["passed"] is False
    assert all(test["outcome"] == "missing" for test in target["tests"])
    assert report["outcome"]["overall"] == "rejected"


@pytest.mark.parametrize(
    ("body", "failed_check"),
    [
        ("--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n", "paths stay inside"),
        ("--- /dev/null\n+++ b/.git/config\n@@ -0,0 +1 @@\n+[core]\n", "paths stay inside"),
        (
            "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n"
            "@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n",
            "no symbolic links",
        ),
        (
            "diff --git a/blob.bin b/blob.bin\nnew file mode 100644\nindex 0000000..1111111\n"
            "Binary files /dev/null and b/blob.bin differ\n",
            "binary policy",
        ),
        ("--- /dev/null\n+++ b/pytest.py\n@@ -0,0 +1 @@\n+raise SystemExit(0)\n", "protected"),
        ("--- /dev/null\n+++ b/.tender/rules.toml\n@@ -0,0 +1 @@\n+x = 1\n", "protected"),
        (
            "--- /dev/null\n+++ b/tests/test_median_target.py\n@@ -0,0 +1 @@\n+x = 1\n",
            "protected",
        ),
        ("--- a/stats/core.py\n+++ b/stats/core.py\n@@ -1,3 +1,3 @@\n-x\n", "unified diff syntax"),
        ("this is not a patch\n", "unified diff syntax"),
    ],
)
def test_unsafe_or_malformed_patches_are_rejected_before_execution(
    fixture_repo, tmp_path, body, failed_check
) -> None:
    repo, commit = fixture_repo
    report = run(repo, commit, write_patch(tmp_path, "bad.patch", body), tmp_path / "out")

    failed = [check["name"] for check in report["validation"]["checks"] if not check["passed"]]
    assert any(failed_check in name for name in failed), failed
    assert report["outcome"]["policy"] == "rejected"
    assert report["outcome"]["overall"] == "rejected"
    assert "target" not in report  # nothing ran


def test_patch_for_a_different_base_does_not_apply(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    patch = "--- a/stats/core.py\n+++ b/stats/core.py\n@@ -1 +1 @@\n-not the real line\n+x\n"
    report = run(repo, commit, write_patch(tmp_path, "stale.patch", patch), tmp_path / "out")

    check = next(c for c in report["validation"]["checks"] if c["name"].startswith("applies"))
    assert check["passed"] is False
    assert report["outcome"]["overall"] == "rejected"


def test_size_and_file_limits_are_enforced(fixture_repo, tmp_path) -> None:
    criteria, _ = AcceptanceCriteria.load(CRITERIA)
    criteria.constraints.max_patch_bytes = 10
    result = validate_patch((EXAMPLE / "candidate-2.patch").read_bytes(), criteria, None)
    assert not next(c for c in result.checks if c["name"] == "size limit")["passed"]
    assert validate_patch(b"\xff\xfe", criteria, None).passed is False


def test_parse_patch_counts_hunks_and_detects_truncation() -> None:
    files = parse_patch((EXAMPLE / "candidate-1.patch").read_text())
    assert [(item.path, item.additions, item.deletions) for item in files] == [
        ("stats/core.py", 5, 2)
    ]
    with pytest.raises(ValueError, match="truncated"):
        parse_patch("--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n")


# ------------------------------------------------------- refusals and errors


def test_local_execution_requires_explicit_trust(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    with pytest.raises(PatchEvaluationError, match="trust-local-execution"):
        evaluate_patch(
            repo, commit, EXAMPLE / "candidate-2.patch", CRITERIA, tmp_path / "out", sandbox="local"
        )


def test_missing_docker_fails_closed_without_executing(fixture_repo, tmp_path, monkeypatch) -> None:
    repo, commit = fixture_repo
    monkeypatch.setattr("tender.patch_eval.docker_available", lambda: False)
    report = evaluate_patch(
        repo, commit, EXAMPLE / "candidate-2.patch", CRITERIA, tmp_path / "out", sandbox="docker"
    )
    assert report["outcome"]["environment"] == "sandbox_unavailable"
    assert report["outcome"]["overall"] == "error"
    assert "target" not in report and "baseline" not in report
    assert verify_bundle(tmp_path / "out" / "evidence") == []


def test_unknown_base_commit_is_rejected(fixture_repo, tmp_path) -> None:
    repo, _ = fixture_repo
    report = run(repo, "f" * 40, EXAMPLE / "candidate-2.patch", tmp_path / "out")
    assert report["outcome"]["policy"] == "rejected"
    assert "not found" in " ".join(report["explanations"])


def test_hidden_tests_inside_the_repository_are_refused(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    criteria = tmp_path / "criteria.toml"
    criteria.write_text(
        CRITERIA.read_text().replace('hidden_tests = "hidden"', f'hidden_tests = "{repo}/tests"')
    )
    with pytest.raises(PatchEvaluationError, match="outside the repository"):
        run(repo, commit, EXAMPLE / "candidate-2.patch", tmp_path / "out", criteria=criteria)


def test_non_discriminating_target_checks_are_reported(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    criteria = tmp_path / "criteria.toml"
    shutil.copytree(EXAMPLE / "hidden", tmp_path / "hidden")
    criteria.write_text(
        CRITERIA.read_text().replace(
            'pytest = [\n  "tests/test_median_target.py::'
            'test_median_even_length_averages_middle_values",\n'
            '  "tests/test_median_target.py::test_median_even_length_floats",\n]',
            'pytest = ["tests/test_core.py::test_median_odd_length"]',
        )
    )
    report = run(repo, commit, EXAMPLE / "candidate-2.patch", tmp_path / "out", criteria=criteria)
    assert report["baseline"]["status"] == "passed"
    assert any("patch is needed" in item for item in report["unknowns"])


def test_output_directory_must_be_new(fixture_repo, tmp_path) -> None:
    repo, commit = fixture_repo
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "keep.txt").write_text("x")
    with pytest.raises(PatchEvaluationError, match="not empty"):
        run(repo, commit, EXAMPLE / "candidate-2.patch", tmp_path / "out")


# ------------------------------------------------------------ CLI and API


def test_cli_exit_codes_and_rendering(fixture_repo, tmp_path, capsys) -> None:
    repo, commit = fixture_repo
    common = ["--repo", str(repo), "--base", commit, "--criteria", str(CRITERIA)]
    local = ["--sandbox", "local", "--trust-local-execution", "--python", sys.executable]
    assert (
        cli_main(
            [
                "verify-patch",
                *common,
                "--patch",
                str(EXAMPLE / "candidate-2.patch"),
                "--out",
                str(tmp_path / "a"),
                *local,
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "verify-patch",
                *common,
                "--patch",
                str(EXAMPLE / "candidate-1.patch"),
                "--out",
                str(tmp_path / "b"),
                *local,
            ]
        )
        == 1
    )
    assert cli_main(["report", "render", str(tmp_path / "b" / "report.json")]) == 0
    assert "REJECTED" in capsys.readouterr().out
    assert cli_main(["evidence", "verify", str(tmp_path / "a" / "evidence")]) == 0
    (tmp_path / "a" / "evidence" / "source" / "patch.diff").write_text("tampered")
    assert cli_main(["evidence", "verify", str(tmp_path / "a" / "evidence")]) == 1


def test_reports_api_serves_recorded_demo_reports(monkeypatch) -> None:
    client = TestClient(api_module.app)
    monkeypatch.delenv("TENDER_REPORT_ROOT", raising=False)
    assert client.get("/reports").status_code == 404
    monkeypatch.setenv("TENDER_REPORT_ROOT", str(ROOT / "demo" / "data" / "patch-demo"))
    listing = client.get("/reports").json()
    assert {item["outcome"]["overall"] for item in listing} == {"accepted", "rejected"}
    assert all("diff" not in item["patch"] for item in listing)
    detail = client.get(f"/reports/{listing[0]['report_id']}").json()
    assert detail["patch"]["diff"].startswith("diff --git")
    assert client.get("/reports/not-a-real-id-0000").status_code == 404
    assert client.get("/reports/..%2F..%2Fetc").status_code == 404


def test_committed_demo_reports_are_internally_consistent() -> None:
    index = json.loads((ROOT / "demo/data/patch-demo/index.json").read_text())
    outcomes = []
    for entry in index["reports"]:
        folder = ROOT / "demo/data/patch-demo" / Path(entry["file"]).parent
        report = json.loads((folder / "report.json").read_text())
        assert verify_bundle(folder / "evidence") == []
        assert report["repository"]["base_commit"] == index["base_commit"]
        manifest = (folder / "evidence" / "manifest.json").read_bytes()
        import hashlib

        assert hashlib.sha256(manifest).hexdigest() == report["evidence"]["manifest_sha256"]
        outcomes.append(report["outcome"]["overall"])
    assert outcomes == ["rejected", "accepted"]
    for path in (ROOT / "demo/data/patch-demo").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "/Users/" not in text and "/var/folders" not in text, path
