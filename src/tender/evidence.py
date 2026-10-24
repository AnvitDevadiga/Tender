"""Inspectable, hash-verified evidence bundles for new runs.

A bundle separates immutable *source* evidence (what was requested, what ran,
what each check printed, the actual event stream) from *derived* summaries
(outcome classification, totals). ``manifest.json`` lists every file with its
size and SHA-256; ``manifest.sha256`` holds the digest of the manifest itself.

Bundles are self-consistent, not signed: anyone who can write the directory can
rewrite it and its manifest together. ``verify_bundle`` detects accidental or
partial modification, missing files, and unlisted files. It cannot prove who
produced a bundle.

Every value is sanitized before it is written: secret-like strings are redacted
and host-specific paths are replaced with placeholders. Environment variables
are never exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "tender.evidence.v1"
MANIFEST = "manifest.json"
MANIFEST_DIGEST = "manifest.sha256"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgsk_[A-Za-z0-9]{10,}"), "[redacted:groq-key]"),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{16,}"), "[redacted:api-key]"),
    (re.compile(r"\bgh[opsur]_[A-Za-z0-9]{16,}"), "[redacted:github-token]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted:github-token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted:aws-key]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 [redacted]"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[redacted:private-key]",
    ),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|access[_-]?token|secret|password|passwd)\s*[=:]\s*)"
            r"(['\"]?)[^\s'\"]{6,}\2"
        ),
        r"\1[redacted]",
    ),
)


def _path_replacements() -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    temp_roots = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}
    for root in sorted(temp_roots, key=len, reverse=True):
        if root and root != "/":
            replacements.append((root.rstrip("/"), "<tmp>"))
    replacements.append(("/private/var/folders", "<tmp>"))
    home = str(Path.home())
    if home and home != "/":
        replacements.append((home, "~"))
    return replacements


def sanitize_text(text: str) -> str:
    """Redact secret-like strings and host-specific paths."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for prefix, placeholder in _path_replacements():
        text = text.replace(prefix, placeholder)
    text = re.sub(r"<tmp>/[^\s'\"]*?tender-(?:run|verify|git|patch)-[A-Za-z0-9_]+", "<tmp>", text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_text(str(key)): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def environment_identity(
    sandbox_backend: str, *, docker_image: str | None = None
) -> dict[str, Any]:
    """Execution environment facts; no environment variables are exported."""
    try:
        git_version = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        git_version = None
    from . import __version__

    sandbox: dict[str, Any] = {"backend": sandbox_backend}
    if sandbox_backend == "docker":
        sandbox.update(
            {
                "isolation": "docker container",
                "image": docker_image or os.getenv("TENDER_SANDBOX_IMAGE", "python:3.12-slim"),
                "image_pinned_by_digest": "@sha256:"
                in (docker_image or os.getenv("TENDER_SANDBOX_IMAGE", "")),
                "network": "none",
                "memory": "2g",
                "cpus": "2",
                "pids_limit": 256,
                "capabilities": "all dropped",
                "root_filesystem": "read-only",
                "user": "65532:65532",
            }
        )
    else:
        sandbox.update(
            {
                "isolation": "none (host timeout wrapper)",
                "note": "local backend executes on the host; trusted code only",
            }
        )
    return {
        "tender_version": __version__,
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "platform": platform.platform(terse=True),
        "machine": platform.machine(),
        "git": git_version,
        "sandbox": sandbox,
    }


class EvidenceWriter:
    """Write files into a new bundle directory and seal them with a manifest."""

    def __init__(self, directory: str | Path) -> None:
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        self._files: list[dict[str, Any]] = []

    def add_bytes(self, relative: str, data: bytes, *, kind: str) -> str:
        if kind not in {"source", "derived"}:
            raise ValueError("evidence kind must be source or derived")
        target = (self.root / relative).resolve()
        target.relative_to(self.root.resolve())
        if relative in {MANIFEST, MANIFEST_DIGEST} or target.exists():
            raise ValueError(f"evidence file already written or reserved: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest = sha256_bytes(data)
        self._files.append(
            {"path": Path(relative).as_posix(), "kind": kind, "bytes": len(data), "sha256": digest}
        )
        return digest

    def add_json(self, relative: str, data: Any, *, kind: str) -> str:
        return self.add_bytes(relative, _canonical(sanitize(data)), kind=kind)

    def add_text(self, relative: str, text: str, *, kind: str) -> str:
        return self.add_bytes(relative, sanitize_text(text).encode("utf-8"), kind=kind)

    def add_raw_text(self, relative: str, text: str, *, kind: str) -> str:
        """Write text byte-for-byte (patches whose digest must match the original)."""
        return self.add_bytes(relative, text.encode("utf-8"), kind=kind)

    def add_jsonl(self, relative: str, rows: list[Any], *, kind: str) -> str:
        body = "".join(
            json.dumps(sanitize(row), sort_keys=True, ensure_ascii=False) + "\n" for row in rows
        )
        return self.add_bytes(relative, body.encode("utf-8"), kind=kind)

    def finish(self, metadata: dict[str, Any]) -> dict[str, Any]:
        manifest = {
            "schema": EVIDENCE_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "signed": False,
            "integrity": "sha256 per file; manifest digest in manifest.sha256; not signed",
            **sanitize(metadata),
            "files": sorted(self._files, key=lambda item: item["path"]),
        }
        payload = _canonical(manifest)
        (self.root / MANIFEST).write_bytes(payload)
        (self.root / MANIFEST_DIGEST).write_text(
            f"{sha256_bytes(payload)}  {MANIFEST}\n", encoding="utf-8"
        )
        return manifest


def verify_bundle(directory: str | Path) -> list[str]:
    """Return integrity problems; an empty list means every hash reconciles."""
    root = Path(directory)
    problems: list[str] = []
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        return [f"missing {MANIFEST}"]
    payload = manifest_path.read_bytes()
    digest_path = root / MANIFEST_DIGEST
    if not digest_path.is_file():
        problems.append(f"missing {MANIFEST_DIGEST}")
    else:
        recorded = digest_path.read_text(encoding="utf-8").split()[0]
        if recorded != sha256_bytes(payload):
            problems.append("manifest digest does not match manifest.json")
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError:
        return [*problems, "manifest.json is not valid JSON"]
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        problems.append(f"unexpected schema: {manifest.get('schema')}")
    listed: set[str] = set()
    for entry in manifest.get("files", []):
        relative = str(entry.get("path", ""))
        listed.add(relative)
        target = root / relative
        if not target.is_file():
            problems.append(f"missing file: {relative}")
            continue
        data = target.read_bytes()
        if len(data) != entry.get("bytes") or sha256_bytes(data) != entry.get("sha256"):
            problems.append(f"hash mismatch: {relative}")
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in listed and relative not in {MANIFEST, MANIFEST_DIGEST}:
                problems.append(f"unlisted file: {relative}")
    return problems
