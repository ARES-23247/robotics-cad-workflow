"""
Antigravity (AG) Local-First + Cloud CAD Pipeline
=================================================
Provides a unified CLI and Python library for Antigravity CAD workflows:
1. `inspect <file.FCStd|file.step|file.stl> [--interference] [--clearance <mm>]`:
   Runs headless FreeCAD 1.0 (with XCAF STEP label preservation) to audit all solids
   (validity, volume, bounding box, pairwise boolean interference/collision checks,
   exact B-Rep `distToShape` minimum-clearance checks, and a Top Nearest-Neighbors
   clearance table with 3D witness coordinates).
2. `export <file.FCStd|file.step> [--step <out.step>] [--stl <out.stl>]`:
   Exports STEP AP214 and watertight STL meshes locally via FreeCAD 1.0 (zero API limits).
3. `render <file.FCStd|file.step|file.stl> --out <dir> [--prefix <name>]`:
   Tessellates solids locally via FreeCAD 1.0 and renders 5 multi-angle views
   (iso_top, iso_bottom, top, front, left) plus a 3D cutaway section view (section_iso)
   and a 6-panel engineering contact sheet (overview_5view) using a true per-pixel
   NumPy Z-buffer with silhouette & normal-crease edge detection.
4. `build <script.py> [--audit] [--clearance <mm>] [--render-dir <dir>]`:
   Executes a parametric `build123d` Python script using the repository's `.venv`
   (Python 3.12 + OpenCASCADE OCP), and optionally audits and renders its outputs.
5. `onshape-push <file.step> [--doc-name <name>] [--did <did>] [--wid <wid>] [--render <out.png>]`:
   Uploads a local `.step` model directly to a new or existing personal Onshape Document,
   waits for cloud translation, and optionally renders a cloud `shadedviews` isometric PNG.
6. `onshape-export --did <did> --wid <wid> --eid <eid> --format <STEP|PARASOLID|STL> --out <path>`:
   Uses the Direct Onshape REST API (with 307 redirect HMAC re-signing, zero FeatureScript MCP quota).
"""

import argparse
import base64
import hashlib
import hmac
import io
import json
import math
import os
import random
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import shutil

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def _load_workspace_dotenv(dotenv_path: Path) -> None:
    """Load key=value pairs from a local `.env` file without overriding active env vars."""
    if not dotenv_path.is_file():
        return
    try:
        for raw_line in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass


_load_workspace_dotenv(WORKSPACE_ROOT / ".env")


def _detect_freecad_cmd() -> str:
    env_cmd = os.environ.get("FREECAD_CMD", "").strip()
    if env_cmd and Path(env_cmd).is_file():
        return env_cmd
    candidates = [
        r"C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe",
        r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
        "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd",
        shutil.which("FreeCADCmd") or "",
        shutil.which("freecadcmd") or "",
    ]
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        prog_dir = Path(local_app) / "Programs"
        if prog_dir.is_dir():
            for d in sorted(prog_dir.glob("FreeCAD*"), reverse=True):
                candidates.insert(2, str(d / "bin" / "FreeCADCmd.exe"))
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
    return r"C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe"


def _detect_build123d_python() -> str:
    win_py = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"
    posix_py = WORKSPACE_ROOT / ".venv" / "bin" / "python"
    if win_py.is_file():
        return str(win_py)
    if posix_py.is_file():
        return str(posix_py)
    return sys.executable


FREECAD_CMD = _detect_freecad_cmd()
COTS_LIBRARY = WORKSPACE_ROOT / "cots"
BUILD123D_PYTHON = _detect_build123d_python()

# Onshape Direct REST API keys (loaded from environment or gitignored .env)
ONSHAPE_ACCESS_KEY = os.environ.get("ONSHAPE_ACCESS_KEY", "").strip()
ONSHAPE_SECRET_KEY = os.environ.get("ONSHAPE_SECRET_KEY", "").strip()

# Distinct engineering palette for multi-part renders
PALETTE_RGB = [
    (58, 122, 218),   # Anodized Cobalt Blue
    (224, 72, 62),    # Crimson Red
    (46, 164, 92),    # Emerald Green
    (238, 174, 24),   # TiN Gold
    (152, 84, 184),   # Violet Purple
    (42, 168, 198),   # Cerulean Cyan
    (230, 126, 34),   # Safety Orange
    (140, 156, 170),  # Machined 6061 Aluminum
    (26, 188, 156),   # Turquoise Teal
    (95, 110, 128),   # Gunmetal Steel
]

# Right-handed CAD camera orientations:
# toward = (sin(yaw)*cos(pitch), -cos(yaw)*cos(pitch), sin(pitch))
STANDARD_VIEWS = [
    ("iso_top", -45.0, 35.264, "ISO TOP (+X-Y+Z)"),
    ("iso_bottom", 135.0, -35.264, "ISO BOTTOM (-X+Y-Z)"),
    ("top", 0.0, 90.0, "TOP ORTHO (+Z)"),
    ("front", 0.0, 0.0, "FRONT ORTHO (-Y)"),
    ("left", -90.0, 0.0, "LEFT ORTHO (-X)"),
]


