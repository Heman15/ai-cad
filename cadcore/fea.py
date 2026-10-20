# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linear-static finite element analysis of a single solid part.

Method:
- Mesh: Gmsh, 10-node quadratic tetrahedra.
- Assembly: isotropic linear elasticity, 4-point Gauss rule (exact for the quadratic integrand).
- Solve: SciPy sparse direct solver.
- Stress: element stresses averaged at the nodes, reported as von Mises.

Units: mm, N, MPa.

Assumptions, which are returned with every result:
- linear elastic material;
- small deformations;
- static loads;
- perfectly rigid supports on the selected faces;
- loads spread uniformly over the selected faces.

Stress at sharp re-entrant corners or edge-like supports is mesh-dependent (singular), so it should be judged with a convergence check.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

# 10-node tetrahedron: corners 0-3, then mid-side nodes on these corner pairs (Gmsh order, verified at runtime)
EDGES = [(0, 1), (1, 2), (0, 2), (0, 3), (2, 3), (1, 3)]
_A, _B = 0.5854101966249685, 0.1381966011250105
GAUSS = np.array([[_A, _B, _B, _B], [_B, _A, _B, _B], [_B, _B, _A, _B], [_B, _B, _B, _A]])  # volume coords
NODE_L = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]] +
                  [[0.5 if k in e else 0.0 for k in range(4)] for e in EDGES], dtype=float)
