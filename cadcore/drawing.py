# SPDX-License-Identifier: AGPL-3.0-or-later
"""2D technical drawings from the solid: hidden-line orthographic views, dimensions, hole callouts.

The views come from OpenCascade's exact hidden-line removal (HLR). The automatic dimensioning
is deliberately limited and conservative:
- overall size;
- hole position dimensions from the part's datum edges;
- hole callouts with diameter, depth, counterbore and countersink.

The drawing does not invent tolerances or surface finish. It says so in the notes, and it must
be checked before use for manufacturing.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

import cadquery as cq
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

from .annotations import GDT_LABEL, evaluate_annotations, find_face, symbol_svg
from .builder import BuildResult
from .spec import PartSpec

SHEETS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0)}
SCALES = [(10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50)]

# view name -> (direction toward the viewer, screen x direction)
VIEWS = {
    "front": ((0, -1, 0), (1, 0, 0)),
    "top": ((0, 0, 1), (1, 0, 0)),
    "right": ((1, 0, 0), (0, 1, 0)),
    "left": ((-1, 0, 0), (0, -1, 0)),
    "iso": ((1, -1, 1), (1, 1, 0)),
}


@dataclass
class View2D:
    name: str
    visible: list[list[tuple[float, float]]]
    hidden: list[list[tuple[float, float]]]
    xdir: cq.Vector
    ydir: cq.Vector
    bbox: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax (model mm, y up)

    def project(self, p: cq.Vector) -> tuple[float, float]:
        return p.dot(self.xdir), p.dot(self.ydir)


def _polylines(compound) -> list[list[tuple[float, float]]]:
    if compound is None or compound.IsNull():
        return []
    out = []
    for e in cq.Shape.cast(compound).Edges():
        if e.geomType() == "LINE":
            pts = [e.startPoint(), e.endPoint()]
        else:
            n = max(8, min(64, int(e.Length() / 0.5)))
            pts = [e.positionAt(i / n) for i in range(n + 1)]
        out.append([(p.x, p.y) for p in pts])
    return out


def project_view(shape: cq.Shape, name: str, direction: tuple | None = None) -> View2D:
    n, x = direction or VIEWS[name]
    nv, xv = cq.Vector(*n).normalized(), cq.Vector(*x).normalized()
    algo = HLRBRep_Algo()
    algo.Add(shape.wrapped)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*nv.toTuple()), gp_Dir(*xv.toTuple()))))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    visible = _polylines(h.VCompound()) + _polylines(h.OutLineVCompound())
    hidden = [] if name == "iso" or name.startswith("section") else _polylines(h.HCompound()) + _polylines(h.OutLineHCompound())
    pts = [p for pl in visible for p in pl] or [(0.0, 0.0)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return View2D(name, visible, hidden, xv, nv.cross(xv), (min(xs), min(ys), max(xs), max(ys)))


# ---------------- section views ----------------

_SECTION_AXIS = {"YZ": 0, "XZ": 1, "XY": 2}
# plane -> (direction toward the viewer, screen x), and the same looking from the other side (flip)
_SECTION_DIRS = {
    "XZ": (((0, -1, 0), (1, 0, 0)), ((0, 1, 0), (-1, 0, 0))),
    "YZ": (((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, -1, 0))),
    "XY": (((0, 0, 1), (1, 0, 0)), ((0, 0, -1), (1, 0, 0))),
}
_PARENT_PREF = {"XZ": ("top", "left", "right"), "YZ": ("front", "top"), "XY": ("front", "left", "right")}


@dataclass
class Section:
    label: str
    plane: str
    offset: float
    parent: str
    view: View2D
    hatch: list[list[list[tuple[float, float]]]]  # per cut face: closed loops in view coordinates
    sight: cq.Vector  # direction of sight (toward the kept material)


def _chain(edges: list[cq.Edge]) -> list[cq.Vector]:
    """Points along a closed wire, edges joined end to end whatever their orientation."""
    pieces = []
    for e in edges:
        if e.geomType() == "LINE":
            pieces.append([e.startPoint(), e.endPoint()])
        else:
            n = max(8, min(96, int(e.Length() / 0.5)))
            pieces.append([e.positionAt(i / n) for i in range(n + 1)])
    pts = pieces.pop(0)
    while pieces:
        best = min(range(len(pieces)), key=lambda i: min((pieces[i][0] - pts[-1]).Length, (pieces[i][-1] - pts[-1]).Length))
        seg = pieces.pop(best)
        if (seg[-1] - pts[-1]).Length < (seg[0] - pts[-1]).Length:
            seg = seg[::-1]
        pts.extend(seg[1:])
    return pts


def make_section(solid: cq.Shape, spec: dict[str, Any], label: str, side: str) -> Section:
    """Cut the solid by an axis plane, keep the far side and project it; raises ValueError (user-facing)."""
    if not isinstance(spec, dict):
        raise ValueError("each section must be an object like {\"plane\": \"XZ\", \"offset\": 10}")
    plane = str(spec.get("plane", "XZ")).upper()
    if plane not in _SECTION_AXIS:
        raise ValueError(f"section plane must be XY, YZ or XZ, got {spec.get('plane')!r}")
    ax = _SECTION_AXIS[plane]
    bb = solid.BoundingBox()
    lo, hi = [(bb.xmin, bb.xmax), (bb.ymin, bb.ymax), (bb.zmin, bb.zmax)][ax]
    offset = (lo + hi) / 2 if spec.get("offset") is None else float(spec["offset"])
    if not lo + 1e-6 < offset < hi - 1e-6:
        raise ValueError(f"section {label}-{label}: {plane} plane at {'xyz'[ax]}={offset:g} misses the part "
                         f"(it spans {lo:g} … {hi:g})")
    (d, xd) = _SECTION_DIRS[plane][1 if spec.get("flip") else 0]
    dv = cq.Vector(*d)
    # remove the material between the cutting plane and the viewer
    big = bb.DiagonalLength + 10
    lo3 = [bb.xmin - 5, bb.ymin - 5, bb.zmin - 5]
    size = [bb.xlen + 10, bb.ylen + 10, bb.zlen + 10]
    if d[ax] > 0:
        lo3[ax], size[ax] = offset, big
    else:
        lo3[ax], size[ax] = offset - big, big
    kept = solid.cut(cq.Solid.makeBox(*size, cq.Vector(*lo3)))
    cut_faces = [f for f in kept.Faces() if f.geomType() == "PLANE" and f.normalAt().dot(dv) > 0.999
                 and abs(f.Center().toTuple()[ax] - offset) < 1e-6]
    if not cut_faces:
        raise ValueError(f"section {label}-{label} does not cut any material")
    parent = spec.get("view") or next((v for v in _PARENT_PREF[plane] if v in ("front", "top", side)), None)
    if parent not in ("front", "top", side) or abs(cq.Vector(*VIEWS[parent][0]).dot(cq.Vector(*_UNIT[ax]))) > 1e-9:
        raise ValueError(f"section {label}-{label}: the {plane} plane is not seen edge-on in the {parent} view")
    view = project_view(kept, f"section {label}", (d, xd))
    hatch = [[[view.project(q) for q in _chain(w.Edges())] for w in [f.outerWire(), *f.innerWires()]] for f in cut_faces]
    return Section(label, plane, offset, parent, view, hatch, -dv)


_UNIT = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]


# ---------------- SVG helpers ----------------

class _Svg:
    def __init__(self, w: float, h: float):
        self.w, self.h = w, h
        self.parts: list[str] = []

    def add(self, s: str) -> None:
        self.parts.append(s)

    def line(self, x1, y1, x2, y2, cls="thin"):
        self.add(f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" class="{cls}"/>')

    def poly(self, pts, cls):
        d = " ".join(f"{x:.3f},{y:.3f}" for x, y in pts)
        self.add(f'<polyline points="{d}" class="{cls}"/>')

    def text(self, x, y, s, size=3.5, anchor="middle", rotate=0, weight="normal"):
        rot = f' transform="rotate({rotate:.1f} {x:.3f} {y:.3f})"' if rotate else ""
        self.add(f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size}" text-anchor="{anchor}" font-weight="{weight}"{rot}>{escape(s)}</text>')

    def arrow(self, x, y, dx, dy, size=2.5):
        n = math.hypot(dx, dy) or 1
        ux, uy = dx / n, dy / n
        bx, by = x - ux * size, y - uy * size
        px, py = -uy * size * 0.3, ux * size * 0.3
        self.add(f'<polygon points="{x:.3f},{y:.3f} {bx + px:.3f},{by + py:.3f} {bx - px:.3f},{by - py:.3f}" class="arrow"/>')

    def render(self) -> str:
        style = """
  .thick { stroke: #000; stroke-width: 0.5; fill: none; stroke-linecap: round; }
  .hidden { stroke: #000; stroke-width: 0.25; fill: none; stroke-dasharray: 2 1; }
  .thin { stroke: #000; stroke-width: 0.18; fill: none; }
  .center { stroke: #000; stroke-width: 0.18; fill: none; stroke-dasharray: 6 1.5 1 1.5; }
  .frame { stroke: #000; stroke-width: 0.7; fill: none; }
  .arrow { fill: #000; stroke: none; }
  .sym { stroke: #000; stroke-width: 0.3; fill: none; }
  .symfill { fill: #000; stroke: none; }
  .box { stroke: #000; stroke-width: 0.3; fill: #fff; }
  .cutplane { stroke: #000; stroke-width: 0.25; fill: none; stroke-dasharray: 8 1.5 1.5 1.5; }
  .cutend { stroke: #000; stroke-width: 0.7; fill: none; }
  .hatchline { stroke: #000; stroke-width: 0.13; }
  .cut { stroke: #000; stroke-width: 0.5; stroke-linejoin: round; }
  text { font-family: 'DejaVu Sans', Arial, sans-serif; fill: #000; }
"""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}mm" height="{self.h}mm" viewBox="0 0 {self.w} {self.h}">'
                f'<style>{style}</style>'
                '<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="2" height="2" patternTransform="rotate(45)">'
                f'<line x1="0" y1="0" x2="0" y2="2" class="hatchline"/></pattern></defs><rect width="{self.w}" height="{self.h}" fill="#fff"/>' + "".join(self.parts) + "</svg>")


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _scale_label(s: tuple[int, int]) -> str:
    return f"{s[0]}:{s[1]}"


# ---------------- drawing ----------------

def _hole_groups(result: BuildResult) -> list[dict[str, Any]]:
    groups: dict[tuple, dict[str, Any]] = {}
    for h in result.holes:
        key = (round(h.diameter, 4), h.depth, h.cbore, h.csk, h.thread, h.thread_depth, h.standard,
               tuple(round(c, 6) for c in h.direction.normalized().toTuple()))
        g = groups.setdefault(key, {"diameter": h.diameter, "depth": h.depth, "cbore": h.cbore, "csk": h.csk,
                                    "thread": h.thread, "thread_depth": h.thread_depth, "standard": h.standard,
                                    "direction": h.direction.normalized(), "points": [], "features": set()})
        g["points"].extend(h.points)
        g["features"].add(h.feature)
    return list(groups.values())


def _tol_text(st: dict[str, Any] | None, fit: str | None = None) -> str:
    if not st or not st.get("ok"):
        return ""
    if fit:
        return f" {fit}"
    up, lo = st["upper"], st["lower"]
    if abs(up + lo) < 1e-12:
        return f" ±{_fmt(up)}"
    return f" {'+' if up >= 0 else ''}{_fmt(up)}/{'+' if lo > 0 else ''}{_fmt(lo)}"


def _callout(g: dict[str, Any], tol: str = "") -> str:
    n = len(g["points"])
    if g.get("thread"):
        s = (f"{n}× " if n > 1 else "") + f"{g['thread']}" + (f" ↧{_fmt(g['thread_depth'])}" if g.get("thread_depth") else "")
        s += f"  (Ø{_fmt(g['diameter'])} " + ("THRU)" if g["depth"] is None else f"↧{_fmt(g['depth'])})")
        return s
    s = (f"{n}× " if n > 1 else "") + f"Ø{_fmt(g['diameter'])}{tol} " + ("THRU" if g["depth"] is None else f"↧{_fmt(g['depth'])}")
    if g["cbore"]:
        s += f"  C'BORE Ø{_fmt(g['cbore'][0])} ↧{_fmt(g['cbore'][1])}"
    if g["csk"]:
        s += f"  CSK Ø{_fmt(g['csk'][0])} × {_fmt(g['csk'][1])}°"
    if g.get("standard") and not g.get("thread"):
        s += f"  ({g['standard']})"
    return s


def _frange(a: float, b: float, step: float):
    x = a
    while x <= b:
        yield x
        x += step


def make_drawing(result: BuildResult, spec: PartSpec, sheet: str = "A3", projection: str = "first",
                 sections: list[dict[str, Any]] | None = None, extra_notes: list[str] | None = None,
                 source_note: str | None = None) -> dict[str, Any]:
    """Return {"svg": str, "scale": "1:2", "sheet": "A3", "views": [...], "warnings": [...]}.

    extra_notes: more lines for the notes block; source_note replaces "Generated from the parametric model; ..."
    (e.g. for a part recovered from a mesh).

    sections: [{"plane": "XY"|"YZ"|"XZ", "offset": mm (default: through the middle), "flip": bool,
    "label": "A", "view": parent view for the cutting line}] -> hatched section views A-A, B-B, ...
    Raises ValueError for a section that misses the part or cannot be placed."""
    solid = result.shape.val()
    W, H = SHEETS[sheet]
    ann = {a["id"]: a for a in spec.annotations}
    status = {st["id"]: st for st in evaluate_annotations(spec, result)}
    tol_by_param = {a["parameter"]: (a, status[a["id"]]) for a in spec.annotations if a.get("type") == "tolerance"}
    general = next((a["standard"] for a in spec.annotations if a.get("type") == "general_tolerance"), None)
    finish_all = next((a["ra"] for a in spec.annotations if a.get("type") == "surface_finish" and a.get("all")), None)

    def dim_tol(value: float) -> str:
        """Tolerance text when a toleranced variable has exactly this nominal value."""
        for name, (a, st) in tol_by_param.items():
            if name.startswith("vars.") and st.get("ok") and abs(st["nominal"] - value) < 1e-6:
                return _tol_text(st, a.get("fit"))
        return ""
    views = {name: project_view(solid, name) for name in ("front", "top", "left" if projection == "first" else "right", "iso")}
    side = "left" if projection == "first" else "right"
    fv, tv, sv, iv = views["front"], views["top"], views[side], views["iso"]

    def size(v):
        return v.bbox[2] - v.bbox[0], v.bbox[3] - v.bbox[1]

    fw, fh = size(fv)
    tw, th = size(tv)
    sw, sh = size(sv)
    iw, ih = size(iv)
    margin, title_h, gap = 10.0, 30.0, 25.0
    area_w, area_h = W - 2 * margin, H - 2 * margin - title_h
    # the three orthographic views form an L (front + side in a row, top in the column) and the iso goes top-right
    need_w, need_h = fw + sw + iw * 0.6, fh + th
    scale = next((s for s in SCALES if (need_w * s[0] / s[1] + 4 * gap) <= area_w and (need_h * s[0] / s[1] + 3 * gap) <= area_h), SCALES[-1])
    warnings = []
    k = scale[0] / scale[1]
    if (need_w * k + 4 * gap) > area_w or (need_h * k + 3 * gap) > area_h:
        warnings.append(f"the part does not fit sheet {sheet} even at {_scale_label(scale)}; use a larger sheet")

    # holes seen as circles in the top / front / side view (grouped before layout: their dimensions take room)
    notes = []
    per_view: dict[str, list[tuple[dict[str, Any], list[tuple[float, float]]]]] = defaultdict(list)
    for g in _hole_groups(result):
        d = g["direction"]
        view = next((name for name in ("top", "front", side) if abs(abs(d.dot(cq.Vector(*VIEWS[name][0]).normalized())) - 1) < 1e-6), None)
        if view is None:
            notes.append(_callout(g) + " (inclined; not dimensioned)")
            continue
        per_view[view].append((g, [views[view].project(p) for p in g["points"]]))

    secs = [make_section(solid, sp, (sp.get("label") if isinstance(sp, dict) and sp.get("label") else "ABCDEFGH"[i]), side)
            for i, sp in enumerate(sections or [])]
    if len({sc.label for sc in secs}) != len(secs):
        raise ValueError("section labels must be unique")

    def layout(k: float):
        """View origins (bottom-left, sheet mm, y down) for scale k; first angle: top BELOW front, left RIGHT of front."""
        x0 = margin + gap
        if projection == "first":
            fo = (x0, margin + gap + fh * k)
            to = (x0, fo[1] + gap + th * k)
        else:
            to = (x0, margin + gap + th * k)
            fo = (x0, to[1] + gap + fh * k)
        return {"front": fo, "top": to, side: (fo[0] + fw * k + gap, fo[1]),
                "iso": (W - margin - gap - iw * k * 0.6, margin + gap + ih * k * 0.6)}

    def occupied(k: float, org) -> list[tuple[float, float, float, float]]:
        """Rectangles (x0, y0, x1, y1) taken by views with their dimensions and callouts, notes and title block."""
        rects = []
        for name, v in views.items():
            kk = k * 0.6 if name == "iso" else k
            (ox, oy), (vw, vh) = org[name], size(v)
            x0_, y0_, x1_, y1_ = ox - 16, oy - vh * kk - 9, ox + vw * kk + 6, oy + 16
            items = per_view.get(name, [])
            if items:
                ys = {round(c[1], 4) for _, cs in items for c in cs}
                xs = {round(c[0], 4) for _, cs in items for c in cs}
                text_w = max(len(_callout(g)) for g, _ in items) * 3.2 * 0.6
                x1_ = ox + vw * kk + 10 + 7 * (min(len(ys), 8) + 1) + 6 + text_w
                y1_ = max(oy + 19 + 7 * min(len(xs), 8), oy - vh * kk + 4 + 12 * len(items) + 8)
            rects.append((x0_, y0_, x1_, y1_))
        n_notes = 3 + len(notes) + sum(1 for st in status.values() if not st["ok"])
        n_tol = sum(1 for a in spec.annotations if a.get("type") == "tolerance")
        rects.append((margin, H - margin / 2 - title_h - 10 - 5 * n_notes - (6 * n_tol + 10 if n_tol else 0), margin + 150, H))
        rects.append((W - margin / 2 - 180, H - margin / 2 - title_h - 4, W, H))
        return rects

    def free(r, rects) -> bool:
        return all(r[2] <= q[0] or r[0] >= q[2] or r[3] <= q[1] or r[1] >= q[3] for q in rects)

    def place_sections(k: float, org):
        rects, out = occupied(k, org), {}
        for sc in secs:
            vw, vh = size(sc.view)
            w_, h_ = vw * k + 8, vh * k + 14  # label above, breathing room around
            spot = next(((x, y) for y in _frange(margin, H - margin - h_, 4) for x in _frange(margin, W - margin - w_, 4)
                         if free((x, y, x + w_, y + h_), rects)), None)
            if spot is None:
                return None
            rects.append((spot[0], spot[1], spot[0] + w_, spot[1] + h_))
            out[sc.label] = (spot[0] + 4, spot[1] + 10 + vh * k)  # bottom-left of the section view
        return out

    sec_org: dict[str, tuple[float, float]] = {}
    if secs:
        for cand in SCALES[SCALES.index(scale):]:
            kk = cand[0] / cand[1]
            got = place_sections(kk, layout(kk))
            if got is not None:
                scale, k, sec_org = cand, kk, got
                break
        else:
            raise ValueError(f"the section views do not fit on sheet {sheet}; use a larger sheet or fewer sections")
    k = scale[0] / scale[1]
    org = layout(k)
    front_origin, top_origin, side_origin, iso_origin = org["front"], org["top"], org[side], org["iso"]
    iso_k = k * 0.6

    svg = _Svg(W, H)
    svg.add(f'<rect x="{margin / 2}" y="{margin / 2}" width="{W - margin}" height="{H - margin}" class="frame"/>')

    def place(v: View2D, origin, kk):
        ox, oy = origin
        bx, by = v.bbox[0], v.bbox[1]
        return lambda x, y: (ox + (x - bx) * kk, oy - (y - by) * kk)

    tf = {"front": place(fv, front_origin, k), "top": place(tv, top_origin, k), side: place(sv, side_origin, k), "iso": place(iv, iso_origin, iso_k)}
    for name, v in views.items():
        T = tf[name]
        for pl in v.hidden:
            svg.poly([T(*p) for p in pl], "hidden")
        for pl in v.visible:
            svg.poly([T(*p) for p in pl], "thick")
        bx0, _ = T(v.bbox[0], v.bbox[1])
        bx1, top_y = T(v.bbox[2], v.bbox[3])
        label = {"front": "FRONT", "top": "TOP", "left": "LEFT", "right": "RIGHT", "iso": "ISOMETRIC"}[name]
        svg.text((bx0 + bx1) / 2, top_y - 4, label + ("" if name != "iso" else " (not to scale)"), 3, weight="bold")

    def dim_h(view: str, xa: float, xb: float, y_model: float, offset: float, text: str):
        """Horizontal dimension between model x positions xa, xb drawn at offset mm below (negative = above) y_model."""
        T = tf[view]
        (ax, ay), (bx_, _) = T(xa, y_model), T(xb, y_model)
        y = ay + offset
        svg.line(ax, ay + (1 if offset > 0 else -1), ax, y + (1.5 if offset > 0 else -1.5))
        svg.line(bx_, ay + (1 if offset > 0 else -1), bx_, y + (1.5 if offset > 0 else -1.5))
        svg.line(ax, y, bx_, y)
        svg.arrow(ax, y, ax - bx_, 0)
        svg.arrow(bx_, y, bx_ - ax, 0)
        svg.text((ax + bx_) / 2, y - 1, text)

    def dim_v(view: str, ya: float, yb: float, x_model: float, offset: float, text: str):
        T = tf[view]
        (ax, ay), (_, by_) = T(x_model, ya), T(x_model, yb)
        x = ax + offset
        svg.line(ax + (1 if offset > 0 else -1), ay, x + (1.5 if offset > 0 else -1.5), ay)
        svg.line(ax + (1 if offset > 0 else -1), by_, x + (1.5 if offset > 0 else -1.5), by_)
        svg.line(x, ay, x, by_)
        svg.arrow(x, ay, 0, ay - by_)
        svg.arrow(x, by_, 0, by_ - ay)
        svg.text(x - 1, (ay + by_) / 2, text, rotate=-90)

    # overall dimensions
    dim_h("front", fv.bbox[0], fv.bbox[2], fv.bbox[1], 10, _fmt(fw) + dim_tol(fw))
    dim_v("front", fv.bbox[1], fv.bbox[3], fv.bbox[0], -10, _fmt(fh) + dim_tol(fh))
    dim_v("top", tv.bbox[1], tv.bbox[3], tv.bbox[0], -10, _fmt(th) + dim_tol(th))

    # holes: centre marks, one set of position dimensions per view (from the view's datum edges),
    # callouts stacked beside the view
    for view, items in per_view.items():
        v, T = views[view], tf[view]
        xs = sorted({round(c[0], 4) for _, cs in items for c in cs})[:8]
        ys = sorted({round(c[1], 4) for _, cs in items for c in cs})[:8]
        for i, x in enumerate(xs):
            dim_h(view, v.bbox[0], x, v.bbox[1], 17 + 7 * i, _fmt(x - v.bbox[0]))
        for i, y in enumerate(ys):
            dim_v(view, v.bbox[1], y, v.bbox[2], 10 + 7 * (i + 1), _fmt(y - v.bbox[1]))
        right = T(v.bbox[2], v.bbox[3])[0] + 10 + 7 * (len(ys) + 1) + 6
        top = T(v.bbox[0], v.bbox[3])[1]
        for gi, (g, centers) in enumerate(items):
            r = g["diameter"] / 2 * k
            for cx, cy in centers:
                sx, sy = T(cx, cy)
                svg.line(sx - r - 1.5, sy, sx + r + 1.5, sy, "center")
                svg.line(sx, sy - r - 1.5, sx, sy + r + 1.5, "center")
            sx, sy = T(*max(centers, key=lambda c: (c[0], c[1])))  # leader from the right-most instance
            lx, ly = sx + r * 0.707, sy - r * 0.707
            tx, ty = right, top + 4 + 12 * gi
            svg.line(lx, ly, tx - 1, ty - 1)
            svg.arrow(lx, ly, lx - tx, ly - ty)
            tol = ""
            for fid in sorted(g["features"]):
                if f"{fid}.diameter" in tol_by_param:
                    a, st = tol_by_param[f"{fid}.diameter"]
                    tol = _tol_text(st, a.get("fit"))
            svg.text(tx, ty, _callout(g, tol), 3.2, anchor="start")
            frames = [a for a in spec.annotations if a.get("type") == "gdt" and (a.get("target") or {}).get("feature") in g["features"]]
            for fi, a in enumerate(frames):
                _frame(svg, tx, ty + 1.5 + fi * 6.5, a)

    # section views: cutting-plane line with arrows and letters on the parent view, hatched cut faces
    for sc in secs:
        pv, T = views[sc.parent], tf[sc.parent]
        axis = cq.Vector(*_UNIT[_SECTION_AXIS[sc.plane]])
        ex, ey = axis.dot(pv.xdir), axis.dot(pv.ydir)
        if abs(ex) > 0.5:  # plane seen as a vertical line in the parent view
            u = sc.offset * ex
            a, b = T(u, pv.bbox[3]), T(u, pv.bbox[1])
            a, b = (a[0], a[1] - 6), (b[0], b[1] + 6)
        else:
            w_ = sc.offset * ey
            a, b = T(pv.bbox[0], w_), T(pv.bbox[2], w_)
            a, b = (a[0] - 6, a[1]), (b[0] + 6, b[1])
        svg.line(*a, *b, "cutplane")
        sx_, sy_ = sc.sight.dot(pv.xdir), -sc.sight.dot(pv.ydir)  # sheet y points down
        ln = math.hypot(sx_, sy_) or 1
        sx_, sy_ = sx_ / ln, sy_ / ln
        tx_, ty_ = (b[0] - a[0]), (b[1] - a[1])
        tl = math.hypot(tx_, ty_) or 1
        tx_, ty_ = tx_ / tl, ty_ / tl
        for (px, py), sgn in ((a, -1), (b, 1)):
            svg.line(px, py, px - tx_ * sgn * 5, py - ty_ * sgn * 5, "cutend")  # thick ends of the chain line
            svg.line(px, py, px + sx_ * 6, py + sy_ * 6, "cutend")
            svg.arrow(px + sx_ * 6, py + sy_ * 6, sx_, sy_, 2.8)
            svg.text(px + sx_ * 6 + tx_ * sgn * 3.5, py + sy_ * 6 + ty_ * sgn * 3.5 + 1.5, sc.label, 4.5, weight="bold")
        v = sc.view
        T = place(v, sec_org[sc.label], k)
        for face_loops in sc.hatch:
            d = " ".join("M " + " L ".join(f"{T(*q)[0]:.3f},{T(*q)[1]:.3f}" for q in loop) + " Z" for loop in face_loops)
            svg.add(f'<path d="{d}" fill="url(#hatch)" fill-rule="evenodd" class="cut" data-section="{sc.label}"/>')
        for pl in v.visible:
            svg.poly([T(*p) for p in pl], "thick")
        bx0, _ = T(v.bbox[0], v.bbox[1])
        bx1, top_y = T(v.bbox[2], v.bbox[3])
        svg.text((bx0 + bx1) / 2, top_y - 4, f"SECTION {sc.label}-{sc.label}", 3.5, weight="bold")

    # title block
    tb_w, tb_h = 180.0, title_h
    tx0, ty0 = W - margin / 2 - tb_w, H - margin / 2 - tb_h
    svg.add(f'<rect x="{tx0}" y="{ty0}" width="{tb_w}" height="{tb_h}" class="frame"/>')
    for yy in (ty0 + 10, ty0 + 20):
        svg.line(tx0, yy, tx0 + tb_w, yy)
    svg.line(tx0 + 90, ty0, tx0 + 90, ty0 + tb_h)
    mat = spec.material
    mass = f"{result.volume_mm3 * 1e-9 * mat.density * 1000:.1f} g" if mat else "—"
    rows = [
        (tx0 + 2, ty0 + 6.5, f"PART: {spec.name}", 4.2, "bold"),
        (tx0 + 92, ty0 + 6.5, f"SCALE {_scale_label(scale)}   SHEET {sheet}   UNITS mm", 3.2, "normal"),
        (tx0 + 2, ty0 + 16.5, f"MATERIAL: {mat.name if mat else 'not assigned'}", 3.2, "normal"),
        (tx0 + 92, ty0 + 16.5, f"MASS: {mass}   VOLUME: {result.volume_mm3:.0f} mm³", 3.2, "normal"),
        (tx0 + 2, ty0 + 26.5, f"{'FIRST' if projection == 'first' else 'THIRD'} ANGLE · GENERAL TOL: {general or 'NOT SPECIFIED'}", 3.0, "normal"),
        (tx0 + 92, ty0 + 26.5, f"DATE {_dt.date.today().isoformat()}   AI CAD", 3.2, "normal"),
    ]
    for x, y, s, sz, wt in rows:
        svg.text(x, y, s, sz, anchor="start", weight=wt)

    tol_note = (f"Untoleranced dimensions: {general}." if general else "Tolerances: NOT SPECIFIED — add before manufacture.")
    finish_note = (f"Surface finish Ra {_fmt(finish_all)} µm unless otherwise stated." if finish_all else "Surface finish: NOT SPECIFIED.")
    lost = [f"{i}: {st['message']}" for i, st in status.items() if not st["ok"]]
    notes = [tol_note, finish_note, source_note or "Generated from the parametric model; verify all dimensions before use.",
             *(extra_notes or []), *notes,
             *(f"ANNOTATION NOT SHOWN — {m}" for m in lost)]

    # table of toleranced dimensions (every tolerance, including ones not on an automatic dimension)
    rows_t = [(a["parameter"], st) for a, st in ((ann[i], status[i]) for i in ann if ann[i].get("type") == "tolerance") if st["ok"]]
    if rows_t:
        x_t, y_t = margin + 2, ty0 - 8 - len(notes) * 5 - 6 * len(rows_t)
        svg.text(x_t, y_t - 5, "TOLERANCED DIMENSIONS", 3, anchor="start", weight="bold")
        for i, (param, st) in enumerate(rows_t):
            a = tol_by_param[param][0]
            svg.text(x_t, y_t + i * 5, f"{param}: {_fmt(st['nominal'])}{_tol_text(st, a.get('fit'))}   "
                     f"[{_fmt(st['min'])} … {_fmt(st['max'])}]", 3, anchor="start")

    # datums, face GD&T frames and surface-finish symbols, placed where the face is seen edge-on
    def face_view(ref):
        f = find_face(result, ref)
        if f is None:
            return None
        p = cq.Vector(*ref["point"])
        n = f.normalAt(p).normalized()
        best = min(("front", "top", side), key=lambda vn: abs(n.dot(cq.Vector(*VIEWS[vn][0]).normalized())))
        v = views[best]
        sx, sy = tf[best](*v.project(p))
        dx, dy = n.dot(v.xdir), -n.dot(v.ydir)  # sheet y points down
        ln = math.hypot(dx, dy) or 1
        return sx, sy, dx / ln, dy / ln

    stack: dict[tuple[int, int], int] = defaultdict(int)
    for a in spec.annotations:
        st = status[a["id"]]
        if not st["ok"]:
            continue
        ref = a.get("face") or (a.get("target") or {}).get("face")
        if a.get("type") not in ("datum", "gdt", "surface_finish") or ref is None:
            continue
        hit = face_view(ref)
        if hit is None:
            continue
        sx, sy, dx, dy = hit
        k_ = stack[(round(sx), round(sy))]
        stack[(round(sx), round(sy))] += 1
        ex, ey = sx + dx * (10 + 7 * k_), sy + dy * (10 + 7 * k_)
        if a["type"] == "datum":
            px, py = -dy, dx  # triangle base along the face line
            svg.add(f'<polygon points="{sx + px * 1.8:.2f},{sy + py * 1.8:.2f} {sx - px * 1.8:.2f},{sy - py * 1.8:.2f} '
                    f'{sx + dx * 2.6:.2f},{sy + dy * 2.6:.2f}" class="symfill"/>')
            svg.line(sx + dx * 2.6, sy + dy * 2.6, ex, ey)
            svg.add(f'<rect x="{ex - 3:.2f}" y="{ey - 3:.2f}" width="6" height="6" class="box"/>')
            svg.text(ex, ey + 1.3, a["label"], 3.5, weight="bold")
        elif a["type"] == "gdt":
            svg.line(sx, sy, ex, ey)
            svg.arrow(sx, sy, sx - ex, sy - ey)
            _frame(svg, ex, ey - 3, a)
        else:
            _finish(svg, ex, ey, a["ra"])

    for i, n in enumerate(notes):  # above the title block, left-aligned
        svg.text(margin + 2, ty0 - 4 - (len(notes) - 1 - i) * 5, n, 3, anchor="start")
    notes = []
    return {"svg": svg.render(), "scale": _scale_label(scale), "sheet": sheet, "projection": projection,
            "views": list(views) + [f"section {sc.label}" for sc in secs], "warnings": warnings,
            "sections": [{"label": sc.label, "plane": sc.plane, "offset": sc.offset, "view": sc.parent} for sc in secs]}


def _frame(svg: _Svg, x: float, y: float, a: dict[str, Any]) -> float:
    """Feature control frame at (x, y) top-left; returns its width."""
    h = 6.0
    tol = ("Ø" if a.get("diameter_zone") else "") + _fmt(a["tolerance"])
    mod = a.get("modifier")  # drawn as a circled letter (fonts often lack Ⓜ/Ⓛ)
    cells = [h, max(10.0, 2.1 * len(tol) + 3 + (5 if mod else 0))] + [h] * len(a.get("datums") or [])
    cx = x
    for i, w in enumerate(cells):
        svg.add(f'<rect x="{cx:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h}" class="box"/>')
        if i == 0:
            svg.add(symbol_svg(a["characteristic"], cx + 0.5, y + 0.5, h - 1))
        elif i == 1:
            tx_ = cx + w / 2 - (2.5 if mod else 0)
            svg.text(tx_, y + 4.2, tol, 3.2)
            if mod:
                mx = tx_ + 1.05 * len(tol) + 3
                svg.add(f'<circle cx="{mx:.2f}" cy="{y + h / 2:.2f}" r="1.9" class="sym"/>')
                svg.text(mx, y + h / 2 + 1, mod, 2.6)
        else:
            svg.text(cx + w / 2, y + 4.2, a["datums"][i - 2], 3.2, weight="bold")
        cx += w
    return cx - x


def _finish(svg: _Svg, x: float, y: float, ra: float) -> None:
    """Surface texture symbol (machining required) with the Ra value."""
    svg.add(f'<polyline points="{x - 2:.2f},{y - 2:.2f} {x:.2f},{y:.2f} {x + 4:.2f},{y - 7:.2f} {x + 13:.2f},{y - 7:.2f}" class="sym"/>')
    svg.line(x - 2, y - 2, x + 2.3, y - 2, "sym")
    svg.text(x + 8.5, y - 8, f"Ra {_fmt(ra)}", 2.8)


def to_pdf(svg: str) -> bytes:
    import cairosvg
    return cairosvg.svg2pdf(bytestring=svg.encode())


def to_png(svg: str, width_px: int = 1600) -> bytes:
    import cairosvg
    return cairosvg.svg2png(bytestring=svg.encode(), output_width=width_px)
