# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemblies: mate solving (incl. a 180° flip), DOF, conflicts, interference, BOM, persistence, STEP, API."""

import math

import cadquery as cq
import numpy as np
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


# ---- block on a base: mate types, editing, suppression, lock, move, explode, mass, measure ----
BASE_X = {"point": [100, 30, 5], "normal": [1, 0, 0]}
BLOCK_BOTTOM = {"point": [15, 10, 0], "normal": [0, 0, -1]}
BLOCK_X = {"point": [30, 10, 4], "normal": [1, 0, 0]}


@pytest.fixture()
def blocks(parts):
    c = Project()
    c.execute("create_plate", length=30, width=20, thickness=8)
    c.execute("assign_material", name="aluminum_6061")
    c.execute("set_name", name="Block")
    c.save(parts / "block.aicad")
    a = AssemblyProject(path=str(parts / "b.aicadasm"))
    ok(a.execute("add_component", file="base.aicad"))
    ok(a.execute("add_component", file="block.aicad", position=[130, 0, 20]))
    return a


def world_dir(a, cid, v):
    c = a.spec.component(cid)
    return rotation_matrix(c.rotation) @ np.asarray(v, dtype=float)


def zrange(a, cid):
    bb = a.world_shapes()[cid].BoundingBox()
    return bb.zmin, bb.zmax


def test_perpendicular_from_parallel_start(blocks):
    a = blocks
    ok(a.execute("add_mate", type="coincident", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}))
    # the side faces start parallel (a saddle point for the solver); prealign turns the block about the coincident normal
    ok(a.execute("add_mate", type="perpendicular", a={"component": "base_1", "face": BASE_X}, b={"component": "block_1", "face": BLOCK_X}))
    assert a.status["converged"] and a.status["dof"] == 2
    assert world_dir(a, "block_1", [1, 0, 0]) @ [1, 0, 0] == pytest.approx(0, abs=1e-8)
    assert world_dir(a, "block_1", [0, 0, -1]) == pytest.approx([0, 0, -1], abs=1e-8)  # still sitting on the base
    assert zrange(a, "block_1") == pytest.approx((10, 18), abs=1e-6)


def test_angle_mate_and_edit_value(blocks):
    a = blocks
    ok(a.execute("add_mate", type="coincident", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}))
    ok(a.execute("add_mate", type="angle", a={"component": "base_1", "face": BASE_X}, b={"component": "block_1", "face": BLOCK_X}, value=30))
    assert a.status["dof"] == 2
    assert world_dir(a, "block_1", [1, 0, 0]) @ [1, 0, 0] == pytest.approx(math.cos(math.radians(30)), abs=1e-8)
    angle_id = a.spec.mates[-1]["id"]
    ok(a.execute("edit_mate", id=angle_id, value=120))
    assert world_dir(a, "block_1", [1, 0, 0]) @ [1, 0, 0] == pytest.approx(math.cos(math.radians(120)), abs=1e-8)
    ok(a.execute("flip_mate", id=angle_id))  # 180 - 120
    assert a.spec.mates[-1]["value"] == 60
    assert world_dir(a, "block_1", [1, 0, 0]) @ [1, 0, 0] == pytest.approx(0.5, abs=1e-8)
    assert world_dir(a, "block_1", [0, 0, -1]) == pytest.approx([0, 0, -1], abs=1e-8)
    assert not a.execute("edit_mate", id=angle_id, value=200)["ok"]


