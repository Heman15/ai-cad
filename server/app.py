# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local HTTP API for the CAD workspace.

Run: .venv/bin/python -m uvicorn server.app:app --port 8000
All model operations are serialised by one lock; FastAPI runs these sync handlers
in a worker thread pool, so CAD work never blocks the UI process.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cadcore import COMMANDS, PartSpec, Project
from cadcore.assistant import Assistant
from cadcore.builder import FEATURE_TYPES
from cadcore.deps import feature_dependencies
from cadcore.materials import MATERIALS
from cadcore.annotations import evaluate_annotations
from cadcore.mesh import describe, measure, resolve

from . import config

log = logging.getLogger("ai_cad")
app = FastAPI(title="AI CAD")
LOCK = threading.RLock()


def _exit_with_parent() -> None:
    """When launched by the desktop shell, exit if that process disappears (crash, kill), never linger."""
    import os
    import time

    parent = int(os.environ["AI_CAD_PARENT_PID"])
    while True:
        time.sleep(1.0)
        try:
            os.kill(parent, 0)
        except ProcessLookupError:
            os._exit(0)


if __import__("os").environ.get("AI_CAD_PARENT_PID"):
    threading.Thread(target=_exit_with_parent, daemon=True).start()


class State:
    project = Project()
    path: str | None = None  # file the project was opened from / saved to
    assistant: Assistant | None = None
    ui_selection: list[dict[str, Any]] = []
    cc_session: str | None = None  # Claude Code session resumed across chat messages


S = State()


def _client():
    c = config.load()
    key = config.api_key()
    if c["provider"] == "openai":
        import openai

        if not c.get("base_url"):
            raise PermissionError("no API base URL configured")
        local = any(h in c["base_url"] for h in ("localhost", "127.0.0.1"))
        if not key and not local:
            raise PermissionError("no API key configured")
        return openai.OpenAI(api_key=key or "local", base_url=c["base_url"])
    if not key:
        raise PermissionError("no API key configured")
    return anthropic.Anthropic(api_key=key, base_url=c.get("base_url") or None)


def assistant() -> Assistant:
    c = config.load()
    if S.assistant is None:
        S.assistant = Assistant(S.project, client_factory=_client)
        S.assistant.lock = LOCK
    S.assistant.project = S.project
    if S.assistant.provider != c["provider"]:
        S.assistant.reset()  # message formats differ between providers
    S.assistant.provider = "openai" if c["provider"] == "openai" else "anthropic"
    S.assistant.model, S.assistant.effort = c["model"], c["effort"]
    return S.assistant


def snapshot() -> dict[str, Any]:
    with LOCK:
        p = S.project
        out: dict[str, Any] = {
            "spec": p.spec.to_dict(),
            "parameters": p.spec.parameters(),
            "dependencies": feature_dependencies(p.spec),
            "can_undo": p.can_undo,
            "can_redo": p.can_redo,
            "path": S.path,
            "entities": None, "volume_mm3": None, "bbox_mm": None, "mass_kg": None, "evaluated": {},
            "annotations": [],
        }
        if p.result:
            out["entities"] = describe(p.result)
            out["volume_mm3"] = p.result.volume_mm3
            out["bbox_mm"] = p.result.bbox_mm
            out["evaluated"] = p.result.variables
            out["annotations"] = evaluate_annotations(p.spec, p.result)
            if p.result.sheet is not None:
                from cadcore.sheetmetal import flat_pattern
                try:
                    fp = flat_pattern(p.result.sheet)
                    out["sheet"] = {"flat_ok": True, "flat_size": fp["size"], "bends": len(fp["bends"])}
                except ValueError as e:
                    out["sheet"] = {"flat_ok": False, "message": str(e)}
            out["threads"] = [{"feature": t.feature, "label": t.label, "internal": t.internal, "modeled": t.modeled,
                               "length": t.length} for t in p.result.threads]
            if p.spec.material:
                out["mass_kg"] = p.result.volume_mm3 * 1e-9 * p.spec.material.density
        return out


def _replace_project(project: Project, path: str | None) -> None:
    S.project, S.path = project, path
    if S.assistant:
        S.assistant.reset()


# ---------------- model ----------------

