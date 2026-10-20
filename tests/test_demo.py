# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 'first complete demonstration', end to end through the HTTP API (including the chat stream).

The Claude API is replaced by a scripted client that issues the same tool calls a model would,
so this verifies the plumbing deterministically: questions, building, selection-based edits,
manual edits visible to the AI, rejection, mass, undo/redo, save/reopen, STEP export.
"""

import json
import os
import stat
from types import SimpleNamespace as NS

import cadquery as cq
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import server.app as appmod
    appmod.S.project = appmod.Project()
    appmod.S.path = None
    appmod.S.assistant = None
    return appmod


def tool(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def text(t):
    return NS(type="text", text=t)


class Scripted:
    """Stands in for anthropic.Anthropic; records what the model would have been shown."""

    def __init__(self):
        self.queue, self.seen = [], []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        content = kw["messages"][-1]["content"]
        self.seen.append("\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"))
        return self.queue.pop(0)


def chat(client, text_, selection=()):
    with client.stream("POST", "/api/chat/stream", json={"text": text_, "selection": list(selection)}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done", events[-1]
    return events


def test_first_complete_demonstration(server, tmp_path):
    api = TestClient(server.app)
    fake = Scripted()
    server.S.assistant = server.Assistant(server.S.project, client_factory=lambda: fake)
    server.S.assistant.lock = server.LOCK

    # 1-2. Describe a plate; the assistant asks for missing hole details instead of guessing.
    fake.queue = [NS(stop_reason="end_turn", content=[text(
        "What diameter should the mounting holes be, and how far from the edges?")])]
    ev = chat(api, "Make a 120 x 80 x 6 mm plate with four mounting holes")
    assert "diameter" in ev[-1]["reply"] and api.get("/api/model").json()["entities"] is None

    # 3. User answers; the assistant builds it step by step and records intent.
    fake.queue = [
        NS(stop_reason="tool_use", content=[tool("a", "create_plate", {"length": 120, "width": 80, "thickness": 6})]),
        NS(stop_reason="tool_use", content=[
            tool("b", "set_variable", {"name": "edge", "value": 10, "min": 0}),
            tool("c", "add_hole", {"id": "mounting_holes", "diameter": 6.6, "face": "top", "positions": [
                ["=edge", "=edge"], ["=length-edge", "=edge"], ["=edge", "=width-edge"], ["=length-edge", "=width-edge"]]}),
            tool("d", "record_design_intent", {"requirements": ["120x80x6 plate", "4 holes Ø6.6, 10 mm from edges"],
                                               "assumptions": ["through holes"]}),
        ]),
        NS(stop_reason="end_turn", content=[text("Built the plate with four Ø6.6 holes.")]),
    ]
    ev = chat(api, "6.6 mm holes, 10 mm from each edge")
    model = ev[-1]["model"]
    assert model["bbox_mm"] == pytest.approx([120, 80, 6])
    assert [e for e in ev if e["type"] == "tool"][0]["model"]["entities"]  # live update after first step
    assert model["spec"]["intent"]["assumptions"] == ["through holes"]

    # 4. Select one hole in the view and say "make this 10 mm".
    face = next(f for f in model["entities"]["faces"] if f["feature"] == "mounting_holes")
    sel = [{"kind": "face", "point": face["point"], "type": face["type"], "area": face["area"]}]
    fake.queue = [
        NS(stop_reason="tool_use", content=[tool("e", "set_parameter", {"name": "mounting_holes.diameter", "value": 10})]),
        NS(stop_reason="end_turn", content=[text("All four holes are now 10 mm.")]),
    ]
    ev = chat(api, "make this 10 mm", sel)
    assert '"feature": "mounting_holes"' in fake.seen[-2] and '"count": 4' in fake.seen[-2]  # the model saw the selection
    assert ev[-1]["diff"]["changed"] == [{"name": "mounting_holes.diameter", "old": 6.6, "new": 10}]

    # 5. Manual change of thickness; 6. the AI sees it and changes it again.
    r = api.post("/api/command", json={"command": "set_parameter", "args": {"name": "vars.thickness", "value": 8}}).json()
    assert r["result"]["ok"] and r["model"]["bbox_mm"][2] == pytest.approx(8)
    fake.queue = [
        NS(stop_reason="tool_use", content=[tool("f", "set_parameter", {"name": "vars.thickness", "value": 10})]),
        NS(stop_reason="end_turn", content=[text("Thickness changed from 8 to 10 mm.")]),
    ]
    ev = chat(api, "make it 2 mm thicker")
    assert '"thickness": 8' in fake.seen[-2]  # the manual edit was in the model state sent to the AI
    assert ev[-1]["model"]["bbox_mm"][2] == pytest.approx(10)

    # 7. Invalid edit is rejected and the previous model kept.
    r = api.post("/api/command", json={"command": "set_parameter", "args": {"name": "mounting_holes.diameter", "value": 30}}).json()
    assert not r["result"]["ok"] and "unchanged" in r["result"]["message"]
    assert r["model"]["spec"]["features"][1]["params"]["diameter"] == 10

    # 8. Mass after material assignment.
    r = api.post("/api/command", json={"command": "assign_material", "args": {"name": "aluminum_6061"}}).json()
    vol = r["model"]["volume_mm3"]
    assert r["model"]["mass_kg"] == pytest.approx(vol * 1e-9 * 2700)
    assert vol == pytest.approx(120 * 80 * 10 - 4 * 3.141592653589793 * 25 * 10)

    # 9. Undo and redo.
    assert api.post("/api/undo").json()["model"]["mass_kg"] is None
    assert api.post("/api/redo").json()["model"]["mass_kg"] == pytest.approx(vol * 1e-9 * 2700)

    # 10. Save, close, reopen, edit the feature history.
    path = tmp_path / "demo.aicad"
    assert api.post("/api/project/save", json={"path": str(path)}).json()["ok"]
    api.post("/api/new")
    assert api.get("/api/model").json()["entities"] is None
    m = api.post("/api/project/open", json={"path": str(path)}).json()["model"]
    assert m["volume_mm3"] == pytest.approx(vol) and m["spec"]["intent"]["requirements"]
    r = api.post("/api/command", json={"command": "set_parameter", "args": {"name": "vars.edge", "value": 15}}).json()
    assert r["result"]["ok"]
    holes = [f for f in r["model"]["entities"]["faces"] if f["feature"] == "mounting_holes"]
    assert sorted({tuple(f["hole_instance"]["position"][:2]) for f in holes}) == [(15, 15), (15, 65), (105, 15), (105, 65)]

    # 11. Export STEP and verify its geometry independently.
    step = tmp_path / "demo.step"
    assert api.post("/api/export/step", json={"path": str(step)}).json()["ok"]
    solid = cq.importers.importStep(str(step)).val()
    assert solid.isValid() and solid.Volume() == pytest.approx(r["model"]["volume_mm3"], rel=1e-6)
    bb = solid.BoundingBox()
    assert (bb.xlen, bb.ylen, bb.zlen) == pytest.approx((120, 80, 10))
    assert len([f for f in solid.Faces() if f.geomType() == "CYLINDER"]) == 4
    # project data stays separate from the STEP export
    assert json.loads(path.read_text())["spec_version"] == 2 and path.read_text().find("ISO-10303") < 0


def test_api_key_is_never_exposed(server, tmp_path):
    api = TestClient(server.app)
    secret = "sk-ant-test-SECRET-123"
    r = api.post("/api/settings", json={"api_key": secret, "model": "claude-opus-5-5"})
    assert r.status_code == 200 and secret not in r.text and r.json()["has_key"]
    assert secret not in api.get("/api/settings").text and secret not in api.get("/api/info").text
    cfg = tmp_path / "config.json"
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    assert api.post("/api/settings", json={"api_key": ""}).json()["has_key"] is False


def test_chat_without_key_reports_clearly(server):
    api = TestClient(server.app)
    ev = []
    with api.stream("POST", "/api/chat/stream", json={"text": "hi"}) as r:
        ev = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert ev[-1]["type"] == "error" and "API key" in ev[-1]["message"]
