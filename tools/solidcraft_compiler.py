"""
SolidCraft Project Assembly Compiler, Thumbnail Renderer & Floating COTS Dock
=============================================================================
1. `rename_sku_only_sc_files()`:
   Enriches all SKU-only `.sc` filenames in `cots/solidcraft/` to `<SKU>_<Clean_Title>.sc`
   and updates `cots/catalog.db` so Windows Explorer and SolidCraft's native File Picker
   show human-readable descriptions alongside SKUs.
2. `render_all_cots_thumbnails()`:
   Renders 3D isometric PNG thumbnails (`cots/solidcraft/thumbs/<part_id>.png`) for all
   2,588 normalized `.sc` COTS parts using a persistent `solidcraft-cli mcp --headless` pool.
3. `compile_project_to_solidcraft()`:
   Compiles any project assembly (`.step` / `.step.gz` + `*_constraints.json`) into a
   self-contained, jointed, motion-coupled SolidCraft `.sc` assembly using Option 1
   (Hybrid In-Place COTS Tagging):
   - Unlocks the imported STEP assembly (`component.unlock`) while keeping exact 3D poses.
   - Matches COTS occurrences against `onshape_cloud_instances` and `cots/catalog.db`,
     assigning physical materials (`Steel`, `Aluminum`, `Brass`, `ABS Plastic`, `Nylon`,
     `Polycarbonate`) and `component.metadata.set` (`part_number`, `description`,
     `bom_type: "purchased"`) so repeated fasteners/bearings roll up in `assembly.bom`.
   - Translates `mate_groups` -> `joint.rigid_group`, `mates` -> `occurrence.ground` &
     `joint.create` (`flip: True`), and `gear_relations` -> `joint.motion_link`.
   - Exports a full verification suite: `<name>.sc`, `<name>_sc_bom.csv`, `<name>_sc_bom.md`,
     `<name>_sc_verification.json` (with `assembly.mass_properties` & `joint.sweep`),
     and `<name>_sc_motion.gif` (`motion.export_animation`).
   - Optionally pushes/opens the compiled `.sc` assembly in the live SolidCraft GUI (`--live`).
4. `serve_cots_dock_and_browser()`:
   Serves the visual COTS catalog (`cots/solidcraft/index.html`) with 3D thumbnails and
   a compact Floating SolidCraft COTS Dock (`--dock`) that queries live occurrences from
   port 27878 and performs 1-click Insert & Mate.
"""

from __future__ import annotations

from fractions import Fraction
import gzip
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import webbrowser

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.solidcraft_cots import (
    COTS_DIR,
    DB_PATH,
    SOLIDCRAFT_LIB_DIR,
    find_solidcraft_cli,
    infer_solidcraft_material,
    solidcraft_insert_cots,
)

logger = logging.getLogger("solidcraft_compiler")
THUMBS_DIR = SOLIDCRAFT_LIB_DIR / "thumbs"


def clean_title_slug(sku: Optional[str], title: Optional[str], max_stem_chars: int = 92) -> str:
    """Build a filesystem-safe `<SKU>_<Clean_Title>` stem without duplicating the leading SKU."""
    sku_clean = (sku or "").strip()
    title_clean = (title or "").strip()
    if not title_clean or title_clean.lower() == sku_clean.lower():
        return sku_clean
    if sku_clean and title_clean.lower().startswith(sku_clean.lower()):
        title_clean = title_clean[len(sku_clean) :].lstrip(" -_:|/")
    slug = re.sub(r"[^A-Za-z0-9.+-]+", "_", title_clean).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return sku_clean
    combined = f"{sku_clean}_{slug}" if sku_clean else slug
    if len(combined) > max_stem_chars:
        combined = combined[:max_stem_chars].rstrip("_-.")
    return combined