class CommandIn(BaseModel):
    command: str
    args: dict[str, Any] = {}


class BatchIn(BaseModel):
    commands: list[dict[str, Any]]
    label: str = "batch"


class RefsIn(BaseModel):
    refs: list[dict[str, Any]]


class PathIn(BaseModel):
    path: str


@app.get("/api/version")
def version():
    """Content hashes of the open part and assembly; the UI polls this to pick up external changes."""
    import hashlib

    with LOCK:
        part = json.dumps([S.project.spec.to_dict(), S.path], sort_keys=True, default=str)
        asm = json.dumps([A.project.spec.to_dict(), A.project.path], sort_keys=True, default=str) if A.project else ""
    return {"part": hashlib.sha1(part.encode()).hexdigest(), "asm": hashlib.sha1(asm.encode()).hexdigest()}


class SelectionIn(BaseModel):
    selection: list[dict[str, Any]] = []


@app.post("/api/ui/selection")
def ui_selection_set(body: SelectionIn):
    S.ui_selection = body.selection
    return {"ok": True}


@app.get("/api/ui/selection")
def ui_selection_get():
    with LOCK:
        if not S.ui_selection or not S.project.result:
            return {"selection": []}
        return {"selection": [resolve(S.project.result, r) for r in S.ui_selection]}


@app.on_event("startup")
def _announce():
    """Write host/port so local tools (the MCP server for Claude Code) can find this running app."""
    import os

    port = os.environ.get("AI_CAD_PORT")
    if port:
        info = config.config_path().parent / "backend.json"
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(json.dumps({"url": f"http://127.0.0.1:{port}", "pid": os.getpid()}))


@app.get("/api/info")
def info():
    return {"commands": COMMANDS, "materials": MATERIALS, "feature_types": FEATURE_TYPES, "ai": config.public()}


@app.get("/api/model")
def get_model():
    return snapshot()


@app.post("/api/command")
def command(body: CommandIn):
    if body.command in ("export_step", "export_stl"):
        raise HTTPException(400, "use /api/export")
    with LOCK:
        r = S.project.execute(body.command, **body.args)
    return {"result": r.to_dict(), "model": snapshot()}


@app.post("/api/batch")
def batch(body: BatchIn):
    with LOCK:
        r = S.project.execute_batch(body.commands, body.label)
    return {"result": r.to_dict(), "model": snapshot()}


@app.post("/api/preview")
def preview(body: BatchIn):
    with LOCK:
        return S.project.preview(body.commands)


@app.post("/api/undo")
def undo():
    with LOCK:
        r = S.project.undo()
    return {"result": r.to_dict(), "model": snapshot()}


@app.post("/api/redo")
def redo():
    with LOCK:
        r = S.project.redo()
    return {"result": r.to_dict(), "model": snapshot()}


@app.post("/api/new")
def new():
    with LOCK:
        _replace_project(Project(), None)
    return {"model": snapshot()}


@app.post("/api/resolve")
def resolve_refs(body: RefsIn):
    with LOCK:
        if not S.project.result:
            return {"items": [{"status": "missing", "reason": "the model is empty"} for _ in body.refs]}
        return {"items": [resolve(S.project.result, r) for r in body.refs]}


@app.post("/api/measure")
def measure_refs(body: RefsIn):
    with LOCK:
        if not S.project.result:
            raise HTTPException(400, "the model is empty")
        return measure(S.project.result, body.refs)


class SketchIn(BaseModel):
    sketch: dict[str, Any]


@app.post("/api/sketch/solve")
def sketch_solve(body: SketchIn):
    """Solve a sketch for the editor (uses the part's variables for formulas); does not change the model."""
    from cadcore.builder import BuildError, _eval_variables
    from cadcore.expr import ExprError, evaluate
    from cadcore.sketch import SketchError, curve_samples, profile_faces, solve

    with LOCK:
        try:
            names = _eval_variables(S.project.spec.variables)
        except BuildError as e:
            return {"ok": False, "message": str(e)}

    def num(v, what):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            return float(str(v).strip().lstrip("="))
        except ValueError:
            try:
                return evaluate(str(v).strip().lstrip("="), names)
            except ExprError as e:
                raise SketchError(f"{what}: {e}")

    try:
        sol = solve(body.sketch, num)
    except SketchError as e:
        return {"ok": False, "message": str(e)}
    out = {"ok": True, **sol.to_dict(), "curves": curve_samples(body.sketch, sol)}
    if sol.converged:
        try:
            faces = profile_faces(body.sketch, sol)
            out["profile"] = {"faces": len(faces), "area_mm2": sum(f.Area() for f in faces)}
        except SketchError as e:
            out["profile"] = {"error": str(e)}
        except Exception:  # degenerate geometry while editing
            out["profile"] = {"error": "profile could not be built (degenerate geometry)"}
    return out


