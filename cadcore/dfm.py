# SPDX-License-Identifier: AGPL-3.0-or-later
"""Design-for-manufacturing checks: rule-based, with the threshold stated in every finding.

These are screening checks, not a quote or a guarantee. They find the most common problems:
- walls that are too thin;
- internal corners a cutter can't reach;
- surfaces that can't be machined from any direction;
- holes that are too deep or too small;
- 3D-print overhangs.

A machinist or print bureau may accept or reject parts these checks don't flag.
"""

from __future__ import annotations

from typing import Any

import cadquery as cq
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector

from .builder import BuildResult

PROCESSES = {
    "cnc_milling": {"label": "CNC milling (3-axis)", "min_wall": 1.0, "min_hole": 1.0, "max_hole_ratio": 10.0,
                    "min_inner_radius": 0.5, "setup_dirs": ["+Z", "-Z", "+X", "-X", "+Y", "-Y"]},
    "fdm_printing": {"label": "FDM 3D printing", "min_wall": 0.8, "min_hole": 2.0, "max_overhang_deg": 45.0},
    "sheet_metal": {"label": "Sheet metal (laser cut + press brake)", "min_bend_radius_factor": 1.0,
                    "min_flange_factor": 4.0, "hole_to_bend_factor": 2.0, "min_hole_factor": 1.0},
}
_DIRS = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0), "+Z": (0, 0, 1), "-Z": (0, 0, -1)}


class _Rays:
    def __init__(self, solid: cq.Shape):
        self.solid = solid
        self.isec = IntCurvesFace_ShapeIntersector()
        self.isec.Load(solid.wrapped, 1e-6)

    def hits(self, p: cq.Vector, d: cq.Vector, start: float = 1e-4) -> list[float]:
        self.isec.Perform(gp_Lin(gp_Pnt(*p.toTuple()), gp_Dir(*d.toTuple())), start, 1e9)
        return sorted(self.isec.WParameter(i) for i in range(1, self.isec.NbPnt() + 1))


def _samples(face: cq.Face, n: int = 6) -> list[tuple[cq.Vector, cq.Vector]]:
    """Points on the face (triangle centroids projected onto the surface) with outward normals."""
    verts, tris = face.tessellate(0.2, 0.3)
    tris = sorted(tris, key=lambda t: -((verts[t[1]] - verts[t[0]]).cross(verts[t[2]] - verts[t[0]])).Length)[: max(n, 1)]
    out = []
    for t in tris:
        c = (verts[t[0]] + verts[t[1]] + verts[t[2]]) * (1 / 3)
        try:
            p = face.Center() if face.distance(cq.Vertex.makeVertex(*c.toTuple())) > 0.05 else c
            out.append((p, face.normalAt(p).normalized()))
        except Exception:
            continue
    return out