def run_freecad_script(script_code: str) -> str:
    """Executes a Python snippet inside FreeCAD 1.0 headless interpreter."""
    if not Path(FREECAD_CMD).exists():
        raise FileNotFoundError(f"FreeCADCmd.exe not found at {FREECAD_CMD}")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
        tmp.write(script_code)
        tmp_path = tmp.name
    try:
        res = subprocess.run(
            [FREECAD_CMD, tmp_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"FreeCADCmd failed ({res.returncode}):\n{res.stderr}\n{res.stdout}")
        return res.stdout
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def run_build123d_script(
    script_path: Path,
    args: list[str] | None = None,
    auto_solidcraft: bool = True,
) -> str:
    """
    Executes a parametric build123d Python script using the repository .venv.
    When `auto_solidcraft=True` (default), automatically compiles any produced
    project `.step`/`.step.gz` with a companion `*_constraints.json` into a
    jointed, motion-coupled `.sc` assembly + BOM + verification JSON + motion GIF.
    """
    script_path = Path(script_path).resolve()
    if not script_path.exists():
        raise FileNotFoundError(f"build123d script not found: {script_path}")
    python_exe = BUILD123D_PYTHON if Path(BUILD123D_PYTHON).exists() else sys.executable
    env = os.environ.copy()
    # Ensure tools/ is importable from any build123d script (e.g. `from tools.ag_cad_helpers import ...`)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(WORKSPACE_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
    cmd = [python_exe, str(script_path)] + (args or [])
    t0 = time.time()
    res = subprocess.run(
        cmd,
        cwd=str(script_path.parent),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"build123d script failed ({res.returncode}):\n{res.stderr}\n{res.stdout}")
    out_lines = [res.stdout.rstrip()] if res.stdout.strip() else []

    if auto_solidcraft:
        proj_root = script_path.parent.parent if script_path.parent.name == "src" else script_path.parent
        cad_dir = proj_root / "cad"
        if cad_dir.is_dir():
            from tools.solidcraft_compiler import compile_project_to_solidcraft

            for cpath in sorted(cad_dir.rglob("*_constraints.json")):
                stem = cpath.name[: -len("_constraints.json")]
                step_cand = next(
                    (
                        p
                        for p in [
                            cpath.parent / f"{stem}.step",
                            cpath.parent / f"{stem}.stp",
                            cpath.parent / f"{stem}.step.gz",
                        ]
                        if p.is_file() and p.stat().st_mtime >= t0 - 5.0
                    ),
                    None,
                )
                if step_cand is not None:
                    sc_rep = compile_project_to_solidcraft(step_cand, constraints_path=cpath)
                    out_lines.append(
                        f"[AG-SOLIDCRAFT] Compiled {sc_rep['assembly_sc_path']} "
                        f"({sc_rep['status']}, {sc_rep['joints_created']} joints, "
                        f"{sc_rep['cots_components_tagged']} COTS tagged, {sc_rep['total_mass_kg']} kg)"
                    )

    return "\n".join(out_lines)


# Shared FreeCAD snippet to load .FCStd or XCAF .step with preserved part labels
_FREECAD_LOAD_ITEMS_SNIPPET = """
def _load_named_solids(p: Path):
    ext = p.suffix.lower()
    if ext == '.fcstd':
        doc = App.openDocument(str(p))
        doc.recompute()
    elif ext in ('.step', '.stp'):
        doc = App.newDocument('StepAudit')
        import Import
        Import.insert(str(p), doc.Name)
        doc.recompute()
    else:
        doc = None

    raw_items = []
    if doc is not None:
        # 1. Prefer direct top-level children of a root App::Part assembly container
        #    so multi-solid COTS sub-assemblies (e.g. a 66-solid motor) stay grouped
        #    as one logical component instead of exploding into 66 separate leaf solids.
        roots = [o for o in doc.Objects if o.TypeId == 'App::Part' and not getattr(o, 'InList', [])]
        if len(roots) == 1:
            top_children = [
                c for c in getattr(roots[0], 'OutList', [])
                if c.TypeId not in ('App::Origin',) and not c.TypeId.startswith('App::DocumentObjectGroup')
            ]
            if len(top_children) >= 2:
                for child in top_children:
                    if hasattr(child, 'Visibility') and child.Visibility is False and ext == '.fcstd':
                        continue
                    try:
                        s = Part.getShape(child)
                    except Exception:
                        s = getattr(child, 'Shape', None)
                    if s is not None and not s.isNull() and len(s.Solids) > 0 and s.Volume > 1e-3:
                        raw_items.append((child.Name, getattr(child, 'Label', child.Name), s))

        # 2. Fallback: collect leaf Part::Feature objects (skipping App::Part and Group containers)
        if not raw_items:
            for obj in doc.Objects:
                if hasattr(obj, 'Shape') and obj.Shape and not obj.Shape.isNull():
                    if obj.TypeId.startswith('App::DocumentObjectGroup') or obj.TypeId in ('App::Part', 'App::Origin'):
                        continue
                    if hasattr(obj, 'Visibility') and obj.Visibility is False and ext == '.fcstd':
                        continue
                    s = obj.Shape
                    if len(s.Solids) > 0 and s.Volume > 1e-3:
                        raw_items.append((obj.Name, getattr(obj, 'Label', obj.Name), s))
        if not raw_items:
            for obj in doc.Objects:
                if hasattr(obj, 'Shape') and obj.Shape and not obj.Shape.isNull():
                    if obj.TypeId.startswith('App::DocumentObjectGroup') or obj.TypeId in ('App::Part', 'App::Origin'):
                        continue
                    s = obj.Shape
                    if len(s.Solids) > 0 and s.Volume > 1e-3:
                        raw_items.append((obj.Name, getattr(obj, 'Label', obj.Name), s))
    if not raw_items:
        shape = Part.read(str(p))
        raw_items = [(f'Solid_{i+1}', f'{p.stem}_Solid_{i+1}', sol) for i, sol in enumerate(shape.Solids) if sol.Volume > 1e-3]

    # Unpack single compound if it wraps multiple distinct solids
    if len(raw_items) == 1 and len(raw_items[0][2].Solids) > 1:
        base_name, base_label, comp_shape = raw_items[0]
        raw_items = [
            (f'{base_name}_S{i+1}', f'{base_label}_S{i+1}', sol)
            for i, sol in enumerate(comp_shape.Solids)
            if sol.Volume > 1e-3
        ]
    return raw_items
"""


def _load_ocp_named_solids(cad_path: Path) -> list[tuple[str, str, object]]:
    """
    Fast in-process OpenCASCADE 8.0 (`build123d.import_step`) XCAF STEP loader.
    Preserves top-level component names & multi-solid COTS grouping without spawning FreeCADCmd.exe.
    Supports `.step`, `.stp`, and `.step.gz`.
    """
    import gzip
    from build123d import import_step
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    actual_path = Path(cad_path).resolve()
    tmp_unzipped: Path | None = None
    if actual_path.name.lower().endswith(".step.gz"):
        with tempfile.NamedTemporaryFile("wb", suffix=".step", delete=False) as tf:
            with gzip.open(actual_path, "rb") as gz:
                tf.write(gz.read())
            tmp_unzipped = Path(tf.name)
        actual_path = tmp_unzipped

    def _vol_mm3(tds) -> float:
        gp = GProp_GProps()
        BRepGProp.VolumeProperties_s(tds, gp)
        return abs(float(gp.Mass()))

    try:
        root = import_step(str(actual_path))
        raw_items: list[tuple[str, str, object]] = []
        children = list(getattr(root, "children", []) or [])
        if len(children) >= 2:
            for idx, ch in enumerate(children, 1):
                sols = list(ch.solids())
                if sols and _vol_mm3(ch.wrapped) > 1e-3:
                    lbl = str(getattr(ch, "label", "") or "").strip() or f"{cad_path.stem}_Part_{idx}"
                    raw_items.append((f"Part_{idx}", lbl, ch.wrapped))
        if not raw_items:
            sols = list(root.solids())
            base_lbl = str(getattr(root, "label", "") or "").strip() or cad_path.stem
            if len(sols) == 1 and _vol_mm3(sols[0].wrapped) > 1e-3:
                raw_items.append((base_lbl, base_lbl, sols[0].wrapped))
            elif len(sols) > 1:
                for i, sol in enumerate(sols, 1):
                    if _vol_mm3(sol.wrapped) > 1e-3:
                        raw_items.append((f"{base_lbl}_S{i}", f"{base_lbl}_S{i}", sol.wrapped))
        return raw_items
    finally:
        if tmp_unzipped is not None:
            tmp_unzipped.unlink(missing_ok=True)


def _ocp_shape_faces_count(tds) -> int:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    cnt = 0
    exp = TopExp_Explorer(tds, TopAbs_FACE)
    while exp.More():
        cnt += 1
        exp.Next()
    return cnt


def _ocp_shape_solids(tds) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    cast_solid = getattr(TopoDS, "Solid", None) or getattr(TopoDS, "Solid_s")
    out = []
    exp = TopExp_Explorer(tds, TopAbs_SOLID)
    while exp.More():
        out.append(cast_solid(exp.Current()))
        exp.Next()
    return out


def _ocp_tight_bbox(tds) -> tuple[float, float, float, float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    BRepMesh_IncrementalMesh(tds, 0.25, False, 0.35, True)
    box = Bnd_Box()
    BRepBndLib.Add_s(tds, box, True)
    if box.IsVoid():
        BRepBndLib.Add_s(tds, box, False)
    pmin = box.CornerMin()
    pmax = box.CornerMax()
    return (float(pmin.X()), float(pmin.Y()), float(pmin.Z()), float(pmax.X()), float(pmax.Y()), float(pmax.Z()))


def _inspect_cad_ocp(
    cad_path: Path,
    check_interference: bool = False,
    clearance_threshold: float = 0.0,
) -> dict:
    """Fast in-process OCP 8.0 / trimesh audit matching FreeCAD's exact JSON schema."""
    import trimesh
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    if cad_path.suffix.lower() == ".stl":
        m = trimesh.load(str(cad_path), force="mesh")
        if isinstance(m, trimesh.Scene):
            m = trimesh.util.concatenate(list(m.geometry.values()))
        bmin = m.bounds[0]
        bmax = m.bounds[1]
        ext = bmax - bmin
        is_wt = bool(m.is_watertight)
        # Check non-manifold edges: any edge shared by >2 faces
        edges_sorted = np.sort(m.edges, axis=1)
        _, counts = np.unique(edges_sorted, axis=0, return_counts=True)
        has_nm = bool(np.any(counts > 2))
        valid_solid = is_wt and not has_nm
        vol = abs(float(m.volume)) if is_wt else 0.0
        part_entry = {
            "name": cad_path.stem,
            "label": cad_path.stem,
            "valid": valid_solid,
            "is_watertight": is_wt,
            "has_non_manifolds": has_nm,
            "facet_count": int(len(m.faces)),
            "solid_count": 1 if valid_solid else 0,
            "volume_mm3": round(vol, 2),
            "bbox_min": [round(float(bmin[0]), 2), round(float(bmin[1]), 2), round(float(bmin[2]), 2)],
            "bbox_max": [round(float(bmax[0]), 2), round(float(bmax[1]), 2), round(float(bmax[2]), 2)],
            "size_mm": [round(float(ext[0]), 2), round(float(ext[1]), 2), round(float(ext[2]), 2)],
        }
        return {
            "file": str(cad_path),
            "part_count": 1,
            "invalid_parts": [] if valid_solid else [cad_path.stem],
            "total_volume_cm3": round(vol / 1000.0, 2),
            "overall_envelope_mm": part_entry["size_mm"],
            "interferences": [],
            "clearance_warnings": [],
            "closest_pairs": [],
            "parts": [part_entry],
        }

    def _vol(sh) -> float:
        gp = GProp_GProps()
        BRepGProp.VolumeProperties_s(sh, gp)
        return abs(float(gp.Mass()))

    raw_items = _load_ocp_named_solids(cad_path)
    solids_out = []
    shapes_meta = []

    for name, label, s in raw_items:
        bb = _ocp_tight_bbox(s)
        sub_sols = _ocp_shape_solids(s)
        vol_mm3 = _vol(s)
        if len(sub_sols) == 1:
            is_val = bool(BRepCheck_Analyzer(sub_sols[0]).IsValid())
        else:
            is_val = len(sub_sols) > 0 and all(_vol(sol) > 0.0 for sol in sub_sols)
        solids_out.append(
            {
                "name": name,
                "label": label,
                "valid": is_val,
                "solid_count": len(sub_sols),
                "volume_mm3": round(vol_mm3, 2),
                "bbox_min": [round(bb[0], 2), round(bb[1], 2), round(bb[2], 2)],
                "bbox_max": [round(bb[3], 2), round(bb[4], 2), round(bb[5], 2)],
                "size_mm": [round(bb[3] - bb[0], 2), round(bb[4] - bb[1], 2), round(bb[5] - bb[2], 2)],
            }
        )
        shapes_meta.append((label, s, bb, sub_sols))

    def _box_pen(ba, bb) -> float:
        return min(
            min(ba[3], bb[3]) - max(ba[0], bb[0]),
            min(ba[4], bb[4]) - max(ba[1], bb[1]),
            min(ba[5], bb[5]) - max(ba[2], bb[2]),
        )

    def _box_dist(ba, bb) -> float:
        dx = max(0.0, max(ba[0] - bb[3], bb[0] - ba[3]))
        dy = max(0.0, max(ba[1] - bb[4], bb[1] - ba[4]))
        dz = max(0.0, max(ba[2] - bb[5], bb[2] - ba[5]))
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    interferences = []
    clearance_warnings = []
    closest_pairs = []
    clr_thresh = float(clearance_threshold)
    near_search_radius = max(clr_thresh, 5.0)

    if (check_interference or clr_thresh > 0.0) and len(shapes_meta) <= 80:
        for i in range(len(shapes_meta)):
            n1, s1, bb1, raw_s1 = shapes_meta[i]
            sols1 = [(sol, _ocp_tight_bbox(sol), _ocp_shape_faces_count(sol)) for sol in raw_s1 if _vol(sol) > 5.0] if len(raw_s1) > 1 else [(s1, bb1, _ocp_shape_faces_count(s1))]
            for j in range(i + 1, len(shapes_meta)):
                n2, s2, bb2, raw_s2 = shapes_meta[j]
                sols2 = [(sol, _ocp_tight_bbox(sol), _ocp_shape_faces_count(sol)) for sol in raw_s2 if _vol(sol) > 5.0] if len(raw_s2) > 1 else [(s2, bb2, _ocp_shape_faces_count(s2))]
                has_overlap = False

                if check_interference and _box_pen(bb1, bb2) > 0.02:
                    overlap_vol = 0.0
                    for sub1, sbb1, fc1 in sols1:
                        if _box_pen(sbb1, bb2) <= 0.02:
                            continue
                        for sub2, sbb2, fc2 in sols2:
                            if _box_pen(sbb1, sbb2) > 0.02:
                                if max(fc1, fc2) > 1200 or fc1 * fc2 > 25000:
                                    continue
                                try:
                                    op = BRepAlgoAPI_Common(sub1, sub2)
                                    if op.IsDone():
                                        cv = _vol(op.Shape())
                                        if cv > 0.5:
                                            overlap_vol += cv
                                except Exception:
                                    pass
                    if overlap_vol > 5.0:
                        has_overlap = True
                        interferences.append({"part_a": n1, "part_b": n2, "overlap_mm3": round(overlap_vol, 2)})

                if not has_overlap and _box_dist(bb1, bb2) <= near_search_radius + 0.5:
                    cand_pairs = []
                    for sub1, sbb1, fc1 in sols1:
                        if _box_dist(sbb1, bb2) > near_search_radius + 0.5:
                            continue
                        for sub2, sbb2, fc2 in sols2:
                            d_box = _box_dist(sbb1, sbb2)
                            if d_box <= near_search_radius + 0.5:
                                cand_pairs.append((d_box, sub1, sub2, fc1, fc2))
                    cand_pairs.sort(key=lambda x: x[0])
                    best_dist = None
                    best_pts = None
                    for d_box, sub1, sub2, fc1, fc2 in cand_pairs[:12]:
                        if best_dist is not None and d_box >= best_dist - 1e-4:
                            break
                        if fc1 * fc2 > 4000 or max(fc1, fc2) > 900:
                            continue
                        try:
                            dss = BRepExtrema_DistShapeShape(sub1, sub2)
                            if dss.IsDone() and dss.NbSolution() >= 1:
                                dist = float(dss.Value())
                                if best_dist is None or dist < best_dist:
                                    best_dist = dist
                                    p_a = dss.PointOnShape1(1)
                                    p_b = dss.PointOnShape2(1)
                                    best_pts = (p_a, p_b)
                                    if best_dist <= 1e-4:
                                        break
                        except Exception:
                            pass
                    if best_dist is not None and best_pts is not None:
                        p_a, p_b = best_pts
                        pair_rec = {
                            "part_a": n1,
                            "part_b": n2,
                            "min_distance_mm": round(best_dist, 4),
                            "witness_a_mm": [round(float(p_a.X()), 2), round(float(p_a.Y()), 2), round(float(p_a.Z()), 2)],
                            "witness_b_mm": [round(float(p_b.X()), 2), round(float(p_b.Y()), 2), round(float(p_b.Z()), 2)],
                        }
                        closest_pairs.append(pair_rec)
                        if clr_thresh > 0.0 and 1e-4 < best_dist < clr_thresh:
                            clearance_warnings.append(pair_rec)

    closest_pairs.sort(key=lambda x: x["min_distance_mm"])
    clearance_warnings.sort(key=lambda x: x["min_distance_mm"])

    all_xmin = min((x["bbox_min"][0] for x in solids_out), default=0.0)
    all_ymin = min((x["bbox_min"][1] for x in solids_out), default=0.0)
    all_zmin = min((x["bbox_min"][2] for x in solids_out), default=0.0)
    all_xmax = max((x["bbox_max"][0] for x in solids_out), default=0.0)
    all_ymax = max((x["bbox_max"][1] for x in solids_out), default=0.0)
    all_zmax = max((x["bbox_max"][2] for x in solids_out), default=0.0)

    return {
        "file": str(cad_path),
        "part_count": len(solids_out),
        "invalid_parts": [x["label"] for x in solids_out if not x["valid"]],
        "total_volume_cm3": round(sum(x["volume_mm3"] for x in solids_out) / 1000.0, 2),
        "overall_envelope_mm": [
            round(all_xmax - all_xmin, 2),
            round(all_ymax - all_ymin, 2),
            round(all_zmax - all_zmin, 2),
        ],
        "interferences": interferences,
        "clearance_warnings": clearance_warnings,
        "closest_pairs": closest_pairs[:15],
        "parts": solids_out,
    }


def _extract_tessellation_ocp(cad_path: Path, deflection: float = 0.25) -> dict:
    """Fast in-process OCP 8.0 / trimesh triangle mesh extractor for `render_cad`."""
    import trimesh
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    if cad_path.suffix.lower() == ".stl":
        m = trimesh.load(str(cad_path), force="mesh")
        if isinstance(m, trimesh.Scene):
            m = trimesh.util.concatenate(list(m.geometry.values()))
        return {
            "file": str(cad_path),
            "parts": [{"label": cad_path.stem, "vertices": np.round(m.vertices, 3).tolist(), "faces": m.faces.tolist()}],
        }

    raw = _load_ocp_named_solids(cad_path)
    parts_out = []
    for _, lbl, sh in raw:
        f_cnt = _ocp_shape_faces_count(sh)
        defl = max(float(deflection), 0.85) if f_cnt > 1000 else float(deflection)
        BRepMesh_IncrementalMesh(sh, defl, False, 0.35, True)
        v_list = []
        f_list = []
        v_off = 0
        cast_face = getattr(TopoDS, "Face", None) or getattr(TopoDS, "Face_s")
        exp = TopExp_Explorer(sh, TopAbs_FACE)
        while exp.More():
            face = cast_face(exp.Current())
            loc = TopLoc_Location()
            tri_h = BRep_Tool.Triangulation_s(face, loc)
            if tri_h is not None:
                trsf = loc.Transformation()
                nb_n = tri_h.NbNodes()
                nb_t = tri_h.NbTriangles()
                fv = np.empty((nb_n, 3), dtype=np.float64)
                for ni in range(1, nb_n + 1):
                    pt = tri_h.Node(ni).Transformed(trsf)
                    fv[ni - 1] = (pt.X(), pt.Y(), pt.Z())
                rev = face.Orientation() == TopAbs_REVERSED
                ft = np.empty((nb_t, 3), dtype=np.int32)
                for ti in range(1, nb_t + 1):
                    t = tri_h.Triangle(ti)
                    n1, n2, n3 = t.Get()
                    ft[ti - 1] = (n1 - 1 + v_off, n3 - 1 + v_off, n2 - 1 + v_off) if rev else (n1 - 1 + v_off, n2 - 1 + v_off, n3 - 1 + v_off)
                v_list.append(fv)
                f_list.append(ft)
                v_off += nb_n
            exp.Next()
        if v_list and f_list:
            parts_out.append(
                {
                    "label": lbl,
                    "vertices": np.vstack(v_list),
                    "faces": np.vstack(f_list),
                }
            )
    return {"file": str(cad_path), "parts": parts_out}


def _export_cad_ocp(
    cad_path: Path,
    step_path: Path | None = None,
    stl_path: Path | None = None,
    linear_deflection: float = 0.1,
) -> dict:
    """Fast in-process OCP 8.0 STEP AP214 and watertight STL exporter."""
    import gzip
    from build123d import export_step, export_stl, import_step
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    actual_path = Path(cad_path).resolve()
    tmp_unzipped: Path | None = None
    if actual_path.name.lower().endswith(".step.gz"):
        with tempfile.NamedTemporaryFile("wb", suffix=".step", delete=False) as tf:
            with gzip.open(actual_path, "rb") as gz:
                tf.write(gz.read())
            tmp_unzipped = Path(tf.name)
        actual_path = tmp_unzipped

    try:
        root = import_step(str(actual_path))
        if step_path is not None:
            sp = Path(step_path).resolve()
            sp.parent.mkdir(parents=True, exist_ok=True)
            export_step(root, str(sp))
        if stl_path is not None:
            stlp = Path(stl_path).resolve()
            stlp.parent.mkdir(parents=True, exist_ok=True)
            export_stl(root, str(stlp), tolerance=float(linear_deflection), angular_tolerance=0.35)

        gp = GProp_GProps()
        BRepGProp.VolumeProperties_s(root.wrapped, gp)
        sol_cnt = len(list(root.solids()))
        return {
            "source": str(cad_path),
            "solid_count": sol_cnt,
            "volume_cm3": round(abs(float(gp.Mass())) / 1000.0, 2),
            "step_path": str(Path(step_path).resolve()) if step_path else None,
            "stl_path": str(Path(stl_path).resolve()) if stl_path else None,
        }
    finally:
        if tmp_unzipped is not None:
            tmp_unzipped.unlink(missing_ok=True)


def inspect_cad(
    cad_path: Path,
    check_interference: bool = False,
    clearance_threshold: float = 0.0,
    engine: str = "auto",
) -> dict:
    """Audits all solids in a .FCStd, .step, or .stl file locally (in-process OCP 8.0 default for STEP/STL, FreeCAD 1.0 for .FCStd)."""
    cad_path = Path(cad_path).resolve()
    if not cad_path.exists():
        raise FileNotFoundError(f"CAD file not found: {cad_path}")
    eng = engine.strip().lower()
    is_fcstd = cad_path.suffix.lower() == ".fcstd"
    if eng != "freecad" and not is_fcstd:
        return _inspect_cad_ocp(cad_path, check_interference=check_interference, clearance_threshold=clearance_threshold)
    out_json = cad_path.parent / f".ag_audit_{cad_path.stem}_{os.getpid()}.json"
    script = f"""
import json
from pathlib import Path
import FreeCAD as App
import Part
import Mesh

{_FREECAD_LOAD_ITEMS_SNIPPET}

p = Path(r'''{cad_path}''')
if p.suffix.lower() == '.stl':
    m = Mesh.Mesh(str(p))
    bb = m.BoundBox
    valid_solid = bool(m.isSolid()) and not bool(m.hasNonManifolds())
    part_entry = {{
        'name': p.stem,
        'label': p.stem,
        'valid': valid_solid,
        'is_watertight': bool(m.isSolid()),
        'has_non_manifolds': bool(m.hasNonManifolds()),
        'facet_count': int(m.CountFacets),
        'solid_count': 1 if valid_solid else 0,
        'volume_mm3': round(float(m.Volume), 2),
        'bbox_min': [round(bb.XMin, 2), round(bb.YMin, 2), round(bb.ZMin, 2)],
        'bbox_max': [round(bb.XMax, 2), round(bb.YMax, 2), round(bb.ZMax, 2)],
        'size_mm': [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
    }}
    report = {{
        'file': str(p),
        'part_count': 1,
        'invalid_parts': [] if valid_solid else [p.stem],
        'total_volume_cm3': round(float(m.Volume) / 1000.0, 2),
        'overall_envelope_mm': part_entry['size_mm'],
        'interferences': [],
        'clearance_warnings': [],
        'closest_pairs': [],
        'parts': [part_entry],
    }}
    Path(r'''{out_json}''').write_text(json.dumps(report, indent=2), encoding='utf-8')
else:
    raw_items = _load_named_solids(p)
    solids = []
    shapes = []
    for name, label, s in raw_items:
        try:
            bb = s.optimalBoundingBox(False) if len(s.Faces) <= 500 else s.BoundBox
        except Exception:
            bb = s.BoundBox
        is_val = bool(s.isValid()) if len(s.Solids) == 1 else (len(s.Solids) > 0 and all(sol.Volume > 0 for sol in s.Solids))
        solids.append({{
            'name': name,
            'label': label,
            'valid': is_val,
            'solid_count': len(s.Solids),
            'volume_mm3': round(s.Volume, 2),
            'bbox_min': [round(bb.XMin, 2), round(bb.YMin, 2), round(bb.ZMin, 2)],
            'bbox_max': [round(bb.XMax, 2), round(bb.YMax, 2), round(bb.ZMax, 2)],
            'size_mm': [round(bb.XLength, 2), round(bb.YLength, 2), round(bb.ZLength, 2)],
        }})
        shapes.append((label, s, bb))

    interferences = []
    clearance_warnings = []
    closest_pairs = []
    check_ix = {bool(check_interference)}
    clr_thresh = float({float(clearance_threshold)})
    near_search_radius = max(clr_thresh, 5.0)

    def _aabb_penetration(ba, bb):
        dx = min(ba.XMax, bb.XMax) - max(ba.XMin, bb.XMin)
        dy = min(ba.YMax, bb.YMax) - max(ba.YMin, bb.YMin)
        dz = min(ba.ZMax, bb.ZMax) - max(ba.ZMin, bb.ZMin)
        return min(dx, dy, dz)

    def _aabb_min_dist(ba, bb):
        dx = max(0.0, max(ba.XMin - bb.XMax, bb.XMin - ba.XMax))
        dy = max(0.0, max(ba.YMin - bb.YMax, bb.YMin - ba.YMax))
        dz = max(0.0, max(ba.ZMin - bb.ZMax, bb.ZMin - ba.ZMax))
        return (dx * dx + dy * dy + dz * dz) ** 0.5

    if (check_ix or clr_thresh > 0.0) and len(shapes) <= 80:
        for i in range(len(shapes)):
            n1, s1, bb1 = shapes[i]
            sols1 = [sol for sol in s1.Solids if sol.Volume > 5.0] if len(s1.Solids) > 1 else [s1]
            for j in range(i + 1, len(shapes)):
                n2, s2, bb2 = shapes[j]
                sols2 = [sol for sol in s2.Solids if sol.Volume > 5.0] if len(s2.Solids) > 1 else [s2]
                has_overlap = False
                if check_ix and _aabb_penetration(bb1, bb2) > 0.02:
                    overlap_vol = 0.0
                    for sub1 in sols1:
                        sbb1 = sub1.BoundBox
                        if _aabb_penetration(sbb1, bb2) <= 0.02:
                            continue
                        obb1 = None
                        for sub2 in sols2:
                            if _aabb_penetration(sbb1, sub2.BoundBox) > 0.02:
                                try:
                                    if obb1 is None:
                                        obb1 = sub1.optimalBoundingBox(False) if len(sub1.Faces) <= 500 else sbb1
                                    if _aabb_penetration(obb1, bb2) <= 0.02:
                                        break
                                    obb2 = sub2.optimalBoundingBox(False) if len(sub2.Faces) <= 500 else sub2.BoundBox
                                    if _aabb_penetration(obb1, obb2) > 0.02:
                                        if max(len(sub1.Faces), len(sub2.Faces)) > 1200 or len(sub1.Faces) * len(sub2.Faces) > 25000:
                                            continue
                                        common = sub1.common(sub2)
                                        if common.Volume > 0.5:
                                            overlap_vol += common.Volume
                                except Exception:
                                    pass
                    if overlap_vol > 5.0:
                        has_overlap = True
                        interferences.append({{
                            'part_a': n1,
                            'part_b': n2,
                            'overlap_mm3': round(overlap_vol, 2),
                        }})
                if not has_overlap and _aabb_min_dist(bb1, bb2) <= near_search_radius + 0.5:
                    cand_pairs = []
                    for sub1 in sols1:
                        sbb1 = sub1.BoundBox
                        if _aabb_min_dist(sbb1, bb2) > near_search_radius + 0.5:
                            continue
                        for sub2 in sols2:
                            d_box = _aabb_min_dist(sbb1, sub2.BoundBox)
                            if d_box <= near_search_radius + 0.5:
                                cand_pairs.append((d_box, sub1, sub2))
                    cand_pairs.sort(key=lambda x: x[0])
                    best_dist = None
                    best_pts = None
                    for d_box, sub1, sub2 in cand_pairs[:12]:
                        if best_dist is not None and d_box >= best_dist - 1e-4:
                            break
                        if len(sub1.Faces) * len(sub2.Faces) > 2500 or max(len(sub1.Faces), len(sub2.Faces)) > 800:
                            continue
                        try:
                            dist, pts, _ = sub1.distToShape(sub2)
                            if len(pts) > 0 and (best_dist is None or dist < best_dist):
                                best_dist = float(dist)
                                best_pts = pts[0]
                                if best_dist <= 1e-4:
                                    break
                        except Exception:
                            pass
                    if best_dist is not None and best_pts is not None:
                        p_a, p_b = best_pts
                        pair_rec = {{
                            'part_a': n1,
                            'part_b': n2,
                            'min_distance_mm': round(best_dist, 4),
                            'witness_a_mm': [round(p_a.x, 2), round(p_a.y, 2), round(p_a.z, 2)],
                            'witness_b_mm': [round(p_b.x, 2), round(p_b.y, 2), round(p_b.z, 2)],
                        }}
                        closest_pairs.append(pair_rec)
                        if clr_thresh > 0.0 and 1e-4 < best_dist < clr_thresh:
                            clearance_warnings.append(pair_rec)

    closest_pairs.sort(key=lambda x: x['min_distance_mm'])
    clearance_warnings.sort(key=lambda x: x['min_distance_mm'])

    all_xmin = min((x['bbox_min'][0] for x in solids), default=0.0)
    all_ymin = min((x['bbox_min'][1] for x in solids), default=0.0)
    all_zmin = min((x['bbox_min'][2] for x in solids), default=0.0)
    all_xmax = max((x['bbox_max'][0] for x in solids), default=0.0)
    all_ymax = max((x['bbox_max'][1] for x in solids), default=0.0)
    all_zmax = max((x['bbox_max'][2] for x in solids), default=0.0)

    report = {{
        'file': str(p),
        'part_count': len(solids),
        'invalid_parts': [x['label'] for x in solids if not x['valid']],
        'total_volume_cm3': round(sum(x['volume_mm3'] for x in solids) / 1000.0, 2),
        'overall_envelope_mm': [
            round(all_xmax - all_xmin, 2),
            round(all_ymax - all_ymin, 2),
            round(all_zmax - all_zmin, 2),
        ],
        'interferences': interferences,
        'clearance_warnings': clearance_warnings,
        'closest_pairs': closest_pairs[:15],
        'parts': solids,
    }}
    Path(r'''{out_json}''').write_text(json.dumps(report, indent=2), encoding='utf-8')
"""
    try:
        run_freecad_script(script)
        return json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        out_json.unlink(missing_ok=True)


def export_cad(
    cad_path: Path,
    step_path: Path | None = None,
    stl_path: Path | None = None,
    linear_deflection: float = 0.1,
    engine: str = "auto",
) -> dict:
    """Exports STEP AP214 and/or watertight STL locally (in-process OCP 8.0 default for STEP, FreeCAD 1.0 for .FCStd)."""
    cad_path = Path(cad_path).resolve()
    if not cad_path.exists():
        raise FileNotFoundError(f"CAD file not found: {cad_path}")
    if step_path is None and stl_path is None:
        raise ValueError("Specify at least one of step_path or stl_path")

    eng = engine.strip().lower()
    if eng != "freecad" and cad_path.suffix.lower() != ".fcstd":
        return _export_cad_ocp(
            cad_path,
            step_path=step_path,
            stl_path=stl_path,
            linear_deflection=linear_deflection,
        )

    step_str = str(Path(step_path).resolve()) if step_path else ""
    stl_str = str(Path(stl_path).resolve()) if stl_path else ""
    if step_path:
        Path(step_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    if stl_path:
        Path(stl_path).resolve().parent.mkdir(parents=True, exist_ok=True)

    out_json = cad_path.parent / f".ag_export_{cad_path.stem}_{os.getpid()}.json"
    script = f"""
import json
from pathlib import Path
import FreeCAD as App
import Part
import Mesh
import MeshPart

{_FREECAD_LOAD_ITEMS_SNIPPET}

p = Path(r'''{cad_path}''')
raw_items = _load_named_solids(p)
shapes = [s for _, _, s in raw_items]
compound = Part.makeCompound(shapes) if len(shapes) > 1 else shapes[0]

step_target = r'''{step_str}'''
stl_target = r'''{stl_str}'''

if step_target:
    compound.exportStep(step_target)

if stl_target:
    mesh = MeshPart.meshFromShape(Shape=compound, LinearDeflection={float(linear_deflection)}, AngularDeflection=0.35, Relative=False)
    mesh.write(stl_target)

info = {{
    'source': str(p),
    'solid_count': len(compound.Solids),
    'volume_cm3': round(compound.Volume / 1000.0, 2),
    'step_path': step_target or None,
    'stl_path': stl_target or None,
}}
Path(r'''{out_json}''').write_text(json.dumps(info, indent=2), encoding='utf-8')
"""
    try:
        run_freecad_script(script)
        return json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        out_json.unlink(missing_ok=True)


def _extract_tessellation(cad_path: Path, deflection: float = 0.25, engine: str = "auto") -> dict:
    """Extracts triangle meshes per named solid from .FCStd, .step, or .stl (in-process OCP 8.0 default for STEP/STL)."""
    cad_path = Path(cad_path).resolve()
    eng = engine.strip().lower()
    if eng != "freecad" and cad_path.suffix.lower() != ".fcstd":
        return _extract_tessellation_ocp(cad_path, deflection=deflection)
    out_json = cad_path.parent / f".ag_mesh_{cad_path.stem}_{os.getpid()}.json"
    script = f"""
import json
from pathlib import Path
import FreeCAD as App
import Part
import Mesh

{_FREECAD_LOAD_ITEMS_SNIPPET}

p = Path(r'''{cad_path}''')
parts_out = []

if p.suffix.lower() == '.stl':
    m = Mesh.Mesh(str(p))
    topo = m.Topology
    verts = [[round(v.x, 3), round(v.y, 3), round(v.z, 3)] for v in topo[0]]
    faces = [list(f) for f in topo[1]]
    parts_out.append({{'label': p.stem, 'vertices': verts, 'faces': faces}})
else:
    raw = _load_named_solids(p)
    for _, lbl, sh in raw:
        defl = max({float(deflection)}, 0.85) if len(sh.Faces) > 1000 else {float(deflection)}
        pts, tris = sh.tessellate(defl)
        verts = [[round(v.x, 3), round(v.y, 3), round(v.z, 3)] for v in pts]
        faces = [list(t) for t in tris]
        if verts and faces:
            parts_out.append({{'label': lbl, 'vertices': verts, 'faces': faces}})

Path(r'''{out_json}''').write_text(json.dumps({{'file': str(p), 'parts': parts_out}}), encoding='utf-8')
"""
    try:
        run_freecad_script(script)
        return json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        out_json.unlink(missing_ok=True)


def _load_font(size: int, bold: bool = False):
    font_path = Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf")
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def _render_single_view(
    parts_data: list[dict],
    yaw_deg: float,
    pitch_deg: float,
    title: str,
    subtitle: str,
    width: int = 1000,
    height: int = 1000,
    section_clip_y: float | None = None,
) -> Image.Image:
    """
    True per-pixel NumPy Z-buffer rasterizer with silhouette/part-boundary and
    crease-angle edge detection (eliminates painter's algorithm sorting artifacts
    and internal cylinder tessellation lines).
    """
    W, H = width, height
    img = Image.new("RGB", (W, H), (245, 247, 250))
    draw = ImageDraw.Draw(img)

    grid_step = 80
    for gx in range(0, W, grid_step):
        draw.line([(gx, 0), (gx, H)], fill=(232, 236, 241), width=1)
    for gy in range(0, H, grid_step):
        draw.line([(0, gy), (W, gy)], fill=(232, 236, 241), width=1)

    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    right = np.array([math.cos(yaw), math.sin(yaw), 0.0], dtype=np.float64)
    up = np.array(
        [-math.sin(yaw) * math.sin(pitch), math.cos(yaw) * math.sin(pitch), math.cos(pitch)],
        dtype=np.float64,
    )
    toward = np.cross(right, up)
    t_norm = np.linalg.norm(toward)
    if t_norm > 1e-9:
        toward /= t_norm

    light_dir = np.array([-0.40, 0.50, 0.76], dtype=np.float64)
    light_dir /= np.linalg.norm(light_dir)

    u_min, u_max = float("inf"), float("-inf")
    v_min, v_max = float("inf"), float("-inf")

    projected_parts = []
    for idx, part in enumerate(parts_data):
        verts = np.asarray(part["vertices"], dtype=np.float64)
        faces = np.asarray(part["faces"], dtype=np.int32)
        if len(verts) == 0 or len(faces) == 0:
            continue
        u = verts @ right
        v = verts @ up
        d = verts @ toward
        u_min = min(u_min, float(u.min()))
        u_max = max(u_max, float(u.max()))
        v_min = min(v_min, float(v.min()))
        v_max = max(v_max, float(v.max()))
        projected_parts.append((idx, verts, faces, u, v, d))

    if not projected_parts:
        return img

    pad = 95
    header_h = 84
    avail_w = W - 2 * pad
    avail_h = H - header_h - 2 * pad
    span_u = max(u_max - u_min, 1e-3)
    span_v = max(v_max - v_min, 1e-3)
    scale = min(avail_w / span_u, avail_h / span_v)
    uc = 0.5 * (u_min + u_max)
    vc = 0.5 * (v_min + v_max)
    cx = W * 0.5
    cy = header_h + (H - header_h) * 0.5

    pixels = np.array(img, dtype=np.uint8)
    zbuf = np.full((H, W), -np.inf, dtype=np.float64)
    part_buf = np.full((H, W), -1, dtype=np.int32)
    norm_buf = np.zeros((H, W, 3), dtype=np.float32)

    for idx, verts, faces, u, v, d in projected_parts:
        base_rgb = np.array(PALETTE_RGB[idx % len(PALETTE_RGB)], dtype=np.float64)

        # Optional 3D section cut plane: discard triangles whose centroid has Y < section_clip_y
        if section_clip_y is not None:
            tri_y_mean = verts[faces, 1].mean(axis=1)
            keep_mask = tri_y_mean >= section_clip_y
            faces = faces[keep_mask]
            if len(faces) == 0:
                continue

        px = cx + scale * (u - uc)
        py = cy - scale * (v - vc)

        v0 = verts[faces[:, 0]]
        v1 = verts[faces[:, 1]]
        v2 = verts[faces[:, 2]]
        raw_normals = np.cross(v1 - v0, v2 - v0)
        n_len = np.linalg.norm(raw_normals, axis=1)

        for t_idx in range(len(faces)):
            nl = n_len[t_idx]
            if nl < 1e-9:
                continue
            normal = raw_normals[t_idx] / nl
            dot_view = float(np.dot(normal, toward))
            if section_clip_y is None and dot_view < -0.05:
                continue
            # Flip normal to face camera if back-facing (important for section cuts!)
            is_backface = dot_view < 0.0
            eff_normal = -normal if is_backface else normal

            f0, f1, f2 = faces[t_idx]
            ax, ay, az = px[f0], py[f0], d[f0]
            bx, by, bz = px[f1], py[f1], d[f1]
            cx_t, cy_t, cz_t = px[f2], py[f2], d[f2]

            left = max(0, int(math.floor(min(ax, bx, cx_t))))
            right_p = min(W - 1, int(math.ceil(max(ax, bx, cx_t))))
            top = max(header_h, int(math.floor(min(ay, by, cy_t))))
            bottom = min(H - 1, int(math.ceil(max(ay, by, cy_t))))
            if right_p < left or bottom < top:
                continue

            sub_z = zbuf[top : bottom + 1, left : right_p + 1]
            if max(az, bz, cz_t) <= float(sub_z.min()):
                continue

            den = (by - cy_t) * (ax - cx_t) + (cx_t - bx) * (ay - cy_t)
            if abs(den) < 1e-9:
                continue

            ndotl = abs(float(np.dot(eff_normal, light_dir)))
            ndotv = abs(dot_view)
            intensity = min(1.05, max(0.28, 0.36 + 0.46 * ndotl + 0.18 * ndotv))
            if section_clip_y is not None and is_backface:
                # Warm cross-hatch tint on interior cut walls in section view
                shade = tuple(int(min(255, c * intensity * 0.88 + 25)) for c in base_rgb)
            else:
                shade = tuple(int(min(255, c * intensity)) for c in base_rgb)

            xs, ys = np.meshgrid(
                np.arange(left, right_p + 1, dtype=np.float64) + 0.5,
                np.arange(top, bottom + 1, dtype=np.float64) + 0.5,
            )
            w0 = ((by - cy_t) * (xs - cx_t) + (cx_t - bx) * (ys - cy_t)) / den
            w1 = ((cy_t - ay) * (xs - cx_t) + (ax - cx_t) * (ys - cy_t)) / den
            w2 = 1.0 - w0 - w1

            z = w0 * az + w1 * bz + w2 * cz_t
            mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (z > sub_z)
            if np.any(mask):
                sub_z[mask] = z[mask]
                pixels[top : bottom + 1, left : right_p + 1][mask] = shade
                part_buf[top : bottom + 1, left : right_p + 1][mask] = idx
                norm_buf[top : bottom + 1, left : right_p + 1][mask] = eff_normal

    # Vectorized Silhouette + Sharp Crease Edge Detection (renders crisp CAD edges)
    edge_mask = np.zeros((H, W), dtype=bool)
    # 1. Part-ID / Silhouette boundaries
    edge_mask[1:, :] |= part_buf[1:, :] != part_buf[:-1, :]
    edge_mask[:, 1:] |= part_buf[:, 1:] != part_buf[:, :-1]
    # 2. Normal discontinuity crease lines (>32 degrees between adjacent visible pixels)
    both_fg_v = (part_buf[1:, :] >= 0) & (part_buf[:-1, :] >= 0)
    dot_v = np.sum(norm_buf[1:, :] * norm_buf[:-1, :], axis=2)
    edge_mask[1:, :] |= both_fg_v & (dot_v < 0.84)

    both_fg_h = (part_buf[:, 1:] >= 0) & (part_buf[:, :-1] >= 0)
    dot_h = np.sum(norm_buf[:, 1:] * norm_buf[:, :-1], axis=2)
    edge_mask[:, 1:] |= both_fg_h & (dot_h < 0.84)

    edge_mask[:header_h, :] = False
    pixels[edge_mask] = (pixels[edge_mask].astype(np.int32) * 35 // 100).astype(np.uint8)

    out_img = Image.fromarray(pixels)
    odraw = ImageDraw.Draw(out_img)
    odraw.rectangle([(0, 0), (W, header_h)], fill=(28, 42, 58))
    odraw.text((26, 14), title, font=_load_font(24, bold=True), fill=(255, 255, 255))
    odraw.text((26, 48), subtitle, font=_load_font(15, bold=False), fill=(180, 200, 220))
    return out_img


def render_cad(
    cad_path: Path,
    out_dir: Path,
    prefix: str | None = None,
    width: int = 1000,
    height: int = 1000,
    engine: str = "auto",
) -> dict[str, str]:
    """
    Tessellates a CAD model (in-process OCP 8.0 default for STEP/STL, FreeCAD 1.0 for .FCStd)
    and renders 5 standard views + a 3D mid-plane cutaway section view + a 6-panel engineering contact sheet.
    """
    cad_path = Path(cad_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or cad_path.stem

    mesh_data = _extract_tessellation(cad_path, engine=engine)
    parts = mesh_data["parts"]
    if not parts:
        raise RuntimeError(f"No renderable solid geometry found in {cad_path}")

    all_pts = np.concatenate([np.asarray(p["vertices"], dtype=np.float64) for p in parts], axis=0)
    mins = all_pts.min(axis=0)
    maxs = all_pts.max(axis=0)
    spans = maxs - mins
    mid_y = float(0.5 * (mins[1] + maxs[1]))
    subtitle = (
        f"{cad_path.name} | Solids: {len(parts)} | "
        f"Envelope: {spans[0]:.1f} x {spans[1]:.1f} x {spans[2]:.1f} mm"
    )

    rendered_paths: dict[str, str] = {}
    view_images: dict[str, Image.Image] = {}

    for view_key, yaw, pitch, view_label in STANDARD_VIEWS:
        im = _render_single_view(
            parts,
            yaw_deg=yaw,
            pitch_deg=pitch,
            title=f"{stem} — {view_label}",
            subtitle=subtitle,
            width=width,
            height=height,
        )
        out_file = out_dir / f"{stem}_{view_key}.png"
        im.save(str(out_file))
        rendered_paths[view_key] = str(out_file)
        view_images[view_key] = im

    # 6th View: 3D Mid-Plane Cutaway Section View (slicing Y >= mid_y)
    sec_im = _render_single_view(
        parts,
        yaw_deg=-35.0,
        pitch_deg=30.0,
        title=f"{stem} — SECTION CUT (Y >= {mid_y:.1f} mm)",
        subtitle=subtitle,
        width=width,
        height=height,
        section_clip_y=mid_y,
    )
    sec_file = out_dir / f"{stem}_section_iso.png"
    sec_im.save(str(sec_file))
    rendered_paths["section_iso"] = str(sec_file)
    view_images["section_iso"] = sec_im

    # Build 6-panel engineering contact sheet (3x2 grid + bottom legend bar)
    tile_w, tile_h = 600, 600
    legend_h = 120
    sheet_w, sheet_h = tile_w * 3 + 80, tile_h * 2 + 115 + legend_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), (235, 239, 244))
    sdraw = ImageDraw.Draw(sheet)
    sdraw.rectangle([(0, 0), (sheet_w, 70)], fill=(20, 32, 46))
    sdraw.text(
        (25, 18),
        f"AG 6-VIEW + SECTION ENGINEERING AUDIT — {subtitle}",
        font=_load_font(24, bold=True),
        fill=(255, 255, 255),
    )

    grid_positions = [
        ("iso_top", 20, 85),
        ("top", 20 + tile_w + 20, 85),
        ("front", 20 + (tile_w + 20) * 2, 85),
        ("iso_bottom", 20, 85 + tile_h + 20),
        ("left", 20 + tile_w + 20, 85 + tile_h + 20),
        ("section_iso", 20 + (tile_w + 20) * 2, 85 + tile_h + 20),
    ]
    for vkey, gx, gy in grid_positions:
        thumb = view_images[vkey].resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (gx, gy))

    # Footer Part Legend Bar
    ly = 85 + tile_h * 2 + 35
    sdraw.rectangle([(20, ly), (sheet_w - 20, sheet_h - 15)], fill=(255, 255, 255), outline=(200, 210, 220), width=2)
    sdraw.text((35, ly + 12), "NAMED B-REP SOLIDS (XCAF STEP / FCStd LEGEND):", font=_load_font(17, bold=True), fill=(28, 42, 58))
    for p_idx, p_info in enumerate(parts[:10]):
        col = PALETTE_RGB[p_idx % len(PALETTE_RGB)]
        col_x = 35 + (p_idx % 5) * 360
        row_y = ly + 42 + (p_idx // 5) * 30
        sdraw.rectangle([(col_x, row_y + 2), (col_x + 20, row_y + 18)], fill=col, outline=(40, 40, 40))
        sdraw.text((col_x + 28, row_y), str(p_info["label"])[:36], font=_load_font(15), fill=(40, 50, 60))

    overview_path = out_dir / f"{stem}_overview_5view.png"
    sheet.save(str(overview_path))
    rendered_paths["overview_5view"] = str(overview_path)
    return rendered_paths


def sign_onshape_headers(
    method: str,
    path: str,
    query: str = "",
    content_type: str = "application/json",
    accept: str = "application/json",
) -> dict:
    access_key = ONSHAPE_ACCESS_KEY or os.environ.get("ONSHAPE_ACCESS_KEY", "").strip()
    secret_key = ONSHAPE_SECRET_KEY or os.environ.get("ONSHAPE_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError(
            "ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY are not configured. "
            "Set them in a local gitignored `.env` file (see `.env.example`) or use session-authenticated browser sync."
        )
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=25))
    date = formatdate(time.time(), usegmt=True)
    str_to_sign = f"{method}\n{nonce}\n{date}\n{content_type}\n{path}\n{query}\n".lower()
    sig = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), str_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "Authorization": f"On {access_key}:HmacSHA256:{sig}",
        "Date": date,
        "On-Nonce": nonce,
        "Content-Type": content_type,
        "Accept": accept,
    }


