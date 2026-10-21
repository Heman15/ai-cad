# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tessellate a built part for the viewport and describe every face/edge as a CAD entity.

Each face and edge carries the feature that produced it plus a geometric signature
(type, a point on it, normal/axis, size). Selections are stored as these signatures,
so after regeneration they can be re-found or reported as missing - never guessed.
"""

from __future__ import annotations

from typing import Any

import cadquery as cq

from .builder import BuildResult

ON_TOL = 1e-3  # mm: a point this close to a tool body's surface lies on it


def _r(v: float) -> float:
    return round(v, 4) + 0.0  # +0.0 turns -0.0 into 0.0


def _vec(v: cq.Vector) -> list[float]:
    return [_r(c) for c in v.toTuple()]


def _expanded_contains(box: cq.BoundBox, p: cq.Vector, tol: float = 0.05) -> bool:
    return (box.xmin - tol <= p.x <= box.xmax + tol and box.ymin - tol <= p.y <= box.ymax + tol
            and box.zmin - tol <= p.z <= box.zmax + tol)


class _Owner:
    """Finds the feature whose tool surface (or created faces) contains a point; latest feature wins."""

    def __init__(self, result: BuildResult):
        self.tools = [(fid, body, body.BoundingBox()) for fid, body in reversed(result.tools)]
        self.created = [(fid, faces) for fid, faces in reversed(result.created)]

    def __call__(self, p: cq.Vector) -> str | None:
        v = cq.Vertex.makeVertex(*p.toTuple())
        # Surfaces of add/cut bodies first: a face trimmed by a later fillet still lies on its original surface.
        for fid, body, box in self.tools:
            if _expanded_contains(box, p) and any(f.distance(v) < ON_TOL for f in body.Faces()):
                return fid
        for fid, faces in self.created:
            if any(f.distance(v) < ON_TOL for f in faces):
                return fid
        return None


def _face_info(face: cq.Face, sample: cq.Vector) -> dict[str, Any]:
    t = face.geomType()
    info: dict[str, Any] = {"type": t, "point": _vec(sample), "area": _r(face.Area()), "center": _vec(face.Center())}
    if t == "PLANE":
        info["normal"] = _vec(face.normalAt(sample))
    elif t == "CYLINDER":
        surf = face._geomAdaptor().Cylinder()
        ax = surf.Axis()
        info["radius"] = _r(surf.Radius())
        info["axis_point"] = [_r(ax.Location().X()), _r(ax.Location().Y()), _r(ax.Location().Z())]
        info["axis_dir"] = [_r(ax.Direction().X()), _r(ax.Direction().Y()), _r(ax.Direction().Z())]
    return info


def _edge_info(edge: cq.Edge) -> dict[str, Any]:
    t = edge.geomType()
    info: dict[str, Any] = {"type": t, "point": _vec(edge.positionAt(0.5)), "length": _r(edge.Length())}
    if t == "CIRCLE":
        info["radius"] = _r(edge.radius())
        info["center"] = _vec(edge.arcCenter())
    return info


def _hole_instance(result: BuildResult, fid: str | None, info: dict[str, Any]) -> dict[str, Any] | None:
    """For a cylindrical hole face, which of the feature's hole positions it is."""
    if fid is None or info.get("type") != "CYLINDER":
        return None
    holes = [h for h in result.holes if h.feature == fid]
    pts = [(h, pt) for h in holes for pt in h.points]
    if not pts:
        return None
    ap, ad = cq.Vector(*info["axis_point"]), cq.Vector(*info["axis_dir"])
    dist = [((pt - ap) - ad * (pt - ap).dot(ad)).Length for _, pt in pts]
    i = min(range(len(pts)), key=dist.__getitem__)
    if dist[i] > 0.05:
        return None
    return {"index": i, "count": len(pts), "position": _vec(pts[i][1]), "diameter": pts[i][0].diameter}


