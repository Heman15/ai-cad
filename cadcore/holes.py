# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hole Wizard: standard ISO metric holes from size tables, expanded into plain hole parameters.

Tables (mm):
- clearance holes: ISO 273 (fine / medium / coarse series = fit "close" / "normal" / "loose");
- tapped holes: tap drill per ISO 2306 / DIN 336 for ISO 261 coarse threads, d - P for the listed fine pitches;
- counterbores for ISO 4762 socket head cap screws: counterbore Ø per DIN 974-1 (series for ISO 4762),
  depth = head height k, so the head sits flush;
- countersinks for ISO 10642 countersunk socket screws: 90°, countersink Ø = theoretical head Ø dk,
  so the head sits flush.
"""

from __future__ import annotations

from typing import Any

from . import threads as _threads

# size -> (close, normal, loose) clearance diameter, ISO 273
CLEARANCE = {
    "M1.6": (1.7, 1.8, 2.0), "M2": (2.2, 2.4, 2.6), "M2.5": (2.7, 2.9, 3.1), "M3": (3.2, 3.4, 3.6),
    "M4": (4.3, 4.5, 4.8), "M5": (5.3, 5.5, 5.8), "M6": (6.4, 6.6, 7.0), "M8": (8.4, 9.0, 10.0),
    "M10": (10.5, 11.0, 12.0), "M12": (13.0, 13.5, 14.5), "M14": (15.0, 15.5, 16.5), "M16": (17.0, 17.5, 18.5),
    "M18": (19.0, 20.0, 21.0), "M20": (21.0, 22.0, 24.0), "M22": (23.0, 24.0, 26.0), "M24": (25.0, 26.0, 28.0),
    "M27": (28.0, 30.0, 32.0), "M30": (31.0, 33.0, 35.0), "M33": (34.0, 36.0, 38.0), "M36": (37.0, 39.0, 42.0),
}
FITS = ("close", "normal", "loose")

# ISO 4762 socket head cap screw: size -> (counterbore Ø, head height k)
CBORE_ISO4762 = {
    "M3": (6.5, 3.0), "M4": (8.0, 4.0), "M5": (10.0, 5.0), "M6": (11.0, 6.0), "M8": (15.0, 8.0),
    "M10": (18.0, 10.0), "M12": (20.0, 12.0), "M14": (24.0, 14.0), "M16": (26.0, 16.0),
    "M20": (33.0, 20.0), "M24": (40.0, 24.0), "M30": (48.0, 30.0), "M36": (57.0, 36.0),
}

# ISO 10642 countersunk socket screw: size -> theoretical head Ø dk (90° head)
CSK_ISO10642 = {"M3": 6.72, "M4": 8.96, "M5": 11.2, "M6": 13.44, "M8": 17.92, "M10": 22.4,
                "M12": 26.88, "M14": 30.8, "M16": 33.6, "M20": 40.32}

# tap drill for coarse threads (ISO 2306 / DIN 336 practice); fine threads use d - P
TAP_DRILL_COARSE = {1.6: 1.25, 2: 1.6, 2.5: 2.05, 3: 2.5, 4: 3.3, 5: 4.2, 6: 5.0, 8: 6.8, 10: 8.5, 12: 10.2, 14: 12.0,
                    16: 14.0, 18: 15.5, 20: 17.5, 22: 19.5, 24: 21.0, 27: 24.0, 30: 26.5, 36: 32.0, 42: 37.5, 48: 43.0}

# ISO 261 fine pitches accepted for tapped holes (coarse pitches come from threads.COARSE)
FINE = {3: (0.35,), 4: (0.5,), 5: (0.5,), 6: (0.75,), 8: (1.0,), 10: (1.25, 1.0), 12: (1.5, 1.25),
        14: (1.5,), 16: (1.5,), 18: (1.5,), 20: (1.5, 2.0), 22: (1.5,), 24: (2.0,), 27: (2.0,), 30: (2.0,), 36: (3.0,)}

TYPES = ("clearance", "tapped", "counterbore", "countersink")


def _size(size: Any) -> str:
    s = str(size).strip().upper().replace(" ", "")
    return s if s.startswith("M") else f"M{s}"


def expand(p: dict[str, Any]) -> dict[str, Any]:
    """Hole params with standard/type/size/fit -> plain hole params (diameter, cbore_*, csk_*, thread).

    Raises ValueError with a user-facing message for unknown standards, types, sizes or fits.
    """
    std = str(p.get("standard", "")).upper()
    if std != "ISO":
        raise ValueError(f"standard must be \"ISO\", got {p.get('standard')!r}")
    kind = p.get("type", "clearance")
    if kind not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}, got {kind!r}")
    for k in ("diameter", "cbore_diameter", "cbore_depth", "csk_diameter", "csk_angle", "thread"):
        if p.get(k) is not None:
            raise ValueError(f"{k} comes from the standard table; remove it or drop 'standard' to size the hole by hand")
    fit = p.get("fit", "normal")
    if fit not in FITS:
        raise ValueError(f"fit must be close, normal or loose, got {fit!r}")
    out = {k: v for k, v in p.items() if k not in ("standard", "type", "size", "fit")}
    if kind == "tapped":
        try:
            d, pitch = _threads.parse(str(p.get("size", "")))
        except ValueError as e:
            raise ValueError(str(e))
        coarse = _threads.COARSE.get(d)
        if pitch != coarse and pitch not in FINE.get(d, ()):  # type: ignore[call-overload]
            known = sorted({f"M{k:g}" for k in _threads.COARSE} | {f"M{k:g}x{q:g}" for k, qs in FINE.items() for q in qs})
            raise ValueError(f"no ISO tapped hole M{d:g}×{pitch:g}; known: {', '.join(known)}")
        out["thread"] = f"M{d:g}" if pitch == coarse else f"M{d:g}x{pitch:g}"
        out["diameter"] = TAP_DRILL_COARSE[d] if pitch == coarse else _threads.tap_drill(d, pitch)
        return out
    size = _size(p.get("size", ""))
    table = {"clearance": CLEARANCE, "counterbore": CBORE_ISO4762, "countersink": CSK_ISO10642}[kind]
    if size not in table or size not in CLEARANCE:
        raise ValueError(f"no ISO {kind} hole for size {p.get('size')!r}; known sizes: {', '.join(table)}")
    out["diameter"] = CLEARANCE[size][FITS.index(fit)]
    if kind == "counterbore":
        out["cbore_diameter"], out["cbore_depth"] = CBORE_ISO4762[size]
    elif kind == "countersink":
        out["csk_diameter"], out["csk_angle"] = CSK_ISO10642[size], 90
    return out


def label(p: dict[str, Any]) -> str:
    """Short description for callouts, e.g. 'M6 SHCS ISO 4762 C'BORE'."""
    kind = p.get("type", "clearance")
    size = _size(p.get("size", ""))
    return {"clearance": f"{size} CLEARANCE ({p.get('fit', 'normal').upper()}, ISO 273)",
            "tapped": f"{size} TAPPED",
            "counterbore": f"{size} ISO 4762 C'BORE",
            "countersink": f"{size} ISO 10642 CSK"}[kind]
