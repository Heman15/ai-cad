# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemblies: mate solving (incl. a 180° flip), DOF, conflicts, interference, BOM, persistence, STEP, API."""

import cadquery as cq
import pytest
from fastapi.testclient import TestClient

from cadcore import Project
from cadcore.assembly import AssemblyProject, rotation_matrix, rotation_vector

TOP = {"point": [20, 20, 10], "normal": [0, 0, 1]}
BORE = {"point": [55, 30, 5]}
PIN_SHANK = {"point": [5, 0, 20]}
PIN_HEAD_TOP = {"point": [6, 0, 4], "normal": [0, 0, 1]}


@pytest.fixture()
def parts(tmp_path):
    b = Project()
    b.execute("create_plate", length=100, width=60, thickness=10)
    b.execute("add_hole", id="bore", diameter=10, positions=[[50, 30]])
    b.execute("assign_material", name="steel")
    b.execute("set_name", name="Base")
    b.save(tmp_path / "base.aicad")
    p = Project()
    p.execute("create_shaft", segments=[{"diameter": 16, "length": 4}, {"diameter": 10, "length": 30}])
    p.execute("assign_material", name="steel")
    p.execute("set_name", name="Pin")
    p.save(tmp_path / "pin.aicad")
    return tmp_path


def ok(r):
    assert r["ok"], r["message"]


def test_rotation_vector_roundtrip():
    for rv in ([0, 0, 0], [90, 0, 0], [0, 180, 0], [30, 40, -50], [180, 0, 0]):
        assert rotation_matrix(rotation_vector(rotation_matrix(rv))) == pytest.approx(rotation_matrix(rv))


def test_pin_in_bore(parts):
    a = AssemblyProject(path=str(parts / "asm.aicadasm"))
    ok(a.execute("add_component", file="base.aicad"))
    ok(a.execute("add_component", file="pin.aicad", position=[20, 10, 30]))
    assert a.status["dof"] == 6
    ok(a.execute("add_mate", type="concentric", a={"component": "base_1", "face": BORE}, b={"component": "pin_1", "face": PIN_SHANK}))
    ok(a.execute("add_mate", type="coincident", a={"component": "base_1", "face": TOP}, b={"component": "pin_1", "face": PIN_HEAD_TOP}))
    assert a.status["converged"] and a.status["dof"] == 1  # free spin about the pin axis
    bb = a.world_shapes()["pin_1"].BoundingBox()  # flipped: head on the plate, shank down through the bore
    assert (bb.xmin, bb.xmax, bb.ymin, bb.ymax, bb.zmin, bb.zmax) == pytest.approx((42, 58, 22, 38, -20, 14), abs=1e-6)
    assert a.interference() == []

    r = a.execute("add_mate", type="distance", a={"component": "base_1", "face": TOP}, b={"component": "pin_1", "face": PIN_HEAD_TOP}, value=5)
    assert not r["ok"] and "conflict" in r["message"]
    bom = {row["part"]: row for row in a.bom()}
    assert bom["Pin"]["qty"] == 1 and bom["Base"]["mass_each_kg"] == pytest.approx(0.4648, rel=1e-3)

    a.save(str(parts / "asm.aicadasm"))
    b = AssemblyProject.load(str(parts / "asm.aicadasm"))
    assert [c.file for c in b.spec.components] == ["base.aicad", "pin.aicad"] and b.status["converged"]
    a.export_step(str(parts / "asm.step"))
    assert len(cq.importers.importStep(str(parts / "asm.step")).solids().vals()) == 2


def test_interference_and_edit_propagation(parts):
    a = AssemblyProject(path=str(parts / "a.aicadasm"))
    ok(a.execute("add_component", file="base.aicad"))
    ok(a.execute("add_component", file="base.aicad", position=[30, 0, 0]))
    hit = a.interference()
    overlap = 70 * 60 * 10 - 2 * 3.141592653589793 * 25 * 10  # both bores lie inside the overlap
    assert len(hit) == 1 and hit[0]["volume_mm3"] == pytest.approx(overlap, rel=1e-9)
    # editing the part file changes the assembly (components reference files)
    p = Project.load(parts / "base.aicad")
    assert p.execute("set_parameter", name="vars.thickness", value=5).ok
    p.save(parts / "base.aicad")
    assert a.interference()[0]["volume_mm3"] == pytest.approx(overlap / 2, rel=1e-9)


def test_assembly_api(parts, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "c.json"))
    import server.app as appmod
    appmod.A.project = None
    api = TestClient(appmod.app)
    api.post("/api/asm/new")
    r = api.post("/api/asm/command", json={"command": "add_component", "args": {"file": str(parts / "base.aicad")}}).json()
    assert r["result"]["ok"] and r["model"]["entities"]["faces"]
    face = r["model"]["entities"]["faces"][0]
    assert {"component", "local_point", "point"} <= set(face)
    r = api.post("/api/asm/command", json={"command": "add_component", "args": {"file": str(parts / "nope.aicad")}}).json()
    assert not r["result"]["ok"] and "not found" in r["result"]["message"]
    assert api.post("/api/asm/save", json={"path": str(parts / "x.aicadasm")}).json()["ok"]