def _call_onshape_json(
    method: str,
    path: str,
    query: str = "",
    body: dict | None = None,
    max_retries: int = 4,
) -> dict:
    """
    Executes an HMAC-SHA256 signed Onshape REST request with automatic retry on
    transient gateway/rate-limit errors (429, 502, 503, 504) and 204 No Content support.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    url = f"https://cad.onshape.com{path}" + (f"?{query}" if query else "")
    for attempt in range(max_retries):
        headers = sign_onshape_headers(method, path, query)
        req = urllib.request.Request(url, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8").strip()
                return json.loads(raw) if raw else {"status": resp.status}
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt + 1 < max_retries:
                time.sleep(1.5 * (2**attempt))
                continue
            raise
    return {}


def make_assembly_mate_connector_payload(
    name: str,
    occ_path: list[str],
    tx_mm: float = 0.0,
    ty_mm: float = 0.0,
    tz_mm: float = 0.0,
    rot_axis: str = "ABOUT_Z",
    rot_deg: float = 0.0,
    flip_primary: bool = False,
    secondary_axis: str = "PLUS_X",
) -> dict:
    """
    Creates a deterministic Onshape Assembly BTMMateConnector-66 payload anchored
    to an occurrence's Part Studio Origin (or World Origin when occ_path == [])
    with exact parametric translation/rotation offsets. Avoids fragile B-Rep face IDs.
    """
    return {
        "feature": {
            "btType": "BTMMateConnector-66",
            "featureType": "mateConnector",
            "name": name,
            "suppressed": False,
            "parameters": [
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Origin type",
                    "value": "ON_ENTITY",
                    "parameterId": "originType",
                },
                {
                    "btType": "BTMParameterQueryWithOccurrenceList-67",
                    "parameterId": "originQuery",
                    "queries": [
                        {
                            "btType": "BTMFeatureQueryWithOccurrence-157",
                            "path": occ_path,
                            "featureId": "Origin",
                            "queryData": "ORIGIN_Z",
                        }
                    ],
                },
                {"btType": "BTMParameterBoolean-144", "value": True, "parameterId": "transform"},
                {"btType": "BTMParameterQuantity-147", "expression": f"{tx_mm:.6f} mm", "parameterId": "translationX"},
                {"btType": "BTMParameterQuantity-147", "expression": f"{ty_mm:.6f} mm", "parameterId": "translationY"},
                {"btType": "BTMParameterQuantity-147", "expression": f"{tz_mm:.6f} mm", "parameterId": "translationZ"},
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Rotation axis",
                    "value": rot_axis,
                    "parameterId": "rotationType",
                },
                {"btType": "BTMParameterQuantity-147", "expression": f"{rot_deg:.6f} deg", "parameterId": "rotation"},
                {"btType": "BTMParameterBoolean-144", "value": flip_primary, "parameterId": "flipPrimary"},
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Reorient secondary axis",
                    "value": secondary_axis,
                    "parameterId": "secondaryAxisType",
                },
            ],
        }
    }


def make_assembly_mate_payload(name: str, mate_type: str, mc_id_1: str, mc_id_2: str) -> dict:
    """Creates an Onshape Assembly BTMMate-64 payload (FASTENED, REVOLUTE, CYLINDRICAL, SLIDER, PLANAR)."""
    return {
        "feature": {
            "btType": "BTMMate-64",
            "featureType": "mate",
            "name": name,
            "suppressed": False,
            "parameters": [
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Mate type",
                    "value": mate_type.upper(),
                    "parameterId": "mateType",
                },
                {
                    "btType": "BTMParameterQueryWithOccurrenceList-67",
                    "parameterId": "mateConnectorsQuery",
                    "queries": [
                        {"btType": "BTMFeatureQueryWithOccurrence-157", "path": [], "featureId": mc_id_1, "queryData": ""},
                        {"btType": "BTMFeatureQueryWithOccurrence-157", "path": [], "featureId": mc_id_2, "queryData": ""},
                    ],
                },
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Reorient secondary axis",
                    "value": "PLUS_X",
                    "parameterId": "secondaryAxisAlignment",
                },
                {"btType": "BTMParameterBoolean-144", "value": False, "parameterId": "primaryAxisAlignment"},
                {"btType": "BTMParameterBoolean-144", "value": False, "parameterId": "limitsEnabled"},
                {"btType": "BTMParameterBoolean-144", "value": False, "parameterId": "mateOffsetEnabled"},
            ],
        }
    }


def make_assembly_group_payload(name: str, occ_ids: list[str]) -> dict:
    """Creates an Onshape Assembly BTMMateGroup-65 payload linking rigid occurrences."""
    return {
        "feature": {
            "btType": "BTMMateGroup-65",
            "featureType": "mateGroup",
            "name": name,
            "suppressed": False,
            "parameters": [
                {
                    "btType": "BTMParameterQueryWithOccurrenceList-67",
                    "parameterId": "occurrencesQuery",
                    "queries": [
                        {"btType": "BTMIndividualOccurrenceQuery-626", "path": [oid]} for oid in occ_ids
                    ],
                }
            ],
        }
    }


def make_assembly_gear_payload(
    name: str,
    mate_id_1: str,
    mate_id_2: str,
    ratio_expr: str = "1",
    reverse: bool = False,
) -> dict:
    """Creates an Onshape Assembly BTMMateRelation-1412 GEAR payload between two Revolute mates."""
    return {
        "feature": {
            "btType": "BTMMateRelation-1412",
            "featureType": "mateRelation",
            "name": name,
            "suppressed": False,
            "parameters": [
                {
                    "btType": "BTMParameterEnum-145",
                    "enumName": "Relation type",
                    "value": "GEAR",
                    "parameterId": "relationType",
                },
                {
                    "btType": "BTMParameterQueryWithOccurrenceList-67",
                    "parameterId": "matesQuery",
                    "queries": [
                        {"btType": "BTMFeatureQueryWithOccurrence-157", "path": [], "featureId": mate_id_1, "queryData": ""},
                        {"btType": "BTMFeatureQueryWithOccurrence-157", "path": [], "featureId": mate_id_2, "queryData": ""},
                    ],
                },
                {"btType": "BTMParameterQuantity-147", "expression": str(ratio_expr), "parameterId": "relationRatio"},
                {"btType": "BTMParameterBoolean-144", "value": bool(reverse), "parameterId": "reverseDirection"},
            ],
        }
    }


def _resolve_occurrence_id(instances: list[dict], target: str | None) -> list[str]:
    if not target or target.upper() in ("WORLD", "ORIGIN", "ROOT"):
        return []
    for inst in instances:
        if inst["id"] == target or inst["name"] == target or inst["name"].startswith(target):
            return [inst["id"]]
    for inst in instances:
        if target.lower() in inst["name"].lower():
            return [inst["id"]]
    raise KeyError(f"Could not resolve occurrence '{target}' among assembly instances: {[i['name'] for i in instances]}")


def resolve_mate_connector_rotation(mc: dict) -> tuple[str, float]:
    """
    Maps a Mate Connector specification's `axis` ('X', 'Y', or 'Z') to Onshape's
    `rotationType` (`ABOUT_X`, `ABOUT_Y`, `ABOUT_Z`) and `rotation` angle (deg)
    so that the Mate Connector's primary Z-axis aligns with the requested axis.
    """
    if "rot_axis" in mc or "rot_deg" in mc:
        return mc.get("rot_axis", "ABOUT_Z"), float(mc.get("rot_deg", 0.0))
    ax = (mc.get("axis") or "Z").upper()
    if ax == "X":
        return ("ABOUT_Y", 90.0)
    if ax == "Y":
        return ("ABOUT_X", -90.0)
    return ("ABOUT_Z", 0.0)


def compile_onshape_browser_bundle(
    spec: dict | Path | str | None = None,
    instances: list[dict] | None = None,
    cots_insertables: list[dict] | None = None,
    out_path: Path | str | None = None,
    *,
    constraints_path: Path | str | None = None,
    out_bundle_path: Path | str | None = None,
    onshape_cloud_instances: list[dict] | None = None,
) -> dict:
    """
    Pure-offline (zero-API-key) compiler that converts a declarative `*_constraints.json`
    `spec` into standard Onshape `/api/v6/assemblies/d/{did}/w/{wid}/e/{eid}/features`
    payloads (`BTMMateGroup-65`, `BTMMateConnector-66`, `BTMMate-64`, `BTMMateRelation-1412`)
    plus native `FRCDesignApp` (`ftc-design-lib`, `frc-design-lib`, `mkcad`) cloud instance
    descriptors for execution inside a signed-in Onshape browser session.
    """
    src = spec if spec is not None else constraints_path
    if isinstance(src, (str, Path)):
        spec_dict = json.loads(Path(src).read_text(encoding="utf-8"))
    elif isinstance(src, dict):
        spec_dict = src
    else:
        raise ValueError("compile_onshape_browser_bundle requires spec dict or constraints_path")

    cloud_ins = (
        cots_insertables
        if cots_insertables is not None
        else (
            onshape_cloud_instances
            if onshape_cloud_instances is not None
            else spec_dict.get("onshape_cloud_instances", [])
        )
    )
    target_out = out_path if out_path is not None else out_bundle_path

    def _occ_Or_placeholder(part_name: str | None) -> list[str]:
        if not part_name or part_name.upper() in ("WORLD", "ORIGIN", "ROOT"):
            return []
        if instances:
            return _resolve_occurrence_id(instances, part_name)
        return [f"{{{{OCC:{part_name}}}}}"]

    steps = []
    for mg in spec_dict.get("mate_groups", []):
        occ_ids = [_occ_Or_placeholder(p)[0] for p in mg["parts"]]
        steps.append(
            {
                "kind": "mateGroup",
                "name": mg["name"],
                "parts": list(mg["parts"]),
                "payload": make_assembly_group_payload(mg["name"], occ_ids),
            }
        )

    for mc in spec_dict.get("mate_connectors", []):
        occ_path = _occ_Or_placeholder(mc.get("part"))
        rot_axis, rot_deg = resolve_mate_connector_rotation(mc)
        steps.append(
            {
                "kind": "mateConnector",
                "key": mc["key"],
                "name": mc["name"],
                "part": mc.get("part"),
                "axis": mc.get("axis", "Z"),
                "payload": make_assembly_mate_connector_payload(
                    name=mc["name"],
                    occ_path=occ_path,
                    tx_mm=float(mc.get("tx_mm", 0.0)),
                    ty_mm=float(mc.get("ty_mm", 0.0)),
                    tz_mm=float(mc.get("tz_mm", 0.0)),
                    rot_axis=rot_axis,
                    rot_deg=rot_deg,
                    flip_primary=bool(mc.get("flip_primary", False)),
                    secondary_axis=mc.get("secondary_axis", "PLUS_X"),
                ),
            }
        )

    for m in spec_dict.get("mates", []):
        steps.append(
            {
                "kind": "mate",
                "key": m.get("key"),
                "name": m["name"],
                "type": m["type"],
                "mc1": m["mc1"],
                "mc2": m["mc2"],
                "payload": make_assembly_mate_payload(
                    name=m["name"],
                    mate_type=m["type"],
                    mc_id_1=f"{{{{MC:{m['mc1']}}}}}",
                    mc_id_2=f"{{{{MC:{m['mc2']}}}}}",
                ),
            }
        )

    for gr in spec_dict.get("gear_relations", []):
        steps.append(
            {
                "kind": "mateRelation",
                "name": gr["name"],
                "mate1": gr["mate1"],
                "mate2": gr["mate2"],
                "ratio": str(gr.get("ratio", "1")),
                "reverse": bool(gr.get("reverse", False)),
                "payload": make_assembly_gear_payload(
                    name=gr["name"],
                    mate_id_1=f"{{{{MATE:{gr['mate1']}}}}}",
                    mate_id_2=f"{{{{MATE:{gr['mate2']}}}}}",
                    ratio_expr=str(gr.get("ratio", "1")),
                    reverse=bool(gr.get("reverse", False)),
                ),
            }
        )

    bundle = {
        "mode": "PURE_OFFLINE_ONSHAPE_SESSION_BUNDLE",
        "transport": "session-authenticated-browser-offline-bundle",
        "api_key_calls": 0,
        "step_count": len(steps),
        "total_steps": len(steps),
        "cots_cloud_insertables": cloud_ins,
        "steps": steps,
    }
    if target_out is not None:
        out_p = Path(target_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return bundle


def onshape_constrain_assembly(
    did: str,
    wid: str,
    eid_asm: str,
    spec: dict | None = None,
    clear_existing: bool = False,
    emit_browser_bundle: Path | None = None,
) -> dict:
    """
    Applies deterministic Origin-anchored Mate Connectors (BTMMateConnector-66),
    Mate Groups (BTMMateGroup-65), Kinematic Mates (BTMMate-64), and Gear Relations
    (BTMMateRelation-1412) to an Onshape Assembly. If spec is None, automatically
    grounds the base occurrence (01_* or first sorted part) to the World Origin.
    Can also emit a browser-handoff JSON bundle for zero-quota session execution.
    """
    asm = _call_onshape_json(
        "GET",
        f"/api/v6/assemblies/d/{did}/w/{wid}/e/{eid_asm}",
        query="includeMateFeatures=true&includeNonSolids=true",
    )
    instances = sorted(asm.get("rootAssembly", {}).get("instances", []), key=lambda x: x.get("name", ""))
    if not instances:
        raise RuntimeError(f"Assembly {eid_asm} contains 0 instances to constrain.")

    if clear_existing:
        f_list = _call_onshape_json("GET", f"/api/v6/assemblies/d/{did}/w/{wid}/e/{eid_asm}/features")
        for f in reversed(f_list.get("features", [])):
            fid = urllib.parse.quote(f["featureId"], safe="")
            _call_onshape_json("DELETE", f"/api/v6/assemblies/d/{did}/w/{wid}/e/{eid_asm}/features/featureid/{fid}")

    if spec is None:
        base_inst = instances[0]
        spec = {
            "mate_connectors": [
                {"key": "mc_world", "name": "MC | World Frame Origin (Z=0 mm)", "part": "WORLD", "tx_mm": 0, "ty_mm": 0, "tz_mm": 0},
                {"key": "mc_base", "name": f"MC | {base_inst['name']} Base Mount (Z=0 mm)", "part": base_inst["id"], "tx_mm": 0, "ty_mm": 0, "tz_mm": 0},
            ],
            "mates": [
                {"key": "m_ground", "name": f"FASTENED | {base_inst['name']} to World Origin", "type": "FASTENED", "mc1": "mc_world", "mc2": "mc_base"}
            ],
        }

    created_features = []
    mc_ids: dict[str, str] = {}
    mate_ids: dict[str, str] = {}
    browser_requests = []
    post_path = f"/api/v6/assemblies/d/{did}/w/{wid}/e/{eid_asm}/features"

    for mg in spec.get("mate_groups", []):
        occ_ids = [_resolve_occurrence_id(instances, p)[0] for p in mg["parts"]]
        payload = make_assembly_group_payload(mg["name"], occ_ids)
        browser_requests.append({"step": mg["name"], "method": "POST", "path": post_path, "body": payload})
        res = _call_onshape_json("POST", post_path, body=payload)
        created_features.append({
            "name": mg["name"],
            "type": "mateGroup",
            "featureId": res["feature"]["featureId"],
            "status": res.get("featureState", {}).get("featureStatus"),
        })

    for mc in spec.get("mate_connectors", []):
        occ_path = _resolve_occurrence_id(instances, mc.get("part"))
        rot_axis, rot_deg = resolve_mate_connector_rotation(mc)
        payload = make_assembly_mate_connector_payload(
            name=mc["name"],
            occ_path=occ_path,
            tx_mm=float(mc.get("tx_mm", 0.0)),
            ty_mm=float(mc.get("ty_mm", 0.0)),
            tz_mm=float(mc.get("tz_mm", 0.0)),
            rot_axis=rot_axis,
            rot_deg=rot_deg,
            flip_primary=bool(mc.get("flip_primary", False)),
            secondary_axis=mc.get("secondary_axis", "PLUS_X"),
        )
        browser_requests.append({"step": mc["name"], "method": "POST", "path": post_path, "body": payload})
        res = _call_onshape_json("POST", post_path, body=payload)
        fid = res["feature"]["featureId"]
        mc_ids[mc["key"]] = fid
        created_features.append({
            "name": mc["name"],
            "type": "mateConnector",
            "featureId": fid,
            "status": res.get("featureState", {}).get("featureStatus"),
        })

    for m in spec.get("mates", []):
        payload = make_assembly_mate_payload(
            name=m["name"],
            mate_type=m["type"],
            mc_id_1=mc_ids[m["mc1"]],
            mc_id_2=mc_ids[m["mc2"]],
        )
        browser_requests.append({"step": m["name"], "method": "POST", "path": post_path, "body": payload})
        res = _call_onshape_json("POST", post_path, body=payload)
        fid = res["feature"]["featureId"]
        if "key" in m:
            mate_ids[m["key"]] = fid
        created_features.append({
            "name": m["name"],
            "type": "mate",
            "featureId": fid,
            "status": res.get("featureState", {}).get("featureStatus"),
        })

    for gr in spec.get("gear_relations", []):
        payload = make_assembly_gear_payload(
            name=gr["name"],
            mate_id_1=mate_ids[gr["mate1"]],
            mate_id_2=mate_ids[gr["mate2"]],
            ratio_expr=str(gr.get("ratio", "1")),
            reverse=bool(gr.get("reverse", False)),
        )
        browser_requests.append({"step": gr["name"], "method": "POST", "path": post_path, "body": payload})
        res = _call_onshape_json("POST", post_path, body=payload)
        fid = res["feature"]["featureId"]
        created_features.append({
            "name": gr["name"],
            "type": "mateRelation",
            "featureId": fid,
            "status": res.get("featureState", {}).get("featureStatus"),
        })

    if emit_browser_bundle:
        Path(emit_browser_bundle).parent.mkdir(parents=True, exist_ok=True)
        Path(emit_browser_bundle).write_text(
            json.dumps({"did": did, "wid": wid, "eid_asm": eid_asm, "requests": browser_requests}, indent=2),
            encoding="utf-8",
        )

    errors = [f for f in created_features if f["status"] != "OK"]
    return {
        "did": did,
        "wid": wid,
        "eid_assembly": eid_asm,
        "feature_count": len(created_features),
        "error_count": len(errors),
        "features": created_features,
    }


def onshape_push(
    file_path: Path,
    doc_name: str | None = None,
    did: str | None = None,
    wid: str | None = None,
    render_out: Path | None = None,
    constraints_path: Path | None = None,
    auto_ground: bool = True,
) -> dict:
    """
    Uploads a local .step file to a new or existing personal Onshape Document,
    waits for cloud translation, constrains the resulting Assembly with Mate Connectors
    and Mates (either from constraints_path or auto-grounding the base part),
    and optionally renders a cloud shaded view PNG.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if not did or not wid:
        title = doc_name or f"AG CAD — {file_path.stem}"
        doc_info = _call_onshape_json("POST", "/api/v6/documents", body={"name": title, "isPublic": False})
        did = doc_info["id"]
        wid = doc_info["defaultWorkspace"]["id"]

    boundary = "----AGBoundary" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
    content_type = f"multipart/form-data; boundary={boundary}"
    t_path = f"/api/v6/translations/d/{did}/w/{wid}"
    file_bytes = file_path.read_bytes()
    fields = {
        "allowFaultyParts": "true",
        "createComposite": "false",
        "translate": "true",
        "yAxisIsUp": "false",
        "importWithinDocument": "true",
    }
    body_parts = []
    for k, v in fields.items():
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f"{v}\r\n".encode("utf-8")
        )
    body_parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )
    payload = b"".join(body_parts)
    req = urllib.request.Request(
        f"https://cad.onshape.com{t_path}",
        headers=sign_onshape_headers("POST", t_path, "", content_type=content_type),
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        tid = json.loads(resp.read().decode("utf-8"))["id"]

    result_eids = []
    for _ in range(45):
        time.sleep(2)
        tstatus = _call_onshape_json("GET", f"/api/v6/translations/{tid}")
        state = tstatus.get("requestState")
        if state == "DONE":
            result_eids = tstatus.get("resultElementIds") or []
            break
        elif state == "FAILED":
            raise RuntimeError(f"Onshape STEP import failed: {tstatus}")

    result_eid = result_eids[0] if result_eids else None
    elements = _call_onshape_json("GET", f"/api/v6/documents/d/{did}/w/{wid}/elements")
    ps_el = next((el for el in elements if el["elementType"] == "PARTSTUDIO" and el["id"] in result_eids), None)
    asm_el = next((el for el in elements if el["elementType"] == "ASSEMBLY" and el["id"] in result_eids), None)

    imported_parts_summary = []
    if ps_el:
        ps_parts = _call_onshape_json("GET", f"/api/v6/parts/d/{did}/w/{wid}/e/{ps_el['id']}")
        non_solids = [p["name"] for p in ps_parts if p.get("bodyType") != "solid"]
        if non_solids:
            raise RuntimeError(
                f"Onshape Parasolid STEP import degraded {len(non_solids)} part(s) into non-solid bodies "
                f"({non_solids}). Avoid single-edge 360-deg periodic B-spline extrusions; split closed spline "
                f"loops into >=2 non-periodic edges."
            )
        imported_parts_summary = [
            {"partId": p.get("partId"), "name": p.get("name"), "bodyType": p.get("bodyType")}
            for p in ps_parts
        ]

    constraint_report = None
    if constraints_path or auto_ground:
        if not asm_el and ps_el:
            new_asm = _call_onshape_json(
                "POST",
                f"/api/v6/assemblies/d/{did}/w/{wid}",
                body={"name": f"{file_path.stem}_Assembly"},
            )
            asm_el = {"id": new_asm["id"], "elementType": "ASSEMBLY", "name": f"{file_path.stem}_Assembly"}
            _call_onshape_json(
                "POST",
                f"/api/v6/assemblies/d/{did}/w/{wid}/e/{asm_el['id']}/instances",
                body={
                    "documentId": did,
                    "elementId": ps_el["id"],
                    "includePartTypes": ["PARTS"],
                    "isWholePartStudio": True,
                },
            )
            result_eids.append(asm_el["id"])
        if asm_el:
            spec_data = json.loads(Path(constraints_path).read_text(encoding="utf-8")) if constraints_path else None
            constraint_report = onshape_constrain_assembly(did, wid, asm_el["id"], spec=spec_data, clear_existing=False)

    render_target_eid = ps_el["id"] if ps_el else result_eid
    if render_out and render_target_eid:
        q_iso = (
            "outputWidth=1200&outputHeight=1200&pixelSize=0.00015"
            "&viewMatrix=0.7071,-0.7071,0,0,0.4082,0.4082,0.8165,-0.008,-0.5774,-0.5774,0.5774,0"
        )
        res_img = _call_onshape_json(
            "GET",
            f"/api/partstudios/d/{did}/w/{wid}/e/{render_target_eid}/shadedviews",
            query=q_iso,
        )
        if res_img and "images" in res_img:
            raw_img = Image.open(io.BytesIO(base64.b64decode(res_img["images"][0])))
            bg = Image.new("RGB", raw_img.size, (255, 255, 255))
            bg.paste(raw_img, mask=raw_img.split()[3])
            Path(render_out).parent.mkdir(parents=True, exist_ok=True)
            bg.save(str(render_out))

    url = f"https://cad.onshape.com/documents/{did}/w/{wid}" + (f"/e/{result_eid}" if result_eid else "")
    return {
        "did": did,
        "wid": wid,
        "eid": result_eid,
        "result_eids": result_eids,
        "url": url,
        "render_path": str(render_out) if render_out else None,
        "constraints": constraint_report,
    }


def onshape_export(did: str, wid: str, eid: str, fmt: str, out_path: Path):
    """Exports STEP, PARASOLID, or STL from Onshape via Direct REST API (with 307 redirect signing)."""
    path = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/translations"
    payload = json.dumps({"formatName": fmt.upper(), "storeInDocument": False}).encode("utf-8")
    req = urllib.request.Request(
        f"https://cad.onshape.com{path}",
        data=payload,
        headers=sign_onshape_headers("POST", path),
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        tid = json.loads(resp.read().decode("utf-8"))["id"]

    for _ in range(45):
        time.sleep(2)
        tpath = f"/api/v6/translations/{tid}"
        treq = urllib.request.Request(
            f"https://cad.onshape.com{tpath}",
            headers=sign_onshape_headers("GET", tpath),
            method="GET",
        )
        with urllib.request.urlopen(treq) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        if status["requestState"] == "DONE":
            ext_id = status["resultExternalDataIds"][0]
            dl_path = f"/api/v6/documents/d/{did}/externaldata/{ext_id}"

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(NoRedirect)
            try:
                resp = opener.open(
                    urllib.request.Request(
                        f"https://cad.onshape.com{dl_path}",
                        headers=sign_onshape_headers("GET", dl_path, "", "application/json", "application/octet-stream"),
                        method="GET",
                    )
                )
                data = resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 307, 308):
                    loc = e.headers["Location"]
                    u = urllib.parse.urlparse(loc)
                    resp = urllib.request.urlopen(
                        urllib.request.Request(
                            loc,
                            headers=sign_onshape_headers("GET", u.path, u.query, "application/json", "application/octet-stream"),
                            method="GET",
                        )
                    )
                    data = resp.read()
                else:
                    raise
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            print(f"[AG-CAD] Exported {fmt} to {out_path} ({len(data):,} bytes)")
            return
        elif status["requestState"] == "FAILED":
            raise RuntimeError(f"Onshape translation failed: {status}")
    raise TimeoutError("Onshape translation timed out")


def lint_cad_assembly(step_path: Path, constraints_path: Path | None = None) -> dict:
    """
    Loads a multi-body STEP assembly (and optional `*_constraints.json`) and runs
    `audit_mechanism_physics()` to check:
    1. Structural Support Connectivity (`FLOATING_PART_ERROR`)
    2. Actuator-to-Rotor Coaxial Coupling (`UNCOUPLED_ACTUATOR_ERROR`)
    3. Game-Piece Swept Tunnel, Wheel Contact & Coaxial Gear OD (`BLOCKED_BALL_PATH_ERROR`, `NON_CONTACTING_WHEEL_ERROR`, `GEAR_EXCEEDS_WHEEL_OD_ERROR`)
    4. Multi-Pose Revolute Sweep Collision (`REVOLUTE_SWEEP_COLLISION_ERROR`)
    """
    if str(WORKSPACE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKSPACE_ROOT))
    from build123d import import_step
    from tools.ag_cad_helpers import audit_mechanism_physics

    step_file = Path(step_path)
    if constraints_path is None:
        candidate = step_file.parent / f"{step_file.stem}_constraints.json"
        if candidate.exists():
            constraints_path = candidate

    constraints = None
    if constraints_path and Path(constraints_path).exists():
        constraints = json.loads(Path(constraints_path).read_text(encoding="utf-8"))

    loaded = import_step(str(step_file))
    children = list(getattr(loaded, "children", None) or [])
    if len(children) == 1 and getattr(children[0], "children", None):
        children = list(children[0].children)
    parts = children if len(children) >= 2 else list(loaded.solids())

    report = audit_mechanism_physics(parts, constraints=constraints)
    report["file"] = str(step_file)
    report["constraints_file"] = str(constraints_path) if constraints_path else None
    report["part_count"] = len(parts)
    return report


