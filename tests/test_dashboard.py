"""Frontend contract tests: CSP-safe markup, shared bundle wiring, and static-site links.

The dashboard is a zero-build ES-module app served by FastAPI under a strict
CSP. The same bundle powers the static replay and the landing page on GitHub
Pages, so these tests also build the static site and check every local link.
"""

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from tender.api import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parent.parent
DEMO_ROOT = ROOT / "demo"


def test_dashboard_shell_serves_linked_assets() -> None:
    page = client.get("/dashboard/")

    assert page.status_code == 200
    assert 'src="app.js"' in page.text
    assert 'href="styles.css"' in page.text
    assert "Tender" in page.text


def test_dashboard_assets_are_served_with_usable_content_types() -> None:
    for path, kind in (
        ("/dashboard/app.js", "javascript"),
        ("/dashboard/lib.js", "javascript"),
        ("/dashboard/styles.css", "text/css"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert kind in response.headers["content-type"]
        assert len(response.content) > 3_000
    assert client.get("/dashboard/assets/favicon.svg").status_code == 200


def test_dashboard_has_no_inline_code_for_strict_csp() -> None:
    page = client.get("/dashboard/").text
    assert "onclick" not in page and "onchange" not in page
    assert "<style" not in page and "style=" not in page
    assert "<script>" not in page
    assert 'type="module"' in page
    assert "replay.js" not in page  # replay mode never touches the live dashboard
    for script in ("app.js", "lib.js"):
        source = (ROOT / "dashboard" / script).read_text(encoding="utf-8")
        assert 'style="' not in source, f"{script} renders an inline style attribute"
        assert " on" + "click=" not in source


def test_dashboard_palette_is_monochrome() -> None:
    styles = (ROOT / "dashboard" / "styles.css").read_text(encoding="utf-8")
    styles += (DEMO_ROOT / "styles.css").read_text(encoding="utf-8")
    for match in re.finditer(r"#([0-9a-fA-F]{6})\b", styles):
        value = match.group(1)
        red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
        assert max(red, green, blue) - min(red, green, blue) <= 2, f"non-gray color #{value}"
    assert "backdrop-filter" not in styles  # no glassmorphism
    assert "prefers-reduced-motion" in styles


def test_replay_page_reuses_the_dashboard_bundle() -> None:
    page = (DEMO_ROOT / "replay" / "index.html").read_text(encoding="utf-8")

    assert 'href="../dashboard/styles.css"' in page
    assert 'src="../dashboard/app.js"' in page
    assert "../../" not in page
    assert 'src="replay.js"' in page
    assert 'href="/docs"' not in page
    assert "onclick" not in page and "<style" not in page and "<script>" not in page

    shim = (DEMO_ROOT / "replay" / "replay.js").read_text(encoding="utf-8")
    assert "window.TENDER_REPLAY = true" in shim
    assert "window.fetch =" in shim
    assert '"/reports"' in shim


def test_replay_snapshot_stays_clean_and_complete() -> None:
    raw = (DEMO_ROOT / "replay" / "records.json").read_text(encoding="utf-8")

    assert "/Users/" not in raw
    match = re.search(r"\b(?:sk-|gsk_|gh[opsu]_)[A-Za-z0-9]{8,}", raw)
    leaked = match.group(0)[:16] if match else None
    assert leaked is None, f"snapshot leaks a secret-like string: {leaked}"

    snapshot = json.loads(raw)
    assert snapshot["schema"] == "tender.dashboard.replay.v2"
    runs = snapshot["runs"]
    run_ids = {run["id"] for run in runs}
    assert runs
    assert snapshot["total"] == len(runs)
    assert snapshot["metrics"]["runs"] == len(runs)
    assert all(run.get("patch") for run in runs)
    assert set(snapshot["events"]) == run_ids
    assert set(snapshot["artifacts"]) == run_ids
    for run_id, events in snapshot["events"].items():
        assert all(event["run_id"] == run_id for event in events)


def test_landing_page_prioritizes_the_demo_and_has_a_mobile_menu() -> None:
    page = (DEMO_ROOT / "index.html").read_text(encoding="utf-8")
    for section in ("how", "evidence", "results", "reproduce", "limits", "install"):
        assert f'id="{section}"' in page
        if section != "reproduce":
            assert f'href="#{section}"' in page
    hero = page[page.index('id="top"') : page.index('id="how"')]
    assert 'href="replay/' in hero  # the recorded demo is one click from the first screen
    assert "data-menu-toggle" in page and 'aria-controls="site-nav"' in page
    assert 'type="module" src="demo.js"' in page
    assert "<style" not in page and "onclick" not in page


def test_readme_links_use_the_case_sensitive_pages_path() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://anvitdevadiga.github.io/Tender/" in readme
    assert "https://anvitdevadiga.github.io/Tender/replay/" in readme
    assert "github.io/tender" not in readme


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.links.append(value)


def test_static_site_has_no_broken_local_links(tmp_path) -> None:
    site = tmp_path / "site"
    subprocess.run(["bash", "scripts/build_demo_site.sh", str(site)], cwd=ROOT, check=True)
    checked = 0
    for page in site.rglob("*.html"):
        parser = _Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for link in parser.links:
            parsed = urlparse(link)
            if parsed.scheme or link.startswith(("#", "mailto:")):
                continue
            path = parsed.path
            target = (page.parent / path).resolve()
            if path.endswith("/") or target.is_dir():
                target = target / "index.html"
            assert target.is_file(), f"{page.relative_to(site)} links to missing {link}"
            assert site.resolve() in target.parents or target == site.resolve()
            checked += 1
    for module in ("demo.js",):
        source = (site / module).read_text(encoding="utf-8")
        for path in re.findall(r'import [^;]+ from "(\./[^"]+)"', source):
            assert (site / path).is_file(), f"{module} imports missing {path}"
    for fetched in ("docs/experiments/retrieval-cap-ab-v1.json",):
        assert (site / fetched).is_file(), f"landing page fetches missing {fetched}"
    assert checked > 10


def test_javascript_unit_tests_pass() -> None:
    import shutil

    import pytest

    if shutil.which("node") is None:
        pytest.skip("node is required for the JavaScript unit tests")
    completed = subprocess.run(
        ["node", "--test", "tests/js/diff.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    for script in ("dashboard/app.js", "dashboard/lib.js", "demo/demo.js", "demo/replay/replay.js"):
        check = subprocess.run(
            ["node", "--check", script], cwd=ROOT, capture_output=True, text=True
        )
        assert check.returncode == 0, check.stderr
