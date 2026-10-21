# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turns a PartSpec feature tree into a validated solid.

Feature types and their params (lengths mm, angles degrees, any number may be "=formula"):

  extrude   plane, shapes, distance, op="add"|"cut", both=False, through=False
  revolve   plane, shapes, axis="u"|"v"|{origin:[u,v], dir:[du,dv]}, angle=360, op="add"|"cut"
  hole      plane, positions=[[u,v],...], diameter, depth=<mm>|"through",
            cbore_diameter, cbore_depth, csk_diameter, csk_angle=90
            Hole Wizard instead of diameter/cbore/csk/thread: standard="ISO", type="clearance"|"tapped"|"counterbore"|"countersink",
            size="M6" (tapped also "M8x1"), fit="close"|"normal"|"loose" (ISO 273 clearance; counterbore for ISO 4762
            socket head cap screws, countersink 90° for ISO 10642; tapped = tap drill + cosmetic thread, thread_depth optional)
  fillet    edges, radius
  variable_fillet  edge={"near": [x,y,z]} (exactly one edge), r1 (at the start), r2 (at the end),
            start=[x,y,z] (which end gets r1; default the edge's first vertex), points=[[t, r], ...] (radius r at
            fraction t in (0,1) along the edge from the start; optional), reverse (swap the ends)
  draft     faces (selector like "#Z", {"near": [pts]} or [{point, normal}, ...]), angle (degrees, 0 < |angle| <= 45),
            neutral_plane (plane; default "XY"; faces keep their size there), pull=[x,y,z] (default: the neutral plane's
            normal, or into the part for a face), flip. A positive angle narrows the part along the pull direction.
            Faces perpendicular to the pull, or that the kernel cannot taper, are reported by position.
  rib       sketch="<id>" (open lines, path_only) | plane + points=[[u,v],...], thickness,
            direction="parallel" (default: thickness across the sketch plane, material filled in the plane between
            the line and the part; open ends are extended along the line until they meet the part)
            | "normal" (thickness in the plane around the line, extruded along the plane normal until it meets the part),
            side="both"|"first"|"second" (parallel: first = toward the plane's normal; normal: first = left of the line),
            flip (when both sides of the line are enclosed, take the larger region). Fails if it does not reach material.
  chamfer   edges, distance
  shell     faces, thickness            (removes the selected faces, hollows inward)
  pattern_linear    feature, count, step=[dx,dy,dz], count2=1, step2=[dx,dy,dz]
  pattern_circular  feature, count, axis_point=[x,y,z], axis_dir=[x,y,z], angle=360
  mirror    feature, plane="XY"|"YZ"|"XZ", offset=0
  sweep     profile={sketch}|{plane, shapes}, path={sketch}|{points:[[x,y,z],..], type: polyline|spline}|{helix:{pitch,height,radius,center,dir}},
            op, transition="right"|"round", frenet     (the path must start on the profile plane)
  loft      sections=[{sketch}|{plane, shapes}, ...] (each one closed outline), ruled=False, op
  thread    designation="M8"|"M8x1", face={"near": [x,y,z]} on a cylindrical face, length, from="start"|"end",
            modeled=False (cosmetic annotation) | True (helical groove). Holes also take thread="M8" (+ thread_depth).
  sheet_base    plane + shapes | sketch: base flange extruded to the sheet thickness (needs set_sheet_metal first)
  sheet_flange  edge={"near": [x,y,z]} straight edge of the base, length (flat part after the bend), angle=90,
                direction="up"|"down", bend_radius (default: sheet), trim_start/trim_end (corner relief)
  sheet_hem     edge={"near": [x,y,z]} straight edge of the base, length (flat return after the fold), type="closed"|"open",
                gap (open hem: inside gap = twice the inner radius), direction="up"|"down", trim_start/trim_end. A 180° fold;
                a closed hem keeps a 0.02 mm gap so both layers stay one valid solid. Unfolds in the flat pattern.
  sketch    plane, points, entities, constraints   (constraint-solved 2D sketch; see sketch.py)
            extrude/revolve take "sketch": "<sketch id>" instead of plane + shapes

plane is one of:
  "XY" | "YZ" | "XZ"                    base plane through the origin
  {"base": "XY", "offset": 5}           plane z=5 (YZ: x=offset, XZ: y=offset)
  {"face": ">Z"}                        planar face of the part built so far (CadQuery selector)
  {"face_at": {"point", "normal"}}      the planar face containing that point (stable reference to a picked face)
  {"origin": [x,y,z], "normal": [x,y,z], "xdir": [x,y,z]}   any plane, also inclined
  All accept "offset" along the normal. Inclined planes use local (u, v) around their origin/face centre.

Directions: on a base plane, positive distance goes toward +axis (+Z for XY, +X for YZ,
+Y for XZ); use a negative distance to go the other way. Holes on a base plane drill
toward -axis. On a face, "add" grows outward and "cut"/holes go into the part.

Edge/face selectors are CadQuery strings ("|Z", ">Z", "<X or >X", ...), "all", or
"feature:<id>" for the edges/faces created by that feature.

shapes are 2D profiles in the plane's coordinates (u, v), each with mode "add" (default) or "subtract":
  {"type": "rect", "w", "h", "x", "y", "centered": true, "r": corner_radius, "angle"}
  {"type": "circle", "d", "x", "y"}
  {"type": "polygon", "points": [[u,v], ...], "r": corner_radius}
  {"type": "slot", "length" (overall), "w", "x", "y", "angle"}
  {"type": "regular_polygon", "d" (across corners), "n", "x", "y", "angle"}
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import cadquery as cq

from .expr import ExprError, convert_units, evaluate, names_in
from . import holes as _holes
from . import sheetmetal as _sm
from . import threads as _threads
from .sketch import SketchError, path_wire, profile_faces, solve
from .spec import Feature, PartSpec


class BuildError(Exception):
    """Raised when a spec cannot produce a valid solid. Message is user-facing."""


@dataclass
class HoleInfo:
    feature: str
    diameter: float
    depth: float | None  # None = through
    points: list[cq.Vector]
    direction: cq.Vector
    cbore: tuple[float, float] | None = None
    csk: tuple[float, float] | None = None
    thread: str | None = None  # e.g. "M8×1.25" (tapped hole; diameter is the tap drill)
    thread_depth: float | None = None
    standard: str | None = None  # Hole Wizard description, e.g. "M6 ISO 4762 C'BORE"


@dataclass
class ThreadInfo:
    feature: str
    label: str  # "M8×1.25"
    internal: bool
    modeled: bool
    axis_point: cq.Vector
    axis_dir: cq.Vector
    length: float


@dataclass
class BuildResult:
    shape: cq.Workplane
    volume_mm3: float
    bbox_mm: tuple[float, float, float]
    tools: list[tuple[str, cq.Shape]] = field(default_factory=list)  # (feature id, body it added/cut), build order
    created: list[tuple[str, list[cq.Face]]] = field(default_factory=list)  # (modifier id, faces it created)
    holes: list[HoleInfo] = field(default_factory=list)
    threads: list[ThreadInfo] = field(default_factory=list)
    sheet: Any = None  # sheetmetal.SheetState for sheet-metal parts
    variables: dict[str, float] = field(default_factory=dict)
    sketches: dict[str, dict[str, Any]] = field(default_factory=dict)  # sketch id -> solve report


_AXES = {"YZ": 0, "XZ": 1, "XY": 2}
_UNIT = [cq.Vector(1, 0, 0), cq.Vector(0, 1, 0), cq.Vector(0, 0, 1)]
_IN_PLANE = {0: (1, 2), 1: (0, 2), 2: (0, 1)}  # normal axis -> (u axis, v axis)
_EPS = 0.01


@dataclass
class PlaneInfo:
    """A sketch/feature plane. point(u, v) = origin + u*xdir + v*ydir.

    Planes parallel to XY/YZ/XZ (base planes and axis-aligned faces) use global in-plane
    coordinates: XY -> (x, y), YZ -> (y, z), XZ -> (x, z). Inclined planes use local
    coordinates around their origin (a face's centre, or the given origin).
    """
    origin: cq.Vector
    xdir: cq.Vector
    ydir: cq.Vector
    normal: cq.Vector  # positive extrusion direction (for faces: pointing out of the material)
    outward: int  # 1 for face planes, 0 for base/custom planes
    axis: int | None = None  # 0/1/2 when the plane is parallel to a base plane

    def point(self, u: float, v: float) -> cq.Vector:
        return self.origin + self.xdir * u + self.ydir * v

    def to_uv(self, p: cq.Vector) -> tuple[float, float]:
        d = p - self.origin
        return d.dot(self.xdir), d.dot(self.ydir)

    @property
    def cq_plane(self) -> cq.Plane:
        return cq.Plane(origin=self.origin.toTuple(), xDir=self.xdir.toTuple(), normal=self.xdir.cross(self.ydir).toTuple())

    def to_dict(self) -> dict[str, Any]:
        r = lambda v: [round(c, 9) + 0.0 for c in v.toTuple()]  # noqa: E731
        return {"origin": r(self.origin), "xdir": r(self.xdir), "ydir": r(self.ydir), "normal": r(self.normal), "axis": self.axis}


def _axis_plane(axis: int, coord: float, normal: cq.Vector | None = None, outward: int = 0) -> PlaneInfo:
    a, b = _IN_PLANE[axis]
    return PlaneInfo(_UNIT[axis] * coord, _UNIT[a], _UNIT[b], normal if normal is not None else _UNIT[axis], outward, axis)


def _frame(normal: cq.Vector, xdir: cq.Vector | None = None) -> tuple[cq.Vector, cq.Vector]:
    """Orthonormal in-plane axes for a normal; xdir defaults to global X projected onto the plane (Y if X is near the normal)."""
    n = normal.normalized()
    x = xdir if xdir is not None else (cq.Vector(1, 0, 0) if abs(n.x) < 0.9 else cq.Vector(0, 1, 0))
    x = (x - n * x.dot(n))
    if x.Length < 1e-9:
        raise ValueError("xdir is parallel to the normal")
    x = x.normalized()
    return x, n.cross(x)


class _Ctx:
    def __init__(self, spec: PartSpec):
        self.spec = spec
        self.vars = _eval_variables(spec.variables)
        for name, lim in spec.limits.items():
            v = self.vars.get(name)
            if v is None:
                continue
            if "min" in lim and v <= lim["min"]:
                raise BuildError(f"variable {name} = {v:g} must be greater than {lim['min']:g}")
            if "max" in lim and v > lim["max"]:
                raise BuildError(f"variable {name} = {v:g} must be at most {lim['max']:g}")
        self.shape: cq.Shape | None = None
        self.tools: dict[str, tuple[str, cq.Shape]] = {}  # fid -> (op, body)
        self.order: list[tuple[str, cq.Shape]] = []
        self.created: list[tuple[str, list[cq.Face]]] = []
        self.holes: list[HoleInfo] = []
        self.threads: list[ThreadInfo] = []
        self.sheet: _sm.SheetState | None = None
        if spec.sheet:
            self.fid = "sheet"
            self.sheet = _sm.SheetState(self.pos(spec.sheet.get("thickness"), "thickness"),
                                        self.pos(spec.sheet.get("bend_radius"), "bend_radius"),
                                        self.num(spec.sheet.get("k_factor", 0.44), "k_factor"))
            if not 0 <= self.sheet.K <= 0.5:
                raise BuildError("sheet.k_factor must be between 0 and 0.5")
        self.sketches: dict[str, tuple[PlaneInfo, list[cq.Face], dict[str, Any]]] = {}
        self.sketch_data: dict[str, tuple[dict[str, Any], Any]] = {}  # sketch id -> (params, solve result)
        # sketches that may be open lines (used by ribs)
        self.open_sketches = {f.params.get("sketch") for f in spec.features if f.type == "rib" and not f.suppressed}
        self.fid = ""

    def num(self, value: Any, what: str) -> float:
        if isinstance(value, bool) or value is None:
            raise BuildError(f"{self.fid}.{what} must be a number, got {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = convert_units(value.strip().lstrip("="))
            try:
                return float(text)
            except ValueError:
                pass
            try:
                return evaluate(text, self.vars)
            except ExprError as e:
                raise BuildError(f"{self.fid}.{what}: {e}")
        raise BuildError(f"{self.fid}.{what} must be a number, got {value!r}")

    def pos(self, value: Any, what: str) -> float:
        v = self.num(value, what)
        if v <= 0:
            raise BuildError(f"{self.fid}.{what} must be positive, got {v:g}")
        return v

    def vec(self, value: Any, what: str) -> cq.Vector:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise BuildError(f"{self.fid}.{what} must be [x, y, z]")
        return cq.Vector(*(self.num(c, f"{what}[{i}]") for i, c in enumerate(value)))

    @property
    def diag(self) -> float:
        if self.shape is None:
            return 1000.0
        return self.shape.BoundingBox().DiagonalLength + 10.0


def _eval_variables(variables: dict[str, Any]) -> dict[str, float]:
    """Evaluate variables in dependency order; reports cycles and unknown names explicitly."""
    out: dict[str, float] = {}
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(name: str, chain: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            cycle = chain[chain.index(name):] + [name]
            raise BuildError(f"circular dependency between variables: {' -> '.join(cycle)}")
        state[name] = 1
        v = variables[name]
        if isinstance(v, bool) or v is None:
            raise BuildError(f"variable {name} must be a number or formula, got {v!r}")
        if isinstance(v, (int, float)):
            out[name] = float(v)
        else:
            for dep in sorted(names_in(str(v))):
                if dep not in variables:
                    raise BuildError(f"variable {name}: unknown variable {dep!r} in {v!r}")
                visit(dep, chain + [name])
            try:
                out[name] = evaluate(str(v).strip().lstrip("="), out)
            except ExprError as e:
                raise BuildError(f"variable {name}: {e}")
        state[name] = 2

    for name in variables:
        visit(name, [])
    return out


# ---------------- planes & sketches ----------------

def _plane(ctx: _Ctx, spec: Any) -> PlaneInfo:
    if isinstance(spec, str):
        spec = {"base": spec}
    if not isinstance(spec, dict):
        raise BuildError(f"{ctx.fid}.plane must be \"XY\", {{\"base\":..., \"offset\":...}}, {{\"face\": selector}}, "
                         "{\"face_at\": ...} or {\"origin\": [x,y,z], \"normal\": [x,y,z]}")
    offset = ctx.num(spec.get("offset", 0), "plane.offset")
    if "face" in spec or "face_at" in spec:
        if ctx.shape is None:
            raise BuildError(f"{ctx.fid}: cannot use a face plane before any solid exists")
        if "face_at" in spec:
            f = _face_at(ctx, spec["face_at"])
        else:
            try:
                faces = cq.Workplane().add(ctx.shape).faces(spec["face"]).vals()
            except Exception as e:
                raise BuildError(f"{ctx.fid}.plane.face: bad selector {spec['face']!r} ({e})")
            planar = [f for f in faces if f.geomType() == "PLANE"]
            if len(planar) != 1:
                raise BuildError(f"{ctx.fid}.plane.face {spec['face']!r} matched {len(planar)} planar faces; it must match exactly one")
            f = planar[0]
        n = f.normalAt().normalized()
        t = n.toTuple()
        axis = max(range(3), key=lambda i: abs(t[i]))
        if abs(abs(t[axis]) - 1) < 1e-9:  # axis-aligned face: keep global in-plane coordinates
            coord = f.Center().toTuple()[axis] + (1 if t[axis] > 0 else -1) * offset
            return _axis_plane(axis, coord, normal=n, outward=1)
        x, y = _frame(n)
        return PlaneInfo(f.Center() + n * offset, x, y, n, 1, None)
    if "normal" in spec:  # custom (possibly inclined) plane
        try:
            n = ctx.vec(spec["normal"], "plane.normal").normalized()
            x, y = _frame(n, ctx.vec(spec["xdir"], "plane.xdir") if spec.get("xdir") is not None else None)
        except ValueError as e:
            raise BuildError(f"{ctx.fid}.plane: {e}")
        origin = ctx.vec(spec.get("origin", [0, 0, 0]), "plane.origin") + n * offset
        return PlaneInfo(origin, x, y, n, 0, None)
    base = str(spec.get("base", "")).upper()
    if base not in _AXES:
        raise BuildError(f"{ctx.fid}.plane.base must be XY, YZ or XZ, got {spec.get('base')!r}")
    return _axis_plane(_AXES[base], offset)


_REF_TOL = 0.05  # mm: how far a stored reference point may be from the entity it names


def _face_at(ctx: _Ctx, ref: Any) -> cq.Face:
    """Planar face containing `point` with outward normal `normal` (a stable reference to a selected face)."""
    if not isinstance(ref, dict):
        raise BuildError(f"{ctx.fid}.plane.face_at must be {{point: [x,y,z], normal: [x,y,z]}}")
    pt = ctx.vec(ref.get("point"), "plane.face_at.point")
    nrm = ctx.vec(ref.get("normal"), "plane.face_at.normal").normalized()
    vtx = cq.Vertex.makeVertex(*pt.toTuple())
    hits = [f for f in ctx.shape.Faces()
            if f.geomType() == "PLANE" and f.normalAt().normalized().dot(nrm) > 0.999 and f.distance(vtx) < _REF_TOL]
    if not hits:
        raise BuildError(f"{ctx.fid}: the referenced face (point {list(pt.toTuple())}, normal {list(nrm.toTuple())}) "
                         "no longer exists; re-select the face")
    return hits[0]


def _shape_faces(ctx: _Ctx, sh: dict[str, Any], i: int) -> list[cq.Face]:
    w = f"shapes[{i}]"
    t = sh.get("type")
    x, y = ctx.num(sh.get("x", 0), f"{w}.x"), ctx.num(sh.get("y", 0), f"{w}.y")
    angle = ctx.num(sh.get("angle", 0), f"{w}.angle")
    if t == "rect":
        rw, rh = ctx.pos(sh.get("w"), f"{w}.w"), ctx.pos(sh.get("h"), f"{w}.h")
        cx, cy = (x, y) if sh.get("centered", True) else (x + rw / 2, y + rh / 2)
        sk = cq.Sketch().push([(cx, cy)]).rect(rw, rh, angle=angle)
    elif t == "circle":
        sk = cq.Sketch().push([(x, y)]).circle(ctx.pos(sh.get("d"), f"{w}.d") / 2)
    elif t == "polygon":
        pts = sh.get("points") or []
        if len(pts) < 3:
            raise BuildError(f"{ctx.fid}.{w}.points needs at least 3 points")
        p = [(ctx.num(a, f"{w}.points[{j}][0]"), ctx.num(b, f"{w}.points[{j}][1]")) for j, (a, b) in enumerate(pts)]
        sk = cq.Sketch().polygon(p + [p[0]])
    elif t == "slot":
        length, sw = ctx.pos(sh.get("length"), f"{w}.length"), ctx.pos(sh.get("w"), f"{w}.w")
        if length <= sw:
            raise BuildError(f"{ctx.fid}.{w}: slot length must exceed its width")
        sk = cq.Sketch().push([(x, y)]).slot(length - sw, sw, angle=angle)
    elif t == "regular_polygon":
        n = int(ctx.num(sh.get("n"), f"{w}.n"))
        if n < 3:
            raise BuildError(f"{ctx.fid}.{w}.n must be at least 3")
        sk = cq.Sketch().push([(x, y)]).regularPolygon(ctx.pos(sh.get("d"), f"{w}.d") / 2, n, angle=angle)
    else:
        raise BuildError(f"{ctx.fid}.{w}.type must be rect, circle, polygon, slot or regular_polygon, got {t!r}")
    r = ctx.num(sh.get("r", 0), f"{w}.r")
    if r > 0 and t in ("rect", "polygon", "regular_polygon"):
        sk = sk.reset().vertices().fillet(r)
    return sk._faces.Faces()


def _profile(ctx: _Ctx, shapes: Any, plane: PlaneInfo) -> list[cq.Face]:
    if not isinstance(shapes, list) or not shapes:
        raise BuildError(f"{ctx.fid}.shapes must be a non-empty list")
    sk = cq.Sketch()
    for i, sh in enumerate(shapes):
        if not isinstance(sh, dict):
            raise BuildError(f"{ctx.fid}.shapes[{i}] must be an object")
        mode = sh.get("mode", "add")
        if mode not in ("add", "subtract"):
            raise BuildError(f"{ctx.fid}.shapes[{i}].mode must be add or subtract")
        if i == 0 and mode == "subtract":
            raise BuildError(f"{ctx.fid}.shapes[0] cannot be subtract")
        for f in _shape_faces(ctx, sh, i):
            sk.face(f, mode="a" if mode == "add" else "s")
    sk.clean()  # merge coplanar pieces of overlapping shapes into single faces
    loc = plane.cq_plane.location
    return [f.moved(loc) for f in sk._faces.Faces()]


def _one(solids: list[cq.Shape]) -> cq.Shape:
    return solids[0] if len(solids) == 1 else cq.Compound.makeCompound(solids)


def _fused(solids: list[cq.Shape]) -> cq.Shape:
    """Bodies from one profile may touch; fuse them so they act as one tool body."""
    if len(solids) == 1:
        return solids[0]
    return solids[0].fuse(*solids[1:]).clean()


# ---------------- features that add or remove a body ----------------

def _sketch_or_shapes(ctx: _Ctx, p: dict) -> tuple[PlaneInfo, list[cq.Face]]:
    if "sketch" in p:
        ref = p["sketch"]
        if ref not in ctx.sketches:
            raise BuildError(f"{ctx.fid}.sketch must name an earlier sketch feature, got {ref!r}")
        pl, faces, _ = ctx.sketches[ref]
        if not faces:
            raise BuildError(f"{ctx.fid}: sketch {ref} has no closed profile")
        return pl, faces
    pl = _plane(ctx, p.get("plane"))
    return pl, _profile(ctx, p.get("shapes"), pl)


def _f_sketch(ctx: _Ctx, p: dict) -> None:
    pl = _plane(ctx, p.get("plane", "XY"))
    try:
        sol = solve(p, lambda v, what: ctx.num(v, what))
    except SketchError as e:
        raise BuildError(f"{ctx.fid}: {e}")
    if not sol.converged:
        cons = p.get("constraints") or []
        names = ", ".join(f"#{i + 1} {cons[i].get('type')}" for i in sol.conflicts[:6])
        raise BuildError(f"{ctx.fid}: sketch constraints conflict ({names}); remove or change one of them")
    try:
        faces = profile_faces(p, sol)
    except SketchError as e:
        if not (p.get("path_only") or ctx.fid in ctx.open_sketches):
            raise BuildError(f"{ctx.fid}: {e} (set path_only for sketches used as sweep paths)")
        faces = []
    loc = pl.cq_plane.location
    report = {**sol.to_dict(), "plane": pl.to_dict()}
    ctx.sketches[ctx.fid] = (pl, [f.moved(loc) for f in faces], report)
    ctx.sketch_data[ctx.fid] = (p, sol)


def _f_extrude(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    op = p.get("op", "add")
    pl, faces = _sketch_or_shapes(ctx, p)
    n = pl.normal
    if p.get("through"):
        if op != "cut":
            raise BuildError(f"{ctx.fid}: through is only valid with op=cut")
        start, vec = -ctx.diag, n * (2 * ctx.diag)
    else:
        d = ctx.num(p.get("distance"), "distance")
        if d == 0:
            raise BuildError(f"{ctx.fid}.distance must not be zero")
        if pl.outward:  # face plane: add grows outward, cut goes into the part
            d = abs(d) * (pl.outward if op == "add" else -pl.outward)
        if p.get("both"):
            start, vec = -abs(d), n * (2 * abs(d))
        else:
            start, vec = 0.0, n * d
        if op == "cut":  # overshoot the starting plane slightly for a clean boolean
            sign = (vec.dot(n) > 0) - (vec.dot(n) < 0)
            start, vec = start - sign * _EPS, vec + n * (sign * _EPS)
    return op, _fused([cq.Solid.extrudeLinear(f.translate(n * start), vec) for f in faces])


def _f_revolve(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    pl, faces = _sketch_or_shapes(ctx, p)
    axis = p.get("axis", "v")
    if axis == "u":
        o, d = (0.0, 0.0), (1.0, 0.0)
    elif axis == "v":
        o, d = (0.0, 0.0), (0.0, 1.0)
    elif isinstance(axis, dict):
        o = tuple(ctx.num(c, "axis.origin") for c in axis.get("origin", [0, 0]))
        d = tuple(ctx.num(c, "axis.dir") for c in axis.get("dir", [0, 1]))
    else:
        raise BuildError(f"{ctx.fid}.axis must be \"u\", \"v\" or {{origin, dir}}")
    a, b = pl.point(*o), pl.point(o[0] + d[0], o[1] + d[1])
    angle = ctx.num(p.get("angle", 360), "angle")
    return p.get("op", "add"), _fused([cq.Solid.revolve(f, angle, a, b) for f in faces])


def _f_hole(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    pl = _plane(ctx, p.get("plane", {"face": ">Z"}))
    standard = None
    if p.get("standard") is not None:  # Hole Wizard: sizes come from the ISO tables
        try:
            p, standard = _holes.expand(p), _holes.label(p)
        except ValueError as e:
            raise BuildError(f"{ctx.fid}: {e}")
    thread = None
    if p.get("thread"):
        try:
            td, tp = _threads.parse(p["thread"])
        except ValueError as e:
            raise BuildError(f"{ctx.fid}.thread: {e}")
        thread = _threads.label(td, tp)
        if p.get("diameter") is None:
            p = {**p, "diameter": _threads.tap_drill(td, tp)}
    dia = ctx.pos(p.get("diameter"), "diameter")
    positions = p.get("positions") or []
    if not positions:
        raise BuildError(f"{ctx.fid}.positions must list at least one [u, v]")
    direction = pl.normal * (-pl.outward if pl.outward else -1)
    through = p.get("depth", "through") in ("through", None)
    depth = None if through else ctx.pos(p.get("depth"), "depth")
    cb = cs = None
    if p.get("cbore_diameter") is not None:
        cb = (ctx.pos(p["cbore_diameter"], "cbore_diameter"), ctx.pos(p.get("cbore_depth"), "cbore_depth"))
        if cb[0] <= dia:
            raise BuildError(f"{ctx.fid}.cbore_diameter must be larger than diameter")
    if p.get("csk_diameter") is not None:
        cs = (ctx.pos(p["csk_diameter"], "csk_diameter"), ctx.num(p.get("csk_angle", 90), "csk_angle"))
        if cs[0] <= dia:
            raise BuildError(f"{ctx.fid}.csk_diameter must be larger than diameter")

    bodies, points = [], []
    for i, pos in enumerate(positions):
        if not isinstance(pos, (list, tuple)) or len(pos) != 2:
            raise BuildError(f"{ctx.fid}.positions[{i}] must be [u, v]")
        pt = pl.point(ctx.num(pos[0], f"positions[{i}][0]"), ctx.num(pos[1], f"positions[{i}][1]"))
        points.append(pt)
        if pl.outward and not p.get("allow_breakout"):
            _check_hole_inside(ctx, pt, direction, max(dia, cb[0] if cb else 0, cs[0] if cs else 0), pos)
        if through:
            start, length = pt - direction * ctx.diag, 2 * ctx.diag
        else:
            start, length = pt - direction * _EPS, depth + _EPS
        body: cq.Shape = cq.Solid.makeCylinder(dia / 2, length, start, direction)
        top = pt - direction * _EPS
        if cb:
            body = body.fuse(cq.Solid.makeCylinder(cb[0] / 2, cb[1] + _EPS, top, direction))
        if cs:
            h = (cs[0] / 2) / math.tan(math.radians(cs[1] / 2))
            body = body.fuse(cq.Solid.makeCone(cs[0] / 2 + _EPS, 0, h + _EPS, top, direction))
        bodies.append(body)
    tdepth = ctx.pos(p["thread_depth"], "thread_depth") if thread and p.get("thread_depth") is not None else None
    if tdepth is not None and depth is not None and tdepth > depth + 1e-9:
        raise BuildError(f"{ctx.fid}.thread_depth {tdepth:g} is deeper than the hole depth {depth:g}")
    if cb and depth is not None and cb[1] >= depth:
        raise BuildError(f"{ctx.fid}: counterbore depth {cb[1]:g} must be less than the hole depth {depth:g}")
    ctx.holes.append(HoleInfo(ctx.fid, dia, depth, points, direction, cb, cs, thread, tdepth, standard))
    return "cut", _one(bodies)


def _check_hole_inside(ctx: _Ctx, pt: cq.Vector, direction: cq.Vector, dia: float, pos) -> None:
    """Reject holes whose rim leaves the face they are drilled from (set allow_breakout to permit)."""
    n = direction.normalized()
    helper = cq.Vector(1, 0, 0) if abs(n.x) < 0.9 else cq.Vector(0, 1, 0)
    a = n.cross(helper).normalized()
    b = n.cross(a)
    probe = pt + n * 0.05
    r = dia / 2 + 0.02
    samples = [probe] + [probe + a * (r * math.cos(k * math.pi / 8)) + b * (r * math.sin(k * math.pi / 8)) for k in range(16)]
    solid = ctx.shape.Solids()[0] if ctx.shape.Solids() else ctx.shape
    if not all(solid.isInside(s_, 1e-4) for s_ in samples):
        raise BuildError(f"{ctx.fid}: hole Ø{dia:g} at {list(pos)} breaks out of the face; move it or set allow_breakout")


def _source(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape, list[HoleInfo]]:
    ref = p.get("feature")
    if ref not in ctx.tools:
        raise BuildError(f"{ctx.fid}.feature must name an earlier extrude/revolve/hole/pattern/mirror, got {ref!r}")
    op, body = ctx.tools[ref]
    return op, body, [h for h in ctx.holes if h.feature == ref]


def _copy_holes(ctx: _Ctx, holes: list[HoleInfo], fn: Callable[[cq.Vector], cq.Vector], dir_fn: Callable[[cq.Vector], cq.Vector]):
    for h in holes:
        ctx.holes.append(HoleInfo(ctx.fid, h.diameter, h.depth, [fn(pt) for pt in h.points], dir_fn(h.direction),
                                  h.cbore, h.csk, h.thread, h.thread_depth, h.standard))


def _rot(v: cq.Vector, axis: cq.Vector, deg: float) -> cq.Vector:
    """Rotate vector v about a unit axis through the origin (Rodrigues)."""
    k = axis.normalized()
    t = math.radians(deg)
    return v * math.cos(t) + k.cross(v) * math.sin(t) + k * (k.dot(v) * (1 - math.cos(t)))


def _f_pattern_linear(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    op, body, holes = _source(ctx, p)
    n1 = int(ctx.num(p.get("count"), "count"))
    n2 = int(ctx.num(p.get("count2", 1), "count2"))
    if n1 < 1 or n2 < 1 or n1 * n2 < 2:
        raise BuildError(f"{ctx.fid}: counts must give at least 2 instances")
    s1 = ctx.vec(p.get("step"), "step")
    s2 = ctx.vec(p.get("step2", [0, 0, 0]), "step2")
    offsets = [s1 * i + s2 * j for i in range(n1) for j in range(n2) if i or j]
    for o in offsets:
        _copy_holes(ctx, holes, lambda v, o=o: v + o, lambda d: d)
    return op, _one([body.translate(o) for o in offsets])


def _f_pattern_circular(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    op, body, holes = _source(ctx, p)
    n = int(ctx.num(p.get("count"), "count"))
    if n < 2:
        raise BuildError(f"{ctx.fid}.count must be at least 2")
    origin = ctx.vec(p.get("axis_point", [0, 0, 0]), "axis_point")
    axis = ctx.vec(p.get("axis_dir", [0, 0, 1]), "axis_dir")
    total = ctx.num(p.get("angle", 360), "angle")
    step = total / n if abs(total - 360) < 1e-9 else total / (n - 1)
    copies = []
    for i in range(1, n):
        a = step * i
        copies.append(body.rotate(origin, origin + axis, a))
        _copy_holes(ctx, holes, lambda v, a=a: origin + _rot(v - origin, axis, a), lambda d, a=a: _rot(d, axis, a))
    return op, _one(copies)


def _f_mirror(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    op, body, holes = _source(ctx, p)
    plane = str(p.get("plane", "YZ")).upper()
    if plane not in _AXES:
        raise BuildError(f"{ctx.fid}.plane must be XY, YZ or XZ")
    axis = _AXES[plane]
    off = ctx.num(p.get("offset", 0), "offset")
    base = [0.0, 0.0, 0.0]
    base[axis] = off

    def flip(v: cq.Vector, about: float) -> cq.Vector:
        c = list(v.toTuple())
        c[axis] = 2 * about - c[axis]
        return cq.Vector(*c)
    _copy_holes(ctx, holes, lambda v: flip(v, off), lambda d: flip(d, 0))
    return op, body.mirror(plane, tuple(base))


def _path(ctx: _Ctx, spec: Any) -> cq.Wire:
    if not isinstance(spec, dict):
        raise BuildError(f"{ctx.fid}.path must be {{sketch}}, {{points, type}} or {{helix}}")
    if "sketch" in spec:
        ref = spec["sketch"]
        if ref not in ctx.sketch_data:
            raise BuildError(f"{ctx.fid}.path.sketch must name an earlier sketch, got {ref!r}")
        params, sol = ctx.sketch_data[ref]
        try:
            return path_wire(params, sol).moved(ctx.sketches[ref][0].cq_plane.location)
        except SketchError as e:
            raise BuildError(f"{ctx.fid}.path: {e}")
    if "helix" in spec:
        h = spec["helix"]
        pitch, height, radius = ctx.pos(h.get("pitch"), "path.helix.pitch"), ctx.pos(h.get("height"), "path.helix.height"), ctx.pos(h.get("radius"), "path.helix.radius")
        return cq.Wire.makeHelix(pitch, height, radius, ctx.vec(h.get("center", [0, 0, 0]), "path.helix.center"),
                                 ctx.vec(h.get("dir", [0, 0, 1]), "path.helix.dir"), lefthand=bool(h.get("lefthand")))
    pts = [ctx.vec(q, f"path.points[{i}]") for i, q in enumerate(spec.get("points") or [])]
    if len(pts) < 2:
        raise BuildError(f"{ctx.fid}.path.points needs at least 2 points")
    if spec.get("type", "polyline") == "spline":
        return cq.Wire.assembleEdges([cq.Edge.makeSpline(pts)])
    return cq.Wire.makePolygon(pts, close=False)


def _f_sweep(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    pl, faces = _sketch_or_shapes(ctx, p.get("profile") or {})
    path = _path(ctx, p.get("path"))
    start = path.startPoint()
    if abs((start - pl.origin).dot(pl.normal)) > 1e-3:
        raise BuildError(f"{ctx.fid}: the path must start on the profile's plane (it starts "
                         f"{abs((start - pl.origin).dot(pl.normal)):.3g} mm away)")
    transition = p.get("transition", "right")
    if transition not in ("right", "round", "transformed"):
        raise BuildError(f"{ctx.fid}.transition must be right, round or transformed")
    solids = [cq.Solid.sweep(f.outerWire(), f.innerWires(), path, makeSolid=True,
                             isFrenet=bool(p.get("frenet", "helix" in (p.get("path") or {}))), transitionMode=transition)
              for f in faces]
    for s_ in solids:
        if not s_.isValid() or s_.Volume() <= 0:
            raise BuildError(f"{ctx.fid}: sweep produced invalid geometry (profile too large for the path's bends?)")
    return p.get("op", "add"), _fused(solids)


def _f_loft(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    sections = p.get("sections") or []
    if len(sections) < 2:
        raise BuildError(f"{ctx.fid}.sections needs at least 2 sections")
    wires = []
    for i, sec in enumerate(sections):
        _, faces = _sketch_or_shapes(ctx, sec)
        if len(faces) != 1 or faces[0].innerWires():
            raise BuildError(f"{ctx.fid}.sections[{i}] must be one closed outline without holes")
        wires.append(faces[0].outerWire())
    solid = cq.Solid.makeLoft(wires, bool(p.get("ruled", False)))
    if not solid.isValid():
        raise BuildError(f"{ctx.fid}: loft produced invalid geometry; check section order and orientation")
    return p.get("op", "add"), solid


def _cylinder_face(ctx: _Ctx, ref: Any) -> cq.Face:
    if not isinstance(ref, dict) or "near" not in ref:
        raise BuildError(f"{ctx.fid}.face must be {{\"near\": [x, y, z]}} on a cylindrical face")
    pt = cq.Vertex.makeVertex(*ctx.vec(ref["near"], "face.near").toTuple())
    hits = sorted(((f.distance(pt), f) for f in ctx.shape.Faces() if f.geomType() == "CYLINDER"), key=lambda t: t[0])
    if not hits or hits[0][0] > _REF_TOL:
        raise BuildError(f"{ctx.fid}: no cylindrical face at {list(ref['near'])}; re-select the face")
    return hits[0][1]


def _f_thread(ctx: _Ctx, p: dict) -> None:
    """Thread on a cylindrical face: cosmetic (annotation) by default, or modelled as a helical groove."""
    try:
        d, pitch = _threads.parse(p.get("designation", ""))
    except ValueError as e:
        raise BuildError(f"{ctx.fid}.designation: {e}")
    face = _cylinder_face(ctx, p.get("face"))
    cyl = face._geomAdaptor().Cylinder()
    r = cyl.Radius()
    ax = cyl.Axis()
    a0, adir = cq.Vector(ax.Location().X(), ax.Location().Y(), ax.Location().Z()), cq.Vector(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()).normalized()
    # internal when the face normal points toward the axis (a hole), external for a shaft
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    ext = BRepExtrema_DistShapeShape(cq.Vertex.makeVertex(*ctx.vec(p["face"]["near"], "face.near").toTuple()).wrapped, face.wrapped)
    q = ext.PointOnShape2(1)
    on = cq.Vector(q.X(), q.Y(), q.Z())
    radial = (on - a0) - adir * (on - a0).dot(adir)
    internal = face.normalAt(on).dot(radial) < 0
    expected = _threads.tap_drill(d, pitch) / 2 if internal else d / 2
    if abs(r - expected) > max(0.15 * pitch, 0.05):
        kind = "tap drill" if internal else "major diameter"
        raise BuildError(f"{ctx.fid}: face Ø{2 * r:g} does not match {kind} Ø{2 * expected:g} for M{d:g}×{pitch:g}")
    # axial extent of the face
    bb = face.BoundingBox()
    corners = [cq.Vector(x, y, z) for x in (bb.xmin, bb.xmax) for y in (bb.ymin, bb.ymax) for z in (bb.zmin, bb.zmax)]
    ts = [(c - a0).dot(adir) for c in corners]
    t0, t1 = min(ts), max(ts)
    length = ctx.pos(p["length"], "length") if p.get("length") is not None else t1 - t0
    if length > t1 - t0 + 1e-6:
        raise BuildError(f"{ctx.fid}.length {length:g} exceeds the face length {t1 - t0:g}")
    from_end = p.get("from", "start")  # start: lower end along the axis direction
    start_t = t0 if from_end == "start" else t1 - length
    start = a0 + adir * start_t
    modeled = bool(p.get("modeled", False))
    if modeled:
        try:
            ctx.shape = _threads.cut_thread(ctx.shape, r, pitch, length, internal, start, adir)
        except Exception:
            raise BuildError(f"{ctx.fid}: modelled thread geometry failed in the kernel; use a cosmetic thread (modeled: false)")
    ctx.threads.append(ThreadInfo(ctx.fid, _threads.label(d, pitch), internal, modeled, start, adir, length))


def _need_sheet(ctx: _Ctx):
    if ctx.sheet is None:
        raise BuildError(f"{ctx.fid}: set the sheet-metal parameters first (thickness, bend radius, K-factor)")
    return ctx.sheet


def _f_sheet_base(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    st = _need_sheet(ctx)
    if st.base_faces_uv:
        raise BuildError(f"{ctx.fid}: the part already has a base flange")
    pl, faces = _sketch_or_shapes(ctx, p)
    st.origin, st.xdir, st.ydir, st.n = pl.origin, pl.xdir, pl.ydir, pl.normal
    loc_inv = pl.cq_plane.location.inverse
    st.base_faces_uv = [f.moved(loc_inv) for f in faces]
    return "add", _fused([cq.Solid.extrudeLinear(f, pl.normal * st.T) for f in faces])


def _f_sheet_flange(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    st = _need_sheet(ctx)
    if not st.base_faces_uv:
        raise BuildError(f"{ctx.fid}: add a base flange first")
    edge = p.get("edge") or {}
    if "near" not in edge:
        raise BuildError(f"{ctx.fid}.edge must be {{\"near\": [x, y, z]}} on a straight base edge")
    near = ctx.vec(edge["near"][0] if edge["near"] and isinstance(edge["near"][0], list) else edge["near"], "edge.near")
    radius = ctx.pos(p["bend_radius"], "bend_radius") if p.get("bend_radius") is not None else st.R
    try:
        fl = _sm.make_flange(st, ctx.shape, near, ctx.pos(p.get("length"), "length"), ctx.num(p.get("angle", 90), "angle"),
                             p.get("direction", "up") != "down", radius, ctx.num(p.get("trim_start", 0), "trim_start"),
                             ctx.num(p.get("trim_end", 0), "trim_end"), ctx.fid)
        _sm.check_collisions(st, fl)
    except ValueError as e:
        raise BuildError(f"{ctx.fid}: {e}")
    st.flanges.append(fl)
    return "add", fl.body


def _track_sheet_feature(ctx: _Ctx, f: Feature) -> None:
    """Record what the flat pattern can represent: holes/through-cuts normal to the base inside the base."""
    st = ctx.sheet
    if st is None or not st.base_faces_uv or f.type in ("sheet_base", "sheet_flange", "sheet_hem", "sketch"):
        return
    if f.type in ("hole", "extrude") and (f.type == "hole" or f.params.get("op") == "cut"):
        op_body = ctx.tools.get(f.id, (None, None))[1]
        if op_body is not None:
            bb = op_body.BoundingBox()
            corners = [cq.Vector(x, y, z) for x in (bb.xmin, bb.xmax) for y in (bb.ymin, bb.ymax) for z in (bb.zmin, bb.zmax)]
            hs = [st.h(c) for c in corners]
            if min(hs) <= 1e-6 and max(hs) >= st.T - 1e-6:  # goes through the full thickness of the base
                inside_base = all(any(_sm_inside_uv(face, *st.uv(c)) for face in st.base_faces_uv) for c in corners)
                if inside_base:
                    sec = op_body.intersect(cq.Solid.extrudeLinear(
                        cq.Face.makePlane(1e5, 1e5, st.origin + st.n * (st.T / 2), st.n), st.n * 1e-3))
                    for face in sec.Faces():
                        if abs(face.normalAt().dot(st.n)) > 0.999 and abs(st.h(face.Center()) - st.T / 2) < 1e-6:
                            to_uv = cq.Plane(st.origin.toTuple(), st.xdir.toTuple(), st.xdir.cross(st.ydir).toTuple()).location.inverse
                            st.cuts_uv.append(face.translate(st.n * (-st.T / 2)).moved(to_uv))
                    return
    st.unsupported.append(f"{f.id} ({f.type}) is not a through-cut in the base flange")


def _sm_inside_uv(face: cq.Face, u: float, v: float) -> bool:
    bb = face.BoundingBox()
    return bb.xmin - 1e-6 <= u <= bb.xmax + 1e-6 and bb.ymin - 1e-6 <= v <= bb.ymax + 1e-6


# ---------------- features that modify the existing solid ----------------

def _select(ctx: _Ctx, selector: Any, kind: str) -> list[cq.Shape]:
    items_all = ctx.shape.Edges() if kind == "edges" else ctx.shape.Faces()
    if isinstance(selector, dict) and "near" in selector:
        # Stable reference: one entity per stored point; refuse rather than guess if it moved.
        found = []
        for i, raw in enumerate(selector["near"] or []):
            pt = cq.Vertex.makeVertex(*ctx.vec(raw, f"{kind}.near[{i}]").toTuple())
            dists = sorted(((e.distance(pt), j) for j, e in enumerate(items_all)), key=lambda t: t[0])
            if not dists or dists[0][0] > _REF_TOL:
                raise BuildError(f"{ctx.fid}.{kind}.near[{i}] {list(raw)}: the referenced {kind[:-1]} no longer exists; re-select it")
            if len(dists) > 1 and dists[1][0] < _REF_TOL and kind == "faces":
                raise BuildError(f"{ctx.fid}.{kind}.near[{i}] is ambiguous; re-select the {kind[:-1]}")
            if items_all[dists[0][1]] not in found:
                found.append(items_all[dists[0][1]])
        if not found:
            raise BuildError(f"{ctx.fid}.{kind}.near is empty")
        return found
    if not isinstance(selector, str) or not selector:
        raise BuildError(f"{ctx.fid}.{kind} must be a selector string")
    items = ctx.shape.Edges() if kind == "edges" else ctx.shape.Faces()
    if selector == "all":
        return items
    if selector.startswith("feature:"):
        ref = selector.split(":", 1)[1]
        if ref not in ctx.tools:
            raise BuildError(f"{ctx.fid}.{kind}: no earlier feature {ref!r}")
        box = ctx.tools[ref][1].BoundingBox()
        found = [e for e in items if _inside(e.BoundingBox(), box)]
    else:
        wp = cq.Workplane().add(ctx.shape)
        try:
            found = (wp.edges(selector) if kind == "edges" else wp.faces(selector)).vals()
        except Exception as e:
            raise BuildError(f"{ctx.fid}.{kind}: bad selector {selector!r} ({e})")
    if not found:
        raise BuildError(f"{ctx.fid}.{kind} {selector!r} matched nothing")
    return found


def _f_fillet(ctx: _Ctx, p: dict) -> None:
    r = ctx.pos(p.get("radius"), "radius")
    edges = _select(ctx, p.get("edges"), "edges")
    try:
        ctx.shape = ctx.shape.fillet(r, edges)
    except Exception:
        raise BuildError(f"{ctx.fid}: fillet radius {r:g} failed on {len(edges)} edge(s); try a smaller radius or fewer edges")


def _f_chamfer(ctx: _Ctx, p: dict) -> None:
    d = ctx.pos(p.get("distance"), "distance")
    edges = _select(ctx, p.get("edges"), "edges")
    try:
        ctx.shape = ctx.shape.chamfer(d, None, edges)
    except Exception:
        raise BuildError(f"{ctx.fid}: chamfer {d:g} failed on {len(edges)} edge(s); try a smaller size")


def _f_shell(ctx: _Ctx, p: dict) -> None:
    t = ctx.pos(p.get("thickness"), "thickness")
    faces = _select(ctx, p.get("faces"), "faces")
    try:
        ctx.shape = cq.Workplane().add(ctx.shape).newObject(faces).shell(-t).findSolid()
    except Exception:
        raise BuildError(f"{ctx.fid}: shell with wall {t:g} failed; try a thinner wall")


def _faces_ref(ctx: _Ctx, sel: Any) -> list[cq.Face]:
    """Faces by selector, {"near": [points]}, or a list of {point, normal} references (picked faces)."""
    if isinstance(sel, list) and sel and all(isinstance(r, dict) for r in sel):
        found = []
        for i, ref in enumerate(sel):
            pt = ctx.vec(ref.get("point"), f"faces[{i}].point")
            nrm = ctx.vec(ref["normal"], f"faces[{i}].normal").normalized() if ref.get("normal") is not None else None
            v = cq.Vertex.makeVertex(*pt.toTuple())
            hits = [f for f in ctx.shape.Faces() if f.distance(v) < _REF_TOL
                    and (nrm is None or f.normalAt(_closest_on(f, pt)).normalized().dot(nrm) > 0.999)]
            if not hits:
                raise BuildError(f"{ctx.fid}.faces[{i}] at {list(pt.toTuple())}: the referenced face no longer exists; re-select it")
            if hits[0] not in found:
                found.append(hits[0])
        return found
    return [f for f in _select(ctx, sel, "faces")]


def _closest_on(shape: cq.Shape, pt: cq.Vector) -> cq.Vector:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    ext = BRepExtrema_DistShapeShape(cq.Vertex.makeVertex(*pt.toTuple()).wrapped, shape.wrapped)
    q = ext.PointOnShape2(1)
    return cq.Vector(q.X(), q.Y(), q.Z())


def _where(f: cq.Shape) -> str:
    return f"{f.geomType()} face at [{', '.join(f'{round(c, 4) + 0.0:g}' for c in f.Center().toTuple())}]"


def _f_draft(ctx: _Ctx, p: dict) -> None:
    """Taper faces by an angle about a neutral plane (OCC BRepOffsetAPI_DraftAngle)."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    angle = ctx.num(p.get("angle"), "angle")
    if angle == 0 or abs(angle) > 45:
        raise BuildError(f"{ctx.fid}.angle must be between -45 and 45 degrees and not 0, got {angle:g}")
    pl = _plane(ctx, p.get("neutral_plane", "XY"))
    if p.get("pull") is not None:
        pull = ctx.vec(p["pull"], "pull")
        if pull.Length < 1e-9:
            raise BuildError(f"{ctx.fid}.pull must not be zero")
        pull = pull.normalized()
    else:  # base/custom plane: its normal; face plane: into the part
        pull = pl.normal * (-1 if pl.outward else 1)
    if p.get("flip"):
        pull = -pull
    normal = pl.xdir.cross(pl.ydir)
    if abs(normal.dot(pull)) < 1e-6:
        raise BuildError(f"{ctx.fid}.pull lies in the neutral plane; it must cross it")
    faces = _faces_ref(ctx, p.get("faces"))
    for f in faces:
        if f.geomType() == "PLANE" and abs(f.normalAt().normalized().dot(pull)) > 1 - 1e-9:
            raise BuildError(f"{ctx.fid}: {_where(f)} is perpendicular to the pull direction and cannot be drafted")
    plane = gp_Pln(gp_Pnt(*pl.origin.toTuple()), gp_Dir(*normal.toTuple()))
    bad = []
    for f in faces:  # each face on its own first, so every face that cannot be tapered is reported
        probe = BRepOffsetAPI_DraftAngle(ctx.shape.wrapped)
        probe.Add(f.wrapped, gp_Dir(*pull.toTuple()), math.radians(angle), plane, True)
        if not probe.AddDone():
            bad.append(_where(f))
    if bad:
        raise BuildError(f"{ctx.fid}: cannot draft {len(bad)} of {len(faces)} face(s): {'; '.join(bad)} "
                         "(the kernel cannot taper them about this neutral plane and pull direction)")
    mk = BRepOffsetAPI_DraftAngle(ctx.shape.wrapped)
    for f in faces:
        mk.Add(f.wrapped, gp_Dir(*pull.toTuple()), math.radians(angle), plane, True)
        if not mk.AddDone():
            raise BuildError(f"{ctx.fid}: {_where(f)} cannot be drafted together with the other faces; draft it in a separate feature")
    mk.Build()
    if not mk.IsDone():
        raise BuildError(f"{ctx.fid}: draft of {len(faces)} face(s) by {angle:g}° failed in the kernel; try a smaller angle")
    out = cq.Shape.cast(mk.Shape())
    if not out.isValid() or not out.Solids():
        raise BuildError(f"{ctx.fid}: draft produced invalid geometry; try a smaller angle or fewer faces")
    ctx.shape = out


def _f_variable_fillet(ctx: _Ctx, p: dict) -> None:
    """Fillet one edge with a radius varying from r1 (start) to r2 (end), optionally through intermediate radii."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.gp import gp_Pnt2d
    from OCP.TColgp import TColgp_Array1OfPnt2d

    sel = p.get("edge")
    if isinstance(sel, dict) and isinstance(sel.get("near"), list) and sel["near"] and not isinstance(sel["near"][0], list):
        sel = {"near": [sel["near"]]}  # a single point
    edges = _select(ctx, sel, "edges")
    if len(edges) != 1:
        raise BuildError(f"{ctx.fid}.edge must name exactly one edge, matched {len(edges)}")
    r1, r2 = ctx.pos(p.get("r1"), "r1"), ctx.pos(p.get("r2"), "r2")
    law = [(0.0, r1)]
    for i, q in enumerate(p.get("points") or []):
        if not isinstance(q, (list, tuple)) or len(q) != 2:
            raise BuildError(f"{ctx.fid}.points[{i}] must be [t, radius] with t in (0, 1) along the edge")
        t, r = ctx.num(q[0], f"points[{i}][0]"), ctx.pos(q[1], f"points[{i}][1]")
        if not law[-1][0] < t < 1:
            raise BuildError(f"{ctx.fid}.points: t values must increase strictly between 0 and 1")
        law.append((t, r))
    law.append((1.0, r2))
    mk = BRepFilletAPI_MakeFillet(ctx.shape.wrapped)
    mk.Add(edges[0].wrapped)
    first = BRep_Tool.Pnt_s(mk.FirstVertex(1))
    last = BRep_Tool.Pnt_s(mk.LastVertex(1))
    if p.get("start") is not None:  # r1 belongs to the end nearest to `start`
        s = ctx.vec(p["start"], "start")
        d_first = (cq.Vector(first.X(), first.Y(), first.Z()) - s).Length
        d_last = (cq.Vector(last.X(), last.Y(), last.Z()) - s).Length
        if d_last < d_first:
            law = [(1 - t, r) for t, r in reversed(law)]
    if p.get("reverse"):  # swap the ends
        law = [(1 - t, r) for t, r in reversed(law)]
    if len(law) == 2:
        mk.SetRadius(law[0][1], law[1][1], 1, 1)
    else:
        arr = TColgp_Array1OfPnt2d(1, len(law))
        for i, (t, r) in enumerate(law):
            arr.SetValue(i + 1, gp_Pnt2d(t, r))
        mk.SetRadius(arr, 1, 1)
    try:
        mk.Build()
        ok = mk.IsDone()
    except Exception:
        ok = False
    out = cq.Shape.cast(mk.Shape()) if ok else None
    if out is None or not out.isValid() or not out.Solids():
        raise BuildError(f"{ctx.fid}: variable fillet {r1:g}→{r2:g} failed on the edge; try smaller radii")
    ctx.shape = out


def _chain_points(edges: list[cq.Edge]) -> list[cq.Vector]:
    """Ordered points of a connected chain of straight edges (open polyline)."""
    if any(e.geomType() != "LINE" for e in edges):
        raise ValueError("rib sketches must contain straight lines only")
    segs = [(e.startPoint(), e.endPoint()) for e in edges]
    pts = list(segs.pop(0))
    while segs:
        for i, (a, b) in enumerate(segs):
            if (a - pts[-1]).Length < 1e-6:
                pts.append(b)
            elif (b - pts[-1]).Length < 1e-6:
                pts.append(a)
            elif (b - pts[0]).Length < 1e-6:
                pts.insert(0, a)
            elif (a - pts[0]).Length < 1e-6:
                pts.insert(0, b)
            else:
                continue
            segs.pop(i)
            break
        else:
            raise ValueError("the rib lines must form one connected chain")
    if (pts[0] - pts[-1]).Length < 1e-6:
        raise ValueError("the rib sketch must be an open line, not a closed loop")
    return pts


def _offset_polyline(uv: list[tuple[float, float]], left: float, right: float) -> list[tuple[float, float]]:
    """Closed outline around a 2D polyline: `left` mm to its left and `right` mm to its right (mitred corners)."""
    def side(dist: float) -> list[tuple[float, float]]:
        lines = []
        for (x0, y0), (x1, y1) in zip(uv, uv[1:]):
            dx, dy = x1 - x0, y1 - y0
            ln = math.hypot(dx, dy)
            nx, ny = -dy / ln * dist, dx / ln * dist
            lines.append(((x0 + nx, y0 + ny), (x1 + nx, y1 + ny)))
        out = [lines[0][0]]
        for (a0, a1), (b0, b1) in zip(lines, lines[1:]):
            d1 = (a1[0] - a0[0], a1[1] - a0[1])
            d2 = (b1[0] - b0[0], b1[1] - b0[1])
            den = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(den) < 1e-12:
                out.append(a1)
                continue
            t = ((b0[0] - a0[0]) * d2[1] - (b0[1] - a0[1]) * d2[0]) / den
            out.append((a0[0] + d1[0] * t, a0[1] + d1[1] * t))
        out.append(lines[-1][1])
        return out
    return side(left) + list(reversed(side(-right)))


def _rib_piece(ctx: _Ctx, tool: cq.Shape, probe: cq.Vector, axis: cq.Vector, origin: cq.Vector, reach: float):
    """The part of `tool` outside the body that contains `probe`, if it is closed off by material before `reach`."""
    pieces = [s_ for s_ in tool.cut(ctx.shape).Solids() if s_.isInside(probe, 1e-6)]
    if not pieces:
        return None
    piece = pieces[0]
    far = max((v.Center() - origin).dot(axis) for v in piece.Vertices())
    return piece if far < reach * 0.999 else None


def _f_rib(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    """Thin wall from an open sketch line, extended to the existing material (like a SolidWorks rib)."""
    if ctx.shape is None:
        raise BuildError(f"{ctx.fid}: a rib needs an existing solid to reach")
    t = ctx.pos(p.get("thickness"), "thickness")
    side = p.get("side", "both")
    if side not in ("both", "first", "second"):
        raise BuildError(f"{ctx.fid}.side must be both, first or second")
    mode = p.get("direction", "parallel")
    if mode not in ("parallel", "normal"):
        raise BuildError(f"{ctx.fid}.direction must be parallel or normal")
    try:
        if "sketch" in p:
            ref = p["sketch"]
            if ref not in ctx.sketch_data:
                raise BuildError(f"{ctx.fid}.sketch must name an earlier sketch, got {ref!r}")
            params, sol = ctx.sketch_data[ref]
            pl = ctx.sketches[ref][0]
            local = _chain_points(path_wire(params, sol).Edges())
            uv = [(q.x, q.y) for q in local]
        else:
            pl = _plane(ctx, p.get("plane"))
            raw = p.get("points") or []
            if len(raw) < 2:
                raise BuildError(f"{ctx.fid}.points needs at least 2 [u, v] points (an open line)")
            uv = [(ctx.num(a, f"points[{i}][0]"), ctx.num(b, f"points[{i}][1]")) for i, (a, b) in enumerate(raw)]
            _chain_points([cq.Edge.makeLine(cq.Vector(*a, 0), cq.Vector(*b, 0)) for a, b in zip(uv, uv[1:])])
    except (SketchError, ValueError) as e:
        raise BuildError(f"{ctx.fid}: {e}")
    if any(math.dist(a, b) < 1e-6 for a, b in zip(uv, uv[1:])):
        raise BuildError(f"{ctx.fid}: the rib line has a zero-length segment")
    n = pl.normal.normalized()  # "first" side: toward the plane's positive / outward normal
    reach = 2 * ctx.diag
    solid = ctx.shape.Solids()[0] if ctx.shape.Solids() else ctx.shape
    ok = lambda q: solid.isInside(q, 1e-4)  # noqa: E731

    if mode == "parallel":
        # extend open ends along their segments until they meet material (as a rib sketch is extended)
        for end in (0, -1):
            a, b = (uv[1], uv[0]) if end == 0 else (uv[-2], uv[-1])
            pa, pb = pl.point(*a), pl.point(*b)
            if ok(pb):
                continue
            ray = cq.Edge.makeLine(pb, pb + (pb - pa).normalized() * reach)
            hits = [v.Center() for v in solid.intersect(ray).Vertices()]
            if not hits:
                raise BuildError(f"{ctx.fid}: the rib line end at {[round(c, 4) for c in pb.toTuple()]} does not reach the "
                                 "part when extended; move it onto (or toward) the material")
            q = min(hits, key=lambda h: (h - pb).Length)
            ext = pl.to_uv(q)
            uv[end] = ext
        z0 = {"both": -t / 2, "first": 0.0, "second": -t}[side]
        pts3 = [pl.point(*q) + n * z0 for q in uv]
        chord = (pts3[-1] - pts3[0])
        candidates = []
        for s in (1, -1):
            m = n.cross(chord).normalized() * s
            region = cq.Face.makeFromWires(cq.Wire.makePolygon(pts3 + [pts3[-1] + m * reach, pts3[0] + m * reach], close=True))
            slab = cq.Solid.extrudeLinear(region, n * t)
            a, b = pts3[0], pts3[1]
            probe = (a + b) * 0.5 + n * (t / 2) + m * min(0.05, t / 4)
            piece = _rib_piece(ctx, slab, probe, m, pts3[0], reach)
            if piece is not None:
                candidates.append(piece)
    else:
        lo, hi = {"both": (t / 2, t / 2), "first": (t, 0.0), "second": (0.0, t)}[side]
        outline = _offset_polyline(uv, lo, hi)
        strip = cq.Face.makeFromWires(cq.Wire.makePolygon([pl.point(*q) for q in outline], close=True))
        mid = pl.point(*((uv[0][0] + uv[1][0]) / 2, (uv[0][1] + uv[1][1]) / 2))
        x0, y0 = uv[0]
        x1, y1 = uv[1]
        ln = math.hypot(x1 - x0, y1 - y0)
        mid = mid + (pl.xdir * (-(y1 - y0) / ln) + pl.ydir * ((x1 - x0) / ln)) * ((lo - hi) / 2)
        candidates = []
        for s in (1, -1):
            prism = cq.Solid.extrudeLinear(strip, n * (s * reach))
            piece = _rib_piece(ctx, prism, mid + n * (s * min(0.05, t / 4)), n * s, mid, reach)
            if piece is not None:
                candidates.append(piece)
    if not candidates:
        raise BuildError(f"{ctx.fid}: the rib does not reach material on either side of the sketch line "
                         f"({'in the sketch plane' if mode == 'parallel' else 'along the sketch normal'}); "
                         "move the line so the region between it and the part is closed off by the part")
    candidates.sort(key=lambda s_: s_.Volume())
    return "add", candidates[1 if p.get("flip") and len(candidates) > 1 else 0]


def _f_sheet_hem(ctx: _Ctx, p: dict) -> tuple[str, cq.Shape]:
    """180° fold on a base edge: open hem (inside gap) or closed hem (folded flat)."""
    st = _need_sheet(ctx)
    if not st.base_faces_uv:
        raise BuildError(f"{ctx.fid}: add a base flange first")
    kind = p.get("type", "closed")
    if kind not in ("closed", "open"):
        raise BuildError(f"{ctx.fid}.type must be closed or open")
    if kind == "open":
        gap = ctx.pos(p.get("gap"), "gap")
    else:
        if p.get("gap") not in (None, 0):
            raise BuildError(f"{ctx.fid}: a closed hem has no gap; use type open")
        gap = _sm.CLOSED_HEM_GAP
    edge = p.get("edge") or {}
    if "near" not in edge:
        raise BuildError(f"{ctx.fid}.edge must be {{\"near\": [x, y, z]}} on a straight base edge")
    near = ctx.vec(edge["near"][0] if edge["near"] and isinstance(edge["near"][0], list) else edge["near"], "edge.near")
    try:
        fl = _sm.make_flange(st, ctx.shape, near, ctx.pos(p.get("length"), "length"), 180.0,
                             p.get("direction", "up") != "down", gap / 2, ctx.num(p.get("trim_start", 0), "trim_start"),
                             ctx.num(p.get("trim_end", 0), "trim_end"), ctx.fid)
        fl.kind = "hem"
        _sm.check_collisions(st, fl)
    except ValueError as e:
        raise BuildError(f"{ctx.fid}: {e}")
    common = fl.body.intersect(ctx.shape)
    if common.Solids() and common.Volume() > 1e-6:
        raise BuildError(f"{ctx.fid}: the hem runs into other material of the part; shorten it or trim its ends")
    st.flanges.append(fl)
    return "add", fl.body


_TOOL_FEATURES = {
    "extrude": _f_extrude, "revolve": _f_revolve, "hole": _f_hole, "sweep": _f_sweep, "loft": _f_loft,
    "sheet_base": _f_sheet_base, "sheet_flange": _f_sheet_flange, "sheet_hem": _f_sheet_hem, "rib": _f_rib,
    "pattern_linear": _f_pattern_linear, "pattern_circular": _f_pattern_circular, "mirror": _f_mirror,
}
_MODIFIERS = {"fillet": _f_fillet, "chamfer": _f_chamfer, "shell": _f_shell, "thread": _f_thread,
              "draft": _f_draft, "variable_fillet": _f_variable_fillet}
FEATURE_TYPES = sorted([*_TOOL_FEATURES, *_MODIFIERS, "sketch"])


def _inside(inner: cq.BoundBox, outer: cq.BoundBox, tol: float = 0.05) -> bool:
    return (
        inner.xmin >= outer.xmin - tol and inner.xmax <= outer.xmax + tol
        and inner.ymin >= outer.ymin - tol and inner.ymax <= outer.ymax + tol
        and inner.zmin >= outer.zmin - tol and inner.zmax <= outer.zmax + tol
    )


def _apply(ctx: _Ctx, op: str, body: cq.Shape) -> None:
    if op == "add":
        ctx.shape = body if ctx.shape is None else ctx.shape.fuse(body).clean()
    elif op == "cut":
        if ctx.shape is None:
            raise BuildError(f"{ctx.fid}: nothing to cut yet; the first feature must add material")
        ctx.shape = ctx.shape.cut(body).clean()
    else:
        raise BuildError(f"{ctx.fid}.op must be add or cut, got {op!r}")


def _build_feature(ctx: _Ctx, f: Feature) -> None:
    if f.type == "sketch":
        _f_sketch(ctx, f.params)
    elif f.type in _TOOL_FEATURES:
        op, body = _TOOL_FEATURES[f.type](ctx, f.params)
        _apply(ctx, op, body)
        ctx.tools[f.id] = (op, body)
        ctx.order.append((f.id, body))
        _track_sheet_feature(ctx, f)
    elif f.type in _MODIFIERS:
        if ctx.shape is None:
            raise BuildError(f"{f.id}: {f.type} needs an existing solid")
        before = ctx.shape.Faces()
        _MODIFIERS[f.type](ctx, f.params)
        _track_sheet_feature(ctx, f)
        ctx.created.append((f.id, [x for x in ctx.shape.Faces() if not any(x.isSame(b) for b in before)]))
    else:
        raise BuildError(f"{f.id}: unknown feature type {f.type!r}; use one of {', '.join(FEATURE_TYPES)}")


def resolve_plane(spec: PartSpec, shape: cq.Shape | None, plane: Any) -> PlaneInfo:
    """Resolve a plane spec against an existing solid (used by the sketch editor)."""
    ctx = _Ctx(spec)
    ctx.shape, ctx.fid = shape, "plane"
    return _plane(ctx, plane)


def has_solid_features(spec: PartSpec) -> bool:
    return any(not f.suppressed and f.type != "sketch" for f in spec.features)


def validate_sketches(spec: PartSpec) -> dict[str, dict[str, Any]]:
    """For a part that has sketches but no solid yet: solve them (raises BuildError on conflicts)."""
    ctx = _Ctx(spec)
    for f in spec.features:
        if f.suppressed or f.type != "sketch":
            continue
        ctx.fid = f.id
        _f_sketch(ctx, f.params)
    return {k: v[2] for k, v in ctx.sketches.items()}


def build(spec: PartSpec) -> BuildResult:
    ctx = _Ctx(spec)
    for f in spec.features:
        if f.suppressed:
            continue
        ctx.fid = f.id
        try:
            _build_feature(ctx, f)
        except BuildError:
            raise
        except Exception as e:  # OCC kernel failures
            detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            raise BuildError(f"{f.id}: geometry operation failed ({detail})")
        if ctx.shape is not None and not ctx.shape.Solids():
            raise BuildError(f"{f.id}: removed all material")

    if ctx.shape is None:
        raise BuildError("the part has no solid features")
    solids = ctx.shape.Solids()
    if len(solids) != 1:
        raise BuildError(f"the part has {len(solids)} separate bodies; features must connect into one solid")
    solid = solids[0]
    if not solid.isValid():
        raise BuildError("resulting geometry is not a valid solid")
    bb = solid.BoundingBox()
    return BuildResult(
        shape=cq.Workplane("XY").add(solid),
        volume_mm3=solid.Volume(),
        bbox_mm=(bb.xlen, bb.ylen, bb.zlen),
        tools=ctx.order,
        created=ctx.created,
        holes=ctx.holes,
        threads=ctx.threads,
        sheet=ctx.sheet,
        variables=ctx.vars,
        sketches={k: v[2] for k, v in ctx.sketches.items()},
    )
