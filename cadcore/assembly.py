# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemblies: components referencing part files, positioned by mates, with interference and BOM.

An assembly is its own document (.aicadasm):
- `components` reference part files (`.aicad` parametric parts or `.step`) by path, relative to the
  assembly file when possible, so editing a part updates every assembly that uses it;
- `mates` relate faces of two components, by face references in the component's own coordinates.

Each component that isn't fixed has 6 unknowns: a translation and a rotation vector. Mates become
residual equations solved by Gauss–Newton with minimum-norm steps (the same approach as the sketch
solver). The solve reports remaining degrees of freedom and names any mates that conflict.

Mates:
- coincident: planar faces touch, normals opposite (`aligned` flips that);
- distance: planar faces parallel at a gap;
- parallel: planar faces parallel;
- concentric: cylindrical axes coincide.
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
MATE_TYPES = ("coincident", "distance", "parallel", "concentric")


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


@dataclass
class AssemblySpec:
    name: str = "Assembly"
    components: list[Component] = field(default_factory=list)
    mates: list[dict[str, Any]] = field(default_factory=list)
    robot: dict[str, Any] | None = None  # links and joints (see urdf.py); joints drive the link components

    def clone(self) -> "AssemblySpec":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "asm_version": ASM_VERSION}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssemblySpec":
        return cls(d.get("name", "Assembly"), [Component(**c) for c in d.get("components", [])], list(d.get("mates", [])), d.get("robot"))

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

def solve(spec: AssemblySpec, shapes: dict[str, cq.Shape]) -> dict[str, Any]:
    """Position non-fixed components to satisfy the mates. Mutates component transforms on success."""
    if not spec.components:
        return {"converged": True, "dof": 0, "conflicts": [], "max_residual": 0.0}
    if not any(c.fixed for c in spec.components):
        spec.components[0].fixed = True  # ground the first component
    free = [c for c in spec.components if not c.fixed]
    index = {c.id: i for i, c in enumerate(free)}
    frames = []
    for mi, m in enumerate(spec.mates):
        if m.get("type") not in MATE_TYPES:
            raise AssemblyError(f"mate {m.get('id')}: type must be one of {', '.join(MATE_TYPES)}")
        fa = _face_frame(shapes[m["a"]["component"]], m["a"]["face"])
        fb = _face_frame(shapes[m["b"]["component"]], m["b"]["face"])
        need = "axis" if m["type"] == "concentric" else "plane"
        if fa["kind"] != need or fb["kind"] != need:
            raise AssemblyError(f"mate {m.get('id')} ({m['type']}) needs two {'cylindrical' if need == 'axis' else 'planar'} faces")
        frames.append((m, fa, fb))

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
        for mi, (m, fa, fb) in enumerate(frames):
            Ra, ta = pose(x, m["a"]["component"])
            Rb, tb = pose(x, m["b"]["component"])
            pa, pb = Ra @ fa["point"] + ta, Rb @ fb["point"] + tb
            if m["type"] == "concentric":
                da, db = Ra @ fa["dir"], Rb @ fb["dir"]
                r = list(np.cross(da, db))
                w = pb - pa
                r += list(w - da * (w @ da))
            else:
                na, nb = Ra @ fa["normal"], Rb @ fb["normal"]
                if m["type"] == "parallel":
                    r = list(np.cross(na, nb))
                else:
                    r = list(na - nb if m.get("aligned") else na + nb)
                    gap = float(m.get("value", 0)) if m["type"] == "distance" else 0.0
                    r.append((pb - pa) @ na - gap)
            out += r
            rows += [mi] * len(r)
        return np.array(out), rows

    x = x0.copy()
    r, rows = residuals(x)
    for _ in range(200):
        if len(r) == 0 or np.max(np.abs(r)) < 1e-10:
            break
        J = np.zeros((len(r), len(x)))
        for j in range(len(x)):
            h = 1e-7 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += h
            J[:, j] = (residuals(xp)[0] - r) / h
        step = np.linalg.lstsq(J, -r, rcond=None)[0]
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
    if len(r) and len(x):
        J = np.zeros((len(r), len(x)))
        for j in range(len(x)):
            h = 1e-7 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += h
            J[:, j] = (residuals(xp)[0] - r) / h
        rank = int(np.linalg.matrix_rank(J, tol=1e-6))
    else:
        rank = 0
    conflicts = sorted({spec.mates[rows[i]].get("id") for i, v in enumerate(r) if abs(v) > 1e-6}) if not converged else []
    if converged:
        for c in free:
            k = 6 * index[c.id]
            c.position = [float(v) for v in x[k:k + 3]]
            c.rotation = [float(v) for v in x[k + 3:k + 6]]
    return {"converged": converged, "dof": len(x) - rank, "conflicts": conflicts, "max_residual": max_res}


