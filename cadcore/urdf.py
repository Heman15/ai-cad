# SPDX-License-Identifier: AGPL-3.0-or-later
"""URDF (and xacro) import: a robot description becomes an AI CAD assembly with movable joints.

- Each link's visual (or collision) geometry becomes a component:
  - box, cylinder and sphere become editable parametric parts (.aicad) with their sizes as variables;
  - STL, OBJ, DAE and PLY meshes become solids (.brep). Coplanar triangles are merged back into flat
    faces, so meshes exported from CAD regain real planar faces. A mesh has no feature history,
    so these solids aren't parametric.
- Joints become kinematic joints of the assembly (revolute, continuous, prismatic, fixed). Their
  values drive the link poses by forward kinematics; limits are enforced.
- Masses from <inertial> are kept, so they can be compared with the CAD volume.

Units: URDF uses metres and radians; AI CAD uses millimetres and degrees.
Not supported: floating and planar joints (imported as fixed, reported), <mimic> joints
(reported), textures. Missing mesh files are reported and skipped, never replaced by a guess.
"""

from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np

from .assembly import AssemblyError, AssemblySpec, Component, rotation_matrix, rotation_vector

M = 1000.0  # metres -> mm
MAX_TRIANGLES = 30000  # larger meshes are decimated (shape kept within the mesh's own accuracy)
JOINT_TYPES = ("revolute", "continuous", "prismatic", "fixed", "floating", "planar")


# ---------------- transforms ----------------

def rpy_matrix(r: float, p: float, y: float) -> np.ndarray:
    """URDF fixed-axis roll/pitch/yaw: R = Rz(yaw) @ Ry(pitch) @ Rx(roll)."""
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def frame(xyz_mm, R) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, xyz_mm
    return T


def axis_angle(axis, deg: float) -> np.ndarray:
    a = np.asarray(axis, dtype=float)
    return rotation_matrix(a / np.linalg.norm(a) * deg)


def to_pose(T: np.ndarray) -> list[float]:
    """4x4 -> [x, y, z, rx, ry, rz] (position mm, rotation vector degrees)."""
    return [float(v) for v in T[:3, 3]] + [float(v) for v in rotation_vector(T[:3, :3])]


def from_pose(p: list[float]) -> np.ndarray:
    return frame(p[:3], rotation_matrix(p[3:6]))


# ---------------- parsing ----------------

def _floats(s: str | None, n: int, default: float = 0.0) -> list[float]:
    if not s:
        return [default] * n
    v = [float(x) for x in s.split()]
    if len(v) != n:
        raise AssemblyError(f"expected {n} numbers, got {s!r}")
    return v


def _origin(el) -> dict[str, list[float]]:
    o = el.find("origin") if el is not None else None
    xyz = _floats(o.get("xyz") if o is not None else None, 3)
    rpy = _floats(o.get("rpy") if o is not None else None, 3)
    return {"xyz": [v * M for v in xyz], "rpy": [math.degrees(v) for v in rpy]}


def origin_matrix(o: dict[str, list[float]]) -> np.ndarray:
    return frame(o["xyz"], rpy_matrix(*[math.radians(v) for v in o["rpy"]]))


def read_xml(path: Path) -> ET.Element:
    """Parse a URDF, expanding xacro macros first when needed."""
    text = path.read_text()
    if path.suffix == ".xacro" or "xmlns:xacro" in text or "<xacro:" in text:
        try:
            import xacro
        except ImportError as e:  # pragma: no cover
            raise AssemblyError("this is a xacro file and the xacro package isn't installed (pip install xacro)") from e
        try:
            text = xacro.process_file(str(path)).toxml()
        except Exception as e:
            raise AssemblyError(f"xacro could not expand {path.name}: {e}") from e
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise AssemblyError(f"{path.name} is not valid XML: {e}") from e
    if root.tag != "robot":
        raise AssemblyError(f"{path.name} is not a URDF (root element is <{root.tag}>, expected <robot>)")
    return root


