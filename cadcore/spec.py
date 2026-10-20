# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parametric part specification: the single source of truth for a model.

A part is an ordered list of features (a feature tree) plus named variables.
Geometry is always rebuilt from this spec, never edited directly.

Conventions (see builder.py for full details):
- All lengths in mm, angles in degrees.
- Any numeric value may be a formula string such as "=length/2 - 5" that uses variables.
- Sketch and hole coordinates are GLOBAL coordinates of the two axes lying in the plane:
  XY plane -> (x, y), YZ plane -> (y, z), XZ plane -> (x, z).
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SPEC_VERSION = 2


@dataclass
class Feature:
    id: str
    type: str
    params: dict[str, Any]
    suppressed: bool = False


@dataclass
class Material:
    name: str
    density: float  # kg/m^3
    E: float | None = None  # Young's modulus, MPa
    nu: float | None = None  # Poisson's ratio
    yield_strength: float | None = None  # MPa


@dataclass
class PartSpec:
    name: str = "Part"
    variables: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, dict[str, float]] = field(default_factory=dict)  # variable -> {"min": .., "max": ..} (exclusive min)
    features: list[Feature] = field(default_factory=list)
    material: Material | None = None
    units: str = "mm"
    source: dict[str, Any] | None = None  # drawing analysis this part was built from
    intent: dict[str, Any] | None = None  # {"requirements": [...], "assumptions": [...], "open_questions": [...]}
    annotations: list[dict[str, Any]] = field(default_factory=list)  # tolerances, datums, GD&T, surface finish
    analysis: dict[str, Any] | None = None  # FEA setup: {"supports": [...], "loads": [...]}
    sheet: dict[str, Any] | None = None  # sheet-metal parameters {"thickness", "bend_radius", "k_factor"}

    def clone(self) -> "PartSpec":
        return copy.deepcopy(self)

    def feature(self, fid: str) -> Feature | None:
        return next((f for f in self.features if f.id == fid), None)

    @property
    def is_empty(self) -> bool:
        return not any(not f.suppressed for f in self.features)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["spec_version"] = SPEC_VERSION
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PartSpec":
        if d.get("spec_version", 1) < 2:
            from .migrate import migrate_v1
            d = migrate_v1(d)
        mat = d.get("material")
        return cls(
            name=d.get("name", "Part"),
            variables=dict(d.get("variables", {})),
            limits=dict(d.get("limits", {})),
            features=[Feature(**f) for f in d.get("features", [])],
            material=Material(**mat) if mat else None,
            units=d.get("units", "mm"),
            source=d.get("source"),
            intent=d.get("intent"),
            annotations=list(d.get("annotations", [])),
            analysis=d.get("analysis"),
            sheet=d.get("sheet"),
        )

    def parameters(self) -> dict[str, Any]:
        """Flat view of every editable value: {"vars.length": 100, "hole_1.diameter": 6, "base.shapes[0].w": "=length"}."""
        out = {f"vars.{k}": v for k, v in self.variables.items()}
        for f in self.features:
            _flatten(f.params, f.id, out)
        return out


def _flatten(obj: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj


_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def parse_path(path: str) -> list[str | int]:
    """'shapes[0].w' -> ['shapes', 0, 'w']"""
    parts: list[str | int] = []
    for seg in path.split("."):
        pos = 0
        for m in _TOKEN.finditer(seg):
            if m.start() != pos:
                raise KeyError(path)
            parts.append(m.group(1) if m.group(1) else int(m.group(2)))
            pos = m.end()
        if pos != len(seg) or not seg:
            raise KeyError(path)
    return parts


def get_path(obj: Any, parts: list[str | int]) -> Any:
    for p in parts:
        obj = obj[p]
    return obj


def set_path(obj: Any, parts: list[str | int], value: Any) -> Any:
    """Set an existing leaf; returns the old value. Raises KeyError/IndexError if the path does not exist."""
    parent = get_path(obj, parts[:-1])
    old = parent[parts[-1]]
    parent[parts[-1]] = value
    return old