def rename_sku_only_sc_files(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """
    Enrich SKU-only `.sc` files on disk and in `cots/catalog.db` with their descriptive
    human titles (`<SKU>_<Clean_Title>.sc`) so Windows Explorer and SolidCraft's native
    File Picker show human-readable part names alongside SKUs.
    """
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    sku_to_title: Dict[str, str] = {}
    for r in conn.execute("SELECT sku, name FROM cots_part_variants WHERE sku IS NOT NULL AND name IS NOT NULL"):
        s, n = str(r["sku"]).strip(), str(r["name"]).strip()
        if s and n and n.lower() != s.lower() and len(n) > len(sku_to_title.get(s, "")):
            sku_to_title[s] = n
    for r in conn.execute("SELECT sku, title FROM local_cots_parts WHERE sku IS NOT NULL AND title IS NOT NULL"):
        s, t = str(r["sku"]).strip(), str(r["title"]).strip()
        if s and t and t.lower() != s.lower() and len(t) > len(sku_to_title.get(s, "")):
            sku_to_title[s] = t

    rows = conn.execute(
        "SELECT part_id, sku, title, local_path, solidcraft_path FROM local_cots_parts WHERE solidcraft_path IS NOT NULL"
    ).fetchall()

    renamed_files = 0
    updated_rows = 0
    updates: List[Tuple[str, str, str]] = []

    for r in rows:
        pid = r["part_id"]
        sku = str(r["sku"] or "").strip()
        cur_title = str(r["title"] or "").strip()
        best_title = sku_to_title.get(sku, cur_title) if sku else cur_title
        cur_sc_rel = str(r["solidcraft_path"])
        if cur_sc_rel.lower().startswith("cots/"):
            cur_sc_rel = cur_sc_rel[5:]

        cur_sc_abs = COTS_DIR / cur_sc_rel
        p = Path(cur_sc_rel)
        new_sc_rel = cur_sc_rel
        if sku and best_title and best_title.lower() != sku.lower():
            if p.stem.lower() == sku.lower():
                new_stem = clean_title_slug(sku, best_title)
                new_sc_rel = (p.parent / f"{new_stem}.sc").as_posix()

        new_sc_abs = COTS_DIR / new_sc_rel
        if new_sc_rel != cur_sc_rel:
            if cur_sc_abs.exists() and not new_sc_abs.exists():
                cur_sc_abs.rename(new_sc_abs)
                renamed_files += 1
            elif cur_sc_abs.exists() and new_sc_abs.exists() and cur_sc_abs.resolve() != new_sc_abs.resolve():
                cur_sc_abs.unlink()
                renamed_files += 1

        if new_sc_rel != cur_sc_rel or (best_title and best_title != cur_title):
            updates.append((new_sc_rel, best_title or cur_title, pid))
            updated_rows += 1

    if updates:
        conn.executemany(
            "UPDATE local_cots_parts SET solidcraft_path = ?, title = ? WHERE part_id = ?",
            updates,
        )
        conn.commit()
    conn.close()
    return {
        "status": "ok",
        "renamed_sc_files_on_disk": renamed_files,
        "updated_db_rows": updated_rows,
    }


def safe_thumb_filename(part_id: str) -> str:
    """Return a clean filename `<slug>.png` for `part_id` inside `cots/solidcraft/thumbs/`."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(part_id)).strip("_")
    return f"{slug[:110]}.png"


def render_all_cots_thumbnails(
    db_path: Path = DB_PATH,
    workers: int = 10,
    force: bool = False,
    limit: Optional[int] = None,
    width: int = 220,
    height: int = 165,
) -> Dict[str, Any]:
    """
    Batch-render 3D isometric PNG thumbnails (`cots/solidcraft/thumbs/<part_id>.png`)
    for all `.sc` COTS parts using a persistent `solidcraft-cli mcp --headless` pool.
    """
    cli_path = find_solidcraft_cli()
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT part_id, solidcraft_path, COALESCE(file_size_bytes, 0) AS sz
        FROM local_cots_parts
        WHERE status = 'VERIFIED_STEP'
          AND solidcraft_path IS NOT NULL
          AND solidcraft_path != ''
        ORDER BY sz ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    tasks: List[Tuple[str, Path, Path]] = []
    skipped = 0
    for r in rows:
        pid = str(r["part_id"])
        sc_rel = str(r["solidcraft_path"])
        if sc_rel.lower().startswith("cots/"):
            sc_rel = sc_rel[5:]
        sc_abs = COTS_DIR / sc_rel
        if not sc_abs.exists():
            continue
        t_path = THUMBS_DIR / safe_thumb_filename(pid)
        if not force and t_path.exists() and t_path.stat().st_size > 100:
            skipped += 1
            continue
        tasks.append((pid, sc_abs, t_path))

    if not tasks:
        return {
            "status": "ok",
            "total_candidates": len(rows),
            "rendered": 0,
            "skipped_existing": skipped,
            "failed": 0,
            "thumbs_dir": str(THUMBS_DIR),
        }

    logger.info(
        "Rendering %d isometric COTS thumbnails into %s using %d workers (%d already cached)...",
        len(tasks),
        THUMBS_DIR,
        workers,
        skipped,
    )

    work_q: "queue.Queue[Optional[Tuple[int, Tuple[str, Path, Path]]]]" = queue.Queue()
    for idx, item in enumerate(tasks, 1):
        work_q.put((idx, item))
    for _ in range(workers):
        work_q.put(None)

    stats = {"rendered": 0, "failed": 0}
    lock = threading.Lock()
    t0 = time.time()

    def _worker() -> None:
        proc: Optional[subprocess.Popen] = None
        req_id = 0

        def _start() -> None:
            nonlocal proc, req_id
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            proc = subprocess.Popen(
                [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            req_id = 1
            proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "solidcraft-thumb-worker", "version": "1.0.0"},
                    },
                })
                + "\n"
            )
            proc.stdin.flush()
            proc.stdout.readline()

        def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal proc, req_id
            if proc is None or proc.poll() is not None:
                _start()
            req_id += 1
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("EOF from headless worker")
            return json.loads(line)

        _start()
        since_restart = 0
        try:
            while True:
                job = work_q.get()
                if job is None:
                    work_q.task_done()
                    break
                idx, (pid, sc_abs, t_path) = job
                ok = False
                try:
                    r_open = _rpc("tools/call", {"name": "open", "arguments": {"path": str(sc_abs.resolve())}})
                    if not r_open.get("result", {}).get("isError"):
                        r_shot = _rpc(
                            "tools/call",
                            {
                                "name": "screenshot",
                                "arguments": {
                                    "view": "iso",
                                    "width": width,
                                    "height": height,
                                    "grid": False,
                                    "axes": False,
                                    "path": str(t_path.resolve()),
                                },
                            },
                        )
                        ok = t_path.exists() and t_path.stat().st_size > 100
                    since_restart += 1
                    if since_restart >= 100 or sc_abs.stat().st_size > 20 * 1024 * 1024:
                        _start()
                        since_restart = 0
                except Exception:
                    _start()
                    since_restart = 0

                with lock:
                    if ok:
                        stats["rendered"] += 1
                    else:
                        stats["failed"] += 1
                    done = stats["rendered"] + stats["failed"]
                    if done % 100 == 0 or done == len(tasks):
                        el = max(0.01, time.time() - t0)
                        rate = done / el
                        eta = (len(tasks) - done) / max(0.01, rate)
                        logger.info(
                            "Thumbnails: %d/%d (%.1f%%) | ok=%d fail=%d | %.1f/s | ETA %.0fs",
                            done,
                            len(tasks),
                            100.0 * done / len(tasks),
                            stats["rendered"],
                            stats["failed"],
                            rate,
                            eta,
                        )
                work_q.task_done()
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return {
        "status": "ok",
        "total_candidates": len(rows),
        "rendered": stats["rendered"],
        "skipped_existing": skipped,
        "failed": stats["failed"],
        "elapsed_sec": round(time.time() - t0, 2),
        "thumbs_dir": str(THUMBS_DIR),
    }


def _axis_vec(axis_str: Optional[str]) -> List[float]:
    ax = (axis_str or "Z").upper().strip()
    sign = -1.0 if ax.startswith("-") else 1.0
    if ax.endswith("X"):
        return [sign, 0.0, 0.0]
    if ax.endswith("Y"):
        return [0.0, sign, 0.0]
    return [0.0, 0.0, sign]


def _parse_ratio(ratio_val: Any) -> float:
    if isinstance(ratio_val, (int, float)):
        return float(ratio_val)
    s = str(ratio_val or "1").strip()
    try:
        return float(Fraction(s))
    except Exception:
        try:
            return float(s)
        except Exception:
            return 1.0


def _match_part_to_cots(
    part_name: str,
    cloud_map: Dict[str, Dict[str, Any]],
    sku_lookup: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Resolve a STEP assembly part label (e.g. `13_COTS_GoBILDA_5203_Shooter_Motor` or
    `36_01_COTS_M4_X_Bearing_Inner_Spool_Clamp_Bolt_000Deg`) to its canonical BOM metadata
    and physical material using Option 1 (Hybrid In-Place Tagging).
    """
    # 1. Check explicit `onshape_cloud_instances` mapping in *_constraints.json
    if part_name in cloud_map:
        ci = cloud_map[part_name]
        sku = str(ci.get("sku") or "").strip()
        vendor = str(ci.get("vendor") or "cots").strip()
        db_row = sku_lookup.get(sku.lower()) if sku else None
        title = (
            (db_row.get("title") if db_row else None)
            or ci.get("insertable_name")
            or part_name
        )
        cat = (db_row.get("category") if db_row else None) or ""
        mat = (
            (db_row.get("solidcraft_material") if db_row else None)
            or infer_solidcraft_material(vendor, cat, f"{part_name} {title}", sku)
        )
        return {
            "is_cots": True,
            "part_number": sku or part_name,
            "description": str(title),
            "vendor": vendor,
            "bom_type": "purchased",
            "material": mat,
            "solidcraft_path": (db_row.get("solidcraft_path") if db_row else None) or ci.get("solidcraft_path"),
        }

    # 2. Check if part name contains a known SKU pattern or `COTS` tag
    # Normalize underscore-separated goBILDA/REV SKUs like `1201_0043_0002` -> `1201-0043-0002`
    m_gb = re.search(r"\b(\d{4})[-_](\d{4})[-_](\d{4})\b", part_name)
    m_gb_short = re.search(r"\b(5202|5203|5204|2000|1611|1600|1601|1603|3417|3626)\b", part_name)
    m_rev = re.search(r"\b(REV[-_]\d{2}[-_]\d{4})\b", part_name, re.IGNORECASE)
    m_mcm = re.search(r"\b(\d{4,5}A\d{3})\b", part_name)

    extracted_sku: Optional[str] = None
    if m_gb:
        extracted_sku = f"{m_gb.group(1)}-{m_gb.group(2)}-{m_gb.group(3)}"
    elif m_rev:
        extracted_sku = m_rev.group(1).upper().replace("_", "-")
    elif m_mcm:
        extracted_sku = m_mcm.group(1)

    if extracted_sku and extracted_sku.lower() in sku_lookup:
        db_row = sku_lookup[extracted_sku.lower()]
        return {
            "is_cots": True,
            "part_number": db_row.get("sku") or extracted_sku,
            "description": db_row.get("title") or part_name,
            "vendor": db_row.get("vendor") or "cots",
            "bom_type": "purchased",
            "material": db_row.get("solidcraft_material")
            or infer_solidcraft_material(db_row.get("vendor", ""), db_row.get("category", ""), part_name, extracted_sku),
            "solidcraft_path": db_row.get("solidcraft_path"),
        }

    is_cots = "cots" in part_name.lower() or bool(extracted_sku) or bool(m_gb_short)
    is_ref = part_name.lower().startswith("ref_") or "_ref_" in part_name.lower() or "gamepiece" in part_name.lower()

    # Strip leading numeric index (`36_01_`, `01_`) and trailing instance suffixes (`_000Deg`, `_01`, `_Left`, `_Right`)
    # so repeated COTS fasteners/rollers share a canonical part_number and roll up in `assembly.bom`!
    base_label = re.sub(r"^\d+(?:_\d+)?_", "", part_name)
    if is_cots:
        canonical_pn = re.sub(r"_(?:\d{1,3}Deg|\d{2}|Left|Right|Front|Rear|Upper|Lower)$", "", base_label, flags=re.IGNORECASE)
        if extracted_sku:
            canonical_pn = extracted_sku
    else:
        canonical_pn = base_label

    human_desc = base_label.replace("_", " ")
    mat = infer_solidcraft_material("", "", part_name, extracted_sku)
    if not is_cots and "3dp" in part_name.lower():
        mat = "ABS Plastic"

    return {
        "is_cots": is_cots,
        "part_number": canonical_pn,
        "description": human_desc,
        "vendor": "COTS" if is_cots else "Custom",
        "bom_type": "reference" if is_ref else ("purchased" if is_cots else "normal"),
        "material": mat,
    }


def compile_project_to_solidcraft(
    step_path: Path,
    constraints_path: Optional[Path] = None,
    out_sc_path: Optional[Path] = None,
    export_gif: bool = True,
    live_push: bool = False,
    connect_port: int = 27878,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """
    Compile a project STEP assembly (`.step` or `.step.gz`) and `*_constraints.json` into a
    fully jointed, motion-coupled SolidCraft `.sc` assembly with full verification deliverables:
    - `<stem>.sc`: Self-contained jointed SolidCraft assembly with Hybrid In-Place COTS Tagging
    - `<stem>_sc_bom.csv` & `<stem>_sc_bom.md`: Grouped Bill of Materials from `assembly.bom`
    - `<stem>_sc_motion.gif`: Driven kinematic animation from `motion.export_animation`
    - `<stem>_sc_verification.json`: Mass/CoM/Inertia report, DOF audit, and `joint.sweep` check
    """
    cli_path = find_solidcraft_cli()
    step_path = Path(step_path).resolve()

    # Support `.step.gz` automatically
    temp_step_dir: Optional[Path] = None
    actual_step_path = step_path
    if not actual_step_path.exists() and Path(str(actual_step_path) + ".gz").exists():
        actual_step_path = Path(str(actual_step_path) + ".gz")

    if actual_step_path.name.endswith(".gz"):
        base_step_name = actual_step_path.name[:-3]
        sibling_step = actual_step_path.parent / base_step_name
        if sibling_step.exists():
            actual_step_path = sibling_step
        else:
            temp_step_dir = Path(tempfile.mkdtemp(prefix="sc_compile_"))
            actual_step_path = temp_step_dir / base_step_name
            with gzip.open(step_path if step_path.exists() else Path(str(step_path) + ".gz"), "rb") as f_in, open(actual_step_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

    if not actual_step_path.exists():
        raise FileNotFoundError(f"STEP file not found: {step_path}")

    stem = re.sub(r"\.step(?:\.gz)?$", "", actual_step_path.name, flags=re.IGNORECASE)
    cad_dir = step_path.parent
    docs_dir = cad_dir.parent / "docs" if (cad_dir.parent / "docs").exists() else cad_dir

    # Auto-discover *_constraints.json if omitted
    if constraints_path is None:
        candidates = [
            cad_dir / f"{stem}_constraints.json",
            cad_dir / f"{re.sub(r'_15to1$', '', stem)}_constraints.json",
        ] + sorted(cad_dir.glob("*_constraints.json"))
        for c in candidates:
            if c.exists():
                constraints_path = c
                break

    spec: Dict[str, Any] = {}
    if constraints_path and Path(constraints_path).exists():
        spec = json.loads(Path(constraints_path).read_text(encoding="utf-8"))

    if out_sc_path is None:
        out_sc_path = cad_dir / f"{stem}.sc"
    out_sc_path = Path(out_sc_path).resolve()

    # Load SKU lookup from catalog.db
    sku_lookup: Dict[str, Dict[str, Any]] = {}
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT sku, vendor, category, title, solidcraft_material, solidcraft_path FROM local_cots_parts WHERE sku IS NOT NULL"):
            s = str(r["sku"]).strip().lower()
            if s and s not in sku_lookup:
                sku_lookup[s] = dict(r)
        conn.close()

    cloud_map: Dict[str, Dict[str, Any]] = {}
    for ci in spec.get("onshape_cloud_instances") or []:
        if isinstance(ci, dict) and ci.get("part_label"):
            cloud_map[str(ci["part_label"])] = ci

    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"SolidCraft compiler worker EOF: {err}")
        return json.loads(line)

    try:
        _rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "solidcraft-project-compiler", "version": "1.0.0"},
        })

        # 1. Determine whether to use Linked Native .sc + COTS .sc Assembly Mode or Monolithic STEP Mode
        native_sc_list = [
            n for n in (spec.get("native_solidcraft_components") or [])
            if isinstance(n, dict) and n.get("part_label") and n.get("solidcraft_path")
        ]
        all_cloud_cots = [
            ci for ci in (spec.get("onshape_cloud_instances") or [])
            if isinstance(ci, dict) and ci.get("part_label")
        ]
        linked_cots_list = [
            ci for ci in all_cloud_cots
            if ci.get("solidcraft_path") and ci.get("solidcraft_world_matrix_4x4")
        ]
        custom_step_rel = spec.get("custom_step_path")
        custom_step_candidate = (
            (WORKSPACE_ROOT / str(custom_step_rel))
            if custom_step_rel
            else (cad_dir / f"{stem}_custom.step")
        )

        def _resolve_sc_path(rel_or_abs: str) -> Optional[Path]:
            p_cand = Path(rel_or_abs)
            candidates = [p_cand] if p_cand.is_absolute() else [WORKSPACE_ROOT / p_cand, COTS_DIR / p_cand, cad_dir / p_cand]
            for cand in candidates:
                if cand.exists():
                    return cand.resolve()
                gz = Path(str(cand) + ".gz")
                if gz.exists():
                    with gzip.open(gz, "rb") as f_in, open(cand, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    return cand.resolve()
            return None

        all_sc_resolved = bool(
            (native_sc_list or linked_cots_list)
            and len(linked_cots_list) == len(all_cloud_cots)
            and all(_resolve_sc_path(str(n["solidcraft_path"])) is not None for n in native_sc_list)
            and all(_resolve_sc_path(str(ci["solidcraft_path"])) is not None for ci in linked_cots_list)
        )
        use_linked_mode = bool(
            all_sc_resolved
            and (custom_step_candidate.exists() or (native_sc_list and not custom_step_rel))
        )

        inserted_links: List[Dict[str, Any]] = []
        linked_comp_ids: set[int] = set()
        linked_cots_comp_meta: Dict[int, Dict[str, Any]] = {}
        linked_native_comp_ids: set[int] = set()

        if use_linked_mode:
            if custom_step_candidate.exists():
                r_open = _rpc("tools/call", {"name": "open", "arguments": {"path": str(custom_step_candidate.resolve())}})
                open_info = json.loads(r_open["result"]["content"][0]["text"])
                root_comp_id = open_info.get("component", 1)
                _rpc("tools/call", {
                    "name": "execute",
                    "arguments": {"command": "component.unlock", "params": {"component": root_comp_id}},
                })
            else:
                _rpc("tools/call", {"name": "new_design", "arguments": {"name": f"{stem}_Assy"}})
                root_comp_id = 0

            # Insert ALL native .sc and COTS .sc components FIRST while at identity [0, 0, 0]
            # so `reuse: True` clones from the identity origin before `occurrence.move`.
            for n_item in native_sc_list:
                sc_p = _resolve_sc_path(str(n_item["solidcraft_path"]))
                if sc_p:
                    r_ins = _rpc("tools/call", {
                        "name": "execute",
                        "arguments": {"command": "component.insert_design", "params": {"path": str(sc_p), "reuse": True}},
                    })
                    ins_info = json.loads(r_ins["result"]["content"][0]["text"])
                    cid = int(ins_info.get("component", -1))
                    oid = int(ins_info.get("occurrence", -1))
                    linked_comp_ids.add(cid)
                    linked_native_comp_ids.add(cid)
                    inserted_links.append({
                        "part_label": str(n_item["part_label"]),
                        "comp_id": cid,
                        "occ_id": oid,
                        "matrix": n_item.get("world_matrix_4x4"),
                        "is_cots": False,
                    })

            for ci_item in linked_cots_list:
                sc_p = _resolve_sc_path(str(ci_item["solidcraft_path"]))
                if sc_p:
                    r_ins = _rpc("tools/call", {
                        "name": "execute",
                        "arguments": {"command": "component.insert_design", "params": {"path": str(sc_p), "reuse": True}},
                    })
                    ins_info = json.loads(r_ins["result"]["content"][0]["text"])
                    cid = int(ins_info.get("component", -1))
                    oid = int(ins_info.get("occurrence", -1))
                    linked_comp_ids.add(cid)
                    linked_cots_comp_meta[cid] = _match_part_to_cots(str(ci_item["part_label"]), cloud_map, sku_lookup)
                    inserted_links.append({
                        "part_label": str(ci_item["part_label"]),
                        "comp_id": cid,
                        "occ_id": oid,
                        "matrix": ci_item.get("solidcraft_world_matrix_4x4"),
                        "is_cots": True,
                    })

            identity_16 = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
            move_cmds: List[Dict[str, Any]] = []
            for il in inserted_links:
                mat = il.get("matrix")
                if il["occ_id"] >= 0 and isinstance(mat, list) and len(mat) in (12, 16) and mat != identity_16:
                    move_cmds.append({
                        "command": "occurrence.move",
                        "params": {
                            "occurrence": il["occ_id"],
                            "space": "world",
                            "matrix": mat,
                            "tolerance": 1e-4,
                            "move_grounded": True,
                        },
                    })
            if move_cmds:
                _rpc("tools/call", {"name": "batch", "arguments": {"verbose": False, "commands": move_cmds}})
        else:
            # 1. Open monolithic STEP assembly
            r_open = _rpc("tools/call", {"name": "open", "arguments": {"path": str(actual_step_path)}})
            open_info = json.loads(r_open["result"]["content"][0]["text"])
            root_comp_id = open_info.get("component", 1)

            # 2. Unlock imported assembly rigid group so parts can take real kinematic joints
            _rpc("tools/call", {
                "name": "execute",
                "arguments": {"command": "component.unlock", "params": {"component": root_comp_id}},
            })

            # 2b. Replace any native SolidCraft feature-tree components (`native_sc_list`) with linked `.sc` designs
            if native_sc_list:
                identity_16 = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
                for n_item in native_sc_list:
                    sc_p = _resolve_sc_path(str(n_item["solidcraft_path"]))
                    if not sc_p:
                        continue
                    plbl = str(n_item["part_label"])
                    _rpc("tools/call", {
                        "name": "execute",
                        "arguments": {"command": "component.delete", "params": {"component": plbl}},
                    })
                    r_ins = _rpc("tools/call", {
                        "name": "execute",
                        "arguments": {"command": "component.insert_design", "params": {"path": str(sc_p), "reuse": True}},
                    })
                    if "result" in r_ins and not r_ins["result"].get("isError"):
                        ins_info = json.loads(r_ins["result"]["content"][0]["text"])
                        cid = int(ins_info.get("component", -1))
                        oid = int(ins_info.get("occurrence", -1))
                        linked_comp_ids.add(cid)
                        linked_native_comp_ids.add(cid)
                        inserted_links.append({
                            "part_label": plbl,
                            "comp_id": cid,
                            "occ_id": oid,
                            "matrix": n_item.get("world_matrix_4x4"),
                            "is_cots": False,
                        })
                        mat = n_item.get("world_matrix_4x4")
                        if oid >= 0 and isinstance(mat, list) and len(mat) in (12, 16) and mat != identity_16:
                            _rpc("tools/call", {
                                "name": "execute",
                                "arguments": {
                                    "command": "occurrence.move",
                                    "params": {
                                        "occurrence": oid,
                                        "space": "world",
                                        "matrix": mat,
                                        "tolerance": 1e-4,
                                        "move_grounded": True,
                                    },
                                },
                            })

        # 3. Inspect components and occurrences
        r_clist = _rpc("tools/call", {
            "name": "execute",
            "arguments": {"command": "component.list", "params": {"compact": True, "bodies": True}},
        })
        clist = json.loads(r_clist["result"]["content"][0]["text"])
        comp_by_name: Dict[str, Dict[str, Any]] = {}
        occ_by_part_name: Dict[str, str] = {}
        occ_name_by_id: Dict[int, str] = {}

        for c in clist.get("components", []):
            cname = str(c.get("name") or "")
            if c.get("id") in (0, root_comp_id):
                continue
            comp_by_name[cname] = c
            occs = c.get("occurrences") or []
            if occs:
                occ_by_part_name[cname] = str(occs[0].get("name") or f"{cname}:1")
            for occ in occs:
                if occ.get("id") is not None and occ.get("name"):
                    occ_name_by_id[int(occ["id"])] = str(occ["name"])

        for il in inserted_links:
            oid = il["occ_id"]
            if oid in occ_name_by_id:
                occ_by_part_name[il["part_label"]] = occ_name_by_id[oid]

        # Snapshot previous verification report if re-compiling an existing assembly (Pillar 3)
        prev_verif: Optional[Dict[str, Any]] = None
        verif_path = docs_dir / f"{stem}_sc_verification.json"
        if verif_path.exists():
            try:
                prev_verif = json.loads(verif_path.read_text(encoding="utf-8"))
            except Exception:
                prev_verif = None

        # 4. Hybrid In-Place COTS Tagging & Material Assignment
        tag_cmds: List[Dict[str, Any]] = []
        cots_tagged_count = 0
        custom_tagged_count = 0
        custom_bodies: List[str] = []
        custom_occurrences: List[str] = []
        for cname, c_obj in comp_by_name.items():
            cid = int(c_obj.get("id", -1))
            bodies = c_obj.get("bodies") or []
            if cid in linked_cots_comp_meta:
                meta = linked_cots_comp_meta[cid]
                cots_tagged_count += len(c_obj.get("occurrences") or [1])
            elif cid in linked_native_comp_ids:
                meta = _match_part_to_cots(cname, cloud_map, sku_lookup)
                custom_tagged_count += len(c_obj.get("occurrences") or [1])
                for b in bodies:
                    if not any(k in b.lower() for k in ("belt", "chain", "gamepiece", "ball", "ref_")):
                        custom_bodies.append(b)
                for occ in c_obj.get("occurrences") or []:
                    oname = str(occ.get("name") or "")
                    if oname and not any(k in cname.lower() for k in ("belt", "chain", "gamepiece", "ball", "ref_")):
                        custom_occurrences.append(oname)
            else:
                meta = _match_part_to_cots(cname, cloud_map, sku_lookup)
                if meta["is_cots"]:
                    cots_tagged_count += 1
                else:
                    custom_tagged_count += 1
                    for b in bodies:
                        if not any(k in b.lower() for k in ("belt", "chain", "gamepiece", "ball", "ref_")):
                            custom_bodies.append(b)
                    if cname in occ_by_part_name and not any(k in cname.lower() for k in ("belt", "chain", "gamepiece", "ball", "ref_")):
                        custom_occurrences.append(occ_by_part_name[cname])
                if bodies and meta["material"]:
                    tag_cmds.append({
                        "command": "material.assign",
                        "params": {"bodies": bodies, "material": meta["material"]},
                    })
            props_dict = {
                "vendor": meta["vendor"],
                "material": meta["material"],
            }
            if meta.get("solidcraft_path"):
                props_dict["solidcraft_path"] = str(meta["solidcraft_path"])
            tag_cmds.append({
                "command": "component.metadata.set",
                "params": {
                    "component": c_obj["id"],
                    "part_number": meta["part_number"],
                    "description": meta["description"],
                    "bom_type": meta["bom_type"],
                    "properties": props_dict,
                },
            })

        if tag_cmds:
            _rpc("tools/call", {"name": "batch", "arguments": {"verbose": False, "commands": tag_cmds}})

        # 5. Translate *_constraints.json -> Grounding, Rigid Groups, Joints & Motion Links
        mc_map: Dict[str, Dict[str, Any]] = {}
        for mc in spec.get("mate_connectors") or []:
            if isinstance(mc, dict) and mc.get("key"):
                mc_map[str(mc["key"])] = mc

        constraint_cmds: List[Dict[str, Any]] = []
        grounded_parts: List[str] = []

        # 5a. Grounding (`FASTENED` to `WORLD`)
        for m in spec.get("mates") or []:
            mtype = str(m.get("type") or "FASTENED").upper()
            mc1 = mc_map.get(str(m.get("mc1") or ""), {})
            mc2 = mc_map.get(str(m.get("mc2") or ""), {})
            p1, p2 = str(mc1.get("part") or ""), str(mc2.get("part") or "")
            if mtype == "FASTENED" and ("WORLD" in (p1.upper(), p2.upper())):
                target_part = p2 if p1.upper() == "WORLD" else p1
                occ_name = occ_by_part_name.get(target_part)
                if occ_name:
                    constraint_cmds.append({
                        "command": "occurrence.ground",
                        "params": {"occurrence": occ_name, "grounded": True},
                    })
                    grounded_parts.append(occ_name)

        # Fallback auto-ground first component if no explicit WORLD mate was present
        if not grounded_parts and occ_by_part_name:
            first_occ = next(iter(occ_by_part_name.values()))
            constraint_cmds.append({
                "command": "occurrence.ground",
                "params": {"occurrence": first_occ, "grounded": True},
            })
            grounded_parts.append(first_occ)

        # 5b. Rigid groups (`mate_groups`)
        rigid_group_count = 0
        for mg in spec.get("mate_groups") or []:
            parts = [occ_by_part_name[p] for p in (mg.get("parts") or []) if p in occ_by_part_name]
            if len(parts) >= 2:
                constraint_cmds.append({
                    "command": "joint.rigid_group",
                    "params": {"occurrences": parts},
                })
                rigid_group_count += 1

        # 5c. Kinematic joints (`mates`)
        type_map = {
            "FASTENED": "rigid",
            "RIGID": "rigid",
            "REVOLUTE": "revolute",
            "SLIDER": "slider",
            "PRISMATIC": "slider",
            "CYLINDRICAL": "cylindrical",
            "PLANAR": "planar",
            "BALL": "ball",
        }
        created_joints: List[Dict[str, Any]] = []
        for idx, m in enumerate(spec.get("mates") or [], 1):
            mtype_raw = str(m.get("type") or "FASTENED").upper()
            mc1 = mc_map.get(str(m.get("mc1") or ""), {})
            mc2 = mc_map.get(str(m.get("mc2") or ""), {})
            p1, p2 = str(mc1.get("part") or ""), str(mc2.get("part") or "")
            if "WORLD" in (p1.upper(), p2.upper()):
                continue
            occ1 = occ_by_part_name.get(p1)
            occ2 = occ_by_part_name.get(p2)
            if not occ1 or not occ2:
                continue

            sc_jtype = type_map.get(mtype_raw, "revolute")
            j_key = str(m.get("key") or f"joint_{idx}")
            pt1 = [float(mc1.get("tx_mm", 0.0)), float(mc1.get("ty_mm", 0.0)), float(mc1.get("tz_mm", 0.0))]
            pt2 = [float(mc2.get("tx_mm", 0.0)), float(mc2.get("ty_mm", 0.0)), float(mc2.get("tz_mm", 0.0))]
            z1 = _axis_vec(mc1.get("axis"))
            z2 = _axis_vec(mc2.get("axis"))

            j_params: Dict[str, Any] = {
                "type": sc_jtype,
                "name": j_key,
                "flip": True,
                "a": {"occurrence": occ1, "point": pt1, "z": z1},
                "b": {"occurrence": occ2, "point": pt2, "z": z2},
            }
            if m.get("limits"):
                j_params["limits"] = m["limits"]
            constraint_cmds.append({"command": "joint.create", "params": j_params})
            created_joints.append({"key": j_key, "type": sc_jtype, "a": occ1, "b": occ2, "label": m.get("name", j_key)})

        # 5d. Gear & belt couplings (`gear_relations` -> `joint.motion_link`)
        created_links: List[Dict[str, Any]] = []
        joint_keys = {j["key"] for j in created_joints}
        for gr in spec.get("gear_relations") or []:
            m1_key = str(gr.get("mate1") or "")
            m2_key = str(gr.get("mate2") or "")
            if m1_key not in joint_keys or m2_key not in joint_keys:
                continue
            r_mag = _parse_ratio(gr.get("ratio", 1.0))
            rev = bool(gr.get("reverse", False))
            # Per workspace convention: reverse=True means same-direction (+ratio), reverse=False means opposite (-ratio)
            signed_ratio = r_mag if rev else -r_mag
            constraint_cmds.append({
                "command": "joint.motion_link",
                "params": {
                    "a": m1_key,
                    "b": m2_key,
                    "ratio": signed_ratio,
                },
            })
            created_links.append({
                "name": gr.get("name", f"{m1_key}->{m2_key}"),
                "a": m1_key,
                "b": m2_key,
                "ratio": signed_ratio,
            })

        # Create a default Motion Study for the primary input joint so it is saved inside the .sc file
        primary_driver: Optional[str] = None
        for j in created_joints:
            if j["type"] in ("revolute", "slider"):
                primary_driver = j["key"]
                break

        if primary_driver:
            constraint_cmds.append({
                "command": "motion.study",
                "params": {
                    "name": "Primary_Drive_Cycle",
                    "steps": 60,
                    "keys": [
                        {
                            "joint": primary_driver,
                            "points": [[0, 0.0], [60, 360.0]],
                        }
                    ],
                },
            })

        if constraint_cmds:
            _rpc("tools/call", {"name": "batch", "arguments": {"verbose": False, "commands": constraint_cmds}})

        # 6. Save compiled `.sc` file
        out_sc_path.parent.mkdir(parents=True, exist_ok=True)
        _rpc("tools/call", {
            "name": "save",
            "arguments": {"path": str(out_sc_path)},
        })

        # 7. Generate BOM (CSV + JSON + Markdown)
        bom_csv_path = docs_dir / f"{stem}_sc_bom.csv"
        bom_md_path = docs_dir / f"{stem}_sc_bom.md"
        _rpc("tools/call", {
            "name": "execute",
            "arguments": {"command": "assembly.bom", "params": {"levels": "flat", "path": str(bom_csv_path)}},
        })
        r_bom = _rpc("tools/call", {
            "name": "execute",
            "arguments": {"command": "assembly.bom", "params": {"levels": "flat"}},
        })
        bom_data = json.loads(r_bom["result"]["content"][0]["text"])
        bom_rows = bom_data.get("rows") or []

        md_bom = [
            f"# SolidCraft Assembly BOM: `{stem}`",
            "",
            f"- **Source Assembly:** `{out_sc_path.name}`",
            f"- **Total Component Occurrences:** {bom_data.get('total_occurrences', 0)}",
            f"- **Unique Line Items:** {len(bom_rows)}",
            "",
            "| Item | Part Number / SKU | Description | Type | Material | Qty |",
            "| ---: | :--- | :--- | :--- | :--- | ---: |",
        ]
        for row in bom_rows:
            props = row.get("properties") or {}
            md_bom.append(
                f"| {row.get('item')} | `{row.get('part_number') or '—'}` | {row.get('description') or '—'} | "
                f"`{row.get('bom_type') or 'normal'}` | {props.get('material') or '—'} | **{row.get('count', 1)}** |"
            )
        bom_md_path.write_text("\n".join(md_bom) + "\n", encoding="utf-8")

        # 8. Mass & Inertia properties + Joint Status Audit
        r_mass = _rpc("tools/call", {
            "name": "execute",
            "arguments": {"command": "assembly.mass_properties", "params": {}},
        })
        mass_data = json.loads(r_mass["result"]["content"][0]["text"])
        total_mass_kg = round(
            sum(
                float((occ.get("mass") or {}).get("mass_kg") or 0.0)
                for occ in (mass_data.get("occurrences") or [])
            ),
            4,
        )

        r_jlist = _rpc("tools/call", {
            "name": "execute",
            "arguments": {"command": "joint.list", "params": {}},
        })
        jlist_data = json.loads(r_jlist["result"]["content"][0]["text"])

        # 8b. Auto-tag Print Faces (`body.print_face`) on custom 3DP bodies (Pillar 4)
        print_faces_tagged: List[Dict[str, Any]] = []
        step_size_mb = actual_step_path.stat().st_size / (1024.0 * 1024.0)
        is_heavy_assembly = step_size_mb > 35.0 or len(comp_by_name) > 30
        if custom_bodies and not is_heavy_assembly:
            r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
            if "result" in r_insp and not r_insp["result"].get("isError"):
                insp_info = json.loads(r_insp["result"]["content"][0]["text"])
                bbox_by_body = {
                    b.get("name"): b.get("bbox")
                    for b in (insp_info.get("bodies") or [])
                    if isinstance(b, dict) and b.get("name") and b.get("bbox")
                }
                for cb in custom_bodies[:16]:
                    bb = bbox_by_body.get(cb)
                    bmin = bb.get("min") if isinstance(bb, dict) else (bb[0] if isinstance(bb, list) and len(bb) == 2 else None)
                    bmax = bb.get("max") if isinstance(bb, dict) else (bb[1] if isinstance(bb, list) and len(bb) == 2 else None)
                    if bmin and bmax:
                        cx = 0.5 * (float(bmin[0]) + float(bmax[0]))
                        cy = 0.5 * (float(bmin[1]) + float(bmax[1]))
                        z_min = float(bmin[2])
                        r_pf = _rpc("tools/call", {
                            "name": "execute",
                            "arguments": {
                                "command": "body.print_face",
                                "params": {"body": cb, "point": [round(cx, 3), round(cy, 3), round(z_min, 3)]},
                            },
                        })
                        if "result" in r_pf and not r_pf["result"].get("isError"):
                            pf_res = json.loads(r_pf["result"]["content"][0]["text"])
                            if pf_res.get("face"):
                                print_faces_tagged.append(pf_res)
                if print_faces_tagged:
                    _rpc("tools/call", {"name": "save", "arguments": {"path": str(out_sc_path)}})

        # 9. Native Rust Clearance Gate (`inspect.clearance`) & Dynamic Sweep (`joint.sweep`) (Pillar 1)
        gif_path: Optional[Path] = None
        sweep_summary: Optional[Dict[str, Any]] = None
        clearance_raw: Optional[Dict[str, Any]] = None

        if custom_bodies and not is_heavy_assembly:
            r_cl = _rpc("tools/call", {
                "name": "execute",
                "arguments": {
                    "command": "inspect.clearance",
                    "params": {
                        "bodies": custom_bodies[:24],
                        "skip_within": "occurrence",
                        "clearance": 0.5,
                        "min_volume": 0.01,
                        "nearest": 2,
                    },
                },
            })
            if "result" in r_cl and not r_cl["result"].get("isError"):
                clearance_raw = json.loads(r_cl["result"]["content"][0]["text"])

        if primary_driver:
            if not is_heavy_assembly:
                moving_occs = [j["b"] for j in created_joints if j["type"] in ("revolute", "slider", "cylindrical")]
                stationary_occs = [o for o in custom_occurrences if o not in moving_occs]
                sweep_params: Dict[str, Any] = {
                    "joint": primary_driver,
                    "from": 0,
                    "to": 360,
                    "step": 15,
                    "refine": 0.01,
                }
                if moving_occs and stationary_occs:
                    sweep_params["occurrences"] = moving_occs[:8]
                    sweep_params["against"] = stationary_occs[:12]
                    sweep_params["report_gaps"] = True
                r_sweep = _rpc("tools/call", {
                    "name": "execute",
                    "arguments": {
                        "command": "joint.sweep",
                        "params": sweep_params,
                    },
                })
                if "result" in r_sweep and not r_sweep["result"].get("isError"):
                    sw = json.loads(r_sweep["result"]["content"][0]["text"])
                    sweep_summary = {
                        "driver_joint": primary_driver,
                        "range_deg": [0, 360],
                        "stopped_at": sw.get("stopped_at"),
                        "stopped_by": sw.get("stopped_by"),
                        "pairs_checked": sw.get("pairs"),
                        "touching_at_start_count": len(sw.get("touching_at_start") or []),
                        "min_gap": sw.get("min_gap"),
                    }

            if export_gif:
                gif_path = docs_dir / f"{stem}_sc_motion.gif"
                _rpc("tools/call", {
                    "name": "execute",
                    "arguments": {
                        "command": "motion.export_animation",
                        "params": {
                            "study": "Primary_Drive_Cycle",
                            "path": str(gif_path),
                            "samples": 16 if is_heavy_assembly else 36,
                            "width": 480 if is_heavy_assembly else 640,
                            "height": 320 if is_heavy_assembly else 420,
                            "fps": 16 if is_heavy_assembly else 18,
                            "grid": False,
                            "axes": False,
                        },
                    },
                })

        clearance_gate = classify_solidcraft_clearance_and_sweep(clearance_raw, sweep_summary)
        revision_diff: Optional[Dict[str, Any]] = None
        if prev_verif and isinstance(prev_verif, dict):
            prev_mass = float(prev_verif.get("total_mass_kg") or 0.0)
            prev_comps = int(prev_verif.get("total_components") or 0)
            prev_joints = int(prev_verif.get("joints_created") or 0)
            revision_diff = {
                "previous_mass_kg": prev_mass,
                "delta_mass_kg": round(total_mass_kg - prev_mass, 4),
                "previous_components": prev_comps,
                "delta_components": len(comp_by_name) - prev_comps,
                "previous_joints": prev_joints,
                "delta_joints": len(created_joints) - prev_joints,
            }

        overall_status = "PASS"
        if jlist_data.get("conflicts"):
            overall_status = "CONFLICTS_DETECTED"
        elif clearance_gate.get("gate_status") == "COLLISION_FAIL":
            overall_status = "COLLISION_FAIL"

        verif_report = {
            "status": overall_status,
            "assembly_mode": "linked_sc" if use_linked_mode else ("hybrid_linked_sc" if linked_native_comp_ids else "monolithic_step"),
            "native_sc_components_linked": len(linked_native_comp_ids),
            "cots_sc_occurrences_linked": len(linked_cots_list) if use_linked_mode else 0,
            "assembly_sc_path": str(out_sc_path),
            "source_step_path": str(step_path),
            "constraints_path": str(constraints_path) if constraints_path else None,
            "total_components": len(comp_by_name),
            "cots_components_tagged": cots_tagged_count,
            "custom_components_tagged": custom_tagged_count,
            "grounded_occurrences": grounded_parts,
            "rigid_groups_created": rigid_group_count,
            "joints_created": len(created_joints),
            "motion_links_created": len(created_links),
            "solver_conflicts": jlist_data.get("conflicts") or [],
            "floating_chains": jlist_data.get("floating") or [],
            "total_mass_kg": total_mass_kg,
            "bom_unique_rows": len(bom_rows),
            "bom_total_occurrences": bom_data.get("total_occurrences", 0),
            "bom_csv_path": str(bom_csv_path),
            "bom_md_path": str(bom_md_path),
            "motion_gif_path": str(gif_path) if (gif_path and gif_path.exists()) else None,
            "kinematic_sweep": sweep_summary,
            "clearance_gate": clearance_gate,
            "print_faces_tagged": print_faces_tagged,
            "revision_diff": revision_diff,
        }
        verif_path.write_text(json.dumps(verif_report, indent=2), encoding="utf-8")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
        if temp_step_dir and temp_step_dir.exists():
            shutil.rmtree(temp_step_dir, ignore_errors=True)

    # 10. Optional Live GUI Push (`--live`)
    live_opened = False
    if live_push and out_sc_path.exists():
        try:
            live_proc = subprocess.Popen(
                [cli_path, "--max-file-size", "2GB", "mcp", "--allow-any-path", "--connect", f"127.0.0.1:{int(connect_port)}"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            live_proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-live-push", "version": "1.0"}},
                })
                + "\n"
            )
            live_proc.stdin.flush()
            live_proc.stdout.readline()
            live_proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "open", "arguments": {"path": str(out_sc_path)}},
                })
                + "\n"
            )
            live_proc.stdin.flush()
            live_proc.stdout.readline()
            live_proc.terminate()
            live_opened = True
        except Exception as exc:
            logger.warning("Could not push to live SolidCraft on port %d: %s", connect_port, exc)

    verif_report["live_pushed"] = live_opened
    verif_report["verification_json_path"] = str(verif_path)
    return verif_report




