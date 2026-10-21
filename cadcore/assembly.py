# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemblies: components referencing part files, positioned by mates, with interference and BOM.

An assembly is its own document (.aicadasm):
- `components` reference part files (`.aicad` parametric parts or `.step`) by path, relative to the
  assembly file when possible, so editing a part updates every assembly that uses it;
- `mates` relate faces of two components, by face references in the component's own coordinates.

Each component that isn't fixed has 6 unknowns: a translation and a rotation vector. Mates become
residual equations solved by Gauss–Newton with minimum-norm steps (the same approach as the sketch
solver). The solve reports remaining degrees of freedom and names any mates that conflict.

Mates (`suppressed` mates are kept but ignored):
- coincident: planar faces touch, normals opposite (`aligned` flips that);
- distance: planar faces parallel at a gap (`value` mm);
- parallel: planar faces (or cylinder axes) parallel;
- perpendicular: planar faces (or cylinder axes) at 90°;
- angle: the normals of two planar faces at `value` degrees (0–180);
- concentric: cylindrical axes coincide;
- lock: two components keep the relative pose they had when the mate was made (no faces needed).

Exploded views are display-only: `explode` holds steps {id, components, direction, distance} that the
UI adds as offsets; they never change component positions.
"""

from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cadquery as cq
import numpy as np

ASM_VERSION = 1
MATE_TYPES = ("coincident", "distance", "parallel", "perpendicular", "angle", "concentric", "lock")
PLANE_MATES = ("coincident", "distance", "angle")
DIRECTION_MATES = ("parallel", "perpendicular")  # two planes or two axes


class AssemblyError(Exception):
    pass


@dataclass
class Component:
    id: str
    file: str
    position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # rotation vector (axis * angle, degrees)
    fixed: bool = False
    name: str | None = None
    link: str | None = None  # robot link this component belongs to (URDF import)
    link_pose: list[float] | None = None  # [x, y, z, rx, ry, rz] of the component in its link frame
    color: str | None = None
    hidden: bool = False
    transparent: bool = False


@dataclass
class AssemblySpec:
    name: str = "Assembly"
    components: list[Component] = field(default_factory=list)
    mates: list[dict[str, Any]] = field(default_factory=list)
    robot: dict[str, Any] | None = None  # links and joints (see urdf.py); joints drive the link components
    explode: list[dict[str, Any]] = field(default_factory=list)  # exploded-view steps (display offsets)

    def clone(self) -> "AssemblySpec":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "asm_version": ASM_VERSION}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssemblySpec":
        return cls(d.get("name", "Assembly"), [Component(**c) for c in d.get("components", [])], list(d.get("mates", [])), d.get("robot"),
                   list(d.get("explode", [])))

    def component(self, cid: str) -> Component | None:
        return next((c for c in self.components if c.id == cid), None)


# ---------------- rotations ----------------

def rotation_matrix(rv_deg: list[float] | np.ndarray) -> np.ndarray:
    r = np.radians(np.asarray(rv_deg, dtype=float))
    th = np.linalg.norm(r)
    if th < 1e-12:
        return np.eye(3)
    k = r / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * (K @ K)


def rotation_vector(R: np.ndarray) -> np.ndarray:
    """Inverse of rotation_matrix (degrees)."""
    c = max(-1.0, min(1.0, (np.trace(R) - 1) / 2))
    th = math.acos(c)
    if th < 1e-9:
        return np.zeros(3)
    if abs(th - math.pi) < 1e-6:  # 180°: axis from the symmetric part
        k = np.sqrt(np.maximum((np.diag(R) + 1) / 2, 0))
        i = int(np.argmax(k))
        k[(i + 1) % 3] = math.copysign(k[(i + 1) % 3], R[i, (i + 1) % 3])
        k[(i + 2) % 3] = math.copysign(k[(i + 2) % 3], R[i, (i + 2) % 3])
        return np.degrees(th * k / np.linalg.norm(k))
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * math.sin(th))
    return np.degrees(th * w)


def align(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotation matrix taking unit vector u onto unit vector v."""
    u, v = u / np.linalg.norm(u), v / np.linalg.norm(v)
    c = float(u @ v)
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:
        perp = np.cross(u, [1, 0, 0] if abs(u[0]) < 0.9 else [0, 1, 0])
        perp /= np.linalg.norm(perp)
        return rotation_matrix(perp * 180.0)
    axis = np.cross(u, v)
    return rotation_matrix(axis / np.linalg.norm(axis) * math.degrees(math.acos(c)))


def location(c: Component) -> cq.Location:
    r = np.asarray(c.rotation, dtype=float)
    th = float(np.linalg.norm(r))
    t = cq.Vector(*c.position)
    if th < 1e-12:
        return cq.Location(t)
    return cq.Location(t, cq.Vector(*(r / th)), th)


# ---------------- component geometry ----------------

