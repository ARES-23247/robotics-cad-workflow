r"""
One-Command Cross-Computer Setup & Doctor for the Robotics CAD Workflow
=======================================================================
Run after cloning (or via `.\install.ps1` / `./install.sh`) on any machine:

    .\install.ps1
    # or directly:
    .venv\Scripts\python.exe tools/setup_solidcraft_cots.py --no-build

What it does:
1. Ensures `SolidCraft` (`v0.40.0+`) is installed. On Windows/macOS/Linux, if
   `solidcraft-cli` is missing or older than `0.40.0`, downloads and installs
   the latest `v0.40.0+` release from `tangency-labs/solidcraft` automatically
   (user-space portable install into `%LOCALAPPDATA%\Programs\SolidCraft` with
   Desktop/Start Menu shortcuts and `PATH` registration—zero UAC prompt needed).
2. Ensures the 2,588-part Robotics COTS library (`cots/vendors/` + `cots/solidcraft/`)
   is populated—downloading & SHA-256 verifying release archive packs from
   `cots/cots_release_manifest.json` when starting from the lightweight starter
   repository—and decompresses any `>95 MiB` `.gz` archives.
3. Regenerates `cots/solidcraft/index.html` and `cots/solidcraft/CATALOG.md` so
   interactive browser paths match this computer's checkout path.
4. Registers `solidcraft`, `solidcraft-live`, and `ares_onshape_local` MCP servers
   in Antigravity (`~/.gemini/config/mcp_config.json`), Gemini CLI
   (`~/.gemini/settings.json`), and desktop AI clients (`solidcraft-cli mcp setup --live`).
5. Installs the Antigravity AI Plugin (`~/.gemini/config/plugins/solidcraft-robotics-cad`),
   the SolidCraft Desktop Plugin (`ares-robotics-cots` + `Alt+C` HWND HUD launcher),
   and agent skills (`solidcraft-cots` & `cad-workspace`) across Antigravity,
   Claude Code, and Codex.
6. Creates `.env` from `.env.example` if missing, and runs a full end-to-end
   health verification (`run_doctor()`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
import zipfile

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.solidcraft_cots import (
    COTS_DIR,
    DB_PATH,
    SOLIDCRAFT_LIB_DIR,
    build_all_solidcraft_cots,
    decompress_missing_gz_archives,
    find_solidcraft_cli,
    generate_human_catalog,
)

logger = logging.getLogger("setup_solidcraft_cots")

MIN_SOLIDCRAFT_VERSION = (0, 40, 0)
SOLIDCRAFT_RELEASES_API = "https://api.github.com/repos/tangency-labs/solidcraft/releases?per_page=15"
RELEASE_MANIFEST_PATH = COTS_DIR / "cots_release_manifest.json"


def _detect_venv_python() -> str:
    if platform.system() == "Windows":
        venv_py = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = WORKSPACE_ROOT / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py.resolve())
    return sys.executable


def _parse_semver(version_text: str) -> Tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _get_solidcraft_version(cli_path: str) -> Tuple[Tuple[int, int, int], str]:
    try:
        res = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        raw = (res.stdout or res.stderr or "").strip()
        return _parse_semver(raw), raw
    except Exception as exc:
        return (0, 0, 0), f"error: {exc}"


def _download_with_progress(url: str, dest: Path, expected_sha256: Optional[str] = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "robotics-cad-workflow-installer/1.1"}
    req = urllib.request.Request(url, headers=headers)
    hasher = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out_f:
        total = int(resp.headers.get("Content-Length") or 0)
        last_pct = -5
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out_f.write(chunk)
            hasher.update(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int((downloaded * 100) / total)
                if pct >= last_pct + 10 or downloaded == total:
                    last_pct = pct
                    logger.info(
                        "  Downloading %s: %d%% (%.1f / %.1f MB)",
                        dest.name,
                        pct,
                        downloaded / (1024 * 1024),
                        total / (1024 * 1024),
                    )
    if expected_sha256:
        actual = hasher.hexdigest().lower()
        if actual != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for {dest.name}: expected {expected_sha256}, got {actual}"
            )


def _add_to_windows_user_path(dir_path: Path) -> None:
    if platform.system() != "Windows":
        return
    target = str(dir_path.resolve())
    cur_env = os.environ.get("PATH", "")
    if target.lower() not in [p.strip().lower() for p in cur_env.split(";") if p.strip()]:
        os.environ["PATH"] = f"{target};{cur_env}"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                existing, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                existing, reg_type = "", winreg.REG_EXPAND_SZ
            parts = [p.strip() for p in str(existing).split(";") if p.strip()]
            if target.lower() not in [p.lower() for p in parts]:
                parts.append(target)
                winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(parts))
    except Exception as exc:
        logger.warning("Could not persist User PATH update (%s); current session PATH is updated.", exc)


def _create_windows_shortcuts(sc_exe: Path, workspace_root: Path) -> List[str]:
    if platform.system() != "Windows" or not sc_exe.is_file():
        return []
    created: List[str] = []
    appdata = os.environ.get("APPDATA", "")
    start_menu_dir = (
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if appdata
        else None
    )
    if not start_menu_dir or not start_menu_dir.is_dir():
        return []

    py_exe = _detect_venv_python()
    plugin_py = workspace_root / "tools" / "solidcraft_plugin.py"
    lnk_sc = start_menu_dir / "SolidCraft.lnk"
    lnk_hud = start_menu_dir / "SolidCraft + Robotics COTS HUD.lnk"

    ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$s1 = $ws.CreateShortcut('{str(lnk_sc).replace("'", "''")}')
$s1.TargetPath = '{str(sc_exe).replace("'", "''")}'
$s1.WorkingDirectory = '{str(workspace_root).replace("'", "''")}'
$s1.Description = 'SolidCraft Parametric 3D CAD'
$s1.Save()

$s2 = $ws.CreateShortcut('{str(lnk_hud).replace("'", "''")}')
$s2.TargetPath = '{str(py_exe).replace("'", "''")}'
$s2.Arguments = '"{str(plugin_py)}" run'
$s2.WorkingDirectory = '{str(workspace_root).replace("'", "''")}'
$s2.IconLocation = '{str(sc_exe).replace("'", "''")},0'
$s2.Description = 'Launch SolidCraft with the Robotics COTS Hardware HUD (Alt+C)'
$s2.Save()
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        for p in (lnk_sc, lnk_hud):
            if p.is_file():
                created.append(str(p))
    except Exception as exc:
        logger.warning("Shortcut creation skipped: %s", exc)
    return created


def ensure_solidcraft_installed(auto_install: bool = True) -> Dict[str, Any]:
    """Locate SolidCraft `v0.40.0+` or automatically download & install it in user space."""
    existing_cli: Optional[str] = None
    existing_ver = (0, 0, 0)
    existing_raw = ""
    try:
        existing_cli = find_solidcraft_cli()
        existing_ver, existing_raw = _get_solidcraft_version(existing_cli)
        if existing_ver >= MIN_SOLIDCRAFT_VERSION:
            return {
                "action": "already_installed",
                "cli_path": existing_cli,
                "version": existing_raw,
            }
        logger.info(
            "Found older SolidCraft (%s at %s); upgrading to >= 0.40.0 for Format 52 COTS support...",
            existing_raw,
            existing_cli,
        )
    except FileNotFoundError:
        if not auto_install:
            raise
        logger.info("SolidCraft not found on this machine; downloading SolidCraft v0.40.0+ automatically...")

    if not auto_install:
        if existing_cli:
            return {"action": "kept_existing", "cli_path": existing_cli, "version": existing_raw}
        raise FileNotFoundError("SolidCraft CLI not installed and auto_install=False.")

    req = urllib.request.Request(
        SOLIDCRAFT_RELEASES_API,
        headers={"User-Agent": "robotics-cad-workflow-installer/1.1", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))

    # Pick highest semver release (including v0.40.0+ RC/pre-releases if needed)
    best_rel = None
    best_ver = (0, 0, 0)
    for rel in releases:
        if rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        if "nightly" in tag.lower():
            continue
        ver = _parse_semver(tag)
        if ver > best_ver:
            best_ver = ver
            best_rel = rel

    if not best_rel:
        raise RuntimeError("Could not find a suitable SolidCraft release on tangency-labs/solidcraft.")

    sys_name = platform.system()
    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine
    assets = {a["name"]: a["browser_download_url"] for a in best_rel.get("assets", [])}

    if sys_name == "Windows":
        target_name = "solidcraft-windows-arm64-portable.zip" if is_arm else "solidcraft-windows-x64-portable.zip"
        asset_url = assets.get(target_name)
        if not asset_url:
            for k, v in assets.items():
                if "windows" in k.lower() and ("arm64" if is_arm else "x64") in k.lower() and k.endswith("portable.zip"):
                    target_name, asset_url = k, v
                    break
        if not asset_url:
            raise RuntimeError(f"Could not locate Windows portable zip in release {best_rel.get('tag_name')}")

        install_dir = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Programs" / "SolidCraft"
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="solidcraft_dl_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / target_name
            logger.info("Installing SolidCraft %s -> %s", best_rel.get("tag_name"), install_dir)
            _download_with_progress(asset_url, zip_path)
            extract_dir = tmp_path / "extracted"
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
            found_cli = next(extract_dir.rglob("solidcraft-cli.exe"), None)
            if not found_cli:
                raise RuntimeError(f"solidcraft-cli.exe not found inside {target_name}")
            src_root = found_cli.parent
            for item in src_root.iterdir():
                dst = install_dir / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)

        _add_to_windows_user_path(install_dir)
        shortcuts = _create_windows_shortcuts(install_dir / "solidcraft.exe", WORKSPACE_ROOT)
        cli_final = str((install_dir / "solidcraft-cli.exe").resolve())
        _, ver_str = _get_solidcraft_version(cli_final)
        return {
            "action": "installed_portable",
            "release_tag": best_rel.get("tag_name"),
            "cli_path": cli_final,
            "version": ver_str,
            "shortcuts": shortcuts,
        }

    elif sys_name == "Darwin":
        cli_zip_url = next((v for k, v in assets.items() if "solidcraft-cli" in k and "macos" in k and k.endswith(".zip")), None)
        if not cli_zip_url:
            raise RuntimeError("macOS solidcraft-cli archive not found; install SolidCraft from https://solidcraft.io/download")
        install_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="solidcraft_dl_") as tmp_dir:
            zip_path = Path(tmp_dir) / "solidcraft-cli-macos.zip"
            _download_with_progress(cli_zip_url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(Path(tmp_dir) / "ext")
            found_cli = next((Path(tmp_dir) / "ext").rglob("solidcraft-cli"), None)
            if not found_cli:
                raise RuntimeError("solidcraft-cli binary not found in macOS zip")
            dst_cli = install_dir / "solidcraft-cli"
            shutil.copy2(found_cli, dst_cli)
            dst_cli.chmod(0o755)
        _, ver_str = _get_solidcraft_version(str(dst_cli))
        return {"action": "installed_cli", "release_tag": best_rel.get("tag_name"), "cli_path": str(dst_cli), "version": ver_str}

    else:
        arch_tag = "aarch64" if is_arm else "x86_64"
        tar_url = next((v for k, v in assets.items() if f"linux-{arch_tag}.tar.gz" in k), None)
        if not tar_url:
            raise RuntimeError("Linux SolidCraft tar.gz not found; install from https://solidcraft.io/download")
        import tarfile

        install_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="solidcraft_dl_") as tmp_dir:
            tgz = Path(tmp_dir) / "solidcraft-linux.tar.gz"
            _download_with_progress(tar_url, tgz)
            with tarfile.open(tgz, "r:gz") as tf:
                tf.extractall(Path(tmp_dir) / "ext")
            for bin_name in ("solidcraft", "solidcraft-cli", "solvecraft-cli"):
                found = next((Path(tmp_dir) / "ext").rglob(bin_name), None)
                if found and found.is_file():
                    dst = install_dir / bin_name
                    shutil.copy2(found, dst)
                    dst.chmod(0o755)
        cli_final = str(install_dir / "solidcraft-cli")
        _, ver_str = _get_solidcraft_version(cli_final)
        return {"action": "installed_tar", "release_tag": best_rel.get("tag_name"), "cli_path": cli_final, "version": ver_str}


def ensure_cots_library_populated(skip_download: bool = False) -> Dict[str, Any]:
    """
    Ensure the 2,588-part COTS STEP + SolidCraft `.sc` library is present locally.
    If local STEP/SC files are already present (e.g. full workspace or already installed),
    returns immediately. Otherwise downloads and verifies archive packs listed in
    `cots/cots_release_manifest.json` from GitHub Releases.
    """
    sc_count = sum(1 for _ in SOLIDCRAFT_LIB_DIR.rglob("*.sc")) if SOLIDCRAFT_LIB_DIR.exists() else 0
    step_count = sum(
        1
        for p in COTS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in (".step", ".stp") and "solidcraft" not in p.parts
    ) if COTS_DIR.exists() else 0

    if sc_count >= 2500 and step_count >= 2500:
        return {
            "action": "already_present",
            "step_files": step_count,
            "sc_files": sc_count,
        }

    if skip_download:
        return {
            "action": "skipped_by_flag",
            "step_files": step_count,
            "sc_files": sc_count,
        }

    if not RELEASE_MANIFEST_PATH.is_file():
        return {
            "action": "no_release_manifest",
            "step_files": step_count,
            "sc_files": sc_count,
        }

    manifest = json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
    packs = manifest.get("packs") or []
    repo_slug = manifest.get("repository", "")
    tag = manifest.get("release_tag", "v1.0.0")
    downloaded_packs: List[str] = []

    marker_dir = COTS_DIR / ".installed_packs"
    marker_dir.mkdir(parents=True, exist_ok=True)

    for pack in packs:
        name = pack["name"]
        sha256 = pack.get("sha256")
        url = pack.get("url") or f"https://github.com/{repo_slug}/releases/download/{tag}/{name}"
        marker = marker_dir / f"{name}.{sha256[:12] if sha256 else 'ok'}.done"
        if marker.is_file():
            continue

        with tempfile.TemporaryDirectory(prefix="cots_pack_") as tmp_dir:
            zip_file = Path(tmp_dir) / name
            logger.info("Fetching COTS library pack %s ...", name)
            _download_with_progress(url, zip_file, expected_sha256=sha256)
            logger.info("Extracting %s into %s ...", name, WORKSPACE_ROOT)
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extractall(WORKSPACE_ROOT)
        marker.write_text("installed\n", encoding="utf-8")
        downloaded_packs.append(name)

    sc_after = sum(1 for _ in SOLIDCRAFT_LIB_DIR.rglob("*.sc")) if SOLIDCRAFT_LIB_DIR.exists() else 0
    step_after = sum(
        1
        for p in COTS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in (".step", ".stp") and "solidcraft" not in p.parts
    )
    return {
        "action": "downloaded_release_packs" if downloaded_packs else "packs_already_extracted",
        "downloaded_packs": downloaded_packs,
        "step_files": step_after,
        "sc_files": sc_after,
    }


def _patch_json_mcp_config(
    config_path: Path,
    cli_path: str,
    python_path: str,
    workspace_root: Path,
) -> bool:
    """Update `mcpServers` in an Antigravity or Gemini CLI JSON config file without clobbering sibling keys."""
    data: Dict[str, Any] = {}
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse %s (%s); starting fresh mcpServers block.", config_path, exc)
            data = {}

    mcp_servers = data.setdefault("mcpServers", {})
    ws_str = str(workspace_root.resolve())
    cad_server_py = str((workspace_root / "tools" / "cad_mcp_server.py").resolve())

    allow_paths = [ws_str]
    printing_dir = workspace_root.parent.parent / "3d-printing"
    if printing_dir.is_dir():
        allow_paths.append(str(printing_dir.resolve()))

    sc_args = ["--max-file-size", "2GB", "mcp", "--headless"]
    for ap in allow_paths:
        sc_args.extend(["--allow-path", ap])

    sc_live_args = ["--max-file-size", "2GB", "mcp", "--connect", "127.0.0.1:27878"]
    for ap in allow_paths:
        sc_live_args.extend(["--allow-path", ap])

    mcp_servers["solidcraft"] = {
        "command": cli_path,
        "args": sc_args,
        "disabled": False,
    }
    mcp_servers["solidcraft-live"] = {
        "command": cli_path,
        "args": sc_live_args,
        "disabled": False,
    }
    mcp_servers["ares_onshape_local"] = {
        "command": python_path,
        "args": [cad_server_py],
        "cwd": ws_str,
        "env": {
            "PYTHONUNBUFFERED": "1",
            "ARES_ONSHAPE_WORKSPACE": ws_str,
        },
        "disabled": False,
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def install_agent_skills(workspace_root: Path, home_dir: Path) -> List[str]:
    """Install `solidcraft-cots` and workspace-locator `cad-workspace` skills across AI clients."""
    installed: List[str] = []
    src_skill = workspace_root / ".agents" / "skills" / "solidcraft-cots" / "SKILL.md"
    if src_skill.is_file():
        targets = [
            home_dir / ".gemini" / "config" / "skills" / "solidcraft-cots" / "SKILL.md",
            home_dir / ".claude" / "skills" / "solidcraft-cots" / "SKILL.md",
            home_dir / ".codex" / "skills" / "solidcraft-cots" / "SKILL.md",
        ]
        for dst in targets:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_skill, dst)
                installed.append(str(dst))
            except Exception as exc:
                logger.warning("Could not install skill to %s: %s", dst, exc)

    ws_posix = workspace_root.resolve().as_posix()
    cad_ws_skill = f"""---