def _wall_thickness(rays: _Rays, faces: list[cq.Face], solid: cq.Shape, limit: float) -> list[dict[str, Any]]:
    findings = []
    for fi, f in enumerate(faces):
        worst = None
        for p, n in _samples(f):
            inward = -n
            hits = [h for h in rays.hits(p + inward * 1e-3, inward) if h > 0.02]  # ignore grazing hits on adjacent faces
            if not hits:
                continue
            t = hits[0] + 1e-3
            if worst is None or t < worst[0]:
                worst = (t, p)
        if worst and worst[0] < limit:
            findings.append({"rule": "min_wall", "severity": "warning", "face_index": fi, "value": round(worst[0], 3),
                             "threshold": limit, "location": [round(c, 3) for c in worst[1].toTuple()],
                             "message": f"wall {worst[0]:.2f} mm thick (minimum {limit} mm)"})
    return _dedupe(findings)


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Opposite faces of one thin wall report the same thickness: keep one per (value, rounded location pair)."""
    out, seen = [], set()
    for f in sorted(findings, key=lambda f: f["value"]):
        key = (f["rule"], round(f["value"], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _sharp_inner_corners(solid: cq.Shape, axis: cq.Vector, min_radius: float) -> list[dict[str, Any]]:
    """Concave straight edges parallel to the tool axis: an end mill leaves a radius there."""
    findings = []
    faces = solid.Faces()
    for e in solid.Edges():
        if e.geomType() != "LINE":
            continue
        d = (e.endPoint() - e.startPoint()).normalized()
        if abs(abs(d.dot(axis)) - 1) > 1e-6:
            continue
        m = e.positionAt(0.5)
        vm = cq.Vertex.makeVertex(*m.toTuple())
        adj = [f for f in faces if f.distance(vm) < 1e-6]
        if len(adj) != 2 or any(f.geomType() != "PLANE" for f in adj):
            continue
        n1, n2 = adj[0].normalAt(m).normalized(), adj[1].normalAt(m).normalized()
        if abs(n1.dot(n2)) > 0.999:
            continue
        eps = 0.05
        # concave (internal) corner: stepping off one face toward the other face's outside stays in material, both ways
        if solid.isInside(m - n1 * eps + n2 * eps, 1e-6) and solid.isInside(m + n1 * eps - n2 * eps, 1e-6):
            findings.append({"rule": "inner_corner", "severity": "warning", "value": 0.0, "threshold": min_radius,
                             "location": [round(c, 3) for c in m.toTuple()],
                             "message": f"sharp internal corner (length {e.Length():.1f} mm) parallel to the tool axis: "
                                        f"a milling cutter leaves a radius; add a fillet ≥ R{min_radius} (larger is cheaper)"})
    return findings


def _accessibility(rays: _Rays, faces: list[cq.Face], dirs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Which setup directions can reach each face (normal not facing away, line of sight to outside)."""
    reach: list[set[str]] = []
    for f in faces:
        ok = set()
        for name in dirs:
            d = cq.Vector(*_DIRS[name])
            sample_ok = True
            for p, n in _samples(f, 3):
                if n.dot(d) < -1e-6:
                    sample_ok = False
                    break
                # start just off the surface so walls parallel to the tool axis (side milling) are not self-occluded
                if rays.hits(p + n * 0.01 + d * 1e-3, d):
                    sample_ok = False
                    break
            if sample_ok:
                ok.add(name)
        reach.append(ok)
    # greedy minimum set of setups covering all reachable faces
    remaining = {i for i, r in enumerate(reach) if r}
    chosen = []
    while remaining:
        best = max(dirs, key=lambda dname: sum(1 for i in remaining if dname in reach[i]))
        covered = {i for i in remaining if best in reach[i]}
        if not covered:
            break
        chosen.append(best)
        remaining -= covered
    findings = []
    for i, r in enumerate(reach):
        if not r:
            p, _ = (_samples(faces[i], 1) or [(faces[i].Center(), None)])[0]
            findings.append({"rule": "undercut", "severity": "warning", "face_index": i, "value": 0, "threshold": None,
                             "location": [round(c, 3) for c in p.toTuple()],
                             "message": "face not reachable along any of the 6 axis directions (undercut): needs a special "
                                        "tool, 4/5-axis machining or a design change"})
    return findings, chosen


def _holes(result: BuildResult, min_d: float, max_ratio: float | None) -> list[dict[str, Any]]:
    findings = []
    for h in result.holes:
        loc = [round(c, 3) for c in h.points[0].toTuple()]
        if h.diameter < min_d:
            findings.append({"rule": "small_hole", "severity": "warning", "value": h.diameter, "threshold": min_d, "location": loc,
                             "message": f"{h.feature}: Ø{h.diameter:g} is below the practical minimum Ø{min_d}"})
        if max_ratio and h.depth is not None and h.depth / h.diameter > max_ratio:
            findings.append({"rule": "deep_hole", "severity": "warning", "value": round(h.depth / h.diameter, 1), "threshold": max_ratio,
                             "location": loc, "message": f"{h.feature}: depth is {h.depth / h.diameter:.1f}× the diameter (> {max_ratio:g}×): "
                                                         "needs deep-hole drilling"})
    return findings