class _Cache:
    def __init__(self):
        self.items: dict[str, tuple[float, Any]] = {}

    def load(self, path: Path) -> dict[str, Any]:
        key = str(path.resolve())
        try:
            import hashlib
            mtime = hashlib.sha256(path.read_bytes()).hexdigest()  # content, so quick successive saves are seen
        except OSError:
            raise AssemblyError(f"component file not found: {path}")
        if key in self.items and self.items[key][0] == mtime:
            return self.items[key][1]
        if path.suffix.lower() in (".step", ".stp", ".brep"):
            shape = cq.Shape.importBrep(str(path)) if path.suffix.lower() == ".brep" else cq.importers.importStep(str(path)).val()
            info = {"shape": shape, "name": path.stem, "material": None, "mass_kg": None, "result": None}
        else:
            from .project import Project
            p = Project.load(path)
            if not p.result:
                raise AssemblyError(f"part {path.name} has no solid")
            m = p.spec.material
            info = {"shape": p.result.shape.val(), "name": p.spec.name, "material": m.name if m else None,
                    "mass_kg": p.result.volume_mm3 * 1e-9 * m.density if m else None, "result": p.result}
        info["volume_mm3"] = sum(abs(x.Volume()) for x in info["shape"].Solids())  # surfaces have no volume
        self.items[key] = (mtime, info)
        return info


CACHE = _Cache()


def resolve_path(asm_path: str | None, file: str) -> Path:
    p = Path(file).expanduser()
    if not p.is_absolute() and asm_path:
        p = Path(asm_path).parent / p
    return p


def _face_frame(shape: cq.Shape, ref: dict[str, Any]) -> dict[str, Any]:
    """Local geometry of a referenced face: plane (point, normal) or cylinder (axis point, direction)."""
    p = cq.Vector(*ref["point"])
    v = cq.Vertex.makeVertex(*p.toTuple())
    best = None
    for f in shape.Faces():
        d = f.distance(v)
        if d > 0.05:
            continue
        if ref.get("normal") and f.geomType() == "PLANE" and f.normalAt(p).normalized().dot(cq.Vector(*ref["normal"]).normalized()) < 0.999:
            continue
        if best is None or d < best[0]:
            best = (d, f)
    if best is None:
        raise AssemblyError("a mate references a face that no longer exists in its part; re-select it")
    f = best[1]
    if f.geomType() == "PLANE":
        return {"kind": "plane", "point": np.array(p.toTuple()), "normal": np.array(f.normalAt(p).normalized().toTuple())}
    if f.geomType() == "CYLINDER":
        ax = f._geomAdaptor().Cylinder().Axis()
        return {"kind": "axis", "point": np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()]),
                "dir": np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])}
    raise AssemblyError(f"mates need planar or cylindrical faces, not {f.geomType().lower()}")


# ---------------- solver ----------------

def pose_matrix(c: Component) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = rotation_matrix(c.rotation), c.position
    return T


def set_pose(c: Component, T: np.ndarray) -> None:
    c.rotation = [float(v) for v in rotation_vector(T[:3, :3])]
    c.position = [float(v) for v in T[:3, 3]]


def _dir(frame: dict[str, Any]) -> np.ndarray:
    return frame["normal"] if frame["kind"] == "plane" else frame["dir"]


def _angle(u: np.ndarray, v: np.ndarray) -> float:
    """Angle between two directions, radians (well conditioned near 0 and 180°)."""
    return math.atan2(float(np.linalg.norm(np.cross(u, v))), float(u @ v))


def _frames(spec: AssemblySpec, shapes: dict[str, cq.Shape]) -> list[tuple[dict[str, Any], Any, Any]]:
    """Active (not suppressed) mates with the local geometry of their faces; checks types and face kinds."""
    frames = []
    for m in spec.mates:
        t = m.get("type")
        if t not in MATE_TYPES:
            raise AssemblyError(f"mate {m.get('id')}: type must be one of {', '.join(MATE_TYPES)}")
        for side in ("a", "b"):
            if not spec.component(m[side]["component"]):
                raise AssemblyError(f"mate {m.get('id')} references the missing component {m[side]['component']!r}")
        if m.get("suppressed"):
            continue
        if t == "lock":
            if len(m.get("pose") or []) != 6:
                raise AssemblyError(f"mate {m.get('id')} (lock) has no stored relative pose")
            frames.append((m, None, None))
            continue
        fa = _face_frame(shapes[m["a"]["component"]], m["a"]["face"])
        fb = _face_frame(shapes[m["b"]["component"]], m["b"]["face"])
        if t == "concentric" and not fa["kind"] == fb["kind"] == "axis":
            raise AssemblyError(f"mate {m.get('id')} (concentric) needs two cylindrical faces")
        if t in PLANE_MATES and not fa["kind"] == fb["kind"] == "plane":
            raise AssemblyError(f"mate {m.get('id')} ({t}) needs two planar faces")
        if t in DIRECTION_MATES and fa["kind"] != fb["kind"]:
            raise AssemblyError(f"mate {m.get('id')} ({t}) needs two planar or two cylindrical faces")
        frames.append((m, fa, fb))
    return frames


