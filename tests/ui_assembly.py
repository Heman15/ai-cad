# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembly workspace in Chrome: insert parts, pick faces on two components, mate, BOM, interference.

Usage: .venv/bin/python tests/ui_assembly.py http://127.0.0.1:8001 <screenshot-dir>
"""

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

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="Assembly", exact=True).click()
    page.get_by_role("button", name="New", exact=True).click()
    for f in ("base.aicad", "block.aicad"):
        page.get_by_placeholder("/path/to/part.aicad").fill(str(work / f))
        page.get_by_role("button", name="＋ Insert part").click()
    tree = page.locator(".tree")
    expect(tree).to_contain_text("block_1")
    expect(tree).to_contain_text("base_1")
    # move the block away so both are pickable (API = same command the transform editor sends)
    httpx.post(f"{URL}/api/asm/command", json={"command": "set_transform", "args": {"id": "block_1", "position": [130, 0, 20], "rotation": [0, 0, 0]}})
    page.reload()
    page.get_by_role("button", name="Assembly", exact=True).click()
    page.get_by_role("button", name="iso", exact=True).click()
    page.wait_for_timeout(500)

    def pick(x, y, z, shift=False):
        p = page.evaluate(f"window.__cadToScreen({x}, {y}, {z})")
        if shift:
            page.keyboard.down("Shift")
        page.mouse.click(p["x"], p["y"])
        if shift:
            page.keyboard.up("Shift")

    pick(50, 30, 10)             # base top face
    pick(145, 10, 20, shift=True)  # block bottom face is hidden; pick block top face (z = 28)
    page.wait_for_timeout(200)
    sel = page.locator("section", has_text="Mate selected faces")
    if "of block_1" not in sel.inner_text():
        pick(145, 10, 28, shift=True)
    expect(sel).to_contain_text("plane of base_1")
    expect(sel).to_contain_text("plane of block_1")
    # flush tops would bury the block in the base -> the interference check must catch that
    page.get_by_label("normals aligned").check()
    page.get_by_role("button", name="Coincident", exact=True).click()
    expect(page.locator(".left .findings")).to_contain_text("coincident (aligned): base_1 ↔ block_1")
    expect(page.locator(".left section").nth(1)).to_contain_text("3 degree(s) of freedom left")
    page.get_by_role("button", name="Interference").click()
    expect(page.locator(".right section", has_text="Interference")).to_contain_text("base_1 ∩ block_1")
    print("✓ flush-top mate buries the block: interference reported")
    # replace it with an aligned 8 mm distance between the tops: the block now sits on the base
    page.get_by_role("button", name="↶ Undo").click()
    expect(page.locator(".left .findings")).not_to_contain_text("coincident")
    pick(50, 30, 10)
    pick(145, 10, 28, shift=True)
    page.get_by_label("distance (mm)").fill("8")
    page.get_by_role("button", name="Distance", exact=True).click()
    expect(page.locator(".left .findings")).to_contain_text("distance 8 (aligned)")
    page.get_by_role("button", name="Interference").click()
    expect(page.locator(".right section", has_text="Interference")).to_contain_text("No overlapping volume")
    asm = httpx.get(f"{URL}/api/asm").json()
    block = next(c for c in asm["components"] if c["id"] == "block_1")
    assert abs(block["position"][2] - 10) < 1e-6, block  # base top at z=10 -> block bottom sits on it
    expect(page.locator("table.dims")).to_contain_text("Block")
    page.screenshot(path=OUT / "ui-assembly.png")
    print("✓ undo, distance mate: block rests on the base (z=10), no interference; BOM shown")
    assert not errors, errors
    browser.close()
    print("Assembly UI test passed")