@app.post("/api/section")
def section(body: dict[str, Any]):
    """The sketch plane's frame, plus the current solid's edges projected onto it (for sketching on a face)."""
    import cadquery as cq

    from cadcore.builder import BuildError, resolve_plane

    with LOCK:
        shape = S.project.result.shape.val() if S.project.result else None
        try:
            pl = resolve_plane(S.project.spec, shape, body.get("plane", "XY"))
        except BuildError as e:
            return {"ok": False, "message": str(e), "polylines": []}
        lines = []
        if S.project.result:
            for e in describe(S.project.result)["edges"]:
                pts = e["polyline"]
                lines.append([list(pl.to_uv(cq.Vector(*pts[i:i + 3]))) for i in range(0, len(pts), 3)])
        return {"ok": True, "plane": pl.to_dict(), "polylines": lines}


# ---------------- files ----------------

@app.get("/api/project")
def download_project():
    return S.project.spec.to_dict()


@app.post("/api/project")
def upload_project(spec: dict[str, Any]):
    try:
        project = Project(PartSpec.from_dict(spec))
    except Exception as e:  # malformed or unbuildable file
        raise HTTPException(400, f"could not open project: {e}")
    with LOCK:
        _replace_project(project, None)
    return {"model": snapshot()}


@app.post("/api/project/save")
def save_to(body: PathIn):
    path = Path(body.path).expanduser()
    if path.suffix.lower() not in (".json", ".aicad"):
        path = path.with_suffix(".aicad")
    with LOCK:
        S.project.save(path)
        S.path = str(path)
    return {"ok": True, "path": str(path), "model": snapshot()}


@app.post("/api/project/open")
def open_from(body: PathIn):
    path = Path(body.path).expanduser()
    try:
        project = Project.load(path)
    except FileNotFoundError:
        raise HTTPException(404, f"file not found: {path}")
    except Exception as e:
        raise HTTPException(400, f"could not open {path.name}: {e}")
    with LOCK:
        _replace_project(project, str(path))
    return {"model": snapshot()}


@app.get("/api/export/{fmt}")
def export_download(fmt: str):
    if fmt not in ("step", "stl"):
        raise HTTPException(404)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f"part.{fmt}"
        with LOCK:
            r = S.project.execute(f"export_{fmt}", path=path)
        if not r.ok:
            raise HTTPException(400, r.message)
        data = path.read_bytes()
    name = (S.project.spec.name or "part").replace(" ", "_")
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


@app.post("/api/export/{fmt}")
def export_to(fmt: str, body: PathIn):
    if fmt not in ("step", "stl"):
        raise HTTPException(404)
    with LOCK:
        r = S.project.execute(f"export_{fmt}", path=Path(body.path).expanduser())
    if not r.ok:
        raise HTTPException(400, r.message)
    return r.to_dict()


# ---------------- 2D drawings ----------------

def _drawing(sheet: str, projection: str) -> dict[str, Any]:
    from cadcore.drawing import SHEETS, make_drawing

    if sheet not in SHEETS or projection not in ("first", "third"):
        raise HTTPException(400, "sheet must be A4/A3/A2 and projection first/third")
    with LOCK:
        if not S.project.result:
            raise HTTPException(400, "the model is empty")
        return make_drawing(S.project.result, S.project.spec, sheet, projection)


@app.get("/api/drawing.svg")
def drawing_svg(sheet: str = "A3", projection: str = "first"):
    d = _drawing(sheet, projection)
    return Response(d["svg"], media_type="image/svg+xml", headers={"X-Drawing-Scale": d["scale"], "X-Drawing-Warnings": "; ".join(d["warnings"])})