def solve(spec: AssemblySpec, shapes: dict[str, cq.Shape]) -> dict[str, Any]:
    """Position non-fixed components to satisfy the mates. Mutates component transforms on success."""
    if not spec.components:
        return {"converged": True, "dof": 0, "conflicts": [], "max_residual": 0.0}
    if not any(c.fixed for c in spec.components):
        spec.components[0].fixed = True  # ground the first component
    free = [c for c in spec.components if not c.fixed]
    index = {c.id: i for i, c in enumerate(free)}
    frames = _frames(spec, shapes)

    _prealign(spec, frames)
    x0 = np.concatenate([np.r_[c.position, c.rotation] for c in free]) if free else np.zeros(0)

    def pose(x, cid):
        c = spec.component(cid)
        if c.fixed:
            return rotation_matrix(c.rotation), np.asarray(c.position, dtype=float)
        k = 6 * index[cid]
        return rotation_matrix(x[k + 3:k + 6]), x[k:k + 3]

    def residuals(x):
        out, rows = [], []
        for m, fa, fb in frames:
            Ra, ta = pose(x, m["a"]["component"])
            Rb, tb = pose(x, m["b"]["component"])
            t = m["type"]
            if t == "lock":  # b's pose in a's frame stays as stored
                Rr, tr = rotation_matrix(m["pose"][3:]), np.asarray(m["pose"][:3], dtype=float)
                r = list((Rb - Ra @ Rr).ravel()) + list(tb - (Ra @ tr + ta))
            elif t == "concentric":
                pa, pb = Ra @ fa["point"] + ta, Rb @ fb["point"] + tb
                da, db = Ra @ fa["dir"], Rb @ fb["dir"]
                r = list(np.cross(da, db))
                w = pb - pa
                r += list(w - da * (w @ da))
            else:
                da, db = Ra @ _dir(fa), Rb @ _dir(fb)
                if t == "parallel":
                    r = list(np.cross(da, db))
                elif t == "perpendicular":
                    r = [float(da @ db)]
                elif t == "angle":
                    r = [_angle(da, db) - math.radians(float(m.get("value", 0)))]
                else:  # coincident, distance
                    pa, pb = Ra @ fa["point"] + ta, Rb @ fb["point"] + tb
                    r = list(da - db if m.get("aligned") else da + db)
                    gap = float(m.get("value", 0)) if t == "distance" else 0.0
                    r.append((pb - pa) @ da - gap)
            out += r
            rows += [m.get("id")] * len(r)
        return np.array(out), rows

    def jacobian(x, r):
        J = np.zeros((len(r), len(x)))
        for j in range(len(x)):
            h = 1e-7 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += h
            J[:, j] = (residuals(xp)[0] - r) / h
        return J

    x = x0.copy()
    r, rows = residuals(x)
    for _ in range(200):
        if len(r) == 0 or np.max(np.abs(r)) < 1e-10:
            break
        step = np.linalg.lstsq(jacobian(x, r), -r, rcond=None)[0]
        t, base = 1.0, float(r @ r)
        while t > 1e-6:
            xn = x + t * step
            rn, _ = residuals(xn)
            if float(rn @ rn) < base:
                x, r = xn, rn
                break
            t /= 2
        else:
            break
    max_res = float(np.max(np.abs(r))) if len(r) else 0.0
    converged = max_res < 1e-6
    rank = int(np.linalg.matrix_rank(jacobian(x, r), tol=1e-6)) if len(r) and len(x) else 0
    conflicts = sorted({str(rows[i]) for i, v in enumerate(r) if abs(v) > 1e-6}) if not converged else []
    if converged:
        for c in free:
            k = 6 * index[c.id]
            c.position = [float(v) for v in x[k:k + 3]]
            c.rotation = [float(v) for v in x[k + 3:k + 6]]
    return {"converged": converged, "dof": len(x) - rank, "conflicts": conflicts, "max_residual": max_res}


def _turn(cur: np.ndarray, target: np.ndarray, theta: float, axes: list[np.ndarray]) -> np.ndarray:
    """A rotation that puts `cur` at angle theta (radians) from `target`. Axes from the component's other
    mates are tried first, since turning about them keeps those mates' directions."""
    perp = np.cross(cur, target)
    if np.linalg.norm(perp) < 1e-9:
        perp = np.cross(cur, [1, 0, 0] if abs(cur[0]) < 0.9 else [0, 1, 0])
    best = None
    for k in [*axes, perp]:
        k = k / np.linalg.norm(k)
        if np.linalg.norm(np.cross(k, cur)) < 1e-6:
            continue  # turning about cur itself changes nothing
        for psi in np.arange(0.0, 360.0, 0.5):
            Q = rotation_matrix(k * psi)
            err = abs(_angle(Q @ cur, target) - theta)
            if best is None or err < best[0] - 1e-12:
                best = (err, Q)
        if best[0] < math.radians(1):
            break
    return best[1]


