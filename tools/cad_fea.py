"""
Dual-Tier 3D B-Rep Solid FEA & FDM Layer-Delamination Engine (`tools/cad_fea.py`)
=================================================================================
Meshes any `.step` solid into 3D tetrahedra via `Gmsh` (OpenCASCADE kernel) and solves:
- **Tier 1 (`backend="skfem"`, Default):** Fast in-process quadratic tetrahedral (`ElementTetP2`)
  3D linear elasticity via `scikit-fem` (<1–2s), computing full 3D Cauchy stress tensors,
  peak & 99th-percentile Von Mises stress, nodal displacement, isotropic yield safety factor,
  **and anisotropic 3D-printed FDM interlayer delamination safety factor** along `print_axis`,
  plus an annotated 3D stress heatmap PNG.
- **Tier 2 (`backend="calculix"`):** Generates standard Abaqus/CalculiX `.inp` decks
  (`*STATIC` and optional `*BUCKLE` eigenvalue buckling) and executes local `ccx.exe`
  (auto-detected from `C:\\Program Files\\FreeCAD 1.0\\bin\\ccx.exe` or `PATH`).

Consistent Engineering Units:
- Length: `mm` | Force: `N` | Stress / Modulus: `MPa` (`N/mm^2`) | Moment: `N*mm`
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Material library in consistent (mm, N, MPa) units:
# z_tensile_mpa & z_shear_mpa capture FDM interlayer bond knockdown vs XY strand yield
MATERIAL_LIBRARY: Dict[str, Dict[str, Any]] = {
    "PLA": {
        "E_mpa": 3100.0,
        "nu": 0.35,
        "density_kg_m3": 1240.0,
        "yield_mpa": 50.0,
        "is_fdm": True,
        "z_tensile_mpa": 22.0,
        "z_shear_mpa": 18.0,
    },
    "PLA-CF": {
        "E_mpa": 4800.0,
        "nu": 0.33,
        "density_kg_m3": 1290.0,
        "yield_mpa": 58.0,
        "is_fdm": True,
        "z_tensile_mpa": 20.0,
        "z_shear_mpa": 17.0,
    },
    "PETG": {
        "E_mpa": 2100.0,
        "nu": 0.37,
        "density_kg_m3": 1270.0,
        "yield_mpa": 45.0,
        "is_fdm": True,
        "z_tensile_mpa": 24.0,
        "z_shear_mpa": 20.0,
    },
    "PAHT-CF": {
        "E_mpa": 6500.0,
        "nu": 0.32,
        "density_kg_m3": 1360.0,
        "yield_mpa": 95.0,
        "is_fdm": True,
        "z_tensile_mpa": 34.0,
        "z_shear_mpa": 26.0,
    },
    "PC": {
        "E_mpa": 2400.0,
        "nu": 0.37,
        "density_kg_m3": 1200.0,
        "yield_mpa": 62.0,
        "is_fdm": True,
        "z_tensile_mpa": 28.0,
        "z_shear_mpa": 22.0,
    },
    "TPU-95A": {
        "E_mpa": 45.0,
        "nu": 0.45,
        "density_kg_m3": 1210.0,
        "yield_mpa": 18.0,
        "is_fdm": True,
        "z_tensile_mpa": 14.0,
        "z_shear_mpa": 11.0,
    },
    "6061-T6": {
        "E_mpa": 68900.0,
        "nu": 0.33,
        "density_kg_m3": 2700.0,
        "yield_mpa": 276.0,
        "is_fdm": False,
    },
    "7075-T6": {
        "E_mpa": 71700.0,
        "nu": 0.33,
        "density_kg_m3": 2810.0,
        "yield_mpa": 503.0,
        "is_fdm": False,
    },
    "304-STEEL": {
        "E_mpa": 193000.0,
        "nu": 0.29,
        "density_kg_m3": 8000.0,
        "yield_mpa": 215.0,
        "is_fdm": False,
    },
    "POLYCARBONATE-SHEET": {
        "E_mpa": 2380.0,
        "nu": 0.37,
        "density_kg_m3": 1200.0,
        "yield_mpa": 65.0,
        "is_fdm": False,
    },
}


def find_calculix_ccx() -> Optional[str]:
    """Locate `ccx.exe` on Windows (including FreeCAD 1.0's bundled FEM solver) or PATH."""
    candidates = [
        os.environ.get("CALCULIX_CCX", ""),
        r"C:\Program Files\FreeCAD 1.0\bin\ccx.exe",
        r"C:\Program Files\FreeCAD 1.1\bin\ccx.exe",
        shutil.which("ccx") or "",
        shutil.which("ccx.exe") or "",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return None


def _mesh_step_with_gmsh(
    step_path: Path,
    mesh_size_mm: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Import a `.step` solid via Gmsh OpenCASCADE and generate a 3D tetrahedral mesh.
    Returns `(points_Nx3_mm, tets_Mx4_zero_based, surface_tris_Kx3_zero_based)`.
    """
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("ag_fea_part")
        gmsh.model.occ.importShapes(str(step_path))
        gmsh.model.occ.synchronize()

        vols = gmsh.model.getEntities(3)
        if not vols:
            raise ValueError(f"No 3D solid volumes found in STEP file: {step_path}")
        if len(vols) > 1:
            # Boolean fragment touching multi-body solids so shared interfaces share conformal nodes
            try:
                gmsh.model.occ.fragment(vols[:1], vols[1:])
                gmsh.model.occ.synchronize()
            except Exception:
                pass

        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        diag = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
        min_span = max(1.0, min(xmax - xmin, ymax - ymin, zmax - zmin))
        target_h = float(mesh_size_mm) if mesh_size_mm is not None else max(min_span * 0.55, diag / 28.0)

        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", max(0.4, target_h * 0.35))
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", target_h)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)  # Delaunay 3D
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        gmsh.model.mesh.generate(3)

        node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
        pts = np.asarray(node_coords, dtype=np.float64).reshape(-1, 3)
        tag_to_idx = {int(tag): idx for idx, tag in enumerate(node_tags)}

        elem_types, _, elem_node_tags = gmsh.model.mesh.getElements(3)
        tets_list: List[np.ndarray] = []
        for etype, enodes in zip(elem_types, elem_node_tags):
            if int(etype) == 4:  # 4-node linear tetrahedron
                raw = np.asarray([tag_to_idx[int(t)] for t in enodes], dtype=np.int64).reshape(-1, 4)
                tets_list.append(raw)
        if not tets_list:
            raise RuntimeError(f"Gmsh produced zero 3D tetrahedral elements for {step_path}")
        tets = np.vstack(tets_list)

        surf_types, _, surf_node_tags = gmsh.model.mesh.getElements(2)
        tris_list: List[np.ndarray] = []
        for etype, snodes in zip(surf_types, surf_node_tags):
            if int(etype) == 2:  # 3-node surface triangle
                raw = np.asarray([tag_to_idx[int(t)] for t in snodes], dtype=np.int64).reshape(-1, 3)
                tris_list.append(raw)
        tris = np.vstack(tris_list) if tris_list else np.zeros((0, 3), dtype=np.int64)
        return pts, tets, tris
    finally:
        gmsh.finalize()


def _select_nodes_mask(
    coords_nx3: np.ndarray,
    selector: Union[str, Dict[str, Any]],
    tol_mm: Optional[float] = None,
) -> np.ndarray:
    """
    Select mesh nodes using a semantic selector:
    - `'xmin' | 'xmax' | 'ymin' | 'ymax' | 'zmin' | 'zmax'`
    - `'point:x,y,z,r'` (sphere of radius `r` mm around `(x, y, z)`)
    - `'cyl_z:x,y,r'` (cylindrical bore around Z axis at `(x, y)` of radius `r` mm)
    - `dict` with `{'bbox': [xmin, ymin, zmin, xmax, ymax, zmax]}` or `{'point': [x,y,z], 'radius': r}`
    """
    bmin = coords_nx3.min(axis=0)
    bmax = coords_nx3.max(axis=0)
    spans = np.maximum(bmax - bmin, 1e-3)
    eps = float(tol_mm) if tol_mm is not None else max(0.15, float(spans.min()) * 0.02)

    if isinstance(selector, dict):
        if "bbox" in selector:
            bb = selector["bbox"]
            return (
                (coords_nx3[:, 0] >= bb[0] - eps)
                & (coords_nx3[:, 0] <= bb[3] + eps)
                & (coords_nx3[:, 1] >= bb[1] - eps)
                & (coords_nx3[:, 1] <= bb[4] + eps)
                & (coords_nx3[:, 2] >= bb[2] - eps)
                & (coords_nx3[:, 2] <= bb[5] + eps)
            )
        if "point" in selector:
            pt = np.asarray(selector["point"], dtype=np.float64)
            rad = float(selector.get("radius", 3.0))
            return np.linalg.norm(coords_nx3 - pt[None, :], axis=1) <= rad

    sel = str(selector).strip().lower()
    if sel == "xmin":
        return coords_nx3[:, 0] <= bmin[0] + eps
    if sel == "xmax":
        return coords_nx3[:, 0] >= bmax[0] - eps
    if sel == "ymin":
        return coords_nx3[:, 1] <= bmin[1] + eps
    if sel == "ymax":
        return coords_nx3[:, 1] >= bmax[1] - eps
    if sel == "zmin":
        return coords_nx3[:, 2] <= bmin[2] + eps
    if sel == "zmax":
        return coords_nx3[:, 2] >= bmax[2] - eps
    if sel.startswith("point:"):
        vals = [float(x) for x in sel.split(":", 1)[1].split(",")]
        pt = np.asarray(vals[:3], dtype=np.float64)
        rad = vals[3] if len(vals) > 3 else 4.0
        return np.linalg.norm(coords_nx3 - pt[None, :], axis=1) <= rad
    if sel.startswith("cyl_z:"):
        vals = [float(x) for x in sel.split(":", 1)[1].split(",")]
        cx, cy, r = vals[0], vals[1], vals[2]
        dr = np.hypot(coords_nx3[:, 0] - cx, coords_nx3[:, 1] - cy)
        return np.abs(dr - r) <= max(eps, 0.35)
    raise ValueError(f"Unsupported FEA boundary selector: {selector}")


def _von_mises_from_cauchy(s_xx: np.ndarray, s_yy: np.ndarray, s_zz: np.ndarray, s_xy: np.ndarray, s_yz: np.ndarray, s_xz: np.ndarray) -> np.ndarray:
    return np.sqrt(
        0.5 * ((s_xx - s_yy) ** 2 + (s_yy - s_zz) ** 2 + (s_zz - s_xx) ** 2)
        + 3.0 * (s_xy**2 + s_yz**2 + s_xz**2)
    )


def _compute_fdm_delamination_metrics(
    sigma_elem: np.ndarray,
    print_axis: str,
    mat_props: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Evaluate interlayer normal tension `sigma_nn` and interlayer shear `tau_nt`
    across the 3D-printed layer plane perpendicular to `print_axis` (`X`, `Y`, or `Z`).
    `sigma_elem` has shape `(N_elem, 3, 3)`.
    """
    ax = print_axis.strip().upper()
    n_vec = np.array([1.0, 0.0, 0.0] if ax == "X" else ([0.0, 1.0, 0.0] if ax == "Y" else [0.0, 0.0, 1.0]), dtype=np.float64)

    # Traction vector on layer plane: t = sigma @ n_vec -> (N_elem, 3)
    t_vec = np.einsum("eij,j->ei", sigma_elem, n_vec)
    sigma_nn = np.einsum("ei,i->e", t_vec, n_vec)
    tau_sq = np.maximum(0.0, np.sum(t_vec**2, axis=1) - sigma_nn**2)
    tau_nt = np.sqrt(tau_sq)

    s_zt = float(mat_props.get("z_tensile_mpa") or mat_props["yield_mpa"])
    s_zs = float(mat_props.get("z_shear_mpa") or (mat_props["yield_mpa"] / math.sqrt(3.0)))

    tensile_term = np.maximum(0.0, sigma_nn) / max(s_zt, 1e-6)
    shear_term = tau_nt / max(s_zs, 1e-6)
    delam_idx = np.sqrt(tensile_term**2 + shear_term**2)

    p99_idx = float(np.percentile(delam_idx, 99.0)) if len(delam_idx) > 10 else float(np.max(delam_idx))
    peak_idx = float(np.max(delam_idx))
    eff_idx = max(p99_idx, 1e-6)
    sf_layer = round(1.0 / eff_idx, 3)

    return {
        "print_axis": ax,
        "is_fdm_anisotropic": bool(mat_props.get("is_fdm", False)),
        "peak_interlayer_tension_mpa": round(float(np.max(np.maximum(0.0, sigma_nn))), 3),
        "p99_interlayer_tension_mpa": round(float(np.percentile(np.maximum(0.0, sigma_nn), 99.0)), 3),
        "peak_interlayer_shear_mpa": round(float(np.max(tau_nt)), 3),
        "z_tensile_allowable_mpa": s_zt,
        "z_shear_allowable_mpa": s_zs,
        "delamination_index_p99": round(p99_idx, 4),
        "delamination_index_peak": round(peak_idx, 4),
        "layer_safety_factor": sf_layer,
        "delamination_risk": "HIGH" if sf_layer < 1.25 else ("MODERATE" if sf_layer < 2.0 else "LOW"),
    }


def _render_fea_heatmap(
    pts_nx3: np.ndarray,
    disp_nx3: np.ndarray,
    tris_kx3: np.ndarray,
    nodal_vm_mpa: np.ndarray,
    out_png: Path,
    title: str,
    subtitle: str,
) -> None:
    """Render an isometric deformed Von Mises stress heatmap PNG using a vectorized Z-buffer."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    W, H = 960, 720
    header_h = 84
    img = np.full((H, W, 3), (245, 247, 250), dtype=np.uint8)

    spans = np.maximum(pts_nx3.max(axis=0) - pts_nx3.min(axis=0), 1e-3)
    max_u = max(float( np.linalg.norm(disp_nx3, axis=1).max() ), 1e-9)
    deform_scale = min(50.0, 0.08 * float(spans.max()) / max_u) if max_u > 1e-6 else 1.0
    def_pts = pts_nx3 + disp_nx3 * deform_scale

    yaw = math.radians(-40.0)
    pitch = math.radians(30.0)
    right = np.array([math.cos(yaw), math.sin(yaw), 0.0])
    up = np.array([-math.sin(yaw) * math.sin(pitch), math.cos(yaw) * math.sin(pitch), math.cos(pitch)])
    toward = np.cross(right, up)

    u = def_pts @ right
    v = def_pts @ up
    d = def_pts @ toward

    pad = 80
    scale = min((W - 2 * pad) / max(float(u.max() - u.min()), 1e-3), (H - header_h - 2 * pad) / max(float(v.max() - v.min()), 1e-3))
    px = W * 0.5 + scale * (u - 0.5 * (u.min() + u.max()))
    py = header_h + (H - header_h) * 0.5 - scale * (v - 0.5 * (v.min() + v.max()))

    vm_max = max(float(np.percentile(nodal_vm_mpa, 99.0)), 1e-3)
    zbuf = np.full((H, W), -np.inf, dtype=np.float64)

    def _jet_rgb(t_val: float) -> Tuple[int, int, int]:
        x = max(0.0, min(1.0, t_val))
        r = int(round(255.0 * max(0.0, min(1.0, 1.5 - abs(4.0 * x - 3.0)))))
        g = int(round(255.0 * max(0.0, min(1.0, 1.5 - abs(4.0 * x - 2.0)))))
        b = int(round(255.0 * max(0.0, min(1.0, 1.5 - abs(4.0 * x - 1.0)))))
        return (r, g, b)

    if len(tris_kx3) > 0:
        v0 = def_pts[tris_kx3[:, 0]]
        v1 = def_pts[tris_kx3[:, 1]]
        v2 = def_pts[tris_kx3[:, 2]]
        raw_n = np.cross(v1 - v0, v2 - v0)
        n_len = np.linalg.norm(raw_n, axis=1)
        light_dir = np.array([-0.35, 0.45, 0.82])

        for ti in range(len(tris_kx3)):
            if n_len[ti] < 1e-9:
                continue
            normal = raw_n[ti] / n_len[ti]
            f0, f1, f2 = tris_kx3[ti]
            ax, ay, az = px[f0], py[f0], d[f0]
            bx, by, bz = px[f1], py[f1], d[f1]
            cx_t, cy_t, cz_t = px[f2], py[f2], d[f2]

            left = max(0, int(math.floor(min(ax, bx, cx_t))))
            right_p = min(W - 1, int(math.ceil(max(ax, bx, cx_t))))
            top = max(header_h, int(math.floor(min(ay, by, cy_t))))
            bot = min(H - 1, int(math.ceil(max(ay, by, cy_t))))
            if right_p < left or bot < top:
                continue
            den = (by - cy_t) * (ax - cx_t) + (cx_t - bx) * (ay - cy_t)
            if abs(den) < 1e-9:
                continue

            tri_vm = float(nodal_vm_mpa[[f0, f1, f2]].mean())
            cr, cg, cb = _jet_rgb(tri_vm / vm_max)
            shade_f = 0.58 + 0.42 * abs(float(np.dot(normal, light_dir)))
            col = (min(255, int(cr * shade_f)), min(255, int(cg * shade_f)), min(255, int(cb * shade_f)))

            xs, ys = np.meshgrid(
                np.arange(left, right_p + 1, dtype=np.float64) + 0.5,
                np.arange(top, bot + 1, dtype=np.float64) + 0.5,
            )
            w0 = ((by - cy_t) * (xs - cx_t) + (cx_t - bx) * (ys - cy_t)) / den
            w1 = ((cy_t - ay) * (xs - cx_t) + (ax - cx_t) * (ys - cy_t)) / den
            w2 = 1.0 - w0 - w1
            z = w0 * az + w1 * bz + w2 * cz_t
            sub_z = zbuf[top : bot + 1, left : right_p + 1]
            mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (z > sub_z)
            if np.any(mask):
                sub_z[mask] = z[mask]
                img[top : bot + 1, left : right_p + 1][mask] = col

    pil_img = Image.fromarray(img)
    draw = ImageDraw.Draw(pil_img)
    f_bold = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 21) if Path("C:/Windows/Fonts/arialbd.ttf").exists() else ImageFont.load_default()
    f_reg = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14) if Path("C:/Windows/Fonts/arial.ttf").exists() else ImageFont.load_default()
    draw.rectangle([(0, 0), (W, header_h)], fill=(24, 36, 52))
    draw.text((22, 14), title, font=f_bold, fill=(255, 255, 255))
    draw.text((22, 46), f"{subtitle} | Deform Scale: {deform_scale:.1f}x | Color Max (p99): {vm_max:.1f} MPa", font=f_reg, fill=(185, 205, 225))
    pil_img.save(str(out_png))


def _solve_skfem(
    pts_nx3: np.ndarray,
    tets_mx4: np.ndarray,
    tris_kx3: np.ndarray,
    mat_props: Dict[str, Any],
    fixed_selector: Union[str, Dict[str, Any]],
    load_selector: Optional[Union[str, Dict[str, Any]]],
    force_n: Optional[Sequence[float]],
    g_accel_vec_m_s2: Optional[Sequence[float]],
    print_axis: str,
    out_png: Optional[Path],
    part_label: str,
) -> Dict[str, Any]:
    """Solve quadratic 10-node tetrahedral (`ElementTetP2`) 3D elasticity in `scikit-fem`."""
    from scipy.sparse.linalg import spsolve
    from skfem import Basis, BilinearForm, ElementTetP2, ElementVector, FacetBasis, LinearForm, MeshTet, condense
    from skfem.models.elasticity import lame_parameters, linear_elasticity

    mesh = MeshTet(np.ascontiguousarray(pts_nx3.T), np.ascontiguousarray(tets_mx4.T))
    elem = ElementVector(ElementTetP2())
    basis = Basis(mesh, elem, intorder=2)

    E = float(mat_props["E_mpa"])
    nu = float(mat_props["nu"])
    lam, mu = lame_parameters(E, nu)

    K = BilinearForm(linear_elasticity(lam, mu)).assemble(basis)
    rhs = np.zeros(basis.N, dtype=np.float64)

    # 1. Surface traction load on `load_selector`
    if load_selector is not None and force_n is not None and np.linalg.norm(force_n) > 1e-9:
        f_vec = np.asarray(force_n, dtype=np.float64)

        def _load_facets_pred(x: np.ndarray) -> np.ndarray:
            # x has shape (3, N_facets)
            return _select_nodes_mask(x.T, load_selector)

        fids = mesh.facets_satisfying(_load_facets_pred)
        if len(fids) == 0:
            raise ValueError(f"load_selector '{load_selector}' matched zero boundary facets.")
        fbasis = FacetBasis(mesh, elem, facets=fids, intorder=2)
        # Compute exact area of selected facets in mm^2
        area_form = LinearForm(lambda v, w: v[0]).assemble(fbasis)
        # Simpler exact triangle area sum on mesh.facets[:, fids]
        sel_tris = mesh.facets[:, fids].T  # (K_sel, 3)
        tv0 = pts_nx3[sel_tris[:, 0]]
        tv1 = pts_nx3[sel_tris[:, 1]]
        tv2 = pts_nx3[sel_tris[:, 2]]
        total_area_mm2 = float(0.5 * np.linalg.norm(np.cross(tv1 - tv0, tv2 - tv0), axis=1).sum())
        traction_mpa = f_vec / max(total_area_mm2, 1e-6)

        @LinearForm
        def surface_load(v, w):
            return traction_mpa[0] * v[0] + traction_mpa[1] * v[1] + traction_mpa[2] * v[2]

        rhs += surface_load.assemble(fbasis)

    # 2. Optional gravity / G-shock body load (e.g. 3g bellypan / electronics load)
    if g_accel_vec_m_s2 is not None and np.linalg.norm(g_accel_vec_m_s2) > 1e-9:
        rho_kg_m3 = float(mat_props["density_kg_m3"])
        # Force per mm^3 = rho [kg/m^3] * 1e-9 [m^3/mm^3] * a [m/s^2] -> N/mm^3
        bf = rho_kg_m3 * 1e-9 * np.asarray(g_accel_vec_m_s2, dtype=np.float64)

        @LinearForm
        def body_load(v, w):
            return bf[0] * v[0] + bf[1] * v[1] + bf[2] * v[2]

        rhs += body_load.assemble(basis)

    # 3. Fixed Dirichlet DOFs on `fixed_selector`
    dof_locs = basis.doflocs.T  # (N_dofs_or_locs, 3)
    # Use basis.get_dofs on facets/nodes satisfying fixed_selector
    fixed_facets = mesh.facets_satisfying(lambda x: _select_nodes_mask(x.T, fixed_selector))
    if len(fixed_facets) > 0:
        D = basis.get_dofs(facets=fixed_facets)
    else:
        # Fallback to nodal DOF coordinate mask
        mask_dof = _select_nodes_mask(dof_locs, fixed_selector)
        D = np.flatnonzero(mask_dof)
        if len(D) == 0:
            raise ValueError(f"fixed_selector '{fixed_selector}' matched zero DOFs.")

    from skfem import solve as skfem_solve

    u_sol = skfem_solve(*condense(K, rhs, D=D))

    # Extract vertex displacements (at original `pts_nx3` vertices)
    # Interpolate displacement & displacement gradient at element centroids (`X_ref = [[0.25],[0.25],[0.25]]`)
    interp = basis.interpolate(u_sol)
    # `u_sol[basis.nodal_dofs[:, :len(pts_nx3)]]` gives exact vertex displacements!
    n_verts = pts_nx3.shape[0]
    u_verts = u_sol[basis.nodal_dofs[:, :n_verts]].T  # (n_verts, 3)
    disp_mag = np.linalg.norm(u_verts, axis=1)
    max_disp_mm = float(disp_mag.max())

    # Strain & Cauchy stress tensor at quadrature points: `interp.grad` has shape `(3, 3, N_elem, N_qp)`
    grad_u = interp.grad  # du_i / dx_j -> (3, 3, N_elem, N_qp)
    grad_mean = grad_u.mean(axis=3)  # (3, 3, N_elem)
    eps = 0.5 * (grad_mean + np.swapaxes(grad_mean, 0, 1))  # (3, 3, N_elem)
    tr_eps = eps[0, 0] + eps[1, 1] + eps[2, 2]

    sigma = np.zeros((tets_mx4.shape[0], 3, 3), dtype=np.float64)
    for i in range(3):
        for j in range(3):
            sigma[:, i, j] = 2.0 * mu * eps[i, j] + (lam * tr_eps if i == j else 0.0)

    elem_vm = _von_mises_from_cauchy(
        sigma[:, 0, 0], sigma[:, 1, 1], sigma[:, 2, 2],
        sigma[:, 0, 1], sigma[:, 1, 2], sigma[:, 0, 2],
    )
    peak_vm = float(elem_vm.max())
    p99_vm = float(np.percentile(elem_vm, 99.0)) if len(elem_vm) > 10 else peak_vm
    yield_mpa = float(mat_props["yield_mpa"])
    yield_sf = round(yield_mpa / max(p99_vm, 1e-6), 3)

    fdm_metrics = _compute_fdm_delamination_metrics(sigma, print_axis=print_axis, mat_props=mat_props)

    if out_png is not None:
        nodal_vm = np.zeros(n_verts, dtype=np.float64)
        nodal_cnt = np.zeros(n_verts, dtype=np.float64)
        for c in range(4):
            np.add.at(nodal_vm, tets_mx4[:, c], elem_vm)
            np.add.at(nodal_cnt, tets_mx4[:, c], 1.0)
        nodal_vm /= np.maximum(nodal_cnt, 1.0)
        _render_fea_heatmap(
            pts_nx3=pts_nx3,
            disp_nx3=u_verts,
            tris_kx3=tris_kx3,
            nodal_vm_mpa=nodal_vm,
            out_png=out_png,
            title=f"AG 3D SOLID FEA — {part_label}",
            subtitle=f"Max Disp: {max_disp_mm:.3f} mm | p99 VM: {p99_vm:.1f} MPa | Yield SF: {yield_sf:.2f} | Layer SF: {fdm_metrics['layer_safety_factor']:.2f}",
        )

    return {
        "solver": "scikit-fem (ElementTetP2 quadratic 10-node tets)",
        "dof_count": int(basis.N),
        "max_displacement_mm": round(max_disp_mm, 4),
        "peak_von_mises_mpa": round(peak_vm, 3),
        "p99_von_mises_mpa": round(p99_vm, 3),
        "yield_strength_mpa": yield_mpa,
        "yield_safety_factor": yield_sf,
        "fdm_delamination": fdm_metrics,
        "heatmap_png": str(out_png) if out_png else None,
    }


def _solve_calculix(
    step_path: Path,
    mat_props: Dict[str, Any],
    fixed_selector: Union[str, Dict[str, Any]],
    load_selector: Optional[Union[str, Dict[str, Any]]],
    force_n: Optional[Sequence[float]],
    mesh_size_mm: Optional[float],
    buckle_modes: int = 0,
    out_inp_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a quadratic `C3D10` Abaqus/CalculiX `.inp` deck via Gmsh and solve with local `ccx.exe`."""
    import gmsh

    ccx_exe = find_calculix_ccx()
    if not ccx_exe:
        raise FileNotFoundError("CalculiX solver (ccx.exe) not found. Install CalculiX or use backend='skfem'.")

    work_dir = Path(tempfile.mkdtemp(prefix="ag_ccx_"))
    try:
        return _run_calculix_fea_in_dir(
            work_dir=work_dir,
            ccx_exe=ccx_exe,
            step_path=step_path,
            mat_props=mat_props,
            fixed_selector=fixed_selector,
            load_selector=load_selector,
            force_n=force_n,
            mesh_size_mm=mesh_size_mm,
            buckle_modes=buckle_modes,
            out_inp_path=out_inp_path,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_calculix_fea_in_dir(
    work_dir: Path,
    ccx_exe: str,
    step_path: Path,
    mat_props: Dict[str, Any],
    fixed_selector: Union[str, Dict[str, Any]],
    load_selector: Optional[Union[str, Dict[str, Any]]],
    force_n: Optional[Sequence[float]],
    mesh_size_mm: Optional[float],
    buckle_modes: int,
    out_inp_path: Optional[Path],
) -> Dict[str, Any]:
    import gmsh

    job_stem = "fea_job"
    raw_mesh_inp = work_dir / "gmsh_mesh.inp"

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("ccx_model")
        gmsh.model.occ.importShapes(str(step_path))
        gmsh.model.occ.synchronize()
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
        diag = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
        min_span = max(1.0, min(xmax - xmin, ymax - ymin, zmax - zmin))
        target_h = float(mesh_size_mm) if mesh_size_mm is not None else max(min_span * 0.55, diag / 26.0)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", max(0.4, target_h * 0.35))
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", target_h)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)  # Quadratic C3D10!
        gmsh.model.mesh.generate(3)

        node_tags, node_coords, _ = gmsh.model.mesh.getNodes()
        coords = np.asarray(node_coords, dtype=np.float64).reshape(-1, 3)
        tags_arr = np.asarray(node_tags, dtype=np.int64)

        gmsh.write(str(raw_mesh_inp))
    finally:
        gmsh.finalize()

    # Keep only `*NODE` and `*ELEMENT, type=C3D10` blocks from Gmsh's `.inp` (skip 1D/2D T3D3/CPS6 elements)
    raw_lines = raw_mesh_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    kept_lines: List[str] = []
    keep_section = False
    elset_name = "EALL"
    for line in raw_lines:
        ul = line.strip().upper()
        if ul.startswith("*HEADING"):
            keep_section = True
            kept_lines.append(line)
        elif ul.startswith("*NODE"):
            keep_section = True
            kept_lines.append("*NODE, NSET=NALL")
        elif ul.startswith("*ELEMENT"):
            if "C3D10" in ul or "C3D4" in ul:
                keep_section = True
                etype = "C3D10" if "C3D10" in ul else "C3D4"
                kept_lines.append(f"*ELEMENT, TYPE={etype}, ELSET={elset_name}")
            else:
                keep_section = False
        elif ul.startswith("*"):
            keep_section = False
        elif keep_section:
            kept_lines.append(line)

    fixed_mask = _select_nodes_mask(coords, fixed_selector)
    fixed_tags = tags_arr[fixed_mask]
    if len(fixed_tags) == 0:
        raise ValueError(f"fixed_selector '{fixed_selector}' matched zero CalculiX nodes.")

    deck: List[str] = list(kept_lines)
    deck.append("*NSET, NSET=NFIX")
    for i in range(0, len(fixed_tags), 12):
        deck.append(", ".join(str(int(t)) for t in fixed_tags[i : i + 12]))

    E = float(mat_props["E_mpa"])
    nu = float(mat_props["nu"])
    rho_tonne_mm3 = float(mat_props["density_kg_m3"]) * 1e-12  # tonne/mm^3 for consistent mm-N-s-MPa
    deck.extend(
        [
            "*MATERIAL, NAME=MAT1",
            "*ELASTIC",
            f"{E:.2f}, {nu:.4f}",
            "*DENSITY",
            f"{rho_tonne_mm3:.6e}",
            f"*SOLID SECTION, ELSET={elset_name}, MATERIAL=MAT1",
            "*BOUNDARY",
            "NFIX, 1, 3, 0.0",
        ]
    )

    if buckle_modes > 0:
        deck.extend(["*STEP", "*BUCKLE", f"{int(buckle_modes)}"])
    else:
        deck.extend(["*STEP", "*STATIC"])

    if load_selector is not None and force_n is not None and np.linalg.norm(force_n) > 1e-9:
        load_mask = _select_nodes_mask(coords, load_selector)
        load_tags = tags_arr[load_mask]
        if len(load_tags) == 0:
            raise ValueError(f"load_selector '{load_selector}' matched zero CalculiX nodes.")
        deck.append("*CLOAD")
        per_node_f = np.asarray(force_n, dtype=np.float64) / float(len(load_tags))
        for nt in load_tags:
            for dof_idx in range(3):
                if abs(per_node_f[dof_idx]) > 1e-9:
                    deck.append(f"{int(nt)}, {dof_idx + 1}, {per_node_f[dof_idx]:.6e}")

    deck.extend(
        [
            "*NODE PRINT, NSET=NALL",
            "U",
            f"*EL PRINT, ELSET={elset_name}",
            "S",
            "*NODE FILE",
            "U",
            "*EL FILE",
            "S",
            "*END STEP",
            "",
        ]
    )

    job_inp = work_dir / f"{job_stem}.inp"
    inp_text = "\n".join(deck)
    job_inp.write_text(inp_text, encoding="utf-8")
    if out_inp_path is not None:
        out_inp_path.parent.mkdir(parents=True, exist_ok=True)
        out_inp_path.write_text(inp_text, encoding="utf-8")
    else:
        out_inp_path = step_path.with_name(f"{step_path.stem}_ccx.inp")
        out_inp_path.write_text(inp_text, encoding="utf-8")

    env = os.environ.copy()
    ccx_bin_dir = str(Path(ccx_exe).parent)
    env["PATH"] = f"{ccx_bin_dir};{env.get('PATH', '')}"
    env["OMP_NUM_THREADS"] = "4"

    proc = subprocess.run(
        [ccx_exe, job_stem],
        cwd=str(work_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"CalculiX ccx failed ({proc.returncode}):\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}")

    dat_file = work_dir / f"{job_stem}.dat"
    max_disp = 0.0
    peak_vm = 0.0
    buckle_factors: List[float] = []

    if dat_file.is_file():
        mode = None
        for raw_l in dat_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            ls = raw_l.strip()
            ll = ls.lower()
            if not ls:
                continue
            if "displacements" in ll and "vx" in ll:
                mode = "disp"
                continue
            if "stresses" in ll and "sxx" in ll:
                mode = "stress"
                continue
            if "buckling factor" in ll:
                mode = "buckle"
                continue
            parts = ls.split()
            if mode == "disp" and len(parts) >= 4:
                try:
                    ux, uy, uz = float(parts[1]), float(parts[2]), float(parts[3])
                    max_disp = max(max_disp, math.sqrt(ux * ux + uy * uy + uz * uz))
                except ValueError:
                    mode = None
            elif mode == "stress" and len(parts) >= 8:
                try:
                    sxx, syy, szz = float(parts[2]), float(parts[3]), float(parts[4])
                    sxy, sxz, syz = float(parts[5]), float(parts[6]), float(parts[7])
                    vm = math.sqrt(
                        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                        + 3.0 * (sxy**2 + syz**2 + sxz**2)
                    )
                    peak_vm = max(peak_vm, vm)
                except ValueError:
                    mode = None
            elif mode == "buckle" and len(parts) >= 2:
                try:
                    int(parts[0])
                    buckle_factors.append(float(parts[1]))
                except ValueError:
                    pass

    shutil.rmtree(work_dir, ignore_errors=True)

    yield_mpa = float(mat_props["yield_mpa"])
    return {
        "solver": f"CalculiX ({Path(ccx_exe).name}, C3D10 quadratic tets)",
        "inp_path": str(out_inp_path),
        "max_displacement_mm": round(max_disp, 4),
        "peak_von_mises_mpa": round(peak_vm, 3),
        "yield_strength_mpa": yield_mpa,
        "yield_safety_factor": round(yield_mpa / max(peak_vm, 1e-6), 3),
        "buckling_factors": buckle_factors,
    }


def run_3d_fea(
    step_path: Union[Path, str],
    material: str = "PLA",
    fixed_selector: Union[str, Dict[str, Any]] = "xmin",
    load_selector: Optional[Union[str, Dict[str, Any]]] = "xmax",
    force_n: Optional[Sequence[float]] = None,
    g_load_vec_g: Optional[Sequence[float]] = None,
    print_axis: str = "Z",
    mesh_size_mm: Optional[float] = None,
    backend: str = "skfem",
    buckle_modes: int = 0,
    out_png: Optional[Union[Path, str]] = None,
    out_inp: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """
    Run 1-command 3D B-Rep tetrahedral FEA on a `.step` file using either:
    - `backend="skfem"` (fast in-process quadratic `ElementTetP2` + FDM Z-layer delamination + heatmap PNG)
    - `backend="calculix"` (Abaqus `.inp` deck + local `ccx.exe` static/buckling solver)
    """
    t0 = time.perf_counter()
    p = Path(step_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"STEP file not found: {p}")

    mat_key = material.strip().upper()
    if mat_key not in MATERIAL_LIBRARY:
        raise ValueError(f"Unknown material '{material}'. Available: {sorted(MATERIAL_LIBRARY.keys())}")
    mat_props = MATERIAL_LIBRARY[mat_key]

    g_accel = [float(g) * 9.80665 for g in g_load_vec_g] if g_load_vec_g is not None else None

    if backend.lower() in ("calculix", "ccx"):
        res = _solve_calculix(
            step_path=p,
            mat_props=mat_props,
            fixed_selector=fixed_selector,
            load_selector=load_selector,
            force_n=force_n,
            mesh_size_mm=mesh_size_mm,
            buckle_modes=buckle_modes,
            out_inp_path=Path(out_inp).resolve() if out_inp else None,
        )
        return {
            "status": "OK",
            "file": str(p),
            "material": mat_key,
            "backend": "calculix",
            **res,
            "elapsed_ms": round(1000.0 * (time.perf_counter() - t0), 1),
        }

    pts, tets, tris = _mesh_step_with_gmsh(p, mesh_size_mm=mesh_size_mm)
    res = _solve_skfem(
        pts_nx3=pts,
        tets_mx4=tets,
        tris_kx3=tris,
        mat_props=mat_props,
        fixed_selector=fixed_selector,
        load_selector=load_selector,
        force_n=force_n,
        g_accel_vec_m_s2=g_accel,
        print_axis=print_axis,
        out_png=Path(out_png).resolve() if out_png else None,
        part_label=p.stem,
    )
    return {
        "status": "OK",
        "file": str(p),
        "material": mat_key,
        "backend": "skfem",
        "node_count": int(pts.shape[0]),
        "tet_count": int(tets.shape[0]),
        **res,
        "elapsed_ms": round(1000.0 * (time.perf_counter() - t0), 1),
    }
