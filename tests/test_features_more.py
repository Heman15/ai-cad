# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draft, rib, variable fillet, Hole Wizard, section views and hems, each checked against hand-computed geometry."""

import json
import math
import re

import cadquery as cq
import pytest
from fastapi.testclient import TestClient

from cadcore import Project
from cadcore.drawing import make_drawing, to_pdf
from cadcore.holes import CBORE_ISO4762, CLEARANCE, CSK_ISO10642
from cadcore.sheetmetal import CLOSED_HEM_GAP, bend_allowance, flat_pattern


def ok(r):
    assert r.ok, r.message
    return r


def block(length=40, width=40, height=20):
    p = Project()
    ok(p.execute("add_feature", id="b", type="extrude", params={
        "plane": "XY", "shapes": [{"type": "rect", "w": length, "h": width}], "distance": height}))
    return p


# ---------------- draft ----------------

def test_draft_side_faces_volume_and_measured_angle():
    p = block()
    ok(p.execute("add_feature", id="d", type="draft", params={"faces": "#Z", "angle": 5}))
    a, h, t = 40, 20, math.tan(math.radians(5))
    # every cross-section shrinks by 2·z·tan(5°) per side: V = ∫ (a - 2zt)² dz
    assert p.result.volume_mm3 == pytest.approx(a * a * h - 2 * a * t * h * h + 4 / 3 * t * t * h ** 3, rel=1e-9)
    sides = [f for f in p.result.shape.val().Faces() if abs(f.normalAt().z) < 0.5]
    assert len(sides) == 4
    for f in sides:  # angle between the face and the pull direction (+Z), measured on the face normal
        assert math.degrees(math.asin(f.normalAt().z)) == pytest.approx(5, abs=1e-9)
    assert p.result.bbox_mm[0] == pytest.approx(40)  # unchanged at the neutral plane z=0


def test_draft_picked_face_with_face_neutral_plane_and_flip():
    p = block()
    # one side face picked in the UI ({point, normal}); neutral plane = top face, pull into the part (-Z)
    ok(p.execute("add_feature", id="d", type="draft", params={
        "faces": [{"point": [-20, 0, 10], "normal": [-1, 0, 0]}], "angle": 3, "neutral_plane": {"face": ">Z"}}))
    t = math.tan(math.radians(3))
    assert p.result.volume_mm3 == pytest.approx(32000 - 40 * 0.5 * 20 * 20 * t, rel=1e-9)
    f = next(f for f in p.result.shape.val().Faces() if f.normalAt().x < -0.5)
    assert math.degrees(math.asin(-f.normalAt().z)) == pytest.approx(3, abs=1e-9)  # leans the other way
    q = block()
    ok(q.execute("add_feature", id="d", type="draft", params={"faces": "<X", "angle": 3, "flip": True}))
    assert q.result.volume_mm3 == pytest.approx(32000 + 40 * 0.5 * 20 * 20 * t, rel=1e-9)  # flipped pull grows it


def test_draft_errors_are_explicit():
    p = block()
    r = p.execute("add_feature", id="d", type="draft", params={"faces": ">Z", "angle": 5})
    assert not r.ok and "perpendicular to the pull direction" in r.message and "face at [0, 0, 20]" in r.message
    for bad in (0, 50, -46):
        r = p.execute("add_feature", id="d", type="draft", params={"faces": "#Z", "angle": bad})
        assert not r.ok and "between -45 and 45" in r.message
    r = p.execute("add_feature", id="d", type="draft", params={"faces": "#Z", "angle": 5, "pull": [1, 0, 0]})
    assert not r.ok and "lies in the neutral plane" in r.message
    r = p.execute("add_feature", id="d", type="draft", params={"faces": [{"point": [99, 0, 0], "normal": [1, 0, 0]}], "angle": 5})
    assert not r.ok and "no longer exists" in r.message
    assert p.result.volume_mm3 == pytest.approx(32000)
    # a cylinder lying across the pull direction cannot be tapered: the face is named, nothing changes
    q = Project()
    ok(q.execute("add_feature", id="rod", type="extrude", params={"plane": "YZ", "shapes": [{"type": "circle", "d": 20}], "distance": 40}))
    r = q.execute("add_feature", id="d", type="draft", params={"faces": [{"point": [20, 0, 10]}], "angle": 5})
    assert not r.ok and "cannot draft 1 of 1 face(s): CYLINDER face at [20, 0, 0]" in r.message