EXPECTED_INTERFACE_KEYWORDS = (
    "screw", "bolt", "nut", "washer", "standoff", "spacer", "collar", "insert",
    "heatset", "bearing", "bushing", "brg", "1611", "1600", "belt", "pulley",
    "gear", "sprocket", "disc", "lobe", "roller", "pin", "cam", "shaft", "axle",
    "motor", "servo", "encoder", "hex", "rex", "spline", "wheel", "flywheel",
    "tire", "core", "hub", "cots",
)


def classify_solidcraft_clearance_and_sweep(
    clearance_raw: Optional[Dict[str, Any]],
    sweep_summary: Optional[Dict[str, Any]] = None,
    max_allowed_overlap_mm3: float = 0.05,
    near_clearance_warn_mm: float = 0.5,
) -> Dict[str, Any]:
    """
    Smart 3-Tier Classification of SolidCraft Rust `inspect.clearance` & `joint.sweep` results:
    - `EXPECTED_INTERFACE`: Fasteners/threads, press-fit bearings, meshing belts/gears/cams,
      coaxial shafts/hubs, `touching_at_start` mated faces, and grazing contacts <= `max_allowed_overlap_mm3`.
    - `NEAR_CLEARANCE_WARN`: Non-overlapping pairs with `0 < distance_mm < near_clearance_warn_mm`.
    - `COLLISION_FAIL`: Unexpected structural B-Rep overlaps `> max_allowed_overlap_mm3` or
      unexpected dynamic `joint.sweep` collision stops (`stopped_by`).
    """
    expected_interfaces: List[Dict[str, Any]] = []
    near_warnings: List[Dict[str, Any]] = []
    collision_failures: List[Dict[str, Any]] = []

    if clearance_raw and isinstance(clearance_raw, dict):
        for ov in clearance_raw.get("overlaps") or []:
            a = str(ov.get("a") or "")
            b = str(ov.get("b") or "")
            vol = float(ov.get("volume_mm3") or 0.0)
            pair_low = f"{a} {b}".lower()
            is_expected = (vol <= max_allowed_overlap_mm3) or any(k in pair_low for k in EXPECTED_INTERFACE_KEYWORDS)
            entry = {
                "a": a,
                "b": b,
                "volume_mm3": round(vol, 4),
                "center_mm": ov.get("center"),
                "tier": "EXPECTED_INTERFACE" if is_expected else "COLLISION_FAIL",
            }
            if is_expected:
                expected_interfaces.append(entry)
            else:
                collision_failures.append(entry)

        for nr in clearance_raw.get("near") or []:
            dist = float(nr.get("distance_mm") or 0.0)
            if 0.0 < dist < near_clearance_warn_mm:
                near_warnings.append({
                    "a": nr.get("a"),
                    "b": nr.get("b"),
                    "distance_mm": round(dist, 4),
                    "from_mm": nr.get("from"),
                    "to_mm": nr.get("to"),
                    "tier": "NEAR_CLEARANCE_WARN",
                })

    if sweep_summary and isinstance(sweep_summary, dict):
        stopped_by = sweep_summary.get("stopped_by")
        if isinstance(stopped_by, dict) and stopped_by.get("a") and stopped_by.get("b"):
            sa = str(stopped_by.get("a") or "")
            sb = str(stopped_by.get("b") or "")
            pair_low = f"{sa} {sb}".lower()
            if not any(k in pair_low for k in EXPECTED_INTERFACE_KEYWORDS):
                collision_failures.append({
                    "a": sa,
                    "b": sb,
                    "at_angle_or_pos": sweep_summary.get("stopped_at"),
                    "contact_point_mm": stopped_by.get("at"),
                    "tier": "COLLISION_FAIL",
                    "source": "joint.sweep",
                })

    gate_status = "COLLISION_FAIL" if collision_failures else ("NEAR_CLEARANCE_WARN" if near_warnings else "PASS")
    return {
        "gate_status": gate_status,
        "collision_fail_count": len(collision_failures),
        "near_warning_count": len(near_warnings),
        "expected_interface_count": len(expected_interfaces),
        "collision_failures": collision_failures,
        "near_warnings": near_warnings,
        "expected_interfaces": expected_interfaces,
        "engine_ms": round(float(clearance_raw.get("ms") or 0.0), 2) if clearance_raw else None,
        "nearest_by_body": clearance_raw.get("nearest") if clearance_raw else None,
    }


