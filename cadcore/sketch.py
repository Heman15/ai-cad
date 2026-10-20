# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constraint-based 2D sketches: a geometric constraint solver plus profile (loop) detection.

Sketch params (all coordinates are the sketch plane's (u, v) = global in-plane axes, see builder):

  points:      {"p1": [u, v], ...}                 initial positions (the solver moves them)
               "origin" is built in: fixed at (0, 0); use it as a line/arc end or in constraints
  entities:    [{"id": "l1", "type": "line",   "p1": "p1", "p2": "p2"},
                {"id": "c1", "type": "circle", "center": "p5", "r": 5},
                {"id": "a1", "type": "arc",    "center": "p6", "start": "p7", "end": "p8"},   # counter-clockwise
                {"id": "s1", "type": "spline", "points": ["p9", "p10", "p11"], "closed": false}, # through the points
                ... optional "construction": true (excluded from profiles)]
  constraints: [{"type": "coincident", "a": "p1", "b": "p2"}, {"type": "horizontal", "line": "l1"},
                {"type": "distance", "a": "p1", "b": "p2", "value": "=length"}, ...]

Constraint types and arguments:
  coincident a b (points) | horizontal line | horizontal a b (points) | vertical line | vertical a b
  distance a b value | horizontal_distance a b value | vertical_distance a b value | length line value
  radius circle value | diameter circle value | equal a b (lines: length, circles/arcs: radius)
  parallel a b | perpendicular a b | angle a b value (degrees, from line a to line b)
  fix point [at=[u, v]] | point_on_line point line | point_on_circle point circle | midpoint point line
  tangent line circle | concentric a b | symmetric a b line
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

Num = Callable[[Any, str], float]  # resolves numbers/formulas


class SketchError(Exception):
    pass


@dataclass
class SolveResult:
    points: dict[str, tuple[float, float]]
    radii: dict[str, float]
    converged: bool
    dof: int
    max_residual: float
    conflicts: list[int] = field(default_factory=list)  # indices of unsatisfied constraints
    redundant: bool = False

    @property
    def status(self) -> str:
        if not self.converged:
            return "conflicting"
        return "fully constrained" if self.dof == 0 else f"under-constrained ({self.dof} DOF)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "points": {k: [round(x, 9), round(y, 9)] for k, (x, y) in self.points.items()},
            "radii": {k: round(v, 9) for k, v in self.radii.items()},
            "converged": self.converged, "dof": self.dof, "status": self.status,
            "max_residual": self.max_residual, "conflicts": self.conflicts, "redundant": self.redundant,
        }


def _default_num(v: Any, what: str) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    raise SketchError(f"{what} must be a number")


