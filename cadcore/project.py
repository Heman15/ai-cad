# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project: a PartSpec plus history, driven only through named commands.

Every change (chat, UI field edit, drawing import) goes through `execute`, which
applies the command to a copy of the spec, rebuilds, and commits only if the
rebuild succeeds. Failed commands leave the last valid model untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cadquery as cq

from .builder import FEATURE_TYPES, BuildError, BuildResult, build, has_solid_features, validate_sketches
from .deps import dependents_of, spec_diff, variable_users
from .materials import MATERIALS, MECHANICAL
from .spec import Feature, Material, PartSpec, parse_path, set_path


class CommandError(Exception):
    """Invalid command or arguments. Message is user-facing."""


@dataclass
class CommandResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "data": self.data}


_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FACE_NAMES = {"top": ">Z", "bottom": "<Z", "front": "<Y", "back": ">Y", "left": "<X", "right": ">X"}

# Commands that only read state; they don't create a history entry.
_READ_ONLY = {"calculate_mass", "get_model", "export_step", "export_stl"}


def _build_or_none(spec: PartSpec) -> BuildResult | None:
    if has_solid_features(spec):
        return build(spec)
    validate_sketches(spec)  # sketch-only parts are valid intermediate states, but must solve
    return None


class Project:
    def __init__(self, spec: PartSpec | None = None):
        self.spec = spec or PartSpec()
        self._undo: list[tuple[PartSpec, str]] = []
        self._redo: list[tuple[PartSpec, str]] = []
        self._result = _build_or_none(self.spec)

    # ---------- public API ----------

    @property
    def result(self) -> BuildResult | None:
        return self._result

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def execute(self, command: str, **args) -> CommandResult:
        handler: Callable | None = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            return CommandResult(False, f"unknown command {command!r}. Available: {', '.join(COMMANDS)}")
        if command in _READ_ONLY:
            try:
                return handler(self.spec, **args)
            except (CommandError, BuildError, TypeError) as e:
                return CommandResult(False, str(e))

        candidate = self.spec.clone()
        try:
            message = handler(candidate, **args)
            result = _build_or_none(candidate)
        except (CommandError, BuildError, TypeError, KeyError, IndexError, ValueError) as e:
            return CommandResult(False, f"{command} rejected, model unchanged: {e}")

        self._undo.append((self.spec, command))
        self._redo.clear()
        self.spec, self._result = candidate, result
        return CommandResult(True, message, self._summary())

    def undo(self) -> CommandResult:
        if not self._undo:
            return CommandResult(False, "nothing to undo")
        prev, cmd = self._undo.pop()
        self._redo.append((self.spec, cmd))
        self.spec, self._result = prev, _build_or_none(prev)
        return CommandResult(True, f"undid {cmd}", self._summary())

    def redo(self) -> CommandResult:
        if not self._redo:
            return CommandResult(False, "nothing to redo")
        nxt, cmd = self._redo.pop()
        self._undo.append((self.spec, cmd))
        self.spec, self._result = nxt, _build_or_none(nxt)
        return CommandResult(True, f"redid {cmd}", self._summary())

    def _apply_commands(self, commands: list[dict[str, Any]]) -> tuple[PartSpec, BuildResult | None, list[str]]:
        """Run commands on a copy, rebuilding after each so later steps see earlier results."""
        candidate = self.spec.clone()
        result, messages = None, []
        for i, c in enumerate(commands):
            name, args = c.get("command"), c.get("args", {})
            if name in _READ_ONLY or not hasattr(self, f"_cmd_{name}"):
                raise CommandError(f"step {i + 1}: {name!r} is not a modelling command")
            try:
                messages.append(getattr(self, f"_cmd_{name}")(candidate, **args))
                result = _build_or_none(candidate)
            except (CommandError, BuildError, TypeError, KeyError, IndexError, ValueError) as e:
                raise CommandError(f"step {i + 1} ({name}) failed: {e}")
        return candidate, result, messages

    def preview(self, commands: list[dict[str, Any]]) -> dict[str, Any]:
        """Evaluate a list of commands without committing (for comparing redesign alternatives)."""
        try:
            candidate, result, messages = self._apply_commands(commands)
        except CommandError as e:
            return {"ok": False, "message": str(e)}
        out = {"ok": True, "messages": messages, "diff": spec_diff(self.spec, candidate)}
        if result:
            out["volume_mm3"] = result.volume_mm3
            out["bbox_mm"] = result.bbox_mm
            if candidate.material:
                out["mass_kg"] = result.volume_mm3 * 1e-9 * candidate.material.density
        return out

    def execute_batch(self, commands: list[dict[str, Any]], label: str = "batch") -> CommandResult:
        """Apply several commands as one transaction and one undo step."""
        try:
            candidate, result, messages = self._apply_commands(commands)
        except CommandError as e:
            return CommandResult(False, f"{label} rejected, model unchanged: {e}")
        diff = spec_diff(self.spec, candidate)
        self._undo.append((self.spec, label))
        self._redo.clear()
        self.spec, self._result = candidate, result
        return CommandResult(True, "; ".join(messages), {**self._summary(), "diff": diff})

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.spec.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        return cls(PartSpec.from_dict(json.loads(Path(path).read_text())))

    # ---------- helpers ----------

    def _summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"features": [f"{f.id} ({f.type})" for f in self.spec.features]}
        if self._result:
            out["volume_mm3"] = round(self._result.volume_mm3, 3)
            out["bbox_mm"] = [round(x, 3) for x in self._result.bbox_mm]
        return out

    @staticmethod
    def _check_new_id(spec: PartSpec, fid: Any) -> None:
        if not isinstance(fid, str) or not _ID_RE.match(fid) or fid == "vars":
            raise CommandError(f"invalid feature id {fid!r}; use letters, digits, underscore")
        if spec.feature(fid):
            raise CommandError(f"feature id {fid!r} already exists")

    @staticmethod
    def _feature(spec: PartSpec, fid: str) -> Feature:
        f = spec.feature(fid)
        if not f:
            raise CommandError(f"no feature named {fid!r}. Features: {', '.join(x.id for x in spec.features) or 'none'}")
        return f

    # ---------- templates (replace the whole part, fully parametric) ----------

    @staticmethod
    def _positive_limits(spec: PartSpec) -> None:
        spec.limits = {k: {"min": 0} for k in spec.variables}

    def _cmd_new_part(self, spec, name="Part"):
        spec.name, spec.variables, spec.features, spec.source, spec.intent = name, {}, [], None, None
        spec.limits = {}
        return f"new empty part {name!r}"

    def _cmd_create_plate(self, spec, length, width, thickness):
        spec.variables = {"length": length, "width": width, "thickness": thickness}
        spec.features = [Feature("base", "extrude", {
            "plane": "XY", "distance": "=thickness",
            "shapes": [{"type": "rect", "w": "=length", "h": "=width", "x": 0, "y": 0, "centered": False}]})]
        self._positive_limits(spec)
        return f"created plate {length} x {width} x {thickness} mm"

    def _cmd_create_l_bracket(self, spec, length, width, thickness, height):
        spec.variables = {"length": length, "width": width, "thickness": thickness, "height": height}
        spec.features = [
            Feature("base", "extrude", {"plane": "XY", "distance": "=thickness",
                                        "shapes": [{"type": "rect", "w": "=length", "h": "=width", "x": 0, "y": 0, "centered": False}]}),
            Feature("upright", "extrude", {"plane": "XY", "distance": "=height",
                                           "shapes": [{"type": "rect", "w": "=thickness", "h": "=width", "x": 0, "y": 0, "centered": False}]}),
        ]
        self._positive_limits(spec)
        return f"created L-bracket {length} x {width} x {height} mm, thickness {thickness} mm"

    def _cmd_create_shaft(self, spec, segments):
        if not isinstance(segments, list) or not segments:
            raise CommandError("segments must be a list of {diameter, length}")
        spec.variables = {}
        pts: list[list[Any]] = [[0, 0]]
        z = "0"
        for i, s in enumerate(segments, 1):
            spec.variables[f"d{i}"] = s["diameter"]
            spec.variables[f"l{i}"] = s["length"]
            pts.append([f"=d{i}/2", z if z == "0" else f"={z}"])
            z = f"l{i}" if z == "0" else f"{z}+l{i}"
            pts.append([f"=d{i}/2", f"={z}"])
        pts.append([0, f"={z}"])
        spec.features = [Feature("shaft", "revolve", {"plane": "XZ", "axis": "v", "angle": 360,
                                                      "shapes": [{"type": "polygon", "points": pts}]})]
        self._positive_limits(spec)
        return f"created stepped shaft with {len(segments)} segment(s) along Z"

    def _cmd_create_flange(self, spec, outer_diameter, inner_diameter, thickness, bolt_circle_diameter, bolt_count, bolt_diameter):
        spec.variables = {"od": outer_diameter, "id": inner_diameter, "t": thickness,
                          "pcd": bolt_circle_diameter, "n_bolts": bolt_count, "bolt_d": bolt_diameter}
        spec.features = [
            Feature("disc", "extrude", {"plane": "XY", "distance": "=t", "shapes": [
                {"type": "circle", "d": "=od"}, {"type": "circle", "d": "=id", "mode": "subtract"}]}),
            Feature("bolt_hole", "hole", {"plane": {"face": ">Z"}, "diameter": "=bolt_d", "depth": "through",
                                          "positions": [["=pcd/2", 0]]}),
            Feature("bolt_pattern", "pattern_circular", {"feature": "bolt_hole", "count": "=n_bolts",
                                                         "axis_point": [0, 0, 0], "axis_dir": [0, 0, 1]}),
        ]
        self._positive_limits(spec)
        return f"created flange OD {outer_diameter}, {bolt_count} x Ø{bolt_diameter} on PCD {bolt_circle_diameter}"

    # ---------- variables ----------

    def _cmd_set_variable(self, spec, name, value, min=None, max=None):
        if not isinstance(name, str) or not _ID_RE.match(name):
            raise CommandError(f"invalid variable name {name!r}")
        old = spec.variables.get(name)
        spec.variables[name] = value
        if min is not None or max is not None:
            spec.limits[name] = {k: v for k, v in (("min", min), ("max", max)) if v is not None}
        return f"variable {name}: {old} -> {value}" if old is not None else f"variable {name} = {value}"

    def _cmd_remove_variable(self, spec, name):
        if name not in spec.variables:
            raise CommandError(f"no variable {name!r}")
        users = variable_users(spec, name)
        if users:
            raise CommandError(f"variable {name!r} is used by {', '.join(users)}")
        del spec.variables[name]
        spec.limits.pop(name, None)
        return f"removed variable {name}"

    # ---------- features ----------

    def _cmd_add_feature(self, spec, id, type, params, after=None):
        self._check_new_id(spec, id)
        if type not in FEATURE_TYPES:
            raise CommandError(f"unknown feature type {type!r}; use one of {', '.join(FEATURE_TYPES)}")
        if not isinstance(params, dict):
            raise CommandError("params must be an object")
        f = Feature(id=id, type=type, params=params)
        if after is None:
            spec.features.append(f)
        else:
            idx = spec.features.index(self._feature(spec, after))
            spec.features.insert(idx + 1, f)
        return f"added {type} {id}"

    def _cmd_import_file(self, spec, file, units="mm", id=None):
        """Add a body read from a mesh or CAD file (STL/OBJ/PLY/3MF/STEP/IGES/BREP); it is not parametric."""
        from .convert import ConvertError, kind_of, scale_of

        path = Path(str(file)).expanduser()
        try:
            if kind_of(path) not in ("mesh", "cad"):
                raise CommandError("import_file takes mesh or CAD files; open .aicad parts directly")
            scale_of(units)
        except ConvertError as e:
            raise CommandError(str(e))
        if not path.exists():
            raise CommandError(f"file not found: {path}")
        if id is None:
            stem = re.sub(r"\W+", "_", path.stem).strip("_").lower() or "body"
            id = stem if stem[0].isalpha() else f"body_{stem}"
            n = 2
            while spec.feature(id):
                id, n = f"{stem}_{n}", n + 1
        self._check_new_id(spec, id)
        spec.features.append(Feature(id=id, type="import_body", params={"file": str(path.resolve()), "units": units}))
        if spec.name in ("Part", "") and len(spec.features) == 1:
            spec.name = path.stem
        return f"imported {path.name} as {id} (a fixed body: later features can cut or add to it)"

    def _cmd_add_hole(self, spec, id, diameter=None, depth="through", face="top", plane=None, positions=None,
                      x=None, y=None, z=None, **extra):
        """Convenience wrapper: hole on a named face (top/bottom/front/back/left/right)."""
        if plane is None:
            if face not in FACE_NAMES:
                raise CommandError(f"face must be one of {', '.join(FACE_NAMES)}")
            plane = {"face": FACE_NAMES[face]}
        if positions is None:
            coords = [c for c in (x, y, z) if c is not None]
            if len(coords) != 2:
                raise CommandError("give positions=[[u,v],...] or exactly two of x, y, z (the in-plane coordinates)")
            positions = [coords]
        if diameter is None and not extra.get("thread") and not extra.get("standard"):
            raise CommandError("give a diameter, a thread like 'M8', or a Hole Wizard standard/type/size (e.g. ISO counterbore M6)")
        params = {"plane": plane, "depth": depth, "positions": positions, **extra}
        if diameter is not None:
            params["diameter"] = diameter
        return self._cmd_add_feature(spec, id, "hole", params)

    def _cmd_add_pocket(self, spec, id, length, width, depth, x, y, face="top", corner_radius=0):
        """Convenience wrapper: rectangular pocket centred at (x, y) in the face's coordinates."""
        if face not in FACE_NAMES:
            raise CommandError(f"face must be one of {', '.join(FACE_NAMES)}")
        shape = {"type": "rect", "w": length, "h": width, "x": x, "y": y}
        if corner_radius:
            shape["r"] = corner_radius
        params = {"plane": {"face": FACE_NAMES[face]}, "op": "cut", "distance": depth, "shapes": [shape]}
        return self._cmd_add_feature(spec, id, "extrude", params)

    def _cmd_update_feature(self, spec, id, params):
        f = self._feature(spec, id)
        if not isinstance(params, dict):
            raise CommandError("params must be an object")
        for k, v in params.items():
            if v is None:
                f.params.pop(k, None)
            else:
                f.params[k] = v
        return f"updated {id}: {', '.join(params)}"

    def _cmd_remove_feature(self, spec, id):
        f = self._feature(spec, id)
        users = dependents_of(spec, id)
        if users:
            raise CommandError(f"{id} is used by {', '.join(users)}; remove or change those first")
        spec.features.remove(f)
        return f"removed {id}"

    def _cmd_suppress_feature(self, spec, id, suppressed=True):
        self._feature(spec, id).suppressed = bool(suppressed)
        return f"{'suppressed' if suppressed else 'unsuppressed'} {id}"

    def _cmd_set_parameter(self, spec, name, value):
        target, _, rest = name.partition(".")
        if not rest:
            raise CommandError(f"parameter name must look like 'feature.param' or 'vars.name', got {name!r}")
        if target == "vars":
            if rest not in spec.variables:
                raise CommandError(f"no variable {rest!r}; use set_variable to create one")
            old, spec.variables[rest] = spec.variables[rest], value
        else:
            f = self._feature(spec, target)
            try:
                old = set_path(f.params, parse_path(rest), value)
            except (KeyError, IndexError, TypeError):
                raise CommandError(f"{target} has no parameter {rest!r}; use update_feature to add new params")
        return f"{name}: {old} -> {value}"

    def _cmd_set_name(self, spec, name):
        spec.name = str(name)
        return f"part renamed to {name!r}"

    def _cmd_assign_material(self, spec, name, density=None, E=None, nu=None, yield_strength=None):
        if density is None:
            density = MATERIALS.get(name.lower())
            if density is None:
                raise CommandError(f"unknown material {name!r}; give a density in kg/m^3 or use one of {', '.join(MATERIALS)}")
        if not isinstance(density, (int, float)) or density <= 0:
            raise CommandError("density must be a positive number in kg/m^3")
        mech = MECHANICAL.get(name.lower(), {})
        E = E if E is not None else mech.get("E")
        nu = nu if nu is not None else mech.get("nu")
        ys = yield_strength if yield_strength is not None else mech.get("yield")
        if E is not None and E <= 0 or nu is not None and not 0 <= nu < 0.5 or ys is not None and ys <= 0:
            raise CommandError("E and yield strength must be positive (MPa), Poisson's ratio in [0, 0.5)")
        spec.material = Material(name=name, density=float(density), E=E, nu=nu, yield_strength=ys)
        extra = f", E {E:g} MPa, yield {ys:g} MPa (typical values)" if E and ys else ""
        return f"material set to {name} ({density} kg/m^3{extra})"

    def _cmd_set_sheet_metal(self, spec, thickness, bend_radius, k_factor=0.44):
        """Make this a sheet-metal part (or change its parameters). K-factor 0.44 is a common default for air bending."""
        spec.sheet = {"thickness": thickness, "bend_radius": bend_radius, "k_factor": k_factor}
        return f"sheet metal: t={thickness}, inside bend radius {bend_radius}, K={k_factor}"

    def _cmd_set_analysis_setup(self, spec, supports, loads):
        if not isinstance(supports, list) or not isinstance(loads, list):
            raise CommandError("supports and loads must be lists")
        for i, s_ in enumerate(supports):
            s_["face"] = self._face_ref(s_.get("face"))
        for i, ld in enumerate(loads):
            ld["face"] = self._face_ref(ld.get("face"))
            if "force" in ld:
                f = ld["force"]
                if not (isinstance(f, list) and len(f) == 3 and all(isinstance(c, (int, float)) for c in f)):
                    raise CommandError(f"load {i + 1}: force must be [Fx, Fy, Fz] in N")
            elif not isinstance(ld.get("pressure"), (int, float)):
                raise CommandError(f"load {i + 1}: give force [Fx, Fy, Fz] (N) or pressure (MPa)")
        spec.analysis = {"supports": supports, "loads": loads}
        return f"analysis setup: {len(supports)} support(s), {len(loads)} load(s)"

    # ---------- annotations (tolerances, datums, GD&T, surface finish) ----------

    def _cmd_add_annotation(self, spec, type, id=None, **fields):
        from .tolerances import _FIT, CHARACTERISTICS, check_gdt, general_tolerance

        existing = {a["id"] for a in spec.annotations}
        if id is None:
            n = 1
            while f"{type}_{n}" in existing:
                n += 1
            id = f"{type}_{n}"
        elif id in existing:
            raise CommandError(f"annotation id {id!r} already exists")
        a: dict[str, Any] = {"id": id, "type": type}
        if type == "tolerance":
            if fields.get("parameter") not in spec.parameters():
                raise CommandError(f"no parameter {fields.get('parameter')!r}")
            if fields.get("fit"):
                if not _FIT.match(str(fields["fit"])):
                    raise CommandError(f"tolerance class {fields['fit']!r} not understood; use e.g. H7 or g6")
                a.update(parameter=fields["parameter"], fit=str(fields["fit"]).strip())
            else:
                plus, minus = fields.get("plus"), fields.get("minus")
                if not isinstance(plus, (int, float)) or not isinstance(minus, (int, float)) or plus < minus:
                    raise CommandError("give plus and minus deviations in mm (plus >= minus), e.g. plus=0.1, minus=-0.1, or a fit like H7")
                a.update(parameter=fields["parameter"], plus=float(plus), minus=float(minus))
            if any(x.get("type") == "tolerance" and x.get("parameter") == a["parameter"] for x in spec.annotations):
                raise CommandError(f"{a['parameter']} already has a tolerance; remove it first")
        elif type == "general_tolerance":
            try:
                general_tolerance(str(fields.get("standard", "")), 10)
            except ValueError as e:
                raise CommandError(str(e))
            spec.annotations = [x for x in spec.annotations if x.get("type") != "general_tolerance"]
            a["standard"] = fields["standard"]
        elif type == "datum":
            label = str(fields.get("label", "")).upper()
            if not (len(label) == 1 and label.isalpha()) or label in {"I", "O", "Q"}:
                raise CommandError("datum label must be one letter (not I, O or Q)")
            if any(x.get("type") == "datum" and x.get("label") == label for x in spec.annotations):
                raise CommandError(f"datum {label} already exists")
            a.update(label=label, face=self._face_ref(fields.get("face")))
        elif type == "gdt":
            ch = fields.get("characteristic")
            tol = fields.get("tolerance")
            if not isinstance(tol, (int, float)) or tol <= 0:
                raise CommandError("tolerance must be a positive number in mm")
            datums = [str(d).upper() for d in fields.get("datums") or []]
            defined = {x["label"] for x in spec.annotations if x.get("type") == "datum"}
            try:
                check_gdt(ch, datums, defined, fields.get("modifier"), bool(fields.get("diameter_zone")))
            except ValueError as e:
                raise CommandError(str(e))
            target = fields.get("target") or {}
            if "feature" in target:
                if not spec.feature(target["feature"]):
                    raise CommandError(f"no feature {target['feature']!r}")
                target = {"feature": target["feature"]}
            else:
                target = {"face": self._face_ref(target.get("face"))}
            a.update(characteristic=ch, tolerance=float(tol), datums=datums, modifier=fields.get("modifier") or None,
                     diameter_zone=bool(fields.get("diameter_zone")), target=target)
        elif type == "surface_finish":
            ra = fields.get("ra")
            if not isinstance(ra, (int, float)) or ra <= 0:
                raise CommandError("ra must be a positive roughness in µm (e.g. 1.6)")
            a["ra"] = float(ra)
            if fields.get("all"):
                spec.annotations = [x for x in spec.annotations if not (x.get("type") == "surface_finish" and x.get("all"))]
                a["all"] = True
            else:
                a["face"] = self._face_ref(fields.get("face"))
        else:
            raise CommandError("type must be tolerance, general_tolerance, datum, gdt or surface_finish")
        spec.annotations.append(a)
        return f"added {type} {id}"

    @staticmethod
    def _face_ref(ref):
        if not isinstance(ref, dict) or not isinstance(ref.get("point"), list) or len(ref["point"]) != 3:
            raise CommandError("face must be a selection reference {point: [x,y,z], normal: [x,y,z]}")
        return {"point": [float(c) for c in ref["point"]], **({"normal": [float(c) for c in ref["normal"]]} if ref.get("normal") else {})}

    def _cmd_remove_annotation(self, spec, id):
        a = next((x for x in spec.annotations if x.get("id") == id), None)
        if a is None:
            raise CommandError(f"no annotation {id!r}")
        if a.get("type") == "datum":
            users = [x["id"] for x in spec.annotations if x.get("type") == "gdt" and a["label"] in x.get("datums", [])]
            if users:
                raise CommandError(f"datum {a['label']} is referenced by {', '.join(users)}")
        spec.annotations.remove(a)
        return f"removed annotation {id}"

    def _cmd_record_design_intent(self, spec, requirements=None, assumptions=None, open_questions=None):
        spec.intent = {"requirements": list(requirements or []), "assumptions": list(assumptions or []),
                       "open_questions": list(open_questions or [])}
        return "recorded design intent"

    def _cmd_attach_source_drawing(self, spec, sha256, filename, media_type, pages):
        spec.source = {**(spec.source or {}), "drawing": {"sha256": sha256, "filename": filename, "media_type": media_type, "pages": pages}}
        spec.source.pop("analysis", None)  # an analysis belongs to one drawing
        return f"attached source drawing {filename} ({pages} page(s))"

    def _cmd_record_drawing_analysis(self, spec, **analysis):
        dims = analysis.get("dimensions") or []
        ids = [d.get("id") for d in dims]
        if len(ids) != len(set(ids)) or not all(isinstance(i, str) and i for i in ids):
            raise CommandError("every dimension needs a unique string id")
        old = {d["id"]: d for d in ((spec.source or {}).get("analysis") or {}).get("dimensions", [])}
        for d in dims:  # keep existing links/confirmations when the analysis is re-recorded
            for k in ("parameter", "confirmed"):
                if k in old.get(d["id"], {}) and k not in d:
                    d[k] = old[d["id"]][k]
        spec.source = {**(spec.source or {}), "analysis": analysis}
        if analysis.get("title") and spec.name == "Part":
            spec.name = str(analysis["title"])
        return f"recorded drawing analysis: {len(dims)} dimensions, {len(analysis.get('questions') or [])} open questions"

    def _dimension(self, spec, dimension_id):
        dims = ((spec.source or {}).get("analysis") or {}).get("dimensions") or []
        d = next((x for x in dims if x.get("id") == dimension_id), None)
        if d is None:
            raise CommandError(f"no drawing dimension {dimension_id!r}")
        return d

    def _cmd_link_dimension(self, spec, dimension_id, parameter):
        d = self._dimension(spec, dimension_id)
        if parameter not in spec.parameters():
            raise CommandError(f"no parameter {parameter!r}; link to e.g. 'vars.length' or 'hole_1.diameter'")
        d["parameter"] = parameter
        return f"drawing dimension {dimension_id} drives {parameter}"

    def _cmd_update_drawing_dimension(self, spec, dimension_id, value, confirmed=True):
        """User correction of a value read from the drawing; also updates the linked model parameter."""
        d = self._dimension(spec, dimension_id)
        old = d.get("value")
        d["value"], d["confirmed"], d["status"] = value, bool(confirmed), "confirmed" if confirmed else d.get("status")
        msg = f"drawing dimension {dimension_id}: {old} -> {value}"
        if d.get("parameter"):
            msg += "; " + self._cmd_set_parameter(spec, d["parameter"], value)
        return msg

    # ---------- read-only commands (take current spec, return CommandResult) ----------

    def _cmd_get_model(self, spec):
        return CommandResult(True, "current model", {"spec": spec.to_dict(), **self._summary()})

    def _cmd_calculate_mass(self, spec):
        if not self._result:
            raise CommandError("no model to measure")
        if not spec.material:
            raise CommandError("assign a material first")
        vol = self._result.volume_mm3
        mass_kg = vol * 1e-9 * spec.material.density
        return CommandResult(
            True,
            f"mass {mass_kg * 1000:.2f} g ({spec.material.name}, volume {vol:.1f} mm^3)",
            {"mass_kg": mass_kg, "volume_mm3": vol, "density_kg_m3": spec.material.density},
        )

    def _cmd_export_step(self, spec, path):
        if not self._result:
            raise CommandError("no model to export")
        cq.exporters.export(self._result.shape, str(path), exportType="STEP")
        return CommandResult(True, f"exported STEP to {path}", {"path": str(path)})

    def _cmd_export_stl(self, spec, path, tolerance=0.05):
        if not self._result:
            raise CommandError("no model to export")
        cq.exporters.export(self._result.shape, str(path), exportType="STL", tolerance=tolerance)
        return CommandResult(True, f"exported STL to {path}", {"path": str(path)})


COMMANDS = sorted(name[5:] for name in dir(Project) if name.startswith("_cmd_"))
