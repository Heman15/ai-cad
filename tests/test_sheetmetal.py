# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sheet metal: bends, flat pattern (bend allowance), cuts in the flat, collisions, DXF, sheet DFM rules."""

import math

import ezdxf
import pytest

from cadcore import Project
from cadcore.dfm import check
from cadcore.sheetmetal import bend_allowance, flat_dxf, flat_pattern


def tray(k=0.5):
    p = Project()
    assert p.execute("set_sheet_metal", thickness=2, bend_radius=2, k_factor=k).ok
    assert p.execute("add_feature", id="base", type="sheet_base", params={
        "plane": "XY", "shapes": [{"type": "rect", "w": 100, "h": 60, "centered": False}]}).ok
    return p


def test_single_flange_geometry_and_allowance():
    p = tray(k=0.44)
    assert p.execute("add_feature", id="f1", type="sheet_flange", params={"edge": {"near": [50, 0, 2]}, "length": 20}).ok
    T, R = 2, 2
    # base + quarter annulus bend + flange
    expected = 100 * 60 * T + math.pi / 4 * ((R + T) ** 2 - R ** 2) * 100 + 20 * T * 100
    assert p.result.volume_mm3 == pytest.approx(expected, rel=1e-9)
    assert p.result.bbox_mm == pytest.approx((100, 60 + R + T, T + R + 20))
    fp = flat_pattern(p.result.sheet)
    ba = bend_allowance(90, R, 0.44, T)
    assert ba == pytest.approx(math.pi / 2 * (2 + 0.88))
    assert fp["size"] == pytest.approx((100, 60 + ba + 20))


def test_flat_area_matches_volume_with_neutral_k_and_cuts():
    p = tray(k=0.5)  # with K = 0.5 the neutral axis is the mid-plane: flat area x t == solid volume
    p.execute("add_feature", id="f1", type="sheet_flange", params={"edge": {"near": [50, 0, 2]}, "length": 20})
    p.execute("add_feature", id="f2", type="sheet_flange", params={"edge": {"near": [100, 30, 2]}, "length": 20})
    p.execute("add_feature", id="f3", type="sheet_flange", params={"edge": {"near": [50, 60, 0]}, "length": 15, "direction": "down", "angle": 45})
    assert p.execute("add_hole", id="h", diameter=8, positions=[[30, 30]], plane={"base": "XY", "offset": 2}).ok
    assert p.execute("add_feature", id="slot", type="extrude", params={"plane": {"base": "XY", "offset": 2}, "op": "cut", "through": True,
                                                                      "shapes": [{"type": "slot", "length": 30, "w": 6, "x": 70, "y": 30}]}).ok
    fp = flat_pattern(p.result.sheet)
    assert fp["area"] * 2 == pytest.approx(p.result.volume_mm3, rel=1e-9)
    assert len(fp["faces"][0].innerWires()) == 2 and len(fp["bends"]) == 3


def test_collisions_trims_and_unsupported_features(tmp_path):
    p = tray()
    p.execute("add_feature", id="a", type="sheet_flange", params={"edge": {"near": [50, 0, 2]}, "length": 20, "angle": 135})
    r = p.execute("add_feature", id="b", type="sheet_flange", params={"edge": {"near": [0, 30, 2]}, "length": 20, "angle": 135})
    assert not r.ok and "≥ 21" in r.message  # 2 + 2 + cos45 * (20 + 2 + 2) = 20.97
    assert p.execute("add_feature", id="b", type="sheet_flange", params={"edge": {"near": [0, 30, 2]}, "length": 20, "angle": 135,
                                                                        "trim_start": 21, "trim_end": 21}).ok
    path = tmp_path / "flat.dxf"
    flat_dxf(flat_pattern(p.result.sheet), str(path))
    layers = {e.dxf.layer for e in ezdxf.readfile(path).modelspace()}
    assert {"OUTLINE", "BEND", "NOTES"} <= layers
    assert p.execute("add_feature", id="r", type="fillet", params={"edges": {"near": [[100, 30, 0]]}, "radius": 1}).ok
    with pytest.raises(ValueError, match="flat pattern unavailable"):
        flat_pattern(p.result.sheet)
    assert not p.execute("add_feature", id="c", type="sheet_flange", params={"edge": {"near": [50, 30, 2]}, "length": 5}).ok


def test_sheet_dfm_rules():
    p = Project()
    p.execute("set_sheet_metal", thickness=3, bend_radius=1, k_factor=0.44)
    p.execute("add_feature", id="base", type="sheet_base", params={"plane": "XY", "shapes": [{"type": "rect", "w": 80, "h": 50, "centered": False}]})
    p.execute("add_feature", id="lip", type="sheet_flange", params={"edge": {"near": [40, 0, 3]}, "length": 4})
    p.execute("add_hole", id="h", diameter=5, positions=[[40, 5]], plane={"base": "XY", "offset": 3})
    rules = {f["rule"] for f in check(p.result, "sheet_metal")["findings"]}
    assert {"bend_radius", "flange_length", "hole_near_bend"} <= rules
