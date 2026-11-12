#!/usr/bin/env python3
"""
SolidCraft Native Plugin & Win32 HWND-Attached HUD Overlay (`ares-robotics-cots`)
=================================================================================

Integrates the 2,588-part Robotics COTS Library & Project Assembly Compiler as:
1. A native SolidCraft Desktop Plugin (`plugins/ares-robotics-cots/plugin.json`)
   registered in **SolidCraft -> Preferences -> Plugins**, respecting the native
   `"disabled_plugins"` toggle in `%APPDATA%\\SolidCraft\\data\\app.ron`.
2. A Win32 `HWND`-attached HUD overlay that docks directly to the right edge of
   the live SolidCraft window, moves/resizes/minimizes with SolidCraft, toggles
   open/closed via `Alt+C`, and supports 1-click live COTS insertion & concentric
   bore auto-mating over `%LOCALAPPDATA%\\SolidCraft\\control\\current.port` (`7878`).
3. An Antigravity AI Plugin (`~/.gemini/config/plugins/solidcraft-robotics-cad`)
   packaging the skills, rules, and MCP/CLI workflows for AI agents.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

logger = logging.getLogger("solidcraft_plugin")

PLUGIN_ID = "ares-robotics-cots"
PLUGIN_MANIFEST = {
    "id": PLUGIN_ID,
    "name": "ARES Robotics COTS Hardware & Assembly Compiler",
    "version": "1.0.0",
    "description": "2,588 FTC/FRC COTS parts (goBILDA, REV, Swyft, ThriftyBot, McMaster, ISO) with live 1-click insertion, concentric bore auto-mate, and jointed assembly compilation.",
}


def get_solidcraft_app_ron_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "SolidCraft" / "data" / "app.ron"


def get_solidcraft_plugin_dirs() -> List[Path]:
    """Return all candidate SolidCraft `plugins/` directories plus workspace master."""
    dirs: List[Path] = [WORKSPACE_ROOT / "plugins"]
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        dirs.append(Path(appdata) / "SolidCraft" / "plugins")
        dirs.append(Path(appdata) / "SolidCraft" / "data" / "plugins")
    if localappdata:
        dirs.append(Path(localappdata) / "SolidCraft" / "plugins")
    return dirs



def send_solidcraft_control_rpc(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    port: Optional[int] = None,
    timeout: float = 1.5,
) -> Dict[str, Any]:
    r"""
    Send a direct 0.5-ms JSON-line control request (`ui.prefs`, `ui.selection`,
    `document.inspect`, `engine.execute`, etc.) to the running SolidCraft app over
    loopback TCP using `%LOCALAPPDATA%\SolidCraft\control\<port>.token`.
    Supports both fixed `--control 7878` and SolidCraft v0.40+ default `27878` (`current.port`).
    """
    import socket

    localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    ctrl_dir = Path(localappdata) / "SolidCraft" / "control"
    actual_port = port or detect_live_solidcraft_port(27878)
    token_file = ctrl_dir / f"{actual_port}.token"
    if not token_file.is_file():
        detected = detect_live_solidcraft_port(actual_port)
        if detected != actual_port and (ctrl_dir / f"{detected}.token").is_file():
            actual_port = detected
            token_file = ctrl_dir / f"{actual_port}.token"
        elif (ctrl_dir / "27878.token").is_file():
            actual_port = 27878
            token_file = ctrl_dir / "27878.token"
        elif (ctrl_dir / "7878.token").is_file():
            actual_port = 7878
            token_file = ctrl_dir / "7878.token"
    if not token_file.is_file():
        return {"ok": False, "error": f"Token file not found: {token_file}"}
    token = token_file.read_text(encoding="utf-8").strip()
    payload = {
        "id": 1,
        "token": token,
        "method": method,
        "params": params or {},
    }
    try:
        with socket.create_connection(("127.0.0.1", actual_port), timeout=timeout) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            with sock.makefile("r", encoding="utf-8", errors="replace") as f:
                line = f.readline()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            return json.loads(line) if line else {"ok": False, "error": "empty reply"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def is_plugin_enabled_in_solidcraft(plugin_id: str = PLUGIN_ID, use_live_rpc: bool = False) -> bool:
    """
    Read `%APPDATA%\\SolidCraft\\data\\app.ron` (`solidcraft.prefs` -> `disabled_plugins`)
    to check whether the user has enabled/disabled the plugin inside
    `SolidCraft -> Preferences -> Plugins`.
    """
    # 1. Optionally query live in-memory SolidCraft preferences via 0.5ms TCP control channel
    if use_live_rpc:
        live_prefs = send_solidcraft_control_rpc("ui.prefs", {}, timeout=0.6)
        if live_prefs.get("ok") and isinstance(live_prefs.get("result"), dict):
            disabled = live_prefs["result"].get("disabled_plugins") or []
            return plugin_id not in disabled

    # 2. Fast disk check on app.ron (avoids opening a TCP connection every second in _win32_loop)
    ron_path = get_solidcraft_app_ron_path()
    if not ron_path.is_file():
        return True
    try:
        text = ron_path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'"disabled_plugins":\s*\[(.*?)\]', text)
        if not m:
            return True
        raw_list = m.group(1)
        return f'"{plugin_id}"' not in raw_list and f'\\"{plugin_id}\\"' not in raw_list
    except Exception:
        return True


def detect_live_solidcraft_port(default_port: int = 27878) -> int:
    """Detect the active SolidCraft control port from `%LOCALAPPDATA%\\SolidCraft\\control`."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        ctrl_dir = Path(localappdata) / "SolidCraft" / "control"
        cur_port_file = ctrl_dir / "current.port"
        if cur_port_file.is_file():
            try:
                p = int(cur_port_file.read_text(encoding="utf-8").strip())
                if p > 0:
                    return p
            except Exception:
                pass
        for pf in sorted(ctrl_dir.glob("*.port")):
            try:
                p = int(pf.read_text(encoding="utf-8").strip())
                if p > 0:
                    return p
            except Exception:
                pass
    return default_port


