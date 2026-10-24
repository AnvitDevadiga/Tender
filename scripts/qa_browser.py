"""Real-browser QA for the static site: screenshots, overflow, console errors, focus.

Optional tooling (not a project dependency):

    pip install playwright==1.49.1   # drives the installed Google Chrome
    bash scripts/build_demo_site.sh /tmp/pages/Tender
    python3 -m http.server 8765 --directory /tmp/pages &
    python scripts/qa_browser.py http://127.0.0.1:8765/Tender/ /tmp/shots

Checks every page at 320/390/768/1024/1440px in light and dark themes, the
inspector tabs, keyboard selection, focus trap and restoration, inert
background, palette, help, compare, theme persistence, the mobile menu, and
reduced motion. Prints a JSON list of findings (empty means clean).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/Tender/"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "shots")
OUT.mkdir(parents=True, exist_ok=True)
WIDTHS = [320, 390, 768, 1024, 1440]
findings: list[str] = []


def overflow(page) -> int:
    return page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )


def check(page, name: str) -> None:
    over = overflow(page)
    if over > 0:
        wide = page.evaluate(
            """() => {
                const limit = document.documentElement.clientWidth + 1;
                return [...document.querySelectorAll('body *')]
                    .filter(e => e.getBoundingClientRect().right > limit)
                    .slice(0, 5)
                    .map(e => `${e.tagName}.${e.className} ${e.getBoundingClientRect().right}`);
            }"""
        )
        findings.append(f"{name}: horizontal overflow {over}px {wide}")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    for scheme in ("light", "dark"):
        for width in WIDTHS:
            context = browser.new_context(
                viewport={"width": width, "height": 900}, color_scheme=scheme, device_scale_factor=1
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            tag = f"{scheme}-{width}"

            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(300)
            check(page, f"landing {tag}")
            page.screenshot(path=str(OUT / f"landing-{tag}.png"), full_page=width in (390, 1440))

            page.goto(BASE + "replay/", wait_until="networkidle")
            page.wait_for_selector("table.runs tbody tr")
            check(page, f"replay {tag}")
            page.screenshot(path=str(OUT / f"replay-{tag}.png"))

            first = page.locator("table.runs tbody tr").first
            first.locator("a.title").click()
            page.wait_for_selector("#inspector.open .verdict")
            page.wait_for_timeout(300)
            check(page, f"inspector {tag}")
            page.screenshot(path=str(OUT / f"inspector-{tag}.png"))
            for tab in ("timeline", "patch", "verification", "tools", "provenance"):
                page.click(f"#tab-{tab}")
                page.wait_for_timeout(150)
                check(page, f"inspector-{tab} {tag}")
                if width in (390, 1440):
                    page.screenshot(path=str(OUT / f"inspector-{tab}-{tag}.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if page.locator("#inspector").is_visible():
                findings.append(f"{tag}: Escape did not close the inspector")
            if errors:
                findings.append(f"{tag}: console errors {errors[:3]}")
            context.close()

    # Keyboard, focus, and dialog semantics on desktop.
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.goto(BASE + "replay/", wait_until="networkidle")
    page.wait_for_selector("table.runs tbody tr")
    page.keyboard.press("j")
    focused = page.evaluate("document.activeElement.className")
    if "title" not in focused:
        findings.append(f"keyboard j did not focus a run link (focused: {focused})")
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector.open")
    page.wait_for_timeout(300)
    inert = page.evaluate("document.getElementById('app').hasAttribute('inert')")
    inside = page.evaluate("document.getElementById('inspector').contains(document.activeElement)")
    if not inert:
        findings.append("background is not inert while the inspector is open")
    if not inside:
        findings.append("focus is not inside the inspector after opening")
    for _ in range(40):
        page.keyboard.press("Tab")
        if not page.evaluate(
            "document.getElementById('inspector').contains(document.activeElement)"
        ):
            findings.append("Tab escaped the inspector focus trap")
            break
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    restored = page.evaluate("document.activeElement && document.activeElement.className")
    if "title" not in str(restored):
        findings.append(f"focus not restored to the run link after close (got {restored})")
    page.keyboard.press("Meta+k")
    page.wait_for_selector("#palette.open")
    page.keyboard.type("verify")
    page.keyboard.press("Escape")
    page.keyboard.press("?")
    page.wait_for_selector("#help-dialog.open")
    page.screenshot(path=str(OUT / "help-dialog.png"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    # Filters, search, compare.
    page.fill("#search", "percent")
    page.wait_for_timeout(300)
    shown = page.locator("table.runs tbody tr").count()
    page.fill("#search", "")
    page.wait_for_timeout(300)
    boxes = page.locator("input[data-action='pick']")
    boxes.nth(0).check()
    boxes.nth(1).check()
    page.click("[data-action='compare-open']")
    page.wait_for_selector("#compare-dialog.open")
    page.screenshot(path=str(OUT / "compare.png"))
    page.keyboard.press("Escape")
    # Theme persistence.
    page.click("#theme-btn")
    stored = page.evaluate("localStorage.getItem('tender.theme')")
    page.reload(wait_until="networkidle")
    applied = page.evaluate("document.documentElement.dataset.theme")
    if stored != applied:
        findings.append(f"theme did not persist: stored {stored}, applied {applied}")
    context.close()

    # Mobile landing menu.
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(BASE, wait_until="networkidle")
    if page.locator("#site-nav").is_visible():
        findings.append("mobile nav visible before opening the menu")
    page.click("[data-menu-toggle]")
    page.wait_for_timeout(250)
    if not page.locator("#site-nav a[href='#results']").is_visible():
        findings.append("mobile menu does not expose Results")
    page.screenshot(path=str(OUT / "landing-menu-390.png"))
    page.keyboard.press("Escape")
    context.close()

    # Reduced motion.
    context = browser.new_context(viewport={"width": 1024, "height": 800}, reduced_motion="reduce")
    page = context.new_page()
    page.goto(BASE, wait_until="networkidle")
    hidden = page.evaluate("[...document.querySelectorAll('.reveal')].length")
    if hidden:
        findings.append(f"reduced motion still applies reveal classes ({hidden})")
    context.close()
    browser.close()

print(json.dumps({"search_matches": shown, "findings": findings}, indent=1))
