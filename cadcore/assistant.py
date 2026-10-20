# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversational design assistant: Claude plans, the CAD core executes.

Claude never touches geometry or runs code. It works through a fixed set of
validated tools that map onto Project commands, so chat and manual controls edit
the same model. Each user turn includes the current model state (so manual edits
are always visible), and tool calls run one at a time so later steps can use the
results of earlier ones.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator

import anthropic

from .builder import FEATURE_TYPES
from .builder import __doc__ as FEATURE_DOC
from .sketch import __doc__ as SKETCH_DOC
from .deps import feature_dependencies, spec_diff
from .materials import MATERIALS
from .mesh import describe, measure, resolve
from .project import FACE_NAMES, Project

_NUM = {"type": "number"}
_NUM_OR_FORMULA = {"anyOf": [{"type": "number"}, {"type": "string"}],
                   "description": "Number in mm/degrees, a value with unit like \"2 in\", or a formula like \"=width/2\"."}
_REF = {
    "type": "object",
    "description": "A selection reference exactly as returned by get_selection or list_entities.",
    "properties": {"kind": {"type": "string", "enum": ["face", "edge", "vertex"]}, "point": {"type": "array", "items": _NUM},
                   "type": {"type": "string"}, "normal": {"type": "array", "items": _NUM}, "area": _NUM, "length": _NUM},
    "required": ["kind", "point", "type"],
}


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}


_COMMAND_ITEM = {"type": "object", "properties": {"command": {"type": "string"}, "args": {"type": "object"}},
                 "required": ["command", "args"]}