def install_solidcraft_desktop_plugin() -> Dict[str, Any]:
    """
    Install `ares-robotics-cots/plugin.json` into SolidCraft's native `plugins/`
    folders and create the auto-launcher wrappers.
    """
    installed_paths: List[str] = []
    manifest_bytes = (json.dumps(PLUGIN_MANIFEST, indent=2) + "\n").encode("utf-8")

    for pdir in get_solidcraft_plugin_dirs():
        target_dir = pdir / PLUGIN_ID
        target_dir.mkdir(parents=True, exist_ok=True)
        mf_path = target_dir / "plugin.json"
        mf_path.write_bytes(manifest_bytes)
        installed_paths.append(str(mf_path))

    # Create/update WindowsApps launcher scripts so running `solidcraft` or `solidcraft-plugin`
    # starts SolidCraft with `--control 7878` and attaches the HUD companion.
    win_apps = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    launcher_paths: List[str] = []
    if win_apps.is_dir():
        py_exe = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"
        plugin_py = WORKSPACE_ROOT / "tools" / "solidcraft_plugin.py"
        prog_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
        sc_exe = Path(prog_files) / "SolidCraft" / "solidcraft.exe"
        if not sc_exe.is_file():
            sc_exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "SolidCraft" / "solidcraft.exe"

        plugin_cmd = win_apps / "solidcraft-plugin.cmd"
        plugin_cmd.write_text(
            f'@echo off\r\n"{py_exe}" "{plugin_py}" run %*\r\n',
            encoding="utf-8",
        )
        launcher_paths.append(str(plugin_cmd))

    return {
        "plugin_id": PLUGIN_ID,
        "manifest": PLUGIN_MANIFEST,
        "installed_manifests": installed_paths,
        "launchers": launcher_paths,
        "enabled_in_solidcraft": is_plugin_enabled_in_solidcraft(PLUGIN_ID),
    }


