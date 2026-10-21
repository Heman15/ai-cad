# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reverse engineering: rebuild a sheet-metal mesh (flat plates, bent brackets) as a parametric part.

A mesh has only triangles. For sheet-metal parts the design can be recovered:
1. flanges: large planar regions with a parallel opposite face one thickness away;
2. thickness: the common distance of those face pairs (one variable, `thickness`);
3. outline and cut-outs of each flange: round holes are fitted as circles (diameter variables), other
   cut-outs stay polygons;
4. bends between flanges: axis, inner radius and angle solved from where the flanges end.
The part is rebuilt as features (extruded flanges plus revolved bends). It is then compared with the
mesh (volume and symmetric difference), and kept only if they match; otherwise the reason is reported.
Not handled: formed features (louvres, embosses), non-planar flanges, parts that aren't sheet metal.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

ACCEPT_SYMDIFF = 0.06  # max (mesh XOR rebuilt) / mesh volume
MIN_T, MAX_T = 0.3, 6.0


def _plane_frame(n: np.ndarray, hint: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Same convention as builder._frame: x from hint (or global X/Y) projected, y = n x x."""
    x = hint if hint is not None else (np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0]))
    x = x - n * (x @ n)
    x /= np.linalg.norm(x)
    return x, np.cross(n, x)


def _fit_circle(pts: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Least-squares circle (centre, radius, max radial error)."""
    A = np.c_[2 * pts, np.ones(len(pts))]
    b = (pts ** 2).sum(1)
    c, *_ = np.linalg.lstsq(A, b, rcond=None)
    centre = c[:2]
    r = math.sqrt(max(c[2] + centre @ centre, 0))
    err = float(np.max(np.abs(np.linalg.norm(pts - centre, axis=1) - r))) if len(pts) else 1e9
    return centre, r, err


def _facet_polygon(mesh, faces: np.ndarray, origin: np.ndarray, x: np.ndarray, y: np.ndarray):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    tri = mesh.vertices[mesh.faces[faces]] - origin
    polys = [Polygon(np.c_[t @ x, t @ y]) for t in tri]
    shape = unary_union([p.buffer(1e-4, join_style=2) for p in polys if p.area > 1e-9]).buffer(-1e-4, join_style=2)
    if shape.geom_type == "MultiPolygon":
        shape = max(shape.geoms, key=lambda g: g.area)
    return shape.simplify(0.01, preserve_topology=True)


def _planes(mesh, total: float) -> list[tuple[np.ndarray, np.ndarray, float, np.ndarray]]:
    """Planar regions as (face indices, normal, area, origin): every triangle on the same plane, connected or not
    (meshes often split a flat plate into patches at slight creases)."""
    fn, V, F, areas = mesh.face_normals, mesh.vertices, mesh.faces, mesh.area_faces
    taken = np.zeros(len(F), bool)
    out = []
    for f, n, o in sorted(zip(mesh.facets, mesh.facets_normal, mesh.facets_origin), key=lambda x: -mesh.area_faces[x[0]].sum()):
        if taken[f].all():
            continue
        c = o @ n
        on = (fn @ n > 0.9995) & (np.abs(V[F] @ n - c).max(1) < 0.03) & ~taken
        if not on.any():
            continue
        a = float(areas[on].sum())
        if a < max(4.0, 0.004 * total):
            continue
        taken |= on
        idx = np.nonzero(on)[0]
        out.append((idx, n, a, o))
    return out


def analyse(mesh) -> dict[str, Any]:
    """Flanges and thickness of a closed mesh (mm). {"ok": bool, "reason"?, "thickness", "flanges": [...]}"""
    if not mesh.is_watertight:
        return {"ok": False, "reason": "the mesh isn't closed"}
    total = mesh.area
    fac = _planes(mesh, total)
    pairs = []
    for i, (fi, ni, ai, oi) in enumerate(fac):
        for j, (fj, nj, aj, oj) in enumerate(fac):
            if j <= i or ni @ nj > -0.998:
                continue
            d = float((oi - oj) @ ni)
            if MIN_T <= d <= MAX_T:
                pairs.append((i, j, d))
    if not pairs:
        return {"ok": False, "reason": "no parallel face pairs a sheet thickness apart (not sheet metal)"}
    ds = np.array([p[2] for p in pairs])
    weights = np.array([fac[p[0]][2] + fac[p[1]][2] for p in pairs])
    t = float(np.median(np.repeat(ds, np.maximum(1, (weights / weights.min()).astype(int))[:]))) if len(ds) else 0
    good = [p for p in pairs if abs(p[2] - t) <= max(0.05, 0.08 * t)]
    paired_area = sum(fac[i][2] + fac[j][2] for i, j, _ in good)
    if paired_area < 0.6 * total:
        return {"ok": False, "reason": f"only {100 * paired_area / total:.0f}% of the surface is paired at one thickness (not sheet metal)"}
    flanges, used = [], set()
    for i, j, d in sorted(good, key=lambda p: -(fac[p[0]][2] + fac[p[1]][2])):
        if i in used or j in used:
            continue
        # does face j really sit behind face i (overlapping outlines)?
        fi, ni, ai, oi = fac[i]
        fj = fac[j][0]
        x, y = _plane_frame(-ni)
        pi = _facet_polygon(mesh, fi, oi, x, y)
        pj = _facet_polygon(mesh, fj, oi, x, y)
        if pi.is_empty or pj.is_empty or pi.intersection(pj).area < 0.5 * min(pi.area, pj.area):
            continue
        used |= {i, j}
        # outline: material exists wherever either face does (one face may be cut back or split in the mesh)
        poly = pi.union(pj).buffer(0)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        flanges.append({"normal_out": ni, "origin": oi, "x": x, "y": y, "polygon": poly, "area": poly.area})
    if not flanges:
        return {"ok": False, "reason": "no flanges found"}
    return {"ok": True, "thickness": round(t, 3), "flanges": flanges}


def _bend(fa: dict, fb: dict, t: float, V: np.ndarray, planes: list[tuple[np.ndarray, float]]) -> dict[str, Any] | None:
    """Bend between two flanges, measured from the mesh: for a radius r the axis is fixed by the two inner
    planes; r is the value whose cylinders (r and r + t) best fit the mesh points in the bend corner.
    Returns axis point/direction, inner radius, signed angle, length and radial start direction, or None."""
    na, nb = fa["normal_out"], fb["normal_out"]
    if abs(na @ nb) > 0.995:
        return None
    a = np.cross(na, nb)
    a /= np.linalg.norm(a)

    def pts3(f):
        ext = np.asarray(f["polygon"].exterior.coords)
        return f["origin"] + ext[:, :1] * f["x"] + ext[:, 1:2] * f["y"]

    Pa, Pb = pts3(fa), pts3(fb)
    lo_ax, hi_ax = max((Pa @ a).min(), (Pb @ a).min()), min((Pa @ a).max(), (Pb @ a).max())
    if hi_ax - lo_ax < 0.5:
        return None
    side = {}
    for key, f, own, other in (("a", fa, Pa, Pb), ("b", fb, Pb, Pa)):
        n = f["normal_out"]
        sgn = 1.0 if (other.mean(0) - own.mean(0)) @ n > 0 else -1.0
        side[key] = (sgn * n, f["origin"] + (0 if sgn > 0 else -t) * n)  # (normal toward the concave side, point on the inner face)
    (ma, pa), (mb, pb) = side["a"], side["b"]
    # C(r) = C0 + r k with (C - pa).ma = r, (C - pb).mb = r, C.a = 0
    M = np.array([ma, mb, a])
    C0 = np.linalg.solve(M, [pa @ ma, pb @ mb, 0.0])
    k = np.linalg.solve(M, [1.0, 1.0, 0.0])
    # mesh points near the corner, inside the flanges' common stretch, not on any flange face
    rel = V - C0
    rho = np.linalg.norm(rel - np.outer(rel @ a, a), axis=1)
    on_plane = np.zeros(len(V), bool)
    for n, c in planes:
        on_plane |= np.abs(V @ n - c) < 0.03
    ax = V @ a
    zone = (rho < 4 * t + 3) & (ax > lo_ax - 0.5) & (ax < hi_ax + 0.5) & ~on_plane
    # only points on the convex side of the corner (where the bend material is)
    zone &= (rel @ (-ma) > -0.05) & (rel @ (-mb) > -0.05)
    pts = V[zone]
    if len(pts) >= 3:
        best = None
        for r in np.linspace(0, 4 * t, 161):
            c = C0 + r * k
            q = pts - c
            d = np.linalg.norm(q - np.outer(q @ a, a), axis=1)
            score = float(np.median(np.minimum(np.abs(d - r), np.abs(d - r - t))))
            if best is None or score < best[0] - 1e-9:
                best = (score, r)
        score, r = best
        if score > 0.15 + 0.1 * t:
            return None
        lo, hi = float((pts @ a).min()), float((pts @ a).max())
    else:  # sharp corner: no curved faces
        r, lo, hi = 0.0, lo_ax, hi_ax
    # a flange that stops short of the inner corner needs at least that radius to connect
    for f, other in ((fa, Pb), (fb, Pa)):
        n = f["normal_out"]
        d = np.cross(a, n)
        own = pts3(f)
        if (other.mean(0) - own.mean(0)) @ d < 0:
            d = -d
        P = _clip_axial(f, a, lo_ax, hi_ax)
        if P is not None:
            e = float(np.max((P - C0) @ d))
            r = max(r, min(-e, 4 * t)) if e < 0 else r
    if hi - lo < 0.5:
        return None
    C = C0 + r * k
    C = C - a * (a @ C) + a * lo
    ang = math.degrees(math.acos(float(np.clip(ma @ mb, -1, 1))))
    u = -ma
    th = math.radians(ang)
    rot = u * math.cos(th) + np.cross(a, u) * math.sin(th) + a * (a @ u) * (1 - math.cos(th))
    sign = 1.0 if rot @ (-mb) > 0.99 else -1.0
    return {"axis_point": C, "axis_dir": a, "radius": float(r), "angle": ang * sign, "length": hi - lo, "radial": -ma}


def _clip_axial(f: dict, a: np.ndarray, lo: float, hi: float) -> np.ndarray | None:
    """3D outline points of a flange, cut to the slab lo <= p.a <= hi (polygon clipped, so edges are kept)."""
    from shapely.geometry import Polygon

    # the axis direction in this flange's 2D frame; the slab is a strip lo <= o + g.(u, v) <= hi
    g = np.array([a @ f["x"], a @ f["y"]])
    gl = float(np.linalg.norm(g))
    if gl < 1e-6:
        return None
    gh, q = g / gl, np.array([-g[1], g[0]]) / gl
    o, L = float(f["origin"] @ a), 1e4
    s0, s1 = (lo - o) / gl, (hi - o) / gl
    strip = Polygon([gh * s0 - q * L, gh * s1 - q * L, gh * s1 + q * L, gh * s0 + q * L])
    poly = f["polygon"].intersection(strip)
    if poly.is_empty:
        return None
    geoms = getattr(poly, "geoms", [poly])
    pts = np.concatenate([np.asarray(g.exterior.coords) for g in geoms if g.geom_type == "Polygon"])
    P = f["origin"] + pts[:, :1] * f["x"] + pts[:, 1:2] * f["y"]
    keep = (P @ a >= lo - 1e-3) & (P @ a <= hi + 1e-3)
    return P[keep] if keep.any() else None


def _r(v: float, k: int = 4) -> float:
    return round(float(v), k) + 0.0


def rebuild(mesh, name: str) -> dict[str, Any]:
    """Parametric PartSpec dict for a sheet-metal mesh, validated against it. {"ok", "spec"?, "report"}"""
    from shapely.geometry import Polygon

    info = analyse(mesh)
    if not info["ok"]:
        return {"ok": False, "report": {"reason": info["reason"]}}
    t = info["thickness"]
    variables: dict[str, Any] = {"thickness": _r(t, 3)}
    features: list[dict[str, Any]] = []
    holes: list[dict[str, Any]] = []
    hole_vars: dict[float, str] = {}
    for k, f in enumerate(info["flanges"], 1):
        poly = f["polygon"]
        n_e = -f["normal_out"]  # extrude into the material
        x, y = f["x"], f["y"]
        shapes = [{"type": "polygon", "points": [[_r(u), _r(v)] for u, v in list(poly.exterior.coords)[:-1]]}]
        for ring in poly.interiors:
            pts = np.asarray(ring.coords)[:-1]
            c, r, err = _fit_circle(pts)
            ring_area = Polygon(pts).area
            if len(pts) >= 6 and err <= 0.06 * r + 0.03 and 0.80 <= ring_area / (math.pi * r * r) <= 1.02:
                dia = round(2 * r, 2)
                var = hole_vars.get(dia)
                if var is None:
                    var = hole_vars[dia] = f"hole_d{len(hole_vars) + 1}"
                    variables[var] = dia
                shapes.append({"type": "circle", "d": f"={var}", "x": _r(c[0]), "y": _r(c[1]), "mode": "subtract"})
                holes.append({"flange": k, "d": dia, "u": _r(c[0], 2), "v": _r(c[1], 2)})
            else:
                shapes.append({"type": "polygon", "points": [[_r(u), _r(v)] for u, v in pts], "mode": "subtract"})
        features.append({"id": f"flange_{k}", "type": "extrude", "params": {
            "plane": {"origin": [_r(c) for c in f["origin"]], "normal": [_r(c, 6) for c in n_e], "xdir": [_r(c, 6) for c in x]},
            "shapes": shapes, "distance": "=thickness"}})
    bends = []
    fl = info["flanges"]
    planes = [(f["normal_out"], float(f["origin"] @ f["normal_out"]) - off) for f in fl for off in (0.0, t)]
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            b = _bend(fl[i], fl[j], t, np.asarray(mesh.vertices), planes)
            if b:
                bends.append((i + 1, j + 1, b))
    if len(fl) > 1 and len(bends) < len(fl) - 1:
        return {"ok": False, "report": {"reason": f"{len(fl)} flanges but only {len(bends)} bend(s) found; the flanges don't connect as a bent sheet",
                                        "thickness": t}}
    radius_vars: dict[float, str] = {}
    for k, (i, j, b) in enumerate(bends, 1):
        rr = round(b["radius"], 2)
        rv = radius_vars.get(rr)
        if rv is None:
            rv = radius_vars[rr] = "bend_radius" if not radius_vars else f"bend_radius_{len(radius_vars) + 1}"
            variables[rv] = rr
        a, radial = b["axis_dir"], b["radial"]
        normal = np.cross(a, radial)  # builder: y = n x xdir; with xdir = a this gives y = radial
        features.append({"id": f"bend_{k}", "type": "revolve", "params": {
            "plane": {"origin": [_r(c) for c in b["axis_point"]], "normal": [_r(c, 6) for c in normal], "xdir": [_r(c, 6) for c in a]},
            "shapes": [{"type": "rect", "w": _r(b["length"]), "h": "=thickness", "x": 0, "y": f"={rv}", "centered": False}],
            "axis": "u", "angle": _r(b["angle"], 3)}})
    spec = {"spec_version": 2, "name": name, "variables": variables, "limits": {"thickness": {"min": 0.1}}, "features": features,
            "units": "mm", "source": {"type": "reverse_engineered", "note": "rebuilt from a mesh; check critical dimensions"}}
    report = {"thickness": t, "flanges": len(fl), "bends": len(bends), "holes": holes,
              "bend_radii": sorted(radius_vars), "bend_angles": [round(abs(b["angle"]), 1) for _, _, b in bends]}
    return {"ok": True, "spec": spec, "report": report}


def validate(spec: dict[str, Any], mesh_solid) -> dict[str, Any]:
    """Build the spec and compare it with the mesh solid: volume difference and symmetric difference."""
    from .project import Project
    from .spec import PartSpec

    p = Project(PartSpec.from_dict(spec))
    if not p.result:
        return {"ok": False, "reason": "the rebuilt part didn't build"}
    solid = p.result.shape.val()
    vm, vr = abs(mesh_solid.Volume()), abs(solid.Volume())
    try:
        xor = sum(abs(s.Volume()) for s in solid.cut(mesh_solid).Solids()) + sum(abs(s.Volume()) for s in mesh_solid.cut(solid).Solids())
    except Exception:  # boolean failure: fall back to the volume comparison only
        xor = abs(vm - vr) * 3
    ratio = xor / vm if vm else 1
    return {"ok": ratio <= ACCEPT_SYMDIFF, "mesh_volume": round(vm, 1), "part_volume": round(vr, 1),
            "volume_diff_pct": round(100 * (vr - vm) / vm, 2), "mismatch_pct": round(100 * ratio, 2), "project": p}
