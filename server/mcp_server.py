# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP server: lets Claude Code (VS Code extension or CLI, using your own Claude login) drive the running
AI CAD app. Every tool goes through the app's HTTP API, the same validated commands the UI uses, so
changes appear live in the app window, and the app's undo history covers them too.

Register once with Claude Code:
    claude mcp add ai-cad -- /path/to/cad/.venv/bin/python /path/to/cad/server/mcp_server.py
Then start AI CAD and ask Claude, for example: "make a CAD model of ~/drawings/bracket.pdf".

The app is found through ~/.config/ai-cad/backend.json, written when it starts, or AI_CAD_URL.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

import logging

import httpx
from mcp.server.mcpserver import Image, MCPServer

logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request log lines

ROOT = Path(__file__).resolve().parent.parent


def _doc(rel: str) -> str:
    """Module docstring without importing it (keeps this process light)."""
    try:
        return ast.get_docstring(ast.parse((ROOT / rel).read_text())) or ""
    except OSError:
        return ""


FEATURE_DOC = _doc("cadcore/builder.py")
SKETCH_DOC = _doc("cadcore/sketch.py")

INSTRUCTIONS = """You are driving AI CAD, a parametric CAD application that the user has open on screen. Every change you make appears live in the user's window, and the user can undo it there.

Rules:
- Read the model first (get_model) and the user's selection (get_selection) when they say "this".
- Before building, make sure you have the dimensions that define the part. Ask about missing ones instead of inventing them. You may assume cosmetic details, but say what you assumed.
- Put the main dimensions in variables and drive the features with formulas, so the design stays editable.
- After building or changing a part:
  - call render_views and check the result visually;
  - call record_design_intent (through run_command) to list the user's requirements separately from your assumptions.
- For a drawing (PDF or image):
  - call import_drawing, so the app keeps the drawing and its dimensions traceable;
  - read every printed dimension, and record them with run_command record_drawing_analysis (each with a bbox as fractions of the page image);
  - build the part, then link each variable to its dimension with link_dimension;
  - never take a dimension by measuring the picture's scale.
- Report mass, stress and deflection only from tool results. A strength analysis needs supports and loads stated by the user."""

mcp = MCPServer("ai-cad", instructions=INSTRUCTIONS)


def _base() -> str:
    if os.environ.get("AI_CAD_URL"):
        return os.environ["AI_CAD_URL"].rstrip("/")
    info = Path(os.environ.get("AI_CAD_CONFIG", Path.home() / ".config" / "ai-cad" / "config.json")).parent / "backend.json"
    try:
        return json.loads(info.read_text())["url"]
    except (OSError, ValueError, KeyError):
        raise RuntimeError("AI CAD is not running. Start the app (desktop app or ./run.sh), then try again.")


def _req(method: str, path: str, body: Any = None, timeout: float = 300) -> Any:
    try:
        r = httpx.request(method, _base() + path, json=body, timeout=timeout)
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach AI CAD. Is the app still open?")
    if r.headers.get("content-type", "").startswith("application/json"):
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(data.get("detail", f"request failed ({r.status_code})"))
        return data
    if r.status_code >= 400:
        raise RuntimeError(f"request failed ({r.status_code})")
    return r.content


def _model_summary(m: dict[str, Any]) -> dict[str, Any]:
    keep = ("spec", "parameters", "dependencies", "evaluated", "volume_mm3", "bbox_mm", "mass_kg", "annotations", "sheet", "threads", "path", "can_undo", "can_redo")
    return {k: m.get(k) for k in keep if k in m}


def _cmd_result(r: dict[str, Any]) -> dict[str, Any]:
    res = r.get("result", {})
    m = r.get("model") or {}
    return {"ok": res.get("ok"), "message": res.get("message"), "volume_mm3": m.get("volume_mm3"), "bbox_mm": m.get("bbox_mm"),
            "diff": (res.get("data") or {}).get("diff")}


# ---------------- inspection ----------------

@mcp.tool()
def get_model() -> dict[str, Any]:
    """The open part: variables, feature tree with parameters, material, volume, mass, bounding box, annotations, sheet-metal state."""
    return _model_summary(_req("GET", "/api/model"))


@mcp.tool()
def get_selection() -> dict[str, Any]:
    """What the user has selected in the 3D view (faces/edges with owning feature, re-checked against the model)."""
    return _req("GET", "/api/ui/selection")


@mcp.tool()
def list_entities(kind: str = "faces", feature: str | None = None) -> list[dict[str, Any]]:
    """Faces or edges of the current solid with owning feature and geometry (point, normal, radius), for picking planes/edges."""
    ents = (_req("GET", "/api/model").get("entities") or {}).get(kind, [])
    keep = ("index", "feature", "type", "point", "normal", "area", "length", "radius", "axis_dir", "center", "hole_instance")
    return [{k: e[k] for k in keep if k in e} for e in ents if feature is None or e.get("feature") == feature][:120]