def _prealign(spec: AssemblySpec, frames) -> None:
    """Give the solver a good start. A face that must flip 180° sits on a saddle point of the
    Gauss–Newton solve, so orientations are fixed up front:
    - each free component is first rotated and translated onto an already-placed partner;
    - later mates on an already-placed free component can still flip it, rotating about its face point.
    """
    placed = {c.id for c in spec.components if c.fixed}

    def want(m, fa_, fm, Rf, Rm):
        if m["type"] == "concentric":
            target, cur = Rf @ fa_["dir"], Rm @ fm["dir"]
            return (target if target @ cur >= 0 else -target), cur
        target, cur = Rf @ fa_["normal"], Rm @ fm["normal"]
        if m["type"] == "parallel":
            return (target if target @ cur >= 0 else -target), cur
        return (target if m.get("aligned") else -target), cur

    for _ in range(len(spec.components) + 1):
        progress = False
        for m, fa, fb in frames:
            ca, cb = m["a"]["component"], m["b"]["component"]
            for move, fm, anchor, fa_ in ((cb, fb, ca, fa), (ca, fa, cb, fb)):
                cm, cf = spec.component(move), spec.component(anchor)
                if cm.fixed or anchor not in placed:
                    continue
                Rf, tf = rotation_matrix(cf.rotation), np.asarray(cf.position, dtype=float)
                Rm, tm = rotation_matrix(cm.rotation), np.asarray(cm.position, dtype=float)
                target, cur = want(m, fa_, fm, Rf, Rm)
                if move not in placed:  # first placement: orient and move onto the partner
                    Rn = align(cur, target) @ Rm
                    tn = Rf @ fa_["point"] + tf - Rn @ fm["point"]
                    if m["type"] == "distance":
                        tn = tn + (Rf @ fa_["normal"]) * float(m.get("value", 0)) * (1 if move == cb else -1)
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
        stem = Path(file).stem.replace(" ", "_").replace("-", "_")
        n = 1
        while spec.component(id or f"{stem}_{n}"):
            if id:
                raise AssemblyError(f"component id {id!r} already exists")
            n += 1
        cid = id or f"{stem}_{n}"
        spec.components.append(Component(cid, str(resolved if self.path is None else file), list(position or [0, 0, 0]),
                                         list(rotation or [0, 0, 0]), bool(fixed) if fixed is not None else not spec.components, name))
        return f"added component {cid}"

    def _cmd_remove_component(self, spec, id):
        c = spec.component(id)
        if not c:
            raise AssemblyError(f"no component {id!r}")
        spec.components.remove(c)
        spec.mates = [m for m in spec.mates if id not in (m["a"]["component"], m["b"]["component"])]
        return f"removed {id} and its mates"

    def _cmd_set_transform(self, spec, id, position=None, rotation=None):
        c = spec.component(id)
        if not c:
            raise AssemblyError(f"no component {id!r}")
        if position is not None:
            c.position = [float(v) for v in position]
        if rotation is not None:
            c.rotation = [float(v) for v in rotation]
        return f"moved {id}"

    def _cmd_set_fixed(self, spec, id, fixed=True):
        c = spec.component(id)
        if not c:
            raise AssemblyError(f"no component {id!r}")
        c.fixed = bool(fixed)
        return f"{id} {'fixed' if fixed else 'free'}"

    def _cmd_add_mate(self, spec, type, a, b, value=0, aligned=False, id=None):
        if type not in MATE_TYPES:
            raise AssemblyError(f"mate type must be one of {', '.join(MATE_TYPES)}")
        for side in (a, b):
            if not spec.component(side.get("component", "")):
                raise AssemblyError(f"no component {side.get('component')!r}")
        if a["component"] == b["component"]:
            raise AssemblyError("a mate needs faces on two different components")
        n = 1
        while any(m["id"] == f"mate_{n}" for m in spec.mates):
            n += 1
        spec.mates.append({"id": id or f"mate_{n}", "type": type, "a": a, "b": b, "value": float(value), "aligned": bool(aligned)})
        return f"added {type} mate"

    def _cmd_remove_mate(self, spec, id):
        before = len(spec.mates)
        spec.mates = [m for m in spec.mates if m["id"] != id]
        if len(spec.mates) == before:
            raise AssemblyError(f"no mate {id!r}")
        return f"removed {id}"

    def _cmd_set_joint(self, spec, name, value):
        from .urdf import set_joint

        if not spec.robot:
            raise AssemblyError("this assembly has no joints (import a URDF first)")
        return set_joint(spec.robot, name, value)

    def _cmd_set_name(self, spec, name):
        spec.name = str(name)
        return "renamed"

    # ---- reports ----
    def world_shapes(self) -> dict[str, cq.Shape]:
        return {c.id: s.moved(location(c)) for c, s in zip(self.spec.components, self.shapes().values())}

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

    def export_step(self, path: str) -> None:
        assy = cq.Assembly(name=self.spec.name.replace(" ", "_"))
        palette = ["steelblue", "orange", "seagreen", "tomato", "gold", "orchid", "gray"]
        for i, (c, s) in enumerate(zip(self.spec.components, self.shapes().values())):
            assy.add(s, name=c.id, loc=location(c), color=cq.Color(palette[i % len(palette)]))
        assy.export(path, exportType="STEP")