# ---------------- rib ----------------

def bracket():
    p = Project()
    ok(p.execute("create_l_bracket", length=80, width=40, thickness=5, height=60))
    return p


def test_rib_parallel_fills_the_triangle_to_the_walls():
    p = bracket()
    v0 = p.result.volume_mm3
    # line from the upright (x=5) to the base (z=5) in the mid-plane y=20: triangle legs 55 × 35, thickness 4
    ok(p.execute("add_feature", id="rib", type="rib", params={"plane": {"base": "XZ", "offset": 20}, "points": [[5, 40], [60, 5]], "thickness": 4}))
    assert p.result.volume_mm3 - v0 == pytest.approx(0.5 * 55 * 35 * 4, rel=1e-9)
    body = dict(p.result.tools)["rib"].BoundingBox()
    assert (body.ymin, body.ymax) == pytest.approx((18, 22))
    # a line that stops short of the material is extended along itself until it meets the part
    q = bracket()
    ok(q.execute("add_feature", id="rib", type="rib", params={"plane": {"base": "XZ", "offset": 20}, "points": [[10, 35], [50, 10]], "thickness": 4}))
    # extended line passes through (5, 38.125) and (58, 5): same triangle shape with legs 53 × 33.125
    assert q.result.volume_mm3 - v0 == pytest.approx(0.5 * 53 * 33.125 * 4, rel=1e-9)


def test_rib_from_open_sketch_one_side():
    p = bracket()
    v0 = p.result.volume_mm3
    ok(p.execute("add_feature", id="sk", type="sketch", params={
        "plane": {"base": "XZ", "offset": 20}, "path_only": True, "points": {"a": [5, 40], "b": [60, 5]},
        "entities": [{"id": "l1", "type": "line", "p1": "a", "p2": "b"}],
        "constraints": [{"type": "fix", "point": "a"}, {"type": "fix", "point": "b"}]}))
    ok(p.execute("add_feature", id="rib", type="rib", params={"sketch": "sk", "thickness": 3, "side": "first"}))
    assert p.result.volume_mm3 - v0 == pytest.approx(0.5 * 55 * 35 * 3, rel=1e-9)
    body = dict(p.result.tools)["rib"].BoundingBox()
    assert (body.ymin, body.ymax) == pytest.approx((20, 23))  # first side = toward +Y, the XZ plane's normal
    assert len(p.result.shape.val().Solids()) == 1


def test_rib_normal_direction_grows_to_the_plate():
    p = Project()
    ok(p.execute("create_plate", length=80, width=40, thickness=5))
    ok(p.execute("add_feature", id="r1", type="rib", params={
        "plane": {"base": "XY", "offset": 25}, "points": [[10, 20], [70, 20]], "thickness": 3, "direction": "normal"}))
    assert p.result.volume_mm3 == pytest.approx(80 * 40 * 5 + 60 * 3 * 20, rel=1e-9)
    ok(p.execute("add_feature", id="r2", type="rib", params={
        "plane": {"base": "XY", "offset": 25}, "points": [[10, 30], [70, 30]], "thickness": 3, "direction": "normal", "side": "first"}))
    body = dict(p.result.tools)["r2"].BoundingBox()
    assert (body.ymin, body.ymax, body.zmin, body.zmax) == pytest.approx((30, 33, 5, 25))  # left of the line (+Y)


def test_rib_that_misses_material_fails_clearly():
    p = bracket()
    r = p.execute("add_feature", id="rib", type="rib", params={"plane": {"base": "XZ", "offset": 20}, "points": [[20, 40], [60, 20]], "thickness": 4})
    assert not r.ok and "does not reach the part" in r.message
    q = Project()
    ok(q.execute("create_plate", length=80, width=40, thickness=5))
    r = q.execute("add_feature", id="rib", type="rib", params={
        "plane": {"base": "XY", "offset": 25}, "points": [[100, 30], [170, 30]], "thickness": 3, "direction": "normal"})
    assert not r.ok and "does not reach material" in r.message


# ---------------- variable fillet ----------------

def _fillet_size(p, x):
    """Width of the fillet face cut by the plane x = const (equals the local radius on a 90° edge)."""
    face = next(f for f in p.result.shape.val().Faces() if f.geomType() != "PLANE")
    return face.intersect(cq.Solid.makeBox(0.01, 200, 200, cq.Vector(x, -100, -100))).BoundingBox().ylen