def _overhangs(solid: cq.Shape, faces: list[cq.Face], max_deg: float) -> list[dict[str, Any]]:
    import math

    zmin = solid.BoundingBox().zmin
    limit = math.cos(math.radians(max_deg))
    findings, area = [], 0.0
    for i, f in enumerate(faces):
        for p, n in _samples(f, 1):
            if -n.z > limit and f.BoundingBox().zmin > zmin + 1e-3:
                area += f.Area()
                findings.append({"rule": "overhang", "severity": "info", "face_index": i, "value": round(math.degrees(math.acos(min(1, -n.z))), 1),
                                 "threshold": max_deg, "location": [round(c, 3) for c in p.toTuple()],
                                 "message": f"overhang ({f.Area():.0f} mm²) needs support material or reorientation"})
    return findings


def _sheet(result: BuildResult, rules: dict[str, float]) -> list[dict[str, Any]]:
    st = result.sheet
    if st is None:
        raise ValueError("this is not a sheet-metal part")
    T, out = st.T, []
    for fl in st.flanges:
        loc = [round(c, 3) for c in ((fl.a + fl.b) * 0.5).toTuple()]
        if fl.radius < rules["min_bend_radius_factor"] * T:
            out.append({"rule": "bend_radius", "severity": "warning", "value": fl.radius, "threshold": rules["min_bend_radius_factor"] * T,
                        "location": loc, "message": f"{fl.feature}: inside bend radius R{fl.radius:g} is below {rules['min_bend_radius_factor']:g}×t; risk of cracking"})
        if fl.length + fl.radius + T < rules["min_flange_factor"] * T:
            out.append({"rule": "flange_length", "severity": "warning", "value": fl.length, "threshold": rules["min_flange_factor"] * T,
                        "location": loc, "message": f"{fl.feature}: flange too short for a press brake (outside length < {rules['min_flange_factor']:g}×t)"})
    for h in result.holes:
        for pt in h.points:
            if h.diameter < rules["min_hole_factor"] * T:
                out.append({"rule": "small_hole", "severity": "warning", "value": h.diameter, "threshold": T, "location": [round(c, 3) for c in pt.toTuple()],
                            "message": f"{h.feature}: Ø{h.diameter:g} is smaller than the sheet thickness"})
            for fl in st.flanges:
                d = (fl.b - fl.a).normalized()
                rel = pt - st.n * st.h(pt) - fl.a
                dist = (rel - d * rel.dot(d)).Length - h.diameter / 2
                limit = rules["hole_to_bend_factor"] * T + fl.radius
                if dist < limit:
                    out.append({"rule": "hole_near_bend", "severity": "warning", "value": round(dist, 2), "threshold": limit,
                                "location": [round(c, 3) for c in pt.toTuple()],
                                "message": f"{h.feature}: hole edge {dist:.1f} mm from bend {fl.feature} (< {limit:g} mm): the hole will distort"})
    return out


def check(result: BuildResult, process: str = "cnc_milling", overrides: dict[str, float] | None = None) -> dict[str, Any]:
    if process not in PROCESSES:
        raise ValueError(f"process must be one of {', '.join(PROCESSES)}")
    rules = {**PROCESSES[process], **(overrides or {})}
    if process == "sheet_metal":
        return {"process": process, "label": rules["label"], "rules": {k: v for k, v in rules.items() if k != "label"},
                "findings": _sheet(result, rules), "summary": {},
                "disclaimer": "Rule-of-thumb screening with the thresholds shown; confirm with your fabricator's tooling."}
    solid = result.shape.val()
    faces = solid.Faces()
    rays = _Rays(solid)
    findings = _wall_thickness(rays, faces, solid, rules["min_wall"])
    findings += _holes(result, rules["min_hole"], rules.get("max_hole_ratio"))
    summary: dict[str, Any] = {}
    if process == "cnc_milling":
        findings += _sharp_inner_corners(solid, cq.Vector(0, 0, 1), rules["min_inner_radius"])
        acc, setups = _accessibility(rays, faces, rules["setup_dirs"])
        findings += acc
        summary["setups"] = setups
        summary["setup_note"] = f"{len(setups)} setup(s) from {', '.join(setups)} reach all machinable faces"
    else:
        findings += _overhangs(solid, faces, rules["max_overhang_deg"])
    return {"process": process, "label": rules["label"], "rules": {k: v for k, v in rules.items() if k not in ("label", "setup_dirs")},
            "findings": findings, "summary": summary,
            "disclaimer": "Rule-of-thumb screening with the thresholds shown; not a substitute for review by the manufacturer."}