@mcp.tool()
def measure(refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure one or two entities (refs as returned by get_selection/list_entities: kind, point, type, normal)."""
    return _req("POST", "/api/measure", {"refs": refs})


@mcp.tool()
def render_views(projection: str = "first", target: str = "auto", sections: list[dict[str, Any]] | None = None) -> Image:
    """Picture to check your work. target "part": hidden-line front/top/side views, isometric, overall and hole
    dimensions of the open part. target "assembly": shaded isometric/front/right/top views of the assembly or robot in
    its current pose. "auto" uses the part when it has geometry, otherwise the assembly.
    sections (part only): hatched section views, e.g. [{"plane": "XZ"}] = section A-A through the middle, seen from the
    front; each {plane: XY|YZ|XZ, offset (mm along the plane's normal axis; default the middle), flip, label, view}."""
    import cairosvg

    if target == "auto":
        target = "part" if _req("GET", "/api/model").get("entities") else "assembly"
    if target == "assembly":
        return Image(data=_req("GET", "/api/asm/render.png?size=700"), format="png")
    q = f"&sections={urllib.parse.quote(json.dumps(sections))}" if sections else ""
    svg = _req("GET", f"/api/drawing.svg?sheet=A3&projection={projection}{q}")
    return Image(data=cairosvg.svg2png(bytestring=svg, output_width=1600), format="png")


# ---------------- modelling ----------------

@mcp.tool(description="Run one modelling command on the open part (validated; a rejected command leaves the model unchanged).\n"
          "Commands: new_part, create_plate, create_l_bracket, create_shaft, create_flange, set_variable(name, value, min, max), "
          "set_parameter(name, value), add_feature(id, type, params, after), add_hole(id, diameter|thread, positions, face|plane, depth, "
          "cbore_diameter, cbore_depth, csk_diameter, csk_angle, thread_depth, or Hole Wizard standard=\"ISO\", type, size, fit), add_pocket, update_feature(id, params), remove_feature(id), "
          "suppress_feature(id, suppressed), assign_material(name, density, E, nu, yield_strength), set_name(name), "
          "record_design_intent(requirements, assumptions, open_questions), add_annotation(type, ...), remove_annotation(id), "
          "set_sheet_metal(thickness, bend_radius, k_factor), set_analysis_setup(supports, loads), "
          "record_drawing_analysis(units, dimensions[{id, value, kind, describes, page, bbox, status, confidence}], questions, ...), "
          "link_dimension(dimension_id, parameter), update_drawing_dimension(dimension_id, value).\n\n"
          "FEATURE TYPES for add_feature:\n" + FEATURE_DOC + "\n\nSKETCHES:\n" + SKETCH_DOC)
def run_command(command: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return _cmd_result(_req("POST", "/api/command", {"command": command, "args": args or {}}))


@mcp.tool()
def run_batch(commands: list[dict[str, Any]], label: str = "Claude Code edit") -> dict[str, Any]:
    """Several commands ([{command, args}]) as ONE transaction and one undo step; all-or-nothing."""
    return _cmd_result(_req("POST", "/api/batch", {"commands": commands, "label": label}))


@mcp.tool()
def undo() -> dict[str, Any]:
    """Undo the last change (same history as the app's Undo button)."""
    return _cmd_result(_req("POST", "/api/undo"))


@mcp.tool()
def redo() -> dict[str, Any]:
    """Redo."""
    return _cmd_result(_req("POST", "/api/redo"))


# ---------------- drawings in / out ----------------

_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


@mcp.tool()
def import_drawing(path: str) -> list[Image | str]:
    """Attach a PDF/PNG/JPEG drawing to the open part (shown in the app's Drawing tab for traceability) and
    return its rendered pages. bbox fractions you record refer to these page images."""
    p = Path(path).expanduser()
    media = _TYPES.get(p.suffix.lower())
    if not media:
        raise RuntimeError("use a PDF, PNG, JPEG or WebP file")
    r = _req("POST", "/api/source-drawing", {"filename": p.name, "media_type": media, "data": base64.b64encode(p.read_bytes()).decode()})
    pages = r["model"]["spec"]["source"]["drawing"]["pages"]
    out: list[Image | str] = [f"{p.name}: {pages} page(s) attached"]
    for i in range(1, pages + 1):
        out.append(Image(data=_req("GET", f"/api/source-drawing/page/{i}"), format="png"))
    return out


@mcp.tool()
def export_file(kind: str, path: str, sheet: str = "A3", projection: str = "first",
                sections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Export the part. kind: step | stl | drawing (PDF or SVG by extension) | flat (sheet-metal flat pattern, DXF or SVG).
    Drawings may add hatched section views: sections=[{"plane": "XZ"|"YZ"|"XY", "offset": mm, "flip": bool}]."""
    if kind in ("step", "stl"):
        return _req("POST", f"/api/export/{kind}", {"path": path})
    if kind == "drawing":
        return _req("POST", "/api/drawing/export", {"path": path, "sheet": sheet, "projection": projection, "sections": sections})
    if kind == "flat":
        return _req("POST", "/api/flat/export", {"path": path})
    raise RuntimeError("kind must be step, stl, drawing or flat")


@mcp.tool()
def project_file(action: str, path: str | None = None) -> dict[str, Any]:
    """action: save (to path, .aicad) | open (path) | new (empty part)."""
    if action == "save":
        return {k: v for k, v in _req("POST", "/api/project/save", {"path": path}).items() if k != "model"}
    if action == "open":
        return _model_summary(_req("POST", "/api/project/open", {"path": path})["model"])
    if action == "new":
        _req("POST", "/api/new")
        return {"ok": True}
    raise RuntimeError("action must be save, open or new")


# ---------------- analysis ----------------

@mcp.tool()
def check_manufacturability(process: str = "cnc_milling") -> dict[str, Any]:
    """Rule-based DFM screening: process cnc_milling | fdm_printing | sheet_metal. Findings include thresholds."""
    return _req("POST", "/api/analysis/dfm", {"process": process})


@mcp.tool()
def run_stress_analysis(supports: list[dict[str, Any]], loads: list[dict[str, Any]], check_convergence: bool = False) -> dict[str, Any]:
    """Linear-static FEA. supports: [{face: {point, normal}}]; loads: [{face, force: [Fx,Fy,Fz] N} | {face, pressure: MPa}].
    Only use loads the user specified. Needs a material with E/nu (assign_material)."""
    r = _req("POST", "/api/command", {"command": "set_analysis_setup", "args": {"supports": supports, "loads": loads}})
    if not r["result"]["ok"]:
        return r["result"]
    out = _req("POST", "/api/analysis/fea", {"check_convergence": check_convergence}, timeout=900)
    out.pop("display", None)
    return out


# ---------------- assemblies ----------------

@mcp.tool()
def assembly(command: str = "get", args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assembly workspace. command 'get' returns components, mates, solver status, explode steps and BOM. Others:
    add_component(file, position, rotation, fixed), remove_component(id), set_transform(id, position, rotation),
    move_component(id, translate [dx,dy,dz], axis, angle deg, pivot?) for free components (mates re-solve),
    set_fixed(id, fixed), set_visibility(ids id|[ids]|'*', hidden?, transparent?),
    add_mate(type, a {component, face {point, normal}}, b {...}, value, aligned) with type
      coincident|distance (value mm)|parallel|perpendicular|angle (value deg between normals, 0-180)|concentric|lock
      (lock needs only {component}; it keeps the current relative pose),
    edit_mate(id, value?, aligned?, suppressed?), flip_mate(id), remove_mate(id),
    pattern_component(id, type linear|circular, count, direction, spacing | axis, point, angle),
    add_explode_step(components, direction, distance), edit_explode_step(id, ...), remove_explode_step(id),
    auto_explode(scale), clear_explode (exploded views are display offsets saved in the file),
    new, save(path), open(path), interference, mass (mass, volume, centre of mass), bom_csv,
    measure(refs [{kind face|edge|vertex, component, point (world)}]). Mate faces use component-local coordinates.
    Robots: import_urdf(path, out_dir?, geometry visual|collision) converts a URDF or xacro into parts plus an
    assembly and opens it; joints(values {joint: degrees or mm}) moves joints; set_joint(name, value) does it as an undo step."""
    if command == "get":
        m = _req("GET", "/api/asm")
    elif command in ("new", "undo", "redo", "interference"):
        m = _req("POST", f"/api/asm/{command}")
        if command == "interference":
            return m
    elif command == "mass":
        return _req("GET", "/api/asm/mass")
    elif command == "bom_csv":
        return {"csv": _req("GET", "/api/asm/bom.csv").decode()}
    elif command == "measure":
        return _req("POST", "/api/asm/measure", {"refs": (args or {}).get("refs", [])})
    elif command in ("save", "open"):
        m = _req("POST", f"/api/asm/{command}", {"path": (args or {}).get("path")})
    elif command == "import_urdf":
        r = _req("POST", "/api/asm/import_urdf", args or {})
        m = {**r["model"], "report": r["report"], "path": r["path"]}
    elif command == "joints":
        return _req("POST", "/api/asm/joints", {"values": (args or {}).get("values", {})})
    else:
        r = _req("POST", "/api/asm/command", {"command": command, "args": args or {}})
        m = {**r["model"], "result": r["result"]}
    m.pop("entities", None)
    return m


@mcp.tool()
def get_from_github(url: str, path: str | None = None) -> dict[str, Any]:
    """Public GitHub repositories only. Without path: list the repository's URDF/xacro and STEP files (no download).
    With path: download that file's folder (its ROS package, so meshes come along) and return the local file,
    the top-level entry points and the licence note (always tell the user the licence note).
    Then import a robot with assembly(command="import_urdf", args={"path": <entry point>})."""
    return _req("POST", "/api/github", {"url": url, "path": path})


if __name__ == "__main__":
    mcp.run("stdio")