@app.get("/api/drawing.pdf")
def drawing_pdf(sheet: str = "A3", projection: str = "first"):
    from cadcore.drawing import to_pdf

    name = (S.project.spec.name or "part").replace(" ", "_")
    return Response(to_pdf(_drawing(sheet, projection)["svg"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}_drawing.pdf"'})


class DrawingExportIn(BaseModel):
    path: str
    sheet: str = "A3"
    projection: str = "first"


@app.post("/api/drawing/export")
def drawing_export(body: DrawingExportIn):
    from cadcore.drawing import to_pdf

    d = _drawing(body.sheet, body.projection)
    path = Path(body.path).expanduser()
    path.write_bytes(to_pdf(d["svg"]) if path.suffix.lower() == ".pdf" else d["svg"].encode())
    return {"ok": True, "message": f"drawing saved to {path}", "scale": d["scale"]}


# ---------------- analysis ----------------

class DfmIn(BaseModel):
    process: str = "cnc_milling"
    overrides: dict[str, float] = {}


@app.post("/api/analysis/dfm")
def analysis_dfm(body: DfmIn):
    from cadcore.dfm import check

    with LOCK:
        result = S.project.result
    if not result:
        raise HTTPException(400, "the model is empty")
    try:
        return check(result, body.process, body.overrides)
    except ValueError as e:
        raise HTTPException(400, str(e))


class FeaIn(BaseModel):
    mesh_size: float | None = None
    check_convergence: bool = False


def material_props(spec) -> dict[str, float]:
    m = spec.material
    if m is None or m.E is None or m.nu is None:
        raise HTTPException(400, "assign a material with Young's modulus and Poisson's ratio first (Material & mass)")
    return {"name": m.name, "E": m.E, "nu": m.nu, **({"yield": m.yield_strength} if m.yield_strength else {})}


@app.post("/api/analysis/fea")
def analysis_fea(body: FeaIn):
    from cadcore.fea import FEAError, analyse

    with LOCK:  # copy what we need, then solve without blocking other requests
        result, spec = S.project.result, S.project.spec.clone()
    if not result:
        raise HTTPException(400, "the model is empty")
    setup = spec.analysis or {}
    try:
        return analyse(result.shape.val(), material_props(spec), setup.get("supports", []), setup.get("loads", []),
                       body.mesh_size, body.check_convergence)
    except FEAError as e:
        raise HTTPException(400, str(e))


# ---------------- sheet metal ----------------

def _flat():
    from cadcore.sheetmetal import flat_pattern

    with LOCK:
        result = S.project.result
        name = S.project.spec.name
    if not result or result.sheet is None:
        raise HTTPException(400, "not a sheet-metal part")
    try:
        return flat_pattern(result.sheet), name
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/flat.svg")
def flat_svg_route():
    from cadcore.sheetmetal import flat_svg

    fp, name = _flat()
    return Response(flat_svg(fp, name), media_type="image/svg+xml",
                    headers={"X-Flat-Size": f"{fp['size'][0]:.2f} x {fp['size'][1]:.2f}"})


@app.get("/api/flat.dxf")
def flat_dxf_route():
    from cadcore.sheetmetal import flat_dxf

    fp, name = _flat()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "flat.dxf"
        flat_dxf(fp, str(path))
        data = path.read_bytes()
    return Response(data, media_type="application/dxf",
                    headers={"Content-Disposition": f'attachment; filename="{(name or "part").replace(" ", "_")}_flat.dxf"'})


@app.post("/api/flat/export")
def flat_export(body: PathIn):
    from cadcore.sheetmetal import flat_dxf, flat_svg

    fp, name = _flat()
    path = Path(body.path).expanduser()
    if path.suffix.lower() == ".svg":
        path.write_text(flat_svg(fp, name))
    else:
        flat_dxf(fp, str(path))
    return {"ok": True, "message": f"flat pattern saved to {path}"}


# ---------------- assemblies ----------------

class AsmState:
    project = None  # cadcore.assembly.AssemblyProject


A = AsmState()
_ASM_MESH_CACHE: dict[str, Any] = {}
BIG_SHAPE_FACES = 1500


def _asm():
    from cadcore.assembly import AssemblyProject

    if A.project is None:
        A.project = AssemblyProject()
    return A.project


def _local_entities(path: str, shape) -> dict[str, Any]:
    """Tessellated faces/edges of a part in its own coordinates (cached per file version)."""
    import cadquery as cq

    from cadcore.mesh import _edge_info, _face_info, _point_on

    import hashlib

    key = f"{path}:{hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ''}"
    if key in _ASM_MESH_CACHE:
        return _ASM_MESH_CACHE[key]
    all_faces = shape.Faces()
    if len(all_faces) > BIG_SHAPE_FACES:  # e.g. an organic mesh: one surface, no per-face picking or edges
        verts, tris = shape.tessellate(0.05, 0.2)
        bb = shape.BoundingBox()
        out = {"faces": [{"index": 0, "type": "MESH", "point": list(bb.center.toTuple()), "area": round(shape.Area(), 3),
                          "center": list(bb.center.toTuple()),
                          "vertices": [c for v in verts for c in v.toTuple()], "triangles": [k for t in tris for k in t]}],
               "edges": []}
        _ASM_MESH_CACHE[key] = out
        return out
    faces = []
    for i, f in enumerate(all_faces):
        verts, tris = f.tessellate(0.05, 0.2)
        info = _face_info(f, _point_on(f, verts, tris))
        faces.append({"index": i, **info, "vertices": [c for v in verts for c in v.toTuple()], "triangles": [k for t in tris for k in t]})
    edges = []
    for e in shape.Edges():
        pts = [e.startPoint(), e.endPoint()] if e.geomType() == "LINE" else [e.positionAt(k / 24) for k in range(25)]
        edges.append({**_edge_info(e), "polyline": [c for q in pts for c in q.toTuple()]})
    out = {"faces": faces, "edges": edges}
    _ASM_MESH_CACHE[key] = out
    return out


def asm_snapshot() -> dict[str, Any]:
    import numpy as np

    from cadcore.assembly import CACHE, resolve_path, rotation_matrix

    a = _asm()
    comps, faces, edges = [], [], []
    for c in a.spec.components:
        path = resolve_path(a.path, c.file)
        entry = {"id": c.id, "file": c.file, "fixed": c.fixed, "position": c.position, "rotation": c.rotation,
                 "link": c.link, "color": c.color}
        try:
            info = CACHE.load(path)
        except Exception as e:  # missing or broken part file: report, keep the rest working
            comps.append({**entry, "error": str(e)})
            continue
        entry.update(name=info["name"], material=info["material"], mass_kg=info["mass_kg"])
        comps.append(entry)
        R, t = rotation_matrix(c.rotation), np.asarray(c.position, dtype=float)
        tf = lambda xs: (np.asarray(xs).reshape(-1, 3) @ R.T + t).ravel().round(5).tolist()  # noqa: E731
        local = _local_entities(str(path), info["shape"])
        for f in local["faces"]:
            faces.append({**f, "index": len(faces), "feature": c.id, "component": c.id, "color": c.color,
                          "local_point": f["point"], "local_normal": f.get("normal"),
                          "point": tf(f["point"]), "normal": (np.asarray(f["normal"]) @ R.T).round(6).tolist() if f.get("normal") else None,
                          "vertices": tf(f["vertices"])})
        for e in local["edges"]:
            edges.append({**e, "index": len(edges), "feature": c.id, "component": c.id, "point": tf(e["point"]), "polyline": tf(e["polyline"])})
    try:
        bom = a.bom()
    except Exception:
        bom = []
    return {"spec": a.spec.to_dict(), "path": a.path, "status": a.status, "can_undo": bool(a._undo), "can_redo": bool(a._redo),
            "components": comps, "entities": {"faces": faces, "edges": edges} if faces else None, "bom": bom}


@app.get("/api/asm")
def asm_get():
    with LOCK:
        return asm_snapshot()


@app.post("/api/asm/command")
def asm_command(body: CommandIn):
    with LOCK:
        r = _asm().execute(body.command, **body.args)
        return {"result": r, "model": asm_snapshot()}


@app.post("/api/asm/undo")
def asm_undo():
    with LOCK:
        return {"result": _asm().undo(), "model": asm_snapshot()}


@app.post("/api/asm/redo")
def asm_redo():
    with LOCK:
        return {"result": _asm().redo(), "model": asm_snapshot()}


@app.post("/api/asm/new")
def asm_new():
    from cadcore.assembly import AssemblyProject

    with LOCK:
        A.project = AssemblyProject()
        return {"model": asm_snapshot()}


@app.post("/api/asm/open")
def asm_open(body: PathIn):
    from cadcore.assembly import AssemblyProject

    try:
        proj = AssemblyProject.load(str(Path(body.path).expanduser()))
    except FileNotFoundError:
        raise HTTPException(404, f"file not found: {body.path}")
    except Exception as e:
        raise HTTPException(400, f"could not open assembly: {e}")
    with LOCK:
        A.project = proj
        return {"model": asm_snapshot()}


class UrdfIn(BaseModel):
    path: str
    out_dir: str | None = None
    geometry: str = "visual"


@app.post("/api/asm/import_urdf")
def asm_import_urdf(body: UrdfIn):
    """Convert a URDF/xacro into part files plus an assembly (written next to the URDF unless out_dir is given), then open it."""
    from cadcore.assembly import AssemblyError, AssemblyProject
    from cadcore.urdf import import_urdf

    if body.geometry not in ("visual", "collision"):
        raise HTTPException(400, "geometry must be 'visual' or 'collision'")
    try:
        r = import_urdf(body.path, body.out_dir, body.geometry)
        proj = AssemblyProject.load(r["assembly"])
    except AssemblyError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(400, f"could not write the converted files: {e}")
    with LOCK:
        A.project = proj
        return {"model": asm_snapshot(), "report": r["report"], "path": r["assembly"]}


class JointsIn(BaseModel):
    values: dict[str, float]


@app.post("/api/asm/joints")
def asm_joints(body: JointsIn):
    """Move robot joints (degrees / mm). No undo step; returns only the new component poses, for smooth dragging."""
    with LOCK:
        a = _asm()
        r = a.move_joints(body.values)
        return {"result": r, "joints": a.spec.robot["joints"] if a.spec.robot else [],
                "poses": {c.id: {"position": c.position, "rotation": c.rotation} for c in a.spec.components}}


@app.post("/api/asm/save")
def asm_save(body: PathIn):
    path = Path(body.path).expanduser()
    if path.suffix.lower() != ".aicadasm":
        path = path.with_suffix(".aicadasm")
    with LOCK:
        _asm().save(str(path))
        return {"ok": True, "path": str(path), "model": asm_snapshot()}


@app.post("/api/asm/interference")
def asm_interference():
    with LOCK:
        return {"items": _asm().interference()}


@app.get("/api/asm/export.step")
def asm_export_download():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "assembly.step"
        with LOCK:
            _asm().export_step(str(path))
        data = path.read_bytes()
    return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="assembly.step"'})


@app.post("/api/asm/export")
def asm_export(body: PathIn):
    with LOCK:
        _asm().export_step(str(Path(body.path).expanduser()))
    return {"ok": True, "message": f"assembly exported to {body.path}"}


# ---------------- AI ----------------

class SettingsIn(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    effort: str | None = None


@app.get("/api/settings")
def get_settings():
    return config.public()


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    if body.provider and body.provider not in ("anthropic", "openai", "claude_code"):
        raise HTTPException(400, "provider must be anthropic, openai (OpenAI-compatible) or claude_code")
    if body.effort and body.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "effort must be low, medium, high, xhigh or max")
    before = config.load()["provider"]
    config.save(body.model_dump())
    if config.load()["provider"] != before:
        S.cc_session = None
    return config.public()


class ChatIn(BaseModel):
    text: str
    selection: list[dict[str, Any]] = []
    interpret_drawing: bool = False
    attachments: list[str] = []  # ids returned by /api/chat/attachment


def _attach_dir() -> Path:
    import os

    return Path(os.environ.get("AI_CAD_CACHE", Path.home() / ".cache" / "ai-cad")) / "attachments"


class AttachIn(BaseModel):
    filename: str
    media_type: str
    data: str  # base64


@app.post("/api/chat/attachment")
def chat_attachment(body: AttachIn):
    """Store a file the user attached in chat; returns an id usable in /api/chat/stream."""
    import base64
    import hashlib

    raw = base64.b64decode(body.data)
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 30 MB)")
    if body.media_type not in ("application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"):
        raise HTTPException(400, "attach a PDF or an image (PNG, JPEG, WebP, GIF)")
    aid = hashlib.sha256(raw).hexdigest()[:16]
    name = Path(body.filename).name or "file"
    d = _attach_dir() / aid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(raw)
    (d / "meta.json").write_text(json.dumps({"name": name, "media_type": body.media_type}))
    return {"id": aid, "name": name, "media_type": body.media_type}


def _attachment(aid: str) -> tuple[Path, dict[str, Any]]:
    if not aid.isalnum():
        raise HTTPException(400, "bad attachment id")
    d = _attach_dir() / aid
    try:
        meta = json.loads((d / "meta.json").read_text())
    except OSError:
        raise HTTPException(404, "attachment not found; attach the file again")
    return d / meta["name"], meta


def _attachment_blocks(ids: list[str]) -> list[dict[str, Any]]:
    """Attachments as image content blocks for API providers (PDF pages are rendered to images)."""
    import base64

    from cadcore.source_drawing import _render_pages

    blocks: list[dict[str, Any]] = []
    for aid in ids:
        path, meta = _attachment(aid)
        if meta["media_type"] == "application/pdf":
            pages = _render_pages(path.read_bytes(), "application/pdf")
            for i, png in enumerate(pages, 1):
                blocks.append({"type": "text", "text": f"Attached {meta['name']}, page {i}:"})
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(png).decode()}})
        else:
            blocks.append({"type": "text", "text": f"Attached image {meta['name']}:"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": meta["media_type"],
                                                       "data": base64.standard_b64encode(path.read_bytes()).decode()}})
    return blocks


