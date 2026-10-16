"""Smoke tests for the static dashboard served at /dashboard and the recorded
replay published with the portfolio demo.

The dashboard is a zero-build ES-module app (index.html + styles.css + app.js)
served directly by FastAPI. These tests guard the asset wiring and the strict
CSP contract (no inline scripts, styles, or event handlers). The replay guards
keep the static snapshot free of local paths and secret-like strings.
"""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from tender.api import app

client = TestClient(app)
DEMO_ROOT = Path(__file__).resolve().parent.parent / "demo"


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
    assert "replay.js" not in page  # replay mode never touches the live dashboard


def test_replay_page_reuses_the_dashboard_bundle() -> None:
    page = (DEMO_ROOT / "replay" / "index.html").read_text(encoding="utf-8")

    # Paths must stay inside the site root: Pages serves this repo under /tender/,
    # so a ../../ hop would escape the deployed prefix.
    assert 'href="../dashboard/styles.css"' in page
    assert 'src="../dashboard/app.js"' in page
    assert "../../" not in page
    assert 'src="replay.js"' in page
    assert 'href="/docs"' not in page  # /docs only exists next to the live API
    assert "onclick" not in page
    assert "<style" not in page
    assert "<script>" not in page

    shim = (DEMO_ROOT / "replay" / "replay.js").read_text(encoding="utf-8")
    assert "window.TENDER_REPLAY = true" in shim
    assert "window.fetch =" in shim


def test_replay_snapshot_stays_clean_and_complete() -> None:
    raw = (DEMO_ROOT / "replay" / "records.json").read_text(encoding="utf-8")

    assert "/Users/" not in raw
    match = re.search(r"\b(?:sk-|gsk_|gh[opsu]_)[A-Za-z0-9]{8,}", raw)
    leaked = match.group(0)[:16] if match else None
    assert leaked is None, f"snapshot leaks a secret-like string: {leaked}"

    snapshot = json.loads(raw)
    assert snapshot["schema"] == "tender.dashboard.replay.v1"
    runs = snapshot["runs"]
    run_ids = {run["id"] for run in runs}
    assert runs
    assert snapshot["total"] == len(runs)
    assert snapshot["metrics"]["runs"] == len(runs)
    assert all(run.get("patch") for run in runs)  # the diff view needs real patches
    assert set(snapshot["events"]) == run_ids
    assert set(snapshot["artifacts"]) == run_ids
    for run_id, events in snapshot["events"].items():
        assert all(event["run_id"] == run_id for event in events)


def test_portfolio_console_section_points_at_existing_assets() -> None:
    page = (DEMO_ROOT / "index.html").read_text(encoding="utf-8")

    assert 'id="console"' in page
    assert 'href="#console"' in page
    assert 'href="replay/"' in page
    for asset in ("dashboard-overview.jpg", "dashboard-drawer.jpg", "dashboard-mobile.jpg"):
        assert f"assets/{asset}" in page
        assert (DEMO_ROOT / "assets" / asset).is_file(), f"missing screenshot: {asset}"
