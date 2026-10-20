# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analysis tab in Chrome: pick support/load faces in 3D, run FEA, view stress; DFM findings.

Usage: .venv/bin/python tests/ui_analysis.py http://127.0.0.1:8001 <screenshot-dir>
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="New", exact=True).click()

    # a 100 x 10 x 10 steel cantilever from the template dialog
    page.get_by_role("button", name="Create part…").click()
    page.get_by_label("length").fill("100")
    page.get_by_label("width").fill("10")
    page.get_by_label("thickness").fill("10")
    page.get_by_role("button", name="OK").click()
    expect(page.locator(".readout")).to_contain_text("100.0 × 10.0 × 10.0 mm")
    page.get_by_role("button", name="Properties").click()
    page.locator("section", has_text="Material & mass").locator("select").select_option("steel")
    expect(page.locator("section", has_text="Material & mass")).to_contain_text("7850")

    page.get_by_role("button", name="Analysis", exact=True).click()
    page.get_by_role("button", name="face", exact=True).click()
    page.get_by_role("button", name="iso", exact=True).click()
    page.wait_for_timeout(500)

    def pick(x, y, z):
        p = page.evaluate(f"window.__cadToScreen({x}, {y}, {z})")
        page.mouse.click(p["x"], p["y"])

    # fixed end x=0 is hidden in iso view (camera at +x,-y); look from the left first
    page.get_by_role("button", name="front", exact=True).click()
    page.wait_for_timeout(300)
    page.evaluate("1")
    page.get_by_role("button", name="iso", exact=True).click()
    page.wait_for_timeout(300)
    pick(100, 5, 5)  # tip face (visible in iso)
    page.get_by_role("button", name=re.compile("Add load on 1 selected")).click()
    expect(page.locator("section", has_text="Strength")).to_contain_text("Loads (1)")
    # rotate to see the x=0 end: the 'left' view is not a toolbar button, so use the camera hook through a standard view
    page.evaluate("window.__cadView && window.__cadView('left')")
    page.wait_for_timeout(300)
    pick(0, 5, 5)
    page.get_by_role("button", name=re.compile("as fixed")).click()
    expect(page.locator("section", has_text="Strength")).to_contain_text("Fixed supports (1)")

    page.get_by_label("convergence check").check()
    page.get_by_role("button", name="Run analysis").click()
    stats = page.locator("section", has_text="Strength").locator(".stats")
    expect(stats).to_contain_text("Max deflection", timeout=60000)
    text = stats.inner_text()
    defl = float(re.search(r"Max deflection\s+([\d.]+)", text, re.I).group(1))
    sigma = float(re.search(r"Max von Mises\s+([\d.]+)", text, re.I).group(1))
    assert 0.189 < defl < 0.193, defl
    assert 57 < sigma < 64, sigma
    page.get_by_role("button", name="deformed").click()
    page.wait_for_timeout(500)
    page.screenshot(path=OUT / "ui-fea.png")
    print(f"✓ FEA from picked faces: deflection {defl} mm (theory 0.190-0.192), σ {sigma} MPa (theory 60)")

    # change the model -> stale results are cleared from the view
    page.get_by_role("button", name="hide").click()

    # DFM: add a deep thin pocket and check
    page.get_by_role("button", name="＋ Feature ▾").click()
    page.get_by_role("button", name="Extruded cut", exact=True).click()
    page.get_by_label("Plane / face").select_option("top")
    page.locator(".shape input").nth(0).fill("20")
    page.locator(".shape input").nth(1).fill("6")
    page.locator(".shape input").nth(2).fill("50")
    page.locator(".shape input").nth(3).fill("5")
    page.get_by_label("depth").fill("9.5")
    page.get_by_role("button", name="OK").click()
    expect(page.locator(".tree")).to_contain_text("cut_1")
    page.get_by_role("button", name="Check", exact=True).click()
    findings = page.locator("section", has_text="Manufacturability").locator(".findings li")
    expect(findings.first).to_be_visible()
    all_text = " ".join(findings.all_inner_texts())
    assert "wall 0.50 mm" in all_text and "sharp internal corner" in all_text, all_text
    page.screenshot(path=OUT / "ui-dfm.png")
    print(f"✓ DFM: {findings.count()} findings incl. 0.5 mm floor and sharp pocket corners")

    assert not errors, errors
    browser.close()
    print("Analysis UI test passed")
