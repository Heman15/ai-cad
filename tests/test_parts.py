# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parts package: grouping of identical and mirror-image bodies, classification with overrides, end-to-end output."""

import csv
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import meshgen as mg  # noqa: E402
from cadcore import parts  # noqa: E402

ARM = Path(__file__).resolve().parent.parent / "examples" / "robot_arm" / "urdf" / "demo_arm.urdf.xacro"


def _bracket(seed: int, mirror: bool = False):
    case = mg.SheetCase("L", [30, 22], [90], t=1.5, r=1.5, width=25, holes=[(0, 10, 7, 4), (1, 12, 15, 3)], seed=seed)
    m = mg.sheet_mesh(case)
    if mirror:
        m.apply_transform(np.diag([-1.0, 1, 1, 1]))  # trimesh keeps the faces outward
        assert m.volume > 0
    return m


def _bodies(meshes):
    return [parts.make_body(m, f"c{i}", f"c{i}") for i, m in enumerate(meshes)]


def test_identical_bodies_group_and_mirror_is_separate():
    bodies = _bodies([_bracket(1), _bracket(2), _bracket(3, mirror=True), _bracket(4, mirror=True)])
    groups = parts.group_bodies(bodies)
    assert sorted(len(g.bodies) for g in groups) == [2, 2]
    assert sum(1 for g in groups if g.mirror_of is not None) == 1
    a, b = bodies[0], bodies[2]
    assert parts.match(a, b)[0] == "mirror"
    assert parts.match(bodies[0], bodies[1])[0] == "same"


def test_different_hole_is_a_different_part():
    other = mg.SheetCase("L", [30, 22], [90], t=1.5, r=1.5, width=25, holes=[(0, 10, 7, 4), (1, 6, 15, 3)], seed=9)
    a, b = _bodies([_bracket(1), mg.sheet_mesh(other)])
    assert parts.match(a, b)[0] is None


def test_package_classifies_and_overrides(tmp_path):
    import trimesh

    screw = trimesh.creation.cylinder(radius=1.5, height=8, sections=24)
    meshes = [("bracket_left", _bracket(1)), ("bracket_right", _bracket(2, mirror=True))]
    meshes += [(f"screw_{i}", screw.copy()) for i in range(4)]
    r = parts.make_package(meshes, tmp_path, name="demo", drawings=False, licence_note="CC-BY test")
    rows = {p["part_number"]: p for p in r["parts"]}
    assert r["summary"]["parts"] == 3 and r["summary"]["mirrored"] == 1
    sheet = [p for p in rows.values() if p["category"] == "sheet_metal"]
    assert len(sheet) == 2 and all(abs(float(p["thickness_mm"]) - 1.5) < 0.02 for p in sheet)
    assert any(p["mirror_of"] for p in sheet)
    screw_row = next(p for p in rows.values() if p["qty"] == 4)
    assert screw_row["category"] == "purchased"
    folder = Path(r["folder"])
    first = next(p for p in sheet if not p["mirror_of"])
    d = folder / first["folder"]
    assert {"part.aicad", "part.step", "part.stl", "preview.png", "flat.dxf"} <= {f.name for f in d.iterdir()}
    assert (folder / "README.md").read_text().count("CC-BY test") == 1
    with open(folder / "BOM.csv") as fh:
        bom = list(csv.DictReader(fh))
    assert {"part_number", "name", "category", "qty", "used_in", "size_mm", "thickness_mm", "volume_mm3", "status",
            "confidence", "reason"} <= set(bom[0])
    # overrides: category and name, by part number
    r2 = parts.make_package(meshes, tmp_path / "o", name="demo", drawings=False,
                            overrides={screw_row["part_number"]: "custom", first["part_number"]: {"name": "base bracket"}})
    rows2 = {p["part_number"]: p for p in r2["parts"]}
    assert rows2[screw_row["part_number"]]["category"] == "custom" and rows2[screw_row["part_number"]]["confidence"] == 1.0
    assert rows2[first["part_number"]]["name"] == "base bracket"
    with pytest.raises(ValueError):
        parts.make_package(meshes, tmp_path / "x", drawings=False, overrides={"PN-999": "custom"})


def test_package_from_urdf_assembly(tmp_path):
    from cadcore.urdf import import_urdf

    asm = import_urdf(ARM, tmp_path / "arm")["assembly"]
    r = parts.make_package(asm, tmp_path, licence_note="example")
    folder = Path(r["folder"])
    assert folder.name.endswith("_parts") and (folder / "BOM.csv").exists()
    assert r["summary"]["parts"] >= 5
    for p in r["parts"]:
        d = folder / p["folder"]
        assert (d / "part.stl").exists() and (d / "preview.png").exists() and (d / "drawing.pdf").exists(), p
        assert (d / "part.step").exists() or (d / "part_mesh.step").exists()
        assert (d / "part.aicad").exists() == p["status"].startswith("parametric")
    assert sum(p["qty"] for p in r["parts"]) == r["summary"]["bodies"]


def test_server_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    import server.app as appmod
    from cadcore.urdf import import_urdf

    api = TestClient(appmod.app)
    api.post("/api/asm/new")
    assert api.post("/api/parts/package", json={}).status_code == 400  # unsaved assembly
    asm = import_urdf(ARM, tmp_path / "arm")["assembly"]
    assert api.post("/api/asm/open", json={"path": asm}).status_code == 200
    r = api.post("/api/parts/package", json={"out_dir": str(tmp_path / "pkg"), "drawings": False}).json()
    assert r["ok"] and Path(r["folder"]).exists()
    stl = tmp_path / "bracket.stl"
    _bracket(5).export(str(stl))
    r = api.post("/api/parts/rebuild", json={"path": str(stl)}).json()
    assert r["ok"] and r["kind"] == "sheet_metal" and abs(r["report"]["thickness"] - 1.5) < 0.02
    assert Path(r["path"]).exists()
    assert api.get("/api/model").json()["spec"]["variables"]["thickness"] == pytest.approx(1.5, abs=0.02)
    tools = appmod._parts_tools()
    assert set(tools) == {"make_parts_package", "rebuild_part_from_mesh"}
    ball = tmp_path / "ball.stl"
    import trimesh

    trimesh.creation.icosphere(radius=10).export(str(ball))
    r = api.post("/api/parts/rebuild", json={"path": str(ball), "open": False}).json()
    assert not r["ok"] and r["message"].startswith("not convertible")
    assert np.isfinite(r["bodies"])
