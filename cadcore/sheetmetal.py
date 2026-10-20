# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sheet metal: base flange, edge flanges with real bends, and the flat pattern (DXF/SVG).

Model:
- The sheet has a thickness T, an inside bend radius R and a K-factor (the neutral axis sits at
  K·T from the inside of the bend).
- A base flange is extruded to thickness T.
- An edge flange on a straight edge of the base adds a bend region (inner radius R, angle θ),
  then a flat flange of length L measured from the end of the bend.

Flat pattern: every flange unfolds to bend allowance BA = θ·(R + K·T) plus its length L,
attached to the base edge. Holes and through-cuts in the base are included.

Supported: flanges on edges of the base flange only (one bend level). Anything else,
such as a cut through a flange or a flange on a flange, makes the flat pattern refuse to
compute and explain why; it never produces a wrong flat.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import cadquery as cq


@dataclass
class FlangeInfo:
    feature: str
    a: cq.Vector  # edge start (on the base, at the bend side face, bottom plane z=0)
    b: cq.Vector
    m: cq.Vector  # outward in-plane direction
    angle: float  # degrees
    radius: float
    length: float
    up: bool
    body: cq.Shape
    edge_on_top: bool


@dataclass
class SheetState:
    T: float
    R: float
    K: float
    origin: cq.Vector | None = None  # base plane frame
    xdir: cq.Vector | None = None
    ydir: cq.Vector | None = None
    n: cq.Vector | None = None
    base_faces_uv: list[cq.Face] = field(default_factory=list)  # base profile in (u, v) plane coordinates
    flanges: list[FlangeInfo] = field(default_factory=list)
    cuts_uv: list[cq.Face] = field(default_factory=list)  # holes / through cuts in the base, (u, v)
    unsupported: list[str] = field(default_factory=list)  # features the flat pattern cannot represent

    def uv(self, p: cq.Vector) -> tuple[float, float]:
        d = p - self.origin
        return d.dot(self.xdir), d.dot(self.ydir)

    def h(self, p: cq.Vector) -> float:
        return (p - self.origin).dot(self.n)


def bend_allowance(angle_deg: float, R: float, K: float, T: float) -> float:
    return math.radians(angle_deg) * (R + K * T)


def make_flange(st: SheetState, shape: cq.Shape, near: cq.Vector, length: float, angle: float, up: bool,
                radius: float, trim_start: float, trim_end: float, fid: str) -> FlangeInfo:
    """Bend + flange solids for a straight edge of the base; raises ValueError with a user-facing message."""
    T = st.T
    v = cq.Vertex.makeVertex(*near.toTuple())
    edges = [e for e in shape.Edges() if e.geomType() == "LINE" and e.distance(v) < 0.05]
    on_base = []
    for e in edges:
        hs = [st.h(e.startPoint()), st.h(e.endPoint())]
        if all(abs(x - T) < 1e-6 for x in hs) or all(abs(x) < 1e-6 for x in hs):
            on_base.append(e)
    if len(on_base) != 1:
        raise ValueError("pick a straight edge on the top or bottom face of the base flange "
                         "(flanges on flanges are not supported)")
    e = on_base[0]
    top = abs(st.h(e.startPoint()) - T) < 1e-6
    a, b = e.startPoint(), e.endPoint()
    if top:  # work on the bottom plane z=0
        a, b = a - st.n * T, b - st.n * T
    d = (b - a)
    L_edge = d.Length
    d = d.normalized()
    if trim_start < 0 or trim_end < 0 or trim_start + trim_end >= L_edge:
        raise ValueError(f"trims must be ≥ 0 and leave part of the {L_edge:g} mm edge")
    a, b = a + d * trim_start, b - d * trim_end
    m = st.n.cross(d)
    mid = (a + b) * 0.5 + st.n * (T / 2)
    solid = shape.Solids()[0] if shape.Solids() else shape
    if solid.isInside(mid + m * 0.05, 1e-6):
        m = -m
    if solid.isInside(mid + m * 0.05, 1e-6) or not solid.isInside(mid - m * 0.05, 1e-6):
        raise ValueError("the picked edge is not on the outer boundary of the base")
    if not 0 < angle <= 180:
        raise ValueError("bend angle must be in (0, 180] degrees")
    if length <= 0 or radius <= 0:
        raise ValueError("flange length and bend radius must be positive")

    # end-face rectangle of the (trimmed) edge, then revolve it about the bend axis
    face = cq.Face.makeFromWires(cq.Wire.makePolygon([a, b, b + st.n * T, a + st.n * T], close=True))
    axis_pt = a + st.n * (T + radius) if up else a - st.n * radius
    for sign in (1, -1):
        bend = cq.Solid.revolve(face, sign * angle, axis_pt, axis_pt + d)
        if (bend.Center() - mid).dot(m) > 0:
            break
    end_face = face.rotate(axis_pt, axis_pt + d, sign * angle)
    th = math.radians(angle)
    direction = m * math.cos(th) + st.n * (math.sin(th) if up else -math.sin(th))
    flange = cq.Solid.extrudeLinear(end_face, direction * length)
    body = bend.fuse(flange).clean()
    return FlangeInfo(fid, a, b, m, angle, radius, length, up, body, top)


