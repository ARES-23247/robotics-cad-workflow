# SolidCraft Robotics COTS & Assembly Compiler Plugins

This directory contains the two companion plugins that integrate the **2,588-part FTC/FRC COTS Library (`cots/solidcraft/`)** and the **Project Assembly Compiler (`solidcraft-compile`)** into both the **SolidCraft Desktop App (`v0.29.0+`)** and **AI Coding Agents (Antigravity / Gemini CLI / Claude Code / Cursor)**.

---

## 1. SolidCraft Desktop Plugin (`plugins/ares-robotics-cots/`)

### How SolidCraft Desktop Plugins Work
SolidCraft (`v0.29.0+`) discovers desktop plugins via `plugins/<id>/plugin.json` (`crates\ui-egui\src\plugins.rs`) and exposes an **Enable / Disable** switch in **SolidCraft → Preferences → Plugins** (persisted in `%APPDATA%\SolidCraft\data\app.ron` under `"disabled_plugins"`).

Because SolidCraft is a compiled Rust (`egui` + `wgpu`) binary that isolates third-party code in external processes, [`tools/solidcraft_plugin.py`](../tools/solidcraft_plugin.py) provides the live companion HUD:
- **Native Preferences Toggle:** Checks `"disabled_plugins"` in `app.ron` in real time—toggling **ARES Robotics COTS Hardware & Assembly Compiler** OFF in **Preferences → Plugins** hides the HUD immediately; toggling it ON restores it.
- **Win32 `HWND`-Attached HUD Overlay:** Docks flush to the right edge of the SolidCraft window, follows SolidCraft when moved or resized, hides when SolidCraft is minimized, and drops `HWND_TOPMOST` automatically when you `Alt+Tab` to another application.
- **Global Hotkey (`Alt+C`):** Press `Alt+C` at any time to toggle the COTS HUD panel open or closed, or click **📌 Snapped (Alt+C)** in the HUD header to detach it into a free-moving window.
- **1-Click Live COTS Insert & Concentric Bore Auto-Mate:** Connects to SolidCraft's live control socket (`%LOCALAPPDATA%\SolidCraft\control\current.port` / `127.0.0.1:27878`) to insert any of the 2,588 normalized `.sc` COTS parts and optionally create a `rigid`, `revolute`, `slider`, or `cylindrical` joint on `flange_seat`, `bore_center`, `output_shaft`, or `sense_origin`.

### Quick Commands
```powershell
# Install both the SolidCraft Desktop Plugin and Antigravity AI Plugin
.venv\Scripts\python.exe tools/solidcraft_plugin.py install

# Launch SolidCraft (if not already open) and attach the HWND-docked COTS HUD (Hotkey: Alt+C)
.venv\Scripts\python.exe tools/solidcraft_plugin.py run
```

---

## 2. Antigravity AI Plugin (`plugins/solidcraft-robotics-cad/`)

Installed automatically to `~/.gemini/config/plugins/solidcraft-robotics-cad` (and mirrored here in Git):
- **`.antigravity-plugin/plugin.json`**: Plugin manifest.
- **`skills/solidcraft-cots/SKILL.md`**: Instructions for searching, inspecting, and mating normalized COTS `.sc` hardware.
- **`skills/solidcraft-assembly-compiler/SKILL.md`**: Instructions for compiling `.step` + `*_constraints.json` assemblies into jointed `.sc` assemblies + BOMs + verification JSON + animated motion GIFs.
- **`rules/AGENTS.md`**: Enforces reusing pre-built `cots/solidcraft/*.sc` parts and verifying zero solver conflicts.

---

## 3. One-Command Setup on Any New Computer

After cloning this repository on another machine:
```powershell
python tools/setup_solidcraft_cots.py
```
This single command decompresses any `>95 MiB` `.gz` archives, registers the `solidcraft`, `solidcraft-live`, and `ares_onshape_local` MCP servers, and installs both the **SolidCraft Desktop Plugin** and **Antigravity AI Plugin**.