def build_onshape_browser_sync_js(
    did: str,
    wid: str,
    asm_eid: str,
    step_path: str | Path,
    bundle_path: str | Path,
    old_ps_eid: str | None = None,
) -> str:
    """
    Milestone 4: Pure-offline generator that compiles a local `.step` file and
    `*_onshape_bundle.json` into a self-contained async JavaScript function string
    for execution inside the signed-in Onshape Chrome tab (`evaluate_script`).
    Performs zero direct API calls from Python.
    """
    step_file = Path(step_path)
    bundle_file = Path(bundle_path)
    step_b64 = base64.b64encode(step_file.read_bytes()).decode("ascii")
    bundle_data = json.loads(bundle_file.read_text(encoding="utf-8"))
    payload_obj = {
        "did": did,
        "wid": wid,
        "asmEid": asm_eid,
        "oldPsEid": old_ps_eid,
        "stepFilename": step_file.name,
        "stepB64": step_b64,
        "bundle": bundle_data,
    }
    payload_json = json.dumps(payload_obj)
    return f"""async () => {{
  const cfg = {payload_json};
  const xsrfCookie = document.cookie.split('; ').find(r => r.startsWith('XSRF-TOKEN='));
  const xsrf = xsrfCookie ? decodeURIComponent(xsrfCookie.split('=')[1]) : '';
  const hdrs = {{
    'Accept': 'application/json;charset=UTF-8; qs=0.09',
    'Content-Type': 'application/json',
    'X-XSRF-TOKEN': xsrf
  }};
  const api = async (method, url, body) => {{
    const opts = {{ method, headers: hdrs, credentials: 'include' }};
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    const txt = await r.text();
    let d = null;
    try {{ d = JSON.parse(txt); }} catch (e) {{ d = {{ raw: txt }}; }}
    if (!r.ok) throw new Error(`${{method}} ${{url}} -> ${{r.status}}: ${{txt.slice(0, 300)}}`);
    return d;
  }};

  const bin = atob(cfg.stepB64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const fd = new FormData();
  fd.append('file', new Blob([bytes], {{ type: 'application/step' }}), cfg.stepFilename);
  fd.append('allowFaultyParts', 'true');
  fd.append('createComposite', 'false');
  fd.append('splitAssembliesIntoMultipleElements', 'false');
  fd.append('translate', 'true');

  const tr = await (await fetch(`/api/v6/translations/d/${{cfg.did}}/w/${{cfg.wid}}`, {{
    method: 'POST', headers: {{ 'X-XSRF-TOKEN': xsrf }}, body: fd, credentials: 'include'
  }})).json();

  let psEid = null;
  for (let w = 0; w < 45; w++) {{
    await new Promise(r => setTimeout(r, 1500));
    const st = await api('GET', `/api/v6/translations/${{tr.id}}`);
    if (st.requestState === 'DONE') {{ psEid = st.resultElementIds[0]; break; }}
    if (st.requestState === 'FAILED') throw new Error('Translation failed: ' + st.failureReason);
  }}
  if (!psEid) throw new Error('Translation timed out');

  await api('POST', `/api/v6/assemblies/d/${{cfg.did}}/w/${{cfg.wid}}/e/${{cfg.asmEid}}/instances`, {{
    documentId: cfg.did, elementId: psEid, includePartTypes: ['PARTS'], isWholePartStudio: true
  }});
  if (cfg.oldPsEid && cfg.oldPsEid !== psEid) {{
    try {{ await fetch(`/api/v6/elements/d/${{cfg.did}}/w/${{cfg.wid}}/e/${{cfg.oldPsEid}}`, {{ method: 'DELETE', headers: hdrs, credentials: 'include' }}); }} catch (e) {{}}
  }}
  return {{ did: cfg.did, wid: cfg.wid, asmEid: cfg.asmEid, psEid }};
}}"""


