"""
SolidCraft COTS Library Builder, Frame Normalizer & Live/Headless MCP Bridge
============================================================================
Converts all local STEP parts in `cots/` into native SolidCraft `.sc` component
designs under `cots/solidcraft/`, normalizing each part's semantic anchor
(`bore_center`, `flange_seat`, `output_shaft`, `sense_origin`, or `bbox_center`)
to `[0, 0, 0]` with its primary bore/shaft axis aligned to `+Z` (`[0, 0, 1]`).

Each generated `.sc` design includes:
1. Canonical 3x4 rigid transformation matrix applied on `file.insert_step` so
   inserting the part in the SolidCraft GUI or via `component.insert_design`
   places its primary mating anchor at `[0, 0, 0]` along `+Z`.
2. Inferred physical material (`Aluminum`, `Steel`, `Stainless Steel`, `Brass`,
   `Nylon`, `Polycarbonate`, `ABS Plastic`) assigned to all bodies for accurate
   `assembly.mass_properties` and `.urdf` inertia exports.
3. Embedded BOM properties (`part_number`, `description`, `organization`,
   `bom_type: "purchased"`) and `io.solidcraft.cots` metadata containing all
   normalized functional frames (`bore_center`, `flange_seat`, `output_shaft`,
   `bore_min`, `bore_max`, `ear_mount`, `mount_holes_mm`, `bbox_center`, `com_mm`).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import json
import logging
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import zipfile

logger = logging.getLogger("solidcraft_cots")

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
COTS_DIR = WORKSPACE_ROOT / "cots"
SOLIDCRAFT_LIB_DIR = COTS_DIR / "solidcraft"
DB_PATH = COTS_DIR / "catalog.db"
GITIGNORE_PATH = WORKSPACE_ROOT / ".gitignore"
GITHUB_MAX_BLOB_BYTES = 95 * 1024 * 1024  # 95 MiB safe threshold under GitHub's 100 MiB limit


def find_solidcraft_cli() -> str:
    """Locate `solidcraft-cli` across Windows, macOS, and Linux."""
    env_cli = os.environ.get("SOLIDCRAFT_CLI")
    if env_cli and Path(env_cli).is_file():
        return str(Path(env_cli).resolve())

    which_cli = shutil.which("solidcraft-cli")
    if which_cli:
        return which_cli

    candidates: List[Path] = []
    if platform.system() == "Windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        if local_app:
            candidates.append(Path(local_app) / "Programs" / "SolidCraft" / "solidcraft-cli.exe")
        candidates.append(Path(prog_files) / "SolidCraft" / "solidcraft-cli.exe")
    elif platform.system() == "Darwin":
        candidates.extend(
            [
                Path("/Applications/SolidCraft.app/Contents/MacOS/solidcraft-cli"),
                Path.home() / "Applications/SolidCraft.app/Contents/MacOS/solidcraft-cli",
                Path.home() / ".local/bin/solidcraft-cli",
                Path("/opt/homebrew/bin/solidcraft-cli"),
                Path("/usr/local/bin/solidcraft-cli"),
            ]
        )
    else:
        candidates.extend(
            [
                Path.home() / ".local/bin/solidcraft-cli",
                Path("/usr/local/bin/solidcraft-cli",),
                Path("/usr/bin/solidcraft-cli"),
            ]
        )

    existing_candidates = [c.resolve() for c in candidates if c.is_file()]
    if existing_candidates:
        # Prefer the most recently modified installation (e.g. if v0.40+ MSI is in Program Files alongside an older AppData install)
        existing_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(existing_candidates[0])

    raise FileNotFoundError(
        "Could not locate `solidcraft-cli`. Install SolidCraft or add `solidcraft-cli` to PATH."
    )


def ensure_solidcraft_schema(conn: sqlite3.Connection) -> None:
    """Ensure `local_cots_parts` has the SolidCraft library columns."""
    cols = {c[1] for c in conn.execute("PRAGMA table_info(local_cots_parts)").fetchall()}
    migrations = [
        ("solidcraft_path", "TEXT"),
        ("solidcraft_material", "TEXT"),
        ("solidcraft_anchor", "TEXT"),
        ("solidcraft_matrix_json", "TEXT"),
        ("solidcraft_frames_json", "TEXT"),
    ]
    for col_name, col_type in migrations:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE local_cots_parts ADD COLUMN {col_name} {col_type}")
    conn.commit()


def infer_solidcraft_material(
    vendor: str,
    category: str,
    title: str,
    sku: Optional[str] = None,
) -> str:
    """
    Map a robotics COTS part to one of SolidCraft's built-in physical materials:
    `Aluminum`, `Steel`, `Stainless Steel`, `Brass`, `Nylon`, `Polycarbonate`, `ABS Plastic`.
    """
    t = f"{vendor or ''} {category or ''} {sku or ''} {title or ''}".lower()
    cat = (category or "").lower()

    if "stainless" in t:
        return "Stainless Steel"
    if any(k in t for k in ("brass", "ruthex", "rx-m", "heat-set", "heat set", "2304-")):
        return "Brass"
    if any(k in t for k in ("polycarbonate", "lexan", "clear shield")):
        return "Polycarbonate"
    if any(k in t for k in ("acetal", "delrin", "pom", "nylon", "plastic", "uhmw", "ptfe", "teflon")):
        return "Nylon"
    if cat == "bearings" or any(
        k in t
        for k in (
            "ball bearing",
            "flanged bearing",
            "thrust bearing",
            "v-groove bearing",
            "needle bearing",
            "slewing bearing",
            "linear guide",
            "mgn",
        )
    ):
        return "Steel"
    if cat == "fasteners" or any(
        k in t
        for k in (
            "screw",
            "bolt",
            "hex nut",
            "locknut",
            "nyloc",
            "thin nut",
            "t-nut",
            "washer",
            "shim",
            "dowel",
            "clevis pin",
            "cotter",
            "retaining ring",
            "snap ring",
            "e-clip",
            "circlip",
            "rex shaft",
            "d-shaft",
            "steel shaft",
            "lead screw",
            "steel miter",
            "steel bevel",
            "steel spur",
            "steel pinion",
            "chain",
        )
    ):
        if "aluminum" in t:
            return "Aluminum"
        return "Steel"
    if cat in ("wheels", "electronics", "servos", "electrical") or any(
        k in t
        for k in (
            "wheel",
            "mecanum",
            "omni",
            "gecko",
            "compliant",
            "gripforce",
            "traction",
            "tire",
            "battery",
            "control hub",
            "expansion hub",
            "spark max",
            "spark flex",
            "sensor",
            "encoder",
            "camera",
            "servo",
            "axon",
            "belt",
            "timing belt",
            "round belt",
        )
    ):
        if any(k in t for k in ("servoblock", "servo horn", "servo hub", "servo plate", "servo bracket", "servo mount", "battery mount")):
            return "Aluminum"
        return "ABS Plastic"

    if "steel" in t and "aluminum" not in t:
        return "Steel"
    return "Aluminum"


def select_recommended_anchor(row: Dict[str, Any], frames: Dict[str, Any]) -> str:
    """Choose the primary semantic anchor frame for normalizing `[0, 0, 0]`."""
    cat_low = str(row.get("category") or "").lower()
    t_low = f"{row.get('sku') or ''} {row.get('title') or ''} {row.get('part_id') or ''}".lower()

    if cat_low in ("motors", "servos") or any(k in t_low for k in ("servo", "motor", "5202", "5203", "5204", "rev-21-1650")):
        pref = "output_shaft"
    elif cat_low == "bearings" or any(k in t_low for k in ("bearing", "brg-")):
        pref = "flange_seat"
    elif any(k in t_low for k in ("sensor", "encoder", "camera", "limelight", "odometry")):
        pref = "sense_origin"
    else:
        pref = "bore_center"

    for candidate in (pref, "bore_center", "flange_seat", "output_shaft", "bbox_center"):
        f_obj = frames.get(candidate)
        if isinstance(f_obj, dict) and isinstance(f_obj.get("origin_mm"), list) and len(f_obj["origin_mm"]) == 3:
            return candidate
    return "bbox_center"


def _axis_rotation_to_z(axis_name: str) -> List[List[float]]:
    """
    Return a right-handed 3x3 rotation matrix R in SO(3) (det = +1) that maps
    the unit vector along `axis_name` ('X', 'Y', or 'Z') to +Z = [0, 0, 1].
    """
    ax = (axis_name or "Z").upper().strip()
    if ax.endswith("X"):
        # Maps +X -> +Z, +Y -> +Y, +Z -> -X (det = +1, +90 deg about Y)
        return [
            [0.0, 0.0, -1.0],
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
        ]
    elif ax.endswith("Y"):
        # Maps +Y -> +Z, +X -> +X, +Z -> -Y (det = +1, -90 deg about X; converts Y-up to Z-up)
        return [
            [1.0, 0.0, 0.0],
            [0.0, 0.0, -1.0],
            [0.0, 1.0, 0.0],
        ]
    else:
        return [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]


def _apply_rt(rot: List[List[float]], trans: List[float], pt: List[float]) -> List[float]:
    return [
        round(rot[0][0] * pt[0] + rot[0][1] * pt[1] + rot[0][2] * pt[2] + trans[0], 4),
        round(rot[1][0] * pt[0] + rot[1][1] * pt[1] + rot[1][2] * pt[2] + trans[1], 4),
        round(rot[2][0] * pt[0] + rot[2][1] * pt[1] + rot[2][2] * pt[2] + trans[2], 4),
    ]


def _rotate_axis_label(rot: List[List[float]], axis_label: str) -> str:
    vec_map = {"X": [1.0, 0.0, 0.0], "Y": [0.0, 1.0, 0.0], "Z": [0.0, 0.0, 1.0]}
    v = vec_map.get((axis_label or "Z").upper().strip()[-1], [0.0, 0.0, 1.0])
    rv = [
        rot[0][0] * v[0] + rot[0][1] * v[1] + rot[0][2] * v[2],
        rot[1][0] * v[0] + rot[1][1] * v[1] + rot[1][2] * v[2],
        rot[2][0] * v[0] + rot[2][1] * v[1] + rot[2][2] * v[2],
    ]
    abs_rv = [abs(x) for x in rv]
    idx = abs_rv.index(max(abs_rv))
    return ("X", "Y", "Z")[idx]


def compute_normalization_transform(row: Dict[str, Any]) -> Tuple[str, List[float], Dict[str, Any]]:
    """
    Compute the 12-element row-major 3x4 rigid transformation matrix that moves
    the part's semantic anchor to `[0, 0, 0]` and aligns its primary bore/shaft
    axis with `+Z`, returning `(anchor_name, matrix_12, normalized_frames_dict)`.
    """
    raw_frames: Dict[str, Any] = {}
    if row.get("functional_frames_json"):
        try:
            raw_frames = (
                json.loads(row["functional_frames_json"])
                if isinstance(row["functional_frames_json"], str)
                else dict(row["functional_frames_json"])
            )
        except Exception:
            raw_frames = {}

    bmin = row.get("bbox_min_mm")
    bmax = row.get("bbox_max_mm")
    com = row.get("center_of_mass_mm")
    for name_var, val in (("bmin", bmin), ("bmax", bmax), ("com", com)):
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
                if name_var == "bmin":
                    bmin = parsed
                elif name_var == "bmax":
                    bmax = parsed
                else:
                    com = parsed
            except Exception:
                pass

    if "bbox_center" not in raw_frames:
        if isinstance(bmin, list) and isinstance(bmax, list) and len(bmin) == 3 and len(bmax) == 3:
            raw_frames["bbox_center"] = {
                "origin_mm": [
                    round(0.5 * (bmin[0] + bmax[0]), 4),
                    round(0.5 * (bmin[1] + bmax[1]), 4),
                    round(0.5 * (bmin[2] + bmax[2]), 4),
                ],
                "axis": row.get("primary_axis") or "Z",
            }
        elif isinstance(com, list) and len(com) == 3:
            raw_frames["bbox_center"] = {
                "origin_mm": [float(com[0]), float(com[1]), float(com[2])],
                "axis": row.get("primary_axis") or "Z",
            }
        else:
            raw_frames["bbox_center"] = {"origin_mm": [0.0, 0.0, 0.0], "axis": "Z"}

    anchor_name = select_recommended_anchor(row, raw_frames)
    anchor_frame = raw_frames.get(anchor_name) or raw_frames["bbox_center"]
    p0 = [float(x) for x in anchor_frame.get("origin_mm", [0.0, 0.0, 0.0])]
    prim_axis = str(anchor_frame.get("axis") or raw_frames.get("primary_axis") or row.get("primary_axis") or "Z")

    rot = _axis_rotation_to_z(prim_axis)
    trans = [
        round(-(rot[0][0] * p0[0] + rot[0][1] * p0[1] + rot[0][2] * p0[2]), 4),
        round(-(rot[1][0] * p0[0] + rot[1][1] * p0[1] + rot[1][2] * p0[2]), 4),
        round(-(rot[2][0] * p0[0] + rot[2][1] * p0[1] + rot[2][2] * p0[2]), 4),
    ]
    matrix_12 = [
        rot[0][0], rot[0][1], rot[0][2], trans[0],
        rot[1][0], rot[1][1], rot[1][2], trans[1],
        rot[2][0], rot[2][1], rot[2][2], trans[2],
    ]

    norm_frames: Dict[str, Any] = {
        "recommended_anchor": anchor_name,
        "primary_axis": "Z",
        "min_bore_radius_mm": raw_frames.get("min_bore_radius_mm") or row.get("min_bore_radius_mm"),
    }

    for k, v in raw_frames.items():
        if isinstance(v, dict) and isinstance(v.get("origin_mm"), list) and len(v["origin_mm"]) == 3:
            entry: Dict[str, Any] = {
                "origin_mm": _apply_rt(rot, trans, [float(c) for c in v["origin_mm"]]),
                "axis": _rotate_axis_label(rot, str(v.get("axis") or prim_axis)),
                "z_vec": [0.0, 0.0, 1.0] if _rotate_axis_label(rot, str(v.get("axis") or prim_axis)) == "Z" else (
                    [1.0, 0.0, 0.0] if _rotate_axis_label(rot, str(v.get("axis") or prim_axis)) == "X" else [0.0, 1.0, 0.0]
                ),
            }
            if isinstance(v.get("mount_holes_mm"), list):
                holes_out = []
                for h in v["mount_holes_mm"]:
                    if isinstance(h, dict) and isinstance(h.get("center_mm"), list) and len(h["center_mm"]) == 3:
                        h_copy = dict(h)
                        h_copy["center_mm"] = _apply_rt(rot, trans, [float(c) for c in h["center_mm"]])
                        holes_out.append(h_copy)
                    elif isinstance(h, list) and len(h) == 3:
                        holes_out.append(_apply_rt(rot, trans, [float(c) for c in h]))
                entry["mount_holes_mm"] = holes_out
            norm_frames[k] = entry
        elif k == "com_mm" and isinstance(v, list) and len(v) == 3:
            norm_frames["com_mm"] = _apply_rt(rot, trans, [float(c) for c in v])

    return anchor_name, matrix_12, norm_frames


def get_solidcraft_rel_path(local_path: str) -> str:
    """
    Convert a `cots/`-relative STEP path (e.g. `vendors/gobilda/motors/5203.step`)
    to its `cots/`-relative SolidCraft `.sc` path (`solidcraft/vendors/gobilda/motors/5203.sc`).
    """
    rel = str(local_path).replace("\\", "/").lstrip("/")
    if rel.lower().startswith("cots/"):
        rel = rel[5:]
    p = Path(rel)
    return (Path("solidcraft") / p.with_suffix(".sc")).as_posix()


class SolidCraftHeadlessWorker:
    """
    Persistent `solidcraft-cli mcp --headless --allow-any-path` stdio JSON-RPC worker.
    Avoids per-file process startup overhead while isolating memory and crashes.
    """

    def __init__(self, cli_path: str, max_file_size: str = "2GB"):
        self.cli_path = cli_path
        self.max_file_size = max_file_size
        self.proc: Optional[subprocess.Popen] = None
        self._req_id = 0
        self._start()

    def _start(self) -> None:
        self.close()
        self.proc = subprocess.Popen(
            [
                self.cli_path,
                "--max-file-size",
                self.max_file_size,
                "mcp",
                "--headless",
                "--allow-any-path",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "solidcraft-cots-builder", "version": "1.0.0"},
        })

    def _rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self._req_id += 1
        payload = json.dumps({"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params})
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("solidcraft-cli headless worker EOF")
        return json.loads(line)

    def convert_part(
        self,
        step_abs_path: Path,
        sc_abs_path: Path,
        comp_name: str,
        sku: str,
        title: str,
        vendor: str,
        category: str,
        material: str,
        anchor_name: str,
        matrix_12: List[float],
        norm_frames: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Convert one STEP file into a normalized, material-assigned, metadata-tagged `.sc` file."""
        sc_abs_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            r1 = self._rpc("tools/call", {
                "name": "batch",
                "arguments": {
                    "verbose": False,
                    "commands": [
                        {"command": "file.new", "params": {}},
                        {
                            "command": "file.insert_step",
                            "params": {
                                "path": str(step_abs_path),
                                "name": comp_name,
                                "matrix": matrix_12,
                            },
                        },
                        {
                            "command": "document.metadata.set",
                            "params": {
                                "values": {
                                    "part_number": sku or comp_name,
                                    "description": title or comp_name,
                                    "organization": vendor,
                                    "project": f"Robotics COTS ({category})",
                                    "io.solidcraft.cots": {
                                        "sku": sku,
                                        "vendor": vendor,
                                        "category": category,
                                        "title": title,
                                        "material": material,
                                        "recommended_anchor": anchor_name,
                                        "normalization_matrix_3x4": matrix_12,
                                        "frames": norm_frames,
                                    },
                                }
                            },
                        },
                    ],
                },
            })
            if "error" in r1:
                return False, str(r1["error"])
            content_list = r1.get("result", {}).get("content", [])
            if not content_list:
                return False, f"Empty MCP response: {r1}"
            res1 = json.loads(content_list[0].get("text", "{}"))
            if not res1.get("ok", False):
                return False, f"Import failed: {res1.get('failed')}"

            insert_res = res1["results"][1]
            bodies = insert_res.get("created_bodies") or insert_res.get("bodies") or []
            comp_id = insert_res.get("component", comp_name)

            cmds2: List[Dict[str, Any]] = []
            if bodies and material and material != "Default":
                cmds2.append({"command": "material.assign", "params": {"bodies": bodies, "material": material}})
            if comp_id:
                cmds2.append({
                    "command": "component.metadata.set",
                    "params": {
                        "component": comp_id,
                        "part_number": sku or comp_name,
                        "description": title or comp_name,
                        "bom_type": "purchased",
                        "properties": {
                            "vendor": vendor,
                            "category": category,
                            "recommended_anchor": anchor_name,
                        },
                    },
                })
            for fname in ("bore_center", "flange_seat", "output_shaft", "bore_min", "bore_max", "sense_origin"):
                fobj = (norm_frames or {}).get(fname)
                if isinstance(fobj, dict) and isinstance(fobj.get("origin_mm"), list) and len(fobj["origin_mm"]) == 3:
                    cmds2.append({
                        "command": "joint.origin",
                        "params": {
                            "name": fname,
                            "point": fobj["origin_mm"],
                            "z": fobj.get("z_vec", [0.0, 0.0, 1.0]),
                        },
                    })
            mh_list = ((norm_frames or {}).get("ear_mount") or {}).get("mount_holes_mm") or []
            for idx_mh, pt in enumerate(mh_list[:8], 1):
                if isinstance(pt, list) and len(pt) == 3:
                    cmds2.append({"command": "joint.origin", "params": {"name": f"mount_hole_{idx_mh}", "point": pt, "z": [0.0, 0.0, 1.0]}})
            cmds2.append({
                "command": "file.save",
                "params": {
                    "path": str(sc_abs_path),
                    "overwrite": True,
                },
            })

            r2 = self._rpc("tools/call", {
                "name": "batch",
                "arguments": {
                    "verbose": False,
                    "commands": cmds2,
                },
            })
            if "error" in r2:
                return False, str(r2["error"])
            res2 = json.loads(r2["result"]["content"][0]["text"])
            if not res2.get("ok", False):
                return False, f"Save failed: {res2.get('failed')}"
            return True, "ok"
        except Exception as exc:
            self._start()
            return False, str(exc)

    def upgrade_file_format(
        self,
        sc_abs_path: Path,
        target_format: str = "solidcraft/52",
        is_cots: bool = False,
        repair_info: Optional[Tuple[str, str]] = None,
    ) -> Tuple[bool, str]:
        """
        Open an existing `.sc` file and re-save it in-place to upgrade to `target_format` + `solidcore` cache.
        For COTS `.sc` parts (`is_cots=True`), also pre-bake native `joint.origin` mate connectors from
        `io.solidcraft.cots.frames` and apply any repaired `(category, material)` classification.
        """
        bak_path = sc_abs_path.with_name(f".bak_fmt52_{sc_abs_path.name}")
        try:
            doc_pre: Dict[str, Any] = {}
            if is_cots:
                try:
                    with zipfile.ZipFile(sc_abs_path, "r") as zf:
                        doc_pre = json.loads(zf.read("document.json"))
                except Exception:
                    doc_pre = {}

            shutil.copy2(sc_abs_path, bak_path)
            r_open = self._rpc("tools/call", {"name": "open", "arguments": {"path": str(sc_abs_path)}})
            if "error" in r_open or r_open.get("result", {}).get("isError"):
                if bak_path.exists():
                    os.replace(bak_path, sc_abs_path)
                return False, f"open error: {r_open}"

            if is_cots and doc_pre:
                cots_meta = dict((doc_pre.get("metadata") or {}).get("io.solidcraft.cots") or {})
                if repair_info is None and cots_meta:
                    from tools.cad_scraper import normalize_category

                    cur_cat = str(cots_meta.get("category") or "")
                    cur_mat = str(cots_meta.get("material") or "")
                    cur_sku = str(cots_meta.get("sku") or "")
                    cur_title = str(cots_meta.get("title") or sc_abs_path.stem)
                    cur_vendor = str(cots_meta.get("vendor") or "")
                    expected_cat = normalize_category("", f"{cur_sku} {cur_title}") if cur_cat == "electronics" else cur_cat
                    expected_mat = infer_solidcraft_material(cur_vendor, expected_cat, cur_title, cur_sku)
                    if expected_cat != cur_cat or (cur_mat and expected_mat != cur_mat):
                        repair_info = (expected_cat, expected_mat)

                frames = cots_meta.get("frames") or {}
                existing_origins = {
                    str(jo.get("name"))
                    for jo in (doc_pre.get("joint_origins") or [])
                    if isinstance(jo, dict) and jo.get("name")
                }
                cmds: List[Dict[str, Any]] = []
                if repair_info is not None:
                    new_cat, new_mat = repair_info
                    r_insp = self._rpc("tools/call", {"name": "inspect_design", "arguments": {}})
                    if "error" not in r_insp and not r_insp.get("result", {}).get("isError"):
                        bodies = [
                            b["name"]
                            for b in json.loads(r_insp["result"]["content"][0]["text"]).get("bodies", [])
                            if b.get("name")
                        ]
                        if bodies and new_mat:
                            cmds.append({"command": "material.assign", "params": {"bodies": bodies, "material": new_mat}})
                    cots_meta["category"] = new_cat
                    cots_meta["material"] = new_mat
                    cmds.append({
                        "command": "document.metadata.set",
                        "params": {"values": {"project": f"Robotics COTS ({new_cat})", "io.solidcraft.cots": cots_meta}},
                    })

                for fname in ("bore_center", "flange_seat", "output_shaft", "bore_min", "bore_max", "sense_origin"):
                    fobj = frames.get(fname)
                    if fname not in existing_origins and isinstance(fobj, dict) and isinstance(fobj.get("origin_mm"), list) and len(fobj["origin_mm"]) == 3:
                        cmds.append({
                            "command": "joint.origin",
                            "params": {
                                "name": fname,
                                "point": fobj["origin_mm"],
                                "z": fobj.get("z_vec", [0.0, 0.0, 1.0]),
                            },
                        })
                mh_list = (frames.get("ear_mount") or {}).get("mount_holes_mm") or []
                for idx_mh, pt in enumerate(mh_list[:8], 1):
                    jo_nm = f"mount_hole_{idx_mh}"
                    if jo_nm not in existing_origins and isinstance(pt, list) and len(pt) == 3:
                        cmds.append({"command": "joint.origin", "params": {"name": jo_nm, "point": pt, "z": [0.0, 0.0, 1.0]}})

                if cmds:
                    self._rpc("tools/call", {"name": "batch", "arguments": {"verbose": False, "commands": cmds}})

            r_save = self._rpc("tools/call", {"name": "save", "arguments": {"path": str(sc_abs_path)}})
            if "error" in r_save or r_save.get("result", {}).get("isError"):
                if bak_path.exists():
                    os.replace(bak_path, sc_abs_path)
                return False, f"save error: {r_save}"
            fmt, kernel, _ = get_sc_format_info(sc_abs_path)
            if fmt != target_format or (kernel is not None and kernel != "solidcore"):
                if bak_path.exists():
                    os.replace(bak_path, sc_abs_path)
                return False, f"post-save verification mismatch (format={fmt}, kernel={kernel})"
            bak_path.unlink(missing_ok=True)
            return True, "ok"
        except Exception as exc:
            if bak_path.exists():
                try:
                    os.replace(bak_path, sc_abs_path)
                except Exception:
                    pass
            self._start()
            return False, str(exc)

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


