# Architecture

## A. Audit of the starting point (before the v2 rebuild)

The repository started as a prototype:

| Area | What existed | Gap against the desktop-CAD goal |
|---|---|---|
| Model | `PartSpec` v1: one fixed base shape (plate / L-bracket / shaft) plus holes and pockets | No feature tree, no sketches, no formulas, no dependencies, no references to faces or edges |
| Kernel | CadQuery 2.8 on OpenCascade, booleans built by hand | Suitable; kept |
| Commands | `Project.execute`: transactional rebuild, undo/redo | Good pattern; kept and extended |
| UI | Browser page: parameter list, Three.js view, chat | Selected features only; no face or edge picking; no standard views; blank page without WebGL |
| AI | Claude tool loop with 10 tools | No selection context, no streaming, no design intent, no alternatives |
| Desktop | None (plan said Tauri; Rust not installed) | — |
| Files | v1 JSON (`bracket.json`, `plate.json`) | Need explicit migration |

## Current architecture (v2)

```
┌──────────────── Electron window (desktop/main.js) ────────────────┐
│  React UI (web/src)                                               │
│   feature tree · variables · property editor · feature dialogs    │
│   Three.js viewport (feature/face/edge picking, std views)        │
│   chat (SSE stream) · settings                                    │
└──────────────┬──────────────────────────────────────────────────────┘
               │ HTTP on 127.0.0.1:<random port>  (preload exposes only file dialogs)
┌──────────────▼───────────── Python backend process (server/app.py) ┐
│  FastAPI; one lock serialises model operations; worker threads     │
│  ┌──────────────┐   ┌────────────────────────────────────────────┐ │
│  │ Assistant     │──▶│ Project.execute / execute_batch / preview  │ │
│  │ (Claude tools)│   │  candidate spec → build → commit or reject │ │
│  └──────────────┘   └─────────────┬──────────────────────────────┘ │
│                                    ▼                               │
│   builder.py (feature tree → OCC solid)   mesh.py (entities, refs) │
│   expr.py (formulas, units)  deps.py (dependencies, diffs)         │
│   sketch.py (constraint solver)  threads.py  sheetmetal.py         │
│   annotations.py + tolerances.py (ISO 286/2768, GD&T)              │
│   drawing.py (HLR views, dims, frames)  source_drawing.py (import) │
│   fea.py (Gmsh tet10 + SciPy)  dfm.py (manufacturing rules)        │
│   assembly.py (components, mate solver, interference, BOM)         │
└────────────────────────────────────────────────────────────────────┘

Packaged: Electron + resources/app (cadcore, server, web/dist) + resources/python
(standalone CPython 3.12 with all dependencies), built by packaging/build_linux.sh.
```

### Shared parametric model (`cadcore/spec.py`)
`PartSpec` v2 has these parts:
- `name`
- `variables`: numbers, formulas like `"=length/2"`, or values with units like `"2 in"`
- `limits`: allowed range for each variable
- `features`: an ordered tree of `{id, type, params, suppressed}`
- `material`
- `intent`: requirements vs assumptions
- `source`: drawing analysis and migration notes

The spec is the single source of truth: geometry is always regenerated from it, deterministically.

### Regeneration (`cadcore/builder.py`)
Variables are evaluated in dependency order; cycles and unknown names fail with an explicit message. Features build in order:
- **Tool features** add or remove a body: `extrude`, `revolve`, `hole` (also ISO Hole Wizard sizes, `cadcore/holes.py`), `rib`, `pattern_linear`, `pattern_circular`, `mirror`, `sketch`-based extrude and revolve. Their bodies are recorded so faces can be traced back to them.
- **Modifiers** change the existing solid: `fillet`, `variable_fillet`, `chamfer`, `shell`, `draft`. The faces they create are recorded.

Each feature is validated: positive sizes, holes must not break out of their face, the result must be a single valid solid, and references must still resolve. Any failure raises `BuildError`; `Project` then discards the candidate spec and keeps the last valid model.

### Transactions (`cadcore/project.py`)
- `execute(command)`: a single change.
- `execute_batch(commands)`: several changes as one transaction and one undo step, used for accepted alternatives.
- `preview(commands)`: evaluates changes without committing.
- Undo and redo store whole spec snapshots.

