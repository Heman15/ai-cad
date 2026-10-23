# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP API of the Print workspace: printers and materials, the build plate, slicing jobs and G-code.

The plate holds meshes (any file the app can read, or the open part); the UI owns their placement (a 3x3
rotation/scale matrix and a bed position per instance) and sends it with every slice, so what is sliced is
exactly what is shown.
"""
from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from cadcore import convert, slicer
from cadcore.slicer import SlicerError

router = APIRouter(prefix="/api/print")
LOCK = threading.RLock()


def _work() -> Path:
    d = Path(os.environ.get("AI_CAD_CACHE", Path.home() / ".cache" / "ai-cad")) / "print"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Plate:
    objects: dict[str, dict[str, Any]] = {}
    jobs: dict[str, dict[str, Any]] = {}


P = Plate()


def _engine() -> slicer.Engine:
    e = slicer.find_engine()
    if not e:
        raise HTTPException(409, slicer.INSTALL_HINT)
    return e


def _profiles() -> slicer.Profiles:
    try:
        return slicer.profiles(_engine())
    except SlicerError as e:
        raise HTTPException(409, str(e))


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


# ---------------- saved choices ----------------

def _state_file() -> Path:
    return slicer.user_dir() / "print.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(update: dict[str, Any]) -> dict[str, Any]:
    s = {**load_state(), **update}
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(s, indent=2))
    os.chmod(f, 0o600)  # may hold printer API keys
    return s


def _public_state() -> dict[str, Any]:
    s = load_state()
    hosts = [{**h, "api_key": "•••" if h.get("api_key") else ""} for h in s.get("hosts", [])]
    return {**s, "hosts": hosts}


# ---------------- engine and profiles ----------------

@router.get("/engine")
def engine():
    return slicer.engine_status()


@router.post("/engine/install")
def engine_install():
    job = _new_job("install")

    def run():
        try:
            job["result"] = slicer.install_engine(lambda s: job.update(message=s))
            slicer._PROFILES.clear()
            job["status"] = "done"
        except (SlicerError, OSError, Exception) as e:  # noqa: BLE001 - reported to the UI
            job.update(status="error", message=str(e))

    threading.Thread(target=run, daemon=True).start()
    return {"job": job["id"]}


@router.get("/printers")
def printers():
    pr = _profiles()
    out = pr.printers()
    for name, c in slicer.custom_printers().items():
        try:
            cfg = slicer.custom_printer_config(c)
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"vendor": "Custom", "vendor_name": "Custom", "name": name, "model": "", "model_name": name, "family": "",
                    "variant": str(c.get("nozzle")), "nozzle": float(c.get("nozzle") or 0.4), "thumbnail": "", "custom": True,
                    **slicer.bed_of(cfg)})
    return {"printers": out, "state": _public_state()}


@router.get("/thumbnail/{vendor}/{name}")
def thumbnail(vendor: str, name: str):
    pr = _profiles()
    v = pr.vendors.get(vendor)
    if not v or not re.fullmatch(r"[\w.+-]+\.(png|svg|stl)", name):
        raise HTTPException(404)
    f = v.folder / name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "max-age=86400"})


@router.get("/presets")
def presets(printer: str, print: str | None = None):
    pr = _profiles()
    try:
        prints = pr.prints_for(printer)
        defaults = pr.defaults(printer)
        quality = print if print and any(p["name"] == print for p in prints) else defaults["print"]
        fils = pr.filaments_for(printer, quality)
        v, pcfg = pr.printer(printer)
    except SlicerError as e:
        raise _bad(e)
    return {"prints": prints, "filaments": fils, "defaults": defaults, **slicer.bed_of(pcfg),
            "nozzle": slicer._num_list(pcfg.get("nozzle_diameter", "0.4"))[0],
            "bed_model": _bed_asset(v, pcfg), "printer_model": pcfg.get("printer_model", "")}


def _bed_asset(v: slicer.Vendor, pcfg: dict[str, str]) -> dict[str, str] | None:
    m = v.models.get(pcfg.get("printer_model", ""))
    if not m:
        return None
    return {k: f"/api/print/thumbnail/{v.id}/{m[k]}" for k in ("bed_model", "bed_texture", "thumbnail") if m.get(k) and (v.folder / m[k]).exists()}


class ConfigIn(BaseModel):
    printer: str
    print: str
    filament: str
    overrides: dict[str, Any] = {}


@router.post("/config")
def config(body: ConfigIn):
    """Every resolved setting with its help text, for the Custom settings list."""
    pr = _profiles()
    try:
        base = pr.config(body.printer, body.print, body.filament)
    except SlicerError as e:
        raise _bad(e)
    help_ = slicer.option_help(_engine())
    keys = sorted(k for k in base if k in help_)
    return {"settings": [{"key": k, "value": base[k], "override": body.overrides.get(k), **help_[k]} for k in keys]}


class StateIn(BaseModel):
    printer: str | None = None
    print: str | None = None
    filament: str | None = None
    overrides: dict[str, Any] | None = None


@router.post("/state")
def set_state(body: StateIn):
    save_state({k: v for k, v in body.model_dump().items() if v is not None})
    return _public_state()


class CustomPrinterIn(BaseModel):
    name: str
    width: float
    depth: float
    height: float
    nozzle: float = 0.4
    filament_diameter: float = 1.75
    flavor: str = "marlin2"
    heated_bed: bool = True
    origin_center: bool = False
    retract_length: float = 5
    start_gcode: str = ""
    end_gcode: str = ""


@router.post("/custom-printer")
def custom_printer(body: CustomPrinterIn):
    if body.flavor not in ("marlin2", "marlin", "klipper", "reprap", "repetier", "teacup", "makerware", "sailfish", "smoothie", "mach3", "machinekit"):
        raise HTTPException(400, "unknown G-code flavor")
    try:
        return slicer.save_custom_printer(body.model_dump())
    except SlicerError as e:
        raise _bad(e)


@router.delete("/custom-printer/{name}")
def custom_printer_delete(name: str):
    slicer.delete_custom_printer(name)
    return {"ok": True}


# ---------------- plate objects ----------------

def add_object(path: Path, units: str | float | None = None, name: str | None = None) -> dict[str, Any]:
    try:
        m = convert.load_mesh(path, convert.scale_of(units), "fine")
    except convert.ConvertError as e:
        raise _bad(e)
    if not len(m.faces):
        raise HTTPException(400, f"{path.name} has no triangles")
    oid = uuid.uuid4().hex[:10]
    m.vertices -= (m.bounds[0] + m.bounds[1]) / 2  # own centre at the origin: the UI places it
    with LOCK:
        P.objects[oid] = {"id": oid, "name": name or path.stem, "source": str(path), "mesh": m, "units": units}
    return _object_info(P.objects[oid])


def _object_info(o: dict[str, Any]) -> dict[str, Any]:
    return {"id": o["id"], "name": o["name"], "source": o["source"], **convert.mesh_report(o["mesh"])}


class AddIn(BaseModel):
    path: str
    units: str | float | None = None


@router.post("/objects")
def objects_add(body: AddIn):
    return add_object(Path(body.path).expanduser(), body.units)


class UploadIn(BaseModel):
    filename: str
    data: str  # base64
    units: str | float | None = None


@router.post("/objects/upload")
def objects_upload(body: UploadIn):
    import base64

    raw = base64.b64decode(body.data)
    if len(raw) > 300 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 300 MB)")
    name = Path(body.filename).name
    if Path(name).suffix.lower() not in convert.MESH_EXT | convert.CAD_EXT:
        raise HTTPException(400, "upload a mesh or CAD file (STL, OBJ, 3MF, PLY, STEP, IGES)")
    d = _work() / "uploads" / uuid.uuid4().hex[:8]
    d.mkdir(parents=True)
    (d / name).write_bytes(raw)
    return add_object(d / name, body.units)


@router.get("/objects")
def objects_list():
    with LOCK:
        return {"objects": [_object_info(o) for o in P.objects.values()]}


@router.delete("/objects/{oid}")
def objects_delete(oid: str):
    with LOCK:
        P.objects.pop(oid, None)
    return {"ok": True}


@router.get("/objects/{oid}/mesh")
def objects_mesh(oid: str):
    o = _obj(oid)
    V = np.asarray(o["mesh"].vertices, dtype="<f4")
    F = np.asarray(o["mesh"].faces, dtype="<u4")
    return Response(struct.pack("<II", len(V), len(F)) + V.tobytes() + F.tobytes(), media_type="application/octet-stream")


def _obj(oid: str) -> dict[str, Any]:
    with LOCK:
        o = P.objects.get(oid)
    if not o:
        raise HTTPException(404, "object not on the plate any more; add it again")
    return o


class MatrixIn(BaseModel):
    matrix: list[float]  # 3x3 row-major


@router.post("/objects/{oid}/layflat")
def objects_layflat(oid: str, body: MatrixIn):
    o = _obj(oid)
    M = np.asarray(body.matrix, dtype=float).reshape(3, 3)
    m = o["mesh"].copy()
    m.vertices = m.vertices @ M.T
    R = convert.lay_flat(m)
    return {"matrix": (R @ M).ravel().round(9).tolist()}


# ---------------- slicing ----------------

def _new_job(kind: str) -> dict[str, Any]:
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "kind": kind, "status": "running", "progress": 0, "message": "starting", "started": time.time()}
    with LOCK:
        P.jobs[jid] = job
        old = sorted((j for j in P.jobs.values() if j["kind"] == kind), key=lambda j: j["started"])[:-5]
        for j in old:  # keep the last few results only
            P.jobs.pop(j["id"], None)
    return job


class Instance(BaseModel):
    object: str
    matrix: list[float]  # 3x3 row-major (rotation and scale)
    x: float
    y: float


class SliceIn(BaseModel):
    printer: str
    print: str
    filament: str
    overrides: dict[str, Any] = {}
    instances: list[Instance]
    name: str | None = None


def check_fit(instances: list[Instance], bed: dict[str, Any]) -> list[str]:
    problems = []
    lo, hi, height = np.array(bed["min"]), np.array(bed["max"]), bed["height"]
    for i, inst in enumerate(instances):
        o = _obj(inst.object)
        m = convert.placed(o["mesh"], inst.matrix, inst.x, inst.y)
        a, b = m.bounds
        if (a[:2] < lo - 0.01).any() or (b[:2] > hi + 0.01).any():
            problems.append(f"{o['name']} is outside the bed")
        if b[2] > height + 0.01:
            problems.append(f"{o['name']} is {b[2]:.1f} mm tall; the printer reaches {height:g} mm")
    return problems


def start_slice(body: SliceIn) -> dict[str, Any]:
    if not body.instances:
        raise HTTPException(400, "the plate is empty; add a model first")
    pr = _profiles()
    try:
        cfg = pr.config(body.printer, body.print, body.filament, body.overrides)
    except SlicerError as e:
        raise _bad(e)
    problems = check_fit(body.instances, slicer.bed_of(cfg)["bed"])
    if problems:
        raise HTTPException(400, "; ".join(problems))
    e = _engine()
    job = _new_job("slice")
    d = _work() / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    first = _obj(body.instances[0].object)["name"]
    name = re.sub(r"[^\w.-]+", "_", body.name or first).strip("_") or "print"
    files = []
    for i, inst in enumerate(body.instances):
        m = convert.placed(_obj(inst.object)["mesh"], inst.matrix, inst.x, inst.y)
        f = d / f"object_{i + 1}.stl"
        m.export(str(f))
        files.append(f)
    out = d / f"{name}.gcode"
    job.update(gcode=str(out), name=out.name, settings={"printer": body.printer, "print": body.print, "filament": body.filament,
                                                        "overrides": body.overrides})

    def progress(p: int, s: str):
        job.update(progress=p, message=s)

    def run():
        try:
            r = slicer.slice_models(e, cfg, files, out, progress)
            job.update(status="done", progress=100, message="sliced", **r)
        except SlicerError as ex:
            job.update(status="error", message=str(ex))
        except Exception as ex:  # noqa: BLE001 - reported to the UI
            job.update(status="error", message=f"slicing failed: {type(ex).__name__}: {ex}")

    threading.Thread(target=run, daemon=True).start()
    save_state({"printer": body.printer, "print": body.print, "filament": body.filament, "overrides": body.overrides})
    return job


@router.post("/slice")
def slice_(body: SliceIn):
    return {"job": start_slice(body)["id"]}


def _job(jid: str) -> dict[str, Any]:
    with LOCK:
        j = P.jobs.get(jid)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@router.get("/jobs/{jid}")
def job(jid: str):
    j = _job(jid)
    return {k: v for k, v in j.items() if k not in ("gcode",)}


def wait(jid: str, timeout: float = 900) -> dict[str, Any]:
    t = time.time()
    while time.time() - t < timeout:
        j = _job(jid)
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise HTTPException(504, "slicing is taking too long")


def _done_gcode(jid: str) -> Path:
    j = _job(jid)
    if j["status"] != "done":
        raise HTTPException(409, "the job has not finished")
    return Path(j["gcode"])


@router.get("/jobs/{jid}/toolpaths")
def toolpaths(jid: str, travel: bool = False):
    """Binary: uint32 segments, uint32 layers, float32[6n] positions, uint8[n] types (padded to 4),
    float32[layers] z, uint32[layers] first segment of each layer."""
    tp = slicer.toolpaths(_done_gcode(jid), travel=travel)
    n, nl = len(tp["types"]), len(tp["layer_z"])
    pad = (-n) % 4
    blob = (struct.pack("<II", n, nl) + tp["positions"].astype("<f4").tobytes() + tp["types"].tobytes() + b"\0" * pad
            + np.asarray(tp["layer_z"], "<f4").tobytes() + np.asarray(tp["layer_start"], "<u4").tobytes())
    return Response(blob, media_type="application/octet-stream", headers={"X-Types": json.dumps(slicer.TYPES),
                                                                           "X-Truncated": "1" if tp["truncated"] else "0"})


@router.get("/jobs/{jid}/gcode")
def gcode_download(jid: str):
    g = _done_gcode(jid)
    return FileResponse(g, filename=g.name, media_type="text/x.gcode")


class SaveIn(BaseModel):
    path: str


@router.post("/jobs/{jid}/save")
def gcode_save(jid: str, body: SaveIn):
    import shutil

    g = _done_gcode(jid)
    dst = Path(body.path).expanduser()
    if dst.suffix.lower() not in (".gcode", ".gco", ".g"):
        dst = dst.with_suffix(".gcode")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(g, dst)
    return {"ok": True, "path": str(dst), "message": f"saved G-code to {dst}"}


# ---------------- network printers ----------------

class HostIn(BaseModel):
    name: str
    type: str  # octoprint | moonraker | prusalink
    url: str
    api_key: str = ""


@router.post("/hosts")
def hosts_save(body: HostIn):
    if body.type not in ("octoprint", "moonraker", "prusalink"):
        raise HTTPException(400, "printer type must be octoprint, moonraker or prusalink")
    if not re.match(r"^https?://", body.url):
        raise HTTPException(400, "address must start with http:// or https://")
    hosts = [h for h in load_state().get("hosts", []) if h["name"] != body.name]
    old = next((h for h in load_state().get("hosts", []) if h["name"] == body.name), {})
    h = body.model_dump()
    if h["api_key"] == "•••":  # unchanged in the form
        h["api_key"] = old.get("api_key", "")
    save_state({"hosts": [*hosts, h]})
    return _public_state()


@router.delete("/hosts/{name}")
def hosts_delete(name: str):
    save_state({"hosts": [h for h in load_state().get("hosts", []) if h["name"] != name]})
    return _public_state()


class SendIn(BaseModel):
    host: str
    start: bool = False


@router.post("/jobs/{jid}/send")
def gcode_send(jid: str, body: SendIn):
    g = _done_gcode(jid)
    host = next((h for h in load_state().get("hosts", []) if h["name"] == body.host), None)
    if not host:
        raise HTTPException(404, f"no saved printer connection named {body.host!r}")
    try:
        msg = slicer.upload(host, g, g.name, body.start)
    except SlicerError as e:
        raise _bad(e)
    return {"ok": True, "message": msg}


# ---------------- one call for tools (assistant, MCP) ----------------

def slice_file(path: str | None, printer: str | None, quality: str | None, material: str | None,
               overrides: dict[str, Any] | None, out: str | None, units: str | float | None = None,
               part_stl: Path | None = None) -> dict[str, Any]:
    """Slice one file (or the open part) centred on the bed, lying on its most stable face."""
    pr = _profiles()
    st = load_state()
    printer = printer or st.get("printer")
    if not printer:
        raise HTTPException(400, "no printer chosen; pass printer (use list_printers to find its exact name)")
    try:
        pr.printer(printer)
    except SlicerError:
        hits = [p["name"] for p in pr.printers() if printer.lower() in p["name"].lower()]
        if len(hits) == 1:
            printer = hits[0]
        else:
            raise HTTPException(400, f"no printer named {printer!r}" + (f"; did you mean: {', '.join(hits[:8])}" if hits else ""))
    prints = pr.prints_for(printer)
    d = pr.defaults(printer)
    quality = _pick(quality, [p["name"] for p in prints], d["print"], "quality")
    fils = [f["name"] for f in pr.filaments_for(printer, quality)]
    material = _pick(material, fils, d["filament"], "material")
    src = part_stl or Path(str(path)).expanduser()
    info = add_object(src, units, name=Path(path).stem if path else None)
    try:
        o = _obj(info["id"])
        R = convert.lay_flat(o["mesh"])
        _, pcfg = pr.printer(printer)
        bed = slicer.bed_of(pcfg)["bed"]
        inst = Instance(object=info["id"], matrix=R.ravel().tolist(), x=bed["center"][0], y=bed["center"][1])
        jid = start_slice(SliceIn(printer=printer, print=quality, filament=material, overrides=overrides or {}, instances=[inst],
                                  name=info["name"]))["id"]
        j = wait(jid)
    finally:
        objects_delete(info["id"])
    if j["status"] != "done":
        raise HTTPException(400, j["message"])
    result = {"ok": True, "printer": printer, "quality": quality, "material": material, "stats": j["stats"],
              "warnings": j.get("warnings", []), "gcode": j["gcode"], "job": jid}
    if out:
        result["gcode"] = gcode_save(jid, SaveIn(path=out))["path"]
    s = j["stats"]
    result["message"] = (f"sliced {info['name']} for {printer} ({quality}, {material}): {s['time'] or '?'}, "
                         f"{s['filament_g']:g} g / {s['filament_mm'] / 1000:.2f} m filament, {s['layers']} layers; G-code at {result['gcode']}")
    return result


def _pick(want: str | None, names: list[str], default: str | None, what: str) -> str:
    if not want:
        if not default:
            raise HTTPException(400, f"this printer has no {what} profiles")
        return default
    if want in names:
        return want
    hits = [n for n in names if want.lower() in n.lower()]
    if hits:  # names come recommended-first (the printer's own profiles before generic templates)
        return hits[0]
    raise HTTPException(400, f"no {what} {want!r} for this printer; choose from: {', '.join(names[:15])}")


def find_printers(query: str = "", limit: int = 40) -> list[dict[str, Any]]:
    words = query.lower().split()
    out = [p for p in printers()["printers"] if all(w in f"{p['vendor_name']} {p['name']}".lower() for w in words)]
    return [{"name": p["name"], "vendor": p["vendor_name"], "nozzle": p["nozzle"], "bed_mm": [round(b - a) for a, b in zip(p["bed"]["min"], p["bed"]["max"])] + [p["bed"]["height"]]}
            for p in out[:limit]]


def tmp_stl() -> Path:
    fd, name = tempfile.mkstemp(suffix=".stl", dir=_work())
    os.close(fd)
    return Path(name)