TOOLS = [
    # ---- inspection ----
    _tool("get_model", "Full current model: variables (raw and evaluated), feature tree with params, dependencies, "
          "material, volume, bounding box, design intent.", {}, []),
    _tool("get_selection", "What the user currently has selected in the 3D view, re-checked against the current model.", {}, []),
    _tool("list_entities", "List faces or edges of the current solid with owning feature and geometric signature. "
          "Use to find faces for sketch planes or edges for fillets.",
          {"kind": {"type": "string", "enum": ["faces", "edges"]}, "feature": {"type": "string", "description": "Only entities of this feature."}},
          ["kind"]),
    _tool("measure", "Measure one entity (area, length, diameter) or two (distance, angle).",
          {"refs": {"type": "array", "items": _REF, "minItems": 1, "maxItems": 2}}, ["refs"]),
    _tool("calculate_mass", "Mass from the solid's volume and the explicitly assigned material density.", {}, []),
    _tool("render_views", "Render the current solid as a technical drawing image (front/top/side hidden-line views, iso, "
          "overall and hole dimensions) and look at it. Use it to check your work, and to compare against a source drawing.",
          {"projection": {"type": "string", "enum": ["first", "third"]}}, []),
    # ---- modelling ----
    _tool("new_part", "Clear the model and start an empty part.", {"name": {"type": "string"}}, []),
    _tool("create_plate", "Template: replace the model with a parametric plate (variables length, width, thickness; "
          "corner at origin, top face at z=thickness).",
          {"length": _NUM, "width": _NUM, "thickness": _NUM}, ["length", "width", "thickness"]),
    _tool("create_l_bracket", "Template: replace the model with an L-bracket (variables length, width, thickness, height; "
          "base on XY, upright at x=0..thickness).",
          {"length": _NUM, "width": _NUM, "thickness": _NUM, "height": _NUM}, ["length", "width", "thickness", "height"]),
    _tool("create_shaft", "Template: replace the model with a stepped round shaft along +Z (variables d1,l1,d2,l2,...).",
          {"segments": {"type": "array", "items": {"type": "object", "properties": {"diameter": _NUM, "length": _NUM},
                                                   "required": ["diameter", "length"]}}}, ["segments"]),
    _tool("create_flange", "Template: replace the model with a round flange with a bolt circle.",
          {"outer_diameter": _NUM, "inner_diameter": _NUM, "thickness": _NUM, "bolt_circle_diameter": _NUM,
           "bolt_count": {"type": "integer"}, "bolt_diameter": _NUM},
          ["outer_diameter", "inner_diameter", "thickness", "bolt_circle_diameter", "bolt_count", "bolt_diameter"]),
    _tool("set_variable", "Create or change a named variable. Other values can reference it as \"=name\". "
          "Optional limits: values must stay > min and <= max (use min 0 for physical dimensions).",
          {"name": {"type": "string"}, "value": _NUM_OR_FORMULA, "min": _NUM, "max": _NUM}, ["name", "value"]),
    _tool("set_parameter", "Change one existing value by path, e.g. 'vars.thickness', 'hole_1.diameter', "
          "'base.shapes[0].w', 'hole_1.positions[2][0]'.",
          {"name": {"type": "string"}, "value": {"anyOf": [_NUM, {"type": "string"}, {"type": "boolean"}, {"type": "array"}, {"type": "object"}]}},
          ["name", "value"]),
    _tool("add_feature", "Add a feature to the end of the feature tree (or after another feature).\n" + (FEATURE_DOC or "")
          + "\nSKETCHES (type \"sketch\"): use for any outline that is not a simple shape. Constrain it fully (fix one point, "
          "dimension everything) and use formulas for driving dimensions.\n" + (SKETCH_DOC or ""),
          {"id": {"type": "string", "description": "Unique snake_case id, e.g. mounting_holes"},
           "type": {"type": "string", "enum": FEATURE_TYPES},
           "params": {"type": "object"}, "after": {"type": "string"}},
          ["id", "type", "params"]),
    _tool("add_hole", "Convenience: holes on a named face of the current part. Positions are global coordinates of the "
          f"face's two in-plane axes (top/bottom: [x,y]; front/back: [x,z]; left/right: [y,z]). Faces: {', '.join(FACE_NAMES)}.",
          {"id": {"type": "string"}, "diameter": _NUM_OR_FORMULA, "face": {"type": "string", "enum": list(FACE_NAMES)},
           "positions": {"type": "array", "items": {"type": "array", "items": _NUM_OR_FORMULA, "minItems": 2, "maxItems": 2}},
           "depth": _NUM_OR_FORMULA, "cbore_diameter": _NUM_OR_FORMULA, "cbore_depth": _NUM_OR_FORMULA,
           "csk_diameter": _NUM_OR_FORMULA, "csk_angle": _NUM_OR_FORMULA,
           "thread": {"type": "string", "description": "Tapped hole, ISO metric e.g. 'M8' or 'M8x1'; diameter defaults to the tap drill"},
           "thread_depth": _NUM_OR_FORMULA},
          ["id", "positions"]),
    _tool("update_feature", "Set or add params on an existing feature (null removes a param).",
          {"id": {"type": "string"}, "params": {"type": "object"}}, ["id", "params"]),
    _tool("remove_feature", "Delete a feature (refused if other features depend on it).", {"id": {"type": "string"}}, ["id"]),
    _tool("suppress_feature", "Temporarily disable or re-enable a feature.",
          {"id": {"type": "string"}, "suppressed": {"type": "boolean"}}, ["id", "suppressed"]),
    _tool("assign_material", f"Set the material. Known: {', '.join(MATERIALS)} (with typical E/nu/yield). Otherwise give "
          "density in kg/m^3, and for stress analysis E (MPa), nu and yield_strength (MPa).",
          {"name": {"type": "string"}, "density": _NUM, "E": _NUM, "nu": _NUM, "yield_strength": _NUM}, ["name"]),
    _tool("set_name", "Rename the part.", {"name": {"type": "string"}}, ["name"]),
    _tool("record_design_intent", "Record what the user actually required versus what you assumed, plus open questions. "
          "Call this whenever requirements or assumptions change.",
          {"requirements": {"type": "array", "items": {"type": "string"}},
           "assumptions": {"type": "array", "items": {"type": "string"}},
           "open_questions": {"type": "array", "items": {"type": "string"}}}, ["requirements", "assumptions"]),
    _tool("propose_alternatives", "Offer 2-3 redesign options for the user to compare and accept. Each option is a list of "
          "modelling commands ({command, args}) using the same command names as the tools. They are evaluated but NOT applied.",
          {"alternatives": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
              "type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                                               "commands": {"type": "array", "items": _COMMAND_ITEM}},
              "required": ["title", "description", "commands"]}}}, ["alternatives"]),
    _tool("undo", "Undo the last modelling change.", {}, []),
    # ---- manufacturing annotations ----
    _tool("add_annotation", "Add a manufacturing annotation shown on 2D drawings. Types and fields:\n"
          "tolerance: parameter (e.g. 'bore.diameter', 'vars.length') + plus/minus in mm, OR fit (ISO 286: holes D E F G H JS, shafts d e f g h js k m n p, IT5-IT11, e.g. 'H7', 'g6').\n"
          "general_tolerance: standard 'ISO 2768-f|m|c|v' optionally with H|K|L (e.g. 'ISO 2768-mK').\n"
          "datum: label (one letter), face {point, normal} from get_selection/list_entities.\n"
          "gdt: characteristic (straightness flatness circularity cylindricity profile_line profile_surface parallelism perpendicularity "
          "angularity position concentricity symmetry circular_runout total_runout), tolerance (mm), datums [labels] "
          "(none for form tolerances; required for orientation/runout), diameter_zone, modifier M|L, target {face: {point, normal}} or {feature: id}.\n"
          "surface_finish: ra (µm) with face {point, normal}, or all: true for the default.\n"
          "Only add tolerances the user asked for or approved; never invent them.",
          {"type": {"type": "string", "enum": ["tolerance", "general_tolerance", "datum", "gdt", "surface_finish"]},
           "parameter": {"type": "string"}, "plus": _NUM, "minus": _NUM, "fit": {"type": "string"}, "standard": {"type": "string"},
           "label": {"type": "string"}, "face": {"type": "object"}, "characteristic": {"type": "string"}, "tolerance": _NUM,
           "datums": {"type": "array", "items": {"type": "string"}}, "diameter_zone": {"type": "boolean"},
           "modifier": {"type": "string", "enum": ["M", "L"]}, "target": {"type": "object"}, "ra": _NUM, "all": {"type": "boolean"}},
          ["type"]),
    _tool("remove_annotation", "Remove an annotation by id.", {"id": {"type": "string"}}, ["id"]),
    # ---- sheet metal ----
    _tool("set_sheet_metal", "Make the part a sheet-metal part (or change its parameters). Then use add_feature with "
          "sheet_base (profile -> flat base of the sheet thickness) and sheet_flange (bend + flange on a straight base edge). "
          "Holes/through-cuts in the base appear in the flat pattern.",
          {"thickness": _NUM_OR_FORMULA, "bend_radius": _NUM_OR_FORMULA, "k_factor": _NUM}, ["thickness", "bend_radius"]),
    _tool("get_flat_pattern", "Unfold the sheet-metal part: flat size, area and bend list (or why it cannot be unfolded).", {}, []),
    # ---- engineering analysis ----
    _tool("check_manufacturability", "Rule-based design-for-manufacturing screening of the current part: thin walls, "
          "sharp internal corners, undercuts / setups, small or deep holes (CNC), overhangs (FDM). Report findings with their thresholds.",
          {"process": {"type": "string", "enum": ["cnc_milling", "fdm_printing"]}}, ["process"]),
    _tool("run_stress_analysis", "Linear-static FEA (quadratic tetrahedra). Requires supports and loads that the USER stated - "
          "never invent load magnitudes or support locations; ask. Faces are references {point, normal} from get_selection or "
          "list_entities. Uses the assigned material's E, nu and yield (assign_material may set them). Report max von Mises stress, "
          "deflection, safety factor, convergence and the assumptions; do not call a part 'safe' beyond what the numbers show.",
          {"supports": {"type": "array", "items": {"type": "object", "properties": {"face": {"type": "object"}}, "required": ["face"]}},
           "loads": {"type": "array", "items": {"type": "object", "properties": {
               "face": {"type": "object"}, "force": {"type": "array", "items": _NUM, "minItems": 3, "maxItems": 3},
               "pressure": _NUM}, "required": ["face"]}},
           "check_convergence": {"type": "boolean"}}, ["supports", "loads"]),
    # ---- drawing interpretation ----
    _tool("record_drawing_analysis", "Record what you read from the attached engineering drawing, BEFORE modelling. "
          "Every dimension needs a unique id, the value exactly as printed, what it measures, where it is (page and "
          "bbox as fractions 0..1 of the page image, origin top-left), and how sure you are. Never derive a value by "
          "measuring the picture; if a needed value is not printed, add a question instead.",
          {"title": {"type": "string"}, "units": {"type": "string", "description": "e.g. mm, inch"},
           "projection": {"type": "string", "enum": ["first", "third", "unknown"]},
           "views": {"type": "array", "items": {"type": "string"}},
           "dimensions": {"type": "array", "items": {"type": "object", "properties": {
               "id": {"type": "string"}, "value": _NUM, "unit": {"type": "string"},
               "kind": {"type": "string", "enum": ["linear", "diameter", "radius", "angle", "thread", "depth", "count", "other"]},
               "tolerance": {"type": "string"}, "describes": {"type": "string"}, "view": {"type": "string"},
               "page": {"type": "integer"}, "bbox": {"type": "array", "items": _NUM, "minItems": 4, "maxItems": 4},
               "status": {"type": "string", "enum": ["read", "inferred", "unclear"]},
               "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
               "required": ["id", "value", "kind", "describes", "status", "confidence"]}},
           "features": {"type": "array", "items": {"type": "object", "properties": {
               "description": {"type": "string"}, "dimension_ids": {"type": "array", "items": {"type": "string"}}},
               "required": ["description"]}},
           "notes": {"type": "array", "items": {"type": "string"}},
           "questions": {"type": "array", "items": {"type": "string"}}},
          ["units", "dimensions", "questions"]),
    _tool("link_dimension", "Record that a drawing dimension drives a model parameter (e.g. D3 -> vars.width), "
          "so it stays traceable and user corrections propagate.",
          {"dimension_id": {"type": "string"}, "parameter": {"type": "string"}}, ["dimension_id", "parameter"]),
    _tool("update_drawing_dimension", "Correct a drawing dimension value after the user clarified it; also updates the linked parameter.",
          {"dimension_id": {"type": "string"}, "value": _NUM_OR_FORMULA, "confirmed": {"type": "boolean"}}, ["dimension_id", "value"]),
]