name: cad-workspace
description: Locate the maintained robotics CAD workspace, modeling routes, COTS hardware catalog, verification rules, and GitHub sync workflow in {ws_posix}.
---

# Robotics CAD workspace

Use the self-contained workspace at `{ws_posix}`.

1. Read [workspace instructions]({ws_posix}/AGENTS.md) and [modeling workflow]({ws_posix}/WORKFLOW.md).
2. Use the [folder map]({ws_posix}/README.md), then read `projects/<project>/docs/AGENTS.md` when present.
3. Work in `projects/<project>/src`, `cad`, `fabrication/prints`, `fabrication/laser`, and `docs`; use shared `cots/` and `tools/`. Keep all 3D-printed parts and ready-to-print build plates (`.stl`, `.3mf`) inside `projects/<project>/fabrication/prints/`. Never store or mirror files in `~/Documents` or `~/OneDrive`.
"""
    for client_dir in (
        home_dir / ".codex" / "skills" / "cad-workspace" / "SKILL.md",
        home_dir / ".claude" / "skills" / "cad-workspace" / "SKILL.md",
    ):
        try:
            client_dir.parent.mkdir(parents=True, exist_ok=True)
            client_dir.write_text(cad_ws_skill, encoding="utf-8")
            installed.append(str(client_dir))
        except Exception as exc:
            logger.warning("Could not write cad-workspace skill to %s: %s", client_dir, exc)

    return installed


def ensure_dotenv_initialized(workspace_root: Path) -> Optional[str]:
    env_file = workspace_root / ".env"
    env_example = workspace_root / ".env.example"
    if not env_file.exists() and env_example.is_file():
        shutil.copy2(env_example, env_file)
        return str(env_file)
    return None


def run_doctor(home_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Run deterministic health checks across Python/OCP, SolidCraft CLI, COTS catalog, and MCP."""
    home = home_dir or Path.home()
    checks: Dict[str, Any] = {}
    all_ok = True

    # 1. Python + build123d + OCP kernel
    try:
        from build123d import Box

        b = Box(10.0, 10.0, 10.0)
        vol_ok = abs(float(b.volume) - 1000.0) < 1e-3
        checks["python_build123d_ocp"] = {"ok": vol_ok, "python": sys.executable, "test_box_volume_mm3": round(float(b.volume), 2)}
        all_ok = all_ok and vol_ok
    except Exception as exc:
        checks["python_build123d_ocp"] = {"ok": False, "error": str(exc)}
        all_ok = False

    # 2. SolidCraft CLI >= 0.40.0
    try:
        cli_path = find_solidcraft_cli()
        ver_tuple, ver_raw = _get_solidcraft_version(cli_path)
        sc_ok = ver_tuple >= MIN_SOLIDCRAFT_VERSION
        checks["solidcraft_cli"] = {"ok": sc_ok, "cli_path": cli_path, "version": ver_raw}
        all_ok = all_ok and sc_ok
    except Exception as exc:
        checks["solidcraft_cli"] = {"ok": False, "error": str(exc)}
        all_ok = False

    # 3. COTS SQLite DB + local .step & .sc part presence
    try:
        from tools.ag_cad_helpers import resolve_cots_step_path
        from tools.cad_scraper import cots_info

        sample = cots_info("1611-0514-0008").get("local") or {}
        step_fp = resolve_cots_step_path("1611-0514-0008")
        rel_sc = sample.get("solidcraft_path") or ""
        sc_fp = (WORKSPACE_ROOT / rel_sc) if rel_sc.startswith("cots/") else (COTS_DIR / rel_sc)
        cots_ok = bool(DB_PATH.is_file() and step_fp.is_file() and sc_fp.is_file())
        checks["cots_catalog_and_models"] = {
            "ok": cots_ok,
            "db_exists": DB_PATH.is_file(),
            "sample_sku": "1611-0514-0008",
            "sample_step_exists": step_fp.is_file(),
            "sample_sc_exists": sc_fp.is_file() if rel_sc else False,
        }
        all_ok = all_ok and cots_ok
    except Exception as exc:
        checks["cots_catalog_and_models"] = {"ok": False, "error": str(exc)}
        all_ok = False

    # 4. CAD MCP Server JSON-RPC protocol & tool count
    try:
        from tools.cad_mcp_server import handle_request

        list_resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools_list = (list_resp or {}).get("result", {}).get("tools", [])
        mcp_ok = len(tools_list) >= 18
        checks["cad_mcp_server"] = {"ok": mcp_ok, "tool_count": len(tools_list)}
        all_ok = all_ok and mcp_ok
    except Exception as exc:
        checks["cad_mcp_server"] = {"ok": False, "error": str(exc)}
        all_ok = False

    # 5. Antigravity plugin & MCP config files
    ag_plugin_manifest = home / ".gemini" / "config" / "plugins" / "solidcraft-robotics-cad" / "plugin.json"
    ag_mcp_cfg = home / ".gemini" / "config" / "mcp_config.json"
    cfg_ok = ag_plugin_manifest.is_file() and ag_mcp_cfg.is_file()
    checks["antigravity_integration"] = {
        "ok": cfg_ok,
        "plugin_manifest": str(ag_plugin_manifest),
        "mcp_config": str(ag_mcp_cfg),
    }
    all_ok = all_ok and cfg_ok

    return {"status": "PASS" if all_ok else "WARN", "checks": checks}


