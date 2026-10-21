# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reverse engineering: rebuild a closed triangle mesh as a parametric part, or say why it can't be done.

Two kinds of parts are recognised; anything else is reported as not convertible, never approximated:

Sheet metal (flat plates, bent brackets):
1. planes: every triangle on the same plane is one region, connected or not (meshes split plates into patches);
2. flanges: plane pairs facing away from each other one thickness apart, with overlapping outlines. The outline is
   the union of both faces (one face is often cut back near a bend). Narrow strips at an angle to every real flange
   are facets of a low-poly bend and are dropped;
3. thickness: the area-weighted median distance of those pairs (one variable, `thickness`);
4. bends: for two non-parallel flanges, the inner radius is fitted to the mesh vertices in the corner (inner and
   outer cylinders, r and r + t), the bend runs where both flanges reach the bend line. A sharp outer corner (a common
   modelling shortcut) is recognised and rebuilt as overlapping flanges;
5. each flange outline is trimmed or extended exactly to its bend tangent line (plus 0.02 mm overlap), so flanges and
   bends always form one body; round holes become circles with diameter variables, slots become slots.
Prismatic parts (2.5D): every face is parallel or perpendicular to one axis. The part is rebuilt as stacked
extrusions of its cross-sections, with holes.

Every rebuilt part is built and compared with the mesh (symmetric difference / mesh volume); it is accepted only
below ACCEPT_MISMATCH. Flat patterns unfold the flange/bend tree with bend allowance BA = angle * (R + K * T).
Not handled: formed features (louvres, embosses, ribs), curved flanges, bends with R > 6 T, hems, parts with drafts or
fillets in more than one direction.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

ACCEPT_MISMATCH = 0.05  # max (mesh XOR rebuilt) / mesh volume
MIN_T, MAX_T = 0.3, 8.0
K_FACTOR = 0.44


# ---------------- small geometry helpers ----------------

