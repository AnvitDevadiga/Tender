"""Smoke tests for the static dashboard served at /dashboard.

The dashboard is a zero-build ES-module app (index.html + styles.css + app.js)
served directly by FastAPI. These tests guard the asset wiring and the strict
CSP contract (no inline scripts, styles, or event handlers).
"""

from fastapi.testclient import TestClient

from tender.api import app

client = TestClient(app)


def test_dashboard_shell_serves_linked_assets() -> None:
    page = client.get("/dashboard/")

    assert page.status_code == 200
    assert 'src="app.js"' in page.text
    assert 'href="styles.css"' in page.text
    assert "Tender" in page.text


def test_dashboard_assets_are_served_with_usable_content_types() -> None:
    script = client.get("/dashboard/app.js")
    styles = client.get("/dashboard/styles.css")
    favicon = client.get("/dashboard/assets/favicon.svg")

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert len(script.content) > 10_000

    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]
    assert len(styles.content) > 10_000

    assert favicon.status_code == 200


def test_dashboard_shell_has_no_inline_code_for_strict_csp() -> None:
    page = client.get("/dashboard/").text

    assert "onclick" not in page
    assert "onchange" not in page
    assert "<style" not in page
    assert "<script>" not in page  # only the attributed module script is allowed
    assert 'type="module"' in page