def parse(path: str | Path) -> dict[str, Any]:
    """Robot description in AI CAD units (mm, degrees)."""
    path = Path(path).expanduser()
    if not path.exists():
        raise AssemblyError(f"file not found: {path}")
    root = read_xml(path)
    materials = {m.get("name"): _rgba(m) for m in root.findall("material")}
    links: dict[str, Any] = {}
    for le in root.findall("link"):
        name = le.get("name")
        inertial = le.find("inertial")
        mass = inertial.find("mass") if inertial is not None else None
        links[name] = {
            "name": name,
            "mass_kg": float(mass.get("value")) if mass is not None else None,
            "visuals": [_geometry(v, materials) for v in le.findall("visual")],
            "collisions": [_geometry(c, materials) for c in le.findall("collision")],
        }
    joints = []
    for je in root.findall("joint"):
        jt = je.get("type")
        if jt not in JOINT_TYPES:
            raise AssemblyError(f"joint {je.get('name')}: unknown type {jt!r}")
        lim = je.find("limit")
        prism = jt == "prismatic"
        conv = (lambda v: v * M) if prism else math.degrees
        lower = conv(float(lim.get("lower"))) if lim is not None and lim.get("lower") is not None else None
        upper = conv(float(lim.get("upper"))) if lim is not None and lim.get("upper") is not None else None
        if jt == "continuous":
            lower = upper = None
        ax = je.find("axis")
        axis = _floats(ax.get("xyz") if ax is not None else None, 3) if ax is not None else [1.0, 0.0, 0.0]
        if np.linalg.norm(axis) < 1e-12:
            raise AssemblyError(f"joint {je.get('name')}: axis is zero")
        parent, child = je.find("parent").get("link"), je.find("child").get("link")
        for ln in (parent, child):
            if ln not in links:
                raise AssemblyError(f"joint {je.get('name')} refers to missing link {ln!r}")
        joint = {"name": je.get("name"), "type": jt, "parent": parent, "child": child, "origin": _origin(je),
                 "axis": axis, "lower": lower, "upper": upper, "value": 0.0, "mimic": None}
        mim = je.find("mimic")
        if mim is not None:
            joint.update(mimic=mim.get("joint"), multiplier=float(mim.get("multiplier", 1)), offset=conv(float(mim.get("offset", 0))))
        joints.append(joint)
    children = {j["child"] for j in joints}
    roots = [n for n in links if n not in children]
    if len(roots) != 1:
        raise AssemblyError(f"a URDF must form one tree; found root links {roots or 'none (a loop)'}")
    if len(children) != len(joints):
        raise AssemblyError("a link is the child of more than one joint")
    return {"name": root.get("name") or path.stem, "file": str(path), "root": roots[0], "links": links, "joints": joints}


def _rgba(m) -> str | None:
    c = m.find("color") if m is not None else None
    if c is None or not c.get("rgba"):
        return None
    r, g, b, _a = _floats(c.get("rgba"), 4)
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(1, v)) * 255)) for v in (r, g, b))


def _geometry(el, materials) -> dict[str, Any]:
    g = el.find("geometry")
    shape = list(g)[0] if g is not None and len(g) else None
    out: dict[str, Any] = {"origin": _origin(el), "name": el.get("name")}
    mat = el.find("material")
    out["color"] = (_rgba(mat) or materials.get(mat.get("name"))) if mat is not None else None
    if shape is None:
        out["kind"] = "none"
    elif shape.tag == "box":
        out.update(kind="box", size=[v * M for v in _floats(shape.get("size"), 3)])
    elif shape.tag == "cylinder":
        out.update(kind="cylinder", radius=float(shape.get("radius")) * M, length=float(shape.get("length")) * M)
    elif shape.tag == "sphere":
        out.update(kind="sphere", radius=float(shape.get("radius")) * M)
    elif shape.tag == "mesh":
        out.update(kind="mesh", uri=shape.get("filename"), scale=_floats(shape.get("scale"), 3, 1.0) if shape.get("scale") else [1.0, 1.0, 1.0])
    else:
        out.update(kind="unsupported", tag=shape.tag)
    return out