class _Model:
    """Maps sketch params onto a flat unknown vector and residual functions."""

    def __init__(self, params: dict[str, Any], num: Num):
        pts = params.get("points") or {}
        if not isinstance(pts, dict):
            raise SketchError("points must be an object {id: [u, v]}")
        # "origin" is a built-in point fixed at the sketch plane's origin (0, 0); geometry may use it directly
        pts = {"origin": [0, 0], **{k: v for k, v in pts.items() if k != "origin"}}
        self.point_ids = list(pts)
        self.index: dict[str, int] = {pid: 2 * i for i, pid in enumerate(self.point_ids)}
        x0 = []
        for pid in self.point_ids:
            p = pts[pid]
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise SketchError(f"point {pid} must be [u, v]")
            x0 += [num(p[0], f"points.{pid}[0]"), num(p[1], f"points.{pid}[1]")]

        self.entities: dict[str, dict[str, Any]] = {}
        self.radius_index: dict[str, int] = {}
        for i, e in enumerate(params.get("entities") or []):
            eid, t = e.get("id"), e.get("type")
            if not eid or eid in self.entities or eid in self.index:
                raise SketchError(f"entities[{i}] needs a unique id")
            if t == "spline":
                pts_ = e.get("points") or []
                if len(pts_) < (3 if e.get("closed") else 2) or any(p not in self.index for p in pts_):
                    raise SketchError(f"entity {eid}: spline needs 2+ known point ids (3+ when closed)")
                self.entities[eid] = e
                continue
            refs = {"line": ("p1", "p2"), "circle": ("center",), "arc": ("center", "start", "end")}.get(t)
            if refs is None:
                raise SketchError(f"entity {eid}: type must be line, circle, arc or spline")
            for r in refs:
                if e.get(r) not in self.index:
                    raise SketchError(f"entity {eid}.{r} refers to unknown point {e.get(r)!r}")
            if t == "circle":
                self.radius_index[eid] = len(x0)
                x0.append(num(e.get("r", 1), f"{eid}.r"))
            self.entities[eid] = e
        self.x0 = np.array(x0, dtype=float)
        self.constraints = list(params.get("constraints") or [])
        self.fns: list[tuple[int, Callable[[np.ndarray], list[float]]]] = []
        for ci, c in enumerate(self.constraints):
            self.fns.append((ci, self._constraint(ci, c, num)))
        self.fns.append((-1, lambda x: list(self.P(x, "origin"))))  # origin stays at (0, 0)
        for eid, e in self.entities.items():  # arcs: start and end on the same circle
            if e["type"] == "arc":
                self.fns.append((-1, self._arc_closure(e)))

    # ---- accessors ----
    def P(self, x: np.ndarray, pid: str) -> np.ndarray:
        i = self.index[pid]
        return x[i:i + 2]

    def _point(self, c: dict, key: str, ci: int) -> str:
        pid = c.get(key)
        if pid not in self.index:
            raise SketchError(f"constraints[{ci}] ({c.get('type')}): {key} must be a point id, got {pid!r}")
        return pid

    def _entity(self, c: dict, key: str, ci: int, types: tuple[str, ...]) -> dict:
        e = self.entities.get(c.get(key))
        if e is None or e["type"] not in types:
            raise SketchError(f"constraints[{ci}] ({c.get('type')}): {key} must be a {'/'.join(types)} id, got {c.get(key)!r}")
        return e

    def line(self, x, e):
        return self.P(x, e["p1"]), self.P(x, e["p2"])

    def radius(self, x, e):
        if e["type"] == "circle":
            return x[self.radius_index[e["id"]]]
        return float(np.linalg.norm(self.P(x, e["start"]) - self.P(x, e["center"])))

    def _arc_closure(self, e):
        return lambda x: [float(np.linalg.norm(self.P(x, e["end"]) - self.P(x, e["center"]))
                                - np.linalg.norm(self.P(x, e["start"]) - self.P(x, e["center"])))]

    # ---- residuals ----
    def _constraint(self, ci: int, c: dict, num: Num) -> Callable[[np.ndarray], list[float]]:
        t = c.get("type")
        val = (lambda: num(c.get("value"), f"constraints[{ci}].value")) if "value" in c else None
        pt = lambda k: self._point(c, k, ci)  # noqa: E731
        ent = lambda k, *ty: self._entity(c, k, ci, ty)  # noqa: E731

        def unit(d):
            n = np.linalg.norm(d)
            return d / n if n > 1e-12 else d

        if t == "coincident":
            a, b = pt("a"), pt("b")
            return lambda x: list(self.P(x, a) - self.P(x, b))
        if t in ("horizontal", "vertical"):
            k = 1 if t == "horizontal" else 0
            if "line" in c:
                e = ent("line", "line")
                return lambda x: [self.line(x, e)[0][k] - self.line(x, e)[1][k]]
            a, b = pt("a"), pt("b")
            return lambda x: [self.P(x, a)[k] - self.P(x, b)[k]]
        if t in ("distance", "horizontal_distance", "vertical_distance"):
            a, b, v = pt("a"), pt("b"), val()
            if v < 0:
                raise SketchError(f"constraints[{ci}]: distance must not be negative")
            if t == "distance":
                return lambda x: [float(np.linalg.norm(self.P(x, b) - self.P(x, a))) - v]
            k = 0 if t == "horizontal_distance" else 1
            return lambda x: [abs(self.P(x, b)[k] - self.P(x, a)[k]) - v]
        if t == "length":
            e, v = ent("line", "line"), val()
            return lambda x: [float(np.linalg.norm(self.line(x, e)[1] - self.line(x, e)[0])) - v]
        if t in ("radius", "diameter"):
            e, v = ent("circle", "circle", "arc"), val()
            if v <= 0:
                raise SketchError(f"constraints[{ci}]: {t} must be positive")
            f = 2.0 if t == "diameter" else 1.0
            return lambda x: [f * self.radius(x, e) - v]
        if t == "equal":
            a, b = self.entities.get(c.get("a")), self.entities.get(c.get("b"))
            if not a or not b:
                raise SketchError(f"constraints[{ci}] (equal): a and b must be entity ids")
            if a["type"] == b["type"] == "line":
                return lambda x: [float(np.linalg.norm(np.subtract(*self.line(x, a))) - np.linalg.norm(np.subtract(*self.line(x, b))))]
            if a["type"] != "line" and b["type"] != "line":
                return lambda x: [self.radius(x, a) - self.radius(x, b)]
            raise SketchError(f"constraints[{ci}] (equal): compare two lines or two circles/arcs")
        if t in ("parallel", "perpendicular", "angle"):
            a, b = ent("a", "line"), ent("b", "line")
            if t == "angle":
                ang = math.radians(val())

            def r(x):
                da = unit(self.line(x, a)[1] - self.line(x, a)[0])
                db = unit(self.line(x, b)[1] - self.line(x, b)[0])
                cross, dot = da[0] * db[1] - da[1] * db[0], float(da @ db)
                if t == "parallel":
                    return [cross]
                if t == "perpendicular":
                    return [dot]
                return [cross * math.cos(ang) - dot * math.sin(ang)]  # sin(actual - wanted)
            return r
        if t == "fix":
            p = pt("point")
            at = c.get("at")
            target = np.array([num(at[0], f"constraints[{ci}].at[0]"), num(at[1], f"constraints[{ci}].at[1]")]) if at else self.P(self.x0, p).copy()
            return lambda x: list(self.P(x, p) - target)
        if t == "point_on_line":
            p, e = pt("point"), ent("line", "line")

            def r(x):
                a, b = self.line(x, e)
                d = unit(b - a)
                w = self.P(x, p) - a
                return [w[0] * d[1] - w[1] * d[0]]
            return r
        if t == "midpoint":
            p, e = pt("point"), ent("line", "line")
            return lambda x: list(self.P(x, p) - (self.line(x, e)[0] + self.line(x, e)[1]) / 2)
        if t == "point_on_circle":
            p, e = pt("point"), ent("circle", "circle", "arc")
            return lambda x: [float(np.linalg.norm(self.P(x, p) - self.P(x, e["center"]))) - self.radius(x, e)]
        if t == "tangent":
            ln, e = ent("line", "line"), ent("circle", "circle", "arc")
            shared = {ln["p1"], ln["p2"]} & ({e.get("start"), e.get("end")} - {None})
            if shared:  # line ends on the arc: radius at that point must be perpendicular to the line
                sp = shared.pop()

                def r(x):
                    a, b = self.line(x, ln)
                    return [float(unit(b - a) @ unit(self.P(x, sp) - self.P(x, e["center"])))]
                return r

            def r(x):
                a, b = self.line(x, ln)
                d = unit(b - a)
                w = self.P(x, e["center"]) - a
                return [abs(w[0] * d[1] - w[1] * d[0]) - self.radius(x, e)]
            return r
        if t == "concentric":
            a, b = ent("a", "circle", "arc"), ent("b", "circle", "arc")
            return lambda x: list(self.P(x, a["center"]) - self.P(x, b["center"]))
        if t == "symmetric":
            a, b, e = pt("a"), pt("b"), ent("line", "line")

            def r(x):
                p, q = self.line(x, e)
                d = unit(q - p)
                m = (self.P(x, a) + self.P(x, b)) / 2 - p
                ab = self.P(x, b) - self.P(x, a)
                return [m[0] * d[1] - m[1] * d[0], float(ab @ d)]
            return r
        raise SketchError(f"constraints[{ci}]: unknown constraint type {t!r}")

    def residuals(self, x: np.ndarray) -> np.ndarray:
        out: list[float] = []
        for _, f in self.fns:
            out += f(x)
        return np.array(out, dtype=float)

    def rows(self) -> list[int]:
        """Constraint index for each residual row."""
        x = self.x0
        return [ci for ci, f in self.fns for _ in f(x)]