def solidcraft_inspect_assembly(
    target_path: Path,
    clearance_mm: float = 0.5,
    min_volume_mm3: float = 0.05,
    custom_only: bool = True,
) -> Dict[str, Any]:
    """
    Fast standalone SolidCraft Rust B-Rep clearance & dynamic sweep auditor (`sc-inspect`).
    Runs `inspect.clearance` in parallel across CPU cores and applies the Smart 3-Tier
    Classification (`EXPECTED_INTERFACE`, `NEAR_CLEARANCE_WARN`, `COLLISION_FAIL`).
    """
    cli_path = find_solidcraft_cli()
    target_path = Path(target_path).resolve()
    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-inspect", "version": "1.0"}})
        _rpc("tools/call", {"name": "open", "arguments": {"path": str(target_path)}})
        r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
        insp_data = json.loads(r_insp["result"]["content"][0]["text"])
        all_bodies = [b.get("name") for b in (insp_data.get("bodies") or []) if isinstance(b, dict) and b.get("name")]
        if custom_only:
            filtered = [
                b for b in all_bodies
                if "cots" not in b.lower() and not any(k in b.lower() for k in ("belt", "chain", "gamepiece"))
            ]
            bodies_to_check = filtered if len(filtered) >= 2 else [b for b in all_bodies if "belt" not in b.lower()]
        else:
            bodies_to_check = [b for b in all_bodies if "belt" not in b.lower()]

        r_cl = _rpc("tools/call", {
            "name": "execute",
            "arguments": {
                "command": "inspect.clearance",
                "params": {
                    "bodies": bodies_to_check[:32],
                    "skip_within": "occurrence",
                    "clearance": clearance_mm,
                    "min_volume": 0.01,
                    "nearest": 3,
                },
            },
        })
        cl_data = json.loads(r_cl["result"]["content"][0]["text"]) if ("result" in r_cl and not r_cl["result"].get("isError")) else {}
        return classify_solidcraft_clearance_and_sweep(cl_data, None, max_allowed_overlap_mm3=min_volume_mm3, near_clearance_warn_mm=clearance_mm)
    finally:
        proc.terminate()


