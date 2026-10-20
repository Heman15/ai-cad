# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI tool execution: chat tools run through the same Project commands as manual controls.

The Claude API is replaced by a scripted client so the loop logic is tested deterministically.
"""

from types import SimpleNamespace as NS

from cadcore import Project
from cadcore.assistant import TOOLS, Assistant


def tool_use(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def text(t):
    return NS(type="text", text=t)


def reply(*blocks, stop="tool_use"):
    return NS(stop_reason=stop, content=list(blocks))


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot; the list keeps growing
        return self.responses.pop(0)


def make(responses, project=None):
    client = Scripted(responses)
    return Assistant(project or Project(), client_factory=lambda: client), client


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        assert t["input_schema"]["type"] == "object" and set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])


def test_rejection_is_reported_and_later_steps_are_skipped():
    p = Project()
    p.execute("create_plate", length=100, width=60, thickness=5)
    p.execute("add_hole", id="h1", diameter=6, x=20, y=15)
    a, client = make([
        reply(tool_use("t1", "set_parameter", {"name": "h1.diameter", "value": 50}),   # rejected: breaks out
              tool_use("t2", "assign_material", {"name": "steel"})),                    # must be skipped
        reply(tool_use("t3", "set_parameter", {"name": "h1.diameter", "value": 8})),
        reply(text("Hole h1 is now 8 mm."), stop="end_turn"),
    ], p)
    out = a.send("make the hole bigger")
    assert out["reply"] == "Hole h1 is now 8 mm."
    assert [x["ok"] for x in out["actions"]] == [False, True]
    assert p.spec.feature("h1").params["diameter"] == 8 and p.spec.material is None
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["is_error"] and "rejected" in results[0]["content"]
    assert results[1]["is_error"] and results[1]["content"].startswith("skipped")
    assert out["diff"]["changed"] == [{"name": "h1.diameter", "old": 6, "new": 8}]


def test_manual_edits_and_selection_are_visible_to_the_ai():
    p = Project()
    p.execute("create_plate", length=100, width=60, thickness=5)
    p.execute("add_hole", id="holes", diameter=6, positions=[[10, 10], [90, 10]])
    p.execute("set_parameter", name="vars.thickness", value=7)  # manual edit before the chat turn
    from cadcore.mesh import describe
    face = next(f for f in describe(p.result)["faces"] if f["feature"] == "holes")
    sel = [{"kind": "face", "point": face["point"], "type": face["type"], "area": face["area"]}]
    a, client = make([reply(text("ok"), stop="end_turn")], p)
    a.send("make this 10 mm", selection=sel)
    ctx = client.requests[0]["messages"][0]["content"][0]["text"]
    assert '"thickness": 7' in ctx
    assert "<selection>" in ctx and '"feature": "holes"' in ctx and '"count": 2' in ctx


def test_alternatives_are_previewed_not_applied_then_accepted():
    p = Project()
    p.execute("create_plate", length=100, width=60, thickness=5)
    alts = {"alternatives": [
        {"title": "Thicker", "description": "8 mm plate", "commands": [{"command": "set_parameter", "args": {"name": "vars.thickness", "value": 8}}]},
        {"title": "Invalid", "description": "bad", "commands": [{"command": "set_parameter", "args": {"name": "vars.thickness", "value": -1}}]},
    ]}
    a, _ = make([reply(tool_use("t1", "propose_alternatives", alts)), reply(text("Pick one."), stop="end_turn")], p)
    events = list(a.stream("give me options"))
    shown = next(e for e in events if e["type"] == "alternatives")["items"]
    assert shown[0]["preview"]["ok"] and shown[0]["preview"]["bbox_mm"][2] == 8
    assert not shown[1]["preview"]["ok"]
    assert p.spec.variables["thickness"] == 5  # nothing applied yet
    assert a.accept_alternative(0)["ok"]
    assert p.spec.variables["thickness"] == 8
    p.undo()
    assert p.spec.variables["thickness"] == 5  # accepted alternative is one undo step


def test_api_failure_rolls_back_history():
    class Boom:
        beta = NS(messages=NS(create=lambda **k: (_ for _ in ()).throw(RuntimeError("network down"))))

    a = Assistant(Project(), client_factory=lambda: Boom())
    try:
        a.send("hello")
    except RuntimeError:
        pass
    assert a.messages == []