def check_collisions(st: SheetState, new: FlangeInfo) -> None:
    for f in st.flanges:
        common = f.body.intersect(new.body)
        if common.Solids() and common.Volume() > 1e-6:
            # bends beyond 90° lean back over the base, so the overlap reaches further from the corner
            need = max(fl.radius + st.T + max(0.0, -math.cos(math.radians(fl.angle))) * (fl.length + fl.radius + st.T) for fl in (f, new))
            need = math.ceil(need * 10) / 10
            raise ValueError(f"flange {new.feature} collides with {f.feature} at the corner; set trim_start/trim_end "
                             f"≥ {need:g} mm on the shared corner (bend radius + thickness, plus the lean for bends over 90°)")


# ---------------- flat pattern ----------------

def flat_pattern(st: SheetState) -> dict[str, Any]:
    """{"faces": [cq.Face in (u, v)], "bends": [{line, label}], "size": (w, h), "area": mm²}."""
    if st.unsupported:
        raise ValueError("flat pattern unavailable: " + "; ".join(st.unsupported))
    if not st.base_faces_uv:
        raise ValueError("no base flange")
    sk = cq.Sketch()
    for f in st.base_faces_uv:
        sk.face(f)
    bends = []
    for fl in st.flanges:
        ba = bend_allowance(fl.angle, fl.radius, st.K, st.T)
        ua, ub = cq.Vector(*st.uv(fl.a), 0), cq.Vector(*st.uv(fl.b), 0)
        mu = cq.Vector(fl.m.dot(st.xdir), fl.m.dot(st.ydir), 0).normalized()
        pts = [ua, ub, ub + mu * (ba + fl.length), ua + mu * (ba + fl.length)]
        if sum(p.x * q.y - q.x * p.y for p, q in zip(pts, pts[1:] + pts[:1])) < 0:
            pts.reverse()  # counter-clockwise, so the face normal matches the base and the union merges
        rect = cq.Face.makeFromWires(cq.Wire.makePolygon(pts, close=True))
        sk.face(rect)
        label = f"{'UP' if fl.up else 'DOWN'} {fl.angle:g}° R{fl.radius:g}"
        bends.append({"feature": fl.feature, "start": [(ua + mu * 0).x, (ua).y], "line": [[ua.x, ua.y], [ub.x, ub.y]],
                      "line2": [[(ua + mu * ba).x, (ua + mu * ba).y], [(ub + mu * ba).x, (ub + mu * ba).y]], "label": label,
                      "allowance": ba})
    for c in st.cuts_uv:
        sk.face(c, mode="s")
    sk.clean()
    faces = sk._faces.Faces()
    if len(faces) != 1:
        raise ValueError(f"the unfolded pattern has {len(faces)} separate pieces; check flange trims")
    bb = faces[0].BoundingBox()
    return {"faces": faces, "bends": bends, "size": (bb.xlen, bb.ylen), "area": faces[0].Area(), "origin": (bb.xmin, bb.ymin)}


