# Robotics CAD Workflow & 2,588-Part COTS Library

A self-contained, AI-native parametric CAD, COTS hardware, 3D-print DFM, FEA, and physics-simulation workspace powered by **SolidCraft (`v0.40+`)**, **`build123d` (OpenCASCADE 8.0)**, and **Antigravity**.

## ⚡ One-Command Windows 11/10 Install

Open **PowerShell** on any Windows 11/10 computer and paste:

```powershell
irm https://raw.githubusercontent.com/ARES-23247/robotics-cad-workflow/main/bootstrap.ps1 | iex
```

*(To install into a custom folder instead of `$HOME\dev\robotics\CAD`, run `$env:CAD_WORKSPACE_DIR="C:\CAD"; irm https://raw.githubusercontent.com/ARES-23247/robotics-cad-workflow/main/bootstrap.ps1 | iex`.)*

### Or Clone + Install Manually

```powershell
git clone https://github.com/ARES-23247/robotics-cad-workflow.git C:\dev\CAD
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