def solidcraft_compare_revisions(
    path_a: Path,
    path_b: Path,
    tolerance_mm: float = 0.02,
    out_png: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Compare two part/assembly revisions (`path_a` vs `path_b`) using SolidCraft's native
    B-Rep surface comparison (`inspect.compare`, Pillar 3), coloring material only in A
    red (`#e03131`) and material only in B green (`#2f9e44`).
    """
    cli_path = find_solidcraft_cli()
    path_a = Path(path_a).resolve()
    path_b = Path(path_b).resolve()
    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-compare", "version": "1.0"}})
        _rpc("tools/call", {"name": "open", "arguments": {"path": str(path_a)}})
        _rpc("tools/call", {"name": "execute", "arguments": {"command": "file.insert_step" if path_b.suffix.lower() in (".step", ".stp") else "component.insert_design", "params": {"path": str(path_b)}}})
        r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
        bodies = [b.get("name") for b in (json.loads(r_insp["result"]["content"][0]["text"]).get("bodies") or []) if isinstance(b, dict) and b.get("name")]
        if len(bodies) < 2:
            return {"status": "error", "error": f"Need at least 2 bodies to compare; found {bodies}"}
        r_cmp = _rpc("tools/call", {
            "name": "execute",
            "arguments": {
                "command": "inspect.compare",
                "params": {"a": bodies[0], "b": bodies[-1], "tolerance": tolerance_mm, "color": True},
            },
        })
        cmp_data = json.loads(r_cmp["result"]["content"][0]["text"]) if ("result" in r_cmp and not r_cmp["result"].get("isError")) else {"raw": r_cmp}
        if out_png:
            out_png = Path(out_png).resolve()
            out_png.parent.mkdir(parents=True, exist_ok=True)
            _rpc("tools/call", {"name": "screenshot", "arguments": {"view": "iso", "width": 800, "height": 600, "path": str(out_png)}})
            cmp_data["diff_render_png"] = str(out_png)
        return cmp_data
    finally:
        proc.terminate()


def solidcraft_compare_picture(
    model_path: Path,
    reference_image: Path,
    view: str = "iso",
    fit: str = "width",
    out_overlay_png: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Run SolidCraft's agent-specific `inspect.compare_picture` command (Pillar 3):
    computes silhouette overlap (`0..1`) and 9-station millimeter edge offsets
    (`top`, `bottom`, `left`, `right`) telling the agent which edge to move and by how much.
    """
    import base64

    cli_path = find_solidcraft_cli()
    model_path = Path(model_path).resolve()
    reference_image = Path(reference_image).resolve()
    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-compare-pic", "version": "1.0"}})
        _rpc("tools/call", {"name": "open", "arguments": {"path": str(model_path)}})
        r_cp = _rpc("tools/call", {
            "name": "execute",
            "arguments": {
                "command": "inspect.compare_picture",
                "params": {"reference": str(reference_image), "view": view, "fit": fit, "overlay": bool(out_overlay_png)},
            },
        })
        res = json.loads(r_cp["result"]["content"][0]["text"])
        if out_overlay_png and res.get("overlay_png"):
            out_overlay_png = Path(out_overlay_png).resolve()
            out_overlay_png.parent.mkdir(parents=True, exist_ok=True)
            out_overlay_png.write_bytes(base64.b64decode(res.pop("overlay_png")))
            res["overlay_png_path"] = str(out_overlay_png)
        return res
    finally:
        proc.terminate()


def solidcraft_export_fabrication(
    sc_path: Path,
    project_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Export custom fabricated parts from a `.sc` assembly (Pillar 4):
    - Marks the bottom flat face of each custom 3D-printed body via `body.print_face`
      and exports `.stl` files via `file.save_mesh` into `<project_dir>/fabrication/prints/`.
    - Exports flat custom plates (`min_dim <= 8.5 mm`) as `.dxf` drawings via
      `file.export_view_dxf` into `<project_dir>/fabrication/laser/`.
    """
    cli_path = find_solidcraft_cli()
    sc_path = Path(sc_path).resolve()
    if project_dir is None:
        project_dir = sc_path.parent.parent if sc_path.parent.name == "cad" else sc_path.parent
    prints_dir = Path(project_dir) / "fabrication" / "prints"
    laser_dir = Path(project_dir) / "fabrication" / "laser"
    prints_dir.mkdir(parents=True, exist_ok=True)
    laser_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    exported_stl: List[str] = []
    exported_dxf: List[str] = []
    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-fab", "version": "1.0"}})
        _rpc("tools/call", {"name": "open", "arguments": {"path": str(sc_path)}})
        r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
        insp = json.loads(r_insp["result"]["content"][0]["text"])
        for b in insp.get("bodies") or []:
            bname = str(b.get("name") or "")
            bb = b.get("bbox")
            if not bname or not bb or "cots" in bname.lower() or "belt" in bname.lower():
                continue
            bmin = bb.get("min") if isinstance(bb, dict) else bb[0]
            bmax = bb.get("max") if isinstance(bb, dict) else bb[1]
            dims = [
                abs(float(bmax[0]) - float(bmin[0])),
                abs(float(bmax[1]) - float(bmin[1])),
                abs(float(bmax[2]) - float(bmin[2])),
            ]
            cx = 0.5 * (float(bmin[0]) + float(bmax[0]))
            cy = 0.5 * (float(bmin[1]) + float(bmax[1]))
            z_min = float(bmin[2])
            _rpc("tools/call", {
                "name": "execute",
                "arguments": {"command": "body.print_face", "params": {"body": bname, "point": [round(cx, 3), round(cy, 3), round(z_min, 3)]}},
            })
            stl_out = prints_dir / f"{bname}.stl"
            r_mesh = _rpc("tools/call", {
                "name": "execute",
                "arguments": {"command": "file.save_mesh", "params": {"path": str(stl_out), "bodies": [bname], "quality": "fine"}},
            })
            if stl_out.is_file():
                exported_stl.append(str(stl_out))
            if min(dims) <= 8.5 and max(dims) >= 25.0:
                dxf_out = laser_dir / f"{bname}.dxf"
                _rpc("tools/call", {
                    "name": "execute",
                    "arguments": {"command": "file.export", "params": {"path": str(dxf_out), "bodies": [bname]}},
                })
                if dxf_out.is_file():
                    exported_dxf.append(str(dxf_out))
        return {
            "status": "ok",
            "sc_path": str(sc_path),
            "prints_exported": exported_stl,
            "laser_exported": exported_dxf,
        }
    finally:
        proc.terminate()


def query_live_solidcraft_state(connect_port: int = 27878) -> Dict[str, Any]:
    """Query the active design name, occurrences, and joint origins from live SolidCraft (`127.0.0.1:27878`) via 1-ms TCP control RPC."""
    try:
        from tools.solidcraft_plugin import send_solidcraft_control_rpc

        r_c = send_solidcraft_control_rpc(
            "engine.execute",
            {"command": "component.list", "params": {"compact": True}},
            port=connect_port,
            timeout=1.5,
        )
        if not r_c.get("ok"):
            return {"connected": False, "error": r_c.get("error", "offline"), "occurrences": [], "origins": []}
        r_j = send_solidcraft_control_rpc(
            "engine.execute",
            {"command": "joint.list", "params": {}},
            port=connect_port,
            timeout=1.5,
        )
        c_data = r_c.get("result") if isinstance(r_c.get("result"), dict) else {}
        j_data = r_j.get("result") if isinstance(r_j.get("result"), dict) else {}

        occurrences: List[str] = []
        design_name = "Untitled"
        for comp in c_data.get("components") or []:
            if comp.get("id") == 0:
                design_name = comp.get("name") or "Untitled"
            for occ in comp.get("occurrences") or []:
                if occ.get("name"):
                    occurrences.append(str(occ["name"]))

        origins = [o.get("name") for o in (j_data.get("origins") or []) if isinstance(o, dict) and o.get("name")]
        return {
            "connected": True,
            "design_name": design_name,
            "occurrences": occurrences,
            "origins": origins,
            "joint_count": len(j_data.get("joints") or []),
        }
    except Exception as exc:
        return {"connected": False, "error": str(exc), "occurrences": [], "origins": []}


def generate_visual_catalog_and_dock(db_path: Path = DB_PATH) -> Dict[str, Any]:
    """
    Regenerate `cots/solidcraft/CATALOG.md` and `cots/solidcraft/index.html` with:
    - 3D isometric thumbnail previews (`thumbs/<part_id>.png`)
    - Enriched `<SKU>_<Title>.sc` paths
    - Dual-mode UI: Full Catalog Browser AND Compact Floating SolidCraft COTS Dock (`?dock=1`)
      with live occurrence selection, anchor frame picker, and 1-click Insert & Mate.
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

    items: List[Dict[str, Any]] = []
    by_vendor_cat: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

    for r in rows:
        pid = str(r["part_id"])
        vendor = str(r["vendor"] or "unknown")
        cat = str(r["category"] or "misc")
        sku = str(r["sku"] or "")
        title = str(r["title"] or sku or pid)
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

        thumb_name = safe_thumb_filename(pid)
        entry = {
            "id": pid,
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
            "th": f"thumbs/{thumb_name}",
        }
        items.append(entry)
        by_vendor_cat.setdefault(vendor, {}).setdefault(cat, []).append(entry)

    # Write CATALOG.md
    md_lines = [
        "# SolidCraft Robotics COTS Library Catalog",
        "",
        f"Total pre-built, coordinate-normalized `.sc` components: **{len(items):,}**",
        "",
        "## Quick Usage (Human, Floating Dock & CLI)",
        "",
        "- **Launch Floating SolidCraft COTS Dock (Companion Palette):**",
        "  ```powershell",
        "  .venv\\Scripts\\python.exe tools/solidcraft_cots.py dock",
        "  ```",
        "- **Interactive Full-Screen Visual Browser:**",
        "  ```powershell",
        "  .venv\\Scripts\\python.exe tools/solidcraft_cots.py browser",
        "  ```",
        "- **Compile a Project Assembly (`STEP` + `*_constraints.json` -> Jointed `.sc` + BOM + Motion GIF):**",
        "  ```powershell",
        "  .venv\\Scripts\\python.exe tools/cad_pipeline.py solidcraft-compile projects/cycloidal-actuator/cad/cycloidal_actuator_15to1.step --live",
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

    from tools.solidcraft_builder import list_parametric_templates
    templates_list = list_parametric_templates()
    templates_json = json.dumps(templates_list, separators=(",", ":"))
    data_json = json.dumps(items, separators=(",", ":"))
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SolidCraft COTS Dock &amp; Visual Catalog</title>
<style>
  :root {{
    --bg: #0b1120; --panel: #1e293b; --card: #152033; --border: #334155;
    --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --accent-hover: #0284c7;
    --ok: #22c55e; --warn: #f59e0b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--text);
  }}
  header {{
    position: sticky; top: 0; z-index: 20; background: rgba(11, 17, 32, 0.96);
    backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); padding: 12px 16px;
  }}
  .top-bar {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
  .title-group {{ display: flex; align-items: center; gap: 10px; }}
  h1 {{ margin: 0; font-size: 1.1rem; font-weight: 700; color: var(--accent); }}
  .status-pill {{
    font-size: 0.75rem; padding: 3px 10px; border-radius: 999px; font-weight: 600;
    background: #1e293b; border: 1px solid var(--border); color: var(--muted);
  }}
  .status-pill.live {{ border-color: var(--ok); color: var(--ok); background: rgba(34, 197, 94, 0.1); }}
  .mate-bar {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap: 8px;
    background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin-bottom: 10px;
  }}
  .mate-field {{ display: flex; flex-direction: column; gap: 3px; }}
  .mate-field label {{ font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  .controls {{ display: grid; grid-template-columns: 2fr 1fr 1fr 110px auto auto; gap: 8px; }}
  body.dock-mode .controls {{ grid-template-columns: 1fr 1fr; }}
  body.dock-mode .controls #qInput {{ grid-column: 1 / -1; }}
  input, select, button {{
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 7px 10px; font-size: 0.85rem;
  }}
  input:focus, select:focus {{ outline: 2px solid var(--accent); border-color: transparent; }}
  button {{ cursor: pointer; font-weight: 600; transition: all 0.15s; }}
  .btn-primary {{ background: var(--accent); color: #082f49; border: none; }}
  .btn-primary:hover {{ background: var(--accent-hover); color: #fff; }}
  .btn-ghost {{ background: transparent; }}
  .btn-ghost:hover {{ background: #334155; }}
  main {{ padding: 14px 16px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px;
  }}
  body.dock-mode .grid {{ grid-template-columns: 1fr; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    overflow: hidden; display: flex; flex-direction: column; justify-content: space-between;
    transition: border-color 0.15s, transform 0.15s;
  }}
  .card:hover {{ border-color: var(--accent); transform: translateY(-1px); }}
  body.dock-mode .card {{ flex-direction: row; align-items: center; padding: 6px; gap: 10px; }}
  .thumb-wrap {{
    background: #dbe4f0; height: 140px; display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden;
  }}
  body.dock-mode .thumb-wrap {{ width: 92px; height: 72px; border-radius: 6px; flex-shrink: 0; }}
  .thumb-wrap img {{ width: 100%; height: 100%; object-fit: contain; }}
  .bore-tag {{
    position: absolute; top: 6px; right: 6px; background: rgba(15, 23, 42, 0.85);
    color: #38bdf8; font-size: 0.72rem; font-weight: 700; padding: 2px 7px; border-radius: 999px;
    font-family: ui-monospace, monospace;
  }}
  .card-body {{ padding: 10px 12px; flex: 1; display: flex; flex-direction: column; gap: 5px; }}
  body.dock-mode .card-body {{ padding: 2px 4px; }}
  .badges {{ display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }}
  .badge {{
    display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem;
    background: #1e293b; color: #cbd5e1; font-family: ui-monospace, monospace;
  }}
  .sku {{ font-family: ui-monospace, monospace; color: var(--accent); font-weight: 700; font-size: 0.82rem; }}
  .title {{ font-size: 0.85rem; font-weight: 600; line-height: 1.3; color: #f1f5f9; }}
  .card-actions {{ padding: 8px 12px 10px; display: flex; gap: 6px; flex-wrap: wrap; border-top: 1px solid rgba(51,65,85,0.5); }}
  body.dock-mode .card-actions {{ border-top: none; padding: 0; flex-direction: column; min-width: 112px; }}
  .card-actions button {{ flex: 1; padding: 6px 8px; font-size: 0.78rem; }}
  .toast {{
    position: fixed; bottom: 16px; right: 16px; background: var(--ok); color: #052e16;
    padding: 10px 14px; border-radius: 8px; font-weight: 700; font-size: 0.85rem;
    display: none; z-index: 100; box-shadow: 0 8px 24px rgba(0,0,0,0.5);
  }}
  .mode-tabs {{
    display: flex; gap: 8px; margin-bottom: 10px;
  }}
  .tab-btn {{
    flex: 1; padding: 8px 12px; border-radius: 8px; font-size: 0.84rem; font-weight: 700;
    background: #1e293b; color: var(--muted); border: 1px solid var(--border);
  }}
  .tab-btn.active {{
    background: rgba(56, 189, 248, 0.16); color: var(--accent); border-color: var(--accent);
  }}
  .tpl-card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    overflow: hidden; display: flex; flex-direction: column; justify-content: space-between;
  }}
  .tpl-params {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(125px, 1fr)); gap: 6px;
    padding: 8px 12px; background: rgba(15, 23, 42, 0.55); border-top: 1px solid rgba(51,65,85,0.45);
  }}
  .tpl-param {{ display: flex; flex-direction: column; gap: 2px; }}
  .tpl-param label {{ font-size: 0.68rem; color: var(--muted); }}
  .tpl-param input {{ padding: 4px 7px; font-size: 0.8rem; font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
<header>
  <div class="top-bar">
    <div class="title-group">
      <h1>SolidCraft COTS &amp; Parametric Dock</h1>
      <span class="status-pill" id="liveStatus">Checking port 27878...</span>
    </div>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
      <span style="font-size:0.78rem; color:var(--muted);" id="statsLabel"></span>
      <button class="btn-ghost" id="useSelBtn" title="Read selected occurrence and 3D point from SolidCraft viewport">🎯 Pick Selection</button>
      <button class="btn-ghost" id="pullScriptBtn" title="Pull live GUI feature timeline (*_sc_script.json) and parameters (*_sc_params.json) back to Git">⬇ Pull GUI to Git</button>
      <button class="btn-ghost" id="snapToggleBtn" title="Snap HUD to SolidCraft window or detach as free window (Hotkey: Alt+C)">📌 Snapped (Alt+C)</button>
      <button class="btn-ghost" id="dockToggleBtn" title="Toggle compact dock layout">Compact Dock</button>
      <button class="btn-ghost" id="refreshLiveBtn" title="Refresh active SolidCraft occurrences">↻ Sync</button>
    </div>
  </div>
  <div class="mode-tabs">
    <button class="tab-btn active" id="tabCotsBtn">📦 COTS (2,588)</button>
    <button class="tab-btn" id="tabTplBtn">⚙️ Templates (6)</button>
    <button class="tab-btn" id="tabLiveBtn">🤖 Robotics Audit &amp; Fab</button>
    <button class="tab-btn" id="tabGitBtn">🔀 Git &amp; Files</button>
  </div>
  <div class="mate-bar">
    <div class="mate-field">
      <label>Target Occurrence (Optional Mate)</label>
      <select id="targetOccSelect"><option value="">— Just Insert at [0,0,0] —</option></select>
    </div>
    <div class="mate-field">
      <label>Joint Type</label>
      <select id="jointTypeSelect">
        <option value="rigid">Rigid (Fastened)</option>
        <option value="revolute">Revolute (Turn)</option>
        <option value="slider">Slider (Linear)</option>
        <option value="cylindrical">Cylindrical</option>
      </select>
    </div>
    <div class="mate-field">
      <label>COTS Anchor Frame</label>
      <select id="anchorSelect">
        <option value="">Auto (Recommended Anchor)</option>
        <option value="flange_seat">flange_seat (Bearings)</option>
        <option value="bore_center">bore_center (Gears/Hubs)</option>
        <option value="output_shaft">output_shaft (Motors/Servos)</option>
        <option value="sense_origin">sense_origin (Sensors)</option>
        <option value="bbox_center">bbox_center</option>
      </select>
    </div>
    <div class="mate-field">
      <label>Target Point X Y Z (mm)</label>
      <input type="text" id="targetPtInput" value="0 0 0" placeholder="e.g. 24 0 2">
    </div>
  </div>
  <div class="controls" id="cotsControls">
    <input type="search" id="qInput" placeholder="Search 2,588 parts by keyword, SKU (1611-0514-0008, 5203, REV-49-1559)..." autofocus>
    <select id="vendorSelect"><option value="">All Vendors</option></select>
    <select id="catSelect"><option value="">All Categories</option></select>
    <input type="number" id="boreInput" placeholder="Bore Ø mm" step="0.5" min="0">
    <button class="btn-ghost" id="holesBtn" title="Filter to parts with mounting holes">Mount Holes</button>
    <button class="btn-ghost" id="resetBtn">Reset</button>
  </div>
</header>
<main>
  <div class="grid" id="cardsGrid"></div>
  <div class="grid" id="templatesGrid" style="display:none;"></div>
  <div id="liveView" style="display:none;"></div>
  <div id="gitView" style="display:none;"></div>
</main>
<div class="toast" id="toast"></div>
<script>
const PARTS = {data_json};
const TEMPLATES = {templates_json};
const params = new URLSearchParams(window.location.search);
if (params.get('dock') === '1') {{
  document.body.classList.add('dock-mode');
}}

const qInput = document.getElementById('qInput');
const vendorSelect = document.getElementById('vendorSelect');
const catSelect = document.getElementById('catSelect');
const boreInput = document.getElementById('boreInput');
const holesBtn = document.getElementById('holesBtn');
const cardsGrid = document.getElementById('cardsGrid');
const statsLabel = document.getElementById('statsLabel');
const toast = document.getElementById('toast');
const liveStatus = document.getElementById('liveStatus');
const targetOccSelect = document.getElementById('targetOccSelect');
const jointTypeSelect = document.getElementById('jointTypeSelect');
const anchorSelect = document.getElementById('anchorSelect');
const targetPtInput = document.getElementById('targetPtInput');

let filterHolesOnly = false;

const vendors = [...new Set(PARTS.map(p => p.v))].sort();
vendors.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; vendorSelect.appendChild(o); }});
const cats = [...new Set(PARTS.map(p => p.c))].sort();
cats.forEach(c => {{ const o = document.createElement('option'); o.value = c; o.textContent = c; catSelect.appendChild(o); }});

function showToast(msg) {{
  toast.textContent = msg;
  toast.style.display = 'block';
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => {{ toast.style.display = 'none'; }}, 2600);
}}

async function copyText(txt, label) {{
  await navigator.clipboard.writeText(txt);
  showToast('Copied ' + label + '!');
}}

async function syncLiveState() {{
  try {{
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error('offline');
    const st = await res.json();
    if (st.connected) {{
      liveStatus.textContent = `● Live: ${{st.design_name}} (${{st.occurrences.length}} parts)`;
      liveStatus.classList.add('live');
      const cur = targetOccSelect.value;
      targetOccSelect.innerHTML = '<option value="">— Just Insert at Target Point —</option>' +
        st.occurrences.map(o => `<option value="${{o}}">${{o}}</option>`).join('');
      if (st.occurrences.includes(cur)) targetOccSelect.value = cur;
    }} else {{
      liveStatus.textContent = '○ SolidCraft Port 27878 Offline';
      liveStatus.classList.remove('live');
    }}
  }} catch (e) {{
    liveStatus.textContent = 'Static File Mode (Run `solidcraft_cots.py dock` for live 1-click insert)';
    liveStatus.classList.remove('live');
  }}
}}

async function insertPart(key) {{
  const occ = targetOccSelect.value || null;
  const jtype = occ ? jointTypeSelect.value : null;
  const anc = anchorSelect.value || null;
  const ptParts = targetPtInput.value.trim().split(/[\\s,]+/).map(Number);
  const pt = (ptParts.length === 3 && ptParts.every(n => !isNaN(n))) ? ptParts : [0, 0, 0];

  try {{
    const res = await fetch('/api/insert', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        key,
        target_occurrence: occ,
        joint_type: jtype,
        anchor: anc,
        target_point: pt,
        translate: occ ? null : pt
      }})
    }});
    if (!res.ok) throw new Error('Server offline');
    const data = await res.json();
    if (data.status === 'ok') {{
      const mateMsg = data.joint_created ? ` + mated (${{data.joint_created}})` : '';
      showToast(`Inserted ${{data.component_name}} (occ #${{data.occurrence_id}})${{mateMsg}}`);
      syncLiveState();
    }} else {{
      alert('Insert error: ' + (data.error || JSON.stringify(data)));
    }}
  }} catch (e) {{
    const cmd = `.venv\\\\Scripts\\\\python.exe tools/solidcraft_cots.py insert "${{key}}"`;
    await navigator.clipboard.writeText(cmd);
    showToast('Copied CLI insert command! (Start `python tools/solidcraft_cots.py dock` for live GUI insert)');
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
    if (filterHolesOnly && !p.holes) return false;
    if (!isNaN(bVal) && (p.bore === null || Math.abs(p.bore - bVal) > 0.35)) return false;
    if (tokens.length) {{
      const hay = (p.v + ' ' + p.c + ' ' + p.sku + ' ' + p.t + ' ' + p.mat + ' ' + p.rel).toLowerCase();
      for (const tok of tokens) {{
        if (!hay.includes(tok)) return false;
      }}
    }}
    return true;
  }});

  const shown = filtered.slice(0, 120);
  statsLabel.textContent = `${{shown.length}} of ${{filtered.length.toLocaleString()}} parts`;

  cardsGrid.innerHTML = shown.map(p => {{
    const key = (p.sku || p.id).replace(/'/g, "\\\\'");
    const absPath = p.abs.replace(/'/g, "\\\\'");
    return `<div class="card">
      <div class="thumb-wrap">
        <img src="${{p.th}}" loading="lazy" alt="${{p.t}}" onerror="this.style.display='none'">
        ${{p.bore !== null ? `<span class="bore-tag">Ø ${{p.bore}}mm</span>` : ''}}
      </div>
      <div class="card-body">
        <div class="badges">
          <span class="sku">${{p.sku || p.id}}</span>
          <span class="badge">${{p.v}}/${{p.c}}</span>
        </div>
        <div class="title">${{p.t}}</div>
        <div class="badges">
          <span class="badge">${{p.mat}}</span>
          <span class="badge">@${{p.anc}}</span>
          ${{p.dims ? `<span class="badge">${{p.dims}} mm</span>` : ''}}
        </div>
      </div>
      <div class="card-actions">
        <button class="btn-primary" onclick="insertPart('${{key}}')">+ Insert</button>
        <button class="btn-ghost" onclick="copyText('${{absPath}}', '.sc path')">Copy Path</button>
      </div>
    </div>`;
  }}).join('');
}}

qInput.addEventListener('input', render);
vendorSelect.addEventListener('change', render);
catSelect.addEventListener('change', render);
boreInput.addEventListener('input', render);
holesBtn.addEventListener('click', () => {{
  filterHolesOnly = !filterHolesOnly;
  holesBtn.style.borderColor = filterHolesOnly ? 'var(--accent)' : 'var(--border)';
  render();
}});
document.getElementById('resetBtn').addEventListener('click', () => {{
  qInput.value = ''; vendorSelect.value = ''; catSelect.value = ''; boreInput.value = '';
  filterHolesOnly = false; holesBtn.style.borderColor = 'var(--border)'; render();
}});
document.getElementById('dockToggleBtn').addEventListener('click', () => {{
  document.body.classList.toggle('dock-mode');
}});
document.getElementById('openPluginsBtn').addEventListener('click', async () => {{
  try {{
    await fetch('/api/hud/open-plugins-prefs', {{ method: 'POST' }});
    showToast('Opened SolidCraft Preferences -> Plugins!');
  }} catch (e) {{}}
}});
document.getElementById('useSelBtn').addEventListener('click', async () => {{
  try {{
    const res = await fetch('/api/hud/selection');
    const d = await res.json();
    const sel = (d.result && d.result.selection) || [];
    const hov = d.result && d.result.hover;
    if (sel.length > 0 && sel[0].occurrence) {{
      targetOccSelect.value = sel[0].occurrence;
    }}
    const pt = (sel[0] && sel[0].point) || (hov && hov.point);
    if (Array.isArray(pt) && pt.length === 3) {{
      targetPtInput.value = pt.map(n => Number(n).toFixed(2)).join(' ');
    }}
    showToast(sel.length ? `Synced viewport selection (${{sel.length}} item)` : 'Synced viewport hover point');
  }} catch (e) {{}}
}});
document.getElementById('snapToggleBtn').addEventListener('click', async () => {{
  try {{
    const res = await fetch('/api/hud/toggle-snap', {{ method: 'POST' }});
    const d = await res.json();
    const btn = document.getElementById('snapToggleBtn');
    btn.textContent = d.snap_to_solidcraft ? '📌 Snapped (Alt+C)' : '🔓 Free Window';
    showToast(d.snap_to_solidcraft ? 'HUD snapped to SolidCraft window' : 'HUD detached (free-moving window)');
  }} catch (e) {{}}
}});
document.getElementById('refreshLiveBtn').addEventListener('click', syncLiveState);

const tabCotsBtn = document.getElementById('tabCotsBtn');
const tabTplBtn = document.getElementById('tabTplBtn');
const tabLiveBtn = document.getElementById('tabLiveBtn');
const tabGitBtn = document.getElementById('tabGitBtn');
const cotsControls = document.getElementById('cotsControls');
const templatesGrid = document.getElementById('templatesGrid');
const liveView = document.getElementById('liveView');
const gitView = document.getElementById('gitView');

function collectTemplateParams(tid) {{
  const tpl = TEMPLATES.find(t => t.id === tid);
  const out = {{}};
  if (!tpl) return out;
  tpl.parameters.forEach(p => {{
    const el = document.getElementById(`tpl_${{tid}}_${{p.name}}`);
    if (el) out[p.name] = parseFloat(el.value);
  }});
  return out;
}}

async function buildTemplate(tid, insertLive) {{
  const paramsOverride = collectTemplateParams(tid);
  const nameEl = document.getElementById(`tpl_${{tid}}_partname`);
  const partName = (nameEl && nameEl.value.trim()) ? nameEl.value.trim() : tid;
  const occ = targetOccSelect.value || null;
  const jtype = occ ? jointTypeSelect.value : null;
  const anc = anchorSelect.value || null;
  const ptParts = targetPtInput.value.trim().split(/[\s,]+/).map(Number);
  const pt = (ptParts.length === 3 && ptParts.every(n => !isNaN(n))) ? ptParts : [0, 0, 0];

  showToast(`Building parametric ${{partName}}.sc + running sweep...`);
  try {{
    const res = await fetch('/api/template/build', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        template_id: tid,
        params_override: paramsOverride,
        part_name: partName,
        insert_live: insertLive,
        target_occurrence: occ,
        joint_type: jtype,
        anchor: anc,
        target_point: pt
      }})
    }});
    const data = await res.json();
    if (data.status === 'PASS') {{
      const img = document.getElementById(`tpl_img_${{tid}}`);
      if (img) img.src = `thumbs/tpl_${{tid}}.png?t=${{Date.now()}}`;
      const swCount = (data.parameter_sweeps || []).length;
      const insMsg = (data.live_insert && data.live_insert.status === 'ok')
        ? ` + Inserted ${{data.live_insert.occurrence_label}}`
        : '';
      showToast(`PASS (${{data.build_ms}} ms, ${{swCount}} sweep checks OK)${{insMsg}}`);
      if (insertLive) syncLiveState();
    }} else {{
      alert('Template build error: ' + (data.error || JSON.stringify(data)));
    }}
  }} catch (e) {{
    alert('Template build failed: ' + e);
  }}
}}

