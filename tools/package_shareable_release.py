r"""
Build & Publish the Shareable Robotics CAD Workflow + COTS Library Repository
=============================================================================
Packages the reusable CAD workflow (`tools/`, `plugins/`, `.agents/`, docs,
starter example, and `cots/` indices/thumbnails) into a clean, secret-free,
custom-project-free standalone Git repository, and packs all 2,588 COTS `.step`
and normalized Format-52 `.sc` hardware models into parallel, SHA-256 verified
`< 1.8 GiB` `.zip` shards for GitHub Releases (`cots/cots_release_manifest.json`).

Usage:
    .\.venv\Scripts\python.exe tools/package_shareable_release.py \
        --staging-dir <path/to/staging> \
        --repo ARES-23247/robotics-cad-workflow \
        --tag v1.0.0 \
        --publish
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Tuple
import zipfile

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
COTS_DIR = WORKSPACE_ROOT / "cots"

logger = logging.getLogger("package_shareable_release")

INCLUDED_TOOLS = [
    "ag_cad_helpers.py",
    "cad_fea.py",
    "cad_git_manager.py",
    "cad_mcp_server.py",
    "cad_physics_sim.py",
    "cad_pipeline.py",
    "cad_scraper.py",
    "cad_slicer_audit.py",
    "cad_team_share.py",
    "package_shareable_release.py",
    "setup_solidcraft_cots.py",
    "solidcraft_acp_agent.py",
    "solidcraft_builder.py",
    "solidcraft_compiler.py",
    "solidcraft_cots.py",
    "solidcraft_inspector.py",
    "solidcraft_plugin.py",
    "test_cad_nextgen_pillars.py",
    "test_cad_pipeline.py",
]

ROOT_FILES = [
    "bootstrap.ps1",
    "install.ps1",
    "install.sh",
    "setup-solidcraft-cots.ps1",
    "setup-solidcraft-cots.sh",
    "requirements.txt",
    "requirements-gpu.txt",
    ".env.example",
    ".gitattributes",
]

COTS_BINARY_SUBDIRS = [
    "fasteners",
    "gobilda-and-fasteners",
    "servos-and-hardware",
    "swerve-cots",
    "vendors",
    "solidcraft/fasteners",
    "solidcraft/gobilda-and-fasteners",
    "solidcraft/servos-and-hardware",
    "solidcraft/swerve-cots",
    "solidcraft/vendors",
]

STANDALONE_GITIGNORE = """# Local secrets & environment variables (never commit)
.env
.env.*
!.env.example

# Python & Node virtual environments / caches
__pycache__/
.pytest_cache/
*.py[cod]
*$py.class
.venv/
venv/
node_modules/
.matplotlib/
.mpl-cache/
.validation_deps/
*.log
*.sqlite3
*.db-wal
*.db-shm

# OpenCASCADE / FreeCAD / Slicer intermediate geometry caches
**/baseline-cache/
**/recovery/
**/cached-assembly/
**/cache/
**/slice-runs/
**/.mesh_cache/
**/animation*_frames/
**/*.brep
**/*.gcode

# CAD temporary, lock, and backup files
~$*
*.fcbak
*.FCBak
*.blend1
*.blend2
*.bak
*.tmp
*.lck
*.lock
*.swp

# OS metadata
Thumbs.db
Desktop.ini
.DS_Store

# COTS binary CAD packs downloaded automatically from GitHub Releases by install.ps1
# (Indices, SQLite catalog.db, thumbnails, and HTML/Markdown catalogs remain tracked in Git)
cots/.installed_packs/
cots/fasteners/
cots/gobilda-and-fasteners/
cots/servos-and-hardware/
cots/swerve-cots/
cots/vendors/
cots/solidcraft/diffs/
cots/solidcraft/generated/
cots/solidcraft/fasteners/
cots/solidcraft/gobilda-and-fasteners/
cots/solidcraft/servos-and-hardware/
cots/solidcraft/swerve-cots/
cots/solidcraft/vendors/
"""

STANDALONE_README = r"""# Robotics CAD Workflow & 2,588-Part COTS Library

A self-contained, AI-native parametric CAD, COTS hardware, 3D-print DFM, FEA, and physics-simulation workspace powered by **SolidCraft (`v0.40+`)**, **`build123d` (OpenCASCADE 8.0)**, and **Antigravity**.

## ⚡ One-Command Windows 11/10 Install

Open **PowerShell** on any Windows 11/10 computer and paste:

