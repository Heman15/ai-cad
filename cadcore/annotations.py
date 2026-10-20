# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manufacturing annotations on the parametric model: tolerances, datums, GD&T frames, surface finish.

Annotations are part of the spec and reference the model by parameter path or by a stored face
signature ({point, normal}). After every rebuild they are re-evaluated: tolerance limits follow
the current nominal value; a face that no longer exists is reported, never re-targeted.
"""

from __future__ import annotations

from typing import Any

import cadquery as cq

from .builder import BuildResult
from .expr import ExprError, evaluate
from .tolerances import fit_limits, general_tolerance

REF_TOL = 0.05


def parameter_value(spec, result: BuildResult | None, name: str) -> float | None:
    if name.startswith("vars."):
        return (result.variables if result else {}).get(name[5:])
    v = spec.parameters().get(name)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return evaluate(v.strip().lstrip("="), result.variables if result else {})
        except ExprError:
            return None
    return None


def find_face(result: BuildResult, ref: dict[str, Any]) -> cq.Face | None:
    try:
        p = cq.Vector(*ref["point"])
        n = cq.Vector(*ref["normal"]).normalized() if ref.get("normal") else None
    except (KeyError, TypeError):
        return None
    v = cq.Vertex.makeVertex(*p.toTuple())
    for f in result.shape.val().Faces():
        if f.distance(v) > REF_TOL:
            continue
        if n is not None and f.geomType() == "PLANE" and f.normalAt(p).normalized().dot(n) < 0.999:
            continue
        return f
    return None


def evaluate_annotations(spec, result: BuildResult | None) -> list[dict[str, Any]]:
    """Status for each annotation: {"id", "type", "ok", "message", ...computed values}."""
    out = []
    for a in spec.annotations:
        st: dict[str, Any] = {"id": a.get("id"), "type": a.get("type"), "ok": True, "message": ""}
        t = a.get("type")
        try:
            if t == "tolerance":
                nominal = parameter_value(spec, result, a["parameter"])
                if nominal is None:
                    raise ValueError(f"parameter {a['parameter']} no longer exists or has no numeric value")
                if a.get("fit"):
                    upper, lower = fit_limits(abs(nominal), a["fit"])
                else:
                    upper, lower = float(a.get("plus", 0)), float(a.get("minus", 0))
                st.update(nominal=nominal, upper=upper, lower=lower, max=nominal + upper, min=nominal + lower)
            elif t == "general_tolerance":
                general_tolerance(a["standard"], 10)
            elif t in ("datum", "gdt", "surface_finish"):
                target = a.get("face") or (a.get("target") or {}).get("face")
                feature = (a.get("target") or {}).get("feature")
                if feature is not None:
                    if not spec.feature(feature):
                        raise ValueError(f"target feature {feature} no longer exists")
                elif target is not None:
                    if result is None or find_face(result, target) is None:
                        raise ValueError("the referenced face no longer exists after the last change; re-select it")
                elif not a.get("all"):
                    raise ValueError("no target")
        except (ValueError, KeyError) as e:
            st.update(ok=False, message=str(e))
        out.append(st)
    return out


# ---------------- drawing support ----------------

GDT_LABEL = {
    "straightness": "straightness", "flatness": "flatness", "circularity": "circularity", "cylindricity": "cylindricity",
    "profile_line": "profile of a line", "profile_surface": "profile of a surface", "parallelism": "parallelism",
    "perpendicularity": "perpendicularity", "angularity": "angularity", "position": "position",
    "concentricity": "concentricity", "symmetry": "symmetry", "circular_runout": "circular runout", "total_runout": "total runout",
}


def symbol_svg(ch: str, x: float, y: float, s: float = 5.0) -> str:
    """Vector GD&T symbol centred in a box of size s at (x, y) top-left (no font dependency)."""
    cx, cy, r = x + s / 2, y + s / 2, s * 0.32
    L = lambda x1, y1, x2, y2: f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" class="sym"/>'  # noqa: E731
    C = lambda rr, cx_=cx, cy_=cy: f'<circle cx="{cx_:.2f}" cy="{cy_:.2f}" r="{rr:.2f}" class="sym"/>'  # noqa: E731
    a, b = x + s * 0.15, x + s * 0.85
    top, bot = y + s * 0.25, y + s * 0.75
    if ch == "straightness":
        return L(a, cy, b, cy)
    if ch == "flatness":
        return f'<polygon points="{a + 1:.2f},{top:.2f} {b:.2f},{top:.2f} {b - 1:.2f},{bot:.2f} {a:.2f},{bot:.2f}" class="sym"/>'
    if ch == "circularity":
        return C(r)
    if ch == "cylindricity":
        return C(r * 0.8) + L(a + 0.6, bot, cx - 0.2, top) + L(cx + 0.2, bot, b - 0.6, top)
    if ch == "profile_line":
        return f'<path d="M {a:.2f} {bot:.2f} A {r * 1.4:.2f} {r * 1.4:.2f} 0 0 1 {b:.2f} {bot:.2f}" class="sym"/>'
    if ch == "profile_surface":
        return f'<path d="M {a:.2f} {bot:.2f} A {r * 1.4:.2f} {r * 1.4:.2f} 0 0 1 {b:.2f} {bot:.2f} Z" class="sym"/>'
    if ch == "parallelism":
        return L(a, bot, cx - 0.3, top) + L(cx + 0.3, bot, b, top)
    if ch == "perpendicularity":
        return L(cx, top, cx, bot) + L(a, bot, b, bot)
    if ch == "angularity":
        return L(a, bot, b, bot) + L(a, bot, b, top)
    if ch == "position":
        return C(r * 0.75) + L(a, cy, b, cy) + L(cx, top - 0.5, cx, bot + 0.5)
    if ch == "concentricity":
        return C(r) + C(r * 0.45)
    if ch == "symmetry":
        return L(a, cy, b, cy) + L(a + 0.8, top + 0.4, b - 0.8, top + 0.4) + L(a + 0.8, bot - 0.4, b - 0.8, bot - 0.4)
    if ch == "circular_runout":
        return L(a, bot, b, top) + f'<polygon points="{b:.2f},{top:.2f} {b - 1.4:.2f},{top + 0.2:.2f} {b - 0.5:.2f},{top + 1.2:.2f}" class="symfill"/>'
    if ch == "total_runout":
        return (L(a, bot, b - 1, top) + L(a + 1, bot, b, top) + L(a, bot, a + 1, bot)
                + f'<polygon points="{b:.2f},{top:.2f} {b - 1.4:.2f},{top + 0.2:.2f} {b - 0.5:.2f},{top + 1.2:.2f}" class="symfill"/>')
    return ""