class DrawingIn(BaseModel):
    filename: str
    media_type: str
    data: str  # base64


@app.post("/api/source-drawing")
def upload_source_drawing(body: DrawingIn):
    import base64

    from cadcore import source_drawing

    raw = base64.b64decode(body.data)
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(413, "drawing too large (max 30 MB)")
    try:
        info = source_drawing.store(raw, body.filename, body.media_type)
    except Exception as e:
        raise HTTPException(400, f"could not read the drawing: {e}")
    with LOCK:
        r = S.project.execute("attach_source_drawing", **info)
    return {"result": r.to_dict(), "model": snapshot()}


@app.get("/api/source-drawing/page/{page}")
def source_drawing_page(page: int):
    from cadcore import source_drawing

    d = (S.project.spec.source or {}).get("drawing")
    png = source_drawing.page_png(d["sha256"], page) if d else None
    if png is None:
        raise HTTPException(404, "source drawing page not available (file missing from the local cache)")
    return Response(png, media_type="image/png")


def _drawing_attachments() -> list[dict[str, Any]]:
    import base64

    from cadcore import source_drawing

    d = (S.project.spec.source or {}).get("drawing")
    if not d:
        raise HTTPException(400, "no source drawing attached")
    blocks = []
    for i in range(1, d["pages"] + 1):
        png = source_drawing.page_png(d["sha256"], i)
        if png is None:
            raise HTTPException(404, "source drawing missing from the local cache; import it again")
        blocks.append({"type": "text", "text": f"Drawing page {i} of {d['pages']} ({d['filename']}):"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(png).decode()}})
    return blocks


