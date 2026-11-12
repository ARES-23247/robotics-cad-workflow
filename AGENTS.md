# Robotics CAD Workspace Instructions

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
