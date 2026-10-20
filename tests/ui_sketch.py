# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive the constraint sketcher in Chrome: draw, constrain, solve, extrude, re-edit.

Usage: .venv/bin/python tests/ui_sketch.py http://127.0.0.1:8001 <screenshot-dir>
"""

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else ".")


def center(loc):
    b = loc.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", args=["--enable-unsafe-swiftshader", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.get_by_role("button", name="New", exact=True).click()
    page.get_by_role("button", name="＋ Feature ▾").click()
    page.get_by_role("button", name="Sketch (constraints)…").click()
    svg = page.locator("svg.sk-canvas")
    box = svg.bounding_box()
    cx, cy, scale = 50, 30, 4  # default editor view for an empty part (px per mm)

    def at(u, v):
        return box["x"] + box["width"] / 2 + (u - cx) * scale, box["y"] + box["height"] / 2 - (v - cy) * scale

    status = page.locator(".sk-status")

    def tool(name):
        page.locator(".sk-tools").get_by_role("button", name=name, exact=True).click()

    def pick(loc):
        page.mouse.click(*center(loc))

    def dim(value, label):
        page.locator(".sk-side").get_by_placeholder("value or =formula").fill(str(value))
        page.locator(".sk-side").get_by_role("button", name=label, exact=True).click()

    # rectangle (auto horizontal/vertical constraints)
    tool("rect")
    page.mouse.click(*at(0, 0))
    page.mouse.click(*at(40, 20))
    expect(page.locator("line.sk-ent")).to_have_count(4)
    expect(status).to_contain_text("under-constrained (2 DOF)")  # first corner snapped onto the fixed origin
    print("✓ rectangle drawn from the origin: corner attached, 2 DOF left")

    page.keyboard.press("Escape")  # Esc with nothing pending -> Select tool
    expect(page.locator(".sk-tools button.on")).to_have_text("select")
    pick(page.locator("line.sk-ent").nth(0))
    expect(page.locator(".sk-selinfo")).to_contain_text("Selected: line")
    dim(100, "Length")
    pick(page.locator("line.sk-ent").nth(1))
    dim(60, "Length")
    expect(status).to_contain_text("fully constrained")
    print("✓ Esc → Select, two lengths: fully constrained (no Fix needed)")

    # hole: circle with diameter and position dimensions relative to the fixed corner
    tool("circle")
    page.mouse.click(*at(45, 25))
    page.mouse.click(*at(52, 25))
    tool("select")
    cb = page.locator("circle.sk-ent").bounding_box()
    page.mouse.click(cb["x"] + cb["width"] / 2, cb["y"] + 1)  # on the circle's rim, not its centre point
    dim(12, "Diameter")
    origin = page.locator("g.sk-origin circle")
    pick(origin)
    pick(page.locator("rect.sk-pt").last)
    expect(page.locator(".sk-selinfo")).to_contain_text("origin")
    dim(50, "Horiz. dist.")
    pick(origin)
    pick(page.locator("rect.sk-pt").last)
    dim(30, "Vert. dist.")
    expect(status).to_contain_text("fully constrained · profile 5886.9 mm²")
    print("✓ hole Ø12 dimensioned from the origin: fully constrained, profile area 5886.9 mm²")

    # a line ending on an existing line gets a point-on-line constraint automatically
    n_before = page.locator(".sk-cons li").count()
    tool("line")
    page.mouse.click(*at(20, 75))
    page.mouse.click(*at(20, 60))  # lands on the top edge
    page.keyboard.press("Escape")
    expect(page.locator(".sk-cons")).to_contain_text("point on line")
    page.locator(".sk-tools").get_by_role("button", name="Undo", exact=True).click()
    expect(page.locator(".sk-cons li")).to_have_count(n_before)
    page.keyboard.press("Escape")
    print("✓ drawing onto a line adds 'point on line' automatically; undo removes it")

    # a conflicting dimension is flagged and can be removed
    pick(page.locator("line.sk-ent").nth(2))
    dim(80, "Length")
    expect(status).to_contain_text("conflicting")
    expect(page.locator(".sk-cons li.bad").first).to_be_visible()
    page.screenshot(path=OUT / "ui-sketch-conflict.png")
    page.locator(".sk-cons li").last.get_by_role("button", name="✕").click()
    expect(status).to_contain_text("fully constrained")
    print("✓ conflicting dimension flagged, then removed")

    page.get_by_label("Distance / depth").fill("8")
    page.screenshot(path=OUT / "ui-sketch.png")
    page.locator(".sk-finish").get_by_role("button", name="Extrude", exact=True).click()
    expect(page.locator(".tree")).to_contain_text("sketch_1")
    expect(page.locator(".tree")).to_contain_text("extrude_1")
    expect(page.locator(".readout")).to_contain_text("100.0 × 60.0 × 8.0 mm · 47095.2 mm³")
    page.screenshot(path=OUT / "ui-sketch-extruded.png")
    print("✓ extruded to 8 mm: 47095.2 mm³")

    # edit the sketch later: change the hole diameter dimension
    page.locator(".tree li", has_text="sketch_1").click()
    page.get_by_role("button", name="Edit sketch…").click()
    row = page.locator(".sk-cons li", has_text="diameter")
    row.locator("input").fill("20")
    row.locator("input").press("Tab")
    expect(status).to_contain_text("fully constrained")
    page.get_by_role("button", name="Save sketch").click()
    expect(page.locator(".readout")).to_contain_text("45486.7 mm³")
    print("✓ sketch re-edited (hole Ø20), part regenerated: 45486.7 mm³")

    assert not errors, errors
    browser.close()
    print("Sketch UI test passed")