def _api_error(e: Exception) -> str:
    if isinstance(e, PermissionError):
        return f"AI provider not configured ({e}). Open Settings (⚙) and choose a provider."
    try:
        import openai
        if isinstance(e, openai.AuthenticationError):
            return "The API key was rejected by the provider. Check it in Settings."
        if isinstance(e, openai.APIConnectionError):
            return "Could not reach the AI provider (for Ollama: is `ollama serve` running?)."
        if isinstance(e, openai.NotFoundError):
            return "The provider does not know this model name. Check the model in Settings."
        if isinstance(e, openai.BadRequestError):
            return f"The provider rejected the request; the model may not support tool calling or images ({e.message[:200]})."
        if isinstance(e, openai.RateLimitError):
            return "Rate limited by the AI provider (free tiers have low limits); try again shortly."
        if isinstance(e, openai.APIStatusError):
            return f"AI provider error ({e.status_code})."
    except ImportError:
        pass
    if isinstance(e, anthropic.AuthenticationError):
        return "The API key was rejected. Check it in Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the AI provider; try again shortly."
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the AI provider; check your internet connection."
    if isinstance(e, anthropic.APIStatusError):
        return f"AI provider error ({e.status_code})."
    log.error("chat failed: %s", type(e).__name__)  # never log request bodies or keys
    return f"Assistant error: {type(e).__name__}"