# ---------------- mesh files ----------------

def resolve_uri(uri: str, urdf: Path) -> Path | None:
    """package://pkg/rel, file:///abs, or a path relative to the URDF."""
    if uri.startswith("file://"):
        p = Path(uri[7:])
        return p if p.exists() else None
    if uri.startswith("package://"):
        pkg, _, rel = uri[10:].partition("/")
        bases = [Path(b) for b in os.environ.get("ROS_PACKAGE_PATH", "").split(":") if b]
        bases += list(urdf.resolve().parents)[:5]
        for base in bases:
            for cand in (base / pkg / rel, base / rel if base.name == pkg else None):
                if cand and cand.exists():
                    return cand
            pkg_dir = _find_package(base, pkg)
            if pkg_dir and (pkg_dir / rel).exists():
                return pkg_dir / rel
        return None
    p = (urdf.parent / uri) if not Path(uri).is_absolute() else Path(uri)
    return p if p.exists() else None


def _find_package(base: Path, pkg: str) -> Path | None:
    """A directory under base (two levels) whose package.xml names pkg."""
    try:
        for px in list(base.glob("package.xml")) + list(base.glob("*/package.xml")) + list(base.glob("*/*/package.xml")):
            if re.search(rf"<name>\s*{re.escape(pkg)}\s*</name>", px.read_text(errors="ignore")):
                return px.parent
    except OSError:
        pass
    return None


def mesh_solid(path: Path, scale: list[float]) -> tuple[Any, dict[str, Any]]:
    """Load a mesh (mm, scaled) and turn it into a CAD solid; returns (cq shape, report)."""
    import cadquery as cq
    import trimesh

    try:
        m = trimesh.load(str(path), force="mesh")
    except Exception as e:
        raise AssemblyError(f"could not read mesh {path.name}: {e}") from e
    if not isinstance(m, trimesh.Trimesh) or not len(m.faces):
        raise AssemblyError(f"mesh {path.name} has no triangles")
    unit = 1.0
    if path.suffix.lower() == ".dae":  # COLLADA declares its unit (as RViz applies it)
        u = re.search(r'<unit[^>]*meter="([0-9.eE+-]+)"', path.read_text(errors="ignore")[:4000])
        unit = float(u.group(1)) if u else 1.0
    m.vertices = m.vertices * (np.asarray(scale) * unit * M)
    report: dict[str, Any] = {"triangles_in": int(len(m.faces))}
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    if len(m.faces) > MAX_TRIANGLES:
        try:
            m = m.simplify_quadric_decimation(face_count=MAX_TRIANGLES)
            report["decimated_to"] = int(len(m.faces))
        except Exception as e:  # fast-simplification missing: keep full detail
            report["decimation_skipped"] = str(e)
    if not m.is_watertight:
        trimesh.repair.fill_holes(m)
    trimesh.repair.fix_normals(m)
    closed = bool(m.is_watertight)
    report["closed"] = closed
    shape = _faces_to_shape(m.vertices, m.faces, closed)
    report["faces"] = len(shape.Faces())
    if closed:
        v = shape.Volume()
        if v < 0:  # inward normals
            shape = cq.Shape.cast(shape.wrapped.Reversed())
        report["volume_mm3"] = abs(v)
    return shape, report


