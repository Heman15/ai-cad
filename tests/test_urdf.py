# SPDX-License-Identifier: AGPL-3.0-or-later
"""URDF/xacro import: parsing, units, mesh conversion, forward kinematics, limits and honest reporting."""

import math
from pathlib import Path

import numpy as np
import pytest

from cadcore.assembly import AssemblyError, AssemblyProject
from cadcore.urdf import import_urdf, parse, rpy_matrix

ARM = Path(__file__).resolve().parent.parent / "examples" / "robot_arm" / "urdf" / "demo_arm.urdf.xacro"


def test_rpy_matches_urdf_convention():
    # roll about X, then pitch about Y, then yaw about Z (fixed axes): R = Rz Ry Rx
    R = rpy_matrix(math.pi / 2, 0, math.pi / 2)
    assert np.allclose(R @ [0, 1, 0], [0, 0, 1]) and np.allclose(R @ [1, 0, 0], [0, 1, 0])


def test_parse_xacro_units_and_joints():
    r = parse(ARM)
    assert r["root"] == "base_link" and len(r["links"]) == 9 and len(r["joints"]) == 8
    shoulder = next(j for j in r["joints"] if j["name"] == "shoulder")
    assert shoulder["origin"]["xyz"] == [0, 0, 80] and math.isclose(shoulder["upper"], math.degrees(1.57))
    slide = next(j for j in r["joints"] if j["name"] == "finger_left_slide")
    assert slide["upper"] == pytest.approx(20)  # metres -> mm
    assert next(j for j in r["joints"] if j["name"] == "finger_right_slide")["mimic"] == "finger_left_slide"
    cyl = r["links"]["upper_arm"]["visuals"][0]
    assert cyl["kind"] == "cylinder" and cyl["radius"] == pytest.approx(30) and cyl["color"] == "#f2731a"


def test_import_builds_parts_and_moves_joints(tmp_path):
    r = import_urdf(ARM, tmp_path / "out")
    rep = r["report"]
    assert rep["parametric_parts"] == 6 and rep["mesh_parts"] == 2 and not rep["skipped"]
    assert rep["meshes"]["finger.brep"]["faces"] == 8 and rep["meshes"]["finger.brep"]["volume_mm3"] == pytest.approx(16320, rel=1e-6)
    cam = rep["meshes"]["camera.brep"]["volume_mm3"]  # DAE in centimetres: r 20 mm, h 30 mm, 48-sided
    assert cam == pytest.approx(0.5 * 48 * 20**2 * math.sin(2 * math.pi / 48) * 30, rel=1e-6)
    a = AssemblyProject.load(r["assembly"])
    comps = {c.id: c for c in a.spec.components}
    assert comps["wrist"].position == pytest.approx([0, 0, 630])
    assert a.move_joints({"shoulder": 45})["ok"]
    comps = {c.id: c for c in a.spec.components}
    s = 500 * math.sin(math.radians(45))
    assert comps["wrist"].position == pytest.approx([s, 0, 130 + s], abs=1e-6)
    assert a.move_joints({"finger_left_slide": 10})["ok"]
    comps = {c.id: c for c in a.spec.components}
    assert math.dist(comps["finger_left"].position, comps["finger_right"].position) == pytest.approx(60)
    assert not a.move_joints({"elbow": 200})["ok"]  # beyond ±2.5 rad
    assert not a.move_joints({"finger_right_slide": 3})["ok"]  # mimic joints follow, they aren't driven
    bom = {row["part"]: row["qty"] for row in a.bom()}
    assert bom["finger"] == 2 and bom["cylinder D60 L250"] == 2
    a.save(str(tmp_path / "moved.aicadasm"))
    again = AssemblyProject.load(str(tmp_path / "moved.aicadasm"))
    assert {c.id: c for c in again.spec.components}["wrist"].position == pytest.approx([s, 0, 130 + s], abs=1e-6)


def test_sphere_part_is_parametric(tmp_path):
    from cadcore import Project

    a = AssemblyProject.load(import_urdf(ARM, tmp_path)["assembly"])
    p = Project.load(tmp_path / next(c.file for c in a.spec.components if c.id == "wrist"))
    assert p.result.volume_mm3 == pytest.approx(4 / 3 * math.pi * 35**3, rel=1e-6)
    assert p.execute("set_variable", name="radius", value=40).ok
    assert p.result.volume_mm3 == pytest.approx(4 / 3 * math.pi * 40**3, rel=1e-6)


def test_missing_mesh_is_reported_not_guessed(tmp_path):
    u = tmp_path / "r.urdf"
    u.write_text("""<robot name="r"><link name="a"><visual><geometry><box size="0.1 0.1 0.1"/></geometry></visual></link>
      <link name="b"><visual><geometry><mesh filename="package://nowhere/m.stl"/></geometry></visual></link>
      <joint name="j" type="revolute"><parent link="a"/><child link="b"/><axis xyz="0 0 1"/><limit lower="-1" upper="1"/></joint></robot>""")
    rep = import_urdf(u, tmp_path / "o")["report"]
    assert rep["parametric_parts"] == 1 and "not found" in rep["skipped"][0]


def test_not_a_tree_is_rejected(tmp_path):
    u = tmp_path / "r.urdf"
    u.write_text('<robot name="r"><link name="a"/><link name="b"/></robot>')
    with pytest.raises(AssemblyError, match="one tree"):
        parse(u)
