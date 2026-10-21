# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parts package: turn an assembly of meshes (e.g. a robot imported from URDF) into a list of distinct parts with
manufacturing files.

1. Every component's mesh is split into vertex-connected bodies; each body is moved to a canonical frame (centred,
   principal axes of its surface; the oriented bounding box gives its size).
2. Identical bodies are grouped across the whole assembly. Candidates must agree in area, volume and spread; the match
   is confirmed by surface distance under the 4 proper rotations of the frame (refined by ICP). A body that matches only
   under an improper rotation is the mirror image (left/right hand) and gets its own part number, marked "mirror of".
3. Each part is classified with the evidence measured: sheet metal (rebuilt as a parametric sheet-metal part, or
   uniform thickness), purchased (repeated many times and not sheet metal, fastener-sized, or open/repeated meshes),
   custom (machined, printed or moulded; review). An `overrides` dict corrects any of them.
4. Output folder: BOM.csv, README.md and per part part.aicad (only when rebuilt), part.step (or part_mesh.step),
   part.stl, drawing.pdf, preview.png and, for unfolded sheet metal, flat.dxf.
Dimensions recovered from meshes are only as good as the meshes: every file says so.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

CATEGORIES = ("sheet_metal", "purchased", "custom")
FOLDERS = {"purchased": "01_Purchased", "sheet_metal": "02_Sheet_Metal", "custom": "03_Custom"}
PURCHASED_MIN_QTY = 4
FASTENER_MAX_MM = 12.0


@dataclass
class Body:
    mesh: Any  # trimesh in its canonical frame
    used_in: list[str]  # component / link labels
    source: str
    area: float
    volume: float | None
    spread: np.ndarray  # sqrt of the principal moments, descending
    size: tuple[float, float, float]  # sorted oriented-box extents
    closed: bool
    _cache: dict[str, Any] = field(default_factory=dict)


@dataclass
class Group:
    bodies: list[Body]
    mirror_of: "Group | None" = None
    pn: str = ""
    name: str = ""
    category: str = "custom"
    confidence: float = 0.0
    reason: str = ""
    status: str = ""
    rebuild: dict[str, Any] | None = None
    thickness: float | None = None
    files: list[str] = field(default_factory=list)

    @property
    def rep(self) -> Body:
        return self.bodies[0]


# ---------------- bodies ----------------

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "part"


def canonical(mesh) -> tuple[Any, np.ndarray, np.ndarray]:
    """Mesh moved to its principal frame: (moved mesh, principal moments descending, centre). Axes ordered by
    decreasing spread."""
    tri = mesh.triangles
    a = mesh.area_faces
    w = a / a.sum()
    cen = (tri.mean(1) * w[:, None]).sum(0)
    # second moment of the surface: exact for triangles (vertex + centroid terms)
    d = tri - cen
    s = d.sum(1)
    M = (np.einsum("n,nk,nl->kl", w, s, s) + np.einsum("n,nik,nil->kl", w, d, d)) / 12
    ev, vec = np.linalg.eigh(M)
    order = np.argsort(ev)[::-1]
    R = vec[:, order].T
    if np.linalg.det(R) < 0:
        R[2] *= -1
    m = mesh.copy()
    m.vertices = (np.asarray(mesh.vertices) - cen) @ R.T
    return m, ev[order], cen


def make_body(mesh, used_in: str, source: str) -> Body:
    m, ev, _ = canonical(mesh)
    try:
        size = tuple(sorted(float(x) for x in m.bounding_box_oriented.primitive.extents))
    except Exception:  # flat or degenerate body
        size = tuple(sorted(float(x) for x in m.extents))
    closed = bool(m.is_watertight)
    return Body(m, [used_in], source, float(m.area), abs(float(m.volume)) if closed else None, np.sqrt(np.maximum(ev, 0)),
                size, closed)


def split_bodies(V: np.ndarray, F: np.ndarray) -> list:
    import trimesh

    m = trimesh.Trimesh(np.asarray(V, float), np.asarray(F, np.int64), process=True)
    m.update_faces(m.nondegenerate_faces())
    out = []
    for b in m.split(only_watertight=False):
        if len(b.faces) >= 4 and b.area > 1e-3:
            if b.is_watertight:
                trimesh.repair.fix_normals(b)
            out.append(b)
    return out