def _faces_to_shape(V, F, closed: bool):
    """Triangles -> topologically connected shell (shared vertices and edges), then a solid if closed.
    Coplanar neighbours are merged into single planar faces."""
    import cadquery as cq
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Pnt
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopoDS import TopoDS, TopoDS_Shell, TopoDS_Solid

    verts = [BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, p))).Vertex() for p in V]
    edges: dict[tuple[int, int], Any] = {}

    def edge(a, b):
        k = (min(a, b), max(a, b))
        e = edges.get(k)
        if e is None:
            e = edges[k] = BRepBuilderAPI_MakeEdge(verts[k[0]], verts[k[1]]).Edge()
        return e if a < b else TopoDS.Edge_s(e.Reversed())

    bld = BRep_Builder()
    shell = TopoDS_Shell()
    bld.MakeShell(shell)
    for a, b, c in F:
        mf = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge(a, b), edge(b, c), edge(c, a)).Wire(), True)
        if mf.IsDone():
            bld.Add(shell, mf.Face())
    if closed:
        solid = TopoDS_Solid()
        bld.MakeSolid(solid)
        bld.Add(solid, shell)
        shape = solid
    else:
        shape = shell
    u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    u.Build()
    return cq.Shape.cast(u.Shape())


# ---------------- primitives as parametric parts ----------------

def primitive_part(g: dict[str, Any], name: str) -> dict[str, Any]:
    """PartSpec dict for a box/cylinder/sphere centred on its own origin (as in URDF)."""
    r4 = lambda v: round(v, 4)  # noqa: E731
    if g["kind"] == "box":
        sx, sy, sz = map(r4, g["size"])
        variables = {"size_x": sx, "size_y": sy, "size_z": sz}
        features = [{"id": "box", "type": "extrude", "params": {
            "plane": {"base": "XY", "offset": "=-size_z/2"}, "distance": "=size_z",
            "shapes": [{"type": "rect", "w": "=size_x", "h": "=size_y", "x": 0, "y": 0, "centered": True}]}}]
    elif g["kind"] == "cylinder":
        variables = {"radius": r4(g["radius"]), "length": r4(g["length"])}
        features = [{"id": "cylinder", "type": "extrude", "params": {
            "plane": {"base": "XY", "offset": "=-length/2"}, "distance": "=length",
            "shapes": [{"type": "circle", "d": "=2*radius", "x": 0, "y": 0}]}}]
    else:  # sphere: revolve a half disc about the Z axis
        variables = {"radius": r4(g["radius"])}
        features = [
            {"id": "profile", "type": "sketch", "params": {
                "plane": "XZ", "points": {"top": [0, g["radius"]], "bottom": [0, -g["radius"]]},
                "entities": [{"id": "axis", "type": "line", "p1": "bottom", "p2": "top"},
                             {"id": "arc", "type": "arc", "center": "origin", "start": "bottom", "end": "top"}],
                "constraints": [{"type": "vertical", "line": "axis"},
                                {"type": "distance", "a": "origin", "b": "top", "value": "=radius"},
                                {"type": "distance", "a": "origin", "b": "bottom", "value": "=radius"}]}},
            {"id": "sphere", "type": "revolve", "params": {"sketch": "profile", "axis": "v", "angle": 360}},
        ]
    return {"spec_version": 2, "name": name, "variables": variables, "limits": {k: {"min": 1e-6} for k in variables},
            "features": features, "units": "mm",
            "source": {"type": "urdf", "note": "imported from URDF; sizes are the URDF values in mm"}}


# ---------------- forward kinematics ----------------

def joint_motion(j: dict[str, Any], value: float) -> np.ndarray:
    if j["type"] in ("revolute", "continuous"):
        return frame([0, 0, 0], axis_angle(j["axis"], value))
    if j["type"] == "prismatic":
        a = np.asarray(j["axis"], dtype=float)
        return frame(a / np.linalg.norm(a) * value, np.eye(3))
    return np.eye(4)