def test_coincident_flip_suppress_and_distance_edit(blocks):
    a = blocks
    ok(a.execute("add_mate", type="coincident", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}))
    mid = a.spec.mates[0]["id"]
    assert zrange(a, "block_1") == pytest.approx((10, 18), abs=1e-6) and a.status["dof"] == 3
    ok(a.execute("flip_mate", id=mid))  # normals aligned: the block turns over and hangs below the top face
    assert a.spec.mates[0]["aligned"] and zrange(a, "block_1") == pytest.approx((2, 10), abs=1e-6)
    assert world_dir(a, "block_1", [0, 0, -1]) == pytest.approx([0, 0, 1], abs=1e-8)
    ok(a.execute("flip_mate", id=mid))
    assert zrange(a, "block_1") == pytest.approx((10, 18), abs=1e-6)
    ok(a.execute("edit_mate", id=mid, suppressed=True))  # kept, but no longer constrains
    assert a.spec.mates[0]["suppressed"] and a.status["dof"] == 6
    ok(a.execute("set_transform", id="block_1", position=[130, 0, 40]))
    assert zrange(a, "block_1")[0] > 30
    ok(a.execute("edit_mate", id=mid, suppressed=False))
    assert zrange(a, "block_1") == pytest.approx((10, 18), abs=1e-6)
    ok(a.execute("remove_mate", id=mid))
    ok(a.execute("add_mate", type="distance", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}, value=5))
    assert zrange(a, "block_1") == pytest.approx((15, 23), abs=1e-6)
    ok(a.execute("edit_mate", id=a.spec.mates[0]["id"], value=12.5))
    assert zrange(a, "block_1") == pytest.approx((22.5, 30.5), abs=1e-6)


def test_lock_mate_follows_its_partner(blocks):
    a = blocks
    ok(a.execute("set_transform", id="block_1", position=[130, 5, 20], rotation=[0, 0, 30]))
    ok(a.execute("add_mate", type="lock", a={"component": "base_1"}, b={"component": "block_1"}))
    assert a.status["dof"] == 0
    blk = a.spec.component("block_1")
    assert blk.position == pytest.approx([130, 5, 20], abs=1e-8) and blk.rotation == pytest.approx([0, 0, 30], abs=1e-8)
    ok(a.execute("set_transform", id="base_1", position=[0, 0, 0], rotation=[0, 0, 90]))  # the fixed base turns: the block goes with it
    blk = a.spec.component("block_1")
    assert blk.position == pytest.approx([-5, 130, 20], abs=1e-6) and blk.rotation == pytest.approx([0, 0, 120], abs=1e-6)
    assert not a.execute("move_component", id="base_1", translate=[1, 0, 0])["ok"]  # fixed


def test_move_component_keeps_mates(blocks):
    a = blocks
    ok(a.execute("add_mate", type="coincident", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}))
    before = list(a.spec.component("block_1").position)
    ok(a.execute("move_component", id="block_1", translate=[5, -3, 50]))  # z is blocked by the mate, x/y are free
    after = a.spec.component("block_1").position
    assert after == pytest.approx([before[0] + 5, before[1] - 3, before[2]], abs=1e-6)
    ok(a.execute("move_component", id="block_1", axis=[0, 0, 1], angle=90))  # spin about its own centre: still on the base
    assert world_dir(a, "block_1", [1, 0, 0]) == pytest.approx([0, 1, 0], abs=1e-8) and zrange(a, "block_1") == pytest.approx((10, 18), abs=1e-6)
    r = a.execute("move_component", id="block_1", axis=[1, 0, 0], angle=90)  # tilting would break the mate: solver puts it back
    assert r["ok"] and world_dir(a, "block_1", [0, 0, -1]) == pytest.approx([0, 0, -1], abs=1e-6)


