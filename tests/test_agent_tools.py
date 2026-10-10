from __future__ import annotations

import json
import subprocess

from tender.tools import AgentToolbox


def make_repository(root) -> None:
    root.mkdir()
    (root / "app.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["fastapi>=0.1"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tools@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tender Tools Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)


def toolbox(root, **overrides) -> AgentToolbox:
    defaults = {
        "sandbox_backend": "local",
        "timeout_seconds": 5,
        "test_commands": ["python3 -c 'assert True'"],
        "setup_commands": [],
        "static_analysis_commands": ["python3 -c 'assert True'"],
    }
    defaults.update(overrides)
    return AgentToolbox(root, **defaults)


def test_tool_schemas_are_typed_and_reject_unknown_arguments() -> None:
    definitions = AgentToolbox.definitions()
    names = {item["function"]["name"] for item in definitions}
    directory_schema = next(
        item["function"]["parameters"]
        for item in definitions
        if item["function"]["name"] == "list_directory"
    )
    assert directory_schema["properties"]["path"]["default"] == "."
    assert {
        "read_file",
        "write_file",
        "replace_text",
        "search_code",
        "list_directory",
        "run_command",
        "run_tests",
        "git_diff",
        "git_status",
        "git_log",
        "dependency_lookup",
        "lint",
    } <= names
    result = toolbox(".").execute("git_status", '{"unexpected":true}')
    assert result.ok is False
    assert "extra" in result.content


def test_file_search_git_and_dependency_tools_are_bounded(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    agent_tools = toolbox(repo)
    try:
        assert "return 1" in agent_tools.execute("read_file", '{"path":"app.py"}').content
        assert agent_tools.execute("list_directory", '{"path":"."}').ok is True
        assert agent_tools.execute("list_directory", "{}").ok is True
        assert agent_tools.execute("list_directory", '{"path":".","depth":2,"limit":20}').ok is True
        assert agent_tools.execute("list_directory", '{"path":"","depth":2}').ok is False
        assert (
            "app.py"
            in agent_tools.execute("search_code", '{"query":"answer return","limit":3}').content
        )
        assert (
            "fastapi"
            in agent_tools.execute("dependency_lookup", '{"query":"fastapi","limit":3}').content
        )
        assert agent_tools.execute("git_status", "{}").content == ""
        assert "initial" in agent_tools.execute("git_log", '{"limit":2}').content
    finally:
        agent_tools.close()


def test_file_tools_block_escape_secrets_and_evaluator_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    (repo / ".env.local").write_text("secret", encoding="utf-8")
    agent_tools = toolbox(repo)
    assert agent_tools.execute("read_file", '{"path":"../private.txt"}').ok is False
    assert agent_tools.execute("read_file", '{"path":".env.local"}').ok is False
    assert (
        agent_tools.execute("read_file", '{"path":".tender/evaluator/assertions.py"}').ok
        is False
    )
    blocked = agent_tools.execute(
        "write_file",
        json.dumps({"path": "./.tender/evaluator/assertions.py", "content": "bad"}),
    )
    assert blocked.ok is False
    assert not (repo / ".tender").exists()


def test_write_replace_and_git_diff_preserve_sanitized_audit(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    agent_tools = toolbox(repo)
    write = agent_tools.execute(
        "write_file",
        json.dumps({"path": "new.py", "content": "secret not in the audit"}),
    )
    assert write.ok is True
    assert write.changed_files == ("new.py",)
    assert "secret not in the audit" not in json.dumps(write.audit_input)
    replaced = agent_tools.execute(
        "replace_text",
        json.dumps(
            {
                "path": "app.py",
                "old_text": "return 1",
                "new_text": "return 2",
                "expected_replacements": 1,
            }
        ),
    )
    assert replaced.ok is True
    assert "return 2" in (repo / "app.py").read_text(encoding="utf-8")
    assert "new.py" in agent_tools.execute("git_diff", "{}").content
    assert (repo / "new.py").stat().st_mode & 0o777 == 0o644


def test_replace_text_requires_exact_match_and_run_commands_use_sandbox(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    agent_tools = toolbox(repo)
    unchanged = agent_tools.execute(
        "replace_text",
        json.dumps(
            {
                "path": "app.py",
                "old_text": "missing",
                "new_text": "changed",
                "expected_replacements": 1,
            }
        ),
    )
    assert unchanged.ok is False
    assert "return 1" in (repo / "app.py").read_text(encoding="utf-8")
    assert agent_tools.execute("run_command", '{"command":"python3 -c \'print(42)\'"}').ok
    assert agent_tools.execute("run_tests", "{}").ok
    assert agent_tools.execute("lint", "{}").ok
    agent_tools.close()


def test_read_file_rejects_symlink_escape(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repository(repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (repo / "escape.txt").symlink_to(outside)
    assert toolbox(repo).execute("read_file", '{"path":"escape.txt"}').ok is False