def test_variable_fillet_linear_radius():
    p = Project()
    ok(p.execute("create_plate", length=60, width=30, thickness=30))
    ok(p.execute("add_feature", id="vf", type="variable_fillet", params={"edge": {"near": [30, 0, 30]}, "r1": 2, "r2": 6, "start": [0, 0, 30]}))
    assert _fillet_size(p, 0.01) == pytest.approx(2, abs=0.01) and _fillet_size(p, 59.99) == pytest.approx(6, abs=0.01)
    assert _fillet_size(p, 30) == pytest.approx(4, abs=0.01)
    # removed material ≈ (1 - π/4)·∫r² dx with r linear 2 → 6 over 60 mm
    removed = 60 * 30 * 30 - p.result.volume_mm3
    assert removed == pytest.approx((1 - math.pi / 4) * 60 * (4 + 12 + 36) / 3, rel=0.03)
    p.undo()
    ok(p.execute("add_feature", id="vf", type="variable_fillet", params={"edge": {"near": [30, 0, 30]}, "r1": 2, "r2": 6, "start": [60, 0, 30]}))
    assert _fillet_size(p, 0.01) == pytest.approx(6, abs=0.01) and _fillet_size(p, 59.99) == pytest.approx(2, abs=0.01)
    ok(p.execute("update_feature", id="vf", params={"reverse": True}))
    assert _fillet_size(p, 0.01) == pytest.approx(2, abs=0.01) and _fillet_size(p, 59.99) == pytest.approx(6, abs=0.01)


def test_variable_fillet_intermediate_point_and_errors():
    p = Project()
    ok(p.execute("create_plate", length=60, width=30, thickness=30))
    ok(p.execute("add_feature", id="vf", type="variable_fillet", params={
        "edge": {"near": [30, 0, 30]}, "r1": 2, "r2": 2, "points": [[0.5, 8]]}))
    assert _fillet_size(p, 30) == pytest.approx(8, abs=0.01)
    assert _fillet_size(p, 0.01) == pytest.approx(2, abs=0.01) and _fillet_size(p, 59.99) == pytest.approx(2, abs=0.01)
    r = p.execute("add_feature", id="vf2", type="variable_fillet", params={"edge": {"near": [0, 15, 30]}, "r1": 40, "r2": 60})
    assert not r.ok and "try smaller radii" in r.message
    r = p.execute("add_feature", id="vf2", type="variable_fillet", params={"edge": "|Z", "r1": 1, "r2": 2})
    assert not r.ok and "exactly one edge" in r.message
    r = p.execute("add_feature", id="vf2", type="variable_fillet", params={"edge": {"near": [0, 15, 30]}, "r1": 1, "r2": 2, "points": [[1.2, 3]]})
    assert not r.ok and "between 0 and 1" in r.message


# ---------------- Hole Wizard ----------------

def plate():
    p = Project()
    ok(p.execute("create_plate", length=80, width=40, thickness=10))
    return p


def test_hole_wizard_counterbore_matches_iso_tables():
    p = plate()
    ok(p.execute("add_feature", id="h", type="hole", params={
        "plane": {"face": ">Z"}, "positions": [[20, 20]], "standard": "ISO", "type": "counterbore", "size": "M6", "fit": "normal"}))
    h = p.result.holes[0]
    assert (h.diameter, h.cbore) == (CLEARANCE["M6"][1], CBORE_ISO4762["M6"]) == (6.6, (11.0, 6.0))
    removed = 80 * 40 * 10 - p.result.volume_mm3
    assert removed == pytest.approx(math.pi / 4 * (6.6 ** 2 * 4 + 11 ** 2 * 6), rel=1e-6)
    svg = make_drawing(p.result, p.spec)["svg"]
    assert "Ø6.6 THRU  C&apos;BORE Ø11 ↧6" in svg.replace("'", "&apos;") and "ISO 4762" in svg


