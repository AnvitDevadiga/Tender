"""Documentation links resolve, and docs do not contradict the implementation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s]+)\)|href=\"([^\"]+)\"|src=\"([^\"]+)\"")


def test_relative_links_in_readme_and_docs_resolve() -> None:
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    for document in documents:
        for match in LINK.finditer(document.read_text(encoding="utf-8")):
            link = next(group for group in match.groups() if group)
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = (document.parent / link.split("#", 1)[0]).resolve()
            assert target.exists(), f"{document.relative_to(ROOT)} links to missing {link}"


def test_current_docs_do_not_repeat_corrected_claims() -> None:
    stale = {
        "summarizes up to the most recent 1,000": "metrics now cover every run",
        "supports only the local sandbox": "the runner also supports Docker",
        "rejects redirects to other hosts": "redirects are refused before they are followed",
    }
    for document in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if document.name == "EVALUATION_ERRATA.md":
            continue
        text = document.read_text(encoding="utf-8")
        for phrase, reason in stale.items():
            assert phrase not in text, f"{document.name} still says '{phrase}' ({reason})"