```powershell
irm https://raw.githubusercontent.com/__REPO_SLUG__/main/bootstrap.ps1 | iex
```

*(To install into a custom folder instead of `$HOME\dev\robotics\CAD`, run `$env:CAD_WORKSPACE_DIR="C:\CAD"; irm https://raw.githubusercontent.com/__REPO_SLUG__/main/bootstrap.ps1 | iex`.)*

### Or Clone + Install Manually

```powershell
git clone https://github.com/__REPO_SLUG__.git C:\dev\CAD
cd C:\dev\CAD
.\install.ps1
```

### What `.\install.ps1` Sets Up Automatically
1. **Python 3.12 + OpenCASCADE Environment (`.venv`)**: Installs `uv` (no pre-installed Python needed) and provisions `build123d`, `OCP`, `trimesh`, `scikit-fem`, `Gmsh`, `MuJoCo`, `CoACD`, `ezdxf`, and `pytest`.
2. **SolidCraft Desktop + CLI (`v0.40.0+`)**: Automatically downloads and installs SolidCraft in user space (`%LOCALAPPDATA%\Programs\SolidCraft`, zero admin prompt) with Start Menu shortcuts (`SolidCraft` and `SolidCraft + Robotics COTS HUD`).
3. **2,588 Verified FTC & FRC COTS Hardware Models (`cots/`)**: Downloads and verifies all vendor `.step` models and coordinate-normalized Format-52 `.sc` SolidCraft components (`goBILDA`, `REV DUO`, `REV ION`, `Swyft`, `ThriftyBot`, `Ruthex`, `McMaster`) with pre-baked `joint.origin` mate connectors (`bore_center`, `flange_seat`, `output_shaft`, `mount_hole_1..8`) and physical materials.
4. **Antigravity AI Plugin & MCP Servers**: Installs the `solidcraft-robotics-cad` plugin (`~/.gemini/config/plugins/solidcraft-robotics-cad`) and configures `solidcraft`, `solidcraft-live`, and `ares_onshape_local` MCP servers in Antigravity, Gemini CLI, Claude Code, and Codex.
5. **Live `Alt+C` SolidCraft Hardware HUD**: Registers the `solidcraft-plugin` launcher that docks a searchable COTS browser & 1-click concentric auto-mate panel directly onto the SolidCraft window.

---

## 🚀 Getting Started

1. **Open in Antigravity**: Open your cloned `CAD` folder as a workspace in **Antigravity** (restart Antigravity once after installing so the new MCP servers connect).
2. **Launch SolidCraft + COTS Hardware HUD**:
   ```powershell
   solidcraft-plugin
   ```
   Press **`Alt+C`** anytime inside SolidCraft to toggle the docked COTS Hardware & Assembly HUD.
3. **Build & Compile the Starter Project**:
   ```powershell
   .\.venv\Scripts\python.exe tools\cad_pipeline.py build projects\starter-bearing-block\src\build_bearing_block.py
   ```
   This builds a custom 3D-printed 16 mm grid bearing pillow block (`fabrication/prints/bearing_block.stl`), mates two goBILDA 8 mm REX flanged bearings and a shaft, runs interference checks, and compiles a jointed `.sc` SolidCraft assembly + BOM + animated motion GIF!
4. **Verify Health Anytime**:
   ```powershell
   .\install.ps1 -Doctor
   ```

---

## 📁 Workspace Layout

| Location | Contents |
| :--- | :--- |
| `projects/<project>/src/` | Parametric `build123d` Python recipes and engineering calculations |
| `projects/<project>/cad/` | SolidCraft `.sc` masters, `.step` exports, and `*_constraints.json` specs |
| `projects/<project>/fabrication/prints/` | Watertight `.stl` parts and arranged `.3mf` 3D print plates |
| `projects/<project>/fabrication/laser/` | `.dxf` and `.svg` laser/CNC profiles |
| `projects/<project>/docs/` | Contact-sheet renders, BOMs, FEA stress plots, and kinematic GIFs |
| `cots/` | 2,588 FTC/FRC COTS `.step` & normalized `.sc` parts + `catalog.db` + visual catalog (`cots/solidcraft/index.html`) |
| `tools/` | Unified CAD CLI (`cad_pipeline.py`), MCP server (`cad_mcp_server.py`), helpers (`ag_cad_helpers.py`), FEA, Slicer Audit, MuJoCo Sim, and Team WebGL Packager |
| `plugins/` | SolidCraft Desktop Plugin (`ares-robotics-cots`) and Antigravity AI Plugin (`solidcraft-robotics-cad`) |