def _jacobian(m: _Model, x: np.ndarray, r0: np.ndarray) -> np.ndarray:
    J = np.zeros((len(r0), len(x)))
    for j in range(len(x)):
        h = 1e-7 * max(1.0, abs(x[j]))
        xp = x.copy()
        xp[j] += h
        J[:, j] = (m.residuals(xp) - r0) / h
    return J


def solve(params: dict[str, Any], num: Num = _default_num, tol: float = 1e-9) -> SolveResult:
    m = _Model(params, num)
    x = m.x0.copy()
    if len(x) == 0:
        raise SketchError("sketch has no points")
    r = m.residuals(x) if m.fns else np.zeros(0)
    for _ in range(100):
        if len(r) == 0 or np.max(np.abs(r)) < tol:
            break
        J = _jacobian(m, x, r)
        # Minimum-norm Gauss-Newton step: moves the geometry as little as possible (stable for under-constrained sketches).
        step = np.linalg.lstsq(J, -r, rcond=None)[0]
        t, base = 1.0, float(r @ r)
        while t > 1e-4:
            xn = x + t * step
            rn = m.residuals(xn)
            if float(rn @ rn) < base:
                x, r = xn, rn
                break
            t /= 2
        else:
            break  # no progress: inconsistent constraints
    max_res = float(np.max(np.abs(r))) if len(r) else 0.0
    converged = max_res < 1e-6
    if len(r):
        J = _jacobian(m, x, r)
        rank = int(np.linalg.matrix_rank(J, tol=1e-6))
        rows = len(r)
    else:
        rank, rows = 0, 0
    conflicts = sorted({ci for ci, res in zip(m.rows(), r) if abs(res) > 1e-6 and ci >= 0}) if not converged else []
    points = {pid: (float(x[i]), float(x[i + 1])) for pid, i in m.index.items()}
    radii = {eid: float(m.radius(x, e)) for eid, e in m.entities.items() if e["type"] in ("circle", "arc")}
    return SolveResult(points, radii, converged, len(x) - rank, max_res, conflicts, redundant=rank < rows)