def _point_on(face: cq.Face, verts, tris) -> cq.Vector:
    """A point that lies on the face: the largest triangle's centroid projected onto the true surface."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    if tris:
        a, b, c = (verts[k] for k in max(tris, key=lambda t: (verts[t[1]] - verts[t[0]]).cross(verts[t[2]] - verts[t[0]]).Length))
        guess = (a + b + c) * (1 / 3)
    else:
        guess = face.Center()
    ext = BRepExtrema_DistShapeShape(cq.Vertex.makeVertex(*guess.toTuple()).wrapped, face.wrapped)
    if ext.IsDone() and ext.NbSolution() > 0:
        p = ext.PointOnShape2(1)
        return cq.Vector(p.X(), p.Y(), p.Z())
    return guess


def describe(result: BuildResult, tolerance: float = 0.05, angular: float = 0.2) -> dict[str, Any]:
    """Mesh plus entity table: {"faces": [...], "edges": [...]} with stable signatures."""
    owner = _Owner(result)
    solid = result.shape.val()
    faces = []
    for i, face in enumerate(solid.Faces()):
        verts, tris = face.tessellate(tolerance, angular)
        sample = _point_on(face, verts, tris)
        info = _face_info(face, sample)
        fid = owner(sample)
        entry = {"index": i, "feature": fid, **info,
                 "vertices": [_r(c) for v in verts for c in (v.x, v.y, v.z)],
                 "triangles": [k for t in tris for k in t]}
        inst = _hole_instance(result, fid, info)
        if inst:
            entry["hole_instance"] = inst
        faces.append(entry)
    edges = []
    for i, edge in enumerate(solid.Edges()):
        info = _edge_info(edge)
        pts = [edge.startPoint(), edge.endPoint()] if info["type"] == "LINE" else [edge.positionAt(k / 32) for k in range(33)]
        edges.append({"index": i, "feature": owner(edge.positionAt(0.5)), **info,
                      "polyline": [_r(c) for p in pts for c in p.toTuple()]})
    vertices = [{"index": i, "point": _vec(cq.Vector(*v.toTuple()))} for i, v in enumerate(solid.Vertices())]
    return {"faces": faces, "edges": edges, "vertices": vertices}


# ---------------- selection references ----------------

def resolve(result: BuildResult, ref: dict[str, Any]) -> dict[str, Any]:
    """Re-find a stored selection in the current model by its signature.

    Returns {"status": "ok", "index": i, ...current info} or {"status": "missing"/"changed", "reason": ...}.
    """
    kind, point = ref.get("kind"), ref.get("point")
    if kind not in ("face", "edge", "vertex") or not point:
        return {"status": "invalid", "reason": "selection reference must have kind face/edge/vertex and a point"}
    solid = result.shape.val()
    if kind == "vertex":
        p = cq.Vector(*point)
        near = [i for i, x in enumerate(solid.Vertices()) if (cq.Vector(*x.toTuple()) - p).Length < 0.05]
        if not near:
            return {"status": "missing", "reason": "the selected vertex no longer exists after the last change; select it again"}
        return {"status": "ok", "kind": "vertex", "index": near[0], "type": "VERTEX", "point": _vec(p),
                "feature": _Owner(result)(p)}
    v = cq.Vertex.makeVertex(*point)
    items = solid.Faces() if kind == "face" else solid.Edges()
    near = [(i, e) for i, e in enumerate(items) if e.distance(v) < 0.05]
    near = [(i, e) for i, e in near if e.geomType() == ref.get("type")]
    if kind == "face" and ref.get("normal"):
        n = cq.Vector(*ref["normal"])
        near = [(i, e) for i, e in near if e.geomType() != "PLANE" or e.normalAt(cq.Vector(*point)).dot(n) > 0.999]
    if not near:
        return {"status": "missing", "reason": f"the selected {kind} no longer exists after the last change; select it again"}
    if len(near) > 1:
        return {"status": "ambiguous", "reason": f"the selected {kind} matches {len(near)} entities; select it again"}
    i, e = near[0]
    info = _face_info(e, cq.Vector(*point)) if kind == "face" else _edge_info(e)
    size_key = "area" if kind == "face" else "length"
    status = "ok" if abs(info[size_key] - ref.get(size_key, info[size_key])) < 1e-3 * max(1, info[size_key]) else "changed"
    owner = _Owner(result)(cq.Vector(*point))
    out = {"status": status, "kind": kind, "index": i, "feature": owner, **info}
    if kind == "face":
        inst = _hole_instance(result, owner, info)
        if inst:
            out["hole_instance"] = inst
    if status == "changed":
        out["reason"] = f"the selected {kind} still exists but its size changed ({ref.get(size_key)} -> {info[size_key]})"
    return out


def _entity(solid, r: dict[str, Any]):
    return {"face": solid.Faces, "edge": solid.Edges, "vertex": solid.Vertices}[r["kind"]]()[r["index"]]


class ShapeResult:
    """A bare shape standing in for a build result (no feature history), e.g. an assembly component placed in the world."""

    def __init__(self, shape: cq.Shape):
        self.shape = cq.Workplane("XY").add(shape)
        self.tools, self.created, self.holes = [], [], []


def measure(result: BuildResult, refs: list[dict[str, Any]]) -> dict[str, Any]:
    """Measurements for one or two resolved selections (faces, edges or vertices).

    For two entities, "points" holds the closest pair, so the UI can draw the dimension line.
    """
    return measure_pairs([(result, r) for r in refs])


def measure_pairs(pairs: list[tuple[Any, dict[str, Any]]]) -> dict[str, Any]:
    """Like measure, but each selection is resolved in its own result (assembly components)."""
    import math

    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    resolved = [resolve(res, r) for res, r in pairs]
    bad = [r for r in resolved if r["status"] not in ("ok", "changed")]
    if bad:
        return {"ok": False, "message": bad[0]["reason"]}
    if len(resolved) not in (1, 2):
        return {"ok": False, "message": "select one or two entities to measure"}
    ents = [_entity(res.shape.val(), r) for (res, _), r in zip(pairs, resolved)]
    out: dict[str, Any] = {"ok": True, "entities": resolved}
    if len(ents) == 1:
        r = resolved[0]
        if r["kind"] == "vertex":
            out["x_mm"], out["y_mm"], out["z_mm"] = r["point"]
        elif r["kind"] == "face":
            out["area_mm2"] = r["area"]
            if "radius" in r:
                out["diameter_mm"] = _r(2 * r["radius"])
        else:
            out["length_mm"] = r["length"]
            if "radius" in r:
                out["diameter_mm"] = _r(2 * r["radius"])
        return out
    ext = BRepExtrema_DistShapeShape(ents[0].wrapped, ents[1].wrapped)
    if not (ext.IsDone() and ext.NbSolution() > 0):
        return {"ok": False, "message": "could not compute the distance between these entities"}
    p1, p2 = ext.PointOnShape1(1), ext.PointOnShape2(1)
    out["points"] = [[_r(p1.X()), _r(p1.Y()), _r(p1.Z())], [_r(p2.X()), _r(p2.Y()), _r(p2.Z())]]
    out["min_distance_mm"] = _r(ext.Value())
    out["dx_mm"], out["dy_mm"], out["dz_mm"] = (_r(abs(b - a)) for a, b in zip(*out["points"]))
    a, b = resolved
    if a["kind"] == b["kind"] == "face" and "normal" in a and "normal" in b:
        na, nb = cq.Vector(*a["normal"]), cq.Vector(*b["normal"])
        cosang = max(-1.0, min(1.0, na.dot(nb)))
        out["angle_deg"] = _r(math.degrees(math.acos(abs(cosang))))
        if abs(abs(cosang) - 1) < 1e-6:
            out["parallel_distance_mm"] = _r(abs((cq.Vector(*b["point"]) - cq.Vector(*a["point"])).dot(na)))
    if a["kind"] == b["kind"] == "edge" and a["type"] == b["type"] == "LINE":
        da, db = ((e.endPoint() - e.startPoint()).normalized() for e in ents)
        out["angle_deg"] = _r(math.degrees(math.acos(min(1.0, abs(da.dot(db))))))
    if "center" in a and "center" in b and "radius" in a and "radius" in b and a["kind"] == b["kind"] == "edge":
        out["center_distance_mm"] = _r((cq.Vector(*a["center"]) - cq.Vector(*b["center"])).Length)
        out["centers"] = [a["center"], b["center"]]
    elif all("axis_point" in r for r in resolved):  # two cylinders: distance between (parallel) axes
        pa, da = cq.Vector(*a["axis_point"]), cq.Vector(*a["axis_dir"])
        pb, db = cq.Vector(*b["axis_point"]), cq.Vector(*b["axis_dir"])
        if abs(abs(da.dot(db)) - 1) < 1e-6:
            d = pb - pa
            out["center_distance_mm"] = _r((d - da * d.dot(da)).Length)
            out["centers"] = [a["center"], b["center"]]
    return out
