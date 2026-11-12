"""
Headless 3D Print Slicer & DFM Audit (`tools/cad_slicer_audit.py`)
==================================================================
Provides fast deterministic 3D-printability checks plus headless `OrcaSlicer` CLI
slicing integration for `.stl` and `.3mf` parts/plates in `fabrication/prints/`:

1. Printer Bed Envelope Validation:
   - `bambu_256`: 256.0 x 256.0 x 256.0 mm (default planning envelope in WORKFLOW.md)
   - `snapmaker_u1`: 269.0 x 260.0 x 270.0 mm (Snapmaker U1 multi-tool bed)
   - `prusa_mk4`: 250.0 x 210.0 x 220.0 mm
2. Geometric Printability & Mesh Integrity (`trimesh`):
   - Watertight manifold validation & component island counting.
   - First-layer bed adhesion contact area (`z <= z_min + 0.25 mm`) to catch floating parts
     or microscopic point-contact orientations before slicing.
   - Steep overhang area analysis (`angle_from_horizontal < 35 deg`, i.e. `n_z < -0.574`
     above the first layer) estimating support requirement risk.
3. Headless `OrcaSlicer` CLI Execution (`orca-slicer.exe`):
   - Runs `orca-slicer.exe --slice 0 --outputdir <tmp>` when available and parses generated
     G-code / `.gcode.3mf` headers for exact filament grams, support grams, and print time,
     falling back cleanly to physics-based wall+infill shell volume estimation if no printer
     profile is configured yet.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import zipfile

import numpy as np
import trimesh

BED_PROFILES_MM: Dict[str, Tuple[float, float, float]] = {
    "bambu_256": (256.0, 256.0, 256.0),
    "snapmaker_u1": (269.0, 260.0, 270.0),
    "prusa_mk4": (250.0, 210.0, 220.0),
}

FILAMENT_DENSITY_G_CM3: Dict[str, float] = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PC": 1.20,
    "PAHT-CF": 1.36,
    "PA-CF": 1.25,
}


def find_orcaslicer_cli() -> Optional[str]:
    """Locate `orca-slicer.exe` (or `prusa-slicer-console.exe`) on Windows or PATH."""
    candidates = [
        os.environ.get("ORCASLICER_CLI", ""),
        r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
        r"C:\Program Files\Snapmaker Orca\orca-slicer.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OrcaSlicer" / "orca-slicer.exe"),
        shutil.which("orca-slicer") or "",
        shutil.which("orca-slicer.exe") or "",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    return None


def _load_print_mesh(mesh_path: Path) -> Tuple[trimesh.Trimesh, int]:
    """Load `.stl` or `.3mf` into a single combined `trimesh.Trimesh` and return `(mesh, body_count)`."""
    ext = mesh_path.suffix.lower()
    if ext == ".3mf":
        # Support both trimesh native 3MF and lib3mf / XML fallback
        try:
            loaded = trimesh.load(str(mesh_path), force="scene")
            if isinstance(loaded, trimesh.Scene) and len(loaded.geometry) > 0:
                geoms = []
                for node_name in loaded.graph.nodes_geometry:
                    tf, geom_name = loaded.graph[node_name]
                    g = loaded.geometry[geom_name].copy()
                    g.apply_transform(tf)
                    geoms.append(g)
                if not geoms:
                    geoms = list(loaded.geometry.values())
                combined = trimesh.util.concatenate(geoms)
                return combined, len(geoms)
        except Exception:
            pass
        import xml.etree.ElementTree as ET

        verts_all: List[np.ndarray] = []
        faces_all: List[np.ndarray] = []
        v_offset = 0
        body_cnt = 0
        with zipfile.ZipFile(mesh_path, "r") as zf:
            model_files = [n for n in zf.namelist() if n.lower().endswith(".model")]
            for mf in model_files:
                root = ET.fromstring(zf.read(mf))
                for mesh_el in root.iter():
                    if mesh_el.tag.endswith("mesh"):
                        v_list = []
                        f_list = []
                        for child in mesh_el:
                            if child.tag.endswith("vertices"):
                                for v in child:
                                    v_list.append([float(v.attrib["x"]), float(v.attrib["y"]), float(v.attrib["z"])])
                            elif child.tag.endswith("triangles"):
                                for t in child:
                                    f_list.append([int(t.attrib["v1"]), int(t.attrib["v2"]), int(t.attrib["v3"])])
                        if v_list and f_list:
                            verts_all.append(np.asarray(v_list, dtype=np.float64))
                            faces_all.append(np.asarray(f_list, dtype=np.int64) + v_offset)
                            v_offset += len(v_list)
                            body_cnt += 1
        if not verts_all:
            raise ValueError(f"No triangle geometry found inside 3MF archive: {mesh_path}")
        return trimesh.Trimesh(vertices=np.vstack(verts_all), faces=np.vstack(faces_all), process=True), body_cnt

    loaded = trimesh.load(str(mesh_path), force="mesh")
    if isinstance(loaded, trimesh.Scene):
        geoms = list(loaded.geometry.values())
        return trimesh.util.concatenate(geoms), len(geoms)
    try:
        islands = loaded.split(only_watertight=False)
        body_cnt = len(islands)
    except Exception:
        body_cnt = 1
    return loaded, max(1, body_cnt)


def _parse_gcode_metadata(gcode_text: str) -> Dict[str, Any]:
    """Extract filament weight, length, support info, and estimated print time from Orca/Bambu/Prusa G-code."""
    meta: Dict[str, Any] = {}
    m_g = re.search(r";\s*(?:total\s+)?filament\s+used\s*\[g\]\s*=\s*([0-9.,\s]+)", gcode_text, re.IGNORECASE)
    if m_g:
        vals = [float(x) for x in re.findall(r"[0-9]+(?:\.[0-9]+)?", m_g.group(1))]
        if vals:
            meta["filament_mass_g"] = round(sum(vals), 2)

    m_mm = re.search(r";\s*filament\s+used\s*\[mm\]\s*=\s*([0-9.,\s]+)", gcode_text, re.IGNORECASE)
    if m_mm:
        vals = [float(x) for x in re.findall(r"[0-9]+(?:\.[0-9]+)?", m_mm.group(1))]
        if vals:
            meta["filament_length_m"] = round(sum(vals) / 1000.0, 3)

    m_time = re.search(r";\s*estimated\s+printing\s+time.*=\s*(.+)", gcode_text, re.IGNORECASE)
    if m_time:
        meta["estimated_print_time"] = m_time.group(1).strip()

    m_layers = re.search(r";\s*total\s+layers?\s+count\s*=\s*(\d+)", gcode_text, re.IGNORECASE)
    if m_layers:
        meta["layer_count"] = int(m_layers.group(1))
    return meta


def _try_run_orcaslicer_cli(mesh_path: Path, timeout_s: float = 35.0) -> Dict[str, Any]:
    """Attempt headless slicing via `orca-slicer.exe` and parse any produced `.gcode` / `.3mf` metadata."""
    exe = find_orcaslicer_cli()
    if not exe:
        return {"invoked": False, "reason": "orca-slicer.exe not found"}

    install_dir = Path(exe).parent
    bbl_dir = install_dir / "resources" / "profiles" / "BBL"
    settings_args: List[str] = []
    machine_json = bbl_dir / "machine" / "Bambu Lab X1 Carbon 0.4 nozzle.json"
    process_json = bbl_dir / "process" / "0.20mm Standard @BBL X1C.json"
    filament_json = bbl_dir / "filament" / "Bambu PLA Basic @BBL X1C.json"
    if machine_json.is_file() and process_json.is_file():
        settings_args.extend(["--load-settings", f"{machine_json};{process_json}"])
        if filament_json.is_file():
            settings_args.extend(["--load-filaments", str(filament_json)])

    with tempfile.TemporaryDirectory(prefix="ag_orca_") as tmp_dir:
        cmd = [exe, *settings_args, "--slice", "0", "--outputdir", tmp_dir, str(mesh_path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"invoked": True, "exe": exe, "sliced_ok": False, "reason": "timeout"}
        except Exception as exc:
            return {"invoked": True, "exe": exe, "sliced_ok": False, "reason": str(exc)}

        out_path = Path(tmp_dir)
        for gf in out_path.rglob("*.gcode"):
            txt = gf.read_text(encoding="utf-8", errors="ignore")
            parsed = _parse_gcode_metadata(txt)
            return {"invoked": True, "exe": exe, "sliced_ok": True, **parsed}

        for mf in out_path.rglob("*.3mf"):
            try:
                with zipfile.ZipFile(mf, "r") as zf:
                    for zn in zf.namelist():
                        if zn.endswith(".gcode"):
                            txt = zf.read(zn).decode("utf-8", errors="ignore")
                            parsed = _parse_gcode_metadata(txt)
                            return {"invoked": True, "exe": exe, "sliced_ok": True, **parsed}
            except Exception:
                pass

        return {
            "invoked": True,
            "exe": exe,
            "sliced_ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "note": (proc.stderr or proc.stdout or "").strip()[:300],
        }


def slice_audit_print(
    file_path: Path | str,
    bed_profile: str = "bambu_256",
    bed_size_mm: Optional[Tuple[float, float, float]] = None,
    bed_margin_mm: float = 0.0,
    filament: str = "PLA",
    wall_loops: int = 3,
    infill_fraction: float = 0.25,
    overhang_threshold_deg: float = 55.0,
    run_slicer_cli: bool = True,
) -> Dict[str, Any]:
    """
    Audit a `.stl` or `.3mf` print file or arranged plate for bed fit, manifold integrity,
    first-layer bed adhesion, steep overhangs, filament mass, and optional headless OrcaSlicer G-code metrics.
    """
    t0 = time.perf_counter()
    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Print file not found: {p}")

    bed_xyz = bed_size_mm or BED_PROFILES_MM.get(bed_profile.lower(), BED_PROFILES_MM["bambu_256"])
    eff_bed = (
        round(bed_xyz[0] - 2.0 * bed_margin_mm, 2),
        round(bed_xyz[1] - 2.0 * bed_margin_mm, 2),
        round(bed_xyz[2], 2),
    )

    mesh, body_count = _load_print_mesh(p)
    bounds = mesh.bounds
    bmin = bounds[0]
    bmax = bounds[1]
    extents = bmax - bmin

    # Allow 90-deg XY swap on the bed if oriented along the other horizontal axis
    fits_direct = bool((extents[0] <= eff_bed[0] + 1e-4) and (extents[1] <= eff_bed[1] + 1e-4))
    fits_rotated_90 = bool((extents[1] <= eff_bed[0] + 1e-4) and (extents[0] <= eff_bed[1] + 1e-4))
    fits_z = bool(extents[2] <= eff_bed[2] + 1e-4)
    fits_bed = bool((fits_direct or fits_rotated_90) and fits_z)

    # Face-level DFM analysis: first-layer contact & steep overhangs
    tri_verts = mesh.triangles  # (F, 3, 3)
    tri_areas = mesh.area_faces  # (F,)
    tri_normals = mesh.face_normals  # (F, 3)
    tri_z_max = tri_verts[:, :, 2].max(axis=1)
    tri_z_min = tri_verts[:, :, 2].min(axis=1)

    z0 = float(bmin[2])
    bed_contact_mask = (tri_z_max <= z0 + 0.25) & (tri_normals[:, 2] < -0.85)
    first_layer_area_mm2 = float(tri_areas[bed_contact_mask].sum())

    # Overhang angle measured from the build vertical (-Z): e.g. >55 deg from vertical means <35 deg from horizontal
    nz_thresh = -math.cos(math.radians(max(10.0, min(85.0, overhang_threshold_deg))))
    # Wait: a flat horizontal unsupported bridge has n_z = -1.0 (90 deg overhang from vertical!);
    # a 45-deg slope has n_z = -0.707; a vertical wall has n_z = 0.0.
    # Any downward face above the first layer whose angle from vertical exceeds overhang_threshold_deg
    # satisfies n_z < -sin(90 - overhang_threshold_deg) = -cos(overhang_threshold_deg).
    overhang_mask = (tri_z_min > z0 + 0.60) & (tri_normals[:, 2] < nz_thresh)
    overhang_area_mm2 = float(tri_areas[overhang_mask].sum())
    total_area_mm2 = max(float(tri_areas.sum()), 1e-6)
    overhang_ratio = overhang_area_mm2 / total_area_mm2

    is_watertight = bool(mesh.is_watertight)
    vol_mm3 = abs(float(mesh.volume)) if is_watertight else abs(float(mesh.convex_hull.volume)) * 0.5
    vol_cm3 = vol_mm3 / 1000.0
    surface_cm2 = total_area_mm2 / 100.0

    # Physics-based shell + infill effective volume model when G-code isn't emitted:
    # shell thickness ~= wall_loops * 0.42 mm + top/bottom (~0.8 mm average)
    shell_thickness_cm = min(0.25, (wall_loops * 0.042))
    shell_vol_cm3 = min(vol_cm3, surface_cm2 * shell_thickness_cm)
    interior_vol_cm3 = max(0.0, vol_cm3 - shell_vol_cm3)
    effective_polymer_cm3 = shell_vol_cm3 + interior_vol_cm3 * float(infill_fraction)
    density = FILAMENT_DENSITY_G_CM3.get(filament.upper(), 1.24)
    analytic_mass_g = round(effective_polymer_cm3 * density, 2)
    # Typical high-speed CoreXY volumetric rate ~12 mm^3/s average including travels
    analytic_minutes = round((effective_polymer_cm3 * 1000.0) / (12.0 * 60.0), 1)

    cli_report: Dict[str, Any] = {"invoked": False}
    if run_slicer_cli:
        cli_report = _try_run_orcaslicer_cli(p)

    est_mass_g = float(cli_report.get("filament_mass_g", analytic_mass_g))

    warnings: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []

    if not fits_bed:
        errors.append(
            {
                "code": "EXCEEDS_BUILD_VOLUME",
                "message": (
                    f"Part/plate envelope {extents[0]:.2f} x {extents[1]:.2f} x {extents[2]:.2f} mm "
                    f"exceeds '{bed_profile}' printable envelope {eff_bed[0]} x {eff_bed[1]} x {eff_bed[2]} mm."
                ),
            }
        )
    if not is_watertight:
        warnings.append(
            {
                "code": "NON_WATERTIGHT_MESH",
                "message": "Mesh is not strictly watertight; slicer auto-repair may alter thin features.",
            }
        )
    xy_area = max(float(extents[0] * extents[1]), 1.0)
    if first_layer_area_mm2 < 20.0 or (first_layer_area_mm2 / xy_area) < 0.02:
        warnings.append(
            {
                "code": "LOW_BED_ADHESION_AREA",
                "message": (
                    f"First-layer bed contact is only {first_layer_area_mm2:.1f} mm^2 "
                    f"({100.0 * first_layer_area_mm2 / xy_area:.1f}% of XY footprint); verify print orientation or add brim."
                ),
            }
        )
    if overhang_area_mm2 > 150.0 and overhang_ratio > 0.06:
        warnings.append(
            {
                "code": "SIGNIFICANT_OVERHANGS_DETECTED",
                "message": (
                    f"Detected {overhang_area_mm2:.1f} mm^2 ({100.0 * overhang_ratio:.1f}% of surface) "
                    f"exceeding {overhang_threshold_deg:.0f} deg overhang threshold; supports or chamfering recommended."
                ),
            }
        )

    return {
        "status": "PASS" if not errors else "FAIL",
        "file": str(p),
        "bed_profile": bed_profile,
        "filament": filament.upper(),
        "bed_fit": {
            "fits_bed": fits_bed,
            "fits_without_rotation": fits_direct and fits_z,
            "requires_90deg_z_rotation": (not fits_direct) and fits_rotated_90 and fits_z,
            "part_extents_mm": [round(float(extents[0]), 2), round(float(extents[1]), 2), round(float(extents[2]), 2)],
            "printable_envelope_mm": list(eff_bed),
            "xy_clearance_margin_mm": [
                round(float(eff_bed[0] - (extents[0] if fits_direct else extents[1])), 2),
                round(float(eff_bed[1] - (extents[1] if fits_direct else extents[0])), 2),
            ],
        },
        "mesh": {
            "body_count": body_count,
            "facet_count": int(len(mesh.faces)),
            "is_watertight": is_watertight,
            "solid_volume_cm3": round(vol_cm3, 3),
            "first_layer_contact_mm2": round(first_layer_area_mm2, 2),
            "steep_overhang_area_mm2": round(overhang_area_mm2, 2),
            "steep_overhang_pct": round(100.0 * overhang_ratio, 2),
        },
        "estimated_mass_g": est_mass_g,
        "estimated_print_minutes_approx": analytic_minutes,
        "orcaslicer_cli": cli_report,
        "errors": errors,
        "warnings": warnings,
        "elapsed_ms": round(1000.0 * (time.perf_counter() - t0), 1),
    }