def test_hole_wizard_countersink_clearance_and_tapped():
    p = plate()
    ok(p.execute("add_feature", id="h", type="hole", params={
        "plane": {"face": ">Z"}, "positions": [[20, 20]], "standard": "ISO", "type": "countersink", "size": "M8", "fit": "close"}))
    h = p.result.holes[0]
    assert (h.diameter, h.csk) == (8.4, (CSK_ISO10642["M8"], 90))
    R, r = 17.92 / 2, 8.4 / 2
    hc = R - r  # 90° countersink: depth = radial step
    removed = math.pi * r * r * 10 + math.pi * hc / 3 * (R * R + R * r + r * r) - math.pi * r * r * hc
    assert 80 * 40 * 10 - p.result.volume_mm3 == pytest.approx(removed, rel=1e-6)

    ok(p.execute("add_hole", id="c", positions=[[60, 10]], standard="ISO", type="clearance", size="M10", fit="loose"))
    assert p.result.holes[-1].diameter == 12.0
    ok(p.execute("add_feature", id="t", type="hole", params={
        "plane": {"face": ">Z"}, "positions": [[60, 30]], "depth": 8, "thread_depth": 6, "standard": "ISO", "type": "tapped", "size": "M8"}))
    t = p.result.holes[-1]
    assert (t.diameter, t.thread, t.thread_depth, t.depth) == (6.8, "M8×1.25", 6, 8)
    ok(p.execute("add_feature", id="t2", type="hole", params={
        "plane": {"face": ">Z"}, "positions": [[40, 30]], "standard": "ISO", "type": "tapped", "size": "M8x1"}))
    assert (p.result.holes[-1].diameter, p.result.holes[-1].thread) == (7.0, "M8×1")


def test_hole_wizard_rejects_unknown_sizes_and_conflicts():
    p = plate()
    base = {"plane": {"face": ">Z"}, "positions": [[20, 20]], "standard": "ISO"}
    cases = [({"type": "counterbore", "size": "M7"}, "no ISO counterbore hole for size 'M7'"),
             ({"type": "countersink", "size": "M24"}, "no ISO countersink hole"),
             ({"type": "tapped", "size": "M8x0.9"}, "no ISO tapped hole M8×0.9"),
             ({"type": "clearance", "size": "M6", "fit": "tight"}, "fit must be close, normal or loose"),
             ({"type": "slot", "size": "M6"}, "type must be one of"),
             ({"type": "clearance", "size": "M6", "diameter": 7}, "diameter comes from the standard table"),
             ({"type": "clearance", "size": "M6", "standard": "ANSI"}, "standard must be \"ISO\"")]
    for extra, msg in cases:
        r = p.execute("add_feature", id="h", type="hole", params={**base, **extra})
        assert not r.ok and msg in r.message, (extra, r.message)


# ---------------- section views ----------------

def _section_paths(svg, label):
    out = []
    for m in re.finditer(r'<path d="([^"]+)" fill="url\(#hatch\)"[^>]*data-section="' + label + '"', svg):
        out.append([tuple(map(float, q.split(","))) for q in re.findall(r"-?[\d.]+,-?[\d.]+", m.group(1))])
    return out


