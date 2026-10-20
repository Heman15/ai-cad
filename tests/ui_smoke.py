# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the real UI in Chrome against a running backend (manual parts of the first demonstration).

Usage: .venv/bin/python tests/ui_smoke.py http://127.0.0.1:8001 <screenshot-dir>
Requires: pip install playwright, and Google Chrome installed (uses channel="chrome").
"""

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
ARGS = ["--enable-unsafe-swiftshader", "--use-angle=swiftshader"]


def step(name):
    print(f"✓ {name}", flush=True)


with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=ARGS)
    page = browser.new_page(viewport={"width": 1500, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="New", exact=True).click()
    readout = page.locator(".readout")
    status = page.locator(".status")

    # Create a plate from the template dialog.
    page.get_by_role("button", name="Create part…").click()
    page.get_by_role("button", name="OK").click()
    expect(readout).to_contain_text("100.0 × 60.0 × 5.0 mm")
    step("plate created from template")

    # Hole feature with four positions on the top face.
    page.get_by_role("button", name="＋ Feature ▾").click()
    page.get_by_role("button", name="Hole", exact=True).click()
    page.get_by_label("Feature id").fill("mounting_holes")
    page.get_by_label('Positions "u,v; u,v; …"').fill("10,10; 90,10; 10,50; 90,50")
    page.get_by_role("button", name="OK").click()
    expect(page.locator(".tree")).to_contain_text("mounting_holes")
    expect(readout).to_contain_text("29434.5 mm³")
    step("4 holes added (volume 29434.5 mm³)")

    # Select one hole in the 3D view (face mode) and check entity mapping.
    page.get_by_role("button", name="iso", exact=True).click()
    page.get_by_role("button", name="face", exact=True).click()
    page.wait_for_timeout(400)
    # A point on the far inner wall of hole 4 (centre 90,50), which faces the iso camera.
    pt = page.evaluate("window.__cadToScreen(90 - 1.9, 50 + 2.3, 4.2)")
    page.mouse.click(pt["x"], pt["y"])
    page.get_by_role("button", name="Properties").click()
    expect(page.locator(".sel-list")).to_contain_text("feature mounting_holes · hole 4 of 4")
    expect(page.locator(".stats").first).to_contain_text("6 mm")
    page.screenshot(path=OUT / "ui-hole-selected.png")
    step("clicked hole maps to feature mounting_holes, instance 4/4, measured Ø6")

    # Manual edit of a variable: thickness 5 -> 8.
    row = page.locator("table.vars tr", has_text="thickness")
    row.locator("input").fill("8")
    row.locator("input").press("Enter")
    expect(readout).to_contain_text("100.0 × 60.0 × 8.0 mm")
    step("thickness edited manually to 8")

    # Invalid edit is rejected and the model is kept.
    page.locator(".tree li", has_text="mounting_holes").click()
    diam = page.locator(".param-row", has_text="diameter").locator("input")
    diam.fill("50")
    diam.press("Enter")
    expect(status).to_contain_text("rejected, model unchanged")
    expect(diam).to_have_value("6")
    expect(readout).to_contain_text("100.0 × 60.0 × 8.0 mm")
    step("invalid diameter 50 rejected; previous model kept")

    # Formula edit: holes follow the plate length.
    pos = page.locator(".param-row", has_text="positions[1]").locator("input").first
    pos.fill("=length-10")
    pos.press("Enter")
    expect(status).to_contain_text("-> =length-10")
    step("hole position bound to formula =length-10")

    # Material and mass.
    page.locator("section", has_text="Material & mass").locator("select").select_option("aluminum_6061")
    expect(page.locator("section", has_text="Material & mass")).to_contain_text(" g")
    step("material assigned, mass shown")

    # Undo / redo.
    page.get_by_role("button", name="↶ Undo").click()
    expect(page.locator("section", has_text="Material & mass")).to_contain_text("assign a material")
    page.get_by_role("button", name="↷ Redo").click()
    expect(page.locator("section", has_text="Material & mass")).to_contain_text("aluminum_6061")
    step("undo/redo")

    # Fillet two selected vertical edges.
    page.get_by_role("button", name="iso").click()
    page.get_by_role("button", name="edge", exact=True).click()
    page.wait_for_timeout(400)
    for x, y in ((100, 0), (100, 60)):
        p = page.evaluate(f"window.__cadToScreen({x}, {y}, 4)")
        page.keyboard.down("Shift")
        page.mouse.click(p["x"], p["y"])
        page.keyboard.up("Shift")
    expect(page.locator(".sel-list li")).to_have_count(2)
    page.get_by_role("button", name="＋ Feature ▾").click()
    page.get_by_role("button", name="Fillet", exact=True).click()
    page.get_by_label("radius").fill("6")
    page.get_by_role("button", name="OK").click()
    expect(page.locator(".tree")).to_contain_text("fillet_1")
    page.screenshot(path=OUT / "ui-filleted.png")
    step("fillet on 2 picked edges")

    assert not errors, errors
    browser.close()
    print("UI smoke test passed")
