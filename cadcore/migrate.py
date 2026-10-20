# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit migration of v1 project files (fixed base shape + holes/pockets) to the v2 feature tree."""

from __future__ import annotations

from typing import Any


def _rect(w, h):
    return {"type": "rect", "w": w, "h": h, "x": 0, "y": 0, "centered": False}


def migrate_v1(d: dict[str, Any]) -> dict[str, Any]:
    base_type, base = d.get("base_type"), d.get("base", {})
    variables: dict[str, Any] = {}
    features: list[dict[str, Any]] = []
    notes = list(d.get("notes", [])) + ["migrated from project format v1"]

    if base_type in ("plate", "l_bracket"):
        variables = {k: base[k] for k in ("length", "width", "thickness", "height") if k in base}
        features.append({"id": "base", "type": "extrude",
                         "params": {"plane": "XY", "distance": "=thickness", "shapes": [_rect("=length", "=width")]}})
        if base_type == "l_bracket":
            features.append({"id": "upright", "type": "extrude",
                             "params": {"plane": "XY", "distance": "=height", "shapes": [_rect("=thickness", "=width")]}})
    elif base_type == "shaft":
        pts, z = [[0, 0]], 0.0
        for i, seg in enumerate(base.get("segments", []), 1):
            variables[f"d{i}"], variables[f"l{i}"] = seg["diameter"], seg["length"]
            pts.append([f"=d{i}/2", z])
            z += seg["length"]
            pts.append([f"=d{i}/2", z])
        pts.append([0, z])
        notes.append("shaft segment positions were fixed numbers in v1; edit the profile points if you change lengths")
        features.append({"id": "shaft", "type": "revolve",
                         "params": {"plane": "XZ", "axis": "v", "shapes": [{"type": "polygon", "points": pts}]}})

    for f in d.get("features", []):
        p = dict(f["params"])
        face = p.pop("face", None) or ("top" if base_type == "plate" else "base")
        if face == "upright":
            plane, u, v = {"base": "YZ", "offset": "=thickness"}, p.get("y", 0), p.get("z", 0)
            if p.get("depth") not in (None, "through"):
                notes.append(f"{f['id']}: blind depth now measured from the inner face of the upright")
        else:  # plate top / bracket base: the plane z = thickness
            plane, u, v = {"base": "XY", "offset": "=thickness"}, p.get("x", 0), p.get("y", 0)
        if f["type"] == "hole":
            features.append({"id": f["id"], "type": "hole", "params": {
                "plane": plane, "diameter": p["diameter"], "depth": p.get("depth", "through"), "positions": [[u, v]]}})
        elif f["type"] == "pocket":
            depth = p.get("depth")
            through = depth in (None, "through")
            params = {"plane": plane, "op": "cut", "shapes": [{"type": "rect", "w": p["length"], "h": p["width"], "x": u, "y": v}]}
            if through:
                params["through"] = True
            else:  # cut into the material: toward -Z (or -X on the upright)
                params["distance"] = -depth
            features.append({"id": f["id"], "type": "extrude", "params": params})

    return {
        "spec_version": 2, "name": "Part", "variables": variables, "features": features,
        "limits": {k: {"min": 0} for k in variables},
        "material": d.get("material"), "units": d.get("units", "mm"),
        "source": {"migration_notes": notes},
    }