def run_setup(
    build_missing: bool = False,
    workers: int = 10,
    skip_cots_download: bool = False,
    skip_global_config: bool = False,
    home_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target_home = home_dir or Path.home()

    # 0. Initialize .env from .env.example if absent
    created_dotenv = ensure_dotenv_initialized(WORKSPACE_ROOT)

    # 1. Ensure SolidCraft v0.40.0+ is installed
    sc_install_info = ensure_solidcraft_installed(auto_install=True)
    cli_path = sc_install_info["cli_path"]
    py_path = _detect_venv_python()
    logger.info("Using solidcraft-cli: %s (%s)", cli_path, sc_install_info.get("version"))
    logger.info("Using Python: %s", py_path)

    # 2. Populate COTS library from GitHub Release packs if missing, then decompress .gz files
    cots_sync_info = ensure_cots_library_populated(skip_download=skip_cots_download)
    decompressed = decompress_missing_gz_archives(COTS_DIR) if COTS_DIR.exists() else 0
    logger.info("Decompressed %d .gz archive(s) in cots/.", decompressed)

    # 3. Regenerate local catalog HTML/Markdown so paths match this computer's checkout
    catalog_doc_info = None
    if DB_PATH.is_file():
        try:
            catalog_doc_info = generate_human_catalog(DB_PATH)
        except Exception as exc:
            logger.warning("Could not refresh catalog HTML: %s", exc)

    skill_paths: List[str] = []
    sc_plugin_info: Dict[str, Any] = {"status": "skipped_global"}
    ag_plugin_info: Dict[str, Any] = {"status": "skipped_global"}

    if not skip_global_config:
        # 4. Run solidcraft-cli mcp setup --live for Claude/Cursor/VS Code/Windsurf/OpenCode (only when targeting real home)
        if home_dir is None:
            try:
                subprocess.run([cli_path, "mcp", "setup", "--live"], check=False, capture_output=True, text=True, timeout=20)
            except Exception as exc:
                logger.warning("solidcraft-cli mcp setup warning: %s", exc)

        # 5. Patch Antigravity and Gemini CLI MCP configs with --allow-path and ares_onshape_local
        ag_primary_cfg = target_home / ".gemini" / "config" / "mcp_config.json"
        ag_cfg = target_home / ".gemini" / "antigravity" / "mcp_config.json"
        gem_cfg = target_home / ".gemini" / "settings.json"
        _patch_json_mcp_config(ag_primary_cfg, cli_path, py_path, WORKSPACE_ROOT)
        _patch_json_mcp_config(ag_cfg, cli_path, py_path, WORKSPACE_ROOT)
        _patch_json_mcp_config(gem_cfg, cli_path, py_path, WORKSPACE_ROOT)
        logger.info("Updated MCP configs under %s", target_home / ".gemini")

        # 6. Install agent skills + SolidCraft Desktop Plugin + Antigravity AI Plugin
        skill_paths = install_agent_skills(WORKSPACE_ROOT, target_home)
        if home_dir is None:
            from tools.solidcraft_plugin import install_antigravity_ai_plugin, install_solidcraft_desktop_plugin

            sc_plugin_info = install_solidcraft_desktop_plugin()
            ag_plugin_info = install_antigravity_ai_plugin(
                repository_url="https://github.com/ARES-23247/robotics-cad-workflow"
            )
        else:
            # Sandbox home_dir mode for isolated E2E testing
            from tools.solidcraft_plugin import install_antigravity_ai_plugin

            orig_home = os.environ.get("USERPROFILE")
            try:
                os.environ["USERPROFILE"] = str(target_home)
                os.environ["HOME"] = str(target_home)
                ag_plugin_info = install_antigravity_ai_plugin(
                    repository_url="https://github.com/ARES-23247/robotics-cad-workflow"
                )
            finally:
                if orig_home is not None:
                    os.environ["USERPROFILE"] = orig_home

    # 7. Optionally build any missing .sc files if requested
    build_summary = None
    if build_missing:
        build_summary = build_all_solidcraft_cots(workers=workers, force=False)

    sc_count = sum(1 for _ in SOLIDCRAFT_LIB_DIR.rglob("*.sc")) if SOLIDCRAFT_LIB_DIR.exists() else 0
    doctor_report = run_doctor(home_dir=target_home if not skip_global_config else None)

    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "created_dotenv": created_dotenv,
        "solidcraft": sc_install_info,
        "python": py_path,
        "cots_sync": cots_sync_info,
        "decompressed_gz_archives": decompressed,
        "solidcraft_sc_files_ready": sc_count,
        "catalog_docs": catalog_doc_info,
        "installed_skills": skill_paths,
        "solidcraft_desktop_plugin": sc_plugin_info,
        "antigravity_ai_plugin": ag_plugin_info,
        "build_summary": build_summary,
        "doctor": doctor_report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Turnkey Setup & Doctor for the Robotics CAD Workflow")
    parser.add_argument("--build-missing", action="store_true", help="Build any missing .sc files from STEP")
    parser.add_argument("--no-build", action="store_true", help="Skip building missing .sc files (default)")
    parser.add_argument("--workers", type=int, default=10, help="Parallel worker count if building .sc files")
    parser.add_argument("--skip-cots-download", action="store_true", help="Skip downloading COTS release packs")
    parser.add_argument("--skip-global-config", action="store_true", help="Do not modify user ~/.gemini or SolidCraft plugins")
    parser.add_argument("--home-dir", type=str, default=None, help="Override user home directory (for sandbox testing)")
    parser.add_argument("--doctor", action="store_true", help="Run health verification checks only")
    args = parser.parse_args()

    home_override = Path(args.home_dir).resolve() if args.home_dir else None
    if args.doctor:
        rep = run_doctor(home_dir=home_override)
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["status"] == "PASS" else 1)

    res = run_setup(
        build_missing=bool(args.build_missing and not args.no_build),
        workers=args.workers,
        skip_cots_download=args.skip_cots_download,
        skip_global_config=args.skip_global_config,
        home_dir=home_override,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