def assembly_meshes(asm_path: str | Path) -> list[tuple[str, str, np.ndarray, np.ndarray]]:
    """(component label, source file, vertices mm, faces) for every component of an .aicadasm, in part coordinates."""
    from .assembly import AssemblySpec, resolve_path
    from .urdf import display_mesh_name

    spec = AssemblySpec.from_dict(json.loads(Path(asm_path).read_text()))
    cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    out = []
    for c in spec.components:
        p = resolve_path(str(asm_path), c.file)
        key = str(p.resolve())
        if key not in cache:
            npz = p.with_name(display_mesh_name(p.name))
            if p.suffix.lower() == ".brep" and npz.exists():
                d = np.load(npz)
                cache[key] = (d["vertices"].astype(float), d["faces"].astype(np.int64))
            else:
                cache[key] = _tessellate_file(p)
        label = c.link or c.name or c.id
        out.append((f"{c.id}" + (f" ({label})" if label != c.id else ""), p.name, *cache[key]))
    return out


def _tessellate_file(p: Path) -> tuple[np.ndarray, np.ndarray]:
    import cadquery as cq

    if p.suffix.lower() == ".brep":
        shape = cq.Shape.importBrep(str(p))
    elif p.suffix.lower() in (".step", ".stp"):
        shape = cq.importers.importStep(str(p)).val()
    else:
        from .project import Project

        pr = Project.load(p)
        shape = pr.result.shape.val()
    v, t = shape.tessellate(0.02, 0.2)
    return np.array([x.toTuple() for x in v]), np.array(t, np.int64)


# ---------------- grouping ----------------

_PROPER = [np.diag(s) for s in ((1, 1, 1), (-1, -1, 1), (-1, 1, -1), (1, -1, -1))]
_IMPROPER = [np.diag(s) for s in ((-1, 1, 1), (1, -1, 1), (1, 1, -1), (-1, -1, -1))]


def _similar(a: Body, b: Body) -> bool:
    if a.closed != b.closed:
        return False
    if abs(a.area - b.area) > 0.02 * max(a.area, b.area) + 0.5:
        return False
    if a.volume is not None and abs(a.volume - b.volume) > 0.02 * max(a.volume, b.volume) + 0.5:
        return False
    return bool(np.all(np.abs(a.spread - b.spread) <= 0.03 * a.spread.max() + 0.1))


NORMAL_W = 2.0  # mm per unit of normal difference in the position+normal distance


def _sagitta(mesh) -> float:
    """Typical chord error of the tessellation of curved regions (0 for a mesh of flat faces)."""
    ang = mesh.face_adjacency_angles
    smooth = (ang > math.radians(2)) & (ang < math.radians(60))
    if not smooth.any():
        return 0.0
    e = mesh.face_adjacency_edges[smooth]
    L = np.linalg.norm(mesh.vertices[e[:, 0]] - mesh.vertices[e[:, 1]], axis=1)
    w = 2 * mesh.area_faces[mesh.face_adjacency[smooth]].max(1) / np.maximum(L, 1e-9)  # face width across the edge
    return float(np.median(w * ang[smooth] / 8))


def _samples(b: Body) -> np.ndarray:
    if "pts" not in b._cache:
        import trimesh

        pts, fi = trimesh.sample.sample_surface(b.mesh, 3000, seed=1)
        b._cache["pts"], b._cache["nrm"] = pts, b.mesh.face_normals[fi]
    return b._cache["pts"]


def _normals(b: Body) -> np.ndarray:
    _samples(b)
    return b._cache["nrm"]


def _surface(b: Body):
    """Finely subdivided triangles + centroid tree, for point-to-surface distances."""
    if "surf" not in b._cache:
        import trimesh
        from scipy.spatial import cKDTree

        diag = float(np.linalg.norm(b.mesh.extents))
        V, F = trimesh.remesh.subdivide_to_size(b.mesh.vertices, b.mesh.faces, max(diag / 30, 0.5), max_iter=6)
        tri = V[F]
        b._cache["surf"] = (tri, cKDTree(tri.mean(1)), cKDTree(_samples(b)))
        dense, fi = trimesh.sample.sample_surface(b.mesh, 12000, seed=2)
        b._cache["tree6"] = cKDTree(np.hstack([dense, NORMAL_W * b.mesh.face_normals[fi]]))
        b._cache["spacing"] = math.sqrt(b.area / 12000)
    return b._cache["surf"]


