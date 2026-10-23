# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mesh/CAD import, conversion to STL, printer profiles and slicing.

The slicing tests need the PrusaSlicer engine and are skipped without it."""
import http.server
import json
import threading
from pathlib import Path

import cadquery as cq
import numpy as np
import pytest
import trimesh

from cadcore import convert, slicer
from cadcore.project import Project

ROOT = Path(__file__).resolve().parent.parent
ENGINE = slicer.find_engine()
needs_engine = pytest.mark.skipif(ENGINE is None, reason="slicer engine (PrusaSlicer) not installed")


def l_bracket(path: Path, rotate_x: float = 0.0) -> Path:
    body = (cq.Workplane("XY").box(60, 40, 5, centered=False).union(cq.Workplane("XY").box(5, 40, 30, centered=False)))
    if rotate_x:
        body = body.rotate((0, 0, 0), (1, 0, 0), rotate_x)
    cq.exporters.export(body, str(path))
    return path


# ---------------- conversion ----------------

def test_convert_formats(tmp_path):
    step = ROOT / "bracket.step"
    r = convert.to_stl(step, tmp_path / "b.stl")
    m = trimesh.load(r["path"])
    assert m.is_watertight and r["size_mm"] == [80, 40, 60]
    trimesh.creation.box((10, 10, 10)).export(tmp_path / "box.obj")
    r = convert.to_stl(tmp_path / "box.obj", tmp_path / "box.stl", units="in", binary=False)
    assert r["size_mm"] == [254, 254, 254] and (tmp_path / "box.stl").read_text().startswith("solid")
    trimesh.creation.icosphere().export(tmp_path / "ball.3mf")
    assert convert.to_stl(tmp_path / "ball.3mf", tmp_path / "ball.stl", units="cm")["size_mm"] == [20, 20, 20]
    many = convert.to_stl_many([str(step), str(tmp_path / "missing.step")], tmp_path / "out")
    assert len(many["converted"]) == 1 and "not found" in many["failed"][0]["error"]
    with pytest.raises(convert.ConvertError, match="unsupported"):
        convert.to_stl(tmp_path / "x.dxf", tmp_path / "x.stl")


def test_assembly_to_stl(tmp_path):
    from cadcore.assembly import AssemblyProject

    a = AssemblyProject()
    assert a.execute("add_component", file=str(ROOT / "bracket.step"))["ok"]
    assert a.execute("add_component", file=str(ROOT / "bracket.step"), position=[200, 0, 0])["ok"]
    a.save(str(tmp_path / "two.aicadasm"))
    r = convert.to_stl(tmp_path / "two.aicadasm", tmp_path / "two.stl")
    assert r["size_mm"][0] == pytest.approx(280, abs=0.01)  # both copies, in place


def test_lay_flat_uses_real_faces():
    # L-bracket on its side: the base, not the convex hull's diagonal, must end up on the bed.
    m = trimesh.Trimesh(*convert.shape_triangles(cq.Workplane("XY").box(60, 40, 5, centered=False)
                                                 .union(cq.Workplane("XY").box(5, 40, 30, centered=False))
                                                 .rotate((0, 0, 0), (1, 0, 0), 90).val()))
    flat = convert.placed(m, convert.lay_flat(m), 0, 0)
    assert sorted(np.round(flat.extents, 3)) == [30, 40, 60] and round(flat.extents[2], 3) == 30
    cyl = trimesh.creation.cylinder(5, 40)
    cyl.apply_transform(trimesh.transformations.rotation_matrix(1.2, [1, 0, 0]))
    assert round(convert.placed(cyl, convert.lay_flat(cyl), 0, 0).extents[2], 2) == 40  # stands on its end
    sphere = trimesh.creation.icosphere()
    assert convert.lay_flat(sphere).shape == (3, 3)


def test_placed_mirror_keeps_outward_normals():
    m = trimesh.creation.box((10, 20, 30))
    p = convert.placed(m, np.diag([-1.0, 1, 1]), 100, 50)
    assert p.volume > 0 and np.allclose(p.bounds, [[95, 40, 0], [105, 60, 30]])


# ---------------- importing into a part ----------------

def test_import_stl_into_part(tmp_path):
    stl = l_bracket(tmp_path / "l.stl")
    p = Project()
    r = p.execute("import_file", file=str(stl))
    assert r.ok, r.message
    assert p.spec.name == "l" and p.spec.features[0].type == "import_body"
    assert p.result.volume_mm3 == pytest.approx(60 * 40 * 5 + 5 * 40 * 25, rel=1e-6)
    r = p.execute("add_feature", id="hole", type="extrude", params={"plane": "XY", "distance": 10, "op": "cut",
                                                                    "shapes": [{"type": "circle", "d": 6, "x": 30, "y": 20}]})
    assert r.ok, r.message
    assert p.result.volume_mm3 == pytest.approx(17000 - np.pi * 9 * 5, rel=1e-3)
    assert p.execute("update_feature", id="l", params={"units": "cm"}).ok
    assert p.result.bbox_mm[0] == pytest.approx(600)
    assert not p.execute("import_file", file=str(tmp_path / "nope.stl")).ok


def test_import_multibody_and_open_mesh(tmp_path):
    two = trimesh.util.concatenate([trimesh.creation.box((10, 10, 10)),
                                    trimesh.creation.box((10, 10, 10)).apply_translation((30, 0, 0))])
    two.export(tmp_path / "two.stl")
    p = Project()
    assert p.execute("import_file", file=str(tmp_path / "two.stl")).ok
    assert p.result.volume_mm3 == pytest.approx(2000, rel=1e-6)  # a kit of parts stays several bodies
    sheet = trimesh.Trimesh([[0, 0, 0], [10, 0, 0], [0, 10, 0]], [[0, 1, 2]])
    sheet.export(tmp_path / "sheet.stl")
    r = Project().execute("import_file", file=str(tmp_path / "sheet.stl"))
    assert not r.ok and "no closed volume" in r.message


def test_assembly_inserts_stl(tmp_path):
    from cadcore.assembly import AssemblyProject

    a = AssemblyProject()
    r = a.execute("add_component", file=str(l_bracket(tmp_path / "l.stl")))
    assert r["ok"], r["message"]


# ---------------- profiles ----------------

def test_condition_language():
    cfg = {"printer_notes": "x\\nPRINTER_VENDOR_PRUSA3D\\nPRINTER_MODEL_MK3\\n", "nozzle_diameter": "0.4,0.4", "printer_model": "MK3S",
           "single_extruder_multi_material": "0"}
    ev = slicer.evaluate
    assert ev('printer_notes=~/.*PRINTER_MODEL_MK3.*/ and nozzle_diameter[0]==0.4 and printer_model!="MK3.5MMU3"', cfg)
    assert not ev('printer_notes!~/.*PRUSA3D.*/ or nozzle_diameter[0]>0.4', cfg)
    assert ev('num_extruders>1 and !single_extruder_multi_material', cfg)
    assert ev('(printer_model=="X" or printer_model=="MK3S") and (nozzle_diameter[0]=="0.4")', cfg)
    assert not slicer.compatible({"compatible_printers_condition": "unknown_key==1"}, "p", cfg)
    assert slicer.compatible({"compatible_printers": '"A";"p"'}, "p", cfg)
    assert slicer.compatible({"compatible_printers_condition": '"printer_model==\\"MK3S\\""'.replace('\\"', '"')}, "p", cfg)


@needs_engine
def test_profiles_resolve():
    P = slicer.profiles(ENGINE)
    printers = P.printers()
    assert len(printers) > 500 and len({p["vendor"] for p in printers}) > 20
    name = "Original Prusa i3 MK3S & MK3S+"
    prints = [p["name"] for p in P.prints_for(name)]
    assert "0.20mm QUALITY @MK3" in prints and "0.30mm DRAFT @MK3" in prints
    d = P.defaults(name)
    assert d == {"print": "0.20mm QUALITY @MK3", "filament": "Generic PLA"}
    cfg = P.config(name, d["print"], d["filament"], {"fill_density": "37%", "perimeters": 4})
    assert cfg["fill_density"] == "37%" and cfg["perimeters"] == "4" and "inherits" not in cfg
    assert cfg["printer_settings_id"] == name
    with pytest.raises(slicer.SlicerError):
        P.config(name, d["print"], d["filament"], {"bad key": 1})


@needs_engine
def test_slice_and_toolpaths(tmp_path):
    P = slicer.profiles(ENGINE)
    name = "Creality Ender-3 V2 (0.4 mm nozzle)"
    d = P.defaults(name)
    m = trimesh.creation.box((20, 20, 10))
    m.apply_translation((110, 110, 5))
    m.export(tmp_path / "cube.stl")
    seen = []
    r = slicer.slice_models(ENGINE, P.config(name, d["print"], d["filament"], {"fill_density": "15%"}), [tmp_path / "cube.stl"],
                            tmp_path / "cube.gcode", lambda p, s: seen.append(p))
    s = r["stats"]
    assert s["layers"] == 50 and s["time_s"] > 60 and s["filament_mm"] > 100
    tp = slicer.toolpaths(tmp_path / "cube.gcode")
    assert len(tp["layer_z"]) == 50 and tp["layer_z"][-1] == pytest.approx(10)
    ext = tp["positions"][tp["types"] == slicer.TYPES.index("External perimeter")]
    assert ext[:, [0, 3]].min() == pytest.approx(100.2, abs=0.3) and ext[:, [0, 3]].max() == pytest.approx(119.8, abs=0.3)
    assert seen and max(seen) <= 100


@needs_engine
def test_custom_printer(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "config.json"))
    slicer.save_custom_printer({"name": "Desk delta", "width": 180, "depth": 180, "height": 200, "nozzle": 0.4, "origin_center": True,
                                "heated_bed": False})
    P = slicer.profiles(ENGINE)
    assert slicer.bed_of(P.printer("Desk delta")[1])["bed"]["min"] == [-90, -90]
    d = P.defaults("Desk delta")
    assert d["print"] and d["filament"]
    cfg = P.config("Desk delta", d["print"], d["filament"])
    assert cfg["bed_temperature"] == "0"
    trimesh.creation.box((10, 10, 4)).apply_translation((0, 0, 2)).export(tmp_path / "c.stl")
    r = slicer.slice_models(ENGINE, cfg, [tmp_path / "c.stl"], tmp_path / "c.gcode")
    assert r["stats"]["layers"] > 10 and not r["warnings"], r["warnings"]
    assert "G28" in (tmp_path / "c.gcode").read_text()[:20000]


def test_upload_octoprint(tmp_path):
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got["path"], got["key"] = self.path, self.headers.get("X-Api-Key")
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    g = tmp_path / "x.gcode"
    g.write_text("G28\nG1 X10 E1\n")
    msg = slicer.upload({"type": "octoprint", "url": f"http://127.0.0.1:{srv.server_port}", "api_key": "k1"}, g, "x.gcode", start=True)
    assert "started the print" in msg and got["path"] == "/api/files/local" and got["key"] == "k1"
    assert b"G1 X10 E1" in got["body"] and b'name="print"\r\n\r\ntrue' in got["body"]
    with pytest.raises(slicer.SlicerError, match="http"):
        slicer.upload({"type": "octoprint", "url": "file:///etc"}, g, "x.gcode")


# ---------------- HTTP API ----------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "cfg" / "config.json"))
    monkeypatch.setenv("AI_CAD_CACHE", str(tmp_path / "cache"))
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app)


def test_api_open_any_file_and_convert(client, tmp_path):
    stl = l_bracket(tmp_path / "side.stl", rotate_x=90)
    r = client.post("/api/project/open", json={"path": str(stl)})
    assert r.status_code == 200, r.text
    assert r.json()["model"]["spec"]["features"][0]["type"] == "import_body"
    assert "imported side.stl" in r.json()["result"]["message"]
    r = client.post("/api/convert/stl", json={"sources": [str(ROOT / "bracket.step")], "out": str(tmp_path / "x.stl")})
    assert r.status_code == 200 and Path(r.json()["path"]).exists()
    import base64

    r = client.post("/api/project/import_upload", json={"filename": "up.stl", "data": base64.b64encode(stl.read_bytes()).decode()})
    assert r.status_code == 200 and r.json()["model"]["volume_mm3"] == pytest.approx(17000, rel=1e-6)


@needs_engine
def test_api_print_flow(client, tmp_path):
    l_bracket(tmp_path / "side.stl", rotate_x=90)
    assert client.get("/api/print/engine").json()["found"]
    name = "Creality Ender-3 V2 (0.4 mm nozzle)"
    pre = client.get("/api/print/presets", params={"printer": name}).json()
    assert pre["bed"]["max"] == [215, 220] and pre["bed"]["height"] == 250
    o = client.post("/api/print/objects", json={"path": str(tmp_path / "side.stl")}).json()
    flat = client.post(f"/api/print/objects/{o['id']}/layflat", json={"matrix": [1, 0, 0, 0, 1, 0, 0, 0, 1]}).json()["matrix"]
    body = {"printer": name, "print": pre["defaults"]["print"], "filament": pre["defaults"]["filament"], "overrides": {"brim_width": 4},
            "instances": [{"object": o["id"], "matrix": flat, "x": 110, "y": 110}]}
    j = client.post("/api/print/slice", json=body).json()["job"]
    from server.printing import wait

    res = wait(j)
    assert res["status"] == "done", res["message"]
    assert res["stats"]["layers"] == 150  # lying flat: 30 mm at 0.2 mm
    tp = client.get(f"/api/print/jobs/{j}/toolpaths")
    n, nl = np.frombuffer(tp.content[:8], "<u4")
    assert nl == 150 and n > 1000 and "Skirt/Brim" in json.loads(tp.headers["X-Types"])
    saved = client.post(f"/api/print/jobs/{j}/save", json={"path": str(tmp_path / "out")}).json()
    assert saved["path"].endswith("out.gcode") and Path(saved["path"]).stat().st_size > 10000
    body["instances"][0]["x"] = 5  # half off the bed
    r = client.post("/api/print/slice", json=body)
    assert r.status_code == 400 and "outside the bed" in r.json()["detail"]
    # The one-call version used by the assistant and MCP: partial names, open part.
    client.post("/api/project/open", json={"path": str(tmp_path / "side.stl")})
    r = client.post("/api/print/slice_file", json={"printer": "ender-3 v2 (0.4", "quality": "0.28", "material": "PETG"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert not d["warnings"], d["warnings"]
    assert "SUPERDRAFT" in d["quality"] and "PETG" in d["material"] and d["stats"]["layers"] == 107
    hits = client.get("/api/print/find", params={"query": "prusa mk4s"}).json()["printers"]
    assert hits and all("MK4S" in h["name"] for h in hits)
