# AI CAD

**Free, open-source parametric CAD for mechanical parts, with an AI design assistant built in.** Bring your own AI: your Claude login, any API, or a free local model.

Describe a part or drop in a drawing. The assistant asks for whatever it needs, then builds an **editable parametric model**. You and the AI then edit the **same** model, through chat or manual CAD tools. Every AI action is a validated CAD command, checked by the geometry kernel and undoable. Nothing is generated blind.

![Workspace](docs/images/workspace.png)

## Features

| | |
|---|---|
| **Parametric modelling** | Feature tree containing: constraint sketches, extrude/cut, revolve, sweep, loft, holes (counterbore, countersink, tapped) and an ISO Hole Wizard, threads, fillet, variable-radius fillet, chamfer, shell, draft, rib, patterns, mirror. Variables with formulas (`=width/2 - 5`) and units (`2 in`) |
| **Measure** | Point-to-point, edge, face and hole-centre distances with dX/dY/dZ, angles, diameters and areas, drawn as dimensions in the 3D view; overall part dimensions on demand |
| **Constraint sketcher** | Lines, arcs, circles and splines; 13 constraint types plus dimensions; a fixed origin; live degree-of-freedom and conflict reporting |
| **AI assistant** | Builds and edits through validated tools, asks about missing dimensions, records requirements separately from assumptions, and proposes redesign alternatives you accept with one click |
| **Drawing → CAD** | Import a PDF or image. Every dimension is traced to its spot on the drawing and to the model parameter it drives; mismatches are flagged and you can correct them there |
| **2D drawings** | Hidden-line views (first or third angle) with dimensions, hatched section views, hole and thread callouts, ISO 286 fits, ISO 2768, GD&T frames, datums and surface finish. PDF/SVG export |
| **Analysis** | Linear-static FEA (quadratic tetrahedra) validated against beam theory; manufacturability checks for CNC milling, FDM printing and sheet metal |
| **Sheet metal** | Base and edge flanges and open/closed hems with real bends, corner-collision checks, flat pattern with bend allowance, DXF export |
| **Assemblies** | Parts linked by file, mates (coincident, distance, parallel, concentric), interference check, bill of materials, STEP export |
| **Robots (URDF)** | Import URDF or xacro: box/cylinder/sphere links become editable parts, STL/OBJ/DAE meshes become solids (flat faces recovered), joints become sliders with limits and mimic joints; export the posed robot to STEP |
| **Mesh → parts** | Rebuild meshes as editable parts: sheet metal (thickness, flanges, bends, holes and slots as variables) or extrusions, accepted only within 5% of the mesh. A parts package for a whole robot groups identical and mirror-image parts and writes a BOM, STEP/STL, drawings and flat-pattern DXFs |
| **Open any 3D file** | STL, OBJ, PLY, 3MF, STEP, IGES and BREP open as a part (a fixed body you can cut holes in or add to), or insert into a part or an assembly. Meshes in metres or inches are rescaled with a units setting |
| **Convert to STL** | Any of those formats, AI CAD parts and whole assemblies (every part in place) to STL, one file or a batch; binary or ASCII; three accuracy levels for curved CAD faces |
| **3D printing (Cura-style)** | A Print workspace: about 1000 printers from 35 makers (Creality, Prusa, Voron, Anycubic, Elegoo, QIDI, Sovol…) or your own custom printer; their materials and quality profiles; infill, supports, adhesion, walls, temperatures, speeds and every other slicer setting; models placed, rotated, scaled, mirrored, laid flat (automatically or on a face you click) and arranged on the bed; slicing to G-code with time and filament estimates; a layer-by-layer toolpath preview; save to SD card or send to OctoPrint, Klipper (Moonraker) or PrusaLink |
| **Desktop app** | SolidWorks-style ribbon, FeatureManager tree, heads-up view toolbar, light and dark themes. Runs locally (Electron + Python); projects are plain JSON; STEP/STL export |

| Constraint sketcher | Drawing → CAD, traceable |
|---|---|
| ![Sketcher](docs/images/sketcher.png) | ![Drawing import](docs/images/drawing-import.png) |
| **Generated drawing with GD&T** | **Assembly with mates and BOM** |
| ![Drawing](docs/images/drawing-gdt.png) | ![Assembly](docs/images/assembly.png) |
| **Manufacturability and FEA** | **Sheet-metal flat pattern** |
| ![Analysis](docs/images/analysis.png) | ![Flat pattern](docs/images/flat-pattern.png) |

**Robot imported from URDF/xacro, joints posed with sliders** (try `examples/robot_arm/urdf/demo_arm.urdf.xacro`)