_MUTATING = {"new_part", "create_plate", "create_l_bracket", "create_shaft", "create_flange", "set_variable", "set_parameter",
             "add_feature", "add_hole", "update_feature", "remove_feature", "suppress_feature", "assign_material",
             "set_name", "record_design_intent", "undo", "record_drawing_analysis", "add_annotation", "remove_annotation", "run_stress_analysis", "set_sheet_metal", "link_dimension", "update_drawing_dimension"}

INTERPRET = """Interpret the attached engineering drawing and turn it into an editable parametric model.

1. Call record_drawing_analysis first: every printed dimension (value exactly as printed, unit, what it measures, page and bbox), the projection method, notes, and questions for anything missing, ambiguous or illegible. Convert nothing yet.
2. If the drawing fully defines the part, build it: variables named after what they mean (length, width, hole_d...), features using those variables, then link_dimension for every dimension you used. Prefer sketches for non-rectangular outlines.
   If essential values are missing, ask the questions now and build only after the user answers (or tells you to assume).
3. After building, call render_views and compare the result with the drawing view by view. Report any mismatch.
4. record_design_intent: requirements = what the drawing states; assumptions = everything you assumed."""

SYSTEM = """You are the design assistant inside a parametric mechanical CAD application. You build and edit ONE shared parametric model that the user also edits with manual controls.

How you work:
- You can only change the model through the tools. The CAD kernel validates every step; a rejected step leaves the model unchanged and tells you why.
- Run dependent steps one at a time and read each result before the next. If a step fails, stop, explain the cause in plain words, and fix or ask - do not retry blindly.
- Every user message starts with <model_state> - the current model including any manual edits the user made since your last turn. Trust it over your memory.
- Units are mm and degrees. Coordinates are global; the part normally starts at the origin.

Asking vs. assuming:
- Before building, make sure you have every dimension that defines the part's function (sizes, hole diameters and positions, thickness). If any is missing, ask ONE short, focused question listing exactly what is missing, and suggest sensible defaults the user can accept. Do not build until answered unless the user told you to choose.
- Cosmetic or conventional details (e.g. edge distance when the user says "near the corners") you may assume, but say so.
- After building or changing a design, call record_design_intent listing user requirements separately from your assumptions.

Modelling style:
- Put the main dimensions in variables and reference them with formulas ("=length-10") so the design keeps its intent when dimensions change (e.g. holes stay centred: x "=length/2").
- Edit existing features (set_parameter / update_feature) instead of deleting and recreating them. Never touch features unrelated to the request.
- Use clear feature ids (mounting_holes, rib_1, outer_fillet).

Selections:
- <selection> describes what the user clicked. "this", "that hole", "this face" refer to it. If the selection is reported missing or changed, say so and ask the user to re-select; never guess another entity.
- If the selected hole is one instance of a multi-hole feature (hole_instance.count > 1) and the user asks to change "this" hole, ask whether to change all instances or only this one (only-this-one means moving its position into a new hole feature).
- To use a selected face as a sketch/hole plane, use {"face_at": {"point": <point>, "normal": <normal>}}. To fillet/chamfer selected edges use {"near": [<point>, ...]}.

Redesign: when the user asks for options or a change has several reasonable solutions, use propose_alternatives instead of applying one.

Engineering honesty:
- Report volume, mass, stresses and deflections only from tool results, together with the assumptions the tool returns.
- Strength analysis needs supports and loads that the user specified; ask for them, never invent load cases.
- A safety factor relates to the stated loads and the material's yield only. It is not a certification. Mention fatigue, buckling, impact or temperature when they could matter, since those are not analysed.
- Manufacturability checks are rule-of-thumb screening, not a quote or approval.
- Tolerances and GD&T are design decisions: add them only when the user asks or agrees.
- Sheet-metal parts: use set_sheet_metal, sheet_base and sheet_flange, then check get_flat_pattern.
- Assemblies are edited in the Assembly workspace, which has no AI tools yet. Tell the user how to do it there.

Keep replies short: what changed (with values), what you assumed, and any question."""