# ---------------- profiles ----------------

def profile_wires(params: dict[str, Any], sol: SolveResult):
    """Closed loops of non-construction geometry as CadQuery wires in plane (u, v, 0) coordinates."""
    import cadquery as cq

    def V(pid):
        u, v = sol.points[pid]
        return cq.Vector(u, v, 0)

    edges, closed = [], []
    for e in (params.get("entities") or []):
        if e.get("construction"):
            continue
        if e["type"] == "line":
            a, b = V(e["p1"]), V(e["p2"])
            if (a - b).Length > 1e-9:
                edges.append(cq.Edge.makeLine(a, b))
        elif e["type"] == "circle":
            closed.append(cq.Wire.assembleEdges([cq.Edge.makeCircle(sol.radii[e["id"]], V(e["center"]))]))
        elif e["type"] == "spline":
            edge = spline_edge(e, sol)
            if e.get("closed"):
                closed.append(cq.Wire.assembleEdges([edge]))
            else:
                edges.append(edge)
        elif e["type"] == "arc":
            c, s, t = V(e["center"]), V(e["start"]), V(e["end"])
            r = sol.radii[e["id"]]
            a0 = math.atan2(s.y - c.y, s.x - c.x)
            a1 = math.atan2(t.y - c.y, t.x - c.x)
            if a1 <= a0:
                a1 += 2 * math.pi
            mid = c + cq.Vector(math.cos((a0 + a1) / 2) * r, math.sin((a0 + a1) / 2) * r, 0)
            edges.append(cq.Edge.makeThreePointArc(s, mid, t))
    if edges:
        for w in cq.Wire.combine(edges, tol=1e-6):
            if not w.IsClosed():
                raise SketchError("the sketch has an open chain of lines/arcs; close it or mark it as construction")
            closed.append(w)
    if not closed:
        raise SketchError("the sketch has no closed profile")
    return closed