def link_frames(robot: dict[str, Any]) -> dict[str, np.ndarray]:
    """World transform of every link at the current joint values (root at the assembly origin)."""
    by_child = {j["child"]: j for j in robot["joints"]}
    values = {j["name"]: j["value"] for j in robot["joints"]}
    out: dict[str, np.ndarray] = {robot["root"]: robot_base(robot)}

    def get(link):
        if link in out:
            return out[link]
        j = by_child[link]
        v = values[j["mimic"]] * j.get("multiplier", 1.0) + j.get("offset", 0.0) if j.get("mimic") in values else j["value"]
        out[link] = get(j["parent"]) @ origin_matrix(j["origin"]) @ joint_motion(j, v)
        return out[link]

    for name in robot["links"]:
        get(name)
    return out


def robot_base(robot: dict[str, Any]) -> np.ndarray:
    return from_pose(robot.get("base", [0, 0, 0, 0, 0, 0]))


def apply_kinematics(spec: AssemblySpec) -> None:
    """Place every robot component from its link's frame. Robot components are fixed for the mate solver."""
    robot = spec.robot
    if not robot:
        return
    frames = link_frames(robot)
    for c in spec.components:
        if c.link and c.link in frames:
            T = frames[c.link] @ from_pose(c.link_pose or [0] * 6)
            pose = to_pose(T)
            c.position, c.rotation = pose[:3], pose[3:]


def set_joint(robot: dict[str, Any], name: str, value: float) -> str:
    j = next((x for x in robot["joints"] if x["name"] == name), None)
    if j is None:
        raise AssemblyError(f"no joint {name!r}")
    if j["type"] not in ("revolute", "continuous", "prismatic"):
        raise AssemblyError(f"joint {name} is {j['type']} and can't move")
    if j.get("mimic"):
        raise AssemblyError(f"joint {name} follows {j['mimic']} (mimic); move that joint instead")
    value = float(value)
    lo, hi = j.get("lower"), j.get("upper")
    if lo is not None and hi is not None and not (lo - 1e-9 <= value <= hi + 1e-9):
        unit = "mm" if j["type"] == "prismatic" else "°"
        raise AssemblyError(f"joint {name}: {value:g}{unit} is outside its limits {lo:g}…{hi:g}{unit}")
    j["value"] = value
    return f"{name} = {value:g}{' mm' if j['type'] == 'prismatic' else '°'}"


# ---------------- import ----------------

