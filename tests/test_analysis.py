# SPDX-License-Identifier: AGPL-3.0-or-later
"""FEA validated against beam theory and exact axial solutions; DFM rule checks on known geometry."""

import pytest
from fastapi.testclient import TestClient

import cadquery as cq
from cadcore import Project
from cadcore.dfm import check
from cadcore.fea import FEAError, analyse

STEEL = {"E": 210000, "nu": 0.3, "yield": 235}
FIX_X0 = [{"face": {"point": [0, 5, 5], "normal": [-1, 0, 0]}}]
TIP = {"point": [100, 5, 5], "normal": [1, 0, 0]}


def beam():
    return cq.Workplane().box(100, 10, 10, centered=False).val()


def test_cantilever_matches_beam_theory():
    r = analyse(beam(), STEEL, FIX_X0, [{"face": TIP, "force": [0, 0, -100]}], check_convergence=True)
    I, E, L, P = 10 * 10**3 / 12, 210000, 100, 100
    euler = P * L**3 / (3 * E * I)
    timoshenko = euler + P * L / (E / 2.6 * 5 / 6 * 100)
    assert euler * 0.995 < r["max_displacement_mm"] < timoshenko * 1.005
    assert r["max_von_mises_mpa"] == pytest.approx(P * L * 5 / I, rel=0.05)  # 60 MPa at the root
    assert r["safety_factor"] == pytest.approx(235 / r["max_von_mises_mpa"])
    assert r["equilibrium_error"] < 1e-8
    assert r["convergence"]["max_displacement_change_pct"] < 1


def test_axial_bar_is_exact():
    F, A, L, E = 1000, 100, 100, 210000
    r = analyse(beam(), STEEL, FIX_X0, [{"face": TIP, "force": [F, 0, 0]}])
    # free lateral contraction is restrained at the fixed end, so allow a few % on the peak; extension is near exact
    assert r["max_displacement_mm"] == pytest.approx(F * L / (E * A), rel=0.02)
    assert r["max_von_mises_mpa"] == pytest.approx(F / A, rel=0.15)


def test_pressure_load_and_reactions():
    plate = cq.Workplane().box(100, 50, 5, centered=False).val()
    supports = [{"face": {"point": [0, 25, 2.5], "normal": [-1, 0, 0]}}, {"face": {"point": [100, 25, 2.5], "normal": [1, 0, 0]}}]
    r = analyse(plate, STEEL, supports, [{"face": {"point": [50, 25, 5], "normal": [0, 0, 1]}, "pressure": 0.1}])
    assert r["applied_force_n"][2] == pytest.approx(-0.1 * 100 * 50, rel=1e-6)  # pushes into the top face
    assert r["reaction_force_n"][2] == pytest.approx(500, rel=1e-6)


def test_fea_input_errors():
    with pytest.raises(FEAError):
        analyse(beam(), STEEL, [], [{"face": TIP, "force": [0, 0, -1]}])
    with pytest.raises(FEAError, match="not found"):
        analyse(beam(), STEEL, [{"face": {"point": [0, 50, 50], "normal": [-1, 0, 0]}}], [{"face": TIP, "force": [0, 0, -1]}])


def test_fea_through_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "c.json"))
    import server.app as appmod
    appmod.S.project = appmod.Project()
    api = TestClient(appmod.app)
    api.post("/api/command", json={"command": "create_plate", "args": {"length": 100, "width": 10, "thickness": 10}})
    assert api.post("/api/analysis/fea", json={}).status_code == 400  # no material yet
    api.post("/api/command", json={"command": "assign_material", "args": {"name": "steel"}})
    r = api.post("/api/command", json={"command": "set_analysis_setup", "args": {"supports": FIX_X0, "loads": [{"face": TIP, "force": [0, 0, -100]}]}}).json()
    assert r["result"]["ok"]
    out = api.post("/api/analysis/fea", json={}).json()
    assert out["max_displacement_mm"] == pytest.approx(0.191, rel=0.02) and out["display"]["von_mises"]


def test_dfm_findings():
    p = Project()
    p.execute("create_plate", length=100, width=60, thickness=10)
    p.execute("add_pocket", id="pk", length=30, width=20, depth=9.4, x=50, y=30)
    p.execute("add_hole", id="tiny", diameter=0.8, depth=9, positions=[[10, 10]])
    r = check(p.result, "cnc_milling")
    rules = [f["rule"] for f in r["findings"]]
    assert rules.count("inner_corner") == 4 and "small_hole" in rules and "deep_hole" in rules
    assert any(f["rule"] == "min_wall" and f["value"] == pytest.approx(0.6, abs=0.01) for f in r["findings"])
    assert sorted(r["summary"]["setups"]) == ["+Z", "-Z"]

    q = Project()
    q.execute("create_plate", length=100, width=60, thickness=10)
    q.execute("add_pocket", id="pk", length=30, width=20, depth=4, x=50, y=30, corner_radius=3)
    assert check(q.result, "cnc_milling")["findings"] == []

    s = Project()
    s.execute("create_l_bracket", length=60, width=30, thickness=5, height=40)
    s.execute("add_feature", id="lip", type="extrude", params={"plane": {"base": "XY", "offset": 35}, "distance": 5,
              "shapes": [{"type": "rect", "w": 20, "h": 30, "x": 5, "y": 0, "centered": False}]})
    over = [f for f in check(s.result, "fdm_printing")["findings"] if f["rule"] == "overhang"]
    assert len(over) == 1 and "600 mm²" in over[0]["message"]