def _sse(ev: dict[str, Any]) -> str:
    return f"data: {json.dumps(ev, default=str)}\n\n"


def _claude_code_events(body: ChatIn, app_url: str):
    """In-app chat backed by the user's own Claude Code login (see server/claude_code.py)."""
    from cadcore.deps import spec_diff

    from . import claude_code

    with LOCK:
        before = S.project.spec.clone()
        sel = [resolve(S.project.result, r) for r in body.selection] if body.selection and S.project.result else []
    parts = []
    if sel:
        parts.append("The user's current selection in the 3D view (\"this\"): " + json.dumps(sel))
    files = [_attachment(aid) for aid in body.attachments]
    for path, meta in files:
        parts.append(f"The user attached {meta['name']} at {path} - read it with the Read tool.")
    if body.interpret_drawing:
        from cadcore.assistant import INTERPRET
        d = (S.project.spec.source or {}).get("drawing")
        parts.append(INTERPRET + (f"\nThe drawing is attached to the part as {d['filename']}; call get_model / import_drawing as needed." if d else ""))
    parts.append(body.text)
    c = config.load()
    workdir = files[0][0].parent if files else _attach_dir()
    try:
        for ev in claude_code.stream("\n\n".join(p for p in parts if p), app_url=app_url, session_id=S.cc_session,
                                     workdir=workdir, model=c.get("model") or None, claude_path=c.get("claude_path")):
            if ev["type"] == "session":
                S.cc_session = ev["id"]
            elif ev["type"] == "tool":
                yield _sse({**ev, **({"model": snapshot()} if ev["mutating"] and ev["ok"] else {})})
            elif ev["type"] == "text":
                yield _sse(ev)
            elif ev["type"] == "result":
                S.cc_session = ev.get("session_id") or S.cc_session
                if ev["is_error"]:
                    yield _sse({"type": "error", "message": f"Claude Code: {ev['text'] or 'request failed'} (is it signed in? run Claude Code once)"})
                else:
                    yield _sse({"type": "done", "reply": "", "diff": spec_diff(before, S.project.spec), "model": snapshot()})
    except (FileNotFoundError, RuntimeError) as e:
        yield _sse({"type": "error", "message": str(e)})


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, request: Request):
    if config.load()["provider"] == "claude_code":
        return StreamingResponse(_claude_code_events(body, str(request.base_url).rstrip("/")), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})
    a = assistant()
    attachments, effort, text = (_attachment_blocks(body.attachments) or None), None, body.text
    if body.interpret_drawing:
        from cadcore.assistant import INTERPRET

        attachments, effort = (attachments or []) + _drawing_attachments(), "high"
        text = INTERPRET + (f"\n\nUser note: {body.text}" if body.text.strip() else "")

    def events():
        try:
            for ev in a.stream(text, body.selection, attachments=attachments, effort=effort):
                if (ev["type"] == "tool" and ev.get("mutating") and ev["ok"]) or ev["type"] == "done":
                    ev = {**ev, "model": snapshot()}  # live viewport update after each change
                yield _sse(ev)
        except Exception as e:
            yield _sse({"type": "error", "message": _api_error(e)})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat/reset")
def chat_reset():
    S.cc_session = None
    if S.assistant:
        S.assistant.reset()
    return {"ok": True}


@app.post("/api/alternatives/{index}/accept")
def accept_alternative(index: int):
    r = assistant().accept_alternative(index)
    return {"result": r, "model": snapshot()}


# ---------------- static UI ----------------

@app.middleware("http")
async def no_stale_ui(request, call_next):
    # Hashed files under /assets can be cached; everything else (index.html) must be revalidated.
    response = await call_next(request)
    if not request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
