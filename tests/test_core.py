# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geometry, regeneration, references, persistence and transactions for the v2 feature model."""

import json
import math

import cadquery as cq
import pytest

from cadcore import Project
from cadcore.spec import PartSpec


def approx(a, b, rel=1e-6):
    return a == pytest.approx(b, rel=rel)


def plate(length=100, width=60, thickness=5):
    p = Project()
    assert p.execute("create_plate", length=length, width=width, thickness=thickness).ok
    return p


# ---------- geometry ----------

def test_plate_with_hole_volume_and_mass():
    p = plate()
    r = p.execute("add_hole", id="h1", diameter=6, x=20, y=15)
    assert r.ok, r.message
    expected = 100 * 60 * 5 - math.pi * 9 * 5
    assert approx(p.result.volume_mm3, expected)
    assert p.execute("assign_material", name="aluminum_6061").ok
    assert approx(p.execute("calculate_mass").data["mass_kg"], expected * 1e-9 * 2700)


def test_blind_pocket_and_counterbore():
    p = plate(thickness=10)
    assert p.execute("add_pocket", id="pk", length=20, width=10, depth=4, x=50, y=30).ok
    assert approx(p.result.volume_mm3, 60000 - 800)
    r = p.execute("add_hole", id="cb", diameter=6, x=20, y=20, cbore_diameter=10, cbore_depth=3)
    assert r.ok, r.message
    assert approx(p.result.volume_mm3, 60000 - 800 - math.pi * 9 * 7 - math.pi * 25 * 3)


def test_l_bracket_holes_on_both_flanges():
    p = Project()
    p.execute("create_l_bracket", length=80, width=40, thickness=5, height=60)
    base_vol = 80 * 40 * 5 + 5 * 40 * 55
    assert approx(p.result.volume_mm3, base_vol)
    # base flange: XY plane at z = thickness, drilled downward
    r = p.execute("add_hole", id="base_hole", plane={"base": "XY", "offset": "=thickness"}, diameter=6, positions=[[50, 20]])
    assert r.ok, r.message
    # upright: YZ plane at x = thickness, drilled toward -X
    r = p.execute("add_hole", id="wall_hole", plane={"base": "YZ", "offset": "=thickness"}, diameter=8, positions=[[20, 40]])
    assert r.ok, r.message
    assert approx(p.result.volume_mm3, base_vol - math.pi * 9 * 5 - math.pi * 16 * 5)


def test_stepped_shaft_and_flange_templates():
    p = Project()
    p.execute("create_shaft", segments=[{"diameter": 20, "length": 30}, {"diameter": 12, "length": 50}])
    assert approx(p.result.volume_mm3, math.pi * (100 * 30 + 36 * 50), rel=1e-5)
    assert p.execute("set_parameter", name="vars.d2", value=10).ok
    assert approx(p.result.volume_mm3, math.pi * (100 * 30 + 25 * 50), rel=1e-5)
    assert p.execute("set_parameter", name="vars.l1", value=40).ok  # later segment moves with it
    assert p.result.bbox_mm[2] == pytest.approx(90)

    f = Project()
    f.execute("create_flange", outer_diameter=100, inner_diameter=40, thickness=10,
              bolt_circle_diameter=75, bolt_count=6, bolt_diameter=9)
    assert approx(f.result.volume_mm3, math.pi / 4 * (100**2 - 40**2 - 6 * 81) * 10, rel=1e-5)
    assert f.execute("set_parameter", name="vars.n_bolts", value=8).ok
    assert approx(f.result.volume_mm3, math.pi / 4 * (100**2 - 40**2 - 8 * 81) * 10, rel=1e-5)


