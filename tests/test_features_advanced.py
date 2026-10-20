# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sweep, loft, splines, inclined planes, threads (each checked against hand-computed geometry)."""

import math

import pytest

from cadcore import Project
from cadcore.mesh import describe
from cadcore.threads import expected_groove_volume, parse


def ok(r):
    assert r.ok, r.message
    return r


def test_sweep_polyline_and_sketch_path():
    p = Project()
    ok(p.execute("add_feature", id="rod", type="sweep", params={
        "profile": {"plane": "XY", "shapes": [{"type": "rect", "w": 6, "h": 4}]},
        "path": {"points": [[0, 0, 0], [0, 0, 30]]}}))
    assert p.result.volume_mm3 == pytest.approx(720)

    q = Project()
    ok(q.execute("add_feature", id="path", type="sketch", params={
        "plane": "XZ", "path_only": True, "points": {"a": [0, 0], "b": [0, 40], "c": [30, 40]},
        "entities": [{"id": "l1", "type": "line", "p1": "a", "p2": "b"}, {"id": "l2", "type": "line", "p1": "b", "p2": "c"}],
        "constraints": [{"type": "fix", "point": "a"}, {"type": "fix", "point": "b"}, {"type": "fix", "point": "c"}]}))
    ok(q.execute("add_feature", id="pipe", type="sweep", params={
        "profile": {"plane": "XY", "shapes": [{"type": "circle", "d": 10}, {"type": "circle", "d": 6, "mode": "subtract"}]},
        "path": {"sketch": "path"}, "transition": "round"}))
    ring = math.pi * (25 - 9)
    assert ring * 60 < q.result.volume_mm3 < ring * 70  # between the inner and outer path lengths
    r = q.execute("add_feature", id="bad", type="sweep", params={
        "profile": {"plane": {"base": "XY", "offset": 5}, "shapes": [{"type": "circle", "d": 4}]}, "path": {"sketch": "path"}})
    assert not r.ok and "must start on the profile" in r.message


def test_loft_square_to_square_is_a_frustum():
    p = Project()
    ok(p.execute("add_feature", id="lf", type="loft", params={"ruled": True, "sections": [
        {"plane": "XY", "shapes": [{"type": "rect", "w": 40, "h": 40}]},
        {"plane": {"base": "XY", "offset": 30}, "shapes": [{"type": "rect", "w": 20, "h": 20}]}]}))
    a1, a2, h = 1600, 400, 30
    assert p.result.volume_mm3 == pytest.approx(h / 3 * (a1 + a2 + math.sqrt(a1 * a2)), rel=1e-6)


def test_spline_profile_and_closed_spline():
    p = Project()
    sk = {"plane": "XY", "points": {"a": [0, 0], "b": [40, 0], "c": [50, 20], "d": [20, 35], "e": [-5, 20]},
          "entities": [{"id": "l", "type": "line", "p1": "a", "p2": "b"}, {"id": "s", "type": "spline", "points": ["b", "c", "d", "e", "a"]}],
          "constraints": [{"type": "fix", "point": "a"}, {"type": "fix", "point": "b"}]}
    ok(p.execute("add_feature", id="sk", type="sketch", params=sk))
    ok(p.execute("add_feature", id="body", type="extrude", params={"sketch": "sk", "distance": 5}))
    assert 1200 * 5 < p.result.volume_mm3 < 2000 * 5
    closed = {"plane": "XY", "points": {"a": [10, 0], "b": [0, 10], "c": [-10, 0], "d": [0, -10]},
              "entities": [{"id": "s", "type": "spline", "points": ["a", "b", "c", "d"], "closed": True}],
              "constraints": [{"type": "fix", "point": k} for k in "abcd"]}
    q = Project()
    ok(q.execute("add_feature", id="sk", type="sketch", params=closed))
    ok(q.execute("add_feature", id="disc", type="extrude", params={"sketch": "sk", "distance": 1}))
    assert q.result.volume_mm3 == pytest.approx(math.pi * 100, rel=0.03)  # periodic spline through 4 circle points ≈ circle


def test_inclined_face_and_custom_plane():
    p = Project()
    p.execute("create_plate", length=60, width=40, thickness=20)
    ok(p.execute("add_feature", id="ch", type="chamfer", params={"edges": {"near": [[60, 20, 20]]}, "distance": 10}))
    face = next(f for f in describe(p.result)["faces"] if f["type"] == "PLANE" and abs(f["normal"][0] - 0.7071) < 1e-3)
    v0 = p.result.volume_mm3
    ok(p.execute("add_hole", id="ih", diameter=4, depth=6, positions=[[0, 0]],
                 plane={"face_at": {"point": face["point"], "normal": face["normal"]}}))
    assert v0 - p.result.volume_mm3 == pytest.approx(math.pi * 4 * 6, rel=1e-6)
    ok(p.execute("add_feature", id="boss", type="extrude", params={
        "plane": {"origin": [0, 20, 10], "normal": [-1, 0, 0]}, "distance": 5, "shapes": [{"type": "circle", "d": 8}]}))
    assert p.result.bbox_mm[0] == pytest.approx(65)


def test_threads_cosmetic_modeled_and_tapped_holes():
    assert parse("M8") == (8, 1.25) and parse("m10x1") == (10, 1.0)
    t = Project()
    t.execute("create_shaft", segments=[{"diameter": 20, "length": 10}, {"diameter": 8, "length": 20}])
    v0 = t.result.volume_mm3
    ok(t.execute("add_feature", id="th", type="thread", params={"designation": "M8", "face": {"near": [4, 0, 20]}}))
    assert t.result.volume_mm3 == v0 and t.result.threads[0].label == "M8×1.25" and not t.result.threads[0].internal
    ok(t.execute("update_feature", id="th", params={"modeled": True, "length": 12, "from": "end"}))
    assert v0 - t.result.volume_mm3 == pytest.approx(expected_groove_volume(4, 1.25, 12, False), rel=0.05)
    r = t.execute("add_feature", id="bad", type="thread", params={"designation": "M10", "face": {"near": [10, 0, 5]}})
    assert not r.ok and "does not match" in r.message

    h = Project()
    h.execute("create_plate", length=40, width=40, thickness=10)
    ok(h.execute("add_hole", id="tap", thread="M6", depth=8, thread_depth=6, positions=[[20, 20]]))
    assert (h.result.holes[0].thread, h.result.holes[0].diameter) == ("M6×1", 5.0)
    v0 = h.result.volume_mm3
    ok(h.execute("add_feature", id="ith", type="thread", params={"designation": "M6", "face": {"near": [22.5, 20, 7]},
                                                                "modeled": True, "length": 6, "from": "end"}))
    assert v0 - h.result.volume_mm3 == pytest.approx(expected_groove_volume(2.5, 1, 6, True), rel=0.05)
    assert h.result.threads[0].internal