def install_antigravity_ai_plugin(repository_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Package and install the Antigravity AI Plugin at:
    `~/.gemini/config/plugins/solidcraft-robotics-cad`
    and mirror it into `plugins/solidcraft-robotics-cad/` inside the repository
    so it travels via GitHub.
    """
    user_plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "solidcraft-robotics-cad"
    repo_plugin_dir = WORKSPACE_ROOT / "plugins" / "solidcraft-robotics-cad"

    ag_manifest: Dict[str, Any] = {
        "name": "solidcraft-robotics-cad",
        "displayName": "SolidCraft Robotics CAD & COTS",
        "version": "1.1.0",
        "description": "Robotics CAD workflow: 2,588 normalized FTC/FRC COTS parts, SolidCraft + build123d co-design, jointed assembly compiler, slicer audit, 3D FEA, and MuJoCo physics.",
        "logo": "assets/logo.svg",
        "author": {"name": "ARES Robotics (FTC 23247)"},
        "suggestedPrompts": [
            "Search the COTS catalog for an 8mm REX flanged bearing and design a mounting plate in SolidCraft",
            "Build a parametric motor mount bracket in build123d, compile a jointed .sc assembly, and run 3D FEA",
            "Audit my 3D-printed STL part for bed fit, overhangs, and first-layer adhesion"
        ],
    }
    if repository_url:
        ag_manifest["repository"] = repository_url

    compiler_skill_md = """---
name: solidcraft-assembly-compiler
description: Compile robotics STEP assemblies and *_constraints.json specs into native, jointed, motion-coupled SolidCraft (.sc) assemblies with BOMs, mass/CoM audits, dynamic sweep checks, and animated motion GIFs.
---

# SolidCraft Project Assembly Compiler (`solidcraft-compile`)

Use this skill whenever building, verifying, or converting a project assembly in `projects/<project>/cad/` into a jointed SolidCraft (`.sc`) assembly with COTS metadata, kinematic joints, gear/belt motion links, BOM rollups, and animated GIFs.

## 1. CLI & Pipeline Commands

- **Auto-Compile During Build (Default):**
  ```powershell
  .venv\\Scripts\\python.exe tools/cad_pipeline.py build projects/<project>/src/<script.py>
  ```
  Automatically builds the STEP export and, if a `*_constraints.json` exists, compiles the jointed `.sc` assembly + BOM + verification JSON + motion GIF (pass `--no-solidcraft` to skip).

- **Compile Any Existing `.step` or `.step.gz` Assembly:**
  ```powershell
  .venv\\Scripts\\python.exe tools/cad_pipeline.py solidcraft-compile projects/<project>/cad/<assembly>.step --live
  ```

- **Launch the Native SolidCraft Plugin & HWND-Attached HUD (`Alt+C`):**
  ```powershell
  .venv\\Scripts\\python.exe tools/solidcraft_plugin.py run
  ```

## 2. Generated Deliverables

1. `projects/<project>/cad/<stem>.sc` — Jointed `.sc` assembly with COTS `part_number`/`description`/`bom_type`, `joint.ground`, `joint.rigid_group`, `joint.create`, `joint.motion_link`, and `Primary_Drive_Cycle` motion study.
2. `projects/<project>/docs/<stem>_sc_bom.csv` & `*_sc_bom.md` — Rolled-up flat BOM table.
3. `projects/<project>/docs/<stem>_sc_verification.json` — Solver conflict check, total mass (`kg`), COTS match count, and `0–360°` dynamic `joint.sweep` report.
4. `projects/<project>/docs/<stem>_sc_motion.gif` — Driven 36-frame kinematic animation.
"""

    rules_md = """# SolidCraft Robotics COTS & Assembly Compiler Rules

- **Prefer Pre-Built COTS `.sc` Parts (`cots/solidcraft/`):** Never re-import raw supplier STEP files when assembling COTS hardware in SolidCraft; query `cots_search` (or `tools/solidcraft_cots.py search`) and insert via `solidcraft_insert_cots` (`component.insert_design {"reuse": true}`).
- **Compile Project Assemblies to Jointed `.sc`:** Whenever a project produces a `.step` and `*_constraints.json`, ensure its corresponding jointed `.sc` assembly, `*_sc_bom.md`, `*_sc_verification.json`, and `*_sc_motion.gif` are compiled and conflict-free (`solver_conflicts == []`).
"""

    src_cots_skill = WORKSPACE_ROOT / ".agents" / "skills" / "solidcraft-cots" / "SKILL.md"
    cots_skill_text = src_cots_skill.read_text(encoding="utf-8") if src_cots_skill.is_file() else ""

    logo_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128" fill="none">
  <rect x="14" y="14" width="100" height="100" rx="22" fill="#0F172A" stroke="#38BDF8" stroke-width="4"/>
  <polygon points="64,28 96,46 96,82 64,100 32,82 32,46" fill="#1E293B" stroke="#38BDF8" stroke-width="3.5" stroke-linejoin="round"/>
  <line x1="64" y1="64" x2="64" y2="100" stroke="#38BDF8" stroke-width="3"/>
  <line x1="64" y1="64" x2="96" y2="46" stroke="#38BDF8" stroke-width="3"/>
  <line x1="64" y1="64" x2="32" y2="46" stroke="#38BDF8" stroke-width="3"/>
  <circle cx="64" cy="52" r="10" fill="#0F172A" stroke="#F59E0B" stroke-width="3.5"/>
  <circle cx="64" cy="52" r="3.5" fill="#F59E0B"/>
</svg>
"""

    installed_roots: List[str] = []
    for root in (user_plugin_dir, repo_plugin_dir):
        (root / ".antigravity-plugin").mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "skills" / "solidcraft-cots").mkdir(parents=True, exist_ok=True)
        (root / "skills" / "solidcraft-assembly-compiler").mkdir(parents=True, exist_ok=True)
        (root / "rules").mkdir(parents=True, exist_ok=True)

        manifest_json = json.dumps(ag_manifest, indent=2) + "\n"
        (root / "plugin.json").write_text(manifest_json, encoding="utf-8")
        (root / ".antigravity-plugin" / "plugin.json").write_text(manifest_json, encoding="utf-8")
        (root / "assets" / "logo.svg").write_text(logo_svg, encoding="utf-8")
        if cots_skill_text:
            (root / "skills" / "solidcraft-cots" / "SKILL.md").write_text(cots_skill_text, encoding="utf-8")
        (root / "skills" / "solidcraft-assembly-compiler" / "SKILL.md").write_text(
            compiler_skill_md, encoding="utf-8"
        )
        (root / "rules" / "AGENTS.md").write_text(rules_md, encoding="utf-8")
        installed_roots.append(str(root))

    return {
        "status": "ok",
        "antigravity_plugin_roots": installed_roots,
    }