### Entity mapping and references (`cadcore/mesh.py`)
Every displayed face and edge carries its owning feature (traced from tool surfaces and modifier-created faces) and a geometric signature: type, a point on it, normal or axis, and area or length. A selection is stored as that signature. After any change it is re-resolved and reported as `ok`, `changed`, `missing` or `ambiguous`. It is never silently swapped for another entity.

Features can reference geometry two ways, and both fail loudly if the referenced entity disappears:
- `face_at: {point, normal}` for sketch and hole planes;
- `edges: {near: [points]}` for fillets and chamfers.

### AI (`cadcore/assistant.py`)
- Uses Claude through the official Anthropic SDK. Model, effort and base URL are configurable; the key lives in `~/.config/ai-cad/config.json` (mode 600) or `ANTHROPIC_API_KEY`.
- Tools are validated JSON-schema commands that map 1:1 onto `Project` commands. There is no code execution.
- Each user turn carries the current model state and the user's selection, so the AI always sees manual edits.
- Tool calls run sequentially. After a failed modelling step, the remaining steps in that batch are skipped and the error goes back to the model.
- Tools for alternatives produce previews; accepting one applies it as a single transaction.

### Desktop (`desktop/`)
Electron replaces the earlier Tauri plan: Rust isn't installed, and Electron's bundled Chromium has working GPU/WebGL here, where the system Chrome had acceleration turned off.
- The shell starts the backend on a free localhost port. The backend exits if the shell dies.
- The preload script exposes only the native open, save and export dialogs.

### Analysis (`cadcore/fea.py`, `cadcore/dfm.py`)
- **FEA:**
  - The solid is meshed by Gmsh into 10-node tetrahedra.
  - Supports and loads are located on the exact B-rep face first, then mapped to the matching Gmsh surface by centre of mass and area.
  - The stiffness matrix is assembled in vectorised NumPy and solved with SciPy's sparse direct solver.
  - Stresses are averaged at the nodes.
  - Results carry the force-balance error, an optional mesh-convergence comparison and the stated assumptions.
- **DFM:** ray casting (OCC `IntCurvesFace_ShapeIntersector`) for wall thickness and tool access, edge classification for sharp internal corners, and rule thresholds that are returned with every finding.

### Sheet metal (`cadcore/sheetmetal.py`)
A part becomes sheet metal when `spec.sheet` (thickness, bend radius, K-factor) is set.
- **Bends:** revolving the edge's end-face rectangle about the bend axis gives the exact bend region; the flat flange is extruded from the rotated end face.
- **Flat pattern:** built analytically from the base profile, plus BA + L strips per flange, minus through-cuts in the base.
- **Refusals:** any feature it can't unfold makes it refuse, with the reason, rather than approximate.

### Assemblies (`cadcore/assembly.py`)
- **Documents:** a separate document type (`.aicadasm`) that references part files, so edits to a part propagate. Parts are cached by content hash.
- **Mate solver:** unknowns are a translation and a rotation vector per free component. Before Gauss–Newton runs, a pre-alignment pass orients and places components, because a face needing a 180° flip is a saddle point for the solver.
- **Interference:** pairwise boolean intersection, after a bounding-box test.

### Mesh → parametric parts (`cadcore/reverse.py`, `cadcore/parts.py`)
- **Rebuild:** sheet metal (face pairs one thickness apart → flanges, corner vertices → bends, outlines trimmed to the bend tangent lines so the bodies meet exactly) or 2.5D extrusions; the result is a normal feature tree.
- **Acceptance:** symmetric difference with the mesh by ray casting (OCC booleans are unreliable when the faces lie on each other); above 5% the part stays a mesh and the reason is reported.
- **Parts package:** bodies grouped by shape (principal frame, surface distance, mirror detection), classified with evidence, written as BOM + per-part STEP/STL/drawing/flat pattern.

### Known architectural limits
- Topological naming uses geometric signatures, not OCC history. A reference survives changes that keep its face or edge in place, and is reported missing otherwise.
- The local HTTP API has no authentication. It listens only on 127.0.0.1, but any local process could call it.
- Undo history is per session and not saved in the project file.
