# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading any supported 3D file as triangles or as a CAD shape, and converting it to STL.

Formats: meshes (STL, OBJ, PLY, OFF, 3MF, GLB/glTF, DAE), CAD (STEP, IGES, BREP), AI CAD parts (.aicad/.json)
and assemblies (.aicadasm). Meshes have no units; `scale` turns them into millimetres.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

MESH_EXT = {".stl", ".obj", ".ply", ".off", ".3mf", ".glb", ".gltf", ".dae"}
CAD_EXT = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
PART_EXT = {".aicad", ".json"}
ASM_EXT = {".aicadasm"}
ALL_EXT = MESH_EXT | CAD_EXT | PART_EXT | ASM_EXT
UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "ft": 304.8}
QUALITY = {"coarse": (0.1, 0.5), "normal": (0.02, 0.2), "fine": (0.005, 0.1)}  # linear mm, angular rad


class ConvertError(Exception):
    pass


def kind_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in MESH_EXT:
        return "mesh"
    if ext in CAD_EXT:
        return "cad"
    if ext in PART_EXT:
        return "part"
    if ext in ASM_EXT:
        return "assembly"
    raise ConvertError(f"{Path(path).name}: unsupported file type; use one of {', '.join(sorted(ALL_EXT))}")


def scale_of(units: str | float | None) -> float:
    if units is None or units == "":
        return 1.0
    if isinstance(units, (int, float)):
        if units <= 0:
            raise ConvertError("scale must be positive")
        return float(units)
    if units not in UNITS:
        raise ConvertError(f"units must be one of {', '.join(UNITS)}")
    return UNITS[units]