async function copyTemplateSnippet(tid) {{
  const p = collectTemplateParams(tid);
  const nameEl = document.getElementById(`tpl_${{tid}}_partname`);
  const partName = (nameEl && nameEl.value.trim()) ? nameEl.value.trim() : tid;
  const code = `from tools.solidcraft_builder import build_template_part\nfrom tools.ag_cad_helpers import attach_native_solidcraft_part\n\ntpl = build_template_part(\n    "${{tid}}",\n    params_override=${{JSON.stringify(p)}},\n    out_sc_path="cad/solidcraft/${{partName}}.sc",\n    export_step_path="cad/solidcraft/${{partName}}.step",\n    part_name="${{partName}}",\n)`;
  await copyText(code, 'Python builder snippet');
}}

function renderTemplates() {{
  statsLabel.textContent = `${{TEMPLATES.length}} parametric templates`;
  templatesGrid.innerHTML = TEMPLATES.map(t => {{
    const paramInputs = t.parameters.map(p => `
      <div class="tpl-param">
        <label>${{p.label}} (${{p.unit}})</label>
        <input type="number" id="tpl_${{t.id}}_${{p.name}}" value="${{p.default}}" min="${{p.min}}" max="${{p.max}}" step="${{p.step}}">
      </div>
    `).join('');
    const anchors = (t.anchors || []).map(a => `<span class="badge">@${{a}}</span>`).join('');
    return `<div class="tpl-card">
      <div class="thumb-wrap" style="height:150px;">
        <img id="tpl_img_${{t.id}}" src="${{t.thumb}}" alt="${{t.title}}" onerror="this.style.display='none'">
        <span class="bore-tag">${{t.category}}</span>
      </div>
      <div class="card-body">
        <div class="badges">
          <span class="sku">${{t.id}}</span>
          <span class="badge">${{t.material}}</span>
        </div>
        <div class="title">${{t.title}}</div>
        <div style="font-size:0.76rem; color:var(--muted); line-height:1.35;">${{t.description}}</div>
        <div class="badges">${{anchors}}</div>
      </div>
      <div class="tpl-params">
        <div class="tpl-param" style="grid-column: 1 / -1;">
          <label>Custom Part Name (.sc)</label>
          <input type="text" id="tpl_${{t.id}}_partname" value="${{t.id}}">
        </div>
        ${{paramInputs}}
      </div>
      <div class="card-actions">
        <button class="btn-primary" onclick="buildTemplate('${{t.id}}', true)">⚡ Build &amp; Insert</button>
        <button class="btn-ghost" onclick="buildTemplate('${{t.id}}', false)">💾 Build .sc</button>
        <button class="btn-ghost" onclick="copyTemplateSnippet('${{t.id}}')">📋 Copy Python</button>
      </div>
    </div>`;
  }}).join('');
}}

function setActiveTab(which) {{
  [tabCotsBtn, tabTplBtn, tabLiveBtn, tabGitBtn].forEach(b => b && b.classList.remove('active'));
  cotsControls.style.display = (which === 'cots') ? '' : 'none';
  cardsGrid.style.display = (which === 'cots') ? '' : 'none';
  templatesGrid.style.display = (which === 'tpl') ? '' : 'none';
  liveView.style.display = (which === 'live') ? '' : 'none';
  gitView.style.display = (which === 'git') ? '' : 'none';
  if (which === 'cots') {{ tabCotsBtn.classList.add('active'); render(); }}
  else if (which === 'tpl') {{ tabTplBtn.classList.add('active'); renderTemplates(); }}
  else if (which === 'live') {{ tabLiveBtn.classList.add('active'); loadLiveInspector(); }}
  else if (which === 'git') {{ tabGitBtn.classList.add('active'); loadGitStatus(); }}
}}

tabCotsBtn.addEventListener('click', () => setActiveTab('cots'));
tabTplBtn.addEventListener('click', () => setActiveTab('tpl'));
tabLiveBtn.addEventListener('click', () => setActiveTab('live'));
tabGitBtn.addEventListener('click', () => setActiveTab('git'));

let lastFocusPoint = [0, 0, 0];

async function focusLivePoint(pt, sectionPlane, clearSection, viewPreset) {{
  if (Array.isArray(pt) && pt.length === 3) lastFocusPoint = pt;
  try {{
    const res = await fetch('/api/live/focus', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        target_xyz: pt || null,
        section_plane: sectionPlane || null,
        clear_section: !!clearSection,
        view_preset: viewPreset || null
      }})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      if (clearSection) showToast('Cleared section cut');
      else if (sectionPlane && pt) showToast(`Focused [${{pt.join(', ')}}] + ${{sectionPlane}} section cut`);
      else if (pt) showToast(`Focused camera on [${{pt.join(', ')}}] mm`);
      else if (viewPreset) showToast(`Applied ${{viewPreset}} view`);
    }}
  }} catch (e) {{
    alert('Viewport focus failed: ' + e);
  }}
}}

async function runLiveClearance() {{
  const box = document.getElementById('liveClearanceResults');
  const thrEl = document.getElementById('liveClearanceThr');
  const custEl = document.getElementById('liveClearanceCustom');
  const thr = thrEl ? parseFloat(thrEl.value || '1.0') : 1.0;
  const cust = custEl ? custEl.checked : true;
  if (box) box.innerHTML = '<div style="padding:10px; color:var(--accent); font-size:0.78rem;">Running parallel B-Rep clearance &amp; interference audit in live SolidCraft...</div>';
  try {{
    const res = await fetch('/api/live/clearance', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ clearance_mm: thr, custom_only: cust }})
    }});
    const d = await res.json();
    if (d.status === 'error') {{
      if (box) box.innerHTML = `<div style="color:#f87171; font-size:0.78rem;">Clearance audit error: ${{d.error || 'failed'}}</div>`;
      return;
    }}
    const fails = (d.collision_failures || []).map(x => ({{ ...x, _cls: 'COLLISION_FAIL', _type: 'overlap' }}));
    const warns = (d.near_warnings || []).map(x => ({{ ...x, _cls: 'NEAR_CLEARANCE_WARN', _type: 'gap' }}));
    const exp = (d.expected_interfaces || []).slice(0, 12).map(x => ({{ ...x, _cls: 'EXPECTED_INTERFACE', _type: x.volume_mm3 ? 'overlap' : 'gap' }}));
    const allItems = [...fails, ...warns, ...exp];
    const rows = allItems.map(it => {{
      const pt = it.focus_point_mm || [0, 0, 0];
      const ptJs = `[${{pt[0]}}, ${{pt[1]}}, ${{pt[2]}}]`;
      const isFail = it._cls === 'COLLISION_FAIL';
      const isWarn = it._cls === 'NEAR_CLEARANCE_WARN';
      const badgeColor = isFail ? '#f87171' : (isWarn ? '#fbbf24' : '#38bdf8');
      const label = it._type === 'overlap'
        ? `OVERLAP (${{it.volume_mm3}} mm³)`
        : `GAP ${{it.distance_mm}} mm`;
      return `<div style="padding:6px 0; border-bottom:1px solid rgba(51,65,85,0.35); font-size:0.75rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:6px; flex-wrap:wrap;">
          <div>
            <span class="badge" style="color:${{badgeColor}}; border:1px solid ${{badgeColor}};">${{it._cls}}</span>
            <strong style="margin-left:6px;">${{it.body_a}} ↔ ${{it.body_b}}</strong>
            <span style="color:var(--muted); margin-left:6px;">${{label}} @ [${{pt.join(', ')}}] mm</span>
          </div>
          <div style="display:flex; gap:4px; flex-shrink:0;">
            <button class="btn-primary" style="padding:2px 7px; font-size:0.71rem;" onclick="focusLivePoint(${{ptJs}}, null, false, null)">🎯 Zoom</button>
            <button class="btn-ghost" style="padding:2px 6px; font-size:0.71rem;" onclick="focusLivePoint(${{ptJs}}, 'XZ', false, null)">🔪 XZ</button>
            <button class="btn-ghost" style="padding:2px 6px; font-size:0.71rem;" onclick="focusLivePoint(${{ptJs}}, 'YZ', false, null)">🔪 YZ</button>
            <button class="btn-ghost" style="padding:2px 6px; font-size:0.71rem;" onclick="focusLivePoint(${{ptJs}}, 'XY', false, null)">🔪 XY</button>
          </div>
        </div>
      </div>`;
    }}).join('');
    if (box) {{
      box.innerHTML = `
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin:6px 0;">
          <span class="badge" style="color:#f87171; border:1px solid #f87171;">❌ Collisions: ${{fails.length}}</span>
          <span class="badge" style="color:#fbbf24; border:1px solid #fbbf24;">⚠️ Tight (&lt;${{thr}}mm): ${{warns.length}}</span>
          <span class="badge" style="color:#38bdf8;">🔩 Expected Interfaces: ${{(d.expected_interfaces || []).length}}</span>
          <span class="badge">${{d.audit_wall_ms || 0}} ms (${{d.bodies_checked || 0}} bodies)</span>
        </div>
        <div style="max-height:240px; overflow-y:auto; padding-right:4px;">
          ${{rows || '<div style="color:#4ade80; padding:6px 0;">✅ Zero unexpected collisions or tight clearances!</div>'}}
        </div>
      `;
    }}
  }} catch (e) {{
    if (box) box.innerHTML = `<div style="color:#f87171; font-size:0.78rem;">Audit failed: ${{e}}</div>`;
  }}
}}

async function driveLiveJoint(jid, val, labelId) {{
  const numVal = parseFloat(val);
  const lbl = document.getElementById(labelId);
  if (lbl) lbl.textContent = numVal.toFixed(1);
  try {{
    const res = await fetch('/api/live/joint/drive', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ joint: jid, value: numVal }})
    }});
    const d = await res.json();
    if (d.redirected_to_driver && lbl) {{
      lbl.textContent = `${{numVal.toFixed(1)}} (via ${{d.redirected_to_driver}})`;
    }}
  }} catch (e) {{}}
}}

async function sweepLiveJoint(jid, minV, maxV, slotId) {{
  const box = document.getElementById(slotId);
  if (box) box.innerHTML = '<span style="color:var(--accent); font-size:0.74rem;">Sweeping joint &amp; checking dynamic collisions...</span>';
  try {{
    const res = await fetch('/api/live/joint/sweep', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ joint: jid, from_val: minV, to_val: maxV, steps: 24, auto_focus_on_contact: true }})
    }});
    const d = await res.json();
    if (d.status === 'ok' && box) {{
      if (d.stopped_by) {{
        const pt = d.stopped_by.at || [0, 0, 0];
        const ptJs = `[${{pt[0]}}, ${{pt[1]}}, ${{pt[2]}}]`;
        box.innerHTML = `<span style="color:#f87171; font-size:0.74rem;">🛑 Contact at ${{d.stopped_at}}: ${{d.stopped_by.body_a || ''}} ↔ ${{d.stopped_by.body_b || ''}}</span>
          <button class="btn-primary" style="padding:1px 6px; font-size:0.7rem; margin-left:6px;" onclick="focusLivePoint(${{ptJs}}, 'XZ', false, null)">🎯 Focus Contact</button>`;
      }} else {{
        const mg = d.min_gap && typeof d.min_gap.distance === 'number' ? ` | Min Gap: ${{d.min_gap.distance.toFixed(2)}} mm` : '';
        box.innerHTML = `<span style="color:#4ade80; font-size:0.74rem;">✅ Full travel [${{minV}} → ${{maxV}}] clear (${{d.samples_count || 0}} steps)${{mg}}</span>`;
      }}
    }} else if (box) {{
      box.innerHTML = `<span style="color:#f87171; font-size:0.74rem;">${{d.error || 'Sweep error'}}</span>`;
    }}
  }} catch (e) {{
    if (box) box.innerHTML = `<span style="color:#f87171; font-size:0.74rem;">${{e}}</span>`;
  }}
}}

async function changeLiveParam(pname, inputId, runSweep, slotId) {{
  const el = document.getElementById(inputId);
  if (!el) return;
  const expr = el.value.trim();
  const box = document.getElementById(slotId);
  if (box) box.innerHTML = '<span style="color:var(--accent); font-size:0.73rem;">Updating parameter...</span>';
  try {{
    const res = await fetch('/api/live/param/change', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ name: pname, expression: expr, run_sweep: !!runSweep }})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      showToast(`Updated ${{pname}} = ${{expr}}`);
      if (box) {{
        const sw = d.sweep_summary ? ' | ✅ ±15% Sweep Complete' : '';
        box.innerHTML = `<span style="color:#4ade80; font-size:0.73rem;">Updated (${{expr}})${{sw}}</span>`;
      }}
    }} else if (box) {{
      box.innerHTML = `<span style="color:#f87171; font-size:0.73rem;">${{d.error || 'Failed'}}</span>`;
    }}
  }} catch (e) {{
    if (box) box.innerHTML = `<span style="color:#f87171; font-size:0.73rem;">${{e}}</span>`;
  }}
}}

async function syncAndPullLive(updateLinks, saveDesign, pullGit, openScPath) {{
  showToast(openScPath ? `Opening ${{openScPath}} in live SolidCraft...` : 'Syncing live SolidCraft design & pulling to Git...');
  try {{
    const res = await fetch('/api/live/sync', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        update_links: !!updateLinks,
        save_design: !!saveDesign,
        pull_to_git_json: !!pullGit,
        open_sc_path: openScPath || null
      }})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      showToast('Live SolidCraft design synced!');
      loadLiveInspector();
      syncLiveState();
    }} else {{
      alert('Sync failed: ' + JSON.stringify(d));
    }}
  }} catch (e) {{
    alert('Sync error: ' + e);
  }}
}}