![Robot from URDF](docs/images/robot-urdf.png)

## Install (Linux)

Requirements: Python 3.12 (CadQuery doesn't support 3.14 yet), Node.js 20 or newer, and `libcairo2`.

```bash
git clone <this repository> ai-cad && cd ai-cad
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
(cd web && npm install) && (cd desktop && npm install)
./desktop.sh            # desktop app   (or ./run.sh -> http://127.0.0.1:8000 in a browser)
```

If your system lacks `python3.12-venv`, use `python3.12 -m venv --without-pip .venv` and then `curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python`.

**3D printing** needs the PrusaSlicer engine (AGPL-3.0; the app runs it as a separate program): `sudo apt install prusa-slicer`, or `flatpak install flathub com.prusa3d.PrusaSlicer`. Without admin rights, **Install engine** in the Print workspace unpacks Ubuntu/Debian's package for your user only.

**Self-contained packages:** `packaging/build_linux.sh` builds an AppImage and a `.deb` that include their own Python runtime. Windows and macOS builds aren't scripted yet.

## Choose your AI

Open **⚙** in the app:

| Option | Cost | Notes |
|---|---|---|
| **Claude Code: your Claude login** | included in your Claude plan | No key needed. Uses the Claude Code VS Code extension or CLI you're already signed in to |
| Claude API | pay per use | Strongest at reading drawings |
| Google Gemini | free tier | Reads images |
| Groq, OpenRouter (`:free` models) | free tiers | Usually text only |
| Ollama | free, runs locally | Needs a model with tool calling |
| OpenAI or any OpenAI-compatible API | pay per use | Set the base URL and model |

**Robots by name or link:** in the Assistant tab (Part or Assembly), say *"bring in the Hiwonder AiNex and raise its right arm"* or paste a GitHub link. With the Claude Code option it searches the web for the official repository. With other providers, give it the link. It downloads only the robot's folder to `~/Documents/AI-CAD-Downloads`, imports it, poses it, checks a rendered picture, and tells you the repository's licence. **From GitHub…** in the Assembly toolbar does the same by hand. Downloaded files are never run; xacro expressions that reach into Python internals are refused.

You can also drive the app **from Claude Code itself**: run `scripts/setup-claude-code.sh` (add `--global` to use it from any folder), then in Claude Code: *"Read ~/drawings/bracket.pdf and build it in AI CAD"*. Changes appear live in the app.

## Honest limits

- **AI accuracy:** the AI paths have so far been tested with scripted models and a few live runs, not yet benchmarked on large sets of real drawings. Always review the AI's assumptions and check dimensions before manufacturing.
- **Analysis scope:** linear static only. No fatigue, buckling, contact or thermal analysis. Material values are typical handbook figures.
- **Not yet supported:**
  - surfacing; parting-line and step drafts (draft uses a neutral plane); a variable fillet works on one edge (and its tangent chain) at a time;
  - Hole Wizard standards other than ISO metric (no ANSI inch, pipe threads or ISO 7380/7046 heads);
  - sheet-metal flanges on flanges, and hems on flanges (hems attach to base-flange edges only); closed hems keep a 0.02 mm gap;
  - assemblies have no motion study or dynamics (URDF joints can be posed, not simulated); meshes imported from URDF aren't parametric;
  - 3D printing is FDM only (no resin/SLA), single extruder (no multi-material or tool changers), without per-object settings, modifiers, paint-on supports or seams, and without variable layer height; G-code thumbnails for printer screens are not generated;
  - imported STL/STEP bodies are fixed shapes, not feature trees (for sheet metal and extrusions, **Rebuild as editable part** turns a mesh into features);
  - section views are full sections on the XY/YZ/XZ planes (no offset/aligned/half sections or detail views) and are not dimensioned.
- **Status:** see [docs/ROADMAP.md](docs/ROADMAP.md) for what's verified, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works.

## Tests

```bash
.venv/bin/python -m pytest -q tests     # unit, geometry and API tests
tests/run_all.sh                        # + Chrome UI suites and desktop self-test (run alone on low-RAM machines)
```

## Licence

AI CAD is free software under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)). You can use, change and share it. If you distribute it, or offer a modified version as a network service, you must share the source under the same licence.

**Commercial licences** are available for closed-source or proprietary use; see [COMMERCIAL.md](COMMERCIAL.md). Third-party components are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

Copyright (C) 2026 Heman

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Contributors sign a short [CLA](CLA.md) so the project can remain available under both the open-source and commercial licences.
