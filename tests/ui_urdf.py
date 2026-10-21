# SPDX-License-Identifier: AGPL-3.0-or-later
"""URDF import in the Assembly workspace: convert the example arm, drive a joint with its slider.

Usage: .venv/bin/python tests/ui_urdf.py http://127.0.0.1:8001 <screenshot-dir>
The converted files go to <screenshot-dir>/demo_arm_aicad (passed through the API's out_dir).
"""

import math
import sys
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
XACRO = Path(__file__).resolve().parent.parent / "examples" / "robot_arm" / "urdf" / "demo_arm.urdf.xacro"

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="Assembly", exact=True).click()

    # The browser build asks for a path (the desktop app shows a file dialog). Route the request to a scratch folder.
    page.route("**/api/asm/import_urdf", lambda route: route.continue_(
        post_data=route.request.post_data.replace("}", f', "out_dir": "{OUT / "demo_arm_aicad"}"}}')))
    page.once("dialog", lambda d: d.accept(str(XACRO)))
    page.get_by_role("button", name="Import URDF").click()
    report = page.locator(".urdf-report")
    expect(report).to_contain_text("9 links, 8 joints", timeout=60000)
    expect(report).to_contain_text("6 editable parts")
    expect(report).to_contain_text("finger.brep: 46 triangles → 8 faces")
    expect(page.locator(".robot .joint")).to_have_count(6)
    print("✓ xacro expanded and converted: 6 parametric parts + 2 mesh solids, 6 movable joints")

    api = httpx.Client(base_url=URL)
    wrist = lambda: next(c for c in api.get("/api/asm").json()["spec"]["components"] if c["id"] == "wrist")["position"]  # noqa: E731
    assert [round(v, 3) for v in wrist()] == [0, 0, 630]
    page.get_by_label("joint shoulder").fill("45")
    expect(page.locator(".robot .joint", has_text="shoulder")).to_contain_text("45°")
    page.wait_for_timeout(500)
    x, _, z = wrist()
    assert abs(x - 500 * math.sin(math.radians(45))) < 0.01 and abs(z - (130 + 500 * math.cos(math.radians(45)))) < 0.01, (x, z)
    print(f"✓ shoulder slider to 45°: wrist at x={x:.1f}, z={z:.1f} mm (forward kinematics check)")

    page.get_by_label("joint finger_left_slide").fill("15")
    page.wait_for_timeout(500)
    comps = {c["id"]: c for c in api.get("/api/asm").json()["spec"]["components"]}
    gap = math.dist(comps["finger_left"]["position"], comps["finger_right"]["position"])
    assert abs(gap - 70) < 0.01, gap  # 2 x 20 mm offsets + 2 x 15 mm travel (right finger mimics left)
    page.get_by_label("joint elbow").fill("-60")
    page.wait_for_timeout(600)
    page.screenshot(path=OUT / "ui-urdf-arm.png")
    print(f"✓ gripper opens 15 mm, mimic finger follows (gap {gap:.1f} mm); screenshot")

    page.get_by_role("tab", name="Evaluate").click()
    page.get_by_role("button", name="Bill of Materials").click()
    expect(page.locator("table.bom")).to_contain_text("finger")
    page.get_by_role("button", name="Mass Properties").click()
    expect(page.locator(".tab-body.pm")).to_contain_text("URDF mass")  # links without a material use the URDF mass
    bom = api.get("/api/asm").json()["bom"]
    assert any(r["part"] == "finger" and r["qty"] == 2 for r in bom) and any(r["part"].startswith("cylinder D60 L250") and r["qty"] == 2 for r in bom)
    step = api.get("/api/asm/export.step")
    assert step.status_code == 200 and b"ISO-10303-21" in step.content[:200]
    print("✓ BOM counts shared parts (2 fingers, 2 arm tubes); STEP export of the posed robot")

    assert not errors, errors
    browser.close()
    print("URDF UI test passed")
