# Third-party software

AI CAD is distributed under the GNU AGPL v3 (see `LICENSE`). It uses the following libraries, each under its own licence; all are compatible with AGPL v3 distribution. Versions are those pinned in `requirements.txt` / `package.json` at the time of writing.

| Component | Version | Licence | Used for |
|---|---|---|---|
| CadQuery | 2.8.0 | Apache-2.0 | parametric modelling API |
| cadquery-ocp (OCP bindings) | 7.9.3 | Apache-2.0 (bindings) | Python bindings to OpenCascade |
| Open CASCADE Technology (bundled in cadquery-ocp) | 7.9 | LGPL-2.1 with Open CASCADE exception | geometry kernel (B-rep, booleans, HLR) |
| Gmsh | 4.15.2 | GPL-2.0-or-later | finite-element meshing |
| SciPy | 1.18.1 | BSD-3-Clause | sparse solver (FEA); nearest-point search (part grouping) |
| NumPy | 2.5.4 | BSD-3-Clause and others | numerics |
| ezdxf | 1.4.5 | MIT | DXF export (flat patterns) |
| CairoSVG | 2.9.1 | LGPL-3.0-or-later | SVG → PDF/PNG for drawings |
| pypdfium2 (PDFium) | 5.14.0 | BSD-3-Clause / Apache-2.0 | rendering imported PDF drawings |
| Pillow | 13.0.0 | MIT-CMU | image handling |
| trimesh | 5.1.1 | MIT | reading robot meshes (STL, OBJ, PLY, DAE) |
| pycollada | 0.9.3 | BSD-3-Clause | COLLADA (.dae) meshes |
| xacro | 2.1.1 | BSD-3-Clause | expanding xacro robot descriptions |
| NetworkX | 3.7 | BSD-3-Clause | mesh hole repair (via trimesh); flange/bend graphs (mesh → part) |
| Shapely (GEOS) | 2.2.0 | BSD-3-Clause (GEOS: LGPL-2.1) | 2D outlines when rebuilding parts from meshes |
| fast-simplification | 0.2.0 | MIT | reducing very large meshes |
| lxml | 6.1.3 | BSD-3-Clause | reading 3MF files (via trimesh) |
| FastAPI | 0.146.0 | MIT | local HTTP API |
| Uvicorn | 0.54.0 | BSD-3-Clause | HTTP server |
| Pydantic | 2.14.1 | MIT | request validation |
| httpx | 0.28.1 | BSD-3-Clause | HTTP client (MCP server) |
| MCP Python SDK | 2.3.0 | MIT | Claude Code connector |
| Anthropic Python SDK | 1.15.0 | MIT | Claude API provider |
| OpenAI Python SDK | 3.35.1 | Apache-2.0 | OpenAI-compatible providers |
| React / React DOM | 19.x | MIT | user interface |
| three.js | 0.171.0 | MIT | 3D viewport |
| Lucide (lucide-react) | 0.468.0 | ISC | interface icons |
| Electron | 33.2.1 | MIT (bundles Chromium, BSD-style and others) | desktop shell |

**PrusaSlicer** (AGPL-3.0-or-later, Prusa Research, based on Slic3r) is the slicing engine of the Print workspace, including its printer, print and filament profile bundles. It is not bundled: the app finds an installed copy (the distribution package, Flathub, or one unpacked by **Install engine** into `~/.local/share/ai-cad/slicer-root`) and runs it as a separate program through its command line. The `.deb` recommends the `prusa-slicer` package.

The packaged desktop builds additionally include CPython from python-build-standalone (PSF licence and component licences). Full licence texts are shipped inside each package (`site-packages/*/LICENSE*`, `node_modules/*/LICENSE*`).