def _prealign(spec: AssemblySpec, frames) -> None:
    """Give the solver a good start. A face that must flip 180° (or turn away from parallel, for angle and
    perpendicular mates) sits on a saddle point of the Gauss–Newton solve, so orientations are fixed up front:
    - each free component is first rotated and translated onto an already-placed partner;
    - later mates on an already-placed free component can still flip it, rotating about its face point;
    - lock mates place the component exactly.
    """
    placed = {c.id for c in spec.components if c.fixed}

    def want(m, fa_, fm, Rf, Rm):
        if m["type"] == "concentric":
            target, cur = Rf @ fa_["dir"], Rm @ fm["dir"]
            return (target if target @ cur >= 0 else -target), cur
        target, cur = Rf @ _dir(fa_), Rm @ _dir(fm)
        if m["type"] == "parallel":
            return (target if target @ cur >= 0 else -target), cur
        return (target if m.get("aligned") else -target), cur

    def other_axes(move, mate, Rm):
        """World directions of the moving component's faces in its other direction-fixing mates."""
        out = []
        for m2, f2a, f2b in frames:
            if m2 is mate or m2["type"] not in ("coincident", "distance", "parallel", "concentric"):
                continue
            for cid, f in ((m2["a"]["component"], f2a), (m2["b"]["component"], f2b)):
                if cid == move:
                    out.append(Rm @ _dir(f))
        return out

    for _ in range(3 * len(spec.components) + len(frames) + 2):
        progress = False
        for m, fa, fb in frames:
            ca, cb = m["a"]["component"], m["b"]["component"]
            for move, fm, anchor, fa_ in ((cb, fb, ca, fa), (ca, fa, cb, fb)):
                cm, cf = spec.component(move), spec.component(anchor)
                if cm.fixed or anchor not in placed:
                    continue
                Rf, tf = rotation_matrix(cf.rotation), np.asarray(cf.position, dtype=float)
                Rm, tm = rotation_matrix(cm.rotation), np.asarray(cm.position, dtype=float)
                t = m["type"]
                if t == "lock":
                    rel = np.eye(4)
                    rel[:3, :3], rel[:3, 3] = rotation_matrix(m["pose"][3:]), m["pose"][:3]
                    T = pose_matrix(cf) @ (rel if move == cb else np.linalg.inv(rel))
                    if move in placed and np.abs(T - pose_matrix(cm)).max() < 1e-9:
                        continue
                    set_pose(cm, T)
                    placed.add(move)
                    progress = True
                    break
                if t in ("angle", "perpendicular"):
                    target, cur = Rf @ _dir(fa_), Rm @ _dir(fm)
                    theta = math.pi / 2 if t == "perpendicular" else math.radians(float(m.get("value", 0)))
                    if abs(_angle(cur, target) - theta) < math.radians(25):
                        continue
                    Q = _turn(cur, target, theta, other_axes(move, m, Rm))
                    pw = Rm @ fm["point"] + tm
                    Rn, tn = Q @ Rm, pw - Q @ (Rm @ fm["point"])
                else:
                    target, cur = want(m, fa_, fm, Rf, Rm)
                    if move not in placed:
                        # first placement: turn about its face point, then shift only along the constrained
                        # directions, so a component moved along its free directions stays where it was put
                        Rn = align(cur, target) @ Rm
                        pw = Rm @ fm["point"] + tm
                        tn = pw - Rn @ fm["point"]
                        pm, pf = Rn @ fm["point"] + tn, Rf @ fa_["point"] + tf
                        pa, pb = (pf, pm) if move == cb else (pm, pf)
                        if t == "concentric":
                            d, w = Rf @ fa_["dir"], pb - pa
                            shift = w - d * (w @ d)
                        elif t in ("coincident", "distance"):
                            n = Rf @ fa_["normal"] if move == cb else Rn @ fm["normal"]  # a's world normal
                            gap = float(m.get("value", 0)) if t == "distance" else 0.0
                            shift = n * ((pb - pa) @ n - gap)
                        else:
                            shift = np.zeros(3)
                        tn = tn - shift if move == cb else tn + shift
                        placed.add(move)
                    elif cur @ target < 0.5:  # already placed but facing the wrong way: rotate about its face point
                        Q = align(cur, target)
                        pw = Rm @ fm["point"] + tm
                        Rn, tn = Q @ Rm, pw - Q @ (Rm @ fm["point"])
                    else:
                        continue
                cm.rotation, cm.position = [float(v) for v in rotation_vector(Rn)], [float(v) for v in tn]
                progress = True
                break
        if not progress:
            break


# ---------------- document with transactions ----------------