def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _frame(n: np.ndarray, hint: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Same convention as builder._frame: x from the hint (or global X/Y) projected, y = n x x."""
    x = hint if hint is not None else (np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0]))
    x = x - n * (x @ n)
    x = x / np.linalg.norm(x)
    return x, np.cross(n, x)


def _r(v: float, k: int = 4) -> float:
    return round(float(v), k) + 0.0


def _p(v: float) -> float:
    """Coordinates of faces that must meet exactly (flange ends, bend ends) keep full precision."""
    return round(float(v), 10) + 0.0


def _clean(shape):
    """Valid polygon(s) without slivers or repeated points."""
    from shapely import make_valid

    if not shape.is_valid:
        shape = make_valid(shape)
    shape = shape.buffer(1e-6, join_style=2).buffer(-1e-6, join_style=2)
    return shape.simplify(1e-5, preserve_topology=True)


def _drop_slivers(poly, w: float = 0.05):
    """Fill interior rings that are cracks between mesh patches (thinner than 2w), not real holes."""
    from shapely.geometry import Polygon

    keep = [r for r in poly.interiors if not Polygon(r).buffer(-w, join_style=2).is_empty]
    return Polygon(poly.exterior, keep)


def _geoms(shape) -> list:
    if shape is None or shape.is_empty:
        return []
    if shape.geom_type == "Polygon":
        return [shape]
    return [g for g in getattr(shape, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    o = np.argsort(values)
    cw = np.cumsum(weights[o])
    return float(values[o][np.searchsorted(cw, cw[-1] / 2)])


def _fit_circle(pts: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Least-squares circle (centre, radius, max radial error)."""
    A = np.c_[2 * pts, np.ones(len(pts))]
    b = (pts ** 2).sum(1)
    c, *_ = np.linalg.lstsq(A, b, rcond=None)
    centre = c[:2]
    r = math.sqrt(max(c[2] + centre @ centre, 0))
    err = float(np.max(np.abs(np.linalg.norm(pts - centre, axis=1) - r))) if len(pts) else 1e9
    return centre, r, err


def _fit_slot(pts: np.ndarray) -> dict[str, float] | None:
    """A straight slot (two half circles joined by straight sides) through the ring points, or None."""
    from shapely.geometry import MultiPoint, Polygon

    ring = Polygon(pts)
    rect = MultiPoint(pts).minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    if np.linalg.norm(e1) < np.linalg.norm(e2):
        e1, e2 = e2, e1
    L, w = float(np.linalg.norm(e1)), float(np.linalg.norm(e2))
    if w < 0.3 or L < 1.15 * w:
        return None
    ax = e1 / L
    centre = c.mean(0)
    along, across = (pts - centre) @ ax, (pts - centre) @ np.array([-ax[1], ax[0]])
    # half length of the straight part from the points on the round ends
    ends = np.abs(along) > (L / 2 - w / 2) + 1e-6
    if ends.sum() < 2:
        return None
    h = float(np.median(np.abs(along[ends]) - np.sqrt(np.maximum(0, (w / 2) ** 2 - across[ends] ** 2))))
    h = max(h, 0.0)
    seg = np.clip(along, -h, h)
    err = np.abs(np.hypot(along - seg, across) - w / 2)
    area = w * 2 * h + math.pi * w * w / 4
    if err.max() > 0.06 * w + 0.03 or not 0.88 <= ring.area / area <= 1.03:
        return None
    return {"length": 2 * h + w, "w": w, "x": float(centre[0]), "y": float(centre[1]), "angle": math.degrees(math.atan2(ax[1], ax[0]))}


def _circle_like(pts: np.ndarray) -> tuple[np.ndarray, float] | None:
    from shapely.geometry import Polygon

    if len(pts) < 5:
        return None
    c, r, err = _fit_circle(pts)
    rms = float(np.sqrt(np.mean((np.linalg.norm(pts - c, axis=1) - r) ** 2)))
    # vertices of a tessellated hole lie on its circle; coarse or decimated meshes scatter them (up to 15% rms)
    if r > 0.05 and rms <= 0.15 * r + 0.02 and err <= 0.4 * r + 0.02 and 0.72 <= Polygon(pts).area / (math.pi * r * r) <= 1.03:
        return c, r
    return None


def _cutout_shape(pts: np.ndarray, hole_var, face_rings: list[np.ndarray] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Spec shape (mode subtract) for an inner ring: circle, slot or polygon; plus a hole record for the report.
    face_rings: the same hole as seen on each face (its vertices are true points of the hole wall); the outline's
    ring can be the overlap of two slightly different polygons, so a circle is fitted to the face rings first."""
    from shapely.geometry import Point, Polygon

    fits = [f for f in (_circle_like(fr) for fr in face_rings or []) if f]
    ring = Polygon(pts)
    fits = [f for f in fits if ring.buffer(0.3).contains(Point(f[0]))]
    if not fits:
        f = _circle_like(pts)
        fits = [f] if f else []
    if fits:
        c, r = np.mean([f[0] for f in fits], axis=0), float(np.mean([f[1] for f in fits]))
        dia = round(2 * r, 3)
        return ({"type": "circle", "d": f"={hole_var(dia)}", "x": _r(c[0]), "y": _r(c[1]), "mode": "subtract"},
                {"kind": "hole", "d": dia, "u": _r(c[0], 3), "v": _r(c[1], 3), "segments": len(pts)})
    s = _fit_slot(pts) if len(pts) >= 8 else None
    if s:
        return ({"type": "slot", "length": _r(s["length"]), "w": _r(s["w"]), "x": _r(s["x"]), "y": _r(s["y"]),
                 "angle": _r(s["angle"], 3), "mode": "subtract"},
                {"kind": "slot", "length": round(s["length"], 3), "w": round(s["w"], 3), "u": _r(s["x"], 3), "v": _r(s["y"], 3)})
    return {"type": "polygon", "points": [[_r(u), _r(v)] for u, v in pts], "mode": "subtract"}, {"kind": "cut-out", "points": len(pts)}


class _HoleVars:
    """One diameter variable per hole size; sizes within 0.1 mm (or 4%) of each other share it."""

    def __init__(self, variables: dict[str, Any]):
        self.variables, self.sizes = variables, []  # [(diameter, name)]

    def __call__(self, dia: float) -> str:
        for d, name in self.sizes:
            if abs(d - dia) <= max(0.1, 0.04 * d):
                return name
        name = f"hole_d{len(self.sizes) + 1}"
        self.sizes.append((dia, name))
        self.variables[name] = round(dia, 2)
        return name


# ---------------- mesh preparation and planar regions ----------------

def prepare(mesh):
    """Clean copy of a triangle mesh (mm): merged vertices, no degenerate or duplicate faces, outward normals."""
    import trimesh

    m = trimesh.Trimesh(np.asarray(mesh.vertices, float), np.asarray(mesh.faces, np.int64), process=True)
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    if m.is_watertight:
        trimesh.repair.fix_normals(m)
    return m


def mesh_solid(mesh):
    """OCC solid from a closed mesh (planar faces merged)."""
    import cadquery as cq

    from .urdf import _faces_to_shape

    s = _faces_to_shape(mesh.vertices, mesh.faces, True)
    if s.Volume() < 0:
        s = cq.Shape.cast(s.wrapped.Reversed())
    return s


def _planes(mesh) -> list[dict[str, Any]]:
    """Planar regions: every triangle on the same plane (within 2° and 0.04 mm), connected or not."""
    fn, A = mesh.face_normals, mesh.area_faces
    tri = mesh.triangles
    total = float(A.sum())
    min_a = max(0.5, 0.001 * total)
    seeds = [np.asarray(f) for f in mesh.facets]
    in_facet = np.zeros(len(A), bool)
    for f in seeds:
        in_facet[f] = True
    seeds += [np.array([i]) for i in np.nonzero(~in_facet & (A >= 0.25 * min_a))[0]]
    seeds.sort(key=lambda f: -A[f].sum())
    taken = np.zeros(len(A), bool)
    out = []
    for f in seeds:
        if taken[f].any():
            continue
        n = _unit((fn[f] * A[f, None]).sum(0))
        c = float((tri[f].mean(1) @ n * A[f]).sum() / A[f].sum())
        on = None
        for _ in range(2):  # select, refit, reselect
            sel = ~taken & (fn @ n > 0.9994) & (np.abs(tri @ n - c).max(1) < 0.04)
            if not sel.any():
                break
            on, w = sel, A[sel]
            n = _unit((fn[on] * w[:, None]).sum(0))
            c = float((tri[on].mean(1) @ n * w).sum() / w.sum())
        if on is None:
            continue
        a = float(A[on].sum())
        if a < min_a:
            continue
        taken |= on
        cen = (tri[on].mean(1) * A[on, None]).sum(0) / a
        out.append({"faces": np.nonzero(on)[0], "n": n, "c": c, "area": a, "origin": cen - n * (cen @ n - c)})
    return out


def _poly(mesh, faces: np.ndarray, o: np.ndarray, x: np.ndarray, y: np.ndarray):
    """Region covered by these triangles, projected to the (o, x, y) plane frame (shapely)."""
    from shapely import union_all
    from shapely.geometry import Polygon

    tri = mesh.triangles[faces] - o
    P = np.stack([tri @ x, tri @ y], -1)
    e1, e2 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
    keep = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) > 2e-9
    polys = [Polygon(p).buffer(1e-4, join_style=2) for p in P[keep]]
    if not polys:
        return Polygon()
    return _clean(union_all(polys).buffer(-1e-4, join_style=2).simplify(0.003, preserve_topology=True))


# ---------------- sheet metal ----------------

def analyse(mesh) -> dict[str, Any]:
    """Flanges and thickness of a closed mesh (mm). {"ok", "reason"?, "thickness", "flanges", "paired_fraction"}"""
    if not mesh.is_watertight:
        return {"ok": False, "reason": "the mesh isn't closed"}
    total = float(mesh.area)
    planes = _planes(mesh)
    cache: dict[tuple[int, int], Any] = {}
    frames = {}

    def poly(k: int, owner: int):
        if (k, owner) not in cache:
            o, (x, y) = planes[owner]["origin"], frames[owner]
            cache[(k, owner)] = _poly(mesh, planes[k]["faces"], o, x, y)
        return cache[(k, owner)]

    cands = []
    for i, pi in enumerate(planes):
        for j in range(i + 1, len(planes)):
            pj = planes[j]
            if pi["n"] @ pj["n"] > -0.998:
                continue
            d = pi["c"] - pj["origin"] @ pi["n"]
            if not MIN_T <= d <= MAX_T:
                continue
            frames.setdefault(i, _frame(-pi["n"]))
            a, b = poly(i, i), poly(j, i)
            ov = a.intersection(b).area
            if ov >= 0.3 * min(a.area, b.area) and ov > 0.25:
                cands.append((i, j, d, ov))
    if not cands:
        return {"ok": False, "reason": "no parallel faces a sheet thickness apart (not sheet metal)"}
    t = _weighted_median(np.array([c[2] for c in cands]), np.array([c[3] for c in cands]))
    good = sorted((c for c in cands if abs(c[2] - t) <= max(0.04, 0.05 * t)), key=lambda c: -c[3])
    flanges, used = [], set()
    for i, j, d, _ in good:
        if i in used or j in used:
            continue
        used |= {i, j}
        x, y = frames[i]
        a, b = poly(i, i), poly(j, i)
        union = a.union(b).buffer(1e-3, join_style=2).buffer(-1e-3, join_style=2)
        for piece in _geoms(union):
            if piece.area < max(0.5, 0.5 * t * t):
                continue
            piece = _drop_slivers(piece)
            fo, fi = a.intersection(piece), b.intersection(piece)
            rings = [np.asarray(r.coords)[:-1] for g in _geoms(fo) + _geoms(fi) for r in g.interiors]
            flanges.append({"face_rings": rings, "n": planes[i]["n"], "origin": planes[i]["origin"], "x": x, "y": y, "poly": piece,
                            "face_out": fo, "face_in": fi, "area": piece.area, "paired": fo.area + fi.area,
                            "wide": not piece.buffer(-t, join_style=2).is_empty})
    # narrow strips at an angle to every wide flange are facets of a low-poly bend, not flanges
    wide = [f["n"] for f in flanges if f["wide"]]
    flanges = [f for f in flanges if f["wide"] or any(abs(f["n"] @ w) > 0.9994 for w in wide)]
    flanges.sort(key=lambda f: -f["area"])
    paired = sum(f["paired"] for f in flanges)
    if not flanges:
        return {"ok": False, "reason": "no flat flanges found (not sheet metal)"}
    if paired < 0.4 * total:
        return {"ok": False, "reason": f"only {100 * paired / total:.0f}% of the surface is flat sheet of one thickness (not sheet metal)",
                "thickness": round(t, 3), "paired_fraction": paired / total}
    return {"ok": True, "thickness": t, "flanges": flanges, "paired_fraction": paired / total}


def _to3(f: dict, pts2: np.ndarray) -> np.ndarray:
    return f["origin"] + pts2[:, :1] * f["x"] + pts2[:, 1:2] * f["y"]


def _ring_pts(shape) -> np.ndarray:
    pts = [np.asarray(g.exterior.coords) for g in _geoms(shape)]
    return np.concatenate(pts) if pts else np.zeros((0, 2))


def _sw(f: dict, a: np.ndarray, T: np.ndarray, d: np.ndarray) -> tuple[list[float], list[float]]:
    """Affine maps between a flange's (u, v) and the bend frame (s along the axis, w from the tangent line into the
    flange): shapely affine_transform parameters (forward, inverse)."""
    ax, ay, dx, dy = a @ f["x"], a @ f["y"], d @ f["x"], d @ f["y"]
    s0, w0 = float(a @ f["origin"]), float((f["origin"] - T) @ d)
    fwd = [ax, ay, dx, dy, s0, w0]
    # rows (ax, ay), (dx, dy) are orthonormal: the inverse is the transpose
    inv = [ax, dx, ay, dy, -(ax * s0 + dx * w0), -(ay * s0 + dy * w0)]
    return fwd, inv


def _intervals(shape, gap: float = 0.3) -> list[tuple[float, float]]:
    iv = sorted((g.bounds[0], g.bounds[2]) for g in _geoms(shape))
    out: list[list[float]] = []
    for lo, hi in iv:
        if out and lo <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def _bend(fa: dict, fb: dict, t: float, V: np.ndarray, plane_eqs: list[tuple[np.ndarray, float]]) -> dict[str, Any] | None:
    """Bend between two flanges, or None if they don't meet at a bend."""
    from shapely.affinity import affine_transform
    from shapely.geometry import box

    na, nb = fa["n"], fb["n"]
    if abs(na @ nb) > 0.995:
        return None
    a = _unit(np.cross(na, nb))
    Pa, Pb = _to3(fa, _ring_pts(fa["poly"])), _to3(fb, _ring_pts(fb["poly"]))
    # which side of each flange the other one is on (the concave side of the bend)
    side = []
    for f, other in ((fa, Pb), (fb, Pa)):
        mid = f["origin"] - f["n"] * t / 2
        dist = (other - mid) @ f["n"]
        if (dist > t).any() and (dist < -t).any():
            return None  # the other flange crosses this one: a T, not a bend
        sgn = 1.0 if dist.mean() > 0 else -1.0
        side.append((sgn * f["n"], f["origin"] if sgn > 0 else f["origin"] - t * f["n"]))
    (ma, pa), (mb, pb) = side
    theta = math.degrees(math.acos(float(np.clip(ma @ mb, -1, 1))))
    if not 4.0 <= theta <= 176.0:
        return None
    M = np.array([ma, mb, a])
    C0 = np.linalg.solve(M, [pa @ ma, pb @ mb, 0.0])  # inner corner line (r = 0)
    k = np.linalg.solve(M, [1.0, 1.0, 0.0])  # centre of radius r: C0 + r k
    uA, uB = -ma, -mb
    dA, dB = -_unit(uB - (uB @ uA) * uA), -_unit(uA - (uA @ uB) * uB)  # in-plane, from the bend into the flange
    lo_ax, hi_ax = max((Pa @ a).min(), (Pb @ a).min()), min((Pa @ a).max(), (Pb @ a).max())
    if hi_ax - lo_ax < 0.5:
        return None
    tan_half = math.tan(math.radians(min(theta, 150.0)) / 2)
    r_max = max(6 * t, 6.0)
    # inner radius from the mesh vertices in the corner (inner arc r, outer arc r + t)
    rel = V - C0
    ax_ = V @ a
    on_plane = np.zeros(len(V), bool)
    for n, c in plane_eqs:
        on_plane |= np.abs(V @ n - c) < 0.02
    rho0 = np.linalg.norm(rel - np.outer(rel @ a, a), axis=1)
    zone = (~on_plane & (ax_ > lo_ax - 0.2) & (ax_ < hi_ax + 0.2) & (rho0 < r_max * (np.linalg.norm(k) + 1) + t)
            & (rel @ dA <= r_max * ((k - ma) @ dA) + 0.05) & (rel @ dB <= r_max * ((k - mb) @ dB) + 0.05))
    pts = V[zone]
    r, fit = None, None
    if len(pts) >= 3:
        # for each radius: vertices inside that bend's sector which lie on its inner or outer cylinder (inliers)
        R = np.arange(0.0, r_max, 0.005)
        q = pts[None] - (C0[None] + R[:, None] * k[None])[:, None]  # (nR, np, 3) relative to the centre
        d = np.linalg.norm(q - (q @ a)[..., None] * a, axis=2)
        res = np.minimum(np.abs(d - R[:, None]), np.abs(d - R[:, None] - t))
        rel_p = pts - C0
        inA = rel_p @ dA <= R[:, None] * ((k - ma) @ dA) + 0.03  # beyond A's tangent line
        inB = rel_p @ dB <= R[:, None] * ((k - mb) @ dB) + 0.03
        tol = 0.02 + 0.01 * t
        inl = inA & inB & (res < tol)
        cnt = inl.sum(1)
        if cnt.max() >= 3:
            mean_res = np.where(cnt > 0, (res * inl).sum(1) / np.maximum(cnt, 1), 1e9)
            cand = np.nonzero(cnt == cnt.max())[0]
            best = int(cand[np.argmin(mean_res[cand])])
            r, fit = float(R[best]), float(mean_res[best])
    if r is None:  # no curved faces: radius from where the inner faces stop
        ests = []
        for f, m, dd, inner in ((fa, ma, dA, fa["face_out"] if ma @ fa["n"] > 0 else fa["face_in"]),
                                (fb, mb, dB, fb["face_out"] if mb @ fb["n"] > 0 else fb["face_in"])):
            P = _to3(f, _ring_pts(inner))
            if len(P):
                P = P[(P @ a > lo_ax) & (P @ a < hi_ax)]
            if len(P):
                ests.append(max(0.0, float(((P - C0) @ dd).min())) / tan_half)
        r = min(ests) if ests else 0.0
        if r > r_max:
            return None
    r = round(r, 2)  # the bend_radius variable's value
    C = C0 + r * k
    TA, TB = C - r * ma, C - r * mb  # tangent lines on the inner faces
    # a sharp outer corner (outer faces meet in a line) instead of an outer cylinder
    Co = C0 - t * k
    gap_o = (r + t) * (np.linalg.norm(k) - 1)
    rc = V - Co
    near_o = np.linalg.norm(rc - np.outer(rc @ a, a), axis=1) < min(0.05, max(0.005, 0.4 * gap_o))
    corner = int((near_o & (ax_ > lo_ax - 0.2) & (ax_ < hi_ax + 0.2)).sum()) >= 2
    # where both flanges reach the bend line: the bend's axial segments
    G = max(1.0, t)
    W = (r + t) * tan_half + t + 0.5
    ivs = []
    frames = []
    for f, T, dd in ((fa, TA, dA), (fb, TB, dB)):
        fwd, inv = _sw(f, a, T, dd)
        sw = affine_transform(f["poly"], fwd)
        near = sw.intersection(box(-1e6, -W, 1e6, G))
        if near.is_empty or near.bounds[1] > G - 1e-6:
            return None
        ivs.append(_intervals(near))
        frames.append((fwd, inv, T, dd))
    segs = []
    for lo1, hi1 in ivs[0]:
        for lo2, hi2 in ivs[1]:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if hi - lo >= max(0.5, t):
                segs.append((lo, hi))
    if not segs:
        return None
    # evidence in the mesh: vertices on the bend surface (arcs, outer corner or tangent lines) along each segment
    rc = V - C
    rho = np.linalg.norm(rc - np.outer(rc @ a, a), axis=1)
    on_bend = (np.abs(rho - r) < 0.05) | (np.abs(rho - r - t) < 0.05) | near_o
    segs = [(lo, hi) for lo, hi in segs if (on_bend & (ax_ > lo - 0.3) & (ax_ < hi + 0.3)).sum() >= 2]
    if not segs:
        return None
    rot = uA * math.cos(math.radians(theta)) + np.cross(a, uA) * math.sin(math.radians(theta))
    sign = 1.0 if rot @ uB > 0.99 else -1.0
    return {"a": a, "C": C, "Co": Co, "r": r, "fit": fit, "theta": theta, "sign": sign, "uA": uA, "uB": uB,
            "ma": ma, "mb": mb, "segments": segs, "corner": corner, "W": W, "G": G, "frames": frames}


def _sweep(shape, v: tuple[float, float]):
    """Minkowski sum of a polygon's outline with the segment [0, v]."""
    from shapely import union_all
    from shapely.affinity import translate
    from shapely.geometry import Polygon

    parts = [shape, translate(shape, *v)]
    vv = np.asarray(v)
    for g in _geoms(shape):
        ext = np.asarray(g.exterior.coords)
        for p, q in zip(ext[:-1], ext[1:]):
            quad = Polygon([p, q, q + vv, p + vv])
            if quad.area > 1e-12:
                parts.append(quad)
    return union_all(parts)


def _trim(poly, b: dict, side: int, w_end: float):
    """Flange outline (u, v) trimmed or extended to w = w_end over the bend's segments."""
    from shapely.affinity import affine_transform
    from shapely.geometry import box

    fwd, inv, _, _ = b["frames"][side]
    sw = affine_transform(poly, fwd)
    for lo, hi in b["segments"]:
        sw = sw.difference(box(lo, -b["W"] - 1, hi, w_end))
        reach = sw.intersection(box(lo, w_end, hi, b["G"]))
        if not reach.is_empty:
            ext = _sweep(reach, (0.0, -(b["G"] - w_end) - 1)).intersection(box(lo, w_end, hi, b["G"]))
            sw = sw.union(ext)
    return _clean(affine_transform(sw, inv))


def _corner_w(b: dict, side: int) -> float:
    """w of the outer corner line in a flange's bend frame (where a sharp-cornered flange ends)."""
    _, _, T, dd = b["frames"][side]
    return float((b["Co"] - T) @ dd)


def _rebuild_sheet(mesh, name: str) -> dict[str, Any]:
    from shapely import union_all
    from shapely.geometry import Polygon

    info = analyse(mesh)
    if not info["ok"]:
        return {"ok": False, "reason": info["reason"]}
    t = round(info["thickness"], 3)  # the geometry uses the rounded variable values, so faces meet exactly
    fl = info["flanges"]
    V = np.asarray(mesh.vertices)
    plane_eqs = [(f["n"], float(f["origin"] @ f["n"]) - off) for f in fl for off in (0.0, t)]
    bends = []
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            b = _bend(fl[i], fl[j], t, V, plane_eqs)
            if b:
                b["i"], b["j"] = i, j
                bends.append(b)
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(range(len(fl)))
    g.add_edges_from((b["i"], b["j"]) for b in bends)
    if nx.number_connected_components(g) > 1:
        return {"ok": False, "reason": f"{len(fl)} flanges and {len(bends)} bend(s), but the flanges don't all connect through "
                                       "bends (joined some other way, or not sheet metal)", "thickness": round(t, 3)}
    # outlines: the 3D model reaches 0.02 mm into each bend (or to the outer corner); the flat one stops at the tangent line
    shapes3, shapes_flat = [f["poly"] for f in fl], [f["poly"] for f in fl]
    for b in bends:
        for side, k in ((0, b["i"]), (1, b["j"])):
            w3 = _corner_w(b, side) if b["corner"] else 0.0
            shapes3[k] = _trim(shapes3[k], b, side, w3)
            shapes_flat[k] = _trim(shapes_flat[k], b, side, 0.0)
    for k, f in enumerate(fl):  # extensions toward a bend may have covered holes near it: cut them again
        holes = [Polygon(r) for r in f["poly"].interiors]
        if holes:
            shapes3[k] = shapes3[k].difference(union_all(holes))
            shapes_flat[k] = shapes_flat[k].difference(union_all(holes))
    variables: dict[str, Any] = {"thickness": t}

    hole_var = _HoleVars(variables)

    features: list[dict[str, Any]] = []
    cutouts: list[dict[str, Any]] = []
    for k, (f, shape) in enumerate(zip(fl, shapes3), 1):
        pieces = [_drop_slivers(g) for g in _geoms(shape) if g.area > max(0.2, 0.1 * t * t)]
        shapes: list[dict[str, Any]] = []
        for p in pieces:
            shapes.append({"type": "polygon", "points": [[_p(u), _p(v)] for u, v in list(p.exterior.coords)[:-1]]})
        for p in pieces:
            for ring in p.interiors:
                sh, rec = _cutout_shape(np.asarray(ring.coords)[:-1], hole_var, f["face_rings"])
                shapes.append(sh)
                if rec:
                    cutouts.append({"flange": k, **rec})
        if not shapes:
            continue
        features.append({"id": f"flange_{k}", "type": "extrude", "params": {
            "plane": {"origin": [_p(c) for c in f["origin"]], "normal": [_p(c) for c in -f["n"]], "xdir": [_p(c) for c in f["x"]]},
            "shapes": shapes, "distance": "=thickness", **({"fuse_tol": 1e-3} if features else {})}})
    radius_vars: dict[float, str] = {}
    bend_report = []
    nb = 0
    for b in bends:
        rr = round(b["r"], 2)
        bend_report.append({"flanges": [b["i"] + 1, b["j"] + 1], "angle": round(b["theta"], 2), "inner_radius": rr,
                            "length": round(sum(hi - lo for lo, hi in b["segments"]), 2),
                            "outer_corner": "sharp" if b["corner"] else "round"})
        if b["corner"]:
            continue  # flanges overlap at the corner; no bend body
        rv = radius_vars.get(rr)
        if rv is None:
            rv = radius_vars[rr] = "bend_radius" if not radius_vars else f"bend_radius_{len(radius_vars) + 1}"
            variables[rv] = rr
        a, u = b["a"], b["uA"]
        normal = np.cross(a, u)  # builder: ydir = normal x xdir; with xdir = a this gives ydir = u
        for lo, hi in b["segments"]:
            nb += 1
            origin = b["C"] - a * (a @ b["C"]) + a * lo
            features.append({"id": f"bend_{nb}", "type": "revolve", "params": {
                "plane": {"origin": [_p(c) for c in origin], "normal": [_p(c) for c in normal], "xdir": [_p(c) for c in a]},
                "shapes": [{"type": "rect", "w": _p(hi - lo), "h": "=thickness", "x": 0, "y": f"={rv}", "centered": False}],
                "axis": "u", "angle": _p(b["sign"] * b["theta"]), "fuse_tol": 1e-3}})
    spec = {"spec_version": 2, "name": name, "variables": variables, "limits": {"thickness": {"min": 0.1}}, "features": features,
            "units": "mm", "source": {"type": "reverse_engineered", "note": "rebuilt from a mesh as sheet metal; verify before manufacture"}}
    report = {"kind": "sheet_metal", "thickness": round(t, 3), "flanges": len(fl), "bends": bend_report,
              "bend_radii": sorted(radius_vars), "bend_angles": [b["angle"] for b in bend_report], "cutouts": cutouts,
              "holes": [c for c in cutouts if c["kind"] == "hole"], "flat_fraction": round(info["paired_fraction"], 3)}
    model = {"flanges": fl, "bends": bends, "flat": shapes_flat, "t": t, "graph": g}
    return {"ok": True, "kind": "sheet_metal", "spec": spec, "report": report, "_model": model}


# ---------------- prismatic (2.5D) parts ----------------

def _section(mesh, origin: np.ndarray, n: np.ndarray, o2: np.ndarray, x: np.ndarray, y: np.ndarray):
    """Cross-section of a closed mesh by a plane, as a shapely region in the (o2, x, y) frame (even-odd rule)."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import linemerge
    from trimesh.intersections import mesh_plane

    segs = mesh_plane(mesh, n, origin)
    if not len(segs):
        return Polygon()
    rel = segs - o2
    pts = np.round(np.stack([rel @ x, rel @ y], -1), 6)
    merged = linemerge([LineString(s) for s in pts if np.abs(s[0] - s[1]).max() > 0])
    region = Polygon()
    for ln in getattr(merged, "geoms", [merged]):
        if ln.is_ring and len(ln.coords) >= 4:
            region = region.symmetric_difference(Polygon(ln.coords).buffer(0))
    return region


def _rebuild_prismatic(mesh, name: str) -> dict[str, Any]:
    if not mesh.is_watertight:
        return {"ok": False, "reason": "the mesh isn't closed"}
    fn, A = mesh.face_normals, mesh.area_faces
    total = float(A.sum())
    cands = [p["n"] for p in _planes(mesh)[:10]]
    try:
        cands += list(np.asarray(mesh.bounding_box_oriented.primitive.transform)[:3, :3].T)
    except Exception:  # degenerate box
        pass
    best = (0.0, None)
    for d in cands:
        dots = np.abs(fn @ d)
        frac = float(A[(dots > 0.9995) | (dots < 0.015)].sum() / total)
        if frac > best[0]:
            best = (frac, _unit(np.asarray(d, float)))
    frac, d = best
    if d is None or frac < 0.995:
        return {"ok": False, "reason": f"not an extrusion: {100 * (1 - frac):.1f}% of the surface is sloped or curved across every axis"}
    dots = fn @ d
    caps = np.abs(dots) > 0.9995
    h = np.sort(mesh.triangles_center[caps] @ d)
    levels = [h[0]]
    for v in h[1:]:
        if v - levels[-1] > 0.01:
            levels.append(v)
    zs = mesh.vertices @ d
    levels = sorted(set([float(zs.min())] + [float(v) for v in levels] + [float(zs.max())]))
    levels = [z for i, z in enumerate(levels) if i == 0 or z - levels[i - 1] > 0.01]
    if len(levels) > 16:
        return {"ok": False, "reason": f"not a simple extrusion: {len(levels) - 1} height steps"}
    x, y = _frame(d)
    P0 = mesh.centroid - d * (mesh.centroid @ d)
    slabs = []
    for z0, z1 in zip(levels[:-1], levels[1:]):
        region = _section(mesh, P0 + d * (z0 + z1) / 2, d, P0, x, y)
        if region.is_empty:
            continue
        if slabs and abs(slabs[-1][1] - z0) < 1e-6 and slabs[-1][2].symmetric_difference(region).area < 1e-3 * region.area:
            slabs[-1][1] = z1
        else:
            slabs.append([z0, z1, region])
    if not slabs:
        return {"ok": False, "reason": "no cross-section found"}
    variables: dict[str, Any] = {}

    hole_var = _HoleVars(variables)

    features, cutouts = [], []
    z = slabs[0][0]
    for k, (z0, z1, region) in enumerate(slabs, 1):
        hv = "height" if len(slabs) == 1 else f"height_{k}"
        variables[hv] = round(z1 - z0, 3)
        z0, z = (z0 if k == 1 or abs(z0 - z) > 1e-3 else z), (z0 if k == 1 or abs(z0 - z) > 1e-3 else z) + variables[hv]
        shapes = []
        pieces = _geoms(region)
        for p in pieces:
            shapes.append({"type": "polygon", "points": [[_p(u), _p(v)] for u, v in list(p.exterior.coords)[:-1]]})
        for p in pieces:
            for ring in p.interiors:
                sh, rec = _cutout_shape(np.asarray(ring.coords)[:-1], hole_var)
                shapes.append(sh)
                cutouts.append({"step": k, **rec})
        o = P0 + d * z0
        features.append({"id": f"extrude_{k}", "type": "extrude", "params": {
            "plane": {"origin": [_p(c) for c in o], "normal": [_p(c) for c in d], "xdir": [_p(c) for c in x]},
            "shapes": shapes, "distance": f"={hv}"}})
    spec = {"spec_version": 2, "name": name, "variables": variables, "limits": {}, "features": features, "units": "mm",
            "source": {"type": "reverse_engineered", "note": "rebuilt from a mesh as extrusions; verify before manufacture"}}
    report = {"kind": "prismatic", "axis": [round(float(c), 4) for c in d], "steps": len(slabs),
              "height": round(levels[-1] - levels[0], 3), "cutouts": cutouts, "holes": [c for c in cutouts if c["kind"] == "hole"]}
    return {"ok": True, "kind": "prismatic", "spec": spec, "report": report}


# ---------------- validation and entry point ----------------

def _ray_hits(tris: np.ndarray, gx: np.ndarray, gy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Crossings of vertical rays (grid gx x gy, along z) with triangles: (ray index, z)."""
    rid, zs = [], []
    nx_ = len(gx)
    x0, dx, y0, dy = gx[0], gx[1] - gx[0], gy[0], gy[1] - gy[0]
    for t in tris:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < 1e-12:
            continue  # seen edge-on
        i0, i1 = int(np.ceil((min(ax, bx, cx) - x0) / dx)), int(np.floor((max(ax, bx, cx) - x0) / dx))
        j0, j1 = int(np.ceil((min(ay, by, cy) - y0) / dy)), int(np.floor((max(ay, by, cy) - y0) / dy))
        if i1 < i0 or j1 < j0:
            continue
        I, J = np.meshgrid(np.arange(max(i0, 0), min(i1, nx_ - 1) + 1), np.arange(max(j0, 0), min(j1, len(gy) - 1) + 1), indexing="ij")
        px, py = gx[I.ravel()], gy[J.ravel()]
        l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / det
        l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / det
        l3 = 1 - l1 - l2
        inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if inside.any():
            rid.append((I.ravel() * len(gy) + J.ravel())[inside])
            zs.append((l1 * az + l2 * bz + l3 * cz)[inside])
    if not rid:
        return np.zeros(0, int), np.zeros(0)
    return np.concatenate(rid), np.concatenate(zs)


def compare_volumes(tris_a: np.ndarray, tris_b: np.ndarray, rays: int = 250000) -> dict[str, float]:
    """Volumes of two closed triangle sets and of their symmetric difference, by casting a dense grid of parallel
    rays (exact along each ray; no booleans, so faces lying on each other are no problem)."""
    # rays in a skew direction: walls parallel to the rays would be sampled in whole rays (a biased volume)
    k = _unit(np.array([0.31, 0.53, 0.79]))
    th = 0.37
    Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    Rot = np.eye(3) + math.sin(th) * Kx + (1 - math.cos(th)) * Kx @ Kx
    A, B = tris_a @ Rot.T, tris_b @ Rot.T
    allp = np.concatenate([A.reshape(-1, 3), B.reshape(-1, 3)])
    lo, hi = allp.min(0), allp.max(0)
    pitch = math.sqrt((hi[0] - lo[0]) * (hi[1] - lo[1]) / rays) or 0.01
    rng = np.random.default_rng(7)  # jitter: rays never run exactly along mesh edges
    gx = np.arange(lo[0] + pitch * (0.5 + 0.01 * rng.random()), hi[0], pitch)
    gy = np.arange(lo[1] + pitch * (0.5 + 0.01 * rng.random()), hi[1], pitch)
    out = {}
    ev_r, ev_z, ev_a, ev_b = [], [], [], []
    for key, T in (("a", A), ("b", B)):
        r, z = _ray_hits(T, gx, gy)
        ev_r.append(r)
        ev_z.append(z)
        ev_a.append(np.full(len(r), key == "a"))
        ev_b.append(np.full(len(r), key == "b"))
    r, z = np.concatenate(ev_r), np.concatenate(ev_z)
    fa, fb = np.concatenate(ev_a), np.concatenate(ev_b)
    o = np.lexsort((z, r))
    r, z, fa, fb = r[o], z[o], fa[o], fb[o]
    # parity per ray; rays with an odd count (grazing hits) are skipped
    ca, cb = np.cumsum(fa), np.cumsum(fb)
    starts = np.r_[0, np.nonzero(np.diff(r))[0] + 1]
    ends = np.r_[starts[1:], len(r)]
    na = ca[ends - 1] - np.r_[0, ca[starts[1:] - 1]] if len(r) else np.zeros(0)
    nb = cb[ends - 1] - np.r_[0, cb[starts[1:] - 1]] if len(r) else np.zeros(0)
    good_ray = np.zeros(len(gx) * len(gy), bool)
    good_ray[r[starts]] = (na % 2 == 0) & (nb % 2 == 0)
    base_a = np.repeat(np.r_[0, ca[starts[1:] - 1]] if len(r) else [], ends - starts)
    base_b = np.repeat(np.r_[0, cb[starts[1:] - 1]] if len(r) else [], ends - starts)
    ina, inb = (ca - base_a) % 2 == 1, (cb - base_b) % 2 == 1
    seg = np.r_[np.diff(z), 0.0] * np.r_[r[1:] == r[:-1], False] * good_ray[r]
    cell = pitch * pitch
    out["volume_a"] = float((seg * ina).sum() * cell)
    out["volume_b"] = float((seg * inb).sum() * cell)
    out["xor"] = float((seg * (ina != inb)).sum() * cell)
    out["skipped_rays"] = int(len(starts) - good_ray[r[starts]].sum()) if len(r) else 0
    out["pitch"] = pitch
    return out


def validate(spec: dict[str, Any], mesh) -> dict[str, Any]:
    """Build the spec and compare it with the mesh: volume difference and symmetric difference (ray cast)."""
    from .builder import BuildError
    from .project import Project
    from .spec import PartSpec

    try:
        p = Project(PartSpec.from_dict(spec))
    except BuildError as e:
        return {"ok": False, "reason": f"the rebuilt part didn't build: {e}"}
    if not p.result:
        return {"ok": False, "reason": "the rebuilt part didn't build"}
    part = p.result.shape.val()
    v, t = part.tessellate(0.005, 0.1)
    V = np.array([x.toTuple() for x in v])
    cmp = compare_volumes(np.asarray(mesh.triangles), V[np.asarray(t)])
    vm, vr = abs(float(mesh.volume)), abs(part.Volume())
    ratio = cmp["xor"] / vm if vm else 1.0
    return {"ok": ratio <= ACCEPT_MISMATCH, "mesh_volume": round(vm, 1), "part_volume": round(vr, 1),
            "volume_diff_pct": round(100 * (vr - vm) / vm, 2) if vm else None, "mismatch_pct": round(100 * ratio, 2),
            "ray_pitch_mm": round(cmp["pitch"], 4), "project": p}


def _set_fuse_tol(spec: dict[str, Any], tol: float | None) -> dict[str, Any]:
    out = json.loads(json.dumps(spec))
    for f in out["features"]:
        if "fuse_tol" in f["params"]:
            if tol is None:
                del f["params"]["fuse_tol"]
            else:
                f["params"]["fuse_tol"] = tol
    return out


def rebuild(mesh, name: str = "part") -> dict[str, Any]:
    """Parametric PartSpec for a closed mesh (mm), validated against it.

    Returns {"ok", "kind": "sheet_metal"|"prismatic"|None, "spec"?, "report", "mismatch_pct"?, "reason"?, "project"?}.
    Not ok means "not convertible" with the reasons of every attempt; nothing is approximated."""
    mesh = prepare(mesh)
    if not mesh.is_watertight:
        return {"ok": False, "kind": None, "reason": "not convertible: the mesh isn't closed (open surfaces or gaps)", "report": {}}
    reasons = []
    sheet_report = None
    for attempt in (_rebuild_sheet, _rebuild_prismatic):
        try:
            r = attempt(mesh, name)
        except Exception as e:  # geometry library failure: report it, don't crash the batch
            r = {"ok": False, "reason": f"{attempt.__name__[9:]} analysis failed ({type(e).__name__}: {e})"}
        if not r["ok"]:
            reasons.append(r["reason"])
            continue
        v = validate(r["spec"], mesh)
        # OCC fuses faces that meet exactly with or without fuzz, case by case: on a failed build (or a degenerate
        # result) try the other settings and keep the best
        for tol in (None, 1e-4, 1e-2):
            if v["ok"] or "fuse_tol" not in json.dumps(r["spec"]):
                break
            spec = _set_fuse_tol(r["spec"], tol)
            v2 = validate(spec, mesh)
            if v2.get("mismatch_pct") is not None and (v.get("mismatch_pct") is None or v2["mismatch_pct"] < v["mismatch_pct"]):
                r["spec"], v = spec, v2
        r["report"]["mismatch_pct"] = v.get("mismatch_pct")
        if v["ok"]:
            r.update(mismatch_pct=v["mismatch_pct"], volume_diff_pct=v["volume_diff_pct"], project=v["project"])
            return r
        why = v.get("reason") or (f"rebuilt {r['kind'].replace('_', ' ')} differs from the mesh by {v['mismatch_pct']}% (limit {100 * ACCEPT_MISMATCH:g}%"
                                  + (", estimated from volumes only because the boolean comparison failed)" if v.get("approximate") else ")"))
        reasons.append(why)
        if r["kind"] == "sheet_metal":
            sheet_report = r["report"]
    out = {"ok": False, "kind": None, "reason": "not convertible: " + "; ".join(reasons), "report": sheet_report or {}}
    return out


# ---------------- flat pattern ----------------

def _affine(params: list[float]) -> np.ndarray:
    a, b, d, e, xo, yo = params
    return np.array([[a, b, xo], [d, e, yo], [0, 0, 1.0]])


def _params(M: np.ndarray) -> list[float]:
    return [M[0, 0], M[0, 1], M[1, 0], M[1, 1], M[0, 2], M[1, 2]]


def flat_pattern(rb: dict[str, Any], k_factor: float = K_FACTOR) -> dict[str, Any]:
    """Unfold a rebuilt sheet-metal part: {"outline": shapely polygon, "bend_lines": [...], "size", "area", "check"}.
    Raises ValueError with the reason when it can't be unfolded reliably."""
    import networkx as nx
    from shapely import union_all
    from shapely.affinity import affine_transform
    from shapely.geometry import box

    m = rb.get("_model")
    if not m:
        raise ValueError("not a rebuilt sheet-metal part")
    fl, bends, flat, t, g = m["flanges"], m["bends"], m["flat"], m["t"], m["graph"]
    if g.number_of_edges() >= g.number_of_nodes():
        raise ValueError("the bends form a closed loop (a box needs a seam); no flat pattern")
    root = 0
    place = {root: np.eye(3)}  # flange (u, v) -> flat
    up = {root: -fl[root]["n"]}  # 3D direction that is "up" (toward the viewer) in the flat pattern
    pieces = {root: flat[root]}
    lines = []
    for parent, child in nx.bfs_edges(g, root):
        b = next(b for b in bends if {b["i"], b["j"]} == {parent, child})
        ps, cs = (0, 1) if b["i"] == parent else (1, 0)
        fwd_p, inv_p, _, _ = b["frames"][ps]
        fwd_c, _, _, _ = b["frames"][cs]
        ba = math.radians(b["theta"]) * (b["r"] + k_factor * t)
        # child (u, v) -> (s, w_c) -> parent's (s, -(ba + w_c)) -> parent (u, v) -> flat
        flip = np.array([[1, 0, 0], [0, -1, -ba], [0, 0, 1.0]])
        Mc = place[parent] @ _affine(inv_p) @ flip @ _affine(fwd_c)
        place[child] = Mc
        mp, mc = (b["ma"], b["mb"]) if ps == 0 else (b["mb"], b["ma"])
        up[child] = mc if mp @ up[parent] > 0 else -mc
        direction = "UP" if mp @ up[parent] > 0 else "DOWN"
        pieces[child] = affine_transform(flat[child], _params(Mc))
        for lo, hi in b["segments"]:
            strip = affine_transform(box(lo, -ba, hi, 0), _params(place[parent] @ _affine(inv_p)))
            pieces[("bend", id(b), lo)] = strip
            P = place[parent] @ _affine(inv_p)
            l1 = [(P @ [s, 0, 1])[:2].tolist() for s in (lo, hi)]
            l2 = [(P @ [s, -ba, 1])[:2].tolist() for s in (lo, hi)]
            lines.append({"line": l1, "line2": l2, "label": f"{direction} {b['theta']:.1f}° R{b['r']:.2f}", "allowance": ba})
    geoms = list(pieces.values())
    shrunk = [g.buffer(-0.01, join_style=2) for g in geoms]
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            ov = shrunk[i].intersection(shrunk[j]).area  # shrunk: shared edges never count as overlap
            if ov > 0.01 * min(geoms[i].area, geoms[j].area) + 0.05:
                raise ValueError("flanges overlap each other when unfolded")
    outline = union_all([p.buffer(1e-3, join_style=2) for p in geoms]).buffer(-1e-3, join_style=2)
    parts = _geoms(outline)
    big = [g for g in parts if g.area > 1e-3 * outline.area]
    if len(big) != 1:
        raise ValueError(f"the unfolded pattern has {len(big)} separate pieces")
    outline = big[0]
    outline = _geoms(outline)[0]
    # lay the pattern square to its smallest enclosing rectangle, at the origin
    rect = np.asarray(outline.minimum_rotated_rectangle.exterior.coords)
    e = max((rect[1] - rect[0], rect[2] - rect[1]), key=lambda v: np.linalg.norm(v))
    ang = -math.atan2(e[1], e[0])
    R = np.array([[math.cos(ang), -math.sin(ang), 0], [math.sin(ang), math.cos(ang), 0], [0, 0, 1.0]])
    outline = affine_transform(outline, _params(R))
    minx, miny, maxx, maxy = outline.bounds
    R = np.array([[1, 0, -minx], [0, 1, -miny], [0, 0, 1.0]]) @ R
    outline = affine_transform(outline, [1, 0, 0, 1, -minx, -miny])
    for ln in lines:
        for key in ("line", "line2"):
            ln[key] = [(R @ [x, y, 1])[:2].tolist() for x, y in ln[key]]
    minx, miny, maxx, maxy = outline.bounds
    return {"outline": outline, "bend_lines": lines, "size": (maxx - minx, maxy - miny), "area": outline.area,
            "k_factor": k_factor, "thickness": t}


def flat_dxf(fp: dict[str, Any], path: str) -> None:
    """Write a flat pattern as DXF (mm): OUTLINE (holes fitted as circles), BEND lines and labels."""
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.layers.add("OUTLINE", color=7)
    doc.layers.add("BEND", color=1, linetype="DASHED")
    doc.layers.add("NOTES", color=3)
    msp = doc.modelspace()
    o = fp["outline"]
    msp.add_lwpolyline(list(o.exterior.coords)[:-1], close=True, dxfattribs={"layer": "OUTLINE"})
    for ring in o.interiors:
        pts = np.asarray(ring.coords)[:-1]
        c, r, err = _fit_circle(pts)
        if len(pts) >= 5 and err <= 0.04 * r + 0.02:
            msp.add_circle((float(c[0]), float(c[1])), r, dxfattribs={"layer": "OUTLINE"})
        else:
            msp.add_lwpolyline([tuple(p) for p in pts], close=True, dxfattribs={"layer": "OUTLINE"})
    for bd in fp["bend_lines"]:
        for ln in (bd["line"], bd["line2"]):
            msp.add_line(tuple(ln[0]), tuple(ln[1]), dxfattribs={"layer": "BEND"})
        (ax, ay), (bx, by) = bd["line"]
        msp.add_text(bd["label"], height=2.0, dxfattribs={"layer": "NOTES"}).set_placement(((ax + bx) / 2, (ay + by) / 2))
    minx, miny, _, _ = o.bounds
    msp.add_text(f"FLAT PATTERN {fp['size'][0]:.2f} x {fp['size'][1]:.2f} mm, T {fp['thickness']:.2f}, K {fp['k_factor']:g}",
                 height=2.5, dxfattribs={"layer": "NOTES"}).set_placement((minx, miny - 5))
    doc.saveas(path)