def test_patterns_mirror_fillet_chamfer_shell():
    p = plate()
    p.execute("add_hole", id="h1", diameter=6, x=10, y=10)
    r = p.execute("add_feature", id="grid", type="pattern_linear", params={
        "feature": "h1", "count": 2, "step": ["=length-20", 0, 0], "count2": 2, "step2": [0, "=width-20", 0]})
    assert r.ok, r.message
    assert approx(p.result.volume_mm3, 30000 - 4 * math.pi * 9 * 5)
    assert sum(len(h.points) for h in p.result.holes) == 4

    assert p.execute("add_feature", id="round", type="fillet", params={"edges": {"near": [[0, 0, 2.5]]}, "radius": 5}).ok
    assert p.result.volume_mm3 < 30000 - 4 * math.pi * 9 * 5

    m = plate()
    m.execute("add_pocket", id="pk", length=10, width=10, depth=2, x=20, y=30)
    assert m.execute("add_feature", id="mir", type="mirror", params={"feature": "pk", "plane": "YZ", "offset": "=length/2"}).ok
    assert approx(m.result.volume_mm3, 30000 - 2 * 200)
    assert m.execute("add_feature", id="ch", type="chamfer", params={"edges": "feature:pk", "distance": 0.5}).ok

    s = plate(80, 50, 30)
    assert s.execute("add_feature", id="sh", type="shell", params={"faces": ">Z", "thickness": 2}).ok
    assert approx(s.result.volume_mm3, 80 * 50 * 30 - 76 * 46 * 28)


def test_formulas_units_and_cycles():
    p = plate()
    assert p.execute("set_variable", name="margin", value=12).ok
    assert p.execute("add_hole", id="h1", diameter=6, positions=[["=margin", "=width/2"]]).ok
    assert p.result.holes[0].points[0].x == pytest.approx(12)
    assert p.execute("set_parameter", name="vars.width", value="3 in").ok
    assert p.result.bbox_mm[1] == pytest.approx(76.2)
    assert p.result.holes[0].points[0].y == pytest.approx(38.1)

    r = p.execute("set_variable", name="margin", value="=gap + 1")
    assert not r.ok and "unknown variable 'gap'" in r.message
    p.execute("set_variable", name="a", value="=b + 1")  # rejected: b missing
    assert p.execute("set_variable", name="b", value=1).ok
    assert p.execute("set_variable", name="a", value="=b + 1").ok
    r = p.execute("set_variable", name="b", value="=a * 2")
    assert not r.ok and "circular dependency" in r.message
    assert not p.execute("set_parameter", name="vars.length", value="=__import__('os')").ok


# ---------- validation & rollback ----------

def test_invalid_edits_roll_back():
    p = plate()
    p.execute("add_hole", id="h1", diameter=6, x=20, y=15)
    vol = p.result.volume_mm3
    for cmd, args in [
        ("set_parameter", {"name": "h1.diameter", "value": 50}),        # breaks out of the face
        ("set_parameter", {"name": "h1.diameter", "value": -3}),        # not positive
        ("set_parameter", {"name": "h9.diameter", "value": 5}),         # unknown feature
        ("set_parameter", {"name": "vars.thickness", "value": 0}),      # degenerate solid
        ("add_pocket", {"id": "pk", "length": 10, "width": 10, "depth": 0, "x": 50, "y": 30}),  # zero depth
        ("add_feature", {"id": "far", "type": "extrude", "params": {   # disconnected body
            "plane": "XY", "distance": 5, "shapes": [{"type": "rect", "w": 5, "h": 5, "x": 500, "y": 500}]}}),
        ("add_feature", {"id": "f", "type": "fillet", "params": {"edges": "|Z", "radius": 80}}),
    ]:
        r = p.execute(cmd, **args)
        assert not r.ok, (cmd, args)
        assert p.spec.feature("h1").params["diameter"] == 6
        assert p.result.volume_mm3 == vol


def test_dependencies_block_unsafe_removal():
    p = plate()
    p.execute("add_hole", id="h1", diameter=6, x=10, y=10)
    p.execute("add_feature", id="pat", type="pattern_linear", params={"feature": "h1", "count": 3, "step": [20, 0, 0]})
    r = p.execute("remove_feature", id="h1")
    assert not r.ok and "pat" in r.message
    r = p.execute("remove_variable", name="thickness")
    assert not r.ok and "base" in r.message


