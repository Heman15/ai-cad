# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic sheet-metal and prismatic test parts with known dimensions, tessellated to meshes.

A bent part is a profile walked in the XZ plane (flat lengths and signed bend angles), with the material on the
left of the walk, extruded along Y by `width`. Holes are drilled square to the flanges, slots go in the first one.
`sharp_outer` replaces every outer bend arc by a sharp corner (the inner radius stays), as some designers model it.
The mesh is tessellated coarsely (holes and bends get few segments), then moved by a random rigid motion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cadquery as cq
import numpy as np
import trimesh


@dataclass
class SheetCase:
    name: str
    lengths: list[float]  # flat length of each flange (outer edge to tangent line / tangent to tangent)
    angles: list[float]  # signed bend angle between consecutive flanges (+ = left turn), degrees
    t: float = 1.5
    r: float = 1.5  # inner bend radius
    width: float = 30.0
    holes: list[tuple[int, float, float, float]] = field(default_factory=list)  # (flange, s along, y across, d)
    slots: list[tuple[float, float, float, float]] = field(default_factory=list)  # first flange: (s, y, length, w)
    sharp_outer: bool = False
    ang_tol: float = 0.5  # tessellation angular tolerance (rad): 0.78 gives ~8-segment holes
    seed: int = 0


def _rot(v, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1]])


def sheet_profile(case: SheetCase):
    """Closed 2D profile (list of ("line", p) / ("arc", mid, end) steps from a start point) and flange frames."""
    t, r = case.t, case.r
    p = np.array([0.0, 0.0])
    h = np.array([1.0, 0.0])
    path, other = [p.copy()], [p + np.array([0, t])]  # path side (offset 0) and material side (offset t)
    arcs_path, arcs_other = [], []  # (index in list, mid point) for arcs
    frames = []
    for i, L in enumerate(case.lengths):
        left = np.array([-h[1], h[0]])
        frames.append((p.copy(), h.copy(), left.copy(), L))
        p = p + h * L
        path.append(p.copy())
        other.append(p + left * t)
        if i == len(case.angles):
            break
        ang = case.angles[i]
        if ang > 0:  # left turn: centre on the material side; path side is outside (radius r + t)
            c = p + left * (r + t)
            ro, ri = r + t, r
        else:
            c = p - left * r
            ro, ri = r, r + t
        h2 = _rot(h, ang)
        left2 = np.array([-h2[1], h2[0]])
        p2 = c + (_rot(p - c, ang))
        if ang > 0:
            o1, o2 = c - left * r, c - left2 * r  # material side (inner)
        else:
            o1, o2 = p + left * t, p2 + left2 * t
        mid_p = c + _rot(p - c, ang / 2)
        mid_o = c + _rot(o1 - c, ang / 2)
        sharp_path = case.sharp_outer and ang > 0
        sharp_other = case.sharp_outer and ang < 0
        for lst, arcs, a0, a1, mid, sharp, rad in ((path, arcs_path, p, p2, mid_p, sharp_path, ro), (other, arcs_other, o1, o2, mid_o, sharp_other, ri)):
            if rad < 1e-9:  # radius 0: the arc is a point, already in the list
                continue
            if sharp:  # the two straight sides meet in a corner
                s = np.linalg.solve(np.array([h, -h2]).T, a1 - a0)[0]
                lst[-1] = a0 + h * s
            else:
                arcs.append((len(lst), mid))
                lst.append(a1.copy())
        p, h = p2, h2
    return path, other, arcs_path, arcs_other, frames


def build_sheet(case: SheetCase) -> cq.Shape:
    path, other, arcs_path, arcs_other, frames = sheet_profile(case)
    am = {i: m for i, m in arcs_path}
    ao = {i: m for i, m in arcs_other}
    wp = cq.Workplane("XZ").moveTo(*path[0])
    for i in range(1, len(path)):
        wp = wp.threePointArc(tuple(am[i]), tuple(path[i])) if i in am else wp.lineTo(*path[i])
    wp = wp.lineTo(*other[-1])
    for i in range(len(other) - 1, 0, -1):
        # walking backwards: an arc that ends at other[i] (index i) starts there and ends at other[i-1]
        wp = wp.threePointArc(tuple(ao[i]), tuple(other[i - 1])) if i in ao else wp.lineTo(*other[i - 1])
    wp = wp.close()
    solid = wp.extrude(-case.width).val()  # XZ normal is -Y: extrude toward +Y
    for fi, s, y, d in case.holes:
        p, h, left, L = frames[fi]
        c2 = p + h * s
        o = cq.Vector(c2[0], y, c2[1]) - cq.Vector(left[0], 0, left[1]) * case.t
        n = cq.Vector(left[0], 0, left[1])
        solid = solid.cut(cq.Solid.makeCylinder(d / 2, 3 * case.t, o, n))
    for s, y, L, w in case.slots:
        p, h, left, _ = frames[0]
        c2 = p + h * s
        pl = cq.Plane(origin=(c2[0] - left[0] * case.t, y, c2[1] - left[1] * case.t), xDir=(h[0], 0, h[1]), normal=(left[0], 0, left[1]))
        cutter = cq.Workplane(pl).slot2D(L, w).extrude(3 * case.t).val()
        solid = solid.cut(cutter)
    return solid


def tessellate(shape: cq.Shape, ang_tol: float = 0.5, tol: float = 0.01) -> trimesh.Trimesh:
    verts, tris = shape.tessellate(tol, ang_tol)
    m = trimesh.Trimesh(np.array([v.toTuple() for v in verts]), np.array(tris), process=True)
    m.merge_vertices(digits_vertex=6)
    trimesh.repair.fix_normals(m)
    return m


def random_motion(mesh: trimesh.Trimesh, seed: int) -> trimesh.Trimesh:
    rng = np.random.default_rng(seed)
    T = trimesh.transformations.random_rotation_matrix(rng.random(3))
    T[:3, 3] = rng.uniform(-200, 200, 3)
    m = mesh.copy()
    m.apply_transform(T)
    return m


def sheet_mesh(case: SheetCase) -> trimesh.Trimesh:
    return random_motion(tessellate(build_sheet(case), case.ang_tol), case.seed)


def prism_mesh(seed: int = 0, ang_tol: float = 0.5) -> tuple[trimesh.Trimesh, dict]:
    """A 2.5D machined part: rounded plate 60 x 40 x 8 with a 4 mm boss step, two Ø6 holes and a Ø10 bore."""
    base = (cq.Workplane("XY").rect(60, 40).extrude(8).edges("|Z").fillet(5)
            .faces(">Z").workplane().center(10, 0).rect(30, 20).extrude(4)
            .faces(">Z").workplane().hole(10)
            .faces("<Z").workplane().pushPoints([(-20, 12), (-20, -12)]).hole(6))
    m = random_motion(tessellate(base.val(), ang_tol), seed)
    return m, {"holes": [6.0, 10.0], "volume": base.val().Volume()}