def _check(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConvertError(f"file not found: {path}")
    with open(path, "rb") as fh:
        if fh.read(40).startswith(b"version https://git-lfs"):
            raise ConvertError(f"{path.name} is a Git LFS placeholder; run 'git lfs pull' in its repository to get the real file")
    return path


# ---------------- CAD shapes ----------------

def load_shape(path: str | Path, scale: float = 1.0):
    """A cadquery Shape for a CAD, part or mesh file (meshes become solids made of their triangles)."""
    import cadquery as cq

    k = kind_of(path)
    path = _check(Path(path))
    ext = path.suffix.lower()
    if k == "mesh":
        from .urdf import M, AssemblyError, mesh_solid

        try:
            shape, _ = mesh_solid(path, [scale / M] * 3)  # mesh_solid scales URDF metres to mm itself
        except AssemblyError as e:
            raise ConvertError(str(e)) from e
        return shape
    if k == "part":
        from .project import Project

        try:
            p = Project.load(path)
        except Exception as e:  # noqa: BLE001 - any parse error is the file's problem
            raise ConvertError(f"could not open {path.name}: {e}") from e
        if not p.result:
            raise ConvertError(f"{path.name} has no solid to convert")
        shape = p.result.shape.val()
    elif k == "assembly":
        from .assembly import AssemblyProject

        a = AssemblyProject.load(str(path))
        shapes = list(a.world_shapes().values())
        if not shapes:
            raise ConvertError(f"{path.name} has no components")
        shape = cq.Compound.makeCompound(shapes)
    elif ext in (".step", ".stp"):
        try:
            wp = cq.importers.importStep(str(path))
        except Exception as e:  # noqa: BLE001
            raise ConvertError(f"could not read STEP {path.name}: {e}") from e
        vals = [v for v in wp.vals() if isinstance(v, cq.Shape)]
        if not vals:
            raise ConvertError(f"{path.name} contains no geometry")
        shape = vals[0] if len(vals) == 1 else cq.Compound.makeCompound(vals)
    elif ext in (".iges", ".igs"):
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.IGESControl import IGESControl_Reader

        rd = IGESControl_Reader()
        if rd.ReadFile(str(path)) != IFSelect_RetDone:
            raise ConvertError(f"could not read IGES {path.name}")
        rd.TransferRoots()
        shape = cq.Shape.cast(rd.OneShape())
    else:
        try:
            shape = cq.Shape.importBrep(str(path))
        except Exception as e:  # noqa: BLE001
            raise ConvertError(f"could not read BREP {path.name}: {e}") from e
    if scale != 1.0:
        shape = shape.scale(scale)
    return shape


def shape_triangles(shape, tolerance: float = 0.02, angular: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    verts, tris = shape.tessellate(tolerance, angular)
    if not tris:
        raise ConvertError("the geometry has no surfaces to triangulate")
    return np.array([v.toTuple() for v in verts], dtype=float), np.array(tris, dtype=np.int64)


# ---------------- triangles ----------------

def load_mesh(path: str | Path, scale: float = 1.0, quality: str = "normal"):
    """Any supported file as one trimesh in millimetres. Meshes are read as they are (no decimation), so the
    triangles a printer gets are the ones in the file."""
    import trimesh

    k = kind_of(path)
    path = _check(Path(path))
    if k == "mesh":
        try:
            m = trimesh.load(str(path), force="mesh")
        except Exception as e:  # noqa: BLE001
            raise ConvertError(f"could not read {path.name}: {e}") from e
        if not isinstance(m, trimesh.Trimesh) or not len(m.faces):
            raise ConvertError(f"{path.name} has no triangles")
        if path.suffix.lower() == ".dae":
            import re

            u = re.search(r'<unit[^>]*meter="([0-9.eE+-]+)"', path.read_text(errors="ignore")[:4000])
            scale *= float(u.group(1)) * 1000 if u else 1.0
        if scale != 1.0:
            m.apply_scale(scale)
        return m
    if k == "assembly":
        return _assembly_mesh(path, scale, quality)
    tol, ang = QUALITY.get(quality, QUALITY["normal"])
    V, F = shape_triangles(load_shape(path, scale), tol, ang)
    m = trimesh.Trimesh(V, F, process=True)
    return m


def _assembly_mesh(path: Path, scale: float, quality: str):
    """All components in their assembly positions; mesh components keep their original triangles."""
    import trimesh

    from .assembly import AssemblyProject, location, resolve_path
    from .urdf import display_mesh_name

    a = AssemblyProject.load(str(path))
    tol, ang = QUALITY.get(quality, QUALITY["normal"])
    parts, cache = [], {}
    for c, shape in zip(a.spec.components, a.shapes().values()):
        src = resolve_path(str(path), c.file)
        key = str(src)
        if key not in cache:
            npz = src.with_name(display_mesh_name(src.name))
            if src.suffix.lower() == ".brep" and npz.exists():
                d = np.load(npz)
                cache[key] = (d["vertices"].astype(float), d["faces"].astype(np.int64))
            else:
                cache[key] = shape_triangles(shape, tol, ang)
        V, F = cache[key]
        T = _matrix(location(c))
        parts.append(trimesh.Trimesh(V @ T[:3, :3].T + T[:3, 3], F, process=False))
    if not parts:
        raise ConvertError(f"{path.name} has no components")
    m = trimesh.util.concatenate(parts)
    if scale != 1.0:
        m.apply_scale(scale)
    return m


def _matrix(loc) -> np.ndarray:
    t = loc.wrapped.Transformation()
    return np.array([[t.Value(i, j) for j in range(1, 5)] for i in range(1, 4)] + [[0, 0, 0, 1]], dtype=float)


def mesh_report(m) -> dict[str, Any]:
    lo, hi = m.bounds
    out: dict[str, Any] = {"triangles": int(len(m.faces)), "size_mm": [round(float(x), 3) for x in hi - lo],
                           "watertight": bool(m.is_watertight)}
    if m.is_watertight:
        out["volume_mm3"] = round(abs(float(m.volume)), 3)
    return out


def to_stl(src: str | Path, dst: str | Path, units: str | float | None = None, quality: str = "normal",
           binary: bool = True) -> dict[str, Any]:
    """Convert one file to STL; returns what was written."""
    src, dst = Path(src).expanduser(), Path(dst).expanduser()
    if quality not in QUALITY:
        raise ConvertError(f"quality must be one of {', '.join(QUALITY)}")
    m = load_mesh(src, scale_of(units), quality)
    if dst.suffix.lower() != ".stl":
        dst = dst.with_suffix(".stl")
    dst.parent.mkdir(parents=True, exist_ok=True)
    m.export(str(dst), file_type="stl" if binary else "stl_ascii")
    rep = mesh_report(m)
    note = "" if rep["watertight"] else " (the mesh is not closed: slicers may need to repair it)"
    return {"ok": True, "path": str(dst), "source": str(src), **rep,
            "message": f"wrote {dst.name}: {rep['triangles']} triangles, "
                       f"{' × '.join(f'{x:g}' for x in rep['size_mm'])} mm{note}"}


def to_stl_many(sources: list[str], out_dir: str | Path, **kw: Any) -> dict[str, Any]:
    out_dir = Path(out_dir).expanduser()
    done, failed = [], []
    for s in sources:
        try:
            done.append(to_stl(s, out_dir / (Path(s).stem + ".stl"), **kw))
        except ConvertError as e:
            failed.append({"source": s, "error": str(e)})
    return {"ok": not failed, "converted": done, "failed": failed,
            "message": f"converted {len(done)} of {len(sources)} files to {out_dir}" + (f"; {len(failed)} failed" if failed else "")}


# ---------------- arranging meshes on a print bed ----------------

def lay_flat(m) -> np.ndarray:
    """Rotation (3x3) that puts the mesh on its largest flat face, as a slicer's 'lay flat' does: convex-hull facets
    are grouped into flat faces; the largest one with the centre of mass above it wins (lower centre on ties)."""
    hull = m.convex_hull
    com = m.center_mass if m.is_watertight else m.centroid
    normals, areas = hull.face_normals, hull.area_faces
    mn, ma = m.face_normals, m.area_faces
    used = np.zeros(len(areas), bool)
    best, best_score = np.eye(3), -1.0
    for i in np.argsort(-areas)[:400]:
        if used[i]:
            continue
        group = ~used & (normals @ normals[i] > 0.9998)  # within about 1 degree
        used |= group
        n = (normals[group] * areas[group, None]).sum(0)
        n /= np.linalg.norm(n)
        R = rotation_to(n, np.array([0.0, 0.0, -1.0]))
        h = (hull.vertices - com) @ R.T
        z = h[:, 2].min()
        foot = h[h[:, 2] - z < 0.05][:, :2]
        stable = len(foot) >= 3 and _inside_hull2d(np.zeros(2), foot)
        # Real contact: the part's own faces lying in that plane (a hull facet can bridge a gap, as across an L).
        on = (mn @ n > 0.9998)
        if on.any():
            vz = ((m.vertices - com) @ R.T)[:, 2]
            on &= (vz[m.faces] - z < 0.05).all(1)
        contact = float(ma[on].sum())
        score = round(contact * (1.0 if stable else 0.25), 3) + 1e-6 / max(1e-6, -z)
        if score > best_score:
            best, best_score = R, score
    return best


def _inside_hull2d(p: np.ndarray, pts: np.ndarray) -> bool:
    from scipy.spatial import ConvexHull, QhullError

    try:
        h = ConvexHull(pts)
    except (QhullError, ValueError):
        return False
    return bool(np.all(h.equations[:, :2] @ p + h.equations[:, 2] <= 1e-6))


def rotation_to(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Smallest rotation taking unit vector a onto unit vector b."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if c < -0.999999:  # opposite: turn 180° about any perpendicular axis
        axis = np.cross(a, [1.0, 0, 0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0, 1.0, 0])
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


def placed(m, matrix: list[float] | np.ndarray, x: float, y: float):
    """Copy of the mesh with a 3x3 (or 4x4) matrix applied, centred at (x, y) and resting on z = 0."""
    M = np.asarray(matrix, dtype=float)
    M = M.reshape(4, 4)[:3, :3] if M.size == 16 else M.reshape(3, 3)
    out = m.copy()
    V = out.vertices @ M.T
    if np.linalg.det(M) < 0:  # a mirror flips the triangle winding
        out.faces = out.faces[:, ::-1]
    lo, hi = V.min(0), V.max(0)
    V += np.array([x - (lo[0] + hi[0]) / 2, y - (lo[1] + hi[1]) / 2, -lo[2]])
    out.vertices = V
    return out
