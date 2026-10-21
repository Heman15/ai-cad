# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shaded pictures of triangle meshes without a GPU, so the assistant can look at assemblies and robots.

Painter's algorithm (triangles sorted far to near, flat shaded) drawn with Pillow: accurate enough to check
poses and placement, not a photoreal renderer. Four views in a 2x2 sheet: isometric, front, right, top.
"""

from __future__ import annotations

import io
import math
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

VIEWS = {  # name: (view direction from the camera to the model, up vector) in Z-up model space
    "isometric": (np.array([-1.0, 1.2, -0.9]), np.array([0.0, 0.0, 1.0])),
    "front": (np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0])),
    "right": (np.array([-1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0])),
    "top": (np.array([0.0, 0.0, -1.0]), np.array([0.0, 1.0, 0.0])),
}


def _hex(c: str | None) -> np.ndarray:
    if not c:
        return np.array([0.70, 0.75, 0.80])
    c = c.lstrip("#")
    rgb = np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)]) / 255
    lum = rgb @ [0.3, 0.59, 0.11]
    return rgb + (0.4 - lum) * (lum < 0.4)  # keep dark parts readable


def render_sheet(faces: list[dict[str, Any]], size: int = 700, title: str = "") -> bytes:
    """faces: entity dicts with world 'vertices', 'triangles' and optional 'color'. Returns PNG bytes."""
    tris, cols = [], []
    for f in faces:
        v = np.asarray(f["vertices"], dtype=float).reshape(-1, 3)
        t = np.asarray(f["triangles"], dtype=int).reshape(-1, 3)
        if len(t):
            tris.append(v[t])
            cols.append(np.repeat(_hex(f.get("color"))[None], len(t), axis=0))
    sheet = Image.new("RGB", (2 * size, 2 * size + (30 if title else 0)), (245, 247, 250))
    d = ImageDraw.Draw(sheet)
    if title:
        d.text((10, 8), title, fill=(30, 40, 50))
    if not tris:
        d.text((size - 40, size), "(empty)", fill=(120, 120, 120))
        return _png(sheet)
    T = np.concatenate(tris)  # (n, 3, 3)
    C = np.concatenate(cols)
    lo, hi = T.reshape(-1, 3).min(0), T.reshape(-1, 3).max(0)
    center, extent = (lo + hi) / 2, float(np.linalg.norm(hi - lo)) or 1.0
    for k, (name, (fwd, up)) in enumerate(VIEWS.items()):
        fwd = fwd / np.linalg.norm(fwd)
        right = np.cross(fwd, up)
        right /= np.linalg.norm(right)
        upv = np.cross(right, fwd)
        P = T - center
        x, y, z = P @ right, P @ upv, P @ fwd  # z: depth along the view direction
        n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        nl = np.linalg.norm(n, axis=1)
        keep = nl > 1e-12
        n = n[keep] / nl[keep, None]
        light = np.abs(n @ (-fwd)) * 0.6 + 0.4  # two-sided, head-on light
        shade = np.clip(C[keep] * light[:, None], 0, 1)
        xs, ys, zs = x[keep], y[keep], z[keep]
        order = np.argsort(-zs.mean(1))  # far first
        scale = 0.9 * size / extent
        ox, oy = (k % 2) * size + size / 2, (k // 2) * size + size / 2 + (30 if title else 0)
        X, Y = ox + xs * scale, oy - ys * scale
        for i in order:
            c = tuple(int(255 * q) for q in shade[i])
            d.polygon([(X[i, 0], Y[i, 0]), (X[i, 1], Y[i, 1]), (X[i, 2], Y[i, 2])], fill=c, outline=c)
        d.text(((k % 2) * size + 10, (k // 2) * size + (30 if title else 0) + 8), name, fill=(60, 70, 80))
        _axes(d, (k % 2) * size + 40, (k // 2 + 1) * size + (30 if title else 0) - 40, right, upv)
    return _png(sheet)


def _axes(d: ImageDraw.ImageDraw, x0: float, y0: float, right: np.ndarray, up: np.ndarray) -> None:
    for axis, col, lab in ((np.eye(3)[0], (220, 60, 60), "X"), (np.eye(3)[1], (40, 160, 90), "Y"), (np.eye(3)[2], (60, 110, 230), "Z")):
        dx, dy = axis @ right * 22, -(axis @ up) * 22
        if math.hypot(dx, dy) < 2:
            continue
        d.line([(x0, y0), (x0 + dx, y0 + dy)], fill=col, width=2)
        d.text((x0 + dx * 1.25 - 3, y0 + dy * 1.25 - 5), lab, fill=col)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