def test_section_view_hatch_outline_and_cutting_line():
    p = plate()
    ok(p.execute("add_hole", id="bore", diameter=10, positions=[[40, 20]]))
    d = make_drawing(p.result, p.spec, "A3", "first", sections=[{"plane": "XZ"}])
    svg = d["svg"]
    assert d["sections"] == [{"label": "A", "plane": "XZ", "offset": 20.0, "view": "top"}]
    assert '<pattern id="hatch"' in svg and "SECTION A-A" in svg and 'class="cutplane"' in svg
    k = float(d["scale"].split(":")[0]) / float(d["scale"].split(":")[1])
    pieces = _section_paths(svg, "A")
    assert len(pieces) == 2  # the bore splits the cut face in two: 35 × 10 each
    for pts in pieces:
        xs, ys = [a for a, _ in pts], [b for _, b in pts]
        assert (max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx((35 * k, 10 * k), abs=1e-3)
    allx = [a for pts in pieces for a, _ in pts]
    assert max(allx) - min(allx) == pytest.approx(80 * k, abs=1e-3)
    # the cutting-plane letters appear on the parent view, and the section view does not overlap the standard views
    assert svg.count(">A</text>") == 2
    assert to_pdf(svg).startswith(b"%PDF")


def test_section_offset_with_inner_loop_and_errors():
    p = plate()
    ok(p.execute("add_hole", id="bore", diameter=10, positions=[[40, 20]]))
    d = make_drawing(p.result, p.spec, "A3", "first", sections=[{"plane": "XY", "offset": 5, "label": "C"}])
    (pts,) = _section_paths(d["svg"], "C")
    k = float(d["scale"].split(":")[0]) / float(d["scale"].split(":")[1])
    xs, ys = [a for a, _ in pts], [b for _, b in pts]
    assert (max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx((80 * k, 40 * k), abs=1e-3)
    assert d["sections"][0]["view"] == "front" and re.search(r'data-section="C"', d["svg"])
    path = re.search(r'<path d="([^"]+)"[^>]*data-section="C"', d["svg"]).group(1)
    assert path.count("M ") == 2  # outer outline + the bore as an inner loop (evenodd leaves it unhatched)
    with pytest.raises(ValueError, match="misses the part"):
        make_drawing(p.result, p.spec, sections=[{"plane": "XZ", "offset": 50}])
    with pytest.raises(ValueError, match="plane must be XY, YZ or XZ"):
        make_drawing(p.result, p.spec, sections=[{"plane": "AB"}])


def test_section_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("AI_CAD_CACHE", str(tmp_path / "cache"))
    import server.app as appmod
    appmod.S.project, appmod.S.path, appmod.S.assistant = appmod.Project(), None, None
    client = TestClient(appmod.app)
    assert client.post("/api/command", json={"command": "create_plate", "args": {"length": 80, "width": 40, "thickness": 10}}).json()["result"]["ok"]
    r = client.get("/api/drawing.svg", params={"sections": json.dumps([{"plane": "YZ"}])})
    assert r.status_code == 200 and "SECTION A-A" in r.text
    assert client.get("/api/drawing.svg", params={"sections": json.dumps([{"plane": "YZ", "offset": 999}])}).status_code == 400
    out = tmp_path / "d.svg"
    r = client.post("/api/drawing/export", json={"path": str(out), "sections": [{"plane": "XZ", "flip": True}]})
    assert r.status_code == 200 and "SECTION A-A" in out.read_text()


# ---------------- sheet-metal hems ----------------

def sheet():
    p = Project()
    ok(p.execute("set_sheet_metal", thickness=2, bend_radius=2, k_factor=0.44))
    ok(p.execute("add_feature", id="base", type="sheet_base", params={"plane": "XY", "shapes": [{"type": "rect", "w": 100, "h": 60, "centered": False}]}))
    return p


def test_open_and_closed_hems_and_flat_pattern():
    p = sheet()
    ok(p.execute("add_feature", id="hem", type="sheet_hem", params={"edge": {"near": [50, 0, 2]}, "length": 10, "type": "open", "gap": 2}))
    # half annulus (inner radius 1, outer 3) plus a 10 mm return, 100 mm wide
    assert p.result.volume_mm3 == pytest.approx(12000 + math.pi / 2 * (9 - 1) * 100 + 10 * 2 * 100, rel=1e-6)
    bb = p.result.shape.val().BoundingBox()
    assert (bb.zmin, bb.zmax) == pytest.approx((0, 6))  # base 2 + gap 2 + folded layer 2
    ok(p.execute("add_feature", id="hem2", type="sheet_hem", params={"edge": {"near": [50, 60, 2]}, "length": 10}))
    fp = flat_pattern(p.result.sheet)
    ba_open, ba_closed = bend_allowance(180, 1, 0.44, 2), bend_allowance(180, CLOSED_HEM_GAP / 2, 0.44, 2)
    assert fp["size"] == pytest.approx((100, 60 + ba_open + 10 + ba_closed + 10))
    assert [b["label"] for b in fp["bends"]] == ["HEM UP 180° R1", "HEM UP 180° R0.01"]
    bb = p.result.shape.val().BoundingBox()
    assert bb.zmax == pytest.approx(6)


def test_hem_errors():
    p = sheet()
    r = p.execute("add_feature", id="h", type="sheet_hem", params={"edge": {"near": [50, 0, 2]}, "length": 10, "type": "open"})
    assert not r.ok and "gap must be a number" in r.message
    r = p.execute("add_feature", id="h", type="sheet_hem", params={"edge": {"near": [50, 0, 2]}, "length": 10, "gap": 1})
    assert not r.ok and "closed hem has no gap" in r.message
    ok(p.execute("add_feature", id="h1", type="sheet_hem", params={"edge": {"near": [50, 0, 2]}, "length": 10}))
    r = p.execute("add_feature", id="h2", type="sheet_hem", params={"edge": {"near": [0, 30, 2]}, "length": 10})
    assert not r.ok and "collides" in r.message