class SolidCraftHudController:
    """
    Win32 `HWND` window manager that attaches the COTS HUD window to the right
    edge of the live SolidCraft window, respects `Preferences -> Plugins`
    (`disabled_plugins` in `app.ron`), and binds `Alt+C` to toggle visibility.
    """

    def __init__(self, hud_width: int = 430) -> None:
        self.hud_width = hud_width
        self.snap_to_solidcraft = True
        self.user_hidden = False
        self.plugin_enabled = True
        self.sc_hwnd: Optional[int] = None
        self.hud_hwnd: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def status_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_enabled": self.plugin_enabled,
            "snap_to_solidcraft": self.snap_to_solidcraft,
            "user_hidden": self.user_hidden,
            "sc_hwnd": self.sc_hwnd,
            "hud_hwnd": self.hud_hwnd,
            "hotkey": "Alt+C",
        }

    def toggle_snap(self) -> Dict[str, Any]:
        self.snap_to_solidcraft = not self.snap_to_solidcraft
        if not self.snap_to_solidcraft and self.hud_hwnd and os.name == "nt":
            # Drop topmost when detached into free window mode
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                self.hud_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        return self.status_dict()

    def toggle_visibility(self) -> Dict[str, Any]:
        self.user_hidden = not self.user_hidden
        return self.status_dict()

    def start(self) -> None:
        if os.name != "nt":
            return
        self._thread = threading.Thread(target=self._win32_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _find_windows(self) -> Tuple[Optional[int], Optional[int]]:
        user32 = ctypes.windll.user32
        sc_candidates: List[Tuple[int, int]] = []
        hud_candidates: List[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        def _cb(hwnd: int, lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) and hwnd != self.hud_hwnd:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            if "SolidCraft COTS Dock" in title:
                hud_candidates.append(hwnd)
            elif "SolidCraft" in title and "COTS" not in title and "Visual Studio" not in title and "Antigravity" not in title:
                rc = RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                    w = rc.right - rc.left
                    h = rc.bottom - rc.top
                    if w >= 400 and h >= 300:
                        sc_candidates.append((w * h, hwnd))
            return True

        hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.EnumDesktopWindows(hdesk, WNDENUMPROC(_cb), 0)
            user32.CloseDesktop(hdesk)
        else:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
        sc_best = max(sc_candidates, key=lambda x: x[0])[1] if sc_candidates else None
        hud_best = hud_candidates[0] if hud_candidates else self.hud_hwnd
        return sc_best, hud_best

    def _win32_loop(self) -> None:
        user32 = ctypes.windll.user32
        hdesk_thread = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk_thread:
            user32.SetThreadDesktop(hdesk_thread)
        MOD_ALT = 0x0001
        MOD_NOREPEAT = 0x4000
        VK_C = 0x43
        HOTKEY_ID = 0xA232
        WM_HOTKEY = 0x0312
        PM_REMOVE = 0x0001

        SW_HIDE = 0
        SW_SHOWNOACTIVATE = 4
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOACTIVATE = 0x0010
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT | MOD_NOREPEAT, VK_C)
        msg = wintypes.MSG()
        last_pref_check = 0.0

        try:
            while not self._stop.is_set():
                # 1. Process Alt+C hotkey messages
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self.user_hidden = not self.user_hidden

                # 2. Check SolidCraft Preferences -> Plugins toggle every 1.0s
                now = time.time()
                if now - last_pref_check >= 1.0:
                    self.plugin_enabled = is_plugin_enabled_in_solidcraft(PLUGIN_ID)
                    last_pref_check = now

                sc_hwnd, hud_hwnd = self._find_windows()
                self.sc_hwnd = sc_hwnd
                self.hud_hwnd = hud_hwnd

                if hud_hwnd and user32.IsWindow(hud_hwnd):
                    should_hide = (
                        not self.plugin_enabled
                        or self.user_hidden
                        or (sc_hwnd is not None and bool(user32.IsIconic(sc_hwnd)))
                    )
                    if should_hide:
                        if user32.IsWindowVisible(hud_hwnd):
                            user32.ShowWindow(hud_hwnd, SW_HIDE)
                    else:
                        if not user32.IsWindowVisible(hud_hwnd):
                            user32.ShowWindow(hud_hwnd, SW_SHOWNOACTIVATE)

                        if self.snap_to_solidcraft and sc_hwnd and not user32.IsIconic(sc_hwnd):
                            rc = RECT()
                            if user32.GetWindowRect(sc_hwnd, ctypes.byref(rc)):
                                sc_w = rc.right - rc.left
                                sc_h = rc.bottom - rc.top
                                if sc_w > 500 and sc_h > 350:
                                    w = min(self.hud_width, max(360, int(sc_w * 0.30)))
                                    h = max(420, sc_h - 56)
                                    x = rc.right - w - 12
                                    y = rc.top + 42

                                    fg = user32.GetForegroundWindow()
                                    is_active_pair = fg in (sc_hwnd, hud_hwnd)
                                    z_order = HWND_TOPMOST if is_active_pair else HWND_NOTOPMOST
                                    user32.SetWindowPos(
                                        hud_hwnd,
                                        z_order,
                                        x,
                                        y,
                                        w,
                                        h,
                                        SWP_NOACTIVATE,
                                    )

                time.sleep(0.12)
        finally:
            try:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                pass



UPSTREAM_REPO_URL = "https://github.com/tangency-labs/solidcraft.git"
UPSTREAM_CLONE_DIR = Path(
    os.environ.get("SOLIDCRAFT_UPSTREAM_DIR")
    or str(WORKSPACE_ROOT.parent.parent / "tools" / "solidcraft")
)


def sync_upstream_solidcraft_repo(clone_dir: Path = UPSTREAM_CLONE_DIR) -> Dict[str, Any]:
    """
    Clone or fast-forward a local checkout of
    `https://github.com/tangency-labs/solidcraft.git` and inspect key plugin &
    hardware files (`crates/ui-egui/src/plugins.rs`, `docs/plugins.md`,
    `crates/engine/src/cmd/parts.rs`, `crates/ui-egui/src/control.rs`).
    """
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    if not (clone_dir / ".git").is_dir():
        subprocess.run(
            ["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(clone_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
        action = "cloned"
    else:
        subprocess.run(
            ["git", "-C", str(clone_dir), "pull", "--ff-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        action = "pulled"

    head = subprocess.check_output(
        ["git", "-C", str(clone_dir), "log", "-1", "--oneline"],
        text=True,
    ).strip()

    tracked_files = {}
    for rel in [
        "crates/ui-egui/src/plugins.rs",
        "docs/plugins.md",
        "docs/design/preferences/pages/plugins.md",
        "crates/engine/src/cmd/parts.rs",
        "crates/ui-egui/src/control.rs",
    ]:
        fp = clone_dir / rel
        tracked_files[rel] = {
            "exists": fp.is_file(),
            "size_bytes": fp.stat().st_size if fp.is_file() else 0,
        }

    # Check if Wasmtime / WIT plugin host has landed in crates/ui-egui/src/plugins.rs
    plugins_rs = (clone_dir / "crates/ui-egui/src/plugins.rs")
    wasm_host_landed = False
    if plugins_rs.is_file():
        src = plugins_rs.read_text(encoding="utf-8", errors="ignore")
        wasm_host_landed = ("wasmtime" in src.lower()) or ("solidcraft:plugin" in src.lower())

    return {
        "status": "ok",
        "action": action,
        "upstream_url": UPSTREAM_REPO_URL,
        "local_clone": str(clone_dir),
        "head_commit": head,
        "wasm_wit_host_landed_in_ui": wasm_host_landed,
        "tracked_plugin_files": tracked_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SolidCraft Native Plugin & HWND-Attached HUD Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("install", help="Install both the SolidCraft Desktop Plugin (`plugin.json`) and Antigravity AI Plugin")
    sub.add_parser("status", help="Show SolidCraft plugin installation & enablement status")
    sub.add_parser("sync-upstream", help="Clone or git pull https://github.com/tangency-labs/solidcraft into local tools/solidcraft")

    p_run = sub.add_parser("run", help="Launch SolidCraft (if not running) and attach the HWND-docked COTS HUD plugin")
    p_run.add_argument("sc_file", nargs="?", default=None, help="Optional .sc or .step file to open in SolidCraft")
    p_run.add_argument("--http-port", type=int, default=8765)
    p_run.add_argument("--port", type=int, default=27878)
    p_run.add_argument("--no-window", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if args.cmd == "install":
        sc_res = install_solidcraft_desktop_plugin()
        ag_res = install_antigravity_ai_plugin()
        acp_res = {}
        try:
            from tools.solidcraft_acp_agent import register_in_live_solidcraft
            acp_res = register_in_live_solidcraft(activate=False)
        except Exception as exc:
            acp_res = {"status": "skipped", "error": str(exc)}
        print(
            json.dumps(
                {
                    "solidcraft_desktop_plugin": sc_res,
                    "antigravity_ai_plugin": ag_res,
                    "solidcraft_native_assistant_acp_agent": acp_res,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "sync-upstream":
        print(json.dumps(sync_upstream_solidcraft_repo(), indent=2))
        return 0

    if args.cmd == "status":
        print(
            json.dumps(
                {
                    "plugin_id": PLUGIN_ID,
                    "enabled_in_solidcraft_preferences": is_plugin_enabled_in_solidcraft(PLUGIN_ID),
                    "detected_control_port": detect_live_solidcraft_port(),
                    "plugin_dirs": [str(p / PLUGIN_ID / "plugin.json") for p in get_solidcraft_plugin_dirs()],
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "run":
        install_solidcraft_desktop_plugin()
        install_antigravity_ai_plugin()

        # Ensure the HUD controller & SolidCraft GUI run directly on WinSta0\Default
        if os.name == "nt" and os.environ.get("SC_ON_WINSTA0") != "1":
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

            os.environ["SC_ON_WINSTA0"] = "1"
            si = STARTUPINFO()
            si.cb = ctypes.sizeof(STARTUPINFO)
            si.lpDesktop = "WinSta0\\Default"
            pi = PROCESS_INFORMATION()
            cmd_str = " ".join(f'"{a}"' if " " in a else a for a in [sys.executable] + sys.argv)
            ok = ctypes.windll.kernel32.CreateProcessW(
                None, cmd_str, None, None, False, 0, None, str(WORKSPACE_ROOT), ctypes.byref(si), ctypes.byref(pi)
            )
            if ok:
                ctypes.windll.kernel32.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
                return 0

        ctrl_port = detect_live_solidcraft_port(args.port)
        hud_ctrl = SolidCraftHudController()
        from tools.solidcraft_inspector import ensure_live_solidcraft_gui

        ensure_live_solidcraft_gui(
            port=ctrl_port,
            default_sc_path=Path(args.sc_file) if args.sc_file else None,
        )
        try:
            from tools.solidcraft_acp_agent import register_in_live_solidcraft
            register_in_live_solidcraft(port=ctrl_port, activate=False)
        except Exception:
            pass

        hud_ctrl.start()
        from tools.solidcraft_compiler import serve_cots_dock_and_browser

        serve_cots_dock_and_browser(
            http_port=args.http_port,
            solidcraft_port=ctrl_port,
            dock_mode=True,
            open_window=not args.no_window,
            hud_controller=hud_ctrl,
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
