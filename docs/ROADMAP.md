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
- **2026-10-19 (2):**
  - **Interface:** command ribbon (Features, Sketch, Evaluate, Sheet Metal, Import / Export), FeatureManager tree with icons, heads-up view toolbar (standard views, display styles, selection filter), orientation triad, gradient viewport, hover pre-highlight, status bar, and a light/dark/system theme switch.
  - **Measure tool:** picks vertices, edges and faces (no Shift needed); shows distance, dX/dY/dZ, angle, centre distance, diameter, length, area or coordinates in a panel and as a dimension in the 3D view. "Dimensions" shows the overall part size in 3D. Backend: vertex references, closest-point pairs, and the angle between straight edges.
  - **Tests:** 65 unit/API tests and 5 UI suites pass (the smoke suite now covers the measure tool).
- **2026-10-20:**
  - **URDF import:** URDF and xacro files become an assembly. Box, cylinder and sphere links become parametric parts named by size, so the BOM counts shared parts. STL, OBJ, PLY and DAE meshes become solids: triangles are joined into a connected shell, coplanar faces are merged back into planar faces, COLLADA units are honoured, and meshes over 30k triangles are decimated. Joints (revolute, continuous, prismatic, fixed, mimic) are driven by forward kinematics with limits, from sliders, without undo steps. URDF masses are compared with the CAD volume. Missing meshes are reported, never guessed.
  - **Example:** `examples/robot_arm` (xacro, package:// meshes, a DAE in centimetres).
  - **Not supported:** floating and planar joints (imported as fixed), textures, dynamics.
  - **Tests:** `test_urdf.py` (6) and the `ui_urdf` suite.
- **2026-10-20 (2):**
  - **Real robot:** tested on Hiwonder AiNex, the official xacro with 25 STL meshes: 28 links, 24 movable joints, about 30 s to import, 0.5 s to open. Fixes along the way:
    - `$(find pkg)` now resolves without ROS;
    - fixed joints with a zero axis are accepted;
    - meshes are split into bodies (closed ones become solids, open patches become surfaces, and the report shows the volume as partial);
    - display triangles are saved next to each mesh solid, so opening is 100× faster;
    - flat shading, lifted dark colours, and a floor grid under the robot.
  - **Assistant:**
    - `get_from_github` (list, then download only the needed ROS packages; GitHub only; hooks and symlinks off; licence reported; Git LFS detected);
    - web search for the Claude Code provider;
    - assembly tools (`import_urdf`, `move_joints`, `get_assembly`) for the built-in assistant too;
    - `render_views(target="assembly")`: shaded 4-view pictures drawn on the server;
    - an Assistant tab in the Assembly workspace, with live sync.
  - **Verified end to end:** one chat message ("bring in the AiNex, raise its right arm, turn its head") made Claude Code find the repository, download 8 MB, import, pose and report the licence.
  - **Tests:** 87 unit/API tests and 6 UI suites pass.
- **2026-10-21 (part features):** Closed the main SolidWorks part-modelling gaps:
  - **Draft:** OCC `BRepOffsetAPI_DraftAngle` about a neutral plane (base plane or face) and a pull direction; ±45° limit; faces perpendicular to the pull, or that the kernel cannot taper, are named by position. A 40 × 40 × 20 block drafted 5° matches ∫(a − 2z·tan θ)² dz to 1e-9, and the measured face angle is 5.000°.
  - **Rib:** from an open sketch line or points; parallel to the sketch (filled in the plane up to the part, ends extended along the line) or normal to it (grown along the normal to the part); both/one-sided thickness. It fails clearly when it doesn't reach material. An L-bracket gusset adds exactly ½·55·35·4 mm³.
  - **Variable-radius fillet:** one edge, r1 → r2 plus optional intermediate radii (`BRepFilletAPI_MakeFillet` with a radius law); the end that gets r1 can be picked with a vertex. The measured radius at both ends and the middle matches to 0.01 mm.
  - **Hole Wizard:** ISO 273 clearance (close/normal/loose), tapped (ISO 2306 tap drills, coarse and fine pitches, cosmetic thread), counterbore for ISO 4762 (DIN 974-1 Ø, head-height depth), and 90° countersink for ISO 10642. Unknown sizes are rejected. Drawing callouts name the screw standard.
  - **Section views:** `sections` on `make_drawing`, `/api/drawing.*`, MCP `render_views`/`export_file` and the drawing dialog. Each is a full section on an XY/YZ/XZ plane (default through the middle) with 45° hatching of the cut faces (holes stay unhatched), a cutting-plane line with arrows and letters on the view that sees it edge-on, and placement that avoids the other views (the scale drops if needed).
  - **Sheet-metal hems:** open (gap) and closed hems on base-flange edges, collision-checked, unfolded in the flat pattern with BA = π·(R + K·t).
  - **Tests:** `test_features_more.py` (17), 104 unit/API tests pass. The Chrome UI suites weren't rerun for these dialogs yet.
- **2026-10-21 (assembly):**
  - **Assembly workspace, SolidWorks-style:** the same title bar, ribbon (Assembly, Evaluate, Import / Export), heads-up view toolbar, orientation triad and status bar as the Part workspace. These pieces are now shared components (`TitleBar`, `ViewHud`, `StatusBar` in `ui.tsx`). The FeatureManager tree shows components with fixed `(f)` / floating `(-)` prefixes, robot link names and hidden state, a Mates folder (select, edit, suppress, delete) and a Joints folder with the sliders. A PropertyManager tab hosts each command.
  - **Mates:** angle, perpendicular and lock mates; alignment flip; editing values; suppress and unsuppress. Prealign turns faces off the solver's saddle points, about the component's other mate axes where possible. First placement now moves a component only along constrained directions, so free directions keep where it was put.
  - **Components:**
    - Move and Rotate Component: drag in the 3D view, nudge buttons, or numeric input. Free components only; mates re-solve.
    - Linear and circular component patterns.
    - Show/hide, isolate and transparency.
  - **Exploded views:** steps (direction and distance) and auto-explode radially from the centre, saved in the `.aicadasm` file. They are drawn as display offsets, with an animated explode/collapse toggle.
  - **Evaluate:**
    - measure between faces, edges and vertices of components in world coordinates (`/api/asm/measure`);
    - mass properties: mass from materials, or URDF link masses, plus centre of mass (`/api/asm/mass`);
    - bill of materials with CSV export (`/api/asm/bom.csv`).
  - **MCP:** the `assembly` tool documents the new mate types and commands, plus `mass`, `bom_csv` and `measure`.
  - **Tests:** 95 unit/API tests pass, including 8 new solver tests that check solved poses numerically. The `ui_assembly`, `ui_urdf` and `ui_smoke` suites pass.
- **2026-10-21 (mesh → parametric parts):**
  - **Reverse engineering (`cadcore/reverse.py`):** a closed mesh becomes a parametric part only if the rebuilt part matches it.
    - **Sheet metal:** planar regions (all coplanar triangles, connected or not); flanges are face pairs one thickness apart (outline = union of both faces); narrow strips at an angle to every flange are dropped as low-poly bend facets; bend radius fitted to the corner vertices (inner and outer cylinder), sharp outer corners recognised; each flange is trimmed or extended exactly to its bend's tangent line, so flanges and bends always form one body (fixes the AiNex knee-bracket "2 separate bodies"). Holes are fitted as circles from the vertices of both faces, slots as slots.
    - **Prismatic (2.5D):** if every face is parallel or perpendicular to one axis, the part becomes stacked extrusions of its cross-sections with holes.
    - **Validation:** symmetric difference by casting 250k skew rays through both meshes (no booleans; OCC booleans misclassify solids whose faces lie on each other). Accepted below 5%. Anything else: "not convertible: <reason>".
    - **Flat pattern:** unfolds the flange/bend tree with BA = θ(R + K·T), K = 0.44; refuses closed loops of bends and overlapping flanges; DXF with bend lines.
    - Builder: `fuse_tol` on added bodies (fuzzy fuse for faces that meet exactly); `make_drawing` takes `extra_notes` and `source_note`.
  - **Parts package (`cadcore/parts.py`):** bodies split per component, put in their principal frame, grouped across the assembly (area/volume/spread, then surface distance with a position+normal check so a moved hole counts, under the 4 proper rotations with ICP; improper-only matches are mirror parts with their own number). Classification sheet_metal / purchased / custom with evidence and confidence, `overrides` by part number. Output `<name>_parts/`: BOM.csv, README.md, per part part.aicad (when rebuilt), part.step or part_mesh.step, part.stl, drawing.pdf, preview.png, flat.dxf.
  - **API and AI:** `POST /api/parts/package`, `POST /api/parts/rebuild`; MCP `make_parts_package`, `rebuild_part_from_mesh`; the same tools and workflow notes for the built-in assistant and the Claude Code prompt.
  - **Measured:** synthetic suite (`tests/meshgen.py`: plates with holes and slots, L/U/Z/hat brackets, 45–135°, R = 0–2T, T = 0.8–3 mm, 8-segment holes, sharp outer corners, random poses): 50 parts all recovered, thickness ±0.02, angles ±1°, radii ±0.15 mm, holes ±0.1 mm, mismatch below 1%. **AiNex (25 STL meshes, 236 bodies):** 89 distinct parts (9 mirror-image) in 2 min with drawings; 11 sheet-metal brackets rebuilt as parametric parts (mismatch 0.2–5.0%, knee bracket 1.9%; 8 with flat-pattern DXF), about 15 more bracket-like parts reported not convertible (hems, double walls, faces tilted up to 2°); servo housings and horns grouped and listed as purchased.
  - **Tests:** 218 unit/API tests pass.
  - **Limits:** no formed features, hems, curved flanges or bends over 6T; very sloppy meshes (faces tilted 2° to each other, hems, double walls) are reported as not convertible; bought components are recognised by repetition and size only.
  - **New tests:** `test_reverse.py` (101 cases), `test_parts.py` (5).