def _bad_fraction(a: Body, b: Body, R: np.ndarray, limit: float) -> float:
    """Share of a's surface samples (moved by R) with no point of b nearby that faces the same way: a hole or edge
    that moved shows up here even when it stays within half a thickness of b's surface."""
    _surface(b)
    q = np.hstack([_samples(a) @ R.T, NORMAL_W * (_normals(a) @ R.T)])
    d, _ = b._cache["tree6"].query(q)
    return float(np.mean(d > limit + 1.5 * b._cache["spacing"]))


def _dist(points: np.ndarray, b: Body) -> np.ndarray:
    import trimesh

    tri, tree, _ = _surface(b)
    k = min(8, len(tri))
    _, idx = tree.query(points, k=k)
    idx = idx.reshape(len(points), k)
    cp = trimesh.triangles.closest_point(tri[idx.ravel()], np.repeat(points, k, axis=0))
    d = np.linalg.norm(cp - np.repeat(points, k, axis=0), axis=1).reshape(len(points), k)
    return d.min(1)


def _icp(P: np.ndarray, b: Body, R: np.ndarray, iters: int = 12) -> np.ndarray:
    """Refine a rotation/reflection R (applied to P) toward body b; keeps the sign of det(R)."""
    _, _, tree = _surface(b)
    Q = _samples(b)
    T = np.eye(3)
    for _ in range(iters):
        X = P @ (T @ R).T
        _, j = tree.query(X)
        Y = Q[j]
        H = (X - X.mean(0)).T @ (Y - Y.mean(0))
        U, _, Vt = np.linalg.svd(H)
        D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
        T = (Vt.T @ D @ U.T) @ T
    return T @ R


def match(a: Body, b: Body) -> tuple[str | None, float]:
    """("same" | "mirror" | None, p95 surface distance in mm) between two bodies in their canonical frames."""
    # coarse meshes of the same part differ by their tessellation: allow a few chord errors of their curved regions
    for x in (a, b):
        if "sag" not in x._cache:
            x._cache["sag"] = _sagitta(x.mesh)
    tol = max(0.15, 0.004 * float(np.linalg.norm(b.mesh.extents)), 3 * max(a._cache["sag"], b._cache["sag"]))
    P = _samples(a)
    best: dict[str, float] = {}
    for kind, sets in (("same", _PROPER), ("mirror", _IMPROPER)):
        cands = list(sets)
        # nearly equal spreads: the principal axes are not unique; also try turns about the third axis
        for ax in range(3):
            i, j = [k for k in range(3) if k != ax]
            if abs(a.spread[i] - a.spread[j]) < 0.05 * a.spread[i] + 0.05:
                for ang in np.radians(np.arange(30, 360, 30)):
                    Rz = np.eye(3)
                    Rz[i, i] = Rz[j, j] = math.cos(ang)
                    Rz[i, j], Rz[j, i] = -math.sin(ang), math.sin(ang)
                    cands += [Rz @ S for S in sets]
        scores = []
        _, _, tree = _surface(b)
        for S in cands:
            d, _ = tree.query(P @ S.T)
            scores.append((float(np.mean(d)), S))
        scores.sort(key=lambda x: x[0])
        p95 = 1e9
        for _, S in scores[:3]:
            R = _icp(P, b, S)
            d1 = _dist(P @ R.T, b)
            d2 = _dist(_samples(b) @ R, a)  # the other way (R is orthogonal: inverse = transpose)
            p = float(max(np.percentile(d1, 95), np.percentile(d2, 95)))
            if p <= tol:  # overall shape agrees; local features (holes, notches) must agree too
                bad = max(_bad_fraction(a, b, R, 3 * tol + 0.3), _bad_fraction(b, a, R.T, 3 * tol + 0.3))
                if bad > 0.003:
                    p = max(p, tol * 1.01)
            p95 = min(p95, p)
            if p95 <= tol:
                break
        best[kind] = p95
        if kind == "same" and p95 <= tol:
            return "same", p95
    if best["mirror"] <= tol:
        return "mirror", best["mirror"]
    return None, min(best.values())