def get_sc_format_info(sc_abs_path: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Inspect a `.sc` ZIP archive in <0.1 ms and return `(doc_format, cache_kernel, cache_app)`.
    Example for SolidCraft v0.40.0-rc.4 Format 52: `('solidcraft/52', 'solidcore', '0.40.0-rc.4')`.
    """
    try:
        with zipfile.ZipFile(sc_abs_path, "r") as zf:
            doc = json.loads(zf.read("document.json"))
            doc_fmt = doc.get("format")
            kernel = None
            app = None
            if "cache/model.json" in zf.namelist():
                cm = json.loads(zf.read("cache/model.json"))
                kernel = cm.get("kernel")
                app = cm.get("app")
            return doc_fmt, kernel, app
    except Exception:
        return None, None, None


def heal_cots_catalog_and_export_agent_index(db_path: Path = DB_PATH) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, Any]]:
    """
    1. Re-evaluate `normalize_category` + `infer_solidcraft_material` across all verified
       rows in `cots/catalog.db` (fixing substring misclassifications such as mechanical
       hubs or angled brackets previously marked `electronics` / `ABS Plastic`).
    2. Export `cots/cots_agent_index.jsonl` and `cots/COTS_AGENT_GUIDE.md` so AI agents
       have grep-instant access to every verified COTS SKU, normalized `+Z` stackup,
       bore profile, bolt pattern, gear/sprocket pitch diameter, and `.sc` path.
    Returns `(repaired_abs_sc_map, summary_dict)`.
    """
    from tools.cad_scraper import _format_local_row, normalize_category, rebuild_frc_ftc_index

    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    rows = conn.execute("SELECT * FROM local_cots_parts WHERE status = 'VERIFIED_STEP'").fetchall()
    updates: List[Tuple[str, str, str]] = []
    repaired_sc_map: Dict[str, Tuple[str, str]] = {}

    for r in rows:
        old_cat = str(r["category"] or "")
        old_mat = str(r["solidcraft_material"] or "")
        lp = str(r["local_path"] or "")
        parts = lp.replace("\\", "/").split("/")
        # Preserve vendor sub-folder category when explicit under vendors/<vendor>/<cat>/ unless it was 'electronics'
        if parts[0] == "vendors" and len(parts) >= 3 and old_cat != "electronics":
            new_cat = old_cat
        else:
            new_cat = normalize_category("", f"{r['sku'] or ''} {r['title'] or ''} {lp}")
        new_mat = infer_solidcraft_material(r["vendor"] or "", new_cat, r["title"] or "", r["sku"])
        if new_cat != old_cat or (old_mat and new_mat != old_mat):
            updates.append((new_cat, new_mat, r["part_id"]))
            if r["solidcraft_path"]:
                sc_rel = str(r["solidcraft_path"])
                sc_abs = (WORKSPACE_ROOT / sc_rel) if sc_rel.startswith("cots/") else (COTS_DIR / sc_rel)
                repaired_sc_map[str(sc_abs.resolve()).lower()] = (new_cat, new_mat)

    if updates:
        conn.executemany(
            "UPDATE local_cots_parts SET category = ?, solidcraft_material = ? WHERE part_id = ?",
            updates,
        )
        conn.commit()
        rebuild_frc_ftc_index(conn)

    # Export grep-friendly JSONL index + Markdown agent quick-reference
    refreshed = conn.execute(
        "SELECT * FROM local_cots_parts WHERE status = 'VERIFIED_STEP' ORDER BY vendor, category, COALESCE(sku, part_id)"
    ).fetchall()
    jsonl_path = COTS_DIR / "cots_agent_index.jsonl"
    cat_counts: Dict[str, int] = {}
    with open(jsonl_path, "w", encoding="utf-8") as f_out:
        for r in refreshed:
            d = _format_local_row(r)
            cat = str(d.get("category") or "structure")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            compact = {
                "part_id": d["part_id"],
                "sku": d.get("sku"),
                "vendor": d.get("vendor"),
                "category": cat,
                "title": d.get("title"),
                "sc_path": d.get("solidcraft_path"),
                "step_path": d.get("local_path"),
                "anchor": d.get("solidcraft_anchor") or "bore_center",
                "extents_mm": d.get("extents_mm"),
                "spec": d.get("agent_spec"),
            }
            f_out.write(json.dumps(compact, separators=(",", ":")) + "\n")

    conn.close()
    guide_path = COTS_DIR / "COTS_AGENT_GUIDE.md"
    cat_lines = "\n".join(f"- **`{k}`**: {v} verified parts" for k, v in sorted(cat_counts.items()))
    guide_md = f"""# Agent-First COTS Hardware Guide (`cots/`)

This workspace contains **{len(refreshed)} verified COTS parts** pre-compiled into **SolidCraft Format 52 (`solidcraft/52` + `solidcore`)** under `cots/solidcraft/` with normalized `+Z` functional frames and pre-baked `joint.origin` mate connectors.

## Category Breakdown
{cat_lines}

## Fast Agent Lookup Order
1. **MCP Tool (`ares_onshape_local`)**: Call `cots_search(query=..., bore_mm=..., category=...)` and `cots_info(identifier=...)` — every result includes `agent_spec` (`bore_profile`, `mounting_pattern`, `axial_stackup_mm`, `kinematics.pitch_diameter_mm`, `mass_estimate_g`) plus ready-to-paste `solidcraft_snippet`, `build123d_snippet`, and `mate_graph_snippet`.
2. **Offline / Subagent Grep (`<50 ms`)**: Search [`cots_agent_index.jsonl`](cots_agent_index.jsonl) directly by SKU, tooth count (`"teeth":96`), bore profile (`8mm_REX_hex`, `1/2in_hex`), or bolt pattern (`goBILDA_16mm_M4_grid`).

## Normalized Frame & Pre-Baked `joint.origin` Convention
Every `cots/solidcraft/*.sc` part has its primary mating anchor at `[0, 0, 0]` with its bore/shaft axis aligned to `+Z` (`[0, 0, 1]`), and includes pre-baked `joint.origin` features:
- `bore_center`, `bore_min`, `bore_max` — axial stackup bounds (`axial_stackup_mm.z_min_mm` .. `z_max_mm`)
- `flange_seat` — bearing/collar flange seating plane
- `output_shaft` — motor/servo output face
- `mount_hole_1..8` — detected M3 / M4 / #10-32 bolt circle centers
"""
    guide_path.write_text(guide_md, encoding="utf-8")

    summary = {
        "verified_parts_indexed": len(refreshed),
        "repaired_categories_and_materials": len(updates),
        "agent_index_jsonl": str(jsonl_path),
        "agent_guide_md": str(guide_path),
    }
    return repaired_sc_map, summary


def upgrade_all_sc_to_format_52(
    scope: str = "all",
    workers: int = 8,
    force: bool = False,
    limit: Optional[int] = None,
    exclude_projects: Optional[List[str]] = None,
    target_format: str = "solidcraft/52",
) -> Dict[str, Any]:
    """
    Batch-upgrade existing `.sc` files in `cots/solidcraft/` and/or `projects/`
    to SolidCraft Format 52 (`solidcraft/52` + `solidcore` kernel cache) using
    parallel persistent headless workers.
    Processes leaf COTS parts first (baking `joint.origin`s + repaired materials),
    then project leaf parts before assemblies.
    """
    cli_path = find_solidcraft_cli()
    excl = {p.strip().lower() for p in (exclude_projects or []) if p.strip()}
    repaired_map, cots_heal_summary = (
        heal_cots_catalog_and_export_agent_index(DB_PATH)
        if scope in ("all", "cots")
        else ({}, {})
    )

    candidates: List[Path] = []
    if scope in ("all", "cots"):
        cots_files = [
            p for p in (COTS_DIR / "solidcraft").rglob("*.sc")
            if not p.name.startswith(".bak_fmt52_")
        ]
        cots_files.sort(key=lambda p: p.stat().st_size)
        candidates.extend(cots_files)

    if scope in ("all", "projects"):
        proj_root = WORKSPACE_ROOT / "projects"
        if proj_root.exists():
            proj_files: List[Path] = []
            for p in proj_root.rglob("*.sc"):
                if p.name.startswith(".bak_fmt52_"):
                    continue
                rel_parts = p.relative_to(proj_root).parts
                if rel_parts and rel_parts[0].lower() in excl:
                    continue
                proj_files.append(p)
            # Upgrade leaf parts before sub-designs before top-level assemblies
            proj_files.sort(
                key=lambda p: (
                    1 if "assembly" in p.stem.lower() else 0,
                    p.stat().st_size,
                )
            )
            candidates.extend(proj_files)

    already_upgraded = 0
    todo: List[Path] = []
    for p in candidates:
        fmt, kernel, _ = get_sc_format_info(p)
        needs_repair = str(p.resolve()).lower() in repaired_map
        if not force and not needs_repair and fmt == target_format and kernel in ("solidcore", None):
            already_upgraded += 1
            continue
        todo.append(p)

    if limit is not None:
        todo = todo[: int(limit)]

    logger.info(
        "Format 52 audit: total=%d | already_format_52=%d | to_upgrade=%d (workers=%d, cli=%s)",
        len(candidates),
        already_upgraded,
        len(todo),
        workers,
        cli_path,
    )
    if not todo:
        return {
            "status": "ok",
            "total_scanned": len(candidates),
            "already_format_52": already_upgraded,
            "upgraded": 0,
            "failed": 0,
            "gzipped_over_95mb": 0,
            "elapsed_sec": 0.0,
            "failures": [],
        }

    # Separate COTS leaf files (fully parallel) from project assemblies if scope == 'all'
    def _run_pool(batch_paths: List[Path], pool_workers: int, stats: Dict[str, int], failures: List[Dict[str, str]], t_start: float, grand_total: int) -> None:
        if not batch_paths:
            return
        q: "queue.Queue[Optional[Path]]" = queue.Queue()
        for bp in batch_paths:
            q.put(bp)
        actual_w = max(1, min(pool_workers, len(batch_paths)))
        for _ in range(actual_w):
            q.put(None)

        lock = threading.Lock()

        def _w_fn() -> None:
            w = SolidCraftHeadlessWorker(cli_path)
            since_recycle = 0
            try:
                while True:
                    item = q.get()
                    if item is None:
                        q.task_done()
                        break
                    sz_before = item.stat().st_size if item.exists() else 0
                    had_gz = Path(str(item) + ".gz").exists()
                    is_cots_item = COTS_DIR in item.parents
                    rep_info = repaired_map.get(str(item.resolve()).lower()) if is_cots_item else None
                    ok, msg = w.upgrade_file_format(
                        item,
                        target_format=target_format,
                        is_cots=is_cots_item,
                        repair_info=rep_info,
                    )
                    since_recycle += 1
                    if since_recycle >= 80 or sz_before > 25 * 1024 * 1024:
                        w._start()
                        since_recycle = 0
                    with lock:
                        if ok:
                            stats["upgraded"] += 1
                            if handle_large_sc_gzip_and_gitignore(item):
                                stats["gzipped_over_95mb"] += 1
                            elif had_gz and item.exists():
                                # Refresh pre-existing .sc.gz companion if present
                                gz_p = Path(str(item) + ".gz")
                                with open(item, "rb") as f_in, gzip.open(gz_p, "wb", compresslevel=6) as f_out:
                                    shutil.copyfileobj(f_in, f_out)
                        else:
                            stats["failed"] += 1
                            failures.append({"path": str(item.relative_to(WORKSPACE_ROOT)), "error": msg})
                            logger.warning("FAILED upgrade %s: %s", item.name, msg)

                        done = stats["upgraded"] + stats["failed"]
                        if done % 100 == 0 or done == grand_total:
                            elapsed = max(0.001, time.time() - t_start)
                            rate = done / elapsed
                            eta = (grand_total - done) / max(0.01, rate)
                            logger.info(
                                "Upgrade progress: %d/%d (%.1f%%) | ok=%d fail=%d | %.1f files/s | ETA %.0fs",
                                done,
                                grand_total,
                                100.0 * done / grand_total,
                                stats["upgraded"],
                                stats["failed"],
                                rate,
                                eta,
                            )
                    q.task_done()
            finally:
                w.close()

        threads = [threading.Thread(target=_w_fn, daemon=True) for _ in range(actual_w)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    cots_todo = [p for p in todo if COTS_DIR in p.parents]
    proj_leaf_todo = [p for p in todo if COTS_DIR not in p.parents and "assembly" not in p.stem.lower()]
    proj_asm_todo = [p for p in todo if COTS_DIR not in p.parents and "assembly" in p.stem.lower()]

    stats = {"upgraded": 0, "failed": 0, "gzipped_over_95mb": 0}
    failures: List[Dict[str, str]] = []
    t0 = time.time()
    grand_total = len(todo)

    _run_pool(cots_todo, workers, stats, failures, t0, grand_total)
    _run_pool(proj_leaf_todo, workers, stats, failures, t0, grand_total)
    _run_pool(proj_asm_todo, min(4, workers), stats, failures, t0, grand_total)

    return {
        "status": "ok" if stats["failed"] == 0 else "partial",
        "target_format": target_format,
        "total_scanned": len(candidates),
        "already_format_52": already_upgraded,
        "upgraded": stats["upgraded"],
        "failed": stats["failed"],
        "gzipped_over_95mb": stats["gzipped_over_95mb"],
        "elapsed_sec": round(time.time() - t0, 2),
        "cots_heal": cots_heal_summary,
        "failures": failures[:30],
    }


def handle_large_sc_gzip_and_gitignore(sc_abs_path: Path) -> bool:
    """
    If a generated `.sc` file exceeds 95 MiB, add the raw `.sc` path to `.gitignore`
    so GitHub pushes succeed, and keep a `.sc.gz` companion ONLY when the gzipped
    archive itself fits within GitHub's 95 MiB limit (since `.sc` is already ZIP-compressed).
    Returns True if the oversized `.sc` was handled.
    """
    if not sc_abs_path.exists() or sc_abs_path.stat().st_size <= GITHUB_MAX_BLOB_BYTES:
        return False

    rel_repo_path = sc_abs_path.resolve().relative_to(WORKSPACE_ROOT.resolve()).as_posix()
    if GITIGNORE_PATH.exists():
        existing_Text = GITIGNORE_PATH.read_text(encoding="utf-8")
        if rel_repo_path not in existing_Text:
            with open(GITIGNORE_PATH, "a", encoding="utf-8") as f_gi:
                f_gi.write(f"\n{rel_repo_path}\n")

    gz_path = Path(str(sc_abs_path) + ".gz")
    if not gz_path.exists() or gz_path.stat().st_mtime < sc_abs_path.stat().st_mtime:
        with open(sc_abs_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    if gz_path.exists() and gz_path.stat().st_size > GITHUB_MAX_BLOB_BYTES:
        gz_path.unlink(missing_ok=True)
    return True


def decompress_missing_gz_archives(root_dir: Path = COTS_DIR) -> int:
    """
    On a fresh `git clone`, decompress any `.step.gz` or `.sc.gz` archives whose
    uncompressed `.step` or `.sc` file is not yet on disk.
    """
    decompressed = 0
    for gz_path in root_dir.rglob("*.gz"):
        if not gz_path.name.endswith((".step.gz", ".stp.gz", ".sc.gz")):
            continue
        target_path = gz_path.with_suffix("")  # removes .gz
        if not target_path.exists():
            logger.info("Decompressing %s -> %s...", gz_path.name, target_path.name)
            with gzip.open(gz_path, "rb") as f_in, open(target_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            decompressed += 1
    return decompressed


def build_all_solidcraft_cots(
    db_path: Path = DB_PATH,
    workers: int = 10,
    force: bool = False,
    limit: Optional[int] = None,
    vendor_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Batch-convert all verified local STEP parts in `cots/catalog.db` into normalized
    `.sc` files under `cots/solidcraft/`, updating SQLite metadata in batches.
    """
    cli_path = find_solidcraft_cli()
    decompress_missing_gz_archives(COTS_DIR)

    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_solidcraft_schema(conn)

    sql = """
        SELECT * FROM local_cots_parts
        WHERE status = 'VERIFIED_STEP'
          AND local_path IS NOT NULL
          AND local_path != ''
    """
    params: List[Any] = []
    if vendor_filter:
        sql += " AND LOWER(vendor) LIKE ?"
        params.append(f"%{vendor_filter.lower()}%")
    # Process smallest files first so >95% of parts complete in the first couple of minutes
    sql += " ORDER BY COALESCE(file_size_bytes, 0) ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    total_candidates = len(rows)
    logger.info("Found %d verified local STEP parts for SolidCraft .sc library.", total_candidates)

    tasks: List[Tuple[Dict[str, Any], Path, Path, str, str, str, List[float], Dict[str, Any]]] = []
    db_updates_ready: List[Tuple[str, str, str, str, str, str]] = []
    skipped_up_to_date = 0

    for r in rows:
        lp = str(r["local_path"])
        rel_cots = lp[5:] if lp.lower().startswith("cots/") else lp
        step_abs = COTS_DIR / rel_cots
        if not step_abs.exists():
            continue

        sc_rel = get_solidcraft_rel_path(rel_cots)
        sc_abs = COTS_DIR / sc_rel
        material = infer_solidcraft_material(r.get("vendor", ""), r.get("category", ""), r.get("title", ""), r.get("sku"))
        anchor_name, matrix_12, norm_frames = compute_normalization_transform(r)

        if not force and sc_abs.exists() and sc_abs.stat().st_size > 256:
            skipped_up_to_date += 1
            if (
                r.get("solidcraft_path") != sc_rel
                or r.get("solidcraft_material") != material
                or r.get("solidcraft_anchor") != anchor_name
                or not r.get("solidcraft_frames_json")
            ):
                db_updates_ready.append((
                    sc_rel,
                    material,
                    anchor_name,
                    json.dumps(matrix_12),
                    json.dumps(norm_frames),
                    r["part_id"],
                ))
            continue

        tasks.append((r, step_abs, sc_abs, sc_rel, material, anchor_name, matrix_12, norm_frames))

    if db_updates_ready:
        conn.executemany(
            """
            UPDATE local_cots_parts
            SET solidcraft_path = ?,
                solidcraft_material = ?,
                solidcraft_anchor = ?,
                solidcraft_matrix_json = ?,
                solidcraft_frames_json = ?
            WHERE part_id = ?
            """,
            db_updates_ready,
        )
        conn.commit()

    if not tasks:
        conn.close()
        return {
            "status": "ok",
            "total_candidates": total_candidates,
            "converted": 0,
            "skipped_existing": skipped_up_to_date,
            "failed": 0,
        }

    logger.info(
        "Converting %d STEP parts into cots/solidcraft/*.sc using %d persistent headless workers (%d already up to date)...",
        len(tasks),
        workers,
        skipped_up_to_date,
    )

    work_q: "queue.Queue[Optional[Tuple[int, Any]]]" = queue.Queue()
    for idx, item in enumerate(tasks, 1):
        work_q.put((idx, item))
    for _ in range(workers):
        work_q.put(None)

    results_lock = threading.Lock()
    pending_db: List[Tuple[str, str, str, str, str, str]] = []
    stats = {"converted": 0, "failed": 0, "gzipped_over_95mb": 0}
    failures: List[Dict[str, str]] = []
    t_start = time.time()

    def _worker_loop(worker_idx: int) -> None:
        worker = SolidCraftHeadlessWorker(cli_path)
        parts_since_restart = 0
        try:
            while True:
                job = work_q.get()
                if job is None:
                    work_q.task_done()
                    break
                idx, (r, step_abs, sc_abs, sc_rel, material, anchor_name, matrix_12, norm_frames) = job
                comp_name = (r.get("sku") or step_abs.stem).replace(" ", "_")[:64]
                ok, msg = worker.convert_part(
                    step_abs_path=step_abs,
                    sc_abs_path=sc_abs,
                    comp_name=comp_name,
                    sku=r.get("sku") or "",
                    title=r.get("title") or comp_name,
                    vendor=r.get("vendor") or "unknown",
                    category=r.get("category") or "structure",
                    material=material,
                    anchor_name=anchor_name,
                    matrix_12=matrix_12,
                    norm_frames=norm_frames,
                )
                parts_since_restart += 1
                # Periodically recycle worker every 80 parts to keep memory footprint low
                if parts_since_restart >= 80 or step_abs.stat().st_size > 25 * 1024 * 1024:
                    worker._start()
                    parts_since_restart = 0

                with results_lock:
                    if ok and sc_abs.exists():
                        stats["converted"] += 1
                        if handle_large_sc_gzip_and_gitignore(sc_abs):
                            stats["gzipped_over_95mb"] += 1
                        pending_db.append((
                            sc_rel,
                            material,
                            anchor_name,
                            json.dumps(matrix_12),
                            json.dumps(norm_frames),
                            r["part_id"],
                        ))
                    else:
                        stats["failed"] += 1
                        failures.append({"part_id": r["part_id"], "path": str(step_abs), "error": msg})
                        logger.warning("[%d/%d] FAILED %s: %s", idx, len(tasks), r["part_id"], msg)

                    done_total = stats["converted"] + stats["failed"]
                    if done_total % 50 == 0 or done_total == len(tasks):
                        elapsed = max(0.001, time.time() - t_start)
                        rate = done_total / elapsed
                        eta_s = (len(tasks) - done_total) / max(0.01, rate)
                        logger.info(
                            "Progress: %d/%d (%.1f%%) | ok=%d fail=%d | %.1f parts/s | ETA %.0fs",
                            done_total,
                            len(tasks),
                            100.0 * done_total / len(tasks),
                            stats["converted"],
                            stats["failed"],
                            rate,
                            eta_s,
                        )
                work_q.task_done()
        finally:
            worker.close()

    threads = [threading.Thread(target=_worker_loop, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()

    # Periodically flush pending_db updates on the main thread while workers run
    while any(t.is_alive() for t in threads):
        time.sleep(2.0)
        batch_to_write: List[Tuple[str, str, str, str, str, str]] = []
        with results_lock:
            if len(pending_db) >= 40:
                batch_to_write = list(pending_db)
                pending_db.clear()
        if batch_to_write:
            conn.executemany(
                """
                UPDATE local_cots_parts
                SET solidcraft_path = ?,
                    solidcraft_material = ?,
                    solidcraft_anchor = ?,
                    solidcraft_matrix_json = ?,
                    solidcraft_frames_json = ?
                WHERE part_id = ?
                """,
                batch_to_write,
            )
            conn.commit()

    for t in threads:
        t.join()

    if pending_db:
        conn.executemany(
            """
            UPDATE local_cots_parts
            SET solidcraft_path = ?,
                solidcraft_material = ?,
                solidcraft_anchor = ?,
                solidcraft_matrix_json = ?,
                solidcraft_frames_json = ?
            WHERE part_id = ?
            """,
            pending_db,
        )
        conn.commit()

    conn.close()
    elapsed_total = round(time.time() - t_start, 2)
    return {
        "status": "ok",
        "total_candidates": total_candidates,
        "converted": stats["converted"],
        "skipped_existing": skipped_up_to_date,
        "failed": stats["failed"],
        "gzipped_over_95mb": stats["gzipped_over_95mb"],
        "elapsed_sec": elapsed_total,
        "failures": failures[:20],
    }


def solidcraft_insert_cots(
    part_key_or_sku: str,
    component_name: Optional[str] = None,
    translate: Optional[List[float]] = None,
    anchor: Optional[str] = None,
    joint_type: Optional[str] = None,
    target_occurrence: Optional[str] = None,
    target_point: Optional[List[float]] = None,
    target_axis: Optional[List[float]] = None,
    flip: bool = True,
    limits: Optional[List[List[float]]] = None,
    connect_port: int = 27878,
    design_path: Optional[str] = None,
    register_joint_origins: bool = True,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """
    Look up a COTS part by SKU/ID/query in `cots/catalog.db`, ensure its normalized
    `.sc` library file exists on disk, insert it into the active SolidCraft design
    (`component.insert_design` with `reuse: True`), register named `joint.origin`s
    for its functional frames (`<occurrence>:bore_center`, `<occurrence>:flange_seat`,
    `<occurrence>:output_shaft`, `<occurrence>:mount_hole_1..N`), and optionally
    create a joint mating it to `target_occurrence` in one atomic batch.
    """
    from tools.cad_scraper import cots_info, cots_search

    info = cots_info(part_key_or_sku, db_path=db_path)
    local_part = info.get("local_part")
    if not local_part:
        search_res = cots_search(part_key_or_sku, source="local", limit=1, db_path=db_path)
        local_list = search_res.get("local_parts") or []
        if local_list:
            local_part = local_list[0]
    if not local_part:
        raise ValueError(f"No local COTS part found matching '{part_key_or_sku}'")

    cli_path = find_solidcraft_cli()
    lp = str(local_part.get("cots_rel_path") or local_part.get("local_path") or "")
    rel_cots = lp[5:] if lp.lower().startswith("cots/") else lp
    step_abs = COTS_DIR / rel_cots
    sc_rel = local_part.get("solidcraft_path") or get_solidcraft_rel_path(rel_cots)
    sc_abs = COTS_DIR / sc_rel

    # If .sc is missing, decompress .sc.gz if present or build on the fly
    gz_sc = Path(str(sc_abs) + ".gz")
    if not sc_abs.exists() and gz_sc.exists():
        with gzip.open(gz_sc, "rb") as f_in, open(sc_abs, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    material = local_part.get("solidcraft_material") or infer_solidcraft_material(
        local_part.get("vendor", ""),
        local_part.get("category", ""),
        local_part.get("title", ""),
        local_part.get("sku"),
    )
    anchor_name, matrix_12, norm_frames = compute_normalization_transform(local_part)

    if not sc_abs.exists():
        if not step_abs.exists():
            gz_step = Path(str(step_abs) + ".gz")
            if gz_step.exists():
                with gzip.open(gz_step, "rb") as f_in, open(step_abs, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
        worker = SolidCraftHeadlessWorker(cli_path)
        try:
            c_name = (local_part.get("sku") or step_abs.stem).replace(" ", "_")[:64]
            ok, msg = worker.convert_part(
                step_abs_path=step_abs,
                sc_abs_path=sc_abs,
                comp_name=c_name,
                sku=local_part.get("sku") or "",
                title=local_part.get("title") or c_name,
                vendor=local_part.get("vendor") or "unknown",
                category=local_part.get("category") or "structure",
                material=material,
                anchor_name=anchor_name,
                matrix_12=matrix_12,
                norm_frames=norm_frames,
            )
            if not ok:
                raise RuntimeError(f"Failed to convert {step_abs} to .sc: {msg}")
        finally:
            worker.close()

    # Connect to live SolidCraft GUI (or headless design_path if provided)
    mcp_args = [cli_path, "--max-file-size", "2GB", "mcp", "--allow-any-path"]
    if design_path:
        mcp_args.extend(["--headless", "--in", str(Path(design_path).resolve())])
    else:
        mcp_args.extend(["--connect", f"127.0.0.1:{int(connect_port)}"])

    proc = subprocess.Popen(
        mcp_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def _call_rpc(req_id: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            err_out = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"SolidCraft MCP connection closed: {err_out}")
        return json.loads(line)

    try:
        _call_rpc(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "solidcraft-cots-insert", "version": "1.0.0"},
        })

        ins_params: Dict[str, Any] = {
            "path": str(sc_abs.resolve()),
            "reuse": True,
        }
        if component_name:
            ins_params["name"] = component_name
        if translate and len(translate) == 3:
            ins_params["translate"] = [float(x) for x in translate]

        r_ins = _call_rpc(2, "tools/call", {
            "name": "execute",
            "arguments": {
                "command": "component.insert_design",
                "params": ins_params,
                "verbose": False,
            },
        })
        if "error" in r_ins:
            raise RuntimeError(f"component.insert_design error: {r_ins['error']}")
        ins_res = json.loads(r_ins["result"]["content"][0]["text"])
        inserted_comp_name = ins_res.get("name") or component_name or sc_abs.stem

        # Query component.list or inspect_design to resolve the exact new occurrence label (<name>:<n>)
        r_jlist = _call_rpc(3, "tools/call", {
            "name": "execute",
            "arguments": {"command": "joint.list", "params": {}},
        })
        jlist_res = json.loads(r_jlist["result"]["content"][0]["text"])
        occ_id = ins_res.get("occurrence")
        occ_ref: Any = occ_id if occ_id is not None else inserted_comp_name

        tx = [float(x) for x in translate] if (translate and len(translate) == 3) else [0.0, 0.0, 0.0]

        follow_up_cmds: List[Dict[str, Any]] = []
        created_origin_names: Dict[str, str] = {}

        if register_joint_origins:
            for fname in ("bore_center", "flange_seat", "output_shaft", "bore_min", "bore_max", "sense_origin", "bbox_center"):
                f_obj = norm_frames.get(fname)
                if isinstance(f_obj, dict) and isinstance(f_obj.get("origin_mm"), list) and len(f_obj["origin_mm"]) == 3:
                    pt = [
                        round(float(f_obj["origin_mm"][0]) + tx[0], 4),
                        round(float(f_obj["origin_mm"][1]) + tx[1], 4),
                        round(float(f_obj["origin_mm"][2]) + tx[2], 4),
                    ]
                    z_vec = f_obj.get("z_vec") or [0.0, 0.0, 1.0]
                    oname = f"{inserted_comp_name}_{occ_id}_{fname}"
                    created_origin_names[fname] = oname
                    follow_up_cmds.append({
                        "command": "joint.origin",
                        "params": {
                            "name": oname,
                            "occurrence": occ_ref,
                            "point": pt,
                            "z": z_vec,
                        },
                    })

            ear = norm_frames.get("ear_mount")
            if isinstance(ear, dict) and isinstance(ear.get("mount_holes_mm"), list):
                for h_idx, h in enumerate(ear["mount_holes_mm"][:8], 1):
                    h_pt = h.get("center_mm") if isinstance(h, dict) else (h if isinstance(h, list) else None)
                    if isinstance(h_pt, list) and len(h_pt) == 3:
                        pt = [
                            round(float(h_pt[0]) + tx[0], 4),
                            round(float(h_pt[1]) + tx[1], 4),
                            round(float(h_pt[2]) + tx[2], 4),
                        ]
                        oname = f"{inserted_comp_name}_{occ_id}_mount_hole_{h_idx}"
                        created_origin_names[f"mount_hole_{h_idx}"] = oname
                        follow_up_cmds.append({
                            "command": "joint.origin",
                            "params": {
                                "name": oname,
                                "occurrence": occ_ref,
                                "point": pt,
                                "z": [0.0, 0.0, 1.0],
                            },
                        })

        joint_created_name: Optional[str] = None
        if joint_type and target_occurrence:
            chosen_anchor = anchor or anchor_name
            anchor_f = norm_frames.get(chosen_anchor) or norm_frames.get(anchor_name) or {"origin_mm": [0.0, 0.0, 0.0], "z_vec": [0.0, 0.0, 1.0]}
            b_pt = [
                round(float(anchor_f["origin_mm"][0]) + tx[0], 4),
                round(float(anchor_f["origin_mm"][1]) + tx[1], 4),
                round(float(anchor_f["origin_mm"][2]) + tx[2], 4),
            ]
            b_z = anchor_f.get("z_vec") or [0.0, 0.0, 1.0]
            a_pt = [float(x) for x in target_point] if (target_point and len(target_point) == 3) else [0.0, 0.0, 0.0]
            a_z = [float(x) for x in target_axis] if (target_axis and len(target_axis) == 3) else [0.0, 0.0, 1.0]

            joint_created_name = f"Mate_{inserted_comp_name}_{occ_id}"
            j_params: Dict[str, Any] = {
                "type": joint_type,
                "name": joint_created_name,
                "flip": bool(flip),
                "a": {"occurrence": target_occurrence, "point": a_pt, "z": a_z},
                "b": {"occurrence": occ_ref, "point": b_pt, "z": b_z},
            }
            if limits:
                j_params["limits"] = limits
            follow_up_cmds.append({"command": "joint.create", "params": j_params})

        if design_path:
            follow_up_cmds.append({"command": "file.save", "params": {"path": str(Path(design_path).resolve()), "overwrite": True}})

        batch_reply = None
        if follow_up_cmds:
            r_batch = _call_rpc(4, "tools/call", {
                "name": "batch",
                "arguments": {"verbose": False, "commands": follow_up_cmds},
            })
            if "result" in r_batch and r_batch["result"].get("content"):
                batch_reply = json.loads(r_batch["result"]["content"][0]["text"])

        return {
            "status": "ok",
            "part_id": local_part.get("part_id"),
            "sku": local_part.get("sku"),
            "title": local_part.get("title"),
            "vendor": local_part.get("vendor"),
            "category": local_part.get("category"),
            "material": material,
            "solidcraft_path": f"cots/{sc_rel}",
            "component_name": inserted_comp_name,
            "occurrence_id": occ_id,
            "recommended_anchor": anchor_name,
            "registered_joint_origins": created_origin_names,
            "joint_created": joint_created_name,
            "normalized_frames": norm_frames,
            "batch_status": batch_reply.get("ok") if isinstance(batch_reply, dict) else True,
        }
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


def generate_human_catalog(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """
    Generate human-searchable catalog indexes inside `cots/solidcraft/`:
    1. `cots/solidcraft/CATALOG.md` — Markdown directory of all 2,587 normalized `.sc` parts.
    2. `cots/solidcraft/index.html` — Zero-dependency interactive browser with instant search,
       vendor/category/bore filters, one-click `.sc` path copy, and live SolidCraft insertion.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT part_id, vendor, category, sku, title, solidcraft_path,
               solidcraft_material, solidcraft_anchor, min_bore_radius_mm,
               bbox_min_mm, bbox_max_mm, functional_frames_json
        FROM local_cots_parts
        WHERE status = 'VERIFIED_STEP'
          AND solidcraft_path IS NOT NULL
          AND solidcraft_path != ''
        ORDER BY vendor ASC, category ASC, sku ASC, title ASC
        """
    ).fetchall()
    conn.close()

    SOLIDCRAFT_LIB_DIR.mkdir(parents=True, exist_ok=True)
    md_path = SOLIDCRAFT_LIB_DIR / "CATALOG.md"
    html_path = SOLIDCRAFT_LIB_DIR / "index.html"

    # Build compact JSON dataset for interactive HTML & Markdown table rows
    items: List[Dict[str, Any]] = []
    by_vendor_cat: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for r in rows:
        vendor = str(r["vendor"] or "unknown")
        cat = str(r["category"] or "misc")
        sku = str(r["sku"] or "")
        title = str(r["title"] or sku or r["part_id"])
        sc_rel = str(r["solidcraft_path"])
        full_rel = f"cots/{sc_rel}" if not sc_rel.startswith("cots/") else sc_rel
        abs_sc = (WORKSPACE_ROOT / full_rel).resolve().as_posix()
        bore_r = r["min_bore_radius_mm"]
        bore_d = round(float(bore_r) * 2.0, 2) if bore_r is not None and float(bore_r) > 0 else None

        dims_str = ""
        try:
            bmin = json.loads(r["bbox_min_mm"]) if r["bbox_min_mm"] else None
            bmax = json.loads(r["bbox_max_mm"]) if r["bbox_max_mm"] else None
            if isinstance(bmin, list) and isinstance(bmax, list) and len(bmin) == 3 and len(bmax) == 3:
                dx = round(abs(float(bmax[0]) - float(bmin[0])), 1)
                dy = round(abs(float(bmax[1]) - float(bmin[1])), 1)
                dz = round(abs(float(bmax[2]) - float(bmin[2])), 1)
                dims_str = f"{dx}x{dy}x{dz}"
        except Exception:
            pass

        entry = {
            "id": r["part_id"],
            "v": vendor,
            "c": cat,
            "sku": sku,
            "t": title,
            "mat": r["solidcraft_material"] or "Aluminum",
            "anc": r["solidcraft_anchor"] or "bore_center",
            "bore": bore_d,
            "dims": dims_str,
            "holes": "mount_holes_mm" in str(r["functional_frames_json"] or ""),
            "rel": full_rel,
            "abs": abs_sc,
        }
        items.append(entry)
        by_vendor_cat.setdefault(vendor, {}).setdefault(cat, []).append(entry)

    # 1. Write CATALOG.md
    md_lines = [
        "# SolidCraft Robotics COTS Library Catalog",
        "",
        f"Total pre-built, coordinate-normalized `.sc` components: **{len(items):,}**",
        "",
        "## Quick Usage (Human & CLI)",
        "",
        "- **Interactive Visual Browser:**Open [`cots/solidcraft/index.html`](index.html) in any browser, or run:",
        "  ```powershell",
        "  .venv\\Scripts\\python.exe tools/solidcraft_cots.py browser",
        "  ```",
        "- **Terminal Search (Table View):**",
        "  ```powershell",
        '  .venv\\Scripts\\python.exe tools/solidcraft_cots.py search "flanged bearing" --bore-mm 8',
        "  ```",
        "- **Insert & Mate from Terminal into Live SolidCraft:**",
        "  ```powershell",
        '  .venv\\Scripts\\python.exe tools/solidcraft_cots.py insert 1611-0514-0008 --joint-type rigid --target-occurrence Plate:1 --target-point 24 0 2',
        "  ```",
        "",
        "## Vendors Summary",
        "",
        "| Vendor | Categories | Parts |",
        "| :--- | :--- | ---: |",
    ]
    for v_name, cats in sorted(by_vendor_cat.items()):
        total_v = sum(len(lst) for lst in cats.values())
        cat_list = ", ".join(sorted(cats.keys()))
        md_lines.append(f"| **{v_name}** | {cat_list} | {total_v:,} |")
    md_lines.append("")

    for v_name, cats in sorted(by_vendor_cat.items()):
        md_lines.append(f"## Vendor: `{v_name}`")
        md_lines.append("")
        for c_name, part_list in sorted(cats.items()):
            md_lines.append(f"### `{v_name}` / `{c_name}` ({len(part_list)} parts)")
            md_lines.append("")
            md_lines.append("| SKU | Title | Bore (mm) | Envelope (mm) | Material | Anchor | `.sc` Path |")
            md_lines.append("| :--- | :--- | ---: | :--- | :--- | :--- | :--- |")
            for p in part_list:
                b_txt = f"{p['bore']:g}" if p["bore"] is not None else "—"
                sku_txt = f"`{p['sku']}`" if p["sku"] else "—"
                t_clean = p["t"].replace("|", "\\|")
                md_lines.append(
                    f"| {sku_txt} | {t_clean} | {b_txt} | {p['dims'] or '—'} | {p['mat']} | `{p['anc']}` | `{p['rel']}` |"
                )
            md_lines.append("")

    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    # 2. Write interactive index.html
    data_json = json.dumps(items, separators=(",", ":"))
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SolidCraft Robotics COTS Library ({len(items):,} Parts)</title>
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --border: #334155; --text: #f8fafc;
    --muted: #94a3b8; --accent: #38bdf8; --accent-hover: #0284c7; --ok: #22c55e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--text);
  }}
  header {{
    position: sticky; top: 0; z-index: 10; background: rgba(15, 23, 42, 0.95);
    backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); padding: 16px 24px;
  }}
  .title-row {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }}
  h1 {{ margin: 0; font-size: 1.25rem; font-weight: 700; color: var(--accent); }}
  .stats {{ font-size: 0.875rem; color: var(--muted); }}
  .controls {{ display: grid; grid-template-columns: 2fr 1fr 1fr 1fr auto; gap: 10px; }}
  input, select, button {{
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 12px; font-size: 0.9rem;
  }}
  input:focus, select:focus {{ outline: 2px solid var(--accent); border-color: transparent; }}
  button {{ cursor: pointer; font-weight: 600; transition: background 0.15s; }}
  .btn-primary {{ background: var(--accent); color: #0f172a; border: none; }}
  .btn-primary:hover {{ background: var(--accent-hover); color: #fff; }}
  .btn-ghost:hover {{ background: #334155; }}
  main {{ padding: 16px 24px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
  th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; position: sticky; top: 108px; background: var(--bg); }}
  tr:hover td {{ background: rgba(30, 41, 59, 0.6); }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem;
    background: #334155; color: #e2e8f0; font-family: ui-monospace, monospace;
  }}
  .sku {{ font-family: ui-monospace, monospace; color: var(--accent); font-weight: 600; }}
  .actions {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .toast {{
    position: fixed; bottom: 20px; right: 20px; background: var(--ok); color: #052e16;
    padding: 10px 16px; border-radius: 8px; font-weight: 600; display: none; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }}
</style>
</head>
<body>
<header>
  <div class="title-row">
    <h1>SolidCraft Robotics COTS Library</h1>
    <div class="stats" id="statsLabel">Showing 0 of {len(items):,} normalized .sc components</div>
  </div>
  <div class="controls">
    <input type="search" id="qInput" placeholder="Search by keyword, SKU (e.g. 1611-0514-0008, REV-21-3005), gear, bearing, motor..." autofocus>
    <select id="vendorSelect"><option value="">All Vendors</option></select>
    <select id="catSelect"><option value="">All Categories</option></select>
    <input type="number" id="boreInput" placeholder="Bore Ø mm (e.g. 8)" step="0.5" min="0">
    <button class="btn-ghost" id="resetBtn">Reset</button>
  </div>
</header>
<main>
  <table>
    <thead>
      <tr>
        <th>Vendor / Category</th>
        <th>SKU</th>
        <th>Title</th>
        <th>Bore Ø</th>
        <th>Envelope (mm)</th>
        <th>Material &amp; Anchor</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="rowsBody"></tbody>
  </table>
</main>
<div class="toast" id="toast"></div>
<script>
const PARTS = {data_json};
const qInput = document.getElementById('qInput');
const vendorSelect = document.getElementById('vendorSelect');
const catSelect = document.getElementById('catSelect');
const boreInput = document.getElementById('boreInput');
const rowsBody = document.getElementById('rowsBody');
const statsLabel = document.getElementById('statsLabel');
const toast = document.getElementById('toast');

const vendors = [...new Set(PARTS.map(p => p.v))].sort();
vendors.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; vendorSelect.appendChild(o); }});
const cats = [...new Set(PARTS.map(p => p.c))].sort();
cats.forEach(c => {{ const o = document.createElement('option'); o.value = c; o.textContent = c; catSelect.appendChild(o); }});

function showToast(msg) {{
  toast.textContent = msg;
  toast.style.display = 'block';
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => {{ toast.style.display = 'none'; }}, 2400);
}}

async function copyText(txt, label) {{
  await navigator.clipboard.writeText(txt);
  showToast('Copied ' + label + '!');
}}

async function insertIntoSolidCraft(key) {{
  try {{
    const res = await fetch('/api/insert', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ key }})
    }});
    if (!res.ok) throw new Error('Server not running');
    const data = await res.json();
    if (data.status === 'ok') {{
      showToast('Inserted ' + data.component_name + ' (occurrence ' + data.occurrence_id + ') into SolidCraft!');
    }} else {{
      alert('Insert error: ' + (data.error || JSON.stringify(data)));
    }}
  }} catch (e) {{
    const cmd = '.venv\\\\Scripts\\\\python.exe tools/solidcraft_cots.py insert "' + key + '"';
    await navigator.clipboard.writeText(cmd);
    showToast('Copied terminal insert command! (Run `python tools/solidcraft_cots.py browser` for direct 1-click GUI insert)');
  }}
}}

