# Roadmap and progress

Goal: a desktop mechanical CAD application with an AI design assistant. You describe a part; the assistant asks what it needs to know, then builds an editable parametric model. You and the assistant then change the same model, through chat or manual controls.

This is not SolidWorks: it handles single solid parts, sheet metal, and simple assemblies using the operations listed below. Anything outside that list is shown as unsupported.

Status key: **✅ verified** means implemented with an automated check · **🟡 implemented, partly verified** means it works but has a known gap in verification · **⛔ not supported**.

## Milestones

| | Milestone | Status |
|---|---|---|
| A | Audit and architecture | ✅ `docs/ARCHITECTURE.md` |
| B | Shared parametric feature model and regeneration | ✅ |
| C | Desktop workspace and manual editing | ✅ |
| D | Conversational AI on the same operations | 🟡 tested with a scripted model; **not yet run against the live API** (no key on the dev machine) |
| E | Sketches, constraints, more operations | ✅ includes splines, inclined planes, sweep, loft, threads |
| F | PDF/image interpretation with traceable dimensions | 🟡 plumbing and UI verified; **interpretation quality not yet measured** (needs an API key) |
| G | Technical drawings | ✅ includes tolerances, ISO 286 fits, ISO 2768, GD&T, surface finish |
| H | Sheet metal, assemblies | ✅ first versions (see limits below) |
| I | Engineering analysis | ✅ linear-static FEA validated against beam theory; DFM rule checks |
| J | Installer | 🟡 AppImage + .deb build script; see "Packaging" for its verification status |

## Capabilities and how they are verified

| Capability | Where | Verified by |
|---|---|---|
| Feature tree: extrude/cut, revolve, sweep, loft, hole (counterbore, countersink, tapped), thread (cosmetic or modelled), fillet, chamfer, shell, patterns, mirror, constraint sketch | `cadcore/builder.py` | `test_core.py`, `test_features_advanced.py` (hand-computed volumes: loft frustum exact, thread groove within 1%) |
| Sketch solver: lines, arcs, circles, splines; 13 constraint types, 6 dimension types, DOF and conflict reporting | `sketch.py`, `SketchEditor.tsx` | `test_sketch.py`, `ui_sketch.py` |
| Planes: base planes, any planar face (inclined faces included), custom origin/normal planes | `builder.py` | `test_inclined_face_and_custom_plane` |
| Variables, formulas, units, limits, cycle detection; transactions, undo/redo, batch, preview; dependency checks | `expr.py`, `project.py`, `deps.py` | `test_core.py` |
| Stable face/edge references; explicit errors when a reference is missing | `mesh.py` | tests + `ui_smoke.py` |
| Tolerances (± limits, ISO 286 fits for holes D–JS and shafts d–p, IT5–IT11, sizes 0–500 mm), ISO 2768 general tolerances, datums, all 14 GD&T characteristics with rule checks, surface finish | `tolerances.py`, `annotations.py` | `test_annotations.py` (10 fits against published tables) |
| 2D drawings: hidden-line views, first/third angle, iso, dimensions, hole/thread callouts, tolerances, GD&T frames, datums, finish, title block; PDF/SVG | `drawing.py` | `test_drawings.py`, `test_annotations.py`, `ui_drawing.py` |
| Drawing import with traceable dimensions | `source_drawing.py`, `Drawings.tsx` | `test_drawings.py`, `ui_drawing.py` |
| FEA: linear static, quadratic tetrahedra (Gmsh + SciPy), supports and loads on picked faces, von Mises stress, deflection, safety factor, force-balance check, mesh convergence | `fea.py`, `Analysis.tsx` | `test_analysis.py` (cantilever within 0.2% of theory, axial bar, pressure reactions), `ui_analysis.py` |
| DFM: CNC (wall thickness, sharp internal corners, setups/undercuts, holes), FDM (overhangs, walls), sheet metal (bend radius, flange length, hole-to-bend distance) | `dfm.py` | `test_analysis.py`, `test_sheetmetal.py` |
| Sheet metal: base flange, edge flanges (any angle, up/down, corner trims), collision check, flat pattern with bend allowance and base cut-outs, DXF/SVG | `sheetmetal.py` | `test_sheetmetal.py` (flat area × t equals the 3D volume exactly for K = 0.5) |
| Assemblies: components linked to part files, mates (coincident, distance, parallel, concentric) with a 6-DOF solver, DOF and conflict reporting, interference, BOM, STEP export, undo/redo | `assembly.py`, `Assembly.tsx` | `test_assembly.py`, `ui_assembly.py` |
| AI tools for all part features, annotations, sheet metal, DFM and FEA; designed to ask for loads rather than invent them | `assistant.py` | `test_assistant.py`, `test_demo.py`, `test_drawings.py` (scripted model) |
| Desktop app; packaged build | `desktop/`, `packaging/` | self-test (`AI_CAD_SELFTEST`) |

