# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mesh -> parametric part on synthetic parts with known dimensions (tests/meshgen.py), plus the AiNex knee bracket
when it has been downloaded."""

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meshgen as mg  # noqa: E402
from cadcore import reverse  # noqa: E402

C = mg.SheetCase
CASES = [
    C("plate_t2", [60], [], t=2, holes=[(0, 15, 10, 6), (0, 45, 20, 4)], slots=[(30, 15, 14, 5)], seed=1),
    C("plate_t0.8_lowpoly", [50], [], t=0.8, width=40, holes=[(0, 10, 10, 5), (0, 40, 30, 8)], ang_tol=0.78, seed=2),
    C("L90_r1t", [30, 25], [90], t=1.5, r=1.5, holes=[(0, 12, 15, 5), (1, 14, 15, 4)], seed=3),
    C("L45_r0", [30, 25], [45], t=2, r=0, seed=4),
    C("L60_r0.5t", [28, 22], [-60], t=1.2, r=0.6, holes=[(1, 12, 10, 3)], seed=5),
    C("L120_r2t", [30, 25], [120], t=1, r=2, seed=6),
    C("L135_r2t", [30, 25], [-135], t=1, r=2, seed=7),
    C("L90_t3_r0.5t", [35, 30], [90], t=3, r=1.5, holes=[(0, 18, 15, 6.5), (1, 18, 15, 6.5)], ang_tol=0.78, seed=8),
    C("U_r0.5t", [20, 40, 20], [90, 90], t=1.5, r=0.75, holes=[(1, 20, 15, 8)], seed=9),
    C("Z_r2t", [20, 30, 20], [90, -90], t=3, r=6, seed=10),
    C("hat_t0.8", [15, 20, 30, 20, 15], [90, -90, -90, 90], t=0.8, r=0.8, holes=[(2, 15, 15, 5)], seed=11),
    C("L90_sharp_outer", [30, 25], [90], t=1.5, r=0.3, sharp_outer=True, holes=[(0, 12, 15, 4)], seed=12),
    C("U_sharp_outer_r0", [20, 35, 20], [-90, -90], t=2, r=0, sharp_outer=True, seed=13),
    C("Z_lowpoly", [25, 25, 25], [-75, 75], t=1.5, r=1.5, ang_tol=0.78, holes=[(0, 12, 12, 5), (2, 13, 18, 5)], seed=14),
]

# every thickness / radius / angle combination on an L bracket, each at its own random pose
CASES += [C(f"L{a}_t{t}_r{k}t", [24, 20], [a if i % 2 else -a], t=t, r=k * t, width=22, holes=[(0, 10, 11, 4)], seed=100 + i)
          for i, (t, k, a) in enumerate((t, k, a) for t in (0.8, 1.5, 3.0) for k in (0, 0.5, 1, 2) for a in (45, 90, 135))]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_sheet_metal_recovered(case):
    rb = reverse.rebuild(mg.sheet_mesh(case), case.name)
    assert rb["ok"], rb.get("reason")
    rep = rb["report"]
    assert rb["kind"] == "sheet_metal"
    assert abs(rep["thickness"] - case.t) <= 0.02
    assert rb["mismatch_pct"] < 3.0
    assert len(rep["bend_angles"]) == len(case.angles)
    assert all(abs(x - y) <= 1.0 for x, y in zip(sorted(rep["bend_angles"]), sorted(abs(a) for a in case.angles)))
    for b in rep["bends"]:
        assert abs(b["inner_radius"] - case.r) <= max(0.15, 0.1 * case.r), b
        assert b["outer_corner"] == ("sharp" if case.sharp_outer else "round")
    seg = math.ceil(2 * math.pi / case.ang_tol)
    if seg >= 8:
        got = sorted(h["d"] for h in rep["holes"])
        want = sorted(d for *_, d in case.holes)
        assert len(got) == len(want), (got, want)
        assert all(abs(g - w) <= 0.1 for g, w in zip(got, want)), (got, want)
    slots = [c for c in rep["cutouts"] if c["kind"] == "slot"]
    assert len(slots) == len(case.slots)
    for s, (_, _, L, w) in zip(slots, case.slots):
        assert abs(s["length"] - L) <= 0.1 and abs(s["w"] - w) <= 0.1


@pytest.mark.parametrize("case", [c for c in CASES if not c.sharp_outer], ids=[c.name for c in CASES if not c.sharp_outer])
def test_flat_pattern_length(case, tmp_path):
    """Flat length = sum of flat lengths + bend allowances (K = 0.44)."""
    rb = reverse.rebuild(mg.sheet_mesh(case), case.name)
    fp = reverse.flat_pattern(rb)
    ba = sum(math.radians(abs(a)) * (case.r + 0.44 * case.t) for a in case.angles)
    length = sum(case.lengths) + ba
    assert abs(max(fp["size"]) - max(length, case.width)) <= 0.1 and abs(min(fp["size"]) - min(length, case.width)) <= 0.1, fp["size"]
    reverse.flat_dxf(fp, str(tmp_path / "flat.dxf"))
    assert (tmp_path / "flat.dxf").stat().st_size > 1000


def test_prismatic_part():
    mesh, want = mg.prism_mesh(seed=3, ang_tol=0.6)
    rb = reverse.rebuild(mesh, "block")
    assert rb["ok"] and rb["kind"] == "prismatic", rb.get("reason")
    assert rb["mismatch_pct"] < 3.0
    assert sorted({round(h["d"], 1) for h in rb["report"]["holes"]}) == sorted(want["holes"])


def test_not_convertible_is_reported():
    import trimesh

    rb = reverse.rebuild(trimesh.creation.icosphere(subdivisions=3, radius=20), "ball")
    assert not rb["ok"] and rb["reason"].startswith("not convertible:")
    assert "spec" not in rb
    open_mesh = trimesh.creation.box((10, 10, 10))
    open_mesh.update_faces(np.arange(len(open_mesh.faces) - 2))
    rb = reverse.rebuild(open_mesh, "open")
    assert not rb["ok"] and "closed" in rb["reason"]


AINEX = Path(os.path.expanduser("~/Documents/AI-CAD-Downloads/Hiwonder-ainex/src/ainex_simulations/ainex_description/meshes/r_knee_link.STL"))


@pytest.mark.skipif(not AINEX.exists(), reason="AiNex meshes not downloaded")
def test_ainex_knee_bracket():
    import trimesh

    m = trimesh.load(str(AINEX), force="mesh")
    m.vertices *= 1000
    m.merge_vertices()
    body = max(m.split(only_watertight=False), key=lambda b: len(b.faces))
    rb = reverse.rebuild(body, "knee")
    assert rb["ok"], rb.get("reason")
    assert abs(rb["report"]["thickness"] - 1.5) <= 0.02
    assert sorted(round(a) for a in rb["report"]["bend_angles"]) == [90, 90]
    assert rb["mismatch_pct"] < 3.0
