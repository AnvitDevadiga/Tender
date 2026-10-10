from __future__ import annotations

import asyncio
import json
import os
import re
from urllib import request
from urllib.parse import urlparse

from .models import Issue


class GitHubIssueClient:
    pattern = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?$")
    max_response_bytes = 1_000_000

    def __init__(self, token: str | None = None, timeout: int = 30) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.timeout = timeout

    @classmethod
    def parse_url(cls, url: str) -> tuple[str, str, int]:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port not in (None, 443)
        ):
            raise ValueError(
                "expected a GitHub issue URL: https://github.com/owner/repo/issues/123"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("GitHub issue URL must not include credentials, query, or fragment")
        match = cls.pattern.fullmatch(parsed.path)
        if not match:
            raise ValueError(
                "expected a GitHub issue URL: https://github.com/owner/repo/issues/123"
            )
        owner, repo, number = match.groups()
        if owner in {".", ".."} or repo in {".", ".."}:
            raise ValueError("invalid GitHub repository path")
        return owner, repo, int(number)

    async def fetch(self, url: str) -> Issue:
        owner, repo, number = self.parse_url(url)
        canonical_url = f"https://github.com/{owner}/{repo}/issues/{number}"
        return await asyncio.to_thread(self._fetch_sync, canonical_url, owner, repo, number)

    def _fetch_sync(self, url: str, owner: str, repo: str, number: int) -> Issue:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Tender/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}", headers=headers
        )
        with request.urlopen(req, timeout=self.timeout) as response:
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "api.github.com":
                raise ValueError("GitHub API redirected to an unsupported host")
            payload = response.read(self.max_response_bytes + 1)
        if len(payload) > self.max_response_bytes:
            raise ValueError("GitHub issue response exceeds the 1 MB limit")
        data = json.loads(payload.decode("utf-8"))
        if "pull_request" in data:
            raise ValueError("GitHub pull requests are not accepted as benchmark issues")
        return Issue(
            repository=f"{owner}/{repo}",
            number=number,
            title=data["title"],
            body=data.get("body") or "",
            source_url=url,
        )