Run everything with `tests/run_all.sh`.

## Limits

- **Analysis:**
  - Linear static only: no fatigue, buckling, contact, large deformation, thermal or dynamic analysis.
  - Supports are rigid. Loads are uniform force or pressure on faces.
  - Material values are typical handbook figures.
- **Sheet metal:**
  - Flanges must be on edges of the base flange (no flanges on flanges).
  - The flat pattern includes holes and cuts through the base only. It refuses to compute, with a reason, when other features are present.
  - There are no hems, jogs, louvres or forming tools.
- **Assemblies:**
  - Rigid components only; no sub-assemblies and no motion study.
  - Mate types: coincident, distance, parallel, concentric.
  - The AI can't edit assemblies yet.
- **Threads:**
  - ISO metric only.
  - Modelled threads are V-grooves (60° flanks) for visualisation and mass; they are not certified thread forms.
- **Tolerances:** hole classes K, M, N and P aren't supported (they need the ISO 286 Δ correction). GD&T placement on drawings is automatic and may need manual tidying in a CAD drawing tool.
- **Not available:** draft and rib features, variable fillets, surfacing, cosmetic thread views on drawings, section views.

## Packaging

`packaging/build_linux.sh` builds `desktop/dist/AI CAD-<version>.AppImage` and a `.deb`. They bundle Electron, the UI, a relocatable CPython 3.12 (python-build-standalone) and all CAD libraries.

Runtime needs:
- glibc 2.28 or newer;
- libcairo2;
- OpenGL;
- for the AppImage only, FUSE 2 (or run it with `--appimage-extract-and-run`).

Windows and macOS installers are not built. The code is portable, but the scripts are Linux-only.

## Next steps

1. **Live AI validation:** run the demonstration and a drawing benchmark against the real API, record failures, and tune the prompt and tools.
2. **AI tools for assemblies:** add, mate, check interference.
3. **Section views and user-placed dimensions on drawings.**
4. **Sheet metal:** flanges on flanges, hems, flat-pattern drawings.
5. **Analysis:** modal (natural frequency) and buckling analysis, and contact between assembly parts.

## AI connections

| Path | Status |
|---|---|
| In-app assistant with Claude (Anthropic API) | 🟡 tested with a scripted model |
| In-app assistant with any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter, Ollama, custom) | 🟡 tested with a scripted model; real providers not yet exercised |
| Claude Code (your own login) through the MCP server, with live updates in the app | ✅ protocol tested end to end with an MCP client against a running app |

## Progress notes

- **2026-10-18 (1):** v1 prototype → v2 feature tree, Electron desktop app, sketch solver, AI assistant, drawing import, drawing generation. 28 tests.
- **2026-10-18 (2):** Closed the "not supported" list:
  - **Modelling:** splines, inclined and custom planes, sweep, loft, threads. Helical thread booleans in OCC are fragile, so the thread feature checks the removed volume and retries; it never returns a silently wrong thread.
  - **Tolerances and GD&T:** on the model and on drawings.
  - **FEA:** checked against beam theory.
  - **DFM:** CNC, FDM and sheet metal.
  - **Sheet metal:** with flat pattern and DXF.
  - **Assemblies:** with a mate solver. Faces that must flip 180° are pre-aligned to avoid a Gauss–Newton saddle point.
  - **Packaging.**
  - **Tests:** 60 unit/API tests plus 5 Chrome UI suites.
- **2026-10-19:**
  - **Crash cause:** the system crashes were the out-of-memory killer during `.deb` packaging (7za reached 6.7 GB). The `.deb` is now built with `dpkg-deb`, peaking at 8 MB, and swap is 8 GB.
  - **Sketcher:** added a fixed origin, hover highlighting, Esc to switch to Select, Shift-click selection, automatic point-on-line/circle constraints, and pruning of orphan points. Fixed a bug where Esc never reached the sketcher: the app's key handler re-rendered it between listeners.
  - **AI:** the in-app assistant supports any OpenAI-compatible provider. Added the MCP server for Claude Code, live sync (`/api/version`), and selection sharing.
  - **Tests:** 64 unit/API tests and 5 UI suites pass.
  - **Packaging:** the packaged AppImage/.deb from 2026-10-18 doesn't contain these changes yet; it needs a rebuild.
