# SPDX-License-Identifier: AGPL-3.0-or-later
"""Common engineering material densities in kg/m^3 (typical values)."""

MATERIALS: dict[str, float] = {
    "aluminum_6061": 2700.0,
    "aluminum": 2700.0,
    "steel": 7850.0,
    "mild_steel": 7850.0,
    "stainless_304": 8000.0,
    "brass": 8500.0,
    "copper": 8960.0,
    "titanium_ti6al4v": 4430.0,
    "abs": 1040.0,
    "pla": 1240.0,
    "nylon": 1150.0,
}

# Typical mechanical properties for analysis: E and yield in MPa. These are representative handbook
# values, not certified data — grades, tempers and print settings change them; override when known.
MECHANICAL: dict[str, dict[str, float]] = {
    "aluminum_6061": {"E": 68900, "nu": 0.33, "yield": 276},   # T6
    "aluminum": {"E": 69000, "nu": 0.33, "yield": 95},          # unspecified alloy: conservative
    "steel": {"E": 210000, "nu": 0.30, "yield": 235},           # S235 structural
    "mild_steel": {"E": 210000, "nu": 0.30, "yield": 235},
    "stainless_304": {"E": 193000, "nu": 0.29, "yield": 215},
    "brass": {"E": 100000, "nu": 0.34, "yield": 200},
    "copper": {"E": 117000, "nu": 0.34, "yield": 70},
    "titanium_ti6al4v": {"E": 113800, "nu": 0.34, "yield": 880},
    "abs": {"E": 2200, "nu": 0.35, "yield": 40},
    "pla": {"E": 3500, "nu": 0.36, "yield": 50},
    "nylon": {"E": 2700, "nu": 0.39, "yield": 50},
}