Read [AGENTS.md](AGENTS.md) and [WORKFLOW.md](WORKFLOW.md) for full modeling conventions and CLI commands.
"""

STANDALONE_AGENTS_MD = """# Robotics CAD Workspace Instructions

These instructions apply to Antigravity, Codex, and Claude Code work in this CAD workspace repository. Read [WORKFLOW.md](WORKFLOW.md) for modeling routes, verification, and tool behavior, then read the selected project's `docs/AGENTS.md` when present.

## Workspace and files

- This repository root is the active, self-contained workspace. Run local tools from this root using `.venv/Scripts/python.exe` (Windows) or `.venv/bin/python` (macOS/Linux).
- Work under `projects/<project>/`: recipes in `src/`, CAD masters/exports (`.sc`, `.step`) and constraint specs in `cad/`, 3D-printed parts and build plates (`.stl`, `.3mf`) in `fabrication/prints/`, cutting profiles in `fabrication/laser/`, and plans, verification records, BOMs, and renders in `docs/`.
- Keep all 3D-printed parts and build plates inside their respective project folder (`projects/<project>/fabrication/prints/`).
- Shared purchased hardware and the COTS CAD library live in `cots/`; reusable utilities live in `tools/`.
- Never scatter CAD or 3D-print files into `~/Documents` or `~/OneDrive`.

## Modeling route

- **SolidCraft-First Local Iteration (`SolidCraft` `.sc` + `build123d`)**:
  - Perform day-to-day CAD iteration, AI co-design, COTS placement (`cots/solidcraft/*.sc` via `solidcraft_insert_cots` or the `Alt+C` HUD plugin `tools/solidcraft_plugin.py`), kinematic joint compilation (`solidcraft_compile_project` / `cad_pipeline.py build`), and mass/CoM/BOM checks locally in SolidCraft (`solidcraft` headless MCP and `solidcraft-live` desktop GUI on `127.0.0.1:27878`).
  - Reuse pre-built normalized COTS `.sc` parts from `cots/solidcraft/` (`component.insert_design {"reuse": true}`) rather than re-importing raw supplier STEP files when building SolidCraft assemblies.
- **Optional Onshape Milestone Sync**:
  - When syncing milestone STEP assemblies to Onshape, prefer the signed-in browser session (`build_onshape_browser_sync_js()`) or session-authenticated API Explorer. Never commit API keys to Git; keep any optional `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY` strictly in gitignored `.env`.

## Assembly constraints and verification

1. Ground the stationary base to the world frame and rigidly group fixed subcomponents.
2. Add named mate connectors (`joint.origin`) at mounting, input, and output interfaces.
3. Constrain moving parts with the intended `revolute`, `slider`, `cylindrical`, or `rigid` joints and travel limits, plus gear/belt motion links where coupled.
4. Follow the verification checks in [WORKFLOW.md](WORKFLOW.md): engineering stackup calculations, parametric regeneration (`tools/ag_cad_helpers.py`), B-Rep interference & physics linting (`cad_inspect`, `cad_lint`), 6-panel visual + section-cut inspection (`cad_render`), and printability/FEA checks (`cad_slice_audit`, `cad_fea`) where applicable.
"""

STARTER_PROJECT_README = r"""# Starter Project: 8mm REX Twin-Bearing Pillow Block

A minimal end-to-end example demonstrating:
1. Parametric custom part modeling in `build123d` (`BearingPillowBlock`) on a 16 mm M4 grid with counterbored mounting holes and bearing bores.
2. Loading verified COTS goBILDA 8 mm REX flanged bearings (`1611-0514-4008`) from `cots/` using `load_cots_part()`.
3. Exporting multi-part `.step`, printable `.stl` (`fabrication/prints/`), BOM, `*_constraints.json`, and compiling a jointed SolidCraft `.sc` assembly + animated motion GIF.

## Run It

