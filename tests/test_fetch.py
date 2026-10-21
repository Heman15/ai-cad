# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub fetching (offline parts), the xacro expression guard, assembly pictures, and assistant extension tools."""

import pytest

from cadcore import fetch
from cadcore.assembly import AssemblyError
from cadcore.urdf import parse


@pytest.mark.parametrize("url, expect", [
    ("https://github.com/Hiwonder/ainex", ("Hiwonder", "ainex", None, None)),
    ("https://github.com/Hiwonder/ainex.git", ("Hiwonder", "ainex", None, None)),
    ("https://github.com/o/r/tree/main/src/pkg/", ("o", "r", "main", "src/pkg")),
    ("https://github.com/o/r/blob/ros2/urdf/robot.urdf.xacro", ("o", "r", "ros2", "urdf/robot.urdf.xacro")),
])
def test_github_urls(url, expect):
    u = fetch.parse_url(url)
    assert (u["owner"], u["repo"], u["ref"], u["path"]) == expect


@pytest.mark.parametrize("url", ["https://gitlab.com/o/r", "file:///etc/passwd", "https://github.com/o", "git@github.com:o/r.git",
                                 "https://github.com.evil.com/o/r"])
def test_non_github_urls_are_refused(url):
    with pytest.raises(fetch.FetchError):
        fetch.parse_url(url)


@pytest.mark.parametrize("expr, ok", [("pi/2", True), ("radius * 1.3", True), ("().__class__", False),
                                      ("vars(math)", False), ("python.open('/etc/passwd')", False)])
def test_xacro_expressions_are_screened(tmp_path, expr, ok):
    f = tmp_path / "r.urdf.xacro"
    f.write_text(f"""<robot name="r" xmlns:xacro="http://www.ros.org/wiki/xacro"><xacro:property name="radius" value="0.1"/>
      <link name="a"><visual><geometry><box size="${{{expr}}} 0.1 0.1"/></geometry></visual></link></robot>""")
    if ok:
        assert parse(f)["links"]["a"]["visuals"][0]["kind"] == "box"
    else:
        with pytest.raises(AssemblyError, match="won't evaluate"):
            parse(f)


def test_render_sheet_draws_something():
    from io import BytesIO

    from PIL import Image

    from cadcore.render import render_sheet

    cube = {"vertices": [0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0, 0, 0, 10, 10, 0, 10, 10, 10, 10, 0, 10, 10],
            "triangles": [0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7],
            "color": "#ff8800"}
    img = Image.open(BytesIO(render_sheet([cube], size=300, title="cube"))).convert("RGB")
    assert img.size == (600, 630)
    assert sum(1 for p in img.getdata() if p[0] > 200 and p[2] < 120) > 5000  # orange pixels in the views


def test_assistant_extra_tools_are_offered_and_run():
    from cadcore import Project
    from cadcore.assistant import TOOLS, Assistant

    a = Assistant(Project(), client_factory=lambda: None)
    schema = {"name": "ping", "description": "test", "input_schema": {"type": "object", "properties": {}}}
    a.extra_tools = {"ping": (schema, lambda args: {"ok": True, "message": "pong"}),
                     "boom": ({**schema, "name": "boom"}, lambda args: 1 / 0)}
    assert len(a.tools) == len(TOOLS) + 2
    assert a._run_tool("ping", {})["message"] == "pong"
    assert a._run_tool("boom", {})["ok"] is False  # errors go back to the model, not up the stack
