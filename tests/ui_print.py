# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print workspace and mesh opening, in the browser build.

- Part workspace: open an STL as a part (imported body), then a hole cut into it.
- Print workspace: choose a printer from the library, add an STL lying on its side, lay it flat, change infill and
  supports, slice, check the G-code preview and the statistics.

Usage: .venv/bin/python tests/ui_print.py http://127.0.0.1:8001 <screenshot-dir>
Needs the slicer engine (PrusaSlicer); skipped with a message if it is missing.
"""

import re
import sys
from pathlib import Path

import cadquery as cq
import httpx
from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
OUT.mkdir(parents=True, exist_ok=True)

api = httpx.Client(base_url=URL, timeout=120)
# An L-bracket lying on its side (as a robot STL often is): lay flat must put the 60 x 40 base down.
bracket = OUT / "bracket_side.stl"
body = (cq.Workplane("XY").box(60, 40, 5, centered=(False, False, False))
        .union(cq.Workplane("XY").box(5, 40, 30, centered=(False, False, False))))
cq.exporters.export(body.rotate((0, 0, 0), (1, 0, 0), 90), str(bracket))

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)

    # ---- Part workspace: an STL opens as a part with one imported body
    page.locator('input[type="file"][accept*=".stl"]').first.set_input_files(str(bracket))
    expect(page.locator(".statusbar")).to_contain_text("imported bracket_side.stl", timeout=60000)
    m = api.get("/api/model").json()
    assert m["spec"]["features"][0]["type"] == "import_body", m["spec"]["features"]
    assert abs(m["volume_mm3"] - (60 * 40 * 5 + 5 * 40 * 25)) < 1, m["volume_mm3"]
    print(f"✓ STL opened as a part: imported body, volume {m['volume_mm3']:.0f} mm³")
    r = api.post("/api/command", json={"command": "add_feature", "args": {"id": "cut", "type": "extrude", "params": {
        "plane": "XY", "distance": 100, "op": "cut", "shapes": [{"type": "circle", "d": 3, "x": 30, "y": -2.5}]}}}).json()
    assert r["result"]["ok"], r["result"]["message"]
    print("✓ a cut through the imported body works")

    if not api.get("/api/print/engine").json()["found"]:
        print("- slicer engine not installed: Print workspace checks skipped")
        sys.exit(0)

    # ---- Print workspace
    page.get_by_role("button", name="Print", exact=True).click()
    dialog = page.get_by_role("dialog", name="Choose a printer")
    expect(dialog).to_be_visible(timeout=30000)  # no printer chosen yet: the library opens
    dialog.get_by_placeholder("Search").fill("ender-3 v2")
    card = dialog.locator(".pd-model", has_text="Creality Ender-3 V2").first
    card.get_by_role("button", name="0.4 mm").click()
    expect(page.locator(".printer-card")).to_contain_text("Ender-3 V2")
    expect(page.locator(".printer-card")).to_contain_text("nozzle 0.4 mm")
    expect(page.get_by_label("Quality profile")).to_have_value("0.20 mm NORMAL (0.4 mm nozzle) @CREALITY")
    print("✓ printer chosen from the library; standard quality and material preselected")

    page.locator('.print input[type="file"]').set_input_files(str(bracket))
    expect(page.locator(".plate-list li")).to_have_count(1, timeout=60000)
    expect(page.locator(".plate-list li")).to_contain_text("60×30×40")  # standing on its side
    page.locator(".plate-list li").click()
    page.get_by_role("button", name="Lay Flat").click()
    expect(page.locator(".plate-list li")).to_contain_text(re.compile(r"(60×40|40×60)×30"))
    print("✓ STL added to the plate; Lay Flat puts the bracket on its 60 × 40 base")

    page.get_by_label("Infill density").fill("40")
    page.get_by_label("Support").select_option("buildplate")
    page.get_by_label("Adhesion").select_option("brim")
    page.get_by_role("button", name="Slice", exact=True).last.click()
    expect(page.locator(".pv-legend")).to_be_visible(timeout=120000)
    stats = page.locator(".slice-box .stats")
    expect(stats).to_contain_text("Print time")
    expect(page.locator(".pv-legend")).to_contain_text("External perimeter")
    expect(page.locator(".pv-legend")).to_contain_text("Skirt/Brim")
    layers = page.locator(".pv-layers")
    expect(layers).to_contain_text("/ 150")  # 30 mm at 0.2 mm layers
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "ui-print-preview.png")
    print(f"✓ sliced and previewed: {stats.inner_text().replace(chr(10), ' ')}")

    page.get_by_label("Top layer").fill("20")
    expect(layers).to_contain_text("Layer 21 / 150")
    page.wait_for_timeout(500)
    page.screenshot(path=OUT / "ui-print-layer21.png")
    state = api.get("/api/print/printers").json()["state"]
    assert state["overrides"]["fill_density"] == "40%" and state["overrides"]["support_material"] == 1, state["overrides"]
    print("✓ layer slider; infill/support/brim overrides saved with the printer choice")

    page.get_by_role("tab", name="Prepare").click()
    page.wait_for_timeout(300)
    page.screenshot(path=OUT / "ui-print-prepare.png")
    assert not errors, errors
    browser.close()
print("ALL PRINT UI CHECKS PASSED")