def _compact_state(project: Project) -> dict[str, Any]:
    spec = project.spec
    state: dict[str, Any] = {
        "name": spec.name,
        "variables": spec.variables,
        "features": [{"id": f.id, "type": f.type, "params": f.params, **({"suppressed": True} if f.suppressed else {})}
                     for f in spec.features],
        "material": spec.material and {"name": spec.material.name, "density_kg_m3": spec.material.density},
    }
    if project.result:
        state["evaluated_variables"] = project.result.variables
        state["volume_mm3"] = round(project.result.volume_mm3, 3)
        state["bbox_mm"] = [round(v, 3) for v in project.result.bbox_mm]
    if spec.intent:
        state["design_intent"] = spec.intent
    if spec.annotations:
        state["annotations"] = spec.annotations
        if project.result:
            from .annotations import evaluate_annotations
            bad = [a for a in evaluate_annotations(spec, project.result) if not a["ok"]]
            if bad:
                state["annotation_problems"] = bad
    return state


def _entity_summary(e: dict[str, Any]) -> dict[str, Any]:
    keep = ("index", "feature", "type", "point", "normal", "area", "length", "radius", "axis_dir", "center", "hole_instance")
    return {k: e[k] for k in keep if k in e}


class Assistant:
    def __init__(self, project: Project, client_factory: Callable[[], Any] | None = None,
                 model: str = "claude-opus-5-5", effort: str = "medium"):
        self.project = project
        self.client_factory = client_factory or anthropic.Anthropic
        self.model = model
        self.effort = effort
        self.messages: list[dict[str, Any]] = []
        self.provider = "anthropic"  # or "openai": any OpenAI-compatible chat-completions API
        self.oa_messages: list[dict[str, Any]] = []
        self.selection: list[dict[str, Any]] = []
        self.alternatives: list[dict[str, Any]] = []
        self.lock = None  # optional threading lock shared with manual edits

    def reset(self) -> None:
        self.messages = []
        self.oa_messages = []
        self.alternatives = []

    # ---------- tool execution ----------

    def _locked(self, fn: Callable[[], Any]) -> Any:
        if self.lock is None:
            return fn()
        with self.lock:
            return fn()

    def _run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        p = self.project
        if name == "get_model":
            if not p.result and p.spec.is_empty:
                return {"ok": True, "message": "the model is empty", "data": _compact_state(p)}
            return {"ok": True, "message": "current model",
                    "data": {**_compact_state(p), "dependencies": feature_dependencies(p.spec)}}
        if name == "get_selection":
            if not self.selection:
                return {"ok": True, "message": "nothing is selected", "data": {}}
            if not p.result:
                return {"ok": False, "message": "the model is empty, the selection is invalid", "data": {}}
            return {"ok": True, "message": "current selection", "data": {"selection": [resolve(p.result, r) for r in self.selection]}}
        if name == "list_entities":
            if not p.result:
                return {"ok": False, "message": "the model is empty", "data": {}}
            d = describe(p.result)[args.get("kind", "faces")]
            items = [_entity_summary(e) for e in d if not args.get("feature") or e.get("feature") == args["feature"]]
            return {"ok": True, "message": f"{len(items)} {args.get('kind')}", "data": {"items": items[:80]}}
        if name == "measure":
            if not p.result:
                return {"ok": False, "message": "the model is empty", "data": {}}
            m = measure(p.result, args.get("refs", []))
            return {"ok": m.pop("ok"), "message": m.pop("message", "measured"), "data": m}
        if name == "undo":
            return p.undo().to_dict()
        if name == "get_flat_pattern":
            if not p.result or p.result.sheet is None:
                return {"ok": False, "message": "not a sheet-metal part", "data": {}}
            from .sheetmetal import flat_pattern
            try:
                fp = flat_pattern(p.result.sheet)
            except ValueError as e:
                return {"ok": False, "message": str(e), "data": {}}
            return {"ok": True, "message": f"flat {fp['size'][0]:.2f} x {fp['size'][1]:.2f} mm",
                    "data": {"size": fp["size"], "area_mm2": fp["area"], "bends": [{k: b[k] for k in ("feature", "label", "allowance")} for b in fp["bends"]]}}
        if name == "check_manufacturability":
            if not p.result:
                return {"ok": False, "message": "the model is empty", "data": {}}
            from .dfm import check
            r = check(p.result, args.get("process", "cnc_milling"))
            return {"ok": True, "message": f"{len(r['findings'])} finding(s)", "data": r}
        if name == "run_stress_analysis":
            if not p.result:
                return {"ok": False, "message": "the model is empty", "data": {}}
            set_r = p.execute("set_analysis_setup", supports=args.get("supports", []), loads=args.get("loads", []))
            if not set_r.ok:
                return set_r.to_dict()
            m = p.spec.material
            if m is None or m.E is None:
                return {"ok": False, "message": "assign a material with E and nu first", "data": {}}
            from .fea import FEAError, analyse
            try:
                r = analyse(p.result.shape.val(), {"E": m.E, "nu": m.nu, **({"yield": m.yield_strength} if m.yield_strength else {})},
                            p.spec.analysis["supports"], p.spec.analysis["loads"], check_convergence=bool(args.get("check_convergence")))
            except FEAError as e:
                return {"ok": False, "message": str(e), "data": {}}
            r.pop("display", None)
            return {"ok": True, "message": f"max von Mises {r['max_von_mises_mpa']:.1f} MPa, max deflection {r['max_displacement_mm']:.4f} mm",
                    "data": r}
        if name == "render_views":
            if not p.result:
                return {"ok": False, "message": "the model is empty", "data": {}}
            from .drawing import make_drawing, to_png
            d = make_drawing(p.result, p.spec, "A3", args.get("projection", "first"))
            import base64
            return {"ok": True, "message": f"rendered drawing, scale {d['scale']}", "data": {"warnings": d["warnings"]},
                    "image": base64.standard_b64encode(to_png(d["svg"], 1600)).decode()}
        if name == "propose_alternatives":
            evaluated = []
            for i, alt in enumerate(args.get("alternatives", [])):
                prev = p.preview(alt.get("commands", []))
                evaluated.append({"id": i, "title": alt.get("title"), "description": alt.get("description"),
                                  "commands": alt.get("commands", []), "preview": prev})
            self.alternatives = evaluated
            return {"ok": True, "message": "alternatives evaluated and shown to the user; none applied",
                    "data": {"alternatives": [{"id": a["id"], "title": a["title"], "preview": a["preview"]} for a in evaluated]}}
        return p.execute(name, **args).to_dict()

    # ---------- conversation ----------

    def _turn_context(self) -> str:
        ctx = f"<model_state>\n{json.dumps(_compact_state(self.project))}\n</model_state>"
        if self.selection and self.project.result:
            sel = [resolve(self.project.result, r) for r in self.selection]
            ctx += f"\n<selection>\n{json.dumps(sel)}\n</selection>"
        analysis = (self.project.spec.source or {}).get("analysis")
        if analysis:
            ctx += f"\n<drawing_analysis>\n{json.dumps(analysis)}\n</drawing_analysis>"
        return ctx

    def stream(self, text: str, selection: list[dict[str, Any]] | None = None,
               attachments: list[dict[str, Any]] | None = None, effort: str | None = None) -> Iterator[dict[str, Any]]:
        """Run one user turn, yielding events:
        {"type": "tool", ...} after each tool, {"type": "alternatives"}, and finally {"type": "done", "reply", "diff"}.
        """
        self.selection = selection or []
        before = self.project.spec.clone()
        if self.provider == "openai":
            yield from self._stream_openai(text, attachments, before)
            return
        start = len(self.messages)
        content: list[dict[str, Any]] = list(attachments or [])
        content += [{"type": "text", "text": self._locked(self._turn_context)}, {"type": "text", "text": text}]
        self.messages.append({"role": "user", "content": content})
        client = self.client_factory()
        try:
            yield from self._loop(client, before, effort or self.effort)
        except Exception:
            del self.messages[start:]  # keep history valid if the API call fails mid-turn
            raise

    def send(self, text: str, selection: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Non-streaming convenience: returns {"reply", "actions", "diff"}."""
        actions, final = [], {}
        for ev in self.stream(text, selection):
            if ev["type"] == "tool":
                actions.append(ev)
            elif ev["type"] == "done":
                final = ev
        return {"reply": final.get("reply", ""), "actions": actions, "diff": final.get("diff")}

    def _loop(self, client, before, effort: str) -> Iterator[dict[str, Any]]:
        for _ in range(25):  # cap model round-trips per user turn
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            self.messages.append({"role": "assistant", "content": response.content})
            texts = [b.text for b in response.content if b.type == "text" and b.text.strip()]

            if response.stop_reason == "refusal":
                yield self._done("The model declined this request.", before)
                return
            if response.stop_reason != "tool_use":
                reply = "\n".join(texts).strip()
                if response.stop_reason == "max_tokens":
                    reply += "\n(reply truncated)"
                yield self._done(reply, before)
                return
            if texts:
                yield {"type": "text", "text": "\n".join(texts)}

            results, failed = [], None
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if failed:  # sequential semantics: later steps depended on the failed one
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": f"skipped: earlier step {failed} failed in this batch; re-plan using its error"})
                    continue
                args = block.input if isinstance(block.input, dict) else {}
                try:
                    out = self._locked(lambda: self._run_tool(block.name, args))
                except TypeError as e:  # wrong/missing arguments
                    out = {"ok": False, "message": f"invalid arguments: {e}", "data": {}}
                if not out["ok"] and block.name in _MUTATING:
                    failed = block.name
                event = {"type": "tool", "tool": block.name, "input": args, "ok": out["ok"], "message": out["message"],
                         "mutating": block.name in _MUTATING}
                yield event
                if block.name == "propose_alternatives":
                    yield {"type": "alternatives", "items": self.alternatives}
                image = out.pop("image", None)
                content: Any = json.dumps(out, default=str)
                if image:  # tool results can carry images: the model looks at the rendered views
                    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}},
                               {"type": "text", "text": content}]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": not out["ok"]})
            self.messages.append({"role": "user", "content": results})

        yield self._done("Stopped after too many steps; check the model and try a smaller request.", before)

    # ---------- OpenAI-compatible providers (OpenAI, Gemini, Groq, OpenRouter, Ollama, LM Studio, ...) ----------

    def _stream_openai(self, text: str, attachments: list[dict[str, Any]] | None, before) -> Iterator[dict[str, Any]]:
        start = len(self.oa_messages)
        parts: list[dict[str, Any]] = []
        for a in attachments or []:  # Anthropic-style blocks -> OpenAI content parts
            if a.get("type") == "image":
                parts.append({"type": "image_url", "image_url": {"url": f"data:{a['source']['media_type']};base64,{a['source']['data']}"}})
            elif a.get("type") == "text":
                parts.append({"type": "text", "text": a["text"]})
        parts += [{"type": "text", "text": self._locked(self._turn_context)}, {"type": "text", "text": text}]
        self.oa_messages.append({"role": "user", "content": parts})
        client = self.client_factory()
        try:
            yield from self._loop_openai(client, before)
        except Exception:
            del self.oa_messages[start:]
            raise

    def _loop_openai(self, client, before) -> Iterator[dict[str, Any]]:
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                 for t in TOOLS]
        for _ in range(25):
            resp = client.chat.completions.create(model=self.model, messages=[{"role": "system", "content": SYSTEM}, *self.oa_messages],
                                                  tools=tools, tool_choice="auto")
            choice = resp.choices[0]
            msg = choice.message
            calls = msg.tool_calls or []
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                                       for c in calls]
            self.oa_messages.append(entry)
            if not calls:
                reply = (msg.content or "").strip()
                if choice.finish_reason == "length":
                    reply += "\n(reply truncated)"
                yield self._done(reply, before)
                return
            if msg.content and msg.content.strip():
                yield {"type": "text", "text": msg.content.strip()}
            failed, images = None, []
            for c in calls:
                if failed:
                    self.oa_messages.append({"role": "tool", "tool_call_id": c.id,
                                             "content": f"skipped: earlier step {failed} failed in this batch; re-plan using its error"})
                    continue
                try:
                    args = json.loads(c.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                    out = self._locked(lambda: self._run_tool(c.function.name, args))
                except (ValueError, TypeError) as e:  # bad JSON or wrong/missing arguments
                    args, out = {}, {"ok": False, "message": f"invalid arguments: {e}", "data": {}}
                if not out["ok"] and c.function.name in _MUTATING:
                    failed = c.function.name
                yield {"type": "tool", "tool": c.function.name, "input": args, "ok": out["ok"], "message": out["message"],
                       "mutating": c.function.name in _MUTATING}
                if c.function.name == "propose_alternatives":
                    yield {"type": "alternatives", "items": self.alternatives}
                image = out.pop("image", None)
                if image:
                    images.append(image)
                self.oa_messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out, default=str)})
            if images:  # tool messages can't carry images in this API: show them in a follow-up user message
                self.oa_messages.append({"role": "user", "content": [{"type": "text", "text": "Rendered views from render_views:"},
                                         *({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{im}"}} for im in images)]})
        yield self._done("Stopped after too many steps; check the model and try a smaller request.", before)

    def _done(self, reply: str, before) -> dict[str, Any]:
        return {"type": "done", "reply": reply, "diff": spec_diff(before, self.project.spec)}

    def accept_alternative(self, index: int) -> dict[str, Any]:
        alt = next((a for a in self.alternatives if a["id"] == index), None)
        if alt is None:
            return {"ok": False, "message": "no such alternative (it may have expired)"}
        r = self._locked(lambda: self.project.execute_batch(alt["commands"], label=f"alternative: {alt['title']}"))
        if r.ok:
            self.messages.append({"role": "user", "content": [{"type": "text", "text":
                                  f"[The user accepted alternative {index}: {alt['title']}. It has been applied.]"}]})
            self.alternatives = []
        return r.to_dict()
