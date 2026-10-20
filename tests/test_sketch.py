# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constraint solver, DOF/conflict reporting, profiles, and sketch-based features."""

import math

import pytest

from cadcore import Project
from cadcore.sketch import profile_faces, solve


def rect_sketch(extra=(), w="=L", h="=W"):
    return {
        "plane": "XY",
        "points": {"a": [0, 0], "b": [90, 3], "c": [85, 55], "d": [-2, 50], "o": [40, 30]},
        "entities": [
            {"id": "l1", "type": "line", "p1": "a", "p2": "b"}, {"id": "l2", "type": "line", "p1": "b", "p2": "c"},
            {"id": "l3", "type": "line", "p1": "c", "p2": "d"}, {"id": "l4", "type": "line", "p1": "d", "p2": "a"},
            {"id": "h", "type": "circle", "center": "o", "r": 7},
        ],
        "constraints": [
            {"type": "fix", "point": "a", "at": [0, 0]},
            {"type": "horizontal", "line": "l1"}, {"type": "horizontal", "line": "l3"},
            {"type": "vertical", "line": "l2"}, {"type": "vertical", "line": "l4"},
            {"type": "length", "line": "l1", "value": w}, {"type": "length", "line": "l2", "value": h},
            {"type": "diameter", "circle": "h", "value": 12},
            {"type": "horizontal_distance", "a": "a", "b": "o", "value": "=L/2"},
            {"type": "vertical_distance", "a": "a", "b": "o", "value": "=W/2"},
            *extra,
        ],
    }


def num(vars_):
    from cadcore.expr import evaluate
    return lambda v, what: float(v) if isinstance(v, (int, float)) else evaluate(str(v).lstrip("="), vars_)


def test_rectangle_with_hole_is_fully_constrained():
    r = solve(rect_sketch(), num({"L": 100, "W": 60}))
    assert r.converged and r.dof == 0 and r.status == "fully constrained"
    assert r.points["c"] == pytest.approx((100, 60), abs=1e-9)
    assert r.points["o"] == pytest.approx((50, 30), abs=1e-9)
    faces = profile_faces(rect_sketch(), r)
    assert len(faces) == 1 and faces[0].Area() == pytest.approx(6000 - math.pi * 36)


def test_dof_and_conflicts_are_reported():
    s = rect_sketch()
    s["constraints"] = s["constraints"][:-2]  # hole centre free
    assert solve(s, num({"L": 100, "W": 60})).dof == 2
    bad = solve(rect_sketch([{"type": "length", "line": "l3", "value": 50}]), num({"L": 100, "W": 60}))
    assert not bad.converged and bad.status == "conflicting" and 10 in bad.conflicts


def test_tangent_slot_profile():
    sk = {
        "points": {"p1": [0, 5], "p2": [30, 5], "p3": [30, -5], "p4": [0, -5], "c1": [30, 0], "c2": [0, 0]},
        "entities": [
            {"id": "t", "type": "line", "p1": "p1", "p2": "p2"}, {"id": "a1", "type": "arc", "center": "c1", "start": "p3", "end": "p2"},
            {"id": "b", "type": "line", "p1": "p3", "p2": "p4"}, {"id": "a2", "type": "arc", "center": "c2", "start": "p1", "end": "p4"},
        ],
        "constraints": [
            {"type": "fix", "point": "c2", "at": [0, 0]}, {"type": "horizontal", "a": "c1", "b": "c2"},
            {"type": "distance", "a": "c2", "b": "c1", "value": 40}, {"type": "radius", "circle": "a1", "value": 6},
            {"type": "equal", "a": "a1", "b": "a2"},
            {"type": "tangent", "line": "t", "circle": "a1"}, {"type": "tangent", "line": "b", "circle": "a1"},
            {"type": "tangent", "line": "t", "circle": "a2"}, {"type": "tangent", "line": "b", "circle": "a2"},
        ],
    }
    r = solve(sk)
    assert r.converged and r.dof == 0
    assert profile_faces(sk, r)[0].Area() == pytest.approx(40 * 12 + math.pi * 36, rel=1e-6)


def test_other_constraint_types():
    sk = {
        "points": {"a": [0, 0], "b": [10, 1], "c": [3, 8], "m": [4, 1], "q": [2, 6]},
        "entities": [{"id": "l1", "type": "line", "p1": "a", "p2": "b"}, {"id": "l2", "type": "line", "p1": "a", "p2": "c"},
                     {"id": "l3", "type": "line", "p1": "b", "p2": "c", "construction": True}],
        "constraints": [
            {"type": "fix", "point": "a"}, {"type": "horizontal", "line": "l1"}, {"type": "length", "line": "l1", "value": 20},
            {"type": "angle", "a": "l1", "b": "l2", "value": 60}, {"type": "equal", "a": "l1", "b": "l2"},
            {"type": "midpoint", "point": "m", "line": "l1"}, {"type": "point_on_line", "point": "q", "line": "l2"},
            {"type": "distance", "a": "a", "b": "q", "value": 5},
        ],
    }
    r = solve(sk)
    assert r.converged and r.dof == 0
    assert r.points["c"] == pytest.approx((10, 20 * math.sin(math.radians(60))), abs=1e-7)
    assert r.points["m"] == pytest.approx((10, 0), abs=1e-7)
    assert r.points["q"] == pytest.approx((2.5, 5 * math.sin(math.radians(60))), abs=1e-7)


def test_sketch_features_regenerate_with_variables():
    p = Project()
    p.execute("set_variable", name="L", value=100, min=0)
    p.execute("set_variable", name="W", value=60, min=0)
    assert p.execute("add_feature", id="s1", type="sketch", params=rect_sketch()).ok
    assert p.result is None  # a sketch alone is a valid intermediate state
    assert p.execute("add_feature", id="body", type="extrude", params={"sketch": "s1", "distance": 8}).ok
    assert p.result.volume_mm3 == pytest.approx(8 * (6000 - math.pi * 36))
    assert p.result.sketches["s1"]["status"] == "fully constrained"
    assert p.execute("set_parameter", name="vars.L", value=120).ok
    assert p.result.bbox_mm == pytest.approx((120, 60, 8))
    assert not p.execute("remove_feature", id="s1").ok  # used by body
    r = p.execute("update_feature", id="s1", params={"constraints": rect_sketch([{"type": "length", "line": "l3", "value": 50}])["constraints"]})
    assert not r.ok and "conflict" in r.message

    # sketch on a face of the part, cut into it
    pocket = {"plane": {"face": ">Z"}, "points": {"c": [20, 20]}, "entities": [{"id": "k", "type": "circle", "center": "c", "r": 4}],
              "constraints": [{"type": "fix", "point": "c"}, {"type": "diameter", "circle": "k", "value": 10}]}
    assert p.execute("add_feature", id="s2", type="sketch", params=pocket).ok
    assert p.execute("add_feature", id="cut", type="extrude", params={"sketch": "s2", "op": "cut", "distance": 3}).ok
    assert p.result.volume_mm3 == pytest.approx(8 * (7200 - math.pi * 36) - math.pi * 25 * 3)