class AssemblyProject:
    def __init__(self, spec: AssemblySpec | None = None, path: str | None = None):
        self.spec = spec or AssemblySpec()
        self.path = path
        self._undo: list[AssemblySpec] = []
        self._redo: list[AssemblySpec] = []
        self.status = self._solve(self.spec)

    def shapes(self, spec: AssemblySpec | None = None) -> dict[str, cq.Shape]:
        spec = spec or self.spec
        return {c.id: CACHE.load(resolve_path(self.path, c.file))["shape"] for c in spec.components}

    def _solve(self, spec: AssemblySpec) -> dict[str, Any]:
        from .urdf import apply_kinematics

        apply_kinematics(spec)  # robot joints place their links first; mates then position other parts
        st = solve(spec, self.shapes(spec))
        if not st["converged"]:
            raise AssemblyError(f"mates conflict ({', '.join(map(str, st['conflicts']))}); remove or change one")
        return st

    def execute(self, command: str, **args) -> dict[str, Any]:
        cand = self.spec.clone()
        try:
            msg = getattr(self, f"_cmd_{command}")(cand, **args)
            st = self._solve(cand)
        except AttributeError:
            return {"ok": False, "message": f"unknown assembly command {command!r}"}
        except (AssemblyError, TypeError, KeyError, ValueError) as e:
            return {"ok": False, "message": f"{command} rejected, assembly unchanged: {e}"}
        self._undo.append(self.spec)
        self._redo.clear()
        self.spec, self.status = cand, st
        return {"ok": True, "message": msg}

    def undo(self) -> dict[str, Any]:
        if not self._undo:
            return {"ok": False, "message": "nothing to undo"}
        self._redo.append(self.spec)
        self.spec = self._undo.pop()
        self.status = self._solve(self.spec)
        return {"ok": True, "message": "undone"}

    def redo(self) -> dict[str, Any]:
        if not self._redo:
            return {"ok": False, "message": "nothing to redo"}
        self._undo.append(self.spec)
        self.spec = self._redo.pop()
        self.status = self._solve(self.spec)
        return {"ok": True, "message": "redone"}

    def move_joints(self, values: dict[str, float]) -> dict[str, Any]:
        """Set joint values (degrees / mm) without an undo step, like moving the view."""
        from .urdf import set_joint

        if not self.spec.robot:
            return {"ok": False, "message": "this assembly has no joints (import a URDF first)"}
        cand = self.spec.clone()
        try:
            msgs = [set_joint(cand.robot, k, v) for k, v in values.items()]
            st = self._solve(cand)
        except AssemblyError as e:
            return {"ok": False, "message": str(e)}
        self.spec, self.status = cand, st
        return {"ok": True, "message": ", ".join(msgs)}

    def save(self, path: str) -> None:
        old = self.path
        self.path = path
        for c in self.spec.components:  # store part paths relative to the assembly when possible
            abs_p = resolve_path(old, c.file).resolve()
            try:
                c.file = os.path.relpath(abs_p, Path(path).resolve().parent)
            except ValueError:
                c.file = str(abs_p)
        Path(path).write_text(json.dumps(self.spec.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str) -> "AssemblyProject":
        return cls(AssemblySpec.from_dict(json.loads(Path(path).read_text())), path)

    # ---- commands ----
    def _cmd_add_component(self, spec, file, position=None, rotation=None, fixed=None, id=None, name=None):
        resolved = resolve_path(self.path, file)
        CACHE.load(resolved)  # validates the file
        cid = id or _new_id(spec, Path(file).stem.replace(" ", "_").replace("-", "_"))
        if spec.component(cid):
            raise AssemblyError(f"component id {id!r} already exists")
        spec.components.append(Component(cid, str(resolved if self.path is None else file), list(position or [0, 0, 0]),
                                         list(rotation or [0, 0, 0]), bool(fixed) if fixed is not None else not spec.components, name))
        return f"added component {cid}"

    def _cmd_remove_component(self, spec, id):
        c = _comp(spec, id)
        spec.components.remove(c)
        spec.mates = [m for m in spec.mates if id not in (m["a"]["component"], m["b"]["component"])]
        for s in spec.explode:
            s["components"] = [x for x in s["components"] if x != id]
        spec.explode = [s for s in spec.explode if s["components"]]
        return f"removed {id} and its mates"

    def _cmd_set_transform(self, spec, id, position=None, rotation=None):
        c = _comp(spec, id)
        if position is not None:
            c.position = [float(v) for v in position]
        if rotation is not None:
            c.rotation = [float(v) for v in rotation]
        return f"moved {id}"

    def _cmd_move_component(self, spec, id, translate=None, axis=None, angle=0.0, pivot=None):
        """Move a free component by a translation (mm) and/or a rotation of `angle` degrees about `axis`
        through `pivot` (default: the component's bounding-box centre). Mates then re-solve."""
        c = _comp(spec, id)
        if c.fixed:
            raise AssemblyError(f"{id} is fixed; float it first (Fix/Float)")
        T = pose_matrix(c)
        if axis is not None and float(angle):
            k = np.asarray(axis, dtype=float)
            if np.linalg.norm(k) < 1e-12:
                raise AssemblyError("rotation axis must not be zero")
            if pivot is None:
                bb = self.shapes(spec)[id].moved(location(c)).BoundingBox()
                pivot = [(bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2]
            Q, p = np.eye(4), np.asarray(pivot, dtype=float)
            Q[:3, :3] = rotation_matrix(k / np.linalg.norm(k) * float(angle))
            Q[:3, 3] = p - Q[:3, :3] @ p
            T = Q @ T
        if translate is not None:
            T[:3, 3] += np.asarray(translate, dtype=float)
        set_pose(c, T)
        return f"moved {id}"

    def _cmd_set_fixed(self, spec, id, fixed=True):
        c = _comp(spec, id)
        if c.link and not fixed and spec.robot:
            raise AssemblyError(f"{id} is robot link {c.link}; its joints place it")
        c.fixed = bool(fixed)
        return f"{id} {'fixed' if fixed else 'floating'}"

    def _cmd_set_visibility(self, spec, ids, hidden=None, transparent=None):
        """Show/hide and transparency of one component id or a list; ids '*' means all."""
        targets = spec.components if ids == "*" else [_comp(spec, i) for i in ([ids] if isinstance(ids, str) else ids)]
        for c in targets:
            if hidden is not None:
                c.hidden = bool(hidden)
            if transparent is not None:
                c.transparent = bool(transparent)
        what = [w for w, v in (("hidden" if hidden else "shown", hidden), ("transparent" if transparent else "opaque", transparent)) if v is not None]
        return f"{len(targets)} component(s) {' and '.join(what) or 'unchanged'}"

    def _cmd_add_mate(self, spec, type, a, b, value=0, aligned=False, id=None, suppressed=False):
        if type not in MATE_TYPES:
            raise AssemblyError(f"mate type must be one of {', '.join(MATE_TYPES)}")
        for side in (a, b):
            if not spec.component(side.get("component", "")):
                raise AssemblyError(f"no component {side.get('component')!r}")
            if type != "lock" and not side.get("face", {}).get("point"):
                raise AssemblyError(f"a {type} mate needs a face on each component ({{component, face: {{point, normal}}}})")
        if a["component"] == b["component"]:
            raise AssemblyError("a mate needs two different components")
        if type == "angle" and not 0 <= float(value) <= 180:
            raise AssemblyError("angle must be between 0 and 180 degrees")
        mate = {"id": id or _new_id(spec, "mate", [m["id"] for m in spec.mates]), "type": type, "a": a, "b": b,
                "value": float(value), "aligned": bool(aligned), "suppressed": bool(suppressed)}
        if type == "lock":  # remember b's pose in a's frame
            ca, cb = spec.component(a["component"]), spec.component(b["component"])
            rel = np.linalg.inv(pose_matrix(ca)) @ pose_matrix(cb)
            mate["pose"] = [float(v) for v in rel[:3, 3]] + [float(v) for v in rotation_vector(rel[:3, :3])]
        spec.mates.append(mate)
        return f"added {type} mate {mate['id']}"

    def _cmd_edit_mate(self, spec, id, value=None, aligned=None, suppressed=None):
        m = _mate(spec, id)
        if value is not None:
            if m["type"] == "angle" and not 0 <= float(value) <= 180:
                raise AssemblyError("angle must be between 0 and 180 degrees")
            m["value"] = float(value)
        if aligned is not None:
            m["aligned"] = bool(aligned)
        if suppressed is not None:
            m["suppressed"] = bool(suppressed)
        return f"{id} {'suppressed' if m.get('suppressed') else 'updated'}"

    def _cmd_flip_mate(self, spec, id):
        """Flip a mate's alignment (coincident/distance normals; an angle becomes 180° minus it)."""
        m = _mate(spec, id)
        if m["type"] == "angle":
            m["value"] = 180.0 - float(m.get("value", 0))
        elif m["type"] in ("coincident", "distance"):
            m["aligned"] = not m.get("aligned")
        else:
            raise AssemblyError(f"a {m['type']} mate has no alignment to flip")
        return f"flipped {id}"

    def _cmd_remove_mate(self, spec, id):
        _mate(spec, id)
        spec.mates = [m for m in spec.mates if m["id"] != id]
        return f"removed {id}"

    def _cmd_pattern_component(self, spec, id, type="linear", count=3, direction=None, spacing=50.0, axis=None, point=None, angle=360.0):
        """Copies of a component: linear (count along direction at spacing mm) or circular (count about axis
        through point, spread over angle degrees). Copies are independent free components."""
        c = _comp(spec, id)
        count = int(count)
        if not 2 <= count <= 100:
            raise AssemblyError("count must be 2 to 100 (including the original)")
        ids = []
        for k in range(1, count):
            T = pose_matrix(c)
            if type == "linear":
                d = np.asarray(direction or [1, 0, 0], dtype=float)
                if np.linalg.norm(d) < 1e-12:
                    raise AssemblyError("pattern direction must not be zero")
                T[:3, 3] += d / np.linalg.norm(d) * float(spacing) * k
            elif type == "circular":
                k_ax = np.asarray(axis or [0, 0, 1], dtype=float)
                if np.linalg.norm(k_ax) < 1e-12:
                    raise AssemblyError("pattern axis must not be zero")
                step = float(angle) / (count if abs(float(angle) - 360) < 1e-9 else count - 1)
                Q, p = np.eye(4), np.asarray(point or [0, 0, 0], dtype=float)
                Q[:3, :3] = rotation_matrix(k_ax / np.linalg.norm(k_ax) * step * k)
                Q[:3, 3] = p - Q[:3, :3] @ p
                T = Q @ T
            else:
                raise AssemblyError("pattern type must be linear or circular")
            cid = _new_id(spec, Path(c.file).stem.replace(" ", "_").replace("-", "_"))
            new = Component(cid, c.file, name=c.name, color=c.color)
            set_pose(new, T)
            spec.components.append(new)
            ids.append(cid)
        return f"patterned {id}: added {', '.join(ids)}"

    def _cmd_add_explode_step(self, spec, components, direction, distance, id=None):
        ids = [components] if isinstance(components, str) else list(components)
        for i in ids:
            _comp(spec, i)
        d = np.asarray(direction, dtype=float)
        if not ids or np.linalg.norm(d) < 1e-12:
            raise AssemblyError("an explode step needs components and a non-zero direction")
        sid = id or _new_id(spec, "step", [s["id"] for s in spec.explode])
        spec.explode.append({"id": sid, "components": ids, "direction": [float(v) for v in d / np.linalg.norm(d)], "distance": float(distance)})
        return f"added explode step {sid}"

    def _cmd_edit_explode_step(self, spec, id, direction=None, distance=None, components=None):
        s = next((s for s in spec.explode if s["id"] == id), None)
        if not s:
            raise AssemblyError(f"no explode step {id!r}")
        if direction is not None:
            d = np.asarray(direction, dtype=float)
            if np.linalg.norm(d) < 1e-12:
                raise AssemblyError("direction must not be zero")
            s["direction"] = [float(v) for v in d / np.linalg.norm(d)]
        if distance is not None:
            s["distance"] = float(distance)
        if components is not None:
            s["components"] = [_comp(spec, i).id for i in components]
        return f"updated {id}"

    def _cmd_remove_explode_step(self, spec, id):
        before = len(spec.explode)
        spec.explode = [s for s in spec.explode if s["id"] != id]
        if len(spec.explode) == before:
            raise AssemblyError(f"no explode step {id!r}")
        return f"removed {id}"

    def _cmd_auto_explode(self, spec, scale=1.0, components=None):
        """Replace the explode steps with one step per component, radially out from the assembly centre."""
        world = self.world_shapes(spec)
        ids = components or [c.id for c in spec.components if not c.hidden]
        boxes = {i: world[i].BoundingBox() for i in ids}
        if not boxes:
            raise AssemblyError("nothing to explode")
        lo = np.min([[b.xmin, b.ymin, b.zmin] for b in boxes.values()], axis=0)
        hi = np.max([[b.xmax, b.ymax, b.zmax] for b in boxes.values()], axis=0)
        centre, size = (lo + hi) / 2, float(np.linalg.norm(hi - lo)) or 100.0
        spec.explode = []
        for k, (cid, b) in enumerate(boxes.items(), 1):
            d = np.array([(b.xmin + b.xmax) / 2, (b.ymin + b.ymax) / 2, (b.zmin + b.zmax) / 2]) - centre
            n = float(np.linalg.norm(d))
            direction = d / n if n > 1e-3 * size else np.array([0.0, 0.0, 1.0])
            spec.explode.append({"id": f"step_{k}", "components": [cid], "direction": [float(v) for v in direction],
                                 "distance": float(scale) * max(n, 0.25 * size)})
        return f"auto explode: {len(spec.explode)} steps"

    def _cmd_clear_explode(self, spec):
        spec.explode = []
        return "explode steps removed"

    def _cmd_set_joint(self, spec, name, value):
        from .urdf import set_joint

        if not spec.robot:
            raise AssemblyError("this assembly has no joints (import a URDF first)")
        return set_joint(spec.robot, name, value)

    def _cmd_set_name(self, spec, name):
        spec.name = str(name)
        return "renamed"

    # ---- reports ----
    def world_shapes(self, spec: AssemblySpec | None = None) -> dict[str, cq.Shape]:
        spec = spec or self.spec
        return {c.id: s.moved(location(c)) for c, s in zip(spec.components, self.shapes(spec).values())}

    def interference(self) -> list[dict[str, Any]]:
        ws = list(self.world_shapes().items())
        out = []
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                (ia, a), (ib, b) = ws[i], ws[j]
                ba, bb = a.BoundingBox(), b.BoundingBox()
                if ba.xmax < bb.xmin or bb.xmax < ba.xmin or ba.ymax < bb.ymin or bb.ymax < ba.ymin or ba.zmax < bb.zmin or bb.zmax < ba.zmin:
                    continue
                common = a.intersect(b)
                vol = sum(s.Volume() for s in common.Solids()) if common.Solids() else 0.0
                if vol > 1e-3:
                    out.append({"a": ia, "b": ib, "volume_mm3": vol, "center": list(common.Center().toTuple())})
        return out

    def mass_properties(self) -> dict[str, Any]:
        """Mass, volume and centre of mass summed over components. Mass comes from the part's material,
        else (robots) from the URDF link mass, counted once per link."""
        links = (self.spec.robot or {}).get("links", {})
        used_links: set[str] = set()
        rows, total_m, total_v, mom, vmom = [], 0.0, 0.0, np.zeros(3), np.zeros(3)
        for c, s in zip(self.spec.components, self.world_shapes().values()):
            info = CACHE.load(resolve_path(self.path, c.file))
            vol = float(info["volume_mm3"])
            try:
                com = np.array(cq.Shape.centerOfMass(s).toTuple()) if s.Solids() else np.array(s.Center().toTuple())
            except Exception:  # noqa: BLE001 - surfaces without volume
                bb = s.BoundingBox()
                com = np.array([(bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2])
            mass, source = info["mass_kg"], "material" if info["mass_kg"] is not None else None
            if mass is None and c.link and c.link not in used_links and (links.get(c.link) or {}).get("mass_kg") is not None:
                mass, source = float(links[c.link]["mass_kg"]), "urdf"
                used_links.add(c.link)
            elif mass is None and c.link in used_links:
                source = 'link'  # its link's URDF mass is already counted on a sibling component
            rows.append({"component": c.id, "part": info["name"], "material": info["material"], "volume_mm3": vol,
                         "mass_kg": mass, "mass_source": source, "center_mm": [float(v) for v in com]})
            total_v += vol
            vmom += vol * com
            if mass is not None:
                total_m += mass
                mom += mass * com
        com = mom / total_m if total_m > 0 else (vmom / total_v if total_v > 0 else np.zeros(3))
        missing = [r["component"] for r in rows if r["mass_kg"] is None and r["mass_source"] != "link"]
        return {"mass_kg": total_m, "volume_mm3": total_v, "center_of_mass_mm": [float(v) for v in com],
                "center_from": "mass" if total_m > 0 else "volume", "components": rows, "without_mass": missing}

    def bom(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for c in self.spec.components:
            path = resolve_path(self.path, c.file)
            info = CACHE.load(path)
            row = rows.setdefault(str(path.resolve()), {"part": info["name"], "file": c.file, "qty": 0, "material": info["material"],
                                                        "mass_each_kg": info["mass_kg"], "components": []})
            row["qty"] += 1
            row["components"].append(c.id)
        out = list(rows.values())
        for i, r in enumerate(out, 1):
            r["item"] = i
            r["mass_total_kg"] = r["mass_each_kg"] * r["qty"] if r["mass_each_kg"] is not None else None
        return out

    def bom_csv(self) -> str:
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["item", "part", "qty", "material", "mass_each_kg", "mass_total_kg", "file", "components"])
        for r in self.bom():
            w.writerow([r["item"], r["part"], r["qty"], r["material"] or "",
                        "" if r["mass_each_kg"] is None else f"{r['mass_each_kg']:.6g}",
                        "" if r["mass_total_kg"] is None else f"{r['mass_total_kg']:.6g}", r["file"], " ".join(r["components"])])
        return buf.getvalue()

    def export_step(self, path: str) -> None:
        assy = cq.Assembly(name=self.spec.name.replace(" ", "_"))
        palette = ["steelblue", "orange", "seagreen", "tomato", "gold", "orchid", "gray"]
        for i, (c, s) in enumerate(zip(self.spec.components, self.shapes().values())):
            assy.add(s, name=c.id, loc=location(c), color=cq.Color(palette[i % len(palette)]))
        assy.export(path, exportType="STEP")


def _comp(spec: AssemblySpec, cid: str) -> Component:
    c = spec.component(cid)
    if not c:
        raise AssemblyError(f"no component {cid!r}")
    return c


def _mate(spec: AssemblySpec, mid: str) -> dict[str, Any]:
    m = next((m for m in spec.mates if m["id"] == mid), None)
    if not m:
        raise AssemblyError(f"no mate {mid!r}")
    return m


def _new_id(spec: AssemblySpec, stem: str, taken: list[str] | None = None) -> str:
    used = set(taken if taken is not None else [c.id for c in spec.components])
    n = 1
    while f"{stem}_{n}" in used:
        n += 1
    return f"{stem}_{n}"