# d(L1..L4)/d(xi, eta, zeta) with L1 = 1 - xi - eta - zeta
DL = np.array([[-1, -1, -1], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)


class FEAError(Exception):
    pass


def _dN_dL(L: np.ndarray) -> np.ndarray:
    """(10, 4) derivatives of the shape functions w.r.t. volume coordinates at point L."""
    d = np.zeros((10, 4))
    for i in range(4):
        d[i, i] = 4 * L[i] - 1
    for k, (i, j) in enumerate(EDGES):
        d[4 + k, i] = 4 * L[j]
        d[4 + k, j] = 4 * L[i]
    return d


def _elasticity(E: float, nu: float) -> np.ndarray:
    lam = E * nu / ((1 + nu) * (1 - 2 * nu))
    mu = E / (2 * (1 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[np.arange(3), np.arange(3)] += 2 * mu
    D[np.arange(3, 6), np.arange(3, 6)] = mu
    return D


def _B(dNdx: np.ndarray) -> np.ndarray:
    """dNdx (E, 10, 3) -> strain-displacement matrices (E, 6, 30), engineering shear strains."""
    n = dNdx.shape[0]
    B = np.zeros((n, 6, 30))
    for a in range(10):
        dx, dy, dz = dNdx[:, a, 0], dNdx[:, a, 1], dNdx[:, a, 2]
        c = 3 * a
        B[:, 0, c], B[:, 1, c + 1], B[:, 2, c + 2] = dx, dy, dz
        B[:, 3, c], B[:, 3, c + 1] = dy, dx
        B[:, 4, c + 1], B[:, 4, c + 2] = dz, dy
        B[:, 5, c], B[:, 5, c + 2] = dz, dx
    return B


@dataclass
class Mesh:
    nodes: np.ndarray  # (N, 3)
    tets: np.ndarray  # (E, 10) node indices
    surfaces: dict[int, np.ndarray]  # gmsh surface tag -> (T, 6) tri6 node indices (geometry stays loaded in gmsh)


def mesh_solid(shape, size: float) -> Mesh:
    import gmsh

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "part.brep")
    shape.exportBrep(path)
    if not gmsh.isInitialized():
        gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.clear()
    try:
        gmsh.model.add("part")
        gmsh.model.occ.importShapes(path)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", size / 5)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        gmsh.option.setNumber("Mesh.HighOrderOptimize", 1)
        gmsh.model.mesh.generate(3)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        X = coords.reshape(-1, 3)
        index = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        index[tags.astype(np.int64)] = np.arange(len(tags))
        types, _, enodes = gmsh.model.mesh.getElements(3)
        if 11 not in list(types):
            raise FEAError("meshing produced no quadratic tetrahedra")
        tets = index[enodes[list(types).index(11)].astype(np.int64)].reshape(-1, 10)
        surfaces = {}
        for _, tag in gmsh.model.getEntities(2):
            st, _, sn = gmsh.model.mesh.getElements(2, tag)
            if 9 in list(st):
                surfaces[tag] = index[sn[list(st).index(9)].astype(np.int64)].reshape(-1, 6)
        return Mesh(X, tets, surfaces)
    except FEAError:
        raise
    except Exception as e:
        raise FEAError(f"meshing failed: {e}")


def match_surfaces(ref: dict[str, Any], shape=None, tol: float = 0.05) -> list[int]:
    """Gmsh surface tags for a face reference {point, normal}.

    The face is found exactly on the B-rep (point on the trimmed face, matching normal), then mapped to
    the Gmsh surface with the same centre of mass and area (Gmsh meshed the same B-rep, faces map 1:1).
    """
    import cadquery as cq
    import gmsh

    p = np.array(ref["point"], dtype=float)
    v = cq.Vertex.makeVertex(*p.tolist())
    target = None
    for f in shape.Faces():
        if f.distance(v) > tol:
            continue
        if ref.get("normal") is not None and f.geomType() == "PLANE":
            if f.normalAt(cq.Vector(*p)).normalized().dot(cq.Vector(*ref["normal"]).normalized()) < 0.999:
                continue
        target = f
        break
    if target is None:
        return []
    c, a = np.array(target.Center().toTuple()), target.Area()
    best, best_err = None, 1e9
    for _, tag in gmsh.model.getEntities(2):
        err = np.linalg.norm(np.array(gmsh.model.occ.getCenterOfMass(2, tag)) - c) + abs(gmsh.model.occ.getMass(2, tag) - a) / max(a, 1e-9)
        if err < best_err:
            best, best_err = tag, err
    return [best] if best is not None and best_err < 1e-3 * max(1.0, np.abs(c).max()) else []


def _quality_check(nodes: np.ndarray, tets: np.ndarray) -> None:
    for k, (i, j) in enumerate(EDGES):
        if np.abs(nodes[tets[:, 4 + k]] - (nodes[tets[:, i]] + nodes[tets[:, j]]) / 2).max() > 0.25 * np.abs(nodes).max():
            raise FEAError("unexpected tetrahedron node ordering from the mesher")


def solve_static(mesh: Mesh, E: float, nu: float, fixed_nodes: np.ndarray, loads: np.ndarray) -> dict[str, np.ndarray]:
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve

    X, T = mesh.nodes, mesh.tets
    n_el, n_dof = len(T), 3 * len(X)
    D = _elasticity(E, nu)
    Xe = X[T]  # (E, 10, 3)
    Ke = np.zeros((n_el, 30, 30))
    for L in GAUSS:
        dNdxi = _dN_dL(L) @ DL  # (10, 3)
        J = np.einsum("eai,aj->eij", Xe, dNdxi)  # (E, 3, 3): d x_i / d xi_j
        det = np.linalg.det(J)
        if np.any(det <= 0):
            raise FEAError("mesh has inverted elements; try a different mesh size")
        dNdx = np.einsum("aj,eji->eai", dNdxi, np.linalg.inv(J))
        B = _B(dNdx)
        Ke += np.einsum("eki,kl,elj->eij", B, D, B) * (det / 24.0)[:, None, None]
    dofs = (3 * T[:, :, None] + np.arange(3)).reshape(n_el, 30)
    K = coo_matrix((Ke.ravel(), (np.repeat(dofs, 30, axis=1).ravel(), np.tile(dofs, (1, 30)).ravel())), shape=(n_dof, n_dof)).tocsr()

    fixed = np.zeros(n_dof, dtype=bool)
    fixed[(3 * fixed_nodes[:, None] + np.arange(3)).ravel()] = True
    free = ~fixed
    u = np.zeros(n_dof)
    u[free] = spsolve(K[free][:, free].tocsc(), loads[free])
    reactions = K @ u - loads

    # nodal-averaged stresses
    stress_sum = np.zeros((len(X), 6))
    count = np.zeros(len(X))
    ue = u[dofs]  # (E, 30)
    for a in range(10):
        dNdxi = _dN_dL(NODE_L[a]) @ DL
        J = np.einsum("eai,aj->eij", Xe, dNdxi)
        dNdx = np.einsum("aj,eji->eai", dNdxi, np.linalg.inv(J))
        sig = np.einsum("kl,eli,ei->ek", D, _B(dNdx), ue)
        np.add.at(stress_sum, T[:, a], sig)
        np.add.at(count, T[:, a], 1)
    s = stress_sum / np.maximum(count, 1)[:, None]
    vm = np.sqrt(0.5 * ((s[:, 0] - s[:, 1]) ** 2 + (s[:, 1] - s[:, 2]) ** 2 + (s[:, 2] - s[:, 0]) ** 2)
                 + 3 * (s[:, 3] ** 2 + s[:, 4] ** 2 + s[:, 5] ** 2))
    return {"u": u.reshape(-1, 3), "stress": s, "von_mises": vm, "reactions": reactions.reshape(-1, 3), "fixed": fixed.reshape(-1, 3)[:, 0]}


def _tri6_area(X: np.ndarray, tri: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, b, c = X[tri[:, 0]], X[tri[:, 1]], X[tri[:, 2]]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    return area, cr / np.maximum(2 * area, 1e-30)[:, None]


def analyse(shape, material: dict[str, float], supports: list[dict[str, Any]], loads: list[dict[str, Any]],
            mesh_size: float | None = None, check_convergence: bool = False) -> dict[str, Any]:
    """Run a static analysis. supports: [{"face": {point, normal}}]; loads: [{"face": ref, "force": [Fx,Fy,Fz]} | {"face": ref, "pressure": MPa}]."""
    import gmsh

    if not supports:
        raise FEAError("at least one fixed support face is required")
    if not loads:
        raise FEAError("at least one load is required")
    E, nu = float(material["E"]), float(material["nu"])
    if not (E > 0 and 0 <= nu < 0.5):
        raise FEAError("Young's modulus must be positive and Poisson's ratio in [0, 0.5)")
    bb = shape.BoundingBox()
    size = mesh_size or max(min(bb.xlen, bb.ylen, bb.zlen) / 2.5, max(bb.xlen, bb.ylen, bb.zlen) / 30)

    def run(h: float) -> dict[str, Any]:
        t0 = time.time()
        mesh = mesh_solid(shape, h)
        try:
            _quality_check(mesh.nodes, mesh.tets)
            fixed_nodes, F = set(), np.zeros(3 * len(mesh.nodes))
            for i, s in enumerate(supports):
                tags = match_surfaces(s["face"], shape)
                if not tags:
                    raise FEAError(f"support {i + 1}: the referenced face was not found on the current part; re-select it")
                for t in tags:
                    fixed_nodes.update(mesh.surfaces[t].ravel().tolist())
            applied = np.zeros(3)
            for i, ld in enumerate(loads):
                tags = match_surfaces(ld["face"], shape)
                if not tags:
                    raise FEAError(f"load {i + 1}: the referenced face was not found on the current part; re-select it")
                tris = np.vstack([mesh.surfaces[t] for t in tags])
                area, normal = _tri6_area(mesh.nodes, tris)
                if "force" in ld:
                    f = np.array(ld["force"], dtype=float)
                    tri_force = (area / area.sum())[:, None] * f[None, :]
                elif "pressure" in ld:  # positive pressure pushes into the surface
                    tri_force = -float(ld["pressure"]) * area[:, None] * normal
                else:
                    raise FEAError(f"load {i + 1} needs force [Fx,Fy,Fz] (N) or pressure (MPa)")
                applied += tri_force.sum(axis=0)
                # consistent nodal loads for a uniform traction on a 6-node triangle: corners 0, mid-sides 1/3 each
                for k in (3, 4, 5):
                    np.add.at(F, (3 * tris[:, k][:, None] + np.arange(3)).ravel(), (tri_force / 3).ravel())
            fixed_arr = np.array(sorted(fixed_nodes), dtype=np.int64)
            if len(fixed_arr) < 3:
                raise FEAError("the support faces have too few mesh nodes; use a finer mesh")
            r = solve_static(mesh, E, nu, fixed_arr, F)
            surf_tris = np.vstack(list(mesh.surfaces.values()))
        finally:
            gmsh.clear()
        disp = np.linalg.norm(r["u"], axis=1)
        reaction = r["reactions"][r["fixed"]].sum(axis=0)
        return {"mesh": mesh, "r": r, "disp": disp, "applied": applied, "reaction": reaction, "surf": surf_tris,
                "time": time.time() - t0, "size": h}

    main = run(size)
    r, disp, vm = main["r"], main["disp"], main["r"]["von_mises"]
    i_vm, i_d = int(np.argmax(vm)), int(np.argmax(disp))
    X = main["mesh"].nodes
    out: dict[str, Any] = {
        "max_von_mises_mpa": float(vm[i_vm]), "max_von_mises_at": X[i_vm].tolist(),
        "max_displacement_mm": float(disp[i_d]), "max_displacement_at": X[i_d].tolist(),
        "applied_force_n": main["applied"].tolist(), "reaction_force_n": main["reaction"].tolist(),
        "equilibrium_error": float(np.linalg.norm(main["applied"] + main["reaction"]) / max(np.linalg.norm(main["applied"]), 1e-12)),
        "nodes": int(len(X)), "elements": int(len(main["mesh"].tets)), "mesh_size_mm": float(size), "solve_s": round(main["time"], 2),
        "material": material,
        "assumptions": [
            "Linear elastic material, small deformations, static loads.",
            "Supports are perfectly rigid; loads are spread uniformly over the selected faces.",
            "Stresses at sharp internal corners or at the edge of a support can be singular: they keep rising as the mesh is refined.",
            "Material properties are typical values; check them against your material's datasheet.",
        ],
    }
    if material.get("yield"):
        out["yield_mpa"] = float(material["yield"])
        out["safety_factor"] = float(material["yield"]) / max(float(vm[i_vm]), 1e-12)
    if check_convergence:
        fine = run(size / 1.6)
        out["convergence"] = {
            "fine_mesh_size_mm": float(fine["size"]), "fine_elements": int(len(fine["mesh"].tets)),
            "max_displacement_change_pct": float(100 * abs(fine["disp"].max() - disp.max()) / max(disp.max(), 1e-12)),
            "max_von_mises_change_pct": float(100 * abs(fine["r"]["von_mises"].max() - vm.max()) / max(vm.max(), 1e-12)),
        }
    # display data: surface triangles split into 4 linear triangles each, with nodal results
    surf = main["surf"]
    sub = np.vstack([surf[:, [0, 3, 5]], surf[:, [3, 1, 4]], surf[:, [5, 4, 2]], surf[:, [3, 4, 5]]])
    used = np.unique(sub)
    remap = np.full(len(X), -1)
    remap[used] = np.arange(len(used))
    out["display"] = {
        "vertices": np.round(X[used], 5).ravel().tolist(),
        "triangles": remap[sub].ravel().tolist(),
        "von_mises": np.round(vm[used], 4).tolist(),
        "displacement": np.round(r["u"][used], 7).ravel().tolist(),
    }
    return out
