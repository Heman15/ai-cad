# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command-line front end for the CAD core.

    python Cad.py demo              # build a sample bracket, edit it, compute mass, export STEP
    python Cad.py shell [part.json] # type commands, e.g.  add_hole id=h1 diameter=6 x=20 y=15

Shell extras: undo, redo, save <file>, params, drawing <file.pdf>, help, quit.
Values are JSON when they parse as JSON (numbers, lists like positions=[[10,10]]), otherwise strings ("=length/2").
"""

from __future__ import annotations

import json
import shlex
import sys

from cadcore import COMMANDS, Project


def parse_value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def run_line(project: Project, line: str) -> str:
    parts = shlex.split(line)
    if not parts:
        return ""
    cmd, rest = parts[0], parts[1:]
    if cmd == "help":
        return "commands: " + ", ".join(COMMANDS) + ", undo, redo, save <file>, params, drawing <file.pdf|svg>, quit"
    if cmd == "undo":
        return project.undo().message
    if cmd == "redo":
        return project.redo().message
    if cmd == "save":
        project.save(rest[0])
        return f"saved {rest[0]}"
    if cmd == "params":
        return json.dumps(project.spec.parameters(), indent=2)
    if cmd == "drawing":
        from cadcore.drawing import make_drawing, to_pdf
        if not project.result:
            return "error: the model is empty"
        d = make_drawing(project.result, project.spec, "A3")
        open(rest[0], "wb").write(to_pdf(d["svg"]) if rest[0].endswith(".pdf") else d["svg"].encode())
        return f"saved drawing {rest[0]} (scale {d['scale']})"
    args = {}
    for token in rest:
        key, sep, val = token.partition("=")
        if not sep:
            return f"expected key=value, got {token!r}"
        args[key] = parse_value(val)
    r = project.execute(cmd, **args)
    extra = ""
    if r.ok and "bbox_mm" in r.data:
        extra = f"   [volume {r.data['volume_mm3']} mm^3, bbox {r.data['bbox_mm']}]"
    return ("ok: " if r.ok else "error: ") + r.message + extra


def demo() -> None:
    p = Project()
    script = [
        "create_l_bracket length=80 width=40 thickness=5 height=60",
        # base flange: the plane z = thickness, so the holes follow the variable
        """add_hole id=base_holes diameter=6 plane='{"base":"XY","offset":"=thickness"}' positions=[[55,12],[55,28]]""",
        "add_hole id=bad_hole diameter=6 x=55 y=12",  # 'top' of an L-bracket is the upright's top: rejected
        "add_hole id=wall_hole diameter=8 face=left positions=[[20,40]]",
        "assign_material name=aluminum_6061",
        "calculate_mass",
        "set_parameter name=wall_hole.diameter value=10",
        "set_parameter name=wall_hole.diameter value=80",  # rejected: breaks out
        "calculate_mass",
        "export_step path=bracket.step",
        "drawing bracket_drawing.pdf",
        "save bracket.aicad",
    ]
    for line in script:
        print(f"> {line}\n  {run_line(p, line)}")


def shell(path: str | None) -> None:
    p = Project.load(path) if path else Project()
    print("CAD shell. Type 'help'.")
    while True:
        try:
            line = input("cad> ").strip()
        except EOFError:
            break
        if line in ("quit", "exit"):
            break
        try:
            print(run_line(p, line))
        except Exception as e:  # keep the shell alive on unexpected errors
            print(f"error: {e}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if mode == "shell":
        shell(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        demo()
