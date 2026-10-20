# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dimensional tolerances (ISO 286 fits, ISO 2768 general tolerances) and GD&T annotation rules.

Values are from ISO 286-1/-2 (IT grades, fundamental deviations) and ISO 2768-1, in micrometres
unless stated. Supported fits: holes D E F G H JS, shafts d e f g h js k m n p, grades IT5-IT11,
nominal sizes 0-500 mm. Other letters/grades are rejected rather than guessed.
"""

from __future__ import annotations

import re
from bisect import bisect_left

# upper bounds of the nominal size ranges (mm); a size belongs to the first range with size <= bound
RANGES = [3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500]

IT = {
    5: [4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27],
    6: [6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40],
    7: [10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63],
    8: [14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97],
    9: [25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155],
    10: [40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250],
    11: [60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400],
}

# shaft upper deviations es (µm) for clearance letters
SHAFT_ES = {
    "d": [-20, -30, -40, -50, -65, -80, -100, -120, -145, -170, -190, -210, -230],
    "e": [-14, -20, -25, -32, -40, -50, -60, -72, -85, -100, -110, -125, -135],
    "f": [-6, -10, -13, -16, -20, -25, -30, -36, -43, -50, -56, -62, -68],
    "g": [-2, -4, -5, -6, -7, -9, -10, -12, -14, -15, -17, -18, -20],
    "h": [0] * 13,
}
# shaft lower deviations ei (µm) for transition/interference letters
SHAFT_EI = {
    "k": [0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5],  # IT4..IT7; 0 for coarser grades
    "m": [2, 4, 6, 7, 8, 9, 11, 13, 15, 17, 20, 21, 23],
    "n": [4, 8, 10, 12, 15, 17, 20, 23, 27, 31, 34, 37, 40],
    "p": [6, 12, 15, 18, 22, 26, 32, 37, 43, 50, 56, 62, 68],
}

_FIT = re.compile(r"^\s*([A-Za-z]{1,2})\s*(\d{1,2})\s*$")


def _range_index(size: float) -> int:
    if not 0 < size <= 500:
        raise ValueError(f"ISO 286 limits are supported for sizes 0-500 mm, got {size:g}")
    return bisect_left(RANGES, size)


def fit_limits(size: float, fit: str) -> tuple[float, float]:
    """Deviations in mm (upper, lower) for e.g. 'H7' (hole) or 'g6' (shaft)."""
    m = _FIT.match(fit)
    if not m:
        raise ValueError(f"tolerance class {fit!r} not understood; use e.g. H7, g6, js6")
    letter, grade = m.group(1), int(m.group(2))
    if grade not in IT:
        raise ValueError(f"IT{grade} not supported (IT5-IT11)")
    i = _range_index(size)
    it = IT[grade][i]
    lo = letter.lower()
    if lo == "js":
        return it / 2000, -it / 2000
    hole = letter[0].isupper()
    if lo in SHAFT_ES:
        es = SHAFT_ES[lo][i]
        upper, lower = es, es - it
    elif lo in SHAFT_EI and not hole:
        ei = SHAFT_EI[lo][i] if (lo != "k" or grade <= 7) else 0
        upper, lower = ei + it, ei
    else:
        raise ValueError(f"tolerance class {fit!r} not supported; holes: D E F G H JS, shafts: d e f g h js k m n p")
    if hole:  # hole deviations mirror the shaft letter: EI = -es
        upper, lower = -lower, -upper
    return upper / 1000, lower / 1000


# ISO 2768-1 general tolerances for linear dimensions (± mm) by class, size ranges (mm)
GENERAL_RANGES = [3, 6, 30, 120, 400, 1000, 2000, 4000]
GENERAL = {
    "f": [0.05, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, None],
    "m": [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0],
    "c": [0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 4.0],
    "v": [None, 0.5, 1.0, 1.5, 2.5, 4.0, 6.0, 8.0],
}
_GENERAL_RE = re.compile(r"^ISO 2768-([fmcv])([HKL])?$")


def general_tolerance(standard: str, size: float) -> float | None:
    m = _GENERAL_RE.match(standard.strip())
    if not m:
        raise ValueError("general tolerance must be like 'ISO 2768-m' or 'ISO 2768-mK'")
    if size < 0.5:
        return None
    return GENERAL[m.group(1)][min(bisect_left(GENERAL_RANGES, size), len(GENERAL_RANGES) - 1)]


# ---------------- GD&T ----------------

FORM = {"straightness", "flatness", "circularity", "cylindricity"}
PROFILE = {"profile_line", "profile_surface"}  # datums optional
ORIENTATION = {"parallelism", "perpendicularity", "angularity"}
LOCATION = {"position", "concentricity", "symmetry"}
RUNOUT = {"circular_runout", "total_runout"}
CHARACTERISTICS = FORM | PROFILE | ORIENTATION | LOCATION | RUNOUT


def check_gdt(characteristic: str, datums: list[str], defined_datums: set[str], modifier: str | None,
              diameter_zone: bool) -> None:
    """Raise ValueError for frames that break basic ISO 1101 / ASME Y14.5 rules."""
    if characteristic not in CHARACTERISTICS:
        raise ValueError(f"unknown characteristic {characteristic!r}; use one of {', '.join(sorted(CHARACTERISTICS))}")
    if characteristic in FORM and datums:
        raise ValueError(f"{characteristic} is a form tolerance and takes no datum references")
    if characteristic in ORIENTATION | RUNOUT | {"concentricity", "symmetry"} and not datums:
        raise ValueError(f"{characteristic} needs at least one datum reference")
    missing = [d for d in datums if d not in defined_datums]
    if missing:
        raise ValueError(f"datum(s) {', '.join(missing)} are not defined; add the datum feature first")
    if len(set(datums)) != len(datums) or len(datums) > 3:
        raise ValueError("use up to three distinct datum references (primary, secondary, tertiary)")
    if modifier not in (None, "", "M", "L"):
        raise ValueError("material modifier must be M (MMC) or L (LMC)")
    if modifier and characteristic not in {"position", "straightness", "parallelism", "perpendicularity", "angularity"}:
        raise ValueError(f"a material modifier is not applicable to {characteristic}")
    if diameter_zone and characteristic not in {"position", "straightness", "parallelism", "perpendicularity", "angularity", "concentricity"}:
        raise ValueError(f"a diameter (Ø) tolerance zone does not apply to {characteristic}")