def import_urdf(urdf_path: str | Path, out_dir: str | Path | None = None, geometry: str = "visual") -> dict[str, Any]:
    """Write part files and an .aicadasm for the robot; returns {"assembly": path, "report": {...}}."""
    import cadquery as cq

    urdf = Path(urdf_path).expanduser().resolve()
    robot = parse(urdf)
    out = Path(out_dir).expanduser() if out_dir else urdf.parent / f"{_safe(robot['name'])}_aicad"
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"robot": robot["name"], "links": len(robot["links"]), "joints": len(robot["joints"]),
                              "parametric_parts": 0, "mesh_parts": 0, "skipped": [], "warnings": [], "meshes": {}}
    spec = AssemblySpec(name=robot["name"])
    files: dict[str, str] = {}  # identical geometry -> one part file (BOM quantities)
    for link in robot["links"].values():
        geoms = link["visuals"] if geometry == "visual" else link["collisions"]
        if not geoms and geometry == "visual" and link["collisions"]:
            geoms = link["collisions"]
            report["warnings"].append(f"link {link['name']}: no visual geometry, used its collision geometry")
        for i, g in enumerate(geoms):
            label = f"link {link['name']}"
            if g["kind"] in ("none", "unsupported"):
                report["skipped"].append(f"{label}: {g.get('tag', 'empty')} geometry is not supported")
                continue
            if g["kind"] == "mesh":
                src = resolve_uri(g["uri"], urdf)
                if src is None:
                    report["skipped"].append(f"{label}: mesh {g['uri']} not found (looked next to the URDF, in parent folders and ROS_PACKAGE_PATH)")
                    continue
                key = f"mesh:{src.resolve()}:{g['scale']}"
                if key not in files:
                    try:
                        shape, mrep = mesh_solid(src, g["scale"])
                    except AssemblyError as e:
                        report["skipped"].append(f"{label}: {e}")
                        continue
                    fname = _unique(out, _safe(src.stem), ".brep", files)
                    shape.exportBrep(str(out / fname))
                    files[key] = fname
                    report["meshes"][fname] = {"source": str(src), **mrep}
                    if not mrep["closed"]:
                        report["warnings"].append(f"{src.name}: the mesh isn't closed, so it was imported as surfaces (no volume or mass)")
                    report["mesh_parts"] += 1
            else:
                key = f"{g['kind']}:{json.dumps({k: round(v, 6) if isinstance(v, float) else v for k, v in g.items() if k in ('size', 'radius', 'length')}, sort_keys=True)}"
                if key not in files:
                    label_ = _primitive_name(g)
                    fname = _unique(out, _safe(label_), ".aicad", files)
                    (out / fname).write_text(json.dumps(primitive_part(g, label_), indent=2))
                    files[key] = fname
                    report["parametric_parts"] += 1
            cid = _safe(link["name"]) if len(geoms) == 1 else f"{_safe(link['name'])}_{i + 1}"
            spec.components.append(Component(cid, files[key], fixed=True, name=link["name"], link=link["name"],
                                              link_pose=to_pose(origin_matrix(g["origin"])), color=g.get("color")))
    for j in robot["joints"]:
        if j["type"] in ("floating", "planar"):
            report["warnings"].append(f"joint {j['name']} is {j['type']}; imported as fixed")
        if j.get("mimic"):
            report["warnings"].append(f"joint {j['name']} follows {j['mimic']} (mimic × {j['multiplier']:g})")
    if not spec.components:
        raise AssemblyError("no geometry could be imported: " + "; ".join(report["skipped"][:3]) if report["skipped"] else "the URDF has no geometry")
    robot["geometry"] = geometry
    spec.robot = robot
    apply_kinematics(spec)
    # masses from <inertial> vs the CAD volume of the same link
    vols: dict[str, float] = {}
    for c in spec.components:
        p = out / c.file
        shape = cq.Shape.importBrep(str(p)) if p.suffix == ".brep" else None
        if shape is None:
            from .project import Project
            pr = Project.load(p)
            shape = pr.result.shape.val() if pr.result else None
        if shape is not None and shape.isValid() and shape.ShapeType() == "Solid":
            vols[c.link] = vols.get(c.link, 0.0) + abs(shape.Volume())
    report["masses"] = [{"link": n, "mass_kg": l["mass_kg"], "cad_volume_mm3": round(vols[n], 1) if n in vols else None,
                         "implied_density": round(l["mass_kg"] / (vols[n] * 1e-9)) if l["mass_kg"] and vols.get(n) else None}
                        for n, l in robot["links"].items() if l["mass_kg"] is not None]
    asm_path = out / f"{_safe(robot['name'])}.aicadasm"
    asm_path.write_text(json.dumps(spec.to_dict(), indent=2))
    report["folder"] = str(out)
    return {"assembly": str(asm_path), "report": report}


def _primitive_name(g: dict[str, Any]) -> str:
    f = lambda v: f"{v:.4g}"  # noqa: E731
    if g["kind"] == "box":
        return "box " + "x".join(f(v) for v in g["size"])
    if g["kind"] == "cylinder":
        return f"cylinder D{f(2 * g['radius'])} L{f(g['length'])}"
    return f"sphere D{f(2 * g['radius'])}"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_") or "part"


def _unique(out: Path, stem: str, ext: str, used: dict[str, str]) -> str:
    taken = set(used.values())
    name, n = f"{stem}{ext}", 2
    while name in taken:
        name, n = f"{stem}_{n}{ext}", n + 1
    return name
