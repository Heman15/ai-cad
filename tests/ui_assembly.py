# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembly workspace in Chrome: insert parts, mate faces of two components (coincident, distance, perpendicular),
edit and suppress mates, interference, hide/show, exploded view, measure between components, BOM.

Usage: .venv/bin/python tests/ui_assembly.py http://127.0.0.1:8001 <screenshot-dir>
"""

import re
import sys
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cadcore import Project  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
work = Path(tempfile.mkdtemp())
b = Project()
b.execute("create_plate", length=100, width=60, thickness=10)
b.execute("assign_material", name="steel")
b.execute("set_name", name="Base")
b.save(work / "base.aicad")
c = Project()
c.execute("create_plate", length=30, width=20, thickness=8)
c.execute("assign_material", name="aluminum_6061")
c.execute("set_name", name="Block")
c.save(work / "block.aicad")
api = httpx.Client(base_url=URL)


def block():
    return next(c for c in api.get("/api/asm").json()["spec"]["components"] if c["id"] == "block_1")


with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="Assembly", exact=True).click()
    page.get_by_role("button", name="New", exact=True).click()
    page.get_by_role("button", name="Insert Components").click()
    for f in ("base.aicad", "block.aicad"):
        page.get_by_placeholder("/path/to/part.aicad").fill(str(work / f))
        page.get_by_role("button", name="Insert", exact=True).click()
        expect(page.locator(".tree.comps")).to_contain_text(f.split(".")[0] + "_1")
    tree = page.locator(".tree.comps")
    expect(tree.locator("li", has_text="base_1")).to_contain_text("(f)")
    expect(tree.locator("li", has_text="block_1")).to_contain_text("(-)")
    print("✓ ribbon Insert Components; FeatureManager shows fixed (f) and floating (-) components")
    # move the block away so both are pickable (API = same command the transform editor sends)
    api.post("/api/asm/command", json={"command": "set_transform", "args": {"id": "block_1", "position": [40, 20, 20], "rotation": [0, 0, 0]}})
    page.reload()
    page.get_by_role("button", name="Assembly", exact=True).click()
    page.get_by_role("button", name="Isometric", exact=True).click()
    page.wait_for_timeout(500)

    def pick(x, y, z, shift=False):
        p = page.evaluate(f"window.__cadToScreen({x}, {y}, {z})")
        if shift:
            page.keyboard.down("Shift")
        page.mouse.click(p["x"], p["y"])
        if shift:
            page.keyboard.up("Shift")

    pm = page.locator(".tab-body.pm")
    mates = page.locator(".tree.mates")
    status = page.locator(".fm-status")
    page.get_by_role("button", name="Mate", exact=True).click()
    pick(20, 10, 10)     # base top face
    pick(55, 30, 28)     # block top face (Mate adds picks without Shift)
    expect(pm).to_contain_text("plane of base_1")
    expect(pm).to_contain_text("plane of block_1")
    # flush tops would bury the block in the base -> the interference check must catch that
    page.get_by_label("Flip alignment").check()
    page.get_by_role("button", name="Coincident", exact=True).click()
    expect(mates).to_contain_text("Coincident (aligned)")
    expect(status).to_contain_text("3 degree(s) of freedom left")
    page.get_by_role("tab", name="Evaluate").click()
    page.get_by_role("button", name="Interference Detection").click()
    expect(pm).to_contain_text("base_1 ∩ block_1")
    print("✓ flush-top coincident mate (flipped) buries the block: interference reported")

    # replace it with an aligned 8 mm distance between the tops: the block now sits on the base
    page.get_by_role("button", name="Undo", exact=True).click()
    expect(mates).not_to_contain_text("Coincident")
    page.get_by_role("tab", name="Assembly").click()
    page.get_by_role("button", name="Mate", exact=True).click()
    pick(20, 10, 10)
    pick(55, 30, 28)
    page.get_by_label("Flip alignment").check()
    page.get_by_label("distance (mm)").fill("8")
    page.get_by_role("button", name="Distance", exact=True).click()
    expect(mates).to_contain_text("Distance 8 mm (aligned)")
    assert abs(block()["position"][2] - 10) < 1e-6, block()  # base top at z=10 -> block bottom sits on it
    page.get_by_role("tab", name="Evaluate").click()
    page.get_by_role("button", name="Interference Detection").click()
    expect(pm).to_contain_text("No overlapping volume")
    print("✓ undo, distance mate: block rests on the base (z=10), no interference")

    # perpendicular mate between the two front faces (they start parallel)
    page.get_by_role("tab", name="Assembly").click()
    page.get_by_role("button", name="Mate", exact=True).click()
    pick(50, 0, 5)       # base front face (normal -Y)
    pick(55, 20, 14)     # block front face
    page.get_by_role("button", name="Perpendicular", exact=True).click()
    expect(mates).to_contain_text("Perpendicular")
    expect(status).to_contain_text("2 degree(s) of freedom left")
    rot = block()["rotation"]
    assert abs(abs(rot[2]) - 90) < 1e-6 and abs(rot[0]) < 1e-6 and abs(rot[1]) < 1e-6, rot
    print(f"✓ perpendicular mate turns the block 90° about Z (rotation {rot[2]:.1f}°), 2 DOF left")

    # edit the distance value from the tree, then suppress the perpendicular mate
    page.get_by_role("button", name="Close command").click()
    mates.locator("li", has_text="Distance").click()
    page.get_by_label("Mate value").fill("12")
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(mates).to_contain_text("Distance 12 mm (aligned)")
    assert abs(block()["position"][2] - 14) < 1e-6, block()
    perp = mates.locator("li", has_text="Perpendicular")
    perp.hover()
    perp.get_by_role("button", name="Suppress mate_2").click()
    expect(perp).to_have_class(re.compile("suppressed"))
    expect(status).to_contain_text("3 degree(s) of freedom left")
    print("✓ mate value edited in the PropertyManager (block up to z=14); perpendicular suppressed (3 DOF)")

    # hide / show the block
    tree.locator("li", has_text="block_1").click()
    page.get_by_role("button", name="Show/Hide").click()
    expect(tree.locator("li", has_text="block_1")).to_have_class(re.compile("hidden-comp"))
    assert block()["hidden"] is True
    page.get_by_role("button", name="Show/Hide").click()
    expect(tree.locator("li", has_text="block_1")).not_to_have_class(re.compile("hidden-comp"))
    assert block()["hidden"] is False
    print("✓ Show/Hide hides the selected component and shows it again")

    # exploded view: auto explode, collapse
    page.get_by_role("button", name="Exploded View").click()
    page.get_by_role("button", name="Auto Explode").click()
    expect(pm.locator(".steps-list li")).to_have_count(2)
    hud_toggle = page.get_by_role("button", name="Explode toggle")
    expect(hud_toggle).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".statusbar")).to_contain_text("Exploded view")
    page.wait_for_timeout(500)
    page.screenshot(path=OUT / "ui-assembly-exploded.png")
    assert len(api.get("/api/asm").json()["spec"]["explode"]) == 2
    page.get_by_role("button", name="Collapse", exact=True).first.click()
    expect(hud_toggle).to_have_attribute("aria-pressed", "false")
    print("✓ auto explode: 2 steps saved in the assembly; explode/collapse toggle")

    # measure between the two top faces, in assembly coordinates
    page.get_by_role("button", name="Close command").click()
    page.get_by_role("tab", name="Evaluate").click()
    page.get_by_role("button", name="Measure", exact=True).click()
    page.wait_for_timeout(500)
    faces = api.get("/api/asm").json()["entities"]["faces"]
    top = next(f for f in faces if f["component"] == "block_1" and f.get("normal") and f["normal"][2] > 0.99)
    pick(20, 10, 10)
    pick(*top["center"])
    panel = page.locator(".measure-panel")
    expect(panel).to_contain_text("Normal distance")
    expect(panel).to_contain_text(re.compile(r"Normal distance\s*12 mm"))
    page.screenshot(path=OUT / "ui-assembly-measure.png")
    print("✓ measure between faces of two components: normal distance 12 mm")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    expect(page.locator(".measure-panel")).to_have_count(0)
    page.screenshot(path=OUT / "ui-assembly-after-measure.png")

    page.get_by_role("button", name="Bill of Materials").click()
    expect(pm.locator("table.bom")).to_contain_text("Block")
    expect(page.locator(".statusbar .readout")).to_contain_text("2 components · 2 mates")
    csv = api.get("/api/asm/bom.csv").text
    assert "Block" in csv and csv.startswith("item,part")
    page.wait_for_timeout(400)
    page.screenshot(path=OUT / "ui-assembly.png")
    print("✓ BOM in the PropertyManager and as CSV")
    assert not errors, errors
    browser.close()
    print("Assembly UI test passed")