def _edge_points(e: cq.Edge) -> list[tuple[float, float]]:
    if e.geomType() == "LINE":
        pts = [e.startPoint(), e.endPoint()]
    else:
        n = max(16, int(e.Length() / 0.5))
        pts = [e.positionAt(i / n) for i in range(n + 1)]
    return [(p.x, p.y) for p in pts]


def flat_svg(fp: dict[str, Any], title: str = "") -> str:
    w, h = fp["size"]
    x0, y0 = fp["origin"]
    pad = 15
    W, H = w + 2 * pad, h + 2 * pad + 10
    T = lambda x, y: (x - x0 + pad, H - (y - y0 + pad) - 10)  # noqa: E731
    parts = []
    face = fp["faces"][0]
    for wire in [face.outerWire(), *face.innerWires()]:
        for e in wire.Edges():
            pts = " ".join(f"{T(x, y)[0]:.3f},{T(x, y)[1]:.3f}" for x, y in _edge_points(e))
            parts.append(f'<polyline points="{pts}" fill="none" stroke="#000" stroke-width="0.4"/>')
    for bd in fp["bends"]:
        for ln in (bd["line"], bd["line2"]):
            (ax, ay), (bx, by) = T(*ln[0]), T(*ln[1])
            parts.append(f'<line x1="{ax:.3f}" y1="{ay:.3f}" x2="{bx:.3f}" y2="{by:.3f}" stroke="#c00" stroke-width="0.3" stroke-dasharray="3 2"/>')
        (ax, ay), (bx, by) = T(*bd["line"][0]), T(*bd["line"][1])
        parts.append(f'<text x="{(ax + bx) / 2:.2f}" y="{(ay + by) / 2 - 1.5:.2f}" font-size="3" fill="#c00" text-anchor="middle">{bd["label"]}</text>')
    parts.append(f'<text x="{pad}" y="{H - 3}" font-size="3.5">{title} FLAT PATTERN {w:.2f} × {h:.2f} mm</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.1f}mm" height="{H:.1f}mm" viewBox="0 0 {W:.2f} {H:.2f}">'
            f'<rect width="{W:.2f}" height="{H:.2f}" fill="#fff"/>' + "".join(parts) + "</svg>")


def flat_dxf(fp: dict[str, Any], path: str) -> None:
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.layers.add("OUTLINE", color=7)
    doc.layers.add("BEND", color=1, linetype="DASHED")
    doc.layers.add("NOTES", color=3)
    msp = doc.modelspace()
    face = fp["faces"][0]
    for wire in [face.outerWire(), *face.innerWires()]:
        for e in wire.Edges():
            if e.geomType() == "LINE":
                msp.add_line(e.startPoint().toTuple()[:2], e.endPoint().toTuple()[:2], dxfattribs={"layer": "OUTLINE"})
            elif e.geomType() == "CIRCLE" and e.IsClosed():
                msp.add_circle(e.arcCenter().toTuple()[:2], e.radius(), dxfattribs={"layer": "OUTLINE"})
            else:
                msp.add_lwpolyline(_edge_points(e), dxfattribs={"layer": "OUTLINE"})
    for bd in fp["bends"]:
        msp.add_line(tuple(bd["line"][0]), tuple(bd["line"][1]), dxfattribs={"layer": "BEND"})
        msp.add_line(tuple(bd["line2"][0]), tuple(bd["line2"][1]), dxfattribs={"layer": "BEND"})
        (ax, ay), (bx, by) = bd["line"]
        msp.add_text(bd["label"], height=2.5, dxfattribs={"layer": "NOTES"}).set_placement(((ax + bx) / 2, (ay + by) / 2 + 1))
    doc.saveas(path)
