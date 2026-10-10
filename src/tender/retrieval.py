from __future__ import annotations

import ast
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    text: str
    score: float = 0.0
    reason: str = "lexical token overlap"


class TextRetriever:
    """Bounded deterministic lexical retrieval baseline with auditable selection reasons."""

    ignored = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".aws",
        ".ssh",
        "dist",
        "build",
        "vendor",
        "coverage",
    }
    ignored_names = {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "credentials",
    }
    protected_paths = {
        ".tender/evaluator",
        ".tender/benchmark.toml",
        "benchmarks/tasks.jsonl",
    }
    max_file_bytes = 512 * 1024
    max_files = 20_000
    max_total_bytes = 50 * 1024 * 1024
    max_symbol_files = 100
    max_symbol_file_bytes = 128 * 1024
    token_pattern = re.compile(r"[A-Za-z_][A-Za-z_0-9]{1,}")

    def search(self, root: str | Path, query: str, limit: int = 20) -> list[SearchHit]:
        if not query.strip() or limit <= 0:
            return []
        root = Path(root).resolve()
        query_terms = {token.casefold() for token in self.token_pattern.findall(query)}
        if not query_terms:
            return []
        candidates: list[tuple[float, SearchHit]] = []
        file_count = scanned_bytes = symbol_files = 0
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in self.ignored
                and not (current_path / name).is_symlink()
                and not self._is_protected((current_path / name).relative_to(root).as_posix())
                and not self._is_sensitive_name(name)
            )
            for name in sorted(names):
                path = current_path / name
                if path.is_symlink() or name.casefold() in self.ignored_names:
                    continue
                try:
                    if not path.is_file() or path.stat().st_size > self.max_file_bytes:
                        continue
                    file_count += 1
                    if file_count > self.max_files:
                        return self._rank(candidates, limit)
                    scanned_bytes += path.stat().st_size
                    if scanned_bytes > self.max_total_bytes:
                        return self._rank(candidates, limit)
                    relative = path.relative_to(root)
                    if self._is_protected(relative.as_posix()) or self._is_sensitive_name(name):
                        continue
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                file_tokens = {
                    token.casefold() for line in lines for token in self.token_pattern.findall(line)
                }
                file_overlap = len(query_terms & file_tokens)
                if (
                    path.suffix == ".py"
                    and symbol_files < self.max_symbol_files
                    and path.stat().st_size <= self.max_symbol_file_bytes
                ):
                    symbol_files += 1
                    try:
                        tree = ast.parse("\n".join(lines), filename=str(relative))
                    except (SyntaxError, ValueError, RecursionError):
                        tree = None
                    if tree is not None:
                        for node in ast.walk(tree):
                            if not isinstance(
                                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                            ):
                                continue
                            name_parts = {
                                part.casefold()
                                for part in re.split(r"[^A-Za-z0-9]+|_+", node.name)
                                if len(part) > 1
                            }
                            if not name_parts or not (
                                node.name.casefold() in query_terms
                                or name_parts.issubset(query_terms)
                            ):
                                continue
                            line_number = getattr(node, "lineno", 1)
                            symbol_line = lines[line_number - 1].strip()[:1000]
                            score = 5.0 + 0.35 * file_overlap
                            candidates.append(
                                (
                                    score,
                                    SearchHit(
                                        str(relative),
                                        line_number,
                                        symbol_line,
                                        round(score, 4),
                                        f"Python symbol definition `{node.name}` "
                                        "matches issue terms",
                                    ),
                                )
                            )
                if not file_overlap:
                    continue
                for number, line in enumerate(lines, start=1):
                    line_tokens = {token.casefold() for token in self.token_pattern.findall(line)}
                    overlap = query_terms & line_tokens
                    if overlap:
                        score = sum(
                            1.0 + math.log1p(sum(term in token for token in line_tokens))
                            for term in overlap
                        )
                        score += 0.35 * file_overlap
                        candidates.append(
                            (
                                score,
                                SearchHit(
                                    str(relative),
                                    number,
                                    line.strip()[:1000],
                                    round(score, 4),
                                    f"matched {', '.join(sorted(overlap))}; "
                                    f"file matched {file_overlap} issue terms",
                                ),
                            )
                        )
                        if len(candidates) >= max(200, limit * 20):
                            candidates = self._rank_pairs(candidates, max(100, limit * 10))
        return self._rank(candidates, limit)

    @classmethod
    def _is_protected(cls, path: str) -> bool:
        normalized = path.casefold()
        return any(
            normalized == protected or normalized.startswith(protected.rstrip("/") + "/")
            for protected in cls.protected_paths
        )

    @classmethod
    def _is_sensitive_name(cls, name: str) -> bool:
        normalized = name.casefold()
        return (
            normalized in cls.ignored_names
            or normalized.startswith(".env")
            or "credential" in normalized
            or "secret" in normalized
            or normalized.startswith("token.")
            or normalized.endswith((".token", "_token", "-token", ".pem", ".key", ".p12", ".pfx"))
        )

    @staticmethod
    def _rank_pairs(
        candidates: list[tuple[float, SearchHit]], limit: int
    ) -> list[tuple[float, SearchHit]]:
        candidates.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return candidates[:limit]

    @staticmethod
    def _rank(candidates: list[tuple[float, SearchHit]], limit: int) -> list[SearchHit]:
        candidates.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return [hit for _, hit in candidates[:limit]]