def main():
    parser = argparse.ArgumentParser(description="Antigravity Local-First + Cloud CAD Pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_insp = sub.add_parser("inspect", help="Audit solids, volumes, bounds, interference, and clearance (fast OCP 8.0 default, FreeCAD 1.0 for .FCStd)")
    p_insp.add_argument("file", type=Path, help="Path to .FCStd, .step, .step.gz, or .stl file")
    p_insp.add_argument("--interference", action="store_true", help="Run pairwise solid boolean interference check")
    p_insp.add_argument("--clearance", type=float, default=0.0, help="Minimum distance threshold in mm for distToShape check")
    p_insp.add_argument("--engine", choices=["auto", "ocp", "freecad"], default="auto", help="B-Rep audit engine (default: auto -> OCP 8.0 for STEP/STL, FreeCAD for .FCStd)")
    p_insp.add_argument("--json", action="store_true", help="Output full JSON report")

    p_lint = sub.add_parser("lint", help="Run Automated Mechanical Sanity & Physics Linter on a STEP assembly")
    p_lint.add_argument("file", type=Path, help="Path to assembly .step file")
    p_lint.add_argument("--constraints", type=Path, default=None, help="Optional path to *_constraints.json")
    p_lint.add_argument("--json", action="store_true", help="Output full JSON report")

    p_exp = sub.add_parser("export", help="Export STEP AP214 and/or watertight STL locally (fast OCP 8.0 default)")
    p_exp.add_argument("file", type=Path, help="Input .FCStd, .step, or .step.gz file")
    p_exp.add_argument("--step", type=Path, default=None, help="Output .step file path")
    p_exp.add_argument("--stl", type=Path, default=None, help="Output .stl file path")
    p_exp.add_argument("--engine", choices=["auto", "ocp", "freecad"], default="auto", help="Export engine (default: auto)")

    p_ren = sub.add_parser("render", help="Render 5 multi-angle views + section cut + contact sheet locally")
    p_ren.add_argument("file", type=Path, help="Input .FCStd, .step, .step.gz, or .stl file")
    p_ren.add_argument("--out", type=Path, required=True, help="Output directory for PNG renders")
    p_ren.add_argument("--prefix", type=str, default=None, help="Optional filename prefix")
    p_ren.add_argument("--engine", choices=["auto", "ocp", "freecad"], default="auto", help="Tessellation engine (default: auto)")

    p_bld = sub.add_parser("build", help="Execute a parametric build123d Python script using .venv and auto-compile jointed .sc")
    p_bld.add_argument("script", type=Path, help="Path to build123d .py script")
    p_bld.add_argument("--no-solidcraft", action="store_true", help="Skip automatic SolidCraft jointed .sc assembly compilation")
    p_bld.add_argument("script_args", nargs="*", help="Optional arguments passed to the script")

    p_push = sub.add_parser("onshape-push", help="Upload a local .step file to personal Onshape and constrain assembly")
    p_push.add_argument("file", type=Path, help="Path to local .step file")
    p_push.add_argument("--doc-name", type=str, default=None, help="Name for new Onshape document")
    p_push.add_argument("--did", type=str, default=None, help="Existing Onshape Document ID")
    p_push.add_argument("--wid", type=str, default=None, help="Existing Onshape Workspace ID")
    p_push.add_argument("--constraints", type=Path, default=None, help="Optional JSON constraint spec for Assembly Mate Connectors & Mates")
    p_push.add_argument("--no-auto-ground", action="store_true", help="Disable automatic World Origin Fastened mate on base part")
    p_push.add_argument("--render", type=Path, default=None, help="Optional output path for cloud shaded view PNG")

    p_con = sub.add_parser("onshape-constrain", help="Add Origin-anchored Mate Connectors, Mates, and Gear Relations to an Onshape Assembly")
    p_con.add_argument("--did", required=True, help="Onshape Document ID")
    p_con.add_argument("--wid", required=True, help="Onshape Workspace ID")
    p_con.add_argument("--eid", required=True, help="Onshape Assembly Element ID")
    p_con.add_argument("--spec", type=Path, default=None, help="Path to declarative JSON constraint spec (defaults to auto-grounding base part)")
    p_con.add_argument("--clear", action="store_true", help="Delete existing assembly features before applying constraints")
    p_con.add_argument("--emit-browser-bundle", type=Path, default=None, help="Write prepared JSON requests for zero-quota browser session execution")

    p_on = sub.add_parser("onshape-export", help="Export STEP/PARASOLID/STL via Direct Onshape REST API")
    p_on.add_argument("--did", required=True)
    p_on.add_argument("--wid", required=True)
    p_on.add_argument("--eid", required=True)
    p_on.add_argument("--format", default="STEP", choices=["STEP", "PARASOLID", "STL"])
    p_on.add_argument("--out", type=Path, required=True)

    p_cs = sub.add_parser("cots-search", help="Search local COTS STEP/.sc catalog and Onshape FRCDesignApp insertables")
    p_cs.add_argument("query", type=str, help="Keyword or SKU query")
    p_cs.add_argument("--vendor", type=str, default=None, help="Vendor/library filter (gobilda, rev, swyft, wcp, am, ftc-design-lib, etc.)")
    p_cs.add_argument("--category", type=str, default=None, help="Category filter")
    p_cs.add_argument("--program", type=str, default=None, choices=["FTC", "FRC", "ftc", "frc", "all"], help="Program filter: FTC, FRC, or all")
    p_cs.add_argument("--source", type=str, default="all", choices=["all", "local", "onshape", "variants"])
    p_cs.add_argument("--bore-mm", type=float, default=None, help="Target bore diameter in mm (±0.25 mm)")
    p_cs.add_argument("--max-extent-mm", type=float, default=None, help="Maximum bounding-box dimension in mm")
    p_cs.add_argument("--has-mounting-holes", action="store_true", help="Only return parts with mounting holes")
    p_cs.add_argument("--limit", type=int, default=25)

    p_ci = sub.add_parser("cots-info", help="Get full B-Rep geometric fingerprint, SolidCraft .sc frames, or Onshape schema for a COTS part")
    p_ci.add_argument("key", type=str, help="Part key, SKU, or insertable ID")

    p_sci = sub.add_parser("solidcraft-insert", help="Insert & optionally mate a normalized COTS .sc part into SolidCraft")
    p_sci.add_argument("part_key_or_sku", type=str, help="SKU, part_id, or search query")
    p_sci.add_argument("--name", type=str, default=None, help="Optional custom component name in assembly")
    p_sci.add_argument("--translate", type=float, nargs=3, metavar=("X", "Y", "Z"), default=None, help="Initial [x, y, z] translation in mm")
    p_sci.add_argument("--anchor", type=str, default=None, help="Anchor frame to mate (bore_center, flange_seat, output_shaft, sense_origin, bbox_center)")
    p_sci.add_argument("--joint-type", type=str, default=None, choices=["rigid", "revolute", "slider", "cylindrical", "planar", "ball"])
    p_sci.add_argument("--target-occurrence", type=str, default=None, help="Target occurrence in assembly to mate to (e.g. 'Plate:1')")
    p_sci.add_argument("--target-point", type=float, nargs=3, metavar=("X", "Y", "Z"), default=None)
    p_sci.add_argument("--target-axis", type=float, nargs=3, metavar=("ZX", "ZY", "ZZ"), default=None)
    p_sci.add_argument("--no-flip", action="store_true", help="Set flip=False on the created joint")
    p_sci.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_sci.add_argument("--design", type=str, default=None, help="Optional headless .sc file path")

    p_scc = sub.add_parser("solidcraft-compile", help="Compile a project STEP + *_constraints.json into a jointed .sc assembly + BOM + motion GIF")
    p_scc.add_argument("file", type=Path, help="Path to project .step or .step.gz file")
    p_scc.add_argument("--constraints", type=Path, default=None, help="Optional path to *_constraints.json (auto-discovered if omitted)")
    p_scc.add_argument("--out", type=Path, default=None, help="Optional output .sc path")
    p_scc.add_argument("--no-gif", action="store_true", help="Skip animated .gif export")
    p_scc.add_argument("--live", action="store_true", help="Push/open the compiled .sc assembly in the live SolidCraft GUI")
    p_scc.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")

    p_scd = sub.add_parser("solidcraft-dock", help="Launch the floating SolidCraft COTS companion dock")
    p_scd.add_argument("--http-port", type=int, default=8765, help="Local HTTP port for the dock (default 8765)")
    p_scd.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_scd.add_argument("--no-open", action="store_true", help="Do not auto-open the floating dock window")

    p_scinsp = sub.add_parser("solidcraft-inspect", help="Fast ~300 ms Rust B-Rep clearance + dynamic joint.sweep gate on a .sc assembly")
    p_scinsp.add_argument("file", type=Path, help="Path to .sc assembly file")
    p_scinsp.add_argument("--clearance", type=float, default=0.5, help="Clearance threshold in mm (default 0.5)")
    p_scinsp.add_argument("--min-volume", type=float, default=0.01, help="Minimum overlap volume in mm^3 (default 0.01)")

    p_sccmp = sub.add_parser("solidcraft-compare", help="3D B-Rep revision diff between two .sc or .step files via inspect.compare")
    p_sccmp.add_argument("current_file", type=Path, help="Path to current .sc or .step file")
    p_sccmp.add_argument("other_file", type=Path, help="Path to baseline/other .sc or .step file")
    p_sccmp.add_argument("--out", type=Path, default=None, help="Optional output PNG path for red/green visual diff")

    p_sccpic = sub.add_parser("solidcraft-compare-picture", help="Compare a .sc model against a reference image (silhouette overlap + mm edge offsets)")
    p_sccpic.add_argument("sc_file", type=Path, help="Path to .sc file")
    p_sccpic.add_argument("image", type=Path, help="Path to reference PNG/JPEG image")
    p_sccpic.add_argument("--view", type=str, default="front", choices=["front", "back", "left", "right", "top", "bottom", "iso"], help="Standard camera view")
    p_sccpic.add_argument("--scale", type=float, default=None, help="Optional image width in mm")
    p_sccpic.add_argument("--out", type=Path, default=None, help="Optional output path for red/blue silhouette overlay PNG")

    p_scfab = sub.add_parser("solidcraft-fab", help="Auto-orient flat print faces (Z=0) and export 3D-printed STLs and/or laser DXFs from a .sc design")
    p_scfab.add_argument("file", type=Path, help="Path to .sc file")
    p_scfab.add_argument("--stl-dir", type=Path, default=None, help="Directory to export print-oriented .stl meshes")
    p_scfab.add_argument("--dxf-out", type=Path, default=None, help="Optional output .dxf path")
    p_scfab.add_argument("--dxf-view", type=str, default="top", choices=["top", "bottom", "front", "back", "left", "right"], help="Standard view for DXF projection")

    p_scpull = sub.add_parser("solidcraft-pull", help="Pull timeline command script (*_sc_script.json) and parameters (*_sc_params.json) from a .sc file or live SolidCraft GUI")
    p_scpull.add_argument("file", nargs="?", type=Path, default=None, help="Path to .sc file (optional if --live)")
    p_scpull.add_argument("--live", action="store_true", help="Pull from the currently active design in the live SolidCraft GUI")
    p_scpull.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_scpull.add_argument("--script-out", type=Path, default=None, help="Optional output path for *_sc_script.json")
    p_scpull.add_argument("--params-out", type=Path, default=None, help="Optional output path for *_sc_params.json")

    p_sctpl = sub.add_parser("solidcraft-template", help="List or build parametric robotics part templates (.sc + *_sc_script.json + parameter sweep)")
    p_sctpl.add_argument("template_id", nargs="?", type=str, default=None, help="Template ID to build (omit or pass --list to list templates)")
    p_sctpl.add_argument("--list", action="store_true", help="List all available parametric robotics templates")
    p_sctpl.add_argument("--param", "-p", action="append", default=[], help="Parameter override KEY=VAL (repeatable)")
    p_sctpl.add_argument("--out", type=Path, default=None, help="Output .sc path")
    p_sctpl.add_argument("--step-out", type=Path, default=None, help="Optional .step output path")
    p_sctpl.add_argument("--name", type=str, default=None, help="Optional custom part name")
    p_sctpl.add_argument("--insert-live", action="store_true", help="Insert the built .sc part into the live SolidCraft session on port 27878")
    p_sctpl.add_argument("--port", type=int, default=27878, help="Live SolidCraft port (default 27878)")

    p_gst = sub.add_parser("git-status", help="Show project-grouped Git status, file sizes (MB), and CAD hygiene issues")
    p_gst.add_argument("--scope", type=str, default=None, help="Optional project/scope filter")

    p_ghyg = sub.add_parser("git-hygiene", help="Audit and optionally auto-fix CAD file organization and >50MB .gz compression")
    p_ghyg.add_argument("--scope", type=str, default=None, help="Optional project/scope filter")
    p_ghyg.add_argument("--fix", action="store_true", help="Auto-fix misplaced files, delete COTS in fabrication/, and gzip >50MB binaries")

    p_gdiff = sub.add_parser("git-diff", help="Run a 3D visual & geometric diff against HEAD for a modified .sc/.step file")
    p_gdiff.add_argument("path", type=str, help="Relative path to modified .sc, .step, or .step.gz file")

    p_gcm = sub.add_parser("git-commit", help="Run hygiene guard, stage a project scope, commit, and optionally push")
    p_gcm.add_argument("scope", type=str, help="Project scope (e.g. projects/passive-dual-flywheel-turret or 'all')")
    p_gcm.add_argument("-m", "--message", type=str, required=True, help="Commit message")
    p_gcm.add_argument("--no-push", action="store_true", help="Do not push to origin after committing")

    p_lvi = sub.add_parser("solidcraft-live-inspect", help="Inspect the live SolidCraft GUI scene on port 27878 in <5 ms")
    p_lvi.add_argument("--port", type=int, default=27878)

    p_lvc = sub.add_parser("solidcraft-live-clearance", help="Run a live B-Rep clearance & interference audit in the SolidCraft GUI")
    p_lvc.add_argument("--clearance", type=float, default=1.0)
    p_lvc.add_argument("--all-cots", action="store_true", help="Include COTS-to-COTS pairs")
    p_lvc.add_argument("--port", type=int, default=27878)

    p_lvf = sub.add_parser("solidcraft-live-focus", help="Focus the live SolidCraft 3D camera on [x,y,z] and/or apply a section cut")
    p_lvf.add_argument("--target", type=float, nargs=3, default=None, metavar=("X", "Y", "Z"))
    p_lvf.add_argument("--section", type=str, choices=["XY", "XZ", "YZ"], default=None)
    p_lvf.add_argument("--clear-section", action="store_true")
    p_lvf.add_argument("--view", type=str, choices=["iso", "front", "back", "left", "right", "top", "bottom"], default=None)
    p_lvf.add_argument("--port", type=int, default=27878)

    p_lvd = sub.add_parser("solidcraft-live-drive", help="Drive a joint (and motion-linked followers) in the live SolidCraft GUI")
    p_lvd.add_argument("joint", type=str)
    p_lvd.add_argument("value", type=float)
    p_lvd.add_argument("--port", type=int, default=27878)

    p_lvs = sub.add_parser("solidcraft-live-sweep", help="Sweep a joint across [from, to] in the live SolidCraft GUI and auto-park on contact")
    p_lvs.add_argument("joint", type=str)
    p_lvs.add_argument("--from-val", type=float, default=0.0)
    p_lvs.add_argument("--to-val", type=float, default=360.0)
    p_lvs.add_argument("--steps", type=int, default=24)
    p_lvs.add_argument("--port", type=int, default=27878)

    p_lvp = sub.add_parser("solidcraft-live-param", help="Update a user parameter in the live SolidCraft GUI")
    p_lvp.add_argument("name", type=str)
    p_lvp.add_argument("expression", type=str)
    p_lvp.add_argument("--sweep", action="store_true")
    p_lvp.add_argument("--port", type=int, default=27878)

    p_lvsync = sub.add_parser("solidcraft-live-sync", help="Reload linked subassemblies, save .sc, and pull JSON script/params from live SolidCraft")
    p_lvsync.add_argument("--open", type=str, default=None, help="Optional .sc file to open in live SolidCraft")
    p_lvsync.add_argument("--no-links", action="store_true")
    p_lvsync.add_argument("--no-save", action="store_true")
    p_lvsync.add_argument("--no-pull", action="store_true")
    p_lvsync.add_argument("--port", type=int, default=27878)

    p_lvfab = sub.add_parser("solidcraft-live-fab", help="Export 3D print STLs, laser DXFs, and BOM Markdown from the live SolidCraft GUI assembly")
    p_lvfab.add_argument("--port", type=int, default=27878)

    p_lvpick = sub.add_parser("solidcraft-live-pick", help="Map active 3D viewport selection in live SolidCraft GUI back to build123d Python source & params.json")
    p_lvpick.add_argument("--project-dir", type=Path, default=None, help="Optional project directory (auto-inferred from open .sc path if omitted)")
    p_lvpick.add_argument("--port", type=int, default=27878)

    p_slice = sub.add_parser("slice-audit", help="Headless OrcaSlicer CLI & mesh DFM audit (.stl/.3mf bed fit, overhangs, first-layer contact, mass)")
    p_slice.add_argument("file", type=Path, help="Path to .stl or .3mf print file or plate")
    p_slice.add_argument("--bed-profile", type=str, default="bambu_256", choices=["bambu_256", "snapmaker_u1", "prusa_mk4"], help="Target printer build volume profile")
    p_slice.add_argument("--filament", type=str, default="PLA", help="Filament material (PLA, PETG, ABS, ASA, PA-CF, TPU_95A)")
    p_slice.add_argument("--no-cli", action="store_true", help="Skip invoking headless orca-slicer.exe CLI")
    p_slice.add_argument("--json", action="store_true", help="Output full JSON report")

    p_fea = sub.add_parser("fea", help="Dual-tier 3D B-Rep tetrahedral solid FEA (Gmsh + scikit-fem ElementTetP2 or CalculiX ccx.exe)")
    p_fea.add_argument("file", type=Path, help="Path to .step file")
    p_fea.add_argument("--material", type=str, default="PLA", help="Material key (PLA, PETG,PA-CF, ABS, 6061-T6, 7075-T6, polycarbonate, steel_4140)")
    p_fea.add_argument("--fixed", type=str, default="xmin", help="Fixed boundary selector (xmin, xmax, ymin, ymax, zmin, zmax)")
    p_fea.add_argument("--load", type=str, default="xmax", help="Loaded boundary selector (xmin, xmax, ymin, ymax, zmin, zmax)")
    p_fea.add_argument("--force", type=float, nargs=3, default=[0.0, 0.0, -50.0], metavar=("FX", "FY", "FZ"), help="Applied total force vector in N")
    p_fea.add_argument("--g-load", type=float, nargs=3, default=None, metavar=("GX", "GY", "GZ"), help="Optional body G-load vector in multiples of g")
    p_fea.add_argument("--print-axis", type=str, default="Z", choices=["X", "Y", "Z"], help="FDM build-plate normal axis for Z-layer delamination safety factor")
    p_fea.add_argument("--mesh-size", type=float, default=None, help="Target tetrahedral element size in mm")
    p_fea.add_argument("--backend", type=str, default="skfem", choices=["skfem", "calculix"], help="Solver backend: skfem (Tier 1 fast) or calculix (Tier 2 ccx.exe)")
    p_fea.add_argument("--buckle-modes", type=int, default=0, help="Number of linear *BUCKLE modes to extract (CalculiX backend)")
    p_fea.add_argument("--out-png", type=Path, default=None, help="Optional output PNG path for deformed Von Mises stress heatmap")
    p_fea.add_argument("--out-inp", type=Path, default=None, help="Optional output Abaqus/CalculiX .inp deck path")

    p_phys = sub.add_parser("physics-sim", help="Compile .step + *_constraints.json to MJCF + CoACD collision hulls and simulate in MuJoCo or Genesis")
    p_phys.add_argument("file", type=Path, help="Path to assembly .step file")
    p_phys.add_argument("--constraints", type=Path, default=None, help="Optional path to *_constraints.json")
    p_phys.add_argument("--engine", type=str, default="mujoco", choices=["mujoco", "genesis"], help="Physics backend (mujoco or genesis GPU/CPU)")
    p_phys.add_argument("--duration", type=float, default=0.6, help="Simulation duration in seconds")
    p_phys.add_argument("--out-mjcf", type=Path, default=None, help="Optional output MJCF .xml path")
    p_phys.add_argument("--out-gif", type=Path, default=None, help="Optional output animated .gif path")
    p_phys.add_argument("--interactive", action="store_true", help="Launch interactive 3D physics viewer window")

    p_share = sub.add_parser("share", help="Package a project or .step into a zero-install interactive 3D HTML viewer, .glb, and team handoff .zip bundle")
    p_share.add_argument("target", type=Path, help="Project folder (projects/<name>) or assembly .step/.sc file")
    p_share.add_argument("--out-dir", type=Path, default=None, help="Optional output directory")
    p_share.add_argument("--deflection", type=float, default=0.25, help="Linear tessellation deflection in mm (default 0.25)")
    p_share.add_argument("--no-zip", action="store_true", help="Skip creating <name>_team_bundle.zip")
    p_share.add_argument("--gh-release", type=str, default=None, help="Optional GitHub Release tag (e.g. v1.0-turret) to publish immediately via gh CLI")

    p_scup = sub.add_parser("solidcraft-upgrade-format", help="Batch-upgrade .sc files in cots/ and/or projects/ to SolidCraft Format 52 (solidcore)")
    p_scup.add_argument("--scope", choices=["all", "cots", "projects"], default="all")
    p_scup.add_argument("--workers", type=int, default=8)
    p_scup.add_argument("--force", action="store_true")
    p_scup.add_argument("--limit", type=int, default=None)
    p_scup.add_argument("--exclude-project", action="append", default=[])

    args = parser.parse_args()
    if args.cmd == "inspect":
        report = inspect_cad(
            args.file,
            check_interference=args.interference,
            clearance_threshold=args.clearance,
            engine=args.engine,
        )
        if args.json:
            print(json.dumps(report, indent=2))
            return
        print(f"File: {report['file']}")
        print(f"Valid Solids: {report['part_count']} (Invalid: {len(report['invalid_parts'])})")
        print(f"Total Volume: {report['total_volume_cm3']} cm^3")
        env = report["overall_envelope_mm"]
        print(f"Overall Envelope: {env[0]} x {env[1]} x {env[2]} mm")
        if args.interference:
            print(f"Interferences (>5 mm^3): {len(report['interferences'])}")
            for ix in report["interferences"][:15]:
                print(f"  - {ix['part_a']} <-> {ix['part_b']}: {ix['overlap_mm3']} mm^3")
        if args.clearance > 0.0:
            print(f"Clearance Warnings (<{args.clearance} mm): {len(report['clearance_warnings'])}")
            for cw in report["clearance_warnings"][:15]:
                print(
                    f"  - {cw['part_a']} <-> {cw['part_b']}: {cw['min_distance_mm']} mm "
                    f"(at {cw['witness_a_mm']} <-> {cw['witness_b_mm']})"
                )
        if report.get("closest_pairs"):
            print("Nearest Part Clearances (Top 10):")
            for cp in report["closest_pairs"][:10]:
                print(
                    f"  - {cp['part_a']} <-> {cp['part_b']}: {cp['min_distance_mm']} mm "
                    f"(at {cp['witness_a_mm']} <-> {cp['witness_b_mm']})"
                )
    elif args.cmd == "lint":
        report = lint_cad_assembly(args.file, constraints_path=args.constraints)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"File: {report['file']} ({report['part_count']} parts)")
            print(f"Mechanical Sanity & Physics Audit: {report['status']} (Errors: {report['error_count']})")
            for err in report["errors"]:
                print(f"  [ERROR] {err['code']} ({err['part']}): {err['message']}")
        if report["status"] != "PASS":
            sys.exit(1)
    elif args.cmd == "export":
        info = export_cad(args.file, step_path=args.step, stl_path=args.stl, engine=args.engine)
        print(json.dumps(info, indent=2))
    elif args.cmd == "render":
        paths = render_cad(args.file, args.out, prefix=args.prefix, engine=args.engine)
        for k, v in paths.items():
            print(f"[AG-RENDER] {k}: {v}")
    elif args.cmd == "build":
        out = run_build123d_script(args.script, args.script_args, auto_solidcraft=not args.no_solidcraft)
        if out.strip():
            print(out.strip())
    elif args.cmd == "onshape-push":
        res = onshape_push(
            args.file,
            doc_name=args.doc_name,
            did=args.did,
            wid=args.wid,
            render_out=args.render,
            constraints_path=args.constraints,
            auto_ground=not args.no_auto_ground,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "onshape-constrain":
        spec_data = json.loads(args.spec.read_text(encoding="utf-8")) if args.spec else None
        res = onshape_constrain_assembly(
            args.did,
            args.wid,
            args.eid,
            spec=spec_data,
            clear_existing=args.clear,
            emit_browser_bundle=args.emit_browser_bundle,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "onshape-export":
        onshape_export(args.did, args.wid, args.eid, args.format, args.out)
    elif args.cmd == "cots-search":
        from tools.cad_scraper import cots_search

        print(
            json.dumps(
                cots_search(
                    args.query,
                    vendor=args.vendor,
                    category=args.category,
                    program=args.program,
                    source=args.source,
                    bore_mm=args.bore_mm,
                    max_extent_mm=args.max_extent_mm,
                    has_mounting_holes=True if args.has_mounting_holes else None,
                    limit=args.limit,
                ),
                indent=2,
            )
        )
    elif args.cmd == "cots-info":
        from tools.cad_scraper import cots_info

        print(json.dumps(cots_info(args.key), indent=2))
    elif args.cmd == "solidcraft-insert":
        from tools.solidcraft_cots import solidcraft_insert_cots

        print(
            json.dumps(
                solidcraft_insert_cots(
                    args.part_key_or_sku,
                    component_name=args.name,
                    translate=args.translate,
                    anchor=args.anchor,
                    joint_type=args.joint_type,
                    target_occurrence=args.target_occurrence,
                    target_point=args.target_point,
                    target_axis=args.target_axis,
                    flip=not args.no_flip,
                    connect_port=args.port,
                    design_path=args.design,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-compile":
        from tools.solidcraft_compiler import compile_project_to_solidcraft

        print(
            json.dumps(
                compile_project_to_solidcraft(
                    args.file,
                    constraints_path=args.constraints,
                    out_sc_path=args.out,
                    export_gif=not args.no_gif,
                    live_push=args.live,
                    connect_port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-dock":
        from tools.solidcraft_compiler import serve_cots_dock_and_browser

        serve_cots_dock_and_browser(
            http_port=args.http_port,
            solidcraft_port=args.port,
            dock_mode=True,
            open_window=not args.no_open,
        )
    elif args.cmd == "solidcraft-inspect":
        from tools.solidcraft_compiler import solidcraft_inspect_assembly

        res = solidcraft_inspect_assembly(args.file, clearance_mm=args.clearance, min_volume_mm3=args.min_volume)
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-compare":
        from tools.solidcraft_compiler import solidcraft_compare_revisions

        res = solidcraft_compare_revisions(args.current_file, args.other_file, out_png=args.out)
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-compare-picture":
        from tools.solidcraft_compiler import solidcraft_compare_picture

        res = solidcraft_compare_picture(
            args.sc_file,
            args.image,
            view=args.view,
            out_overlay_png=args.out,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-fab":
        from tools.solidcraft_compiler import solidcraft_export_fabrication

        res = solidcraft_export_fabrication(
            args.file,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-pull":
        from tools.solidcraft_builder import pull_solidcraft_script

        res = pull_solidcraft_script(
            sc_path=args.file,
            out_script_path=args.script_out,
            out_params_path=args.params_out,
            live=args.live,
            connect_port=args.port,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-template":
        from tools.solidcraft_builder import build_template_part, list_parametric_templates

        if args.list or not args.template_id:
            print(json.dumps(list_parametric_templates(), indent=2))
            return
        overrides: Dict[str, Union[float, int, str]] = {}
        for kv in args.param:
            if "=" in kv:
                k, v = kv.split("=", 1)
                overrides[k.strip()] = float(v.strip())
        res = build_template_part(
            args.template_id,
            params_override=overrides,
            out_sc_path=args.out,
            export_step_path=args.step_out,
            part_name=args.name,
            insert_live=args.insert_live,
            connect_port=args.port,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "git-status":
        from tools.cad_git_manager import cad_git_status

        print(json.dumps(cad_git_status(scope_filter=args.scope), indent=2))
    elif args.cmd == "git-hygiene":
        from tools.cad_git_manager import cad_file_hygiene_audit

        print(json.dumps(cad_file_hygiene_audit(scope_filter=args.scope, fix=args.fix), indent=2))
    elif args.cmd == "git-diff":
        from tools.cad_git_manager import cad_visual_diff_head

        print(json.dumps(cad_visual_diff_head(args.path), indent=2))
    elif args.cmd == "git-commit":
        from tools.cad_git_manager import cad_git_stage_commit_push

        print(
            json.dumps(
                cad_git_stage_commit_push(
                    scope=args.scope,
                    message=args.message,
                    push=not args.no_push,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-inspect":
        from tools.solidcraft_inspector import live_inspect_state

        print(json.dumps(live_inspect_state(port=args.port), indent=2))
    elif args.cmd == "solidcraft-live-clearance":
        from tools.solidcraft_inspector import live_run_clearance_audit

        print(
            json.dumps(
                live_run_clearance_audit(
                    port=args.port,
                    clearance_mm=args.clearance,
                    custom_only=not args.all_cots,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-focus":
        from tools.solidcraft_inspector import live_focus_viewport

        print(
            json.dumps(
                live_focus_viewport(
                    target_xyz=args.target,
                    section_plane=args.section,
                    clear_section=args.clear_section,
                    view_preset=args.view,
                    port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-drive":
        from tools.solidcraft_inspector import live_drive_joint

        print(json.dumps(live_drive_joint(joint=args.joint, value=args.value, port=args.port), indent=2))
    elif args.cmd == "solidcraft-live-sweep":
        from tools.solidcraft_inspector import live_sweep_joint

        print(
            json.dumps(
                live_sweep_joint(
                    joint=args.joint,
                    from_val=args.from_val,
                    to_val=args.to_val,
                    steps=args.steps,
                    port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-param":
        from tools.solidcraft_inspector import live_change_parameter

        print(
            json.dumps(
                live_change_parameter(
                    name=args.name,
                    expression=args.expression,
                    run_sweep=args.sweep,
                    port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-sync":
        from tools.solidcraft_inspector import live_sync_and_pull

        print(
            json.dumps(
                live_sync_and_pull(
                    update_links=not args.no_links,
                    save_design=not args.no_save,
                    pull_to_git_json=not args.no_pull,
                    open_sc_path=args.open,
                    port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "solidcraft-live-fab":
        from tools.solidcraft_inspector import live_export_fabrication_pack

        print(json.dumps(live_export_fabrication_pack(port=args.port), indent=2))
    elif args.cmd == "solidcraft-live-pick":
        from tools.solidcraft_inspector import live_pick_to_source

        print(json.dumps(live_pick_to_source(project_dir=args.project_dir, port=args.port), indent=2))
    elif args.cmd == "slice-audit":
        from tools.cad_slicer_audit import slice_audit_print

        res = slice_audit_print(
            args.file,
            bed_profile=args.bed_profile,
            filament=args.filament,
            run_slicer_cli=not args.no_cli,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "fea":
        from tools.cad_fea import run_3d_fea

        res = run_3d_fea(
            step_path=args.file,
            material=args.material,
            fixed_selector=args.fixed,
            load_selector=args.load,
            force_n=args.force,
            g_load_vec_g=args.g_load,
            print_axis=args.print_axis,
            mesh_size_mm=args.mesh_size,
            backend=args.backend,
            buckle_modes=args.buckle_modes,
            out_png=args.out_png,
            out_inp=args.out_inp,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "physics-sim":
        from tools.cad_physics_sim import compile_and_simulate_physics

        res = compile_and_simulate_physics(
            step_path=args.file,
            constraints_path=args.constraints,
            engine=args.engine,
            duration_s=args.duration,
            out_mjcf=args.out_mjcf,
            out_gif=args.out_gif,
            interactive=args.interactive,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "share":
        from tools.cad_team_share import build_team_share_package

        res = build_team_share_package(
            target_path=args.target,
            out_dir=args.out_dir,
            linear_deflection=args.deflection,
            create_zip=not args.no_zip,
            gh_release_tag=args.gh_release,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "solidcraft-upgrade-format":
        from tools.solidcraft_cots import upgrade_all_sc_to_format_52

        res = upgrade_all_sc_to_format_52(
            scope=args.scope,
            workers=args.workers,
            force=args.force,
            limit=args.limit,
            exclude_projects=args.exclude_project,
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

