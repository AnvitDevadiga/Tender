"""Outbound HTTP for credentialed requests.

``urllib.request.urlopen`` follows redirects and copies request headers,
including ``Authorization``, to the redirect target, even on another host.
Every credentialed request Tender makes goes through ``open_url``, which
refuses redirects, so a token can only reach the URL it was written for.
"""

from __future__ import annotations

from typing import Any
from urllib import error, request
from urllib.parse import urlparse

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class RedirectRefused(error.HTTPError):
    """A credentialed request received a redirect, which Tender never follows."""


class _RefuseRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urlparse(newurl)
        raise RedirectRefused(
            req.full_url,
            code,
            f"redirect to {target.scheme}://{target.hostname} refused; "
            "credentials are never forwarded",
            headers,
            fp,
        )


_OPENER = request.build_opener(_RefuseRedirects)


def open_url(req: request.Request, timeout: float) -> Any:
    """Open a request without following redirects."""
    return _OPENER.open(req, timeout=timeout)


def require_secure_endpoint(url: str) -> None:
    """Credentials may travel over plain HTTP only to a loopback server."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return
    raise ValueError(
        "provider endpoint must use https (plain http is allowed only for loopback hosts)"
    )
