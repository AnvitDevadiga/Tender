from __future__ import annotations

from tender.repository import RepositoryAnalyzer
from tender.retrieval import TextRetriever


def test_retriever_ranks_issue_terms_and_tracks_selection_reason(tmp_path) -> None:
    (tmp_path / "unrelated.py").write_text("def unrelated():\n    return 3\n", encoding="utf-8")
    (tmp_path / "calculator.py").write_text(
        "def percentage(total, percent):\n    return total * percent\n", encoding="utf-8"
    )

    hits = TextRetriever().search(tmp_path, "percentage percent result", limit=4)

    assert hits
    assert hits[0].path == "calculator.py"
    assert "percent" in hits[0].reason
    assert hits[0].score > 0


def test_retriever_prioritizes_python_symbol_definitions(tmp_path) -> None:
    source = tmp_path / "src" / "billing.py"
    source.parent.mkdir()
    source.write_text(
        "def calculate_total_tax(amount, rate):\n"
        "    return amount * rate\n\n"
        "def unrelated():\n"
        "    return 0\n",
        encoding="utf-8",
    )

    hits = TextRetriever().search(tmp_path, "calculate total tax", limit=5)

    assert hits
    assert hits[0].path == "src/billing.py"
    assert hits[0].line == 1
    assert "Python symbol definition `calculate_total_tax`" in hits[0].reason
    exact_hits = TextRetriever().search(tmp_path, "calculate_total_tax", limit=5)
    assert exact_hits[0].line == 1
    assert "Python symbol definition" in exact_hits[0].reason


def test_retriever_skips_credentials_and_oversized_files(tmp_path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("token " * 1000, encoding="utf-8")
    retriever = TextRetriever()
    retriever.max_file_bytes = 10

    assert retriever.search(tmp_path, "token", limit=10) == []


def test_retriever_hides_evaluator_and_secret_material(tmp_path) -> None:
    protected = tmp_path / ".tender" / "evaluator"
    protected.mkdir(parents=True)
    (protected / "assertions.py").write_text("percentage regression", encoding="utf-8")
    (tmp_path / "service-secret.json").write_text("percentage credential", encoding="utf-8")
    (tmp_path / "app.py").write_text("percentage implementation", encoding="utf-8")

    hits = TextRetriever().search(tmp_path, "percentage", limit=10)

    assert {hit.path for hit in hits} == {"app.py"}


def test_repository_map_identifies_languages_tests_and_configs(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    result = RepositoryAnalyzer().analyze(tmp_path)

    assert result.file_count == 3
    assert result.languages == {"Python": 2}
    assert result.entry_points == ("src/main.py",)
    assert result.test_files == ("tests/test_main.py",)
    assert "pyproject.toml" in result.dependency_manifests
    assert "pytest -q" in result.command_hints
