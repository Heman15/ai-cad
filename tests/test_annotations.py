# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tolerances (ISO 286 / ISO 2768), GD&T rule checks, reference tracking and drawing output."""

import pytest

from cadcore import Project
from cadcore.annotations import evaluate_annotations
from cadcore.drawing import make_drawing
from cadcore.tolerances import fit_limits, general_tolerance


@pytest.mark.parametrize("size,fit,expected", [
    (20, "H7", (0.021, 0)), (20, "g6", (-0.007, -0.020)), (25, "k6", (0.015, 0.002)), (25, "p6", (0.035, 0.022)),
    (50, "H8", (0.039, 0)), (50, "f7", (-0.025, -0.050)), (10, "js6", (0.0045, -0.0045)), (10, "F8", (0.035, 0.013)),
    (100, "h9", (0, -0.087)), (40, "n6", (0.033, 0.017)),
])
def test_iso286_limits(size, fit, expected):
    assert fit_limits(size, fit) == pytest.approx(expected)


def test_unsupported_classes_are_rejected():
    for bad in ("K7", "z6", "H4", "Q7"):
        with pytest.raises(ValueError):
            fit_limits(20, bad)
    assert general_tolerance("ISO 2768-m", 100) == 0.3 and general_tolerance("ISO 2768-fH", 10) == 0.1


def part():
    p = Project()
    p.execute("create_plate", length=100, width=60, thickness=10)
    p.execute("add_hole", id="bore", diameter=20, positions=[[50, 30]])
    return p


def test_annotations_rules_and_tracking():
    p = part()
    top = {"point": [20, 20, 10], "normal": [0, 0, 1]}
    assert p.execute("add_annotation", type="tolerance", parameter="bore.diameter", fit="H7").ok
    assert not p.execute("add_annotation", type="tolerance", parameter="bore.diameter", plus=0.1, minus=0).ok  # duplicate
    assert not p.execute("add_annotation", type="tolerance", parameter="nope.diameter", fit="H7").ok
    assert p.execute("add_annotation", type="datum", label="A", face={"point": [20, 20, 0], "normal": [0, 0, -1]}).ok
    assert not p.execute("add_annotation", type="datum", label="A", face=top).ok
    r = p.execute("add_annotation", type="gdt", characteristic="flatness", tolerance=0.02, datums=["A"], target={"face": top})
    assert not r.ok and "form tolerance" in r.message
    r = p.execute("add_annotation", type="gdt", characteristic="parallelism", tolerance=0.05, target={"face": top})
    assert not r.ok and "datum" in r.message
    assert not p.execute("add_annotation", type="gdt", characteristic="position", tolerance=0.1, datums=["B"], target={"feature": "bore"}).ok
    assert p.execute("add_annotation", type="gdt", characteristic="parallelism", tolerance=0.05, datums=["A"], target={"face": top}).ok
    assert p.execute("add_annotation", type="gdt", characteristic="position", tolerance=0.1, diameter_zone=True, modifier="M",
                     datums=["A"], target={"feature": "bore"}).ok
    assert not p.execute("remove_annotation", id="datum_1").ok  # still referenced

    st = {s["id"]: s for s in evaluate_annotations(p.spec, p.result)}
    assert st["tolerance_1"]["max"] == pytest.approx(20.021) and all(s["ok"] for s in st.values())
    # nominal changes -> limits follow; the top face moves -> the parallelism frame is reported lost, not moved
    assert p.execute("set_parameter", name="bore.diameter", value=30).ok
    assert p.execute("set_parameter", name="vars.thickness", value=12).ok
    st = {s["id"]: s for s in evaluate_annotations(p.spec, p.result)}
    assert st["tolerance_1"]["upper"] == pytest.approx(0.021) and st["tolerance_1"]["nominal"] == 30
    assert not st["gdt_1"]["ok"] and "no longer exists" in st["gdt_1"]["message"]
    assert st["gdt_2"]["ok"] and st["datum_1"]["ok"]


def test_annotations_on_drawing():
    p = part()
    p.execute("add_annotation", type="tolerance", parameter="bore.diameter", fit="H7")
    p.execute("add_annotation", type="tolerance", parameter="vars.length", plus=0.1, minus=-0.1)
    p.execute("add_annotation", type="general_tolerance", standard="ISO 2768-mK")
    p.execute("add_annotation", type="datum", label="A", face={"point": [20, 20, 0], "normal": [0, 0, -1]})
    p.execute("add_annotation", type="gdt", characteristic="position", tolerance=0.1, diameter_zone=True, datums=["A"], target={"feature": "bore"})
    p.execute("add_annotation", type="surface_finish", ra=3.2, all=True)
    svg = make_drawing(p.result, p.spec, "A3")["svg"]
    for text in ("Ø20 H7 THRU", "100 ±0.1", "ISO 2768-mK", ">A<", "Ø0.1", "Ra 3.2", "TOLERANCED DIMENSIONS", "[20 … 20.02]"):
        assert text in svg, text
    assert "NOT SPECIFIED — add" not in svg