```powershell
.\.venv\Scripts\python.exe tools\cad_pipeline.py build projects\starter-bearing-block\src\build_bearing_block.py
.\.venv\Scripts\python.exe tools\cad_pipeline.py render projects\starter-bearing-block\cad\starter_bearing_block.step --out projects\starter-bearing-block\docs
.\.venv\Scripts\python.exe tools\cad_pipeline.py slice-audit projects\starter-bearing-block\fabrication\prints\bearing_pillow_block.stl
```
"""

STARTER_PROJECT_SCRIPT = '''"""
Starter Parametric CAD Example: 8mm REX Twin-Bearing Pillow Block
=================================================================
Builds a 3D-printable twin-bearing block on the standard 16 mm M4 grid, seats two
verified goBILDA 8 mm REX flanged bearings (`1611-0514-4008`) and a rotating shaft,
and exports `.step`, `_custom.step` (for linked SolidCraft COTS `.sc` assembly),
printable `.stl`, BOM, kinematic sweep audit, and `*_constraints.json`.
"""

from pathlib import Path
import sys

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from build123d import Axis, Box, BuildPart, CounterBoreHole, Cylinder, Locations, Mode, export_stl, fillet
from tools.ag_cad_helpers import (
    assign_part_metadata,
    export_project_deliverables,
    gobilda_pattern_coords,
    load_cots_part,
)

PARAMS = {
    "block_length_mm": 48.0,
    "block_width_mm": 32.0,
    "block_thickness_mm": 14.0,
    "bearing_od_clearance_mm": 14.20,  # 14.0 mm OD + 0.20 mm printed fit clearance
    "m4_clearance_dia_mm": 4.20,
    "shaft_length_mm": 44.0,
}


def main() -> None:
    proj_dir = Path(__file__).resolve().parents[1]
    prints_dir = proj_dir / "fabrication" / "prints"
    prints_dir.mkdir(parents=True, exist_ok=True)

    L = PARAMS["block_length_mm"]
    W = PARAMS["block_width_mm"]
    H = PARAMS["block_thickness_mm"]
    r_bore = 0.5 * PARAMS["bearing_od_clearance_mm"]

    # 1. Custom 3D-printed housing block centered at origin (Z in [-H/2, +H/2])
    with BuildPart() as housing_bp:
        Box(L, W, H)
        fillet(housing_bp.edges().filter_by(Axis.Z), radius=3.0)
        Cylinder(radius=r_bore, height=H, mode=Mode.SUBTRACT)
        mount_pts = [(x, y, H / 2.0) for (x, y) in gobilda_pattern_coords(3, 2, pitch_mm=16.0) if abs(x) > 8.0]
        with Locations(*mount_pts):
            CounterBoreHole(
                radius=0.5 * PARAMS["m4_clearance_dia_mm"],
                counter_bore_radius=3.8,
                counter_bore_depth=2.5,
                depth=H,
            )

    housing = assign_part_metadata(housing_bp.part, "01_Printed_Pillow_Block", (58, 122, 218))

    # 2. Two COTS 8mm REX Flanged Bearings seated on top (+Z) and bottom (-Z) faces
    top_bearing = load_cots_part(
        "1611-0514-4008",
        label="02_COTS_Top_Bearing_8mmREX",
        bore_axis="Z",
        anchor="flange_seat",
        at=(0.0, 0.0, H / 2.0),
    )
    bot_bearing = load_cots_part(
        "1611-0514-4008",
        label="03_COTS_Bottom_Bearing_8mmREX",
        bore_axis="-Z",
        anchor="flange_seat",
        at=(0.0, 0.0, -H / 2.0),
    )

    # 3. Rotating shaft passing cleanly through the 7mm/8mm bearing bore
    with BuildPart() as shaft_bp:
        Cylinder(radius=3.45, height=PARAMS["shaft_length_mm"])
    shaft = assign_part_metadata(shaft_bp.part, "04_Output_Shaft", (238, 174, 24))

    parts = [housing, top_bearing, bot_bearing, shaft]
    stl_path = prints_dir / "bearing_pillow_block.stl"
    export_stl(housing, str(stl_path))

    summary = export_project_deliverables(
        parts=parts,
        project_dir=proj_dir,
        stem="starter_bearing_block",
        assembly_title="Starter Twin-Bearing Pillow Block",
        stationary_parts=[
            "01_Printed_Pillow_Block",
            "02_COTS_Top_Bearing_8mmREX",
            "03_COTS_Bottom_Bearing_8mmREX",
        ],
    )
    print(f"Built {summary['step_path']} and {stl_path}")


if __name__ == "__main__":
    main()
'''


def _pack_single_shard(args: Tuple[Path, List[Tuple[str, str, bool]]]) -> Dict[str, Any]:
    """Worker function: create one shard zip (.sc stored at disk speed, ASCII .step fast-deflated) and compute SHA-256."""
    zip_path, entries = args
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = zip_path.with_suffix(".zip.part")
    tmp_zip.unlink(missing_ok=True)

    raw_bytes_total = 0
    with zipfile.ZipFile(tmp_zip, "w", allowZip64=True) as zf:
        for src_str, arcname, is_gzip_only in entries:
            src = Path(src_str)
            is_sc = arcname.lower().endswith(".sc")
            comp_type = zipfile.ZIP_STORED if is_sc else zipfile.ZIP_DEFLATED
            comp_lvl = None if is_sc else 1
            if is_gzip_only:
                with gzip.open(src, "rb") as gz_in, zf.open(arcname, "w", force_zip64=True) as z_out:
                    while True:
                        buf = gz_in.read(4 * 1024 * 1024)
                        if not buf:
                            break
                        raw_bytes_total += len(buf)
                        z_out.write(buf)
            else:
                raw_bytes_total += src.stat().st_size
                zf.write(src, arcname=arcname, compress_type=comp_type, compresslevel=comp_lvl)

    if zip_path.exists():
        zip_path.unlink()
    tmp_zip.replace(zip_path)

    h = hashlib.sha256()
    with zip_path.open("rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)

    return {
        "name": zip_path.name,
        "file_count": len(entries),
        "uncompressed_bytes": raw_bytes_total,
        "size_bytes": zip_path.stat().st_size,
        "sha256": h.hexdigest(),
    }


def collect_cots_binary_entries() -> List[Tuple[str, str, bool, int]]:
    """
    Collect all clean COTS `.step` and `.sc` files under `cots/` (excluding backups,
    diffs, generated templates, and duplicate `.gz` peers).
    Returns `(abs_src_path, rel_arcname_from_workspace, is_gzip_only, est_bytes)`.
    """
    collected: List[Tuple[str, str, bool, int]] = []
    seen_arcnames = set()

    for subdir in COTS_BINARY_SUBDIRS:
        base = COTS_DIR / subdir
        if not base.exists():
            continue
        for fp in sorted(base.rglob("*")):
            if not fp.is_file():
                continue
            rel_parts_lower = [p.lower() for p in fp.relative_to(COTS_DIR).parts]
            if any(skip in rel_parts_lower for skip in ("cache", "diffs", "generated", "thumbs")):
                continue
            name_low = fp.name.lower()
            if name_low.endswith(".bak") or name_low.startswith(".bak_") or name_low.endswith(".tmp"):
                continue

            if name_low.endswith((".step.gz", ".stp.gz", ".sc.gz")):
                peer = fp.with_suffix("")  # strips .gz
                if peer.is_file():
                    # Raw peer exists; pack the raw peer directly so extraction needs no second pass
                    continue
                arcname = peer.relative_to(WORKSPACE_ROOT).as_posix()
                if arcname not in seen_arcnames:
                    seen_arcnames.add(arcname)
                    collected.append((str(fp), arcname, True, fp.stat().st_size * 3))
            elif name_low.endswith((".step", ".stp", ".sc")):
                arcname = fp.relative_to(WORKSPACE_ROOT).as_posix()
                if arcname not in seen_arcnames:
                    seen_arcnames.add(arcname)
                    collected.append((str(fp), arcname, False, fp.stat().st_size))

    return collected


def build_cots_release_packs(
    packs_out_dir: Path,
    max_uncompressed_bytes_per_shard: int = 1650 * 1024 * 1024,
    workers: int = 10,
) -> List[Dict[str, Any]]:
    """Partition COTS binaries into balanced shards and compress them in parallel."""
    entries = collect_cots_binary_entries()
    logger.info("Collected %d clean COTS CAD files (%.2f GB uncompressed).", len(entries), sum(e[3] for e in entries) / 1e9)

    # Sort largest first, then bin-pack into shards <= max_uncompressed_bytes_per_shard
    entries_sorted = sorted(entries, key=lambda x: x[3], reverse=True)
    shards: List[List[Tuple[str, str, bool]]] = []
    shard_sizes: List[int] = []

    for src_str, arcname, is_gz, est_sz in entries_sorted:
        placed = False
        for idx, cur_sz in enumerate(shard_sizes):
            if cur_sz + est_sz <= max_uncompressed_bytes_per_shard:
                shards[idx].append((src_str, arcname, is_gz))
                shard_sizes[idx] += est_sz
                placed = True
                break
        if not placed:
            shards.append([(src_str, arcname, is_gz)])
            shard_sizes.append(est_sz)

    # Sort entries inside each shard lexicographically for deterministic archives
    tasks: List[Tuple[Path, List[Tuple[str, str, bool]]]] = []
    packs_out_dir.mkdir(parents=True, exist_ok=True)
    for i, shard in enumerate(shards, 1):
        shard.sort(key=lambda t: t[1])
        zip_path = packs_out_dir / f"cots-pack-{i:02d}.zip"
        tasks.append((zip_path, shard))

    logger.info("Compressing %d release packs across %d parallel workers...", len(tasks), min(workers, len(tasks)))
    results_by_name: Dict[str, Dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=min(workers, max(1, len(tasks)))) as pool:
        futs = {pool.submit(_pack_single_shard, t): t[0].name for t in tasks}
        for fut in as_completed(futs):
            meta = fut.result()
            results_by_name[meta["name"]] = meta
            logger.info(
                "  Built %s: %d files, %.1f MB compressed (%.1f MB raw)",
                meta["name"],
                meta["file_count"],
                meta["size_bytes"] / (1024 * 1024),
                meta["uncompressed_bytes"] / (1024 * 1024),
            )

    return [results_by_name[t[0].name] for t in tasks]


def stage_standalone_repository(
    staging_dir: Path,
    repo_slug: str,
    release_tag: str,
    pack_metadata: List[Dict[str, Any]],
) -> None:
    """Populate `staging_dir` with a clean, portable, secret-free standalone CAD repository."""
    if staging_dir.exists():
        import stat
        shutil.rmtree(staging_dir, onexc=lambda fn, p, _e: (os.chmod(p, stat.S_IWRITE), fn(p)))
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 1. Root scripts & config files
    for fname in ROOT_FILES:
        src = WORKSPACE_ROOT / fname
        if src.is_file():
            txt = src.read_text(encoding="utf-8")
            txt = txt.replace("ARES-23247/robotics-cad-workflow", repo_slug)
            (staging_dir / fname).write_text(txt, encoding="utf-8")

    (staging_dir / ".gitignore").write_text(STANDALONE_GITIGNORE, encoding="utf-8")
    (staging_dir / "README.md").write_text(
        STANDALONE_README.replace("__REPO_SLUG__", repo_slug),
        encoding="utf-8",
    )
    (staging_dir / "AGENTS.md").write_text(STANDALONE_AGENTS_MD, encoding="utf-8")

    # Sanitize WORKFLOW.md to remove team-specific BIOBUZZ/2910 references while keeping all engineering rules
    wf_text = (WORKSPACE_ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
    wf_text = wf_text.replace(
        "The recorded Nectar/Pollen fixed-shooter FreeCAD-master choice remains valid. Whole-robot and FRC redesign instructions can still require native final parts. ",
        "",
    )
    (staging_dir / "WORKFLOW.md").write_text(wf_text, encoding="utf-8")

    # 2. Reusable tools/
    tools_dst = staging_dir / "tools"
    tools_dst.mkdir(parents=True, exist_ok=True)
    for tname in INCLUDED_TOOLS:
        src = WORKSPACE_ROOT / "tools" / tname
        if src.is_file():
            shutil.copy2(src, tools_dst / tname)

    # 3. Plugins & .agents skills
    if (WORKSPACE_ROOT / "plugins").is_dir():
        shutil.copytree(WORKSPACE_ROOT / "plugins", staging_dir / "plugins")
    if (WORKSPACE_ROOT / ".agents").is_dir():
        shutil.copytree(WORKSPACE_ROOT / ".agents", staging_dir / ".agents")

    # 4. COTS SQLite DB, agent index, thumbnails, catalogs, & release manifest
    cots_dst = staging_dir / "cots"
    (cots_dst / "solidcraft").mkdir(parents=True, exist_ok=True)
    for cfile in ("catalog.db", "cots_manifest.json", "cots_agent_index.jsonl", "COTS_AGENT_GUIDE.md"):
        src = COTS_DIR / cfile
        if src.is_file():
            shutil.copy2(src, cots_dst / cfile)

    for sc_doc in ("CATALOG.md", "index.html"):
        src = COTS_DIR / "solidcraft" / sc_doc
        if src.is_file():
            txt = src.read_text(encoding="utf-8", errors="ignore")
            # Replace machine-local absolute workspace prefix with portable placeholder (re-stamped on install.ps1)
            ws_posix = WORKSPACE_ROOT.resolve().as_posix()
            txt = txt.replace(ws_posix, "<workspace>")
            (cots_dst / "solidcraft" / sc_doc).write_text(txt, encoding="utf-8")

    thumbs_src = COTS_DIR / "solidcraft" / "thumbs"
    if thumbs_src.is_dir():
        shutil.copytree(thumbs_src, cots_dst / "solidcraft" / "thumbs")

    release_manifest = {
        "repository": repo_slug,
        "release_tag": release_tag,
        "total_packs": len(pack_metadata),
        "total_compressed_bytes": sum(p["size_bytes"] for p in pack_metadata),
        "total_uncompressed_bytes": sum(p["uncompressed_bytes"] for p in pack_metadata),
        "packs": [
            {
                "name": p["name"],
                "size_bytes": p["size_bytes"],
                "uncompressed_bytes": p["uncompressed_bytes"],
                "file_count": p["file_count"],
                "sha256": p["sha256"],
                "url": f"https://github.com/{repo_slug}/releases/download/{release_tag}/{p['name']}",
            }
            for p in pack_metadata
        ],
    }
    (cots_dst / "cots_release_manifest.json").write_text(
        json.dumps(release_manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    # 5. Clean starter example project (zero custom team robot IP)
    starter_dir = staging_dir / "projects" / "starter-bearing-block"
    (starter_dir / "src").mkdir(parents=True, exist_ok=True)
    (starter_dir / "README.md").write_text(STARTER_PROJECT_README, encoding="utf-8")
    (starter_dir / "src" / "build_bearing_block.py").write_text(STARTER_PROJECT_SCRIPT, encoding="utf-8")


def audit_staged_repository(staging_dir: Path) -> Dict[str, Any]:
    """Verify zero personal secrets, zero hardcoded user paths, and zero custom robot projects."""
    forbidden_patterns = [
        re.compile(r"on_IM1h", re.IGNORECASE),
        re.compile(r"mYTqu2z", re.IGNORECASE),
        re.compile(r"Users[/\\]+david", re.IGNORECASE),
    ]
    violations: List[str] = []
    text_exts = {".py", ".md", ".json", ".jsonl", ".html", ".ps1", ".sh", ".txt", ".example", ".svg"}

    for fp in staging_dir.rglob("*"):
        if not fp.is_file():
            continue
        if fp.suffix.lower() in text_exts or fp.name in (".gitignore", ".gitattributes", ".env.example"):
            # Skip scanning this audit function's own regex literals
            if fp.name == "package_shareable_release.py":
                continue
            txt = fp.read_text(encoding="utf-8", errors="ignore")
            for pat in forbidden_patterns:
                m = pat.search(txt)
                if m:
                    violations.append(f"{fp.relative_to(staging_dir)}: matched '{m.group(0)}'")

    proj_subdirs = [p.name for p in (staging_dir / "projects").iterdir() if p.is_dir()]
    if proj_subdirs != ["starter-bearing-block"]:
        violations.append(f"Unexpected project folders in staging: {proj_subdirs}")

    if violations:
        raise RuntimeError("Security / Portability audit FAILED:\n  - " + "\n  - ".join(violations))

    return {
        "status": "PASS",
        "projects_included": proj_subdirs,
        "staged_files": sum(1 for _ in staging_dir.rglob("*") if _.is_file()),
    }


def publish_to_github(
    staging_dir: Path,
    packs_dir: Path,
    repo_slug: str,
    release_tag: str,
    private: bool = False,
    upload_packs: bool = True,
) -> Dict[str, Any]:
    """Initialize git in `staging_dir`, create/push GitHub repo, and optionally upload `release_tag` packs."""
    subprocess.run(["git", "init", "-b", "main"], cwd=staging_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=staging_dir, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: turnkey Robotics CAD Workflow + 2,588-part COTS Library installer"],
        cwd=staging_dir,
        check=True,
        capture_output=True,
        text=True,
    )

    vis_flag = "--private" if private else "--public"
    view_res = subprocess.run(["gh", "repo", "view", repo_slug], capture_output=True, text=True, check=False)
    if view_res.returncode != 0:
        logger.info("Creating GitHub repository %s (%s)...", repo_slug, vis_flag)
        subprocess.run(
            [
                "gh",
                "repo",
                "create",
                repo_slug,
                vis_flag,
                "--description",
                "Turnkey Robotics CAD Workflow: SolidCraft + build123d + 2,588 FTC/FRC COTS Library + Antigravity AI Plugin & MCP",
                "--source",
                str(staging_dir),
                "--remote",
                "origin",
                "--push",
            ],
            check=True,
        )
    else:
        remote_url = f"https://github.com/{repo_slug}.git"
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=staging_dir, check=False)
        subprocess.run(["git", "push", "-u", "origin", "main", "--force"], cwd=staging_dir, check=True)

    uploaded_names: List[str] = []
    if upload_packs:
        # Create or update GitHub Release and upload COTS .zip shards
        zip_files = sorted(packs_dir.glob("cots-pack-*.zip"))
        rel_check = subprocess.run(
            ["gh", "release", "view", release_tag, "-R", repo_slug],
            capture_output=True,
            text=True,
            check=False,
        )
        notes = (
            "Automated COTS Hardware CAD Library packs (2,588 verified FTC/FRC `.step` models + "
            "coordinate-normalized SolidCraft Format-52 `.sc` components). "
            "Downloaded and SHA-256 verified automatically by `install.ps1`."
        )
        if rel_check.returncode != 0:
            logger.info("Creating GitHub Release %s on %s...", release_tag, repo_slug)
            subprocess.run(
                ["gh", "release", "create", release_tag, "-R", repo_slug, "--title", f"Robotics COTS CAD Library ({release_tag})", "--notes", notes],
                check=True,
            )

        for idx, zf in enumerate(zip_files, 1):
            logger.info("Uploading release asset [%d/%d] %s (%.1f MB)...", idx, len(zip_files), zf.name, zf.stat().st_size / (1024 * 1024))
            subprocess.run(
                ["gh", "release", "upload", release_tag, str(zf), "-R", repo_slug, "--clobber"],
                check=True,
            )
            uploaded_names.append(zf.name)

    return {
        "repository_url": f"https://github.com/{repo_slug}",
        "release_url": f"https://github.com/{repo_slug}/releases/tag/{release_tag}",
        "uploaded_packs": uploaded_names,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Package & publish shareable Robotics CAD Workflow + COTS Library")
    parser.add_argument("--staging-dir", type=str, required=True, help="Directory for staged clean repository")
    parser.add_argument("--packs-dir", type=str, default=None, help="Directory for built COTS .zip release shards")
    parser.add_argument("--repo", type=str, default="ARES-23247/robotics-cad-workflow", help="Target GitHub owner/repo")
    parser.add_argument("--tag", type=str, default="v1.0.0", help="Release tag for COTS packs")
    parser.add_argument("--workers", type=int, default=10, help="Parallel compression workers")
    parser.add_argument("--code-only", action="store_true", help="Sync code/tools/docs only, reusing existing COTS release manifest")
    parser.add_argument("--private", action="store_true", help="Create private GitHub repo instead of public")
    parser.add_argument("--publish", action="store_true", help="Create/push GitHub repo and upload release packs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    staging_dir = Path(args.staging_dir).resolve()
    packs_dir = Path(args.packs_dir).resolve() if args.packs_dir else (staging_dir.parent / "cots_release_packs")

    if args.code_only:
        existing_manifest = staging_dir / "cots" / "cots_release_manifest.json"
        if existing_manifest.is_file():
            pack_meta = json.loads(existing_manifest.read_text(encoding="utf-8"))["packs"]
        else:
            import urllib.request
            raw_url = f"https://raw.githubusercontent.com/{args.repo}/main/cots/cots_release_manifest.json"
            with urllib.request.urlopen(raw_url, timeout=30) as resp:
                pack_meta = json.loads(resp.read().decode("utf-8"))["packs"]
    else:
        pack_meta = build_cots_release_packs(packs_dir, workers=args.workers)

    stage_standalone_repository(staging_dir, args.repo, args.tag, pack_meta)
    audit_res = audit_staged_repository(staging_dir)
    logger.info("Staged repository audit: %s", audit_res)

    pub_res = None
    if args.publish:
        pub_res = publish_to_github(
            staging_dir,
            packs_dir,
            args.repo,
            args.tag,
            private=args.private,
            upload_packs=not args.code_only,
        )

    print(
        json.dumps(
            {
                "status": "ok",
                "staging_dir": str(staging_dir),
                "packs_dir": str(packs_dir),
                "pack_count": len(pack_meta),
                "total_compressed_mb": round(sum(p["size_bytes"] for p in pack_meta) / (1024 * 1024), 1),
                "audit": audit_res,
                "published": pub_res,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
