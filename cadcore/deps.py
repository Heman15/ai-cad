# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dependency analysis between variables and features, and spec diffs."""

from __future__ import annotations

from typing import Any

from .expr import names_in
from .spec import PartSpec

_FACE_PLANE_NOTE = "face-referenced plane (depends on the geometry built before it)"


def _walk_strings(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def feature_dependencies(spec: PartSpec) -> dict[str, dict[str, Any]]:
    """{feature_id: {"features": [...explicit refs], "variables": [...], "geometry": bool}}"""
    out = {}
    for f in spec.features:
        refs, vars_ = set(), set()
        p = f.params
        for key in ("feature", "sketch"):
            if isinstance(p.get(key), str):
                refs.add(p[key])
        for key in ("edges", "faces", "edge"):
            sel = p.get(key)
            if isinstance(sel, str) and sel.startswith("feature:"):
                refs.add(sel.split(":", 1)[1])
        for s in _walk_strings(p):
            vars_ |= names_in(s) & set(spec.variables)
        plane = p.get("plane")
        geometry = isinstance(plane, dict) and ("face" in plane or "face_at" in plane)
        neutral = p.get("neutral_plane")
        geometry = geometry or (isinstance(neutral, dict) and ("face" in neutral or "face_at" in neutral))
        geometry = geometry or f.type in ("fillet", "chamfer", "shell", "draft", "variable_fillet", "rib", "sheet_hem")
        out[f.id] = {"features": sorted(refs), "variables": sorted(vars_), "geometry": geometry}
    return out


def dependents_of(spec: PartSpec, fid: str) -> list[str]:
    return [k for k, v in feature_dependencies(spec).items() if fid in v["features"]]


def variable_users(spec: PartSpec, name: str) -> list[str]:
    users = [k for k, v in feature_dependencies(spec).items() if name in v["variables"]]
    users += [f"vars.{k}" for k, v in spec.variables.items() if isinstance(v, str) and name in names_in(v)]
    return users


def spec_diff(old: PartSpec, new: PartSpec) -> dict[str, Any]:
    """What changed between two specs: parameters, added/removed features, material."""
    po, pn = old.parameters(), new.parameters()
    old_ids, new_ids = {f.id for f in old.features}, {f.id for f in new.features}
    added = [f.id for f in new.features if f.id not in old_ids]
    removed = [f.id for f in old.features if f.id not in new_ids]
    changed = [
        {"name": k, "old": po.get(k), "new": pn.get(k)}
        for k in sorted(set(po) | set(pn))
        if po.get(k) != pn.get(k) and k.split(".")[0].split("[")[0] not in added + removed
    ]
    sup = [f.id for f in new.features if f.id in old_ids and old.feature(f.id).suppressed != f.suppressed]
    out: dict[str, Any] = {"changed": changed, "added": added, "removed": removed, "suppression_changed": sup}
    if (old.material and old.material.name) != (new.material and new.material.name):
        out["material"] = {"old": old.material and old.material.name, "new": new.material and new.material.name}
    return out