def group_bodies(bodies: list[Body], progress: Callable[[str], None] | None = None) -> list[Group]:
    groups: list[Group] = []
    for n, b in enumerate(sorted(bodies, key=lambda b: -b.area)):
        if progress and n % 25 == 0:
            progress(f"grouping body {n + 1} of {len(bodies)}")
        placed = False
        mirror_cand = None
        for g in groups:
            if not _similar(g.rep, b):
                continue
            kind, _ = match(b, g.rep)
            if kind == "same":
                g.bodies.append(b)
                placed = True
                break
            if kind == "mirror" and mirror_cand is None:
                mirror_cand = g
        if not placed:
            groups.append(Group([b], mirror_of=mirror_cand))
    for g in groups:  # one group per mirror pair points at the other
        if g.mirror_of is not None and g.mirror_of.mirror_of is g:
            g.mirror_of = None
    return groups


# ---------------- classification ----------------

def classify(g: Group, purchased_min_qty: int = PURCHASED_MIN_QTY, rebuild: bool = True) -> None:
    from . import reverse

    b = g.rep
    qty = len(g.bodies)
    big = max(b.size)
    if b.closed and rebuild:
        try:
            rb = reverse.rebuild(b.mesh, g.name or "part")
        except Exception as e:  # never let one part stop the package
            rb = {"ok": False, "reason": f"not convertible: analysis failed ({type(e).__name__}: {e})", "report": {}}
        g.rebuild = rb
        if rb["ok"]:
            rep = rb["report"]
            if rb["kind"] == "sheet_metal":
                g.category, g.thickness = "sheet_metal", rep["thickness"]
                bends = rep["bends"]
                g.confidence = 0.9 if rb["mismatch_pct"] < 2 else 0.75
                g.reason = (f"rebuilt as sheet metal t={rep['thickness']:g} mm, {rep['flanges']} flange(s), {len(bends)} bend(s), "
                            f"{len(rep['holes'])} hole(s); differs from the mesh by {rb['mismatch_pct']}%")
                return
            shape = f"{rep['steps']}-step extrusion ({len(rep['holes'])} hole(s)), differs from the mesh by {rb['mismatch_pct']}%"
            if qty >= purchased_min_qty or (big <= FASTENER_MAX_MM and (b.volume or 0) < 300):
                g.category, g.confidence = "purchased", 0.5
                g.reason = (f"used {qty} times" if qty >= purchased_min_qty else f"small ({big:.1f} mm)") + \
                    f": likely a standard part (spacer, pin, fastener); rebuilt as a {shape}"
                return
            g.category, g.confidence = "custom", 0.7
            g.reason = f"rebuilt as a {shape}; likely machined or cut from plate"
            return
        rep = rb.get("report") or {}
        info_t = rep.get("thickness")
        if info_t and rep.get("flat_fraction", 0) >= 0.6:
            g.category, g.thickness, g.confidence = "sheet_metal", info_t, 0.5
            g.reason = (f"{100 * rep['flat_fraction']:.0f}% of the surface is flat sheet of thickness {info_t:g} mm, "
                        f"but it could not be rebuilt ({rb['reason'][17:]})")
            return
    if not b.closed and qty >= 2:
        g.category, g.confidence = "purchased", 0.6
        g.reason = f"open (surface-only) mesh used {qty} times: typical of a supplier model of a bought component"
        return
    if qty >= purchased_min_qty:
        g.category, g.confidence = "purchased", 0.6
        g.reason = f"used {qty} times and not sheet metal: likely a standard bought component"
        return
    if big <= FASTENER_MAX_MM and (b.volume or 0) < 300:
        g.category, g.confidence = "purchased", 0.5
        g.reason = f"small ({big:.1f} mm): likely a fastener or other standard part"
        return
    g.category, g.confidence = "custom", 0.4
    why = (g.rebuild or {}).get("reason") or ("open mesh: no volume" if not b.closed else "")
    g.reason = "machined, printed or moulded part; review" + (f" ({why})" if why else "")