function render() {{
  const tokens = qInput.value.toLowerCase().trim().split(/\\s+/).filter(Boolean);
  const vFilter = vendorSelect.value;
  const cFilter = catSelect.value;
  const bVal = parseFloat(boreInput.value);

  const filtered = PARTS.filter(p => {{
    if (vFilter && p.v !== vFilter) return false;
    if (cFilter && p.c !== cFilter) return false;
    if (!isNaN(bVal) && (p.bore === null || Math.abs(p.bore - bVal) > 0.35)) return false;
    if (tokens.length) {{
      const hay = (p.v + ' ' + p.c + ' ' + p.sku + ' ' + p.t + ' ' + p.mat + ' ' + p.rel).toLowerCase();
      for (const tok of tokens) {{
        if (!hay.includes(tok)) return false;
      }}
    }}
    return true;
  }});

  const shown = filtered.slice(0, 250);
  statsLabel.textContent = `Showing ${{shown.length}} of ${{filtered.length}} matching parts (${{PARTS.length.toLocaleString()}} total)`;

  rowsBody.innerHTML = shown.map(p => {{
    const key = p.sku || p.id;
    return `<tr>
      <td><span class="badge">${{p.v}}</span> <span class="badge">${{p.c}}</span></td>
      <td class="sku">${{p.sku || '—'}}</td>
      <td>${{p.t}}</td>
      <td>${{p.bore !== null ? p.bore + ' mm' : '—'}}</td>
      <td><span class="badge">${{p.dims || '—'}}</span></td>
      <td><span class="badge">${{p.mat}}</span> <span class="badge">@${{p.anc}}</span></td>
      <td class="actions">
        <button class="btn-primary" onclick="insertIntoSolidCraft('${{key.replace(/'/g, "\\\\'")}}')">+ Insert in SolidCraft</button>
        <button class="btn-ghost" onclick="copyText('${{p.abs.replace(/'/g, "\\\\'")}}', '.sc path')">Copy .sc Path</button>
        <button class="btn-ghost" onclick="copyText('${{key.replace(/'/g, "\\\\'")}}', 'SKU')">Copy SKU</button>
      </td>
    </tr>`;
  }}).join('');
}}