def test_stale_face_reference_is_reported_not_guessed():
    p = plate()
    top = {"face_at": {"point": [50, 30, 5], "normal": [0, 0, 1]}}
    assert p.execute("add_hole", id="h1", diameter=6, plane=top, positions=[[20, 20]]).ok
    r = p.execute("set_parameter", name="vars.thickness", value=8)  # stored point is no longer on the top face
    assert not r.ok and "no longer exists" in r.message
    assert p.spec.variables["thickness"] == 5


def test_undo_redo_and_batch():
    p = plate()
    p.execute("add_hole", id="h1", diameter=6, x=20, y=15)
    p.execute("set_parameter", name="h1.diameter", value=8)
    assert p.undo().ok and p.spec.feature("h1").params["diameter"] == 6
    assert p.redo().ok and p.spec.feature("h1").params["diameter"] == 8

    r = p.execute_batch([
        {"command": "set_parameter", "args": {"name": "vars.thickness", "value": 10}},
        {"command": "add_hole", "args": {"id": "h2", "diameter": 5, "x": 80, "y": 15}},
    ], label="redesign")
    assert r.ok, r.message
    assert r.data["diff"]["added"] == ["h2"]
    assert p.undo().ok
    assert p.spec.feature("h2") is None and p.spec.variables["thickness"] == 5

    bad = p.execute_batch([
        {"command": "set_parameter", "args": {"name": "vars.thickness", "value": 10}},
        {"command": "set_parameter", "args": {"name": "h1.diameter", "value": 99}},
    ])
    assert not bad.ok and "step 2" in bad.message and p.spec.variables["thickness"] == 5

    prev = p.preview([{"command": "set_parameter", "args": {"name": "vars.length", "value": 120}}])
    assert prev["ok"] and prev["bbox_mm"][0] == pytest.approx(120)
    assert p.spec.variables["length"] == 100  # preview never commits


# ---------- persistence ----------

def test_save_load_and_step_roundtrip(tmp_path):
    p = plate()
    p.execute("add_hole", id="h1", diameter=6, x=20, y=15)
    p.execute("assign_material", name="steel")
    p.save(tmp_path / "part.json")

    q = Project.load(tmp_path / "part.json")
    assert q.spec.parameters() == p.spec.parameters()
    assert q.spec.material.density == 7850
    assert approx(q.result.volume_mm3, p.result.volume_mm3)
    assert q.execute("set_parameter", name="h1.diameter", value=10).ok  # history stays editable

    step = tmp_path / "part.step"
    assert q.execute("export_step", path=step).ok
    reimported = cq.importers.importStep(str(step)).val()
    assert approx(reimported.Volume(), q.result.volume_mm3)
    bb = reimported.BoundingBox()
    assert (bb.xlen, bb.ylen, bb.zlen) == pytest.approx((100, 60, 5))


def test_v1_projects_migrate(tmp_path):
    v1 = {
        "base_type": "l_bracket", "base": {"length": 80, "width": 40, "thickness": 5, "height": 60},
        "features": [
            {"id": "bh", "type": "hole", "params": {"diameter": 6, "depth": "through", "x": 55, "y": 12}},
            {"id": "wh", "type": "hole", "params": {"diameter": 10, "depth": "through", "face": "upright", "y": 20, "z": 40}},
            {"id": "pk", "type": "pocket", "params": {"length": 20, "width": 10, "depth": 2, "x": 30, "y": 20}},
        ],
        "material": {"name": "aluminum_6061", "density": 2700}, "units": "mm", "notes": [], "spec_version": 1,
    }
    path = tmp_path / "old.json"
    path.write_text(json.dumps(v1))
    p = Project.load(path)
    expected = 80 * 40 * 5 + 5 * 40 * 55 - math.pi * 9 * 5 - math.pi * 25 * 5 - 20 * 10 * 2
    assert approx(p.result.volume_mm3, expected)
    assert p.spec.material.density == 2700
    assert "migrated from project format v1" in p.spec.source["migration_notes"]
    assert PartSpec.from_dict(p.spec.to_dict()).to_dict()["spec_version"] == 2