def _apply_override(g: Group, ov: Any) -> None:
    if isinstance(ov, str):
        ov = {"category": ov} if ov in CATEGORIES else {"name": ov}
    if not isinstance(ov, dict):
        raise ValueError(f"override for {g.pn} must be a category ({', '.join(CATEGORIES)}), a name or {{category, name}}")
    if "category" in ov:
        if ov["category"] not in CATEGORIES:
            raise ValueError(f"override for {g.pn}: category must be one of {', '.join(CATEGORIES)}")
        g.category, g.confidence = ov["category"], 1.0
        g.reason = f"set by the user (was: {g.reason})"
    if ov.get("name"):
        g.name = str(ov["name"])


# ---------------- output ----------------

def _mesh_shape(mesh):
    import cadquery as cq

    from .urdf import _faces_to_shape

    s = _faces_to_shape(mesh.vertices, mesh.faces, bool(mesh.is_watertight))
    if mesh.is_watertight and s.Volume() < 0:
        s = cq.Shape.cast(s.wrapped.Reversed())
    return s


def _drawing(g: Group, shape, spec, result, path: Path, notes: list[str], from_mesh: bool) -> str | None:
    from .drawing import make_drawing, to_pdf

    sheet = "A4" if max(g.rep.size) <= 120 else "A3"
    try:
        d = make_drawing(result, spec, sheet=sheet, extra_notes=notes,
                         source_note=("Dimensions recovered from a mesh: verify before manufacture." if from_mesh else
                                      "Rebuilt from a mesh as a parametric part: verify before manufacture."))
        path.write_bytes(to_pdf(d["svg"]))
        return None
    except Exception as e:  # report, keep going
        return f"drawing failed ({type(e).__name__}: {e})"


def _write_part(g: Group, folder: Path, drawings: bool) -> list[str]:
    import cadquery as cq
    from OCP.BRepLib import BRepLib

    from . import reverse
    from .builder import BuildResult
    from .render import render_sheet
    from .spec import PartSpec

    folder.mkdir(parents=True, exist_ok=True)
    b, problems, notes = g.rep, [], []
    b.mesh.export(str(folder / "part.stl"))
    rb = g.rebuild if g.rebuild and g.rebuild.get("ok") else None
    if g.thickness:
        notes.append(f"Measured sheet thickness: {g.thickness:g} mm.")
    if rb:
        spec = dict(rb["spec"], name=g.name)
        (folder / "part.aicad").write_text(json.dumps(spec, indent=2))
        result = rb["project"].result
        shape = result.shape.val()
        cq.exporters.export(cq.Workplane().add(shape), str(folder / "part.step"))
        holes = rb["report"].get("holes", [])
        if holes:
            sizes: dict[float, int] = {}
            for h in holes:
                sizes[round(h["d"], 1)] = sizes.get(round(h["d"], 1), 0) + 1
            notes.append("Holes: " + ", ".join(f"{n}× Ø{d:g}" for d, n in sorted(sizes.items())) + " (measured).")
        if rb["kind"] == "sheet_metal":
            br = rb["report"]["bends"]
            if br:
                notes.append("Bends: " + ", ".join(f"{x['angle']:.0f}° R{x['inner_radius']:g}" for x in br) + " (inner radius, measured).")
            try:
                fp = reverse.flat_pattern(rb)
                reverse.flat_dxf(fp, str(folder / "flat.dxf"))
                notes.append(f"Flat pattern {fp['size'][0]:.1f} × {fp['size'][1]:.1f} mm (K = {fp['k_factor']:g}) in flat.dxf.")
            except ValueError as e:
                problems.append(f"no flat pattern: {e}")
        draw_spec = PartSpec.from_dict(spec)
        from_mesh = False
    else:
        shape = _mesh_shape(b.mesh)
        cq.exporters.export(cq.Workplane().add(shape), str(folder / "part_mesh.step"))
        BRepLib.EncodeRegularity_s(shape.wrapped, math.radians(8))
        bb = shape.BoundingBox()
        result = BuildResult(shape=cq.Workplane().add(shape), volume_mm3=b.volume or 0.0, bbox_mm=(bb.xlen, bb.ylen, bb.zlen))
        draw_spec = PartSpec(name=g.name)
        from_mesh = True
        if not b.closed:
            notes.append("Surface model only (the mesh is not closed): no volume.")
    notes.append(f"Part {g.pn}: {g.category.replace('_', ' ')} ({g.reason[:150]}).")
    if drawings:
        err = _drawing(g, shape, draw_spec, result, folder / "drawing.pdf", notes, from_mesh)
        if err:
            problems.append(err)
    try:
        (folder / "preview.png").write_bytes(render_sheet([{"vertices": b.mesh.vertices.tolist(), "triangles": b.mesh.faces.tolist(),
                                                            "color": "#9aa7b4"}], 400, f"{g.pn} {g.name}"))
    except Exception as e:  # preview is optional
        problems.append(f"preview failed ({e})")
    return problems


