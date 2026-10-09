import subprocess

import pytest

from forgebench.patches import PatchCapture


def test_patch_capture_records_diff_and_detects_protected_changes(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    target = tmp_path / "app.py"
    target.write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    target.write_text("return 2\n", encoding="utf-8")
    artifact = PatchCapture().capture(tmp_path)
    assert artifact.valid
    assert artifact.changed_files == ("app.py",)
    assert len(artifact.sha256) == 64
    assert "return 2" in artifact.diff


def test_patch_capture_rejects_evaluator_changes(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    protected = tmp_path / ".forgebench" / "evaluator"
    protected.parent.mkdir()
    protected.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    protected.write_text("tampered\n", encoding="utf-8")
    artifact = PatchCapture().capture(tmp_path)
    assert artifact.valid is False
    assert artifact.protected_file_changes == (".forgebench/evaluator",)


def test_patch_capture_includes_untracked_files(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "new_module.py").write_text("value = 1\n", encoding="utf-8")

    artifact = PatchCapture().capture(tmp_path)

    assert artifact.changed_files == ("new_module.py",)
    assert "new_module.py" in artifact.diff
    assert "+value = 1" in artifact.diff


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", ".aws/credentials", "service-secret.json", "deploy.key"],
)
def test_patch_capture_protects_secrets(path: str) -> None:
    assert PatchCapture()._is_protected(path) is True
