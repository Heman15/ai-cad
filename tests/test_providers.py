# SPDX-License-Identifier: AGPL-3.0-or-later
"""Any AI provider: OpenAI-compatible tool loop, provider settings/key isolation, live sync for external clients."""

import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from cadcore import Project
from cadcore.assistant import Assistant


def call(id, name, args):
    return NS(id=id, type="function", function=NS(name=name, arguments=json.dumps(args)))


def reply(content=None, calls=None, finish="tool_calls"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls), finish_reason=finish)])


class ScriptedOpenAI:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


def test_openai_compatible_loop_runs_the_same_tools():
    p = Project()
    client = ScriptedOpenAI([
        reply(calls=[call("a", "create_plate", {"length": 100, "width": 60, "thickness": 5}),
                     call("b", "set_parameter", {"name": "vars.thickness", "value": -4}),   # rejected (limit)
                     call("c", "assign_material", {"name": "steel"})]),                   # skipped
        reply(calls=[call("d", "render_views", {})]),
        reply(content="Plate created; thickness must stay positive.", finish="stop"),
    ])
    a = Assistant(p, client_factory=lambda: client)
    a.provider = "openai"
    out = a.send("make a plate")
    assert out["reply"].startswith("Plate created")
    assert [x["ok"] for x in out["actions"]] == [True, False, True]
    assert p.spec.material is None and p.result.bbox_mm[2] == pytest.approx(5)
    req = client.requests[1]
    tools = {t["function"]["name"] for t in req["tools"]}
    assert {"add_feature", "create_plate", "render_views"} <= tools and req["messages"][0]["role"] == "system"
    tool_msgs = [m for m in req["messages"] if m["role"] == "tool"]
    assert "rejected" in tool_msgs[1]["content"] and tool_msgs[2]["content"].startswith("skipped")
    last = client.requests[2]["messages"][-1]  # rendered views follow as an image message for vision models
    assert last["role"] == "user" and last["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_bad_tool_arguments_are_reported_not_crashing():
    client = ScriptedOpenAI([
        reply(calls=[NS(id="x", type="function", function=NS(name="create_plate", arguments="{not json"))]),
        reply(content="sorry", finish="stop"),
    ])
    a = Assistant(Project(), client_factory=lambda: client)
    a.provider = "openai"
    out = a.send("x")
    assert out["actions"][0]["ok"] is False and "invalid arguments" in out["actions"][0]["message"]


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import server.app as appmod
    appmod.S.project, appmod.S.assistant, appmod.S.ui_selection = appmod.Project(), None, []
    return TestClient(appmod.app)


def test_provider_settings_and_key_isolation(api):
    s = api.post("/api/settings", json={"provider": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "m", "api_key": "gsk-SECRET"}).json()
    assert s["provider"] == "openai" and s["has_key"] and s["ready"] and "SECRET" not in json.dumps(s)
    s = api.post("/api/settings", json={"provider": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "g"}).json()
    assert not s["has_key"]  # the Groq key is never sent to Google
    s = api.post("/api/settings", json={"provider": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"}).json()
    assert s["ready"] and not s["has_key"]  # local Ollama needs no key
    s = api.post("/api/settings", json={"provider": "anthropic", "base_url": "", "model": "claude-opus-5-5"}).json()
    assert s["base_url"] is None and not s["ready"]
    assert {"claude", "openai", "gemini", "groq", "openrouter", "ollama"} <= set(s["presets"])


def test_live_sync_version_and_shared_selection(api):
    v0 = api.get("/api/version").json()["part"]
    api.post("/api/command", json={"command": "create_plate", "args": {"length": 50, "width": 40, "thickness": 5}})
    v1 = api.get("/api/version").json()["part"]
    assert v1 != v0 and api.get("/api/version").json()["part"] == v1  # stable when nothing changes
    face = next(f for f in api.get("/api/model").json()["entities"]["faces"] if f.get("normal") == [0, 0, 1])
    api.post("/api/ui/selection", json={"selection": [{"kind": "face", "point": face["point"], "type": "PLANE", "normal": [0, 0, 1], "area": face["area"]}]})
    sel = api.get("/api/ui/selection").json()["selection"]
    assert sel[0]["status"] == "ok" and sel[0]["feature"] == "base"