def _fmt_size(s) -> str:
    return " x ".join(f"{v:.1f}" for v in sorted(s, reverse=True))


def make_package(source: str | Path | list[tuple[str, Any]], out_dir: str | Path | None = None, name: str | None = None,
                 overrides: dict[str, Any] | None = None, licence_note: str = "", purchased_min_qty: int = PURCHASED_MIN_QTY,
                 drawings: bool = True, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Build the parts package for an .aicadasm file or a list of (label, trimesh) meshes (mm).
    Returns {"folder", "parts": [BOM rows], "summary": {...}}."""
    say = progress or (lambda _m: None)
    bodies: list[Body] = []
    if isinstance(source, (str, Path)):
        asm = Path(source)
        name = name or asm.stem
        items = assembly_meshes(asm)
        base = Path(out_dir) if out_dir else asm.parent
    else:
        name = name or "parts"
        items = [(lbl, lbl, np.asarray(m.vertices), np.asarray(m.faces)) for lbl, m in source]
        base = Path(out_dir) if out_dir else Path.cwd()
    say(f"splitting {len(items)} components into bodies")
    split_cache: dict[int, list] = {}
    for label, src, V, F in items:
        key = id(V)
        if key not in split_cache:
            split_cache[key] = [make_body(m, label, src) for m in split_bodies(V, F)]
        for b in split_cache[key]:  # each use of a file counts once per body
            bodies.append(Body(b.mesh, [label], src, b.area, b.volume, b.spread, b.size, b.closed, b._cache))
    say(f"grouping {len(bodies)} bodies")
    groups = group_bodies(bodies, say)
    # stable part numbers: by size, before classification (so overrides refer to fixed numbers)
    groups.sort(key=lambda g: (-(g.rep.volume or 0), -g.rep.area))
    width = max(3, len(str(len(groups))))
    for i, g in enumerate(groups, 1):
        g.pn = f"PN-{i:0{width}d}"
        first = g.rep.used_in[0].split(" (")[0]
        g.name = f"{_safe(first)[:24]} " + "x".join(f"{v:.0f}" for v in sorted(g.rep.size, reverse=True))
    for n, g in enumerate(groups, 1):
        say(f"recognising part {n} of {len(groups)}")
        classify(g, purchased_min_qty)
    overrides = overrides or {}
    unknown = [k for k in overrides if k not in {g.pn for g in groups}]
    if unknown:
        raise ValueError(f"overrides name unknown part numbers: {', '.join(unknown)}")
    for g in groups:
        if g.pn in overrides:
            _apply_override(g, overrides[g.pn])
    folder = base / f"{_safe(name)}_parts"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, g in enumerate(groups, 1):
        say(f"writing part {n} of {len(groups)}")
        rel = Path(FOLDERS[g.category]) / f"{g.pn}_{_safe(g.name)}"
        problems = _write_part(g, folder / rel, drawings)
        rb = g.rebuild or {}
        if rb.get("ok"):
            status = f"parametric ({rb['kind'].replace('_', ' ')}), mismatch {rb['mismatch_pct']}%"
            if rb["kind"] == "sheet_metal":
                status += "; flat.dxf" if (folder / rel / "flat.dxf").exists() else ""
        else:
            status = "mesh only" + (f": {rb['reason'][17:]}" if rb.get("reason") else (": open surface mesh" if not g.rep.closed else ""))
        if problems:
            status += "; " + "; ".join(problems)
        g.status = status
        used: dict[str, int] = {}
        for b in g.bodies:
            for u in b.used_in:
                used[u] = used.get(u, 0) + 1
        rows.append({"part_number": g.pn, "name": g.name, "category": g.category, "qty": len(g.bodies),
                     "used_in": "; ".join(f"{k}" + (f" x{v}" if v > 1 else "") for k, v in used.items()),
                     "size_mm": _fmt_size(g.rep.size), "thickness_mm": g.thickness or "",
                     "volume_mm3": round(g.rep.volume, 1) if g.rep.volume else "", "status": status,
                     "confidence": round(g.confidence, 2), "reason": g.reason,
                     "mirror_of": g.mirror_of.pn if g.mirror_of else "", "folder": str(rel)})
    with open(folder / "BOM.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["part_number"])
        w.writeheader()
        w.writerows(rows)
    summary = {c: sum(1 for g in groups if g.category == c) for c in CATEGORIES}
    summary.update(bodies=len(bodies), parts=len(groups), parametric=sum(1 for g in groups if (g.rebuild or {}).get("ok")),
                   mirrored=sum(1 for g in groups if g.mirror_of))
    (folder / "README.md").write_text(_readme(name, summary, licence_note, purchased_min_qty))
    return {"folder": str(folder), "parts": rows, "summary": summary}


def _readme(name: str, s: dict[str, Any], licence: str, nmin: int) -> str:
    return f"""# {name}: parts package

Generated by AI CAD from the assembly's meshes. **Dimensions recovered from meshes are only as accurate as the meshes:
verify every dimension before manufacture.**

{s['parts']} distinct parts from {s['bodies']} bodies: {s['sheet_metal']} sheet metal, {s['purchased']} purchased,
{s['custom']} custom; {s['parametric']} rebuilt as parametric parts; {s['mirrored']} mirror-image (left/right) parts.

## Files
- `BOM.csv`: part number, name, category, quantity, where used, size, thickness, volume, status, confidence, reason.
- `01_Purchased/`, `02_Sheet_Metal/`, `03_Custom/`: one folder per part with
  - `part.aicad`: the parametric part (only when it was rebuilt; open it in AI CAD to edit),
  - `part.step` (from the parametric part) or `part_mesh.step` (the mesh as a solid, no feature history),
  - `part.stl`, `drawing.pdf`, `preview.png`,
  - `flat.dxf` for sheet-metal parts that could be unfolded (bend allowance BA = angle × (R + K·T), K = 0.44).

## Method
1. Each mesh is split into vertex-connected bodies, moved to its principal frame; size is the oriented bounding box.
2. Identical bodies are grouped: area, volume and spread must agree, then the surfaces must match within
   max(0.15 mm, 0.4% of the size) under a proper rotation. A body matching only its mirror image is a separate part
   ("mirror_of").
3. Sheet metal: flat face pairs one thickness apart become flanges, bends are fitted to the corner vertices, and the
   rebuilt part is accepted only if it differs from the mesh by less than 5% of its volume (measured by ray casting).
   Other closed parts are tried as extrusions. Anything else stays a mesh, and the BOM says why.
4. Purchased: used {nmin} or more times and not sheet metal, open (surface-only) meshes used more than once, or
   smaller than {FASTENER_MAX_MM:g} mm. Custom: everything else (machined, printed or moulded; review).
   Confidence is a rough score for how strong that evidence is; correct the category with overrides.

## Limits
- Materials, tolerances and surface finish are not in meshes: they are NOT SPECIFIED on the drawings.
- Bought components (servos, bearings, fasteners) are listed by their geometry only; identify them before ordering.
- Formed features (louvres, embosses), hems and bends over 6× thickness are not rebuilt.

## Licence
{licence or "The source model's licence applies to these files; check it before sharing or manufacturing."}
"""
