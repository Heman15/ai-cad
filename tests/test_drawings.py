# SPDX-License-Identifier: AGPL-3.0-or-later
"""Technical drawing generation and source-drawing interpretation (traceable dimensions)."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from cadcore import Project
from cadcore.drawing import make_drawing, project_view, to_pdf

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "base_plate_drawing.pdf"


def test_views_and_drawing_content():
    p = Project()
    p.execute("create_l_bracket", length=80, width=40, thickness=5, height=60)
    p.execute("add_hole", id="h", diameter=6, positions=[[55, 12], [55, 28]], plane={"base": "XY", "offset": "=thickness"})
    solid = p.result.shape.val()
    front = project_view(solid, "front")
    assert front.bbox[2] - front.bbox[0] == pytest.approx(80, abs=1e-6)
    assert front.bbox[3] - front.bbox[1] == pytest.approx(60, abs=1e-6)
    assert front.hidden  # hole walls are hidden in the front view
    d = make_drawing(p.result, p.spec, "A3", "first")
    assert d["scale"] == "1:1" and not d["warnings"]
    for text in ("2× Ø6 THRU", ">80<", ">60<", ">40<", ">55<", "FIRST ANGLE", "NOT SPECIFIED"):
        assert text in d["svg"], text
    assert to_pdf(d["svg"]).startswith(b"%PDF")
    third = make_drawing(p.result, p.spec, "A3", "third")
    assert ">RIGHT<" in third["svg"] and "THIRD ANGLE" in third["svg"]


class Scripted:
    def __init__(self, queue):
        self.queue, self.requests = queue, []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append([m for m in kw["messages"]])
        return self.queue.pop(0)


def tool(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("AI_CAD_CACHE", str(tmp_path / "cache"))
    import server.app as appmod
    appmod.S.project, appmod.S.path, appmod.S.assistant = appmod.Project(), None, None
    return appmod, TestClient(appmod.app)


def test_drawing_import_interpretation_and_traceability(api, tmp_path):
    appmod, client = api
    r = client.post("/api/source-drawing", json={"filename": EXAMPLE.name, "media_type": "application/pdf",
                                                 "data": base64.b64encode(EXAMPLE.read_bytes()).decode()}).json()
    assert r["result"]["ok"] and r["model"]["spec"]["source"]["drawing"]["pages"] == 1
    page = client.get("/api/source-drawing/page/1")
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")

    analysis = {"title": "Base plate", "units": "mm", "projection": "first", "questions": [],
                "dimensions": [
                    {"id": "D1", "value": 120, "kind": "linear", "describes": "overall length", "page": 1, "bbox": [0.1, 0.2, 0.2, 0.25], "status": "read", "confidence": "high"},
                    {"id": "D2", "value": 80, "kind": "linear", "describes": "overall width", "page": 1, "bbox": [0.05, 0.3, 0.08, 0.5], "status": "read", "confidence": "high"},
                    {"id": "D3", "value": 6, "kind": "linear", "describes": "thickness", "page": 1, "bbox": [0.05, 0.15, 0.08, 0.18], "status": "read", "confidence": "medium"},
                    {"id": "D4", "value": 6.6, "kind": "diameter", "describes": "4x mounting hole", "page": 1, "bbox": [0.45, 0.3, 0.55, 0.33], "status": "read", "confidence": "high"}]}
    fake = Scripted([
        NS(stop_reason="tool_use", content=[tool("a", "record_drawing_analysis", analysis)]),
        NS(stop_reason="tool_use", content=[tool("b", "create_plate", {"length": 120, "width": 80, "thickness": 6})]),
        NS(stop_reason="tool_use", content=[
            tool("c", "add_hole", {"id": "mounting_holes", "diameter": 6.6, "face": "top", "positions": [[10, 10], [110, 10], [10, 70], [110, 70]]}),
            tool("d", "link_dimension", {"dimension_id": "D1", "parameter": "vars.length"}),
            tool("e", "link_dimension", {"dimension_id": "D3", "parameter": "vars.thickness"}),
            tool("f", "link_dimension", {"dimension_id": "D4", "parameter": "mounting_holes.diameter"})]),
        NS(stop_reason="tool_use", content=[tool("g", "render_views", {})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Built; views match the drawing. Counterbored centre hole not modelled yet: is it required?")]),
    ])
    appmod.S.assistant = appmod.Assistant(appmod.S.project, client_factory=lambda: fake)
    with client.stream("POST", "/api/chat/stream", json={"text": "", "interpret_drawing": True}) as resp:
        events = [json.loads(l[6:]) for l in resp.iter_lines() if l.startswith("data: ")]
    assert events[-1]["type"] == "done", events[-1]
    first_user = fake.requests[0][0]["content"]
    assert any(b.get("type") == "image" for b in first_user)  # the model saw the drawing pages
    render_result = fake.requests[4][-1]["content"][0]["content"]
    assert render_result[0]["type"] == "image"  # ...and its own rendered views
    model = events[-1]["model"]
    dims = {d["id"]: d for d in model["spec"]["source"]["analysis"]["dimensions"]}
    assert dims["D1"]["parameter"] == "vars.length" and dims["D4"]["parameter"] == "mounting_holes.diameter"

    # user corrects a misread value in the drawing table: model follows in the same transaction
    r = client.post("/api/command", json={"command": "update_drawing_dimension", "args": {"dimension_id": "D3", "value": 8}}).json()
    assert r["result"]["ok"] and r["model"]["bbox_mm"][2] == pytest.approx(8)
    assert client.post("/api/undo").json()["model"]["bbox_mm"][2] == pytest.approx(6)

    # traceability survives save/reopen
    path = tmp_path / "plate.aicad"
    client.post("/api/project/save", json={"path": str(path)})
    client.post("/api/new")
    m = client.post("/api/project/open", json={"path": str(path)}).json()["model"]
    assert m["spec"]["source"]["drawing"]["filename"] == EXAMPLE.name
    assert {d["id"] for d in m["spec"]["source"]["analysis"]["dimensions"]} == {"D1", "D2", "D3", "D4"}
    assert client.get("/api/source-drawing/page/1").status_code == 200


def test_link_dimension_rejects_unknown_parameter():
    p = Project()
    p.execute("create_plate", length=10, width=10, thickness=1)
    p.execute("record_drawing_analysis", units="mm", questions=[], dimensions=[
        {"id": "D1", "value": 10, "kind": "linear", "describes": "x", "status": "read", "confidence": "high"}])
    assert not p.execute("link_dimension", dimension_id="D1", parameter="vars.nope").ok
    assert not p.execute("link_dimension", dimension_id="D9", parameter="vars.length").ok
    assert p.execute("link_dimension", dimension_id="D1", parameter="vars.length").ok