async function exportLiveFab() {{
  const box = document.getElementById('liveFabResults');
  if (box) box.innerHTML = '<div style="padding:8px; color:var(--accent); font-size:0.76rem;">Exporting fine-mesh 3D print STLs, laser DXFs &amp; BOM Markdown from live assembly...</div>';
  showToast('Exporting project fabrication pack (STL + DXF + BOM)...');
  try {{
    const res = await fetch('/api/live/fab', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{}})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      showToast(`Exported ${{d.prints_count}} STLs, ${{d.laser_count}} DXFs & BOM (${{d.elapsed_ms}} ms)!`);
      if (box) {{
        box.innerHTML = `<div style="margin-top:8px; padding:8px; background:#0f172a; border:1px solid #4ade80; border-radius:8px; font-size:0.75rem;">
          <div style="color:#4ade80; font-weight:700; margin-bottom:4px;">✅ Fabrication Pack Exported (${{d.elapsed_ms}} ms)</div>
          <div><b>Project:</b> ${{d.project}} | <b>3D Prints (.stl):</b> ${{d.prints_count}} | <b>Laser Plates (.dxf):</b> ${{d.laser_count}}</div>
          <div style="color:var(--muted); margin-top:2px;"><b>BOM Markdown:</b> ${{d.bom_markdown}} (${{d.total_bom_components}} unique components)</div>
        </div>`;
      }}
    }} else if (box) {{
      box.innerHTML = `<div style="color:#f87171; font-size:0.75rem;">Fab export error: ${{d.error || 'failed'}}</div>`;
    }}
  }} catch (e) {{
    if (box) box.innerHTML = `<div style="color:#f87171; font-size:0.75rem;">Fab export failed: ${{e}}</div>`;
  }}
}}

async function loadLiveInspector() {{
  statsLabel.textContent = 'Inspecting live SolidCraft scene...';
  try {{
    const res = await fetch('/api/live/state');
    const st = await res.json();
    if (!st.connected) {{
      liveView.innerHTML = `<div class="tpl-card"><div class="card-body"><div class="title" style="color:#f87171;">SolidCraft Live GUI Not Connected</div><div style="font-size:0.8rem; color:var(--muted);">${{st.error || 'Start SolidCraft with --control 27878'}}</div></div></div>`;
      return;
    }}
    const mp = st.mass_properties || {{}};
    const massG = mp.total_mass_g !== null && mp.total_mass_g !== undefined ? mp.total_mass_g : 'n/a';
    const comMm = mp.center_of_mass_mm || [0, 0, 0];
    const allJoints = st.joints || [];
    const drivenJoints = allJoints.filter(j => j.is_driven);

    statsLabel.textContent = `${{st.body_count}} bodies | ${{st.occurrence_count}} occs | ${{drivenJoints.length}} driven joints | ${{massG}} g`;

    const scOptions = (st.project_sc_files || []).map(f => {{
      const sel = (st.design_path && st.design_path.replace(/\\\\/g, '/').endsWith(f.rel_path)) ? 'selected' : '';
      return `<option value="${{f.path}}" ${{sel}}>${{f.project}} — ${{f.name}}</option>`;
    }}).join('');

    const jointRows = drivenJoints.map((j, idx) => {{
      const jIdStr = String(j.name || j.id);
      const jEsc = jIdStr.replace(/'/g, "\\'");
      const lblId = `j_val_${{idx}}`;
      const swSlot = `j_sw_${{idx}}`;
      const pt = j.origin_mm || [0, 0, 0];
      const ptJs = `[${{pt[0]}}, ${{pt[1]}}, ${{pt[2]}}]`;
      const occPair = (j.occurrences || []).join(' → ');
      const linkBadge = j.follows_joint
        ? `<span class="badge" style="color:#38bdf8;">↪ Follows ${{j.follows_joint}} (${{j.link_ratio}}x)</span>`
        : '';
      return `<div style="padding:7px 0; border-bottom:1px solid rgba(51,65,85,0.35); font-size:0.76rem;">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:6px; flex-wrap:wrap;">
          <div>
            <span class="sku">${{j.name}}</span>
            <span class="badge" style="margin-left:4px;">${{j.type}} (${{j.unit}})</span>
            <span style="color:var(--muted); margin-left:6px;">${{occPair}}</span>
            ${{linkBadge}}
          </div>
          <div style="display:flex; gap:4px; align-items:center;">
            <button class="btn-ghost" style="padding:2px 6px; font-size:0.7rem;" onclick="focusLivePoint(${{ptJs}}, null, false, null)">🎯</button>
            <button class="btn-ghost" style="padding:2px 6px; font-size:0.7rem;" onclick="document.getElementById('j_rng_${{idx}}').value=0; driveLiveJoint('${{jEsc}}', 0, '${{lblId}}')">0°</button>
            <button class="btn-primary" style="padding:2px 7px; font-size:0.7rem;" onclick="sweepLiveJoint('${{jEsc}}', ${{j.min_limit}}, ${{j.max_limit}}, '${{swSlot}}')">⚡ Sweep &amp; Auto-Park</button>
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px; margin-top:5px;">
          <span style="font-size:0.7rem; color:var(--muted); width:36px;">${{j.min_limit}}</span>
          <input type="range" id="j_rng_${{idx}}" min="${{j.min_limit}}" max="${{j.max_limit}}" step="${{j.type === 'slider' ? 0.5 : 2}}" value="${{j.value}}" style="flex:1;" oninput="driveLiveJoint('${{jEsc}}', this.value, '${{lblId}}')">
          <span id="${{lblId}}" style="font-family:ui-monospace,monospace; font-size:0.75rem; color:var(--accent); width:54px; text-align:right;">${{Number(j.value).toFixed(1)}}</span>
        </div>
        <div id="${{swSlot}}" style="margin-top:3px;"></div>
      </div>`;
    }}).join('');

    liveView.innerHTML = `
      <!-- Top Bar: Active Project Switcher, Bidirectional Git Sync & 1-Click Fabrication Pack -->
      <div class="tpl-card" style="margin-bottom:12px;">
        <div class="card-body">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <div>
              <span class="sku">${{st.design_name || 'Untitled'}}</span>
              <span class="badge" style="margin-left:6px;">${{st.body_count}} Bodies</span>
              <span class="badge">${{st.occurrence_count}} Occurrences</span>
              <span class="badge">${{massG}} g</span>
              <span class="badge">CoM [${{comMm.join(', ')}}] mm</span>
            </div>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
              <button class="btn-primary" onclick="exportLiveFab()">🖨️ Export Fab Pack (STL + DXF + BOM)</button>
              <button class="btn-ghost" onclick="syncAndPullLive(false, true, true, null)">💾 Save .sc &amp; Pull JSON</button>
              <button class="btn-ghost" onclick="syncAndPullLive(true, false, false, null)">🔗 Reload Links</button>
              <button class="btn-ghost" onclick="loadLiveInspector()">↻</button>
            </div>
          </div>
          <div style="display:flex; gap:6px; align-items:center; margin-top:8px; flex-wrap:wrap;">
            <span style="font-size:0.75rem; color:var(--muted);">Switch Assembly (.sc):</span>
            <select id="liveScSelect" style="flex:1; min-width:200px; font-size:0.76rem; padding:4px 8px;">
              ${{scOptions}}
            </select>
            <button class="btn-ghost" style="padding:4px 10px; font-size:0.75rem;" onclick="syncAndPullLive(false, false, false, document.getElementById('liveScSelect').value)">📂 Open in GUI</button>
          </div>
          <div id="liveFabResults"></div>
        </div>
      </div>

      <!-- COTS-Filtered Custom Clearance Debugger -->
      <div class="tpl-card" style="margin-bottom:12px;">
        <div class="card-body">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
            <div class="title" style="font-size:0.9rem; color:var(--accent);">🔍 COTS-Filtered Collision &amp; Clearance Debugger</div>
            <button class="btn-ghost" style="padding:2px 7px; font-size:0.72rem; border-color:#f87171; color:#f87171;" onclick="focusLivePoint(null, null, true, null)">✕ Clear Section Cut</button>
          </div>
          <div style="display:flex; gap:8px; align-items:center; margin-top:8px; flex-wrap:wrap;">
            <label style="font-size:0.75rem; color:var(--muted);">Min Gap (mm):</label>
            <input type="number" id="liveClearanceThr" value="1.0" step="0.25" min="0.1" max="10" style="width:68px; padding:3px 6px; font-size:0.75rem;">
            <label class="toggle"><input type="checkbox" id="liveClearanceCustom" checked> Filter COTS Fastener/Bearing Interfaces</label>
            <button class="btn-primary" style="padding:4px 10px; font-size:0.75rem;" onclick="runLiveClearance()">🔍 Audit Custom vs. COTS</button>
          </div>
          <div id="liveClearanceResults" style="margin-top:6px;"></div>
        </div>
      </div>

      <!-- Coupled Drivetrain Unlocker & Auto-Park Sweep -->
      <div class="tpl-card">
        <div class="card-body">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div class="title" style="font-size:0.9rem; color:var(--accent);">⚙️ Coupled Drivetrain &amp; Mechanism Sweep (${{drivenJoints.length}} Driven)</div>
            <span class="badge">${{(st.motion_links || []).length}} Gear/Belt Links</span>
          </div>
          <div style="max-height:340px; overflow-y:auto; margin-top:6px; padding-right:4px;">
            ${{jointRows || '<div style="color:var(--muted); font-size:0.76rem; padding:8px 0;">No non-fixed driven joints in this design.</div>'}}
          </div>
        </div>
      </div>
    `;
  }} catch (e) {{
    liveView.innerHTML = `<div style="color:#f87171;">Failed to inspect live scene: ${{e}}</div>`;
  }}
}}

async function runHygieneFix(scope) {{
  showToast('Running CAD file hygiene & >50MB .gz auto-compression...');
  try {{
    const res = await fetch('/api/git/hygiene', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ scope: scope || null, fix: true }})
    }});
    const d = await res.json();
    showToast(`Fixed ${{ (d.actions_taken || []).length }} hygiene issue(s)!`);
    loadGitStatus();
  }} catch (e) {{
    alert('Hygiene fix failed: ' + e);
  }}
}}

async function restoreDeletedInScope(scope) {{
  showToast(`Restoring deleted tracked files in ${{scope}}...`);
  try {{
    const res = await fetch('/api/git/restore-deleted', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ scope }})
    }});
    const d = await res.json();
    showToast(`Restored ${{d.restored_count || 0}} file(s) in ${{scope}}!`);
    loadGitStatus();
  }} catch (e) {{
    alert('Restore failed: ' + e);
  }}
}}

async function commitScope(scope, idx) {{
  const msgEl = document.getElementById(`git_msg_${{idx}}`);
  const msg = msgEl ? msgEl.value.trim() : '';
  if (!msg) {{ alert('Enter a commit message first.'); return; }}
  showToast(`Staging, committing & pushing ${{scope}}...`);
  try {{
    const res = await fetch('/api/git/commit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ scope, message: msg, push: true, auto_fix_hygiene: true }})
    }});
    const d = await res.json();
    if (d.status === 'ok') {{
      showToast(`Committed ${{d.commit_sha}} (${{d.staged_files_count}} files) & pushed!`);
      loadGitStatus();
    }} else {{
      alert('Commit error: ' + (d.error || JSON.stringify(d)));
    }}
  }} catch (e) {{
    alert('Commit failed: ' + e);
  }}
}}

async function runVisualDiff(relPath, slotId) {{
  const box = document.getElementById(slotId);
  if (box) box.innerHTML = '<div style="padding:8px; color:var(--accent); font-size:0.78rem;">Running SolidCraft 3D surface comparison vs HEAD...</div>';
  try {{
    const res = await fetch('/api/git/diff', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ path: relPath }})
    }});
    const d = await res.json();
    if (d.status === 'ok' && box) {{
      box.innerHTML = `<div style="margin-top:6px; padding:8px; background:#0f172a; border:1px solid var(--accent); border-radius:8px;">
        <div style="display:flex; justify-content:space-between; font-size:0.76rem; margin-bottom:6px;">
          <span><b>Verdict:</b> ${{d.verdict || (d.identical ? 'Identical' : 'Modified')}}</span>
          <span><b>ΔVol:</b> ${{d.volume_delta_mm3 !== null ? d.volume_delta_mm3 + ' mm³' : 'n/a'}} | <b>Max Dev:</b> ${{d.max_deviation_mm || 0}} mm</span>
        </div>
        <img src="${{d.diff_png_url}}" style="width:100%; max-height:240px; object-fit:contain; background:#dbe4f0; border-radius:6px;" alt="3D Diff">
      </div>`;
    }} else if (box) {{
      box.innerHTML = `<div style="color:#f87171; font-size:0.76rem;">Diff error: ${{d.error || 'failed'}}</div>`;
    }}
  }} catch (e) {{
    if (box) box.innerHTML = `<div style="color:#f87171; font-size:0.76rem;">Diff error: ${{e}}</div>`;
  }}
}}

