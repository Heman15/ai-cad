# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drawing import panel (traceable dimensions) and generated 2D drawing, in Chrome.

The AI interpretation step is replaced by recording an analysis through the API (no API key in CI);
everything the user sees and edits afterwards is the real UI.
Usage: .venv/bin/python tests/ui_drawing.py http://127.0.0.1:8001 <screenshot-dir>
"""

import sys
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
PDF = Path(__file__).resolve().parent.parent / "examples" / "base_plate_drawing.pdf"

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="New", exact=True).click()

    # import through the real file input
    page.locator('input[type=file][accept*=".pdf"]').set_input_files(str(PDF))
    expect(page.locator(".src-page img")).to_be_visible()
    expect(page.locator(".tabs button.on")).to_have_text("Drawing")
    print("✓ PDF imported, page rendered in the Drawing tab")

    # stand-in for the AI step: model + analysis with dimension locations (fractions of the page)
    api = httpx.Client(base_url=URL)
    cmds = [
        ("create_plate", {"length": 120, "width": 80, "thickness": 6}),
        ("add_hole", {"id": "mounting_holes", "diameter": 6.6, "positions": [[10, 10], [110, 10], [10, 70], [110, 70]]}),
        ("record_drawing_analysis", {"units": "mm", "projection": "first", "questions": ["Is the centre counterbore required?"], "dimensions": [
            {"id": "D1", "value": 120, "kind": "linear", "describes": "overall length (front view)", "page": 1, "bbox": [0.19, 0.205, 0.24, 0.235], "status": "read", "confidence": "high"},
            {"id": "D2", "value": 80, "kind": "linear", "describes": "overall width (top view)", "page": 1, "bbox": [0.065, 0.37, 0.085, 0.42], "status": "read", "confidence": "high"},
            {"id": "D3", "value": 5, "kind": "linear", "describes": "thickness", "page": 1, "bbox": [0.065, 0.16, 0.085, 0.185], "status": "unclear", "confidence": "low"},
            {"id": "D4", "value": 6.6, "kind": "diameter", "describes": "4x mounting hole", "page": 1, "bbox": [0.46, 0.3, 0.55, 0.325], "status": "read", "confidence": "high"}]}),
        ("link_dimension", {"dimension_id": "D1", "parameter": "vars.length"}),
        ("link_dimension", {"dimension_id": "D3", "parameter": "vars.thickness"}),
        ("link_dimension", {"dimension_id": "D4", "parameter": "mounting_holes.diameter"}),
    ]
    for c, a in cmds:
        r = api.post("/api/command", json={"command": c, "args": a}).json()
        assert r["result"]["ok"], r["result"]
    page.reload()
    page.get_by_role("button", name="Drawing", exact=True).click()
    expect(page.locator(".dim-box")).to_have_count(4)
    row = page.locator("table.dims tr", has_text="thickness")
    expect(row).to_contain_text("≠ drawing")  # drawing says 5, model has 6
    print("✓ 4 dimension boxes on the drawing; thickness mismatch flagged (drawing 5 vs model 6)")

    # the user corrects the misread value: drawing record and model update together
    row.locator("input").fill("6")
    row.locator("input").press("Enter")
    expect(row).not_to_contain_text("≠ drawing")
    expect(row).to_contain_text("confirmed by you")
    page.locator(".dim-box", has_text="D4").click()
    expect(page.locator("table.dims tr.focus")).to_contain_text("4x mounting hole")
    page.screenshot(path=OUT / "ui-drawing-tab.png")
    print("✓ corrected D3 = 6 (confirmed), mismatch cleared; clicking a box focuses its row")

    page.get_by_role("button", name="2D drawing").click()
    page.get_by_label("compare with source").check()
    expect(page.locator(".drawing-views img")).to_have_count(2)
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "ui-drawing-compare.png")
    pdf = api.get("/api/drawing.pdf?sheet=A4")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    print("✓ generated drawing shown next to the source; PDF export works")

    assert not errors, errors
    browser.close()
    print("Drawing UI test passed")