def test_visibility_pattern_explode_persist(blocks, parts):
    a = blocks
    ok(a.execute("set_visibility", ids="block_1", hidden=True))
    ok(a.execute("set_visibility", ids=["base_1"], transparent=True))
    ok(a.execute("pattern_component", id="block_1", type="linear", count=3, direction=[0, 1, 0], spacing=40))
    assert [c.id for c in a.spec.components] == ["base_1", "block_1", "block_2", "block_3"]
    assert a.spec.component("block_3").position == pytest.approx([130, 80, 20])
    ok(a.execute("pattern_component", id="base_1", type="circular", count=4, axis=[0, 0, 1], point=[0, 0, 0]))
    assert rotation_matrix(a.spec.component("base_4").rotation) == pytest.approx(rotation_matrix([0, 0, 270]), abs=1e-9)
    ok(a.execute("add_explode_step", components=["block_1", "block_2"], direction=[0, 0, 2], distance=40))
    assert a.spec.explode[0]["direction"] == [0, 0, 1]
    ok(a.execute("edit_explode_step", id="step_1", distance=60))
    a.save(str(parts / "b.aicadasm"))
    b = AssemblyProject.load(str(parts / "b.aicadasm"))
    assert b.spec.explode == [{"id": "step_1", "components": ["block_1", "block_2"], "direction": [0, 0, 1], "distance": 60}]
    assert b.spec.component("block_1").hidden and b.spec.component("base_1").transparent
    ok(b.execute("remove_component", id="block_1"))
    assert b.spec.explode[0]["components"] == ["block_2"]
    ok(b.execute("auto_explode", scale=1.0))
    steps = b.spec.explode
    assert len(steps) == 6 and all(abs(np.linalg.norm(s["direction"]) - 1) < 1e-9 and s["distance"] > 0 for s in steps)
    ok(b.execute("clear_explode"))
    assert b.spec.explode == []


def test_mass_properties_and_bom_csv(blocks):
    a = blocks
    mp = a.mass_properties()
    base, block = mp["components"]
    assert base["mass_kg"] == pytest.approx(0.4648, rel=1e-3)  # steel plate with a bore
    assert block["mass_kg"] == pytest.approx(30 * 20 * 8 * 1e-9 * 2700, rel=2e-2)
    assert mp["mass_kg"] == pytest.approx(base["mass_kg"] + block["mass_kg"])
    com = (np.array(base["center_mm"]) * base["mass_kg"] + np.array(block["center_mm"]) * block["mass_kg"]) / mp["mass_kg"]
    assert mp["center_of_mass_mm"] == pytest.approx(com.tolist()) and block["center_mm"] == pytest.approx([145, 10, 24])
    csv = a.bom_csv().splitlines()
    assert csv[0].startswith("item,part,qty") and csv[2].startswith("2,Block,1,aluminum_6061")


def test_assembly_measure_api(blocks, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "c.json"))
    import server.app as appmod
    a = blocks
    ok(a.execute("add_mate", type="distance", a={"component": "base_1", "face": TOP}, b={"component": "block_1", "face": BLOCK_BOTTOM}, value=5))
    appmod.A.project = a
    api = TestClient(appmod.app)
    m = api.get("/api/asm").json()
    faces = m["entities"]["faces"]
    top = next(f for f in faces if f["component"] == "base_1" and f.get("normal") == [0, 0, 1])
    bottom = next(f for f in faces if f["component"] == "block_1" and f.get("normal") and f["normal"][2] < -0.99)
    assert m["entities"]["vertices"] and {"component", "local_point"} <= set(m["entities"]["vertices"][0])
    ref = lambda f: {"kind": "face", "component": f["component"], "point": f["point"], "normal": f["normal"], "type": f["type"], "area": f["area"]}  # noqa: E731
    r = api.post("/api/asm/measure", json={"refs": [ref(top), ref(bottom)]}).json()
    assert r["ok"] and r["parallel_distance_mm"] == pytest.approx(5) and r["angle_deg"] == pytest.approx(0)
    v = next(x for x in m["entities"]["vertices"] if x["component"] == "block_1")
    r = api.post("/api/asm/measure", json={"refs": [{"kind": "vertex", "component": "block_1", "point": v["point"]}]}).json()
    assert r["ok"] and [r["x_mm"], r["y_mm"], r["z_mm"]] == pytest.approx(v["point"])
    assert api.get("/api/asm/mass").json()["mass_kg"] > 0.46
    assert api.get("/api/asm/bom.csv").text.startswith("item,part")