async function loadGitStatus() {{
  statsLabel.textContent = 'Loading Git status...';
  try {{
    const res = await fetch('/api/git/status');
    const st = await res.json();
    statsLabel.textContent = `${{st.total_dirty_files}} changed files | ${{st.total_hygiene_issues}} hygiene warnings`;
    const scopeCards = (st.scopes || []).map((grp, idx) => {{
      const defaultMsg = grp.scope.startsWith('projects/')
        ? `feat(${{grp.scope.replace('projects/', '')}}): update CAD & fabrication deliverables`
        : `chore(${{grp.scope}}): sync CAD workspace updates`;
      const fileRows = grp.files.slice(0, 45).map((f, fIdx) => {{
        const slotId = `diff_slot_${{idx}}_${{fIdx}}`;
        const pEsc = f.path.replace(/'/g, "\\'");
        const hygBadge = f.hygiene
          ? `<span class="badge" style="background:rgba(245,158,11,0.2); color:#fbbf24; border:1px solid #f59e0b;" title="${{f.hygiene.message}}">⚠️ ${{f.hygiene.code}}</span>`
          : '';
        const diffBtn = f.visual_diff_available
          ? `<button class="btn-ghost" style="padding:2px 7px; font-size:0.72rem; border-color:var(--accent); color:var(--accent);" onclick="runVisualDiff('${{pEsc}}', '${{slotId}}')">🔍 3D Diff vs HEAD</button>`
          : '';
        const statusColor = f.status === 'deleted' ? '#f87171' : (f.status === 'untracked' ? '#38bdf8' : '#4ade80');
        return `<div style="padding:5px 0; border-bottom:1px solid rgba(51,65,85,0.35); font-size:0.76rem;">
          <div style="display:flex; justify-content:space-between; align-items:center; gap:6px; flex-wrap:wrap;">
            <div style="word-break:break-all;">
              <span style="font-family:ui-monospace,monospace; font-weight:700; color:${{statusColor}}; margin-right:6px;">${{f.xy}}</span>
              <span>${{f.path.replace(grp.scope + '/', '')}}</span>
            </div>
            <div style="display:flex; gap:5px; align-items:center; flex-shrink:0;">
              ${{f.size_mb > 0 ? `<span class="badge">${{f.size_mb}} MB</span>` : ''}}
              ${{hygBadge}}
              ${{diffBtn}}
            </div>
          </div>
          <div id="${{slotId}}"></div>
        </div>`;
      }}).join('');
      const moreNote = grp.files.length > 45 ? `<div style="font-size:0.74rem; color:var(--muted); padding-top:4px;">+ ${{grp.files.length - 45}} more files...</div>` : '';
      const restoreBtn = grp.deleted_count > 0
        ? `<button class="btn-ghost" onclick="restoreDeletedInScope('${{grp.scope}}')">↩️ Restore ${{grp.deleted_count}} Deleted</button>`
        : '';
      const hygBtn = grp.hygiene_issue_count > 0
        ? `<button class="btn-ghost" style="border-color:var(--warn); color:var(--warn);" onclick="runHygieneFix('${{grp.scope}}')">🧹 Fix ${{grp.hygiene_issue_count}} Hygiene</button>`
        : '';
      return `<div class="tpl-card" style="margin-bottom:12px;">
        <div class="card-body">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
            <div class="title" style="font-size:0.95rem; color:var(--accent);">${{grp.scope}}</div>
            <div class="badges">
              <span class="badge">${{grp.modified_count}} mod</span>
              <span class="badge">${{grp.untracked_count}} new</span>
              ${{grp.deleted_count ? `<span class="badge" style="color:#f87171;">${{grp.deleted_count}} del</span>` : ''}}
              <span class="badge">${{grp.total_size_mb}} MB</span>
            </div>
          </div>
          <div style="max-height:260px; overflow-y:auto; margin-top:6px; padding-right:4px;">
            ${{fileRows}}
            ${{moreNote}}
          </div>
        </div>
        <div class="tpl-params" style="grid-template-columns: 1fr;">
          <div class="tpl-param">
            <label>Commit Message for ${{grp.scope}}</label>
            <input type="text" id="git_msg_${{idx}}" value="${{defaultMsg}}">
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-primary" onclick="commitScope('${{grp.scope}}', ${{idx}})">✅ Commit &amp; Push Scope</button>
          ${{hygBtn}}
          ${{restoreBtn}}
        </div>
      </div>`;
    }}).join('');

    gitView.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; background:var(--card); border:1px solid var(--border); border-radius:10px; padding:10px 14px; margin-bottom:12px;">
        <div>
          <span class="sku">Branch: ${{st.branch}}</span>
          <span class="badge" style="margin-left:6px;">↑${{st.ahead}} ↓${{st.behind}}</span>
          <span class="badge" style="margin-left:6px;">Pre-Commit >95MB Guard Active</span>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn-primary" onclick="runHygieneFix(null)">🧹 Auto-Fix All Hygiene &amp; >50MB Gzip (${{st.total_hygiene_issues}})</button>
          <button class="btn-ghost" onclick="loadGitStatus()">↻ Refresh Git</button>
        </div>
      </div>
      ${{scopeCards || '<div style="padding:20px; text-align:center; color:var(--muted);">Working tree is completely clean!</div>'}}
    `;
  }} catch (e) {{
    gitView.innerHTML = `<div style="color:#f87171;">Failed to load Git status: ${{e}}</div>`;
  }}
}}

document.getElementById('pullScriptBtn').addEventListener('click', async () => {{
  showToast('Pulling live GUI timeline & parameters...');
  try {{
    const res = await fetch('/api/hud/pull-script', {{ method: 'POST' }});
    const d = await res.json();
    if (d.status === 'ok') {{
      showToast(`Pulled ${{d.steps_exported || 0}} timeline steps & ${{(d.user_parameters || []).length}} user params to Git!`);
    }} else {{
      alert('Pull error: ' + (d.error || JSON.stringify(d)));
    }}
  }} catch (e) {{
    alert('Pull failed: ' + e);
  }}
}});

render();
syncLiveState();
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


def launch_floating_dock_window(url: str) -> bool:
    """
    Launch a compact, native-feeling floating app window (`--app=... --window-size=440,860`)
    using Edge or Chrome on the interactive desktop.
    """
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    browser_exe = next((c for c in candidates if Path(c).is_file()), None)
    if not browser_exe:
        webbrowser.open(url)
        return True

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class STARTUPINFO(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE),
                ]

            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
                ]

            si = STARTUPINFO()
            si.cb = ctypes.sizeof(STARTUPINFO)
            si.lpDesktop = "WinSta0\\Default"
            pi = PROCESS_INFORMATION()
            cmd = f'"{browser_exe}" --app="{url}" --window-size=460,860 --window-position=1420,80'
            ok = ctypes.windll.kernel32.CreateProcessW(
                None, cmd, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)
            )
            if ok:
                return True
        except Exception:
            pass

    subprocess.Popen([browser_exe, f"--app={url}", "--window-size=460,860"])
    return True


def serve_cots_dock_and_browser(
    http_port: int = 8765,
    solidcraft_port: int = 27878,
    dock_mode: bool = False,
    open_window: bool = True,
    hud_controller: Optional[Any] = None,
) -> None:
    """
    Serve `cots/solidcraft/` (including `index.html` and `thumbs/*.png`) along with
    `/api/state` and `/api/insert` for live 1-click SolidCraft assembly interaction.
    """
    generate_visual_catalog_and_dock(DB_PATH)
    if dock_mode and hud_controller is None:
        try:
            from tools.solidcraft_plugin import SolidCraftHudController

            hud_controller = SolidCraftHudController()
            hud_controller.start()
        except Exception:
            hud_controller = None

    class _DockHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            req_path = parsed.path
            if req_path in ("/", "/index.html"):
                data = (SOLIDCRAFT_LIB_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
            elif req_path == "/api/state":
                st = query_live_solidcraft_state(connect_port=solidcraft_port)
                if hud_controller is not None:
                    st["hud"] = hud_controller.status_dict()
                payload = json.dumps(st).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path == "/api/hud/selection":
                from tools.solidcraft_plugin import send_solidcraft_control_rpc
                sel = send_solidcraft_control_rpc("ui.selection", {}, port=solidcraft_port)
                payload = json.dumps(sel).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path == "/api/hud/status":
                st = hud_controller.status_dict() if hud_controller is not None else {"snap_to_solidcraft": False}
                payload = json.dumps(st).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path == "/api/templates":
                from tools.solidcraft_builder import list_parametric_templates
                payload = json.dumps(list_parametric_templates()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path == "/api/git/status":
                from tools.cad_git_manager import cad_git_status
                payload = json.dumps(cad_git_status()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path == "/api/live/state":
                from tools.solidcraft_inspector import live_inspect_state
                payload = json.dumps(live_inspect_state(port=solidcraft_port)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif req_path.startswith("/diffs/"):
                from tools.cad_git_manager import DIFFS_DIR
                fname = Path(urllib.parse.unquote(req_path[len("/diffs/") :])).name
                d_file = DIFFS_DIR / fname
                if d_file.is_file():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(d_file.read_bytes())
                else:
                    self.send_response(404)
                    self.end_headers()
            elif req_path.startswith("/thumbs/"):
                fname = Path(urllib.parse.unquote(req_path[len("/thumbs/") :])).name
                t_file = THUMBS_DIR / fname
                if t_file.is_file():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(t_file.read_bytes())
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/api/hud/open-plugins-prefs":
                from tools.solidcraft_plugin import send_solidcraft_control_rpc
                res = send_solidcraft_control_rpc("ui.prefs", {"open": "Plugins"}, port=solidcraft_port)
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/hud/toggle-snap":
                res = hud_controller.toggle_snap() if hud_controller is not None else {"snap_to_solidcraft": False}
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/git/hygiene":

                from tools.cad_git_manager import cad_file_hygiene_audit

                length = int(self.headers.get("Content-Length", "0"))

                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

                res = cad_file_hygiene_audit(scope_filter=body.get("scope"), fix=bool(body.get("fix", True)))

                payload = json.dumps(res).encode("utf-8")

                self.send_response(200)

                self.send_header("Content-Type", "application/json")

                self.end_headers()

                self.wfile.write(payload)

            elif self.path == "/api/git/diff":

                from tools.cad_git_manager import cad_visual_diff_head

                length = int(self.headers.get("Content-Length", "0"))

                body = json.loads(self.rfile.read(length).decode("utf-8"))

                try:

                    res = cad_visual_diff_head(body.get("path", ""))

                    payload = json.dumps(res).encode("utf-8")

                    self.send_response(200)

                except Exception as exc:

                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")

                    self.send_response(500)

                self.send_header("Content-Type", "application/json")

                self.end_headers()

                self.wfile.write(payload)

            elif self.path == "/api/git/restore-deleted":

                from tools.cad_git_manager import cad_git_restore_deleted

                length = int(self.headers.get("Content-Length", "0"))

                body = json.loads(self.rfile.read(length).decode("utf-8"))

                res = cad_git_restore_deleted(body.get("scope", ""), paths=body.get("paths"))

                payload = json.dumps(res).encode("utf-8")

                self.send_response(200)

                self.send_header("Content-Type", "application/json")

                self.end_headers()

                self.wfile.write(payload)

            elif self.path == "/api/git/commit":

                from tools.cad_git_manager import cad_git_stage_commit_push

                length = int(self.headers.get("Content-Length", "0"))

                body = json.loads(self.rfile.read(length).decode("utf-8"))

                try:

                    res = cad_git_stage_commit_push(

                        scope=body.get("scope", ""),

                        message=body.get("message", ""),

                        push=bool(body.get("push", True)),

                        auto_fix_hygiene=bool(body.get("auto_fix_hygiene", True)),

                        paths=body.get("paths"),

                    )

                    payload = json.dumps(res).encode("utf-8")

                    self.send_response(200)

                except Exception as exc:

                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")

                    self.send_response(500)

                self.send_header("Content-Type", "application/json")

                self.end_headers()

                self.wfile.write(payload)

            elif self.path == "/api/hud/pull-script":
                from tools.solidcraft_builder import pull_solidcraft_script
                try:
                    res = pull_solidcraft_script(live=True, connect_port=solidcraft_port)
                    payload = json.dumps(res).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/clearance":
                from tools.solidcraft_inspector import live_run_clearance_audit
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_run_clearance_audit(
                    port=solidcraft_port,
                    clearance_mm=float(body.get("clearance_mm", 1.0)),
                    custom_only=bool(body.get("custom_only", True)),
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/focus":
                from tools.solidcraft_inspector import live_focus_viewport
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_focus_viewport(
                    target_xyz=body.get("target_xyz"),
                    section_plane=body.get("section_plane"),
                    clear_section=bool(body.get("clear_section", False)),
                    view_preset=body.get("view_preset"),
                    port=solidcraft_port,
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/joint/drive":
                from tools.solidcraft_inspector import live_drive_joint
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_drive_joint(
                    joint=str(body.get("joint", "")),
                    value=float(body.get("value", 0.0)),
                    port=solidcraft_port,
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/joint/sweep":
                from tools.solidcraft_inspector import live_sweep_joint
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_sweep_joint(
                    joint=str(body.get("joint", "")),
                    from_val=float(body.get("from_val", 0.0)),
                    to_val=float(body.get("to_val", 360.0)),
                    steps=int(body.get("steps", 24)),
                    auto_focus_on_contact=bool(body.get("auto_focus_on_contact", True)),
                    port=solidcraft_port,
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/param/change":
                from tools.solidcraft_inspector import live_change_parameter
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_change_parameter(
                    name=str(body.get("name", "")),
                    expression=str(body.get("expression", "")),
                    run_sweep=bool(body.get("run_sweep", False)),
                    port=solidcraft_port,
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/sync":
                from tools.solidcraft_inspector import live_sync_and_pull
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                res = live_sync_and_pull(
                    update_links=bool(body.get("update_links", False)),
                    save_design=bool(body.get("save_design", False)),
                    pull_to_git_json=bool(body.get("pull_to_git_json", False)),
                    open_sc_path=body.get("open_sc_path"),
                    port=solidcraft_port,
                )
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/live/fab":
                from tools.solidcraft_inspector import live_export_fabrication_pack
                res = live_export_fabrication_pack(port=solidcraft_port)
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/template/build":
                from tools.solidcraft_builder import build_template_part
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                try:
                    res = build_template_part(
                        body.get("template_id", ""),
                        params_override=body.get("params_override"),
                        part_name=body.get("part_name"),
                        insert_live=bool(body.get("insert_live", False)),
                        target_occurrence=body.get("target_occurrence"),
                        joint_type=body.get("joint_type"),
                        anchor=body.get("anchor"),
                        target_point=body.get("target_point"),
                        connect_port=solidcraft_port,
                    )
                    payload = json.dumps(res).encode("utf-8")
                    self.send_response(200)
                except Exception as exc:
                    payload = json.dumps({"status": "error", "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/api/insert":
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                try:
                    res = solidcraft_insert_cots(
                        body.get("key", ""),
                        translate=body.get("translate"),
                        anchor=body.get("anchor"),
                        joint_type=body.get("joint_type"),
                        target_occurrence=body.get("target_occurrence"),
                        target_point=body.get("target_point"),
                        connect_port=solidcraft_port,
                    )
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

    server = ThreadingHTTPServer(("127.0.0.1", http_port), _DockHandler)
    url = f"http://127.0.0.1:{http_port}/" + ("?dock=1" if dock_mode else "")
    print(f"SolidCraft COTS {'Dock' if dock_mode else 'Browser'} active at {url} (SolidCraft port {solidcraft_port})", flush=True)
    if open_window:
        if dock_mode:
            launch_floating_dock_window(url)
        else:
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SolidCraft Project Compiler, Thumbnail Renderer & Floating Dock")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ren = sub.add_parser("rename-skus", help="Enrich SKU-only .sc filenames to <SKU>_<Clean_Title>.sc")
    p_th = sub.add_parser("thumbs", help="Batch-render 3D isometric thumbnails for all COTS .sc parts")
    p_th.add_argument("--workers", type=int, default=10)
    p_th.add_argument("--force", action="store_true")
    p_th.add_argument("--limit", type=int, default=None)

    p_comp = sub.add_parser("compile", help="Compile a project STEP + *_constraints.json into a jointed .sc assembly")
    p_comp.add_argument("step_file", type=Path, help="Path to .step or .step.gz file")
    p_comp.add_argument("--constraints", type=Path, default=None, help="Optional *_constraints.json path")
    p_comp.add_argument("--out", type=Path, default=None, help="Output .sc path")
    p_comp.add_argument("--no-gif", action="store_true", help="Skip animated .gif export")
    p_comp.add_argument("--live", action="store_true", help="Open the compiled .sc assembly in live SolidCraft (port 27878)")
    p_comp.add_argument("--port", type=int, default=27878)

    p_dock = sub.add_parser("dock", help="Launch the floating SolidCraft COTS companion dock")
    p_dock.add_argument("--http-port", type=int, default=8765)
    p_dock.add_argument("--port", type=int, default=27878)
    p_dock.add_argument("--no-open", action="store_true")

    p_insp = sub.add_parser("inspect", help="Run fast SolidCraft Rust B-Rep clearance & collision audit")
    p_insp.add_argument("target_file", type=Path)
    p_insp.add_argument("--clearance", type=float, default=0.5)
    p_insp.add_argument("--all-bodies", action="store_true")

    p_cmp = sub.add_parser("compare", help="Compare two CAD revisions in 3D via inspect.compare")
    p_cmp.add_argument("file_a", type=Path)
    p_cmp.add_argument("file_b", type=Path)
    p_cmp.add_argument("--out-png", type=Path, default=None)

    p_cpic = sub.add_parser("compare-picture", help="Compare a 3D model against a reference picture via inspect.compare_picture")
    p_cpic.add_argument("model_file", type=Path)
    p_cpic.add_argument("reference_image", type=Path)
    p_cpic.add_argument("--view", default="iso")
    p_cpic.add_argument("--out-png", type=Path, default=None)

    p_fab = sub.add_parser("fab", help="Export print-face oriented STLs and flat DXFs from a .sc assembly")
    p_fab.add_argument("sc_file", type=Path)
    p_fab.add_argument("--project-dir", type=Path, default=None)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.cmd == "rename-skus":
        print(json.dumps(rename_sku_only_sc_files(DB_PATH), indent=2))
    elif args.cmd == "thumbs":
        print(json.dumps(render_all_cots_thumbnails(DB_PATH, workers=args.workers, force=args.force, limit=args.limit), indent=2))
    elif args.cmd == "compile":
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
    elif args.cmd == "dock":
        serve_cots_dock_and_browser(http_port=args.http_port, solidcraft_port=args.port, dock_mode=True, open_window=not args.no_open)
    elif args.cmd == "inspect":
        print(json.dumps(solidcraft_inspect_assembly(args.target_file, clearance_mm=args.clearance, custom_only=not args.all_bodies), indent=2))
    elif args.cmd == "compare":
        print(json.dumps(solidcraft_compare_revisions(args.file_a, args.file_b, out_png=args.out_png), indent=2))
    elif args.cmd == "compare-picture":
        print(json.dumps(solidcraft_compare_picture(args.model_file, args.reference_image, view=args.view, out_overlay_png=args.out_png), indent=2))
    elif args.cmd == "fab":
        print(json.dumps(solidcraft_export_fabrication(args.sc_file, project_dir=args.project_dir), indent=2))
