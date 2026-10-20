# SPDX-License-Identifier: AGPL-3.0-or-later
"""ISO metric thread data (coarse and common fine pitches) and helical thread geometry."""

from __future__ import annotations

import math
import re

import cadquery as cq

# nominal diameter -> coarse pitch (ISO 261)
COARSE = {1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75,
          14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 36: 4.0, 42: 4.5, 48: 5.0}

_RE = re.compile(r"^\s*M\s*(\d+(?:\.\d+)?)\s*(?:[x×]\s*(\d+(?:\.\d+)?))?\s*$", re.I)


def parse(designation: str) -> tuple[float, float]:
    """'M8' -> (8, 1.25); 'M8x1' -> (8, 1.0)."""
    m = _RE.match(str(designation))
    if not m:
        raise ValueError(f"thread {designation!r} not understood; use ISO metric like 'M8' or 'M8x1'")
    d = float(m.group(1))
    if m.group(2):
        return d, float(m.group(2))
    if d not in COARSE:
        raise ValueError(f"no coarse pitch known for M{d:g}; give the pitch, e.g. 'M{d:g}x1.5'")
    return d, COARSE[d]


def label(d: float, p: float) -> str:
    return f"M{d:g}×{p:g}" if COARSE.get(d) != p else f"M{d:g}×{p:g}"


def tap_drill(d: float, p: float) -> float:
    return round(d - p, 2)


def groove_cutter(radius: float, pitch: float, length: float, internal: bool,
                  origin: cq.Vector, axis: cq.Vector) -> cq.Shape:
    """Helical 60° V-groove body to subtract from the part.

    External: groove cut inward from the major radius (depth 0.6134 P).
    Internal: groove cut outward from the minor (drilled) radius (depth 0.5413 P).
    """
    depth = 0.6134 * pitch if not internal else 0.5413 * pitch
    over = 0.1 * pitch  # overlap past the surface for a clean boolean
    half = (depth + over) * math.tan(math.radians(30))
    if internal:
        tri = [(radius - over, -half), (radius - over, half), (radius + depth, 0)]
    else:
        tri = [(radius + over, -half), (radius + over, half), (radius - depth, 0)]
    pts = [cq.Vector(x, 0, z) for x, z in tri]
    profile = cq.Wire.makePolygon(pts, close=True)
    helix = cq.Wire.makeHelix(pitch, length, radius)
    body = cq.Solid.sweep(profile, [], helix, makeSolid=True, isFrenet=True)
    n = axis.normalized()
    x = cq.Vector(1, 0, 0) if abs(n.x) < 0.9 else cq.Vector(0, 1, 0)
    x = (x - n * x.dot(n)).normalized()
    return body.moved(cq.Plane(origin=origin.toTuple(), xDir=x.toTuple(), normal=n.toTuple()).location)


def expected_groove_volume(radius: float, pitch: float, length: float, internal: bool) -> float:
    """Approximate material removed by the groove (V cross-section inside the material x helix length)."""
    depth = 0.6134 * pitch if not internal else 0.5413 * pitch
    area = depth * depth * math.tan(math.radians(30))
    rc = radius - depth / 3 if not internal else radius + depth / 3
    return area * 2 * math.pi * rc * length / pitch


def cut_thread(shape: cq.Shape, radius: float, pitch: float, length: float, internal: bool,
               start: cq.Vector, axis: cq.Vector) -> cq.Shape:
    """Cut a helical groove and verify the removed volume; OCC booleans with helical sweeps are fragile,
    so try a few equivalent placements and refuse rather than return a wrong result."""
    expected = expected_groove_volume(radius, pitch, length, internal)
    v0 = shape.Volume()
    attempts = [(start, axis, length), (start + axis * length, -axis, length),
                (start + axis * (pitch / 2), axis, length - pitch), (start + axis * (length - pitch / 2), -axis, length - pitch)]
    for origin, direction, ln in attempts:
        if ln <= pitch:
            continue
        try:
            out = shape.cut(groove_cutter(radius, pitch, ln, internal, origin, direction)).clean()
        except Exception:
            continue
        removed = v0 - out.Volume()
        if out.isValid() and 0.5 * expected * ln / length < removed < 1.6 * expected * ln / length:
            return out
    raise ValueError("thread boolean failed")