def path_wire(params: dict[str, Any], sol: SolveResult):
    """The sketch's non-construction curves joined into ONE wire (open or closed), for sweep paths."""
    import cadquery as cq

    def V(pid):
        u, v = sol.points[pid]
        return cq.Vector(u, v, 0)

    edges = []
    for e in params.get("entities") or []:
        if e.get("construction"):
            continue
        if e["type"] == "line":
            edges.append(cq.Edge.makeLine(V(e["p1"]), V(e["p2"])))
        elif e["type"] == "spline":
            edges.append(spline_edge(e, sol))
        elif e["type"] == "arc":
            c, s, t = V(e["center"]), V(e["start"]), V(e["end"])
            r = sol.radii[e["id"]]
            a0, a1 = math.atan2(s.y - c.y, s.x - c.x), math.atan2(t.y - c.y, t.x - c.x)
            if a1 <= a0:
                a1 += 2 * math.pi
            mid = c + cq.Vector(math.cos((a0 + a1) / 2) * r, math.sin((a0 + a1) / 2) * r, 0)
            edges.append(cq.Edge.makeThreePointArc(s, mid, t))
        elif e["type"] == "circle":
            edges.append(cq.Edge.makeCircle(sol.radii[e["id"]], V(e["center"])))
    if not edges:
        raise SketchError("the path sketch has no curves")
    wires = cq.Wire.combine(edges, tol=1e-6)
    if len(wires) != 1:
        raise SketchError(f"the path sketch must form one connected chain, found {len(wires)}")
    return wires[0]


def spline_edge(e: dict[str, Any], sol: SolveResult):
    """Interpolating spline through the entity's points (periodic when closed)."""
    import cadquery as cq

    pts = [cq.Vector(*sol.points[p], 0) for p in e["points"]]
    if e.get("closed") and e["points"][0] == e["points"][-1]:
        pts = pts[:-1]
    return cq.Edge.makeSpline(pts, periodic=bool(e.get("closed")))


def curve_samples(params: dict[str, Any], sol: SolveResult, n: int = 48) -> dict[str, list[list[float]]]:
    """Sampled polylines of spline entities, for display in the editor."""
    out = {}
    for e in params.get("entities") or []:
        if e.get("type") == "spline":
            try:
                edge = spline_edge(e, sol)
                out[e["id"]] = [[round(p.x, 6), round(p.y, 6)] for p in (edge.positionAt(i / n) for i in range(n + 1))]
            except Exception:
                pass
    return out


def profile_faces(params: dict[str, Any], sol: SolveResult):
    """Faces from closed loops; loops nested inside an odd number of others become holes."""
    import cadquery as cq

    faces = [cq.Face.makeFromWires(w) for w in profile_wires(params, sol)]
    faces.sort(key=lambda f: -f.Area())

    def inside(inner, outer):
        p = inner.outerWire().startPoint()
        return outer.Area() > inner.Area() and outer.isInside(p) if hasattr(outer, "isInside") else False

    depth = []
    for i, f in enumerate(faces):
        pt = f.outerWire().startPoint()
        d = 0
        for g in faces[:i]:
            if g.Area() > f.Area() and _point_in_face(g, pt):
                d += 1
        depth.append(d)
    sk = cq.Sketch()
    for f, d in zip(faces, depth):
        sk.face(f, mode="a" if d % 2 == 0 else "s")
    sk.clean()  # merge coplanar pieces of overlapping loops into single faces
    return sk._faces.Faces()


def _point_in_face(face, p) -> bool:
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    clf = BRepClass_FaceClassifier(face.wrapped, gp_Pnt(p.x, p.y, p.z), 1e-7)
    return clf.State() == TopAbs_IN