qInput.addEventListener('input', render);
vendorSelect.addEventListener('change', render);
catSelect.addEventListener('change', render);
boreInput.addEventListener('input', render);
document.getElementById('resetBtn').addEventListener('click', () => {{
  qInput.value = ''; vendorSelect.value = ''; catSelect.value = ''; boreInput.value = ''; render();
}});
render();
</script>
</body>
</html>
"""
    html_path.write_text(html_content, encoding="utf-8")
    return {
        "status": "ok",
        "total_parts": len(items),
        "markdown_catalog": str(md_path),
        "html_browser": str(html_path),
    }


def serve_catalog_browser(http_port: int = 8765, solidcraft_port: int = 27878, open_browser: bool = True) -> None:
    """
    Serve `cots/solidcraft/index.html` locally with a `/api/insert` endpoint so clicking
    `+ Insert in SolidCraft` in the browser immediately inserts the part into the open SolidCraft window.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import webbrowser

    cat_info = generate_human_catalog(DB_PATH)
    html_bytes = Path(cat_info["html_browser"]).read_bytes()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_bytes)

        def do_POST(self) -> None:
            if self.path == "/api/insert":
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                key = body.get("key", "")
                try:
                    res = solidcraft_insert_cots(key, connect_port=solidcraft_port)
                    payload = json.dumps(res).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("127.0.0.1", http_port), _Handler)
    url = f"http://127.0.0.1:{http_port}"
    print(f"SolidCraft COTS Interactive Browser listening at {url} (connected to SolidCraft port {solidcraft_port})")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build, Search & Manage the SolidCraft .sc COTS Component Library")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search", help="Search the SolidCraft COTS library by keyword, SKU, vendor, bore, or size")
    p_search.add_argument("query", nargs="?", default="", help="Keyword or SKU query (e.g. 'flanged bearing', '5203', 'miter gear')")
    p_search.add_argument("--vendor", type=str, default=None, help="Vendor filter (gobilda, rev, swyft, thriftybot, mcmaster, iso)")
    p_search.add_argument("--category", type=str, default=None, help="Category filter (bearings, gears, motors, structure, fasteners, wheels)")
    p_search.add_argument("--bore-mm", type=float, default=None, help="Target bore diameter in mm (±0.25 mm)")
    p_search.add_argument("--max-extent-mm", type=float, default=None, help="Maximum bounding-box dimension in mm")
    p_search.add_argument("--has-mounting-holes", action="store_true", help="Only return parts with extracted mounting hole patterns")
    p_search.add_argument("--limit", type=int, default=25, help="Max results to return (default 25)")
    p_search.add_argument("--json", action="store_true", help="Output raw JSON instead of a human-readable table")

    p_info = sub.add_parser("info", help="Inspect full SolidCraft metadata, normalized frames, and .sc path for a COTS part")
    p_info.add_argument("key", help="SKU or part_id (e.g. 1611-0514-0008)")

    p_ins = sub.add_parser("insert", help="Insert (and optionally mate) a COTS part into the active SolidCraft design")
    p_ins.add_argument("part_key_or_sku", help="SKU, part_id, or search query")
    p_ins.add_argument("--name", type=str, default=None, help="Optional custom component name in assembly")
    p_ins.add_argument("--translate", type=float, nargs=3, metavar=("X", "Y", "Z"), default=None, help="Initial [x, y, z] translation in mm")
    p_ins.add_argument("--anchor", type=str, default=None, help="Anchor frame to mate (bore_center, flange_seat, output_shaft, sense_origin, bbox_center)")
    p_ins.add_argument("--joint-type", type=str, default=None, choices=["rigid", "revolute", "slider", "cylindrical", "planar", "ball"], help="Optional joint type to create")
    p_ins.add_argument("--target-occurrence", type=str, default=None, help="Target occurrence in assembly to mate to (e.g. 'Plate:1')")
    p_ins.add_argument("--target-point", type=float, nargs=3, metavar=("X", "Y", "Z"), default=None, help="[x, y, z] mate point on target occurrence in mm")
    p_ins.add_argument("--target-axis", type=float, nargs=3, metavar=("ZX", "ZY", "ZZ"), default=None, help="Primary Z axis vector on target occurrence")
    p_ins.add_argument("--no-flip", action="store_true", help="Set flip=False on the created joint (default is flip=True for SolidCraft)")
    p_ins.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_ins.add_argument("--design", type=str, default=None, help="Optional headless .sc file path instead of live GUI")

    p_cat = sub.add_parser("catalog", help="Generate cots/solidcraft/CATALOG.md and interactive cots/solidcraft/index.html")

    p_srv = sub.add_parser("browser", help="Launch the interactive web catalog browser with 1-click 'Insert in SolidCraft'")
    p_srv.add_argument("--http-port", type=int, default=8765, help="Local HTTP port for the catalog browser (default 8765)")
    p_srv.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_srv.add_argument("--no-open", action="store_true", help="Do not auto-open browser tab")

    p_dock = sub.add_parser("dock", help="Launch the compact Floating SolidCraft COTS Dock next to SolidCraft")
    p_dock.add_argument("--http-port", type=int, default=8765, help="Local HTTP port for the dock (default 8765)")
    p_dock.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_dock.add_argument("--no-open", action="store_true", help="Do not auto-open floating dock window")

    p_comp = sub.add_parser("compile", help="Compile a project STEP + *_constraints.json into a jointed .sc assembly + BOM + motion GIF")
    p_comp.add_argument("step_file", type=Path, help="Path to .step or .step.gz file")
    p_comp.add_argument("--constraints", type=Path, default=None, help="Optional *_constraints.json path")
    p_comp.add_argument("--out", type=Path, default=None, help="Output .sc path")
    p_comp.add_argument("--no-gif", action="store_true", help="Skip animated .gif export")
    p_comp.add_argument("--live", action="store_true", help="Open the compiled .sc assembly in live SolidCraft (port 27878)")
    p_comp.add_argument("--port", type=int, default=27878)

    p_th = sub.add_parser("thumbs", help="Batch-render 3D isometric PNG thumbnails for all COTS .sc parts")
    p_th.add_argument("--workers", type=int, default=10)
    p_th.add_argument("--force", action="store_true")
    p_th.add_argument("--limit", type=int, default=None)

    p_ren = sub.add_parser("rename-skus", help="Enrich SKU-only .sc filenames to <SKU>_<Clean_Title>.sc")

    p_build = sub.add_parser("build", help="Batch-convert local STEP COTS parts into normalized cots/solidcraft/*.sc files")
    p_build.add_argument("--workers", type=int, default=10, help="Number of persistent headless SolidCraft workers (default 10)")
    p_build.add_argument("--force", action="store_true", help="Rebuild existing .sc files")
    p_build.add_argument("--limit", type=int, default=None, help="Optional limit on number of parts to convert")
    p_build.add_argument("--vendor", type=str, default=None, help="Optional vendor filter (e.g. gobilda, rev, swyft)")

    p_decomp = sub.add_parser("decompress", help="Decompress any .step.gz and .sc.gz large archives in cots/")

    p_up = sub.add_parser("upgrade-format", help="Batch-upgrade existing .sc files to SolidCraft Format 52 (solidcore)")
    p_up.add_argument("--scope", choices=["all", "cots", "projects"], default="all")
    p_up.add_argument("--workers", type=int, default=8)
    p_up.add_argument("--force", action="store_true")
    p_up.add_argument("--limit", type=int, default=None)
    p_up.add_argument("--exclude-project", action="append", default=[], help="Project folder name to skip (repeatable)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.cmd == "search":
        from tools.cad_scraper import cots_search

        res = cots_search(
            args.query,
            vendor=args.vendor,
            category=args.category,
            source="local",
            bore_mm=args.bore_mm,
            max_extent_mm=args.max_extent_mm,
            has_mounting_holes=True if args.has_mounting_holes else None,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(res, indent=2))
            return
        parts = res.get("local_parts") or []
        print(f"Found {len(parts)} matching SolidCraft COTS parts:")
        print(f"{'SKU / ID':<22} {'Vendor/Cat':<20} {'Bore':<8} {'Material':<15} {'Title':<42} {'SolidCraft .sc Path'}")
        print("-" * 145)
        for p in parts:
            sku_id = (p.get("sku") or p.get("part_id") or "")[:21]
            v_c = f"{p.get('vendor','')}/{p.get('category','')}"[:19]
            br = p.get("min_bore_radius_mm")
            bore_txt = f"{round(float(br)*2, 2):g}mm" if br is not None and float(br) > 0 else "—"
            mat = (p.get("solidcraft_material") or "Aluminum")[:14]
            title = (p.get("title") or "")[:41]
            sc_p = p.get("solidcraft_path") or ""
            print(f"{sku_id:<22} {v_c:<20} {bore_txt:<8} {mat:<15} {title:<42} {sc_p}")
    elif args.cmd == "info":
        from tools.cad_scraper import cots_info

        print(json.dumps(cots_info(args.key), indent=2))
    elif args.cmd == "insert":
        res = solidcraft_insert_cots(
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
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "catalog":
        from tools.solidcraft_compiler import generate_visual_catalog_and_dock

        res = generate_visual_catalog_and_dock(DB_PATH)
        print(json.dumps(res, indent=2))
    elif args.cmd == "browser":
        from tools.solidcraft_compiler import serve_cots_dock_and_browser

        serve_cots_dock_and_browser(http_port=args.http_port, solidcraft_port=args.port, dock_mode=False, open_window=not args.no_open)
    elif args.cmd == "dock":
        from tools.solidcraft_compiler import serve_cots_dock_and_browser

        serve_cots_dock_and_browser(http_port=args.http_port, solidcraft_port=args.port, dock_mode=True, open_window=not args.no_open)
    elif args.cmd == "compile":
        from tools.solidcraft_compiler import compile_project_to_solidcraft

        print(
            json.dumps(
                compile_project_to_solidcraft(
                    args.step_file,
                    constraints_path=args.constraints,
                    out_sc_path=args.out,
                    export_gif=not args.no_gif,
                    live_push=args.live,
                    connect_port=args.port,
                ),
                indent=2,
            )
        )
    elif args.cmd == "thumbs":
        from tools.solidcraft_compiler import render_all_cots_thumbnails

        print(json.dumps(render_all_cots_thumbnails(DB_PATH, workers=args.workers, force=args.force, limit=args.limit), indent=2))
    elif args.cmd == "rename-skus":
        from tools.solidcraft_compiler import rename_sku_only_sc_files

        print(json.dumps(rename_sku_only_sc_files(DB_PATH), indent=2))
    elif args.cmd == "build":
        res = build_all_solidcraft_cots(
            workers=args.workers,
            force=args.force,
            limit=args.limit,
            vendor_filter=args.vendor,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "decompress":
        cnt = decompress_missing_gz_archives(COTS_DIR)
        print(json.dumps({"decompressed_archives": cnt}, indent=2))
    elif args.cmd == "upgrade-format":
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
