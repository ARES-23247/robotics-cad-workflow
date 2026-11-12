---
name: solidcraft-cots
description: >-
  Search, inspect, and insert robotics COTS hardware (goBILDA, REV DUO/ION, Swyft,
  ThriftyBot, Ruthex, McMaster, and ISO metric fasteners/bearings) into SolidCraft
  assemblies using the pre-built cots/solidcraft/*.sc library, solidcraft_insert_cots,
  parts.insert, and normalized functional frames.
---

# SolidCraft Robotics COTS Library & Hardware Integration

## Overview
This workspace provides two complementary COTS hardware systems for SolidCraft (`v0.29.0+`):

1. **Built-in ISO Standard Hardware (`parts.insert` / `parts.fastener` in `solidcraft` / `solidcraft-live`):**
   - Use for standard metric fasteners (`socket_head_cap_screw`, `button_head_cap_screw`, `hex_bolt`, `hex_nut`, `washer`, `set_screw`, `dowel_pin`) and standard `bearing` envelopes (`625`, `626`, `608`, `6000`–`6004`, `6200`–`6204`).
   - Passing `at: [[x, y, z], ...]` on hole rims automatically creates a single reusable component and seats an occurrence onto every hole with a `rigid` joint.

2. **Pre-Built Robotics COTS `.sc` Library (`cots/solidcraft/` + `ares_onshape_local` MCP + `cots/cots_agent_index.jsonl`):**
   - Contains **2,600+ Format 52 (`solidcraft/52` + `solidcore`) `.sc` component files** for goBILDA, REV DUO (FTC), REV ION (FRC), Swyft, ThriftyBot, Melonbotics, Limo, Ruthex, and McMaster hardware.
   - **Canonical `[0, 0, 0]` / `+Z` Normalization + Pre-Baked `joint.origin`s:** Every `.sc` part in `cots/solidcraft/` has its recommended semantic anchor (`bore_center`, `flange_seat`, `output_shaft`, `sense_origin`, or `bbox_center`) placed at `[0, 0, 0]` with its primary bore/shaft axis aligned to `+Z` (`[0, 0, 1]`), and includes pre-baked `joint.origin` mate connectors (`bore_center`, `flange_seat`, `output_shaft`, `bore_min`, `bore_max`, `mount_hole_1..8`).
   - **Agent Engineering Spec (`agent_spec`):** Every `cots_search` / `cots_info` result (and offline `cots/cots_agent_index.jsonl` row) provides `bore_profile` (`8mm_REX_hex`, `1/2in_hex`, etc.), `mounting_pattern` (`goBILDA_16mm_M4_grid`, `REV_M3_pattern`, `MAXPattern_2in`), `axial_stackup_mm` (`z_min_mm`..`z_max_mm`), `kinematics` (`teeth`, `pitch_diameter_mm`), `mass_estimate_g`, and ready-to-paste `mate_graph_snippet`.
   - **Physical Materials & BOM Metadata:** Every `.sc` part has its physical material (`Aluminum`, `Steel`, `Stainless Steel`, `Brass`, `Nylon`, `Polycarbonate`, `ABS Plastic`) assigned to its bodies for accurate `assembly.mass_properties` and `.urdf` inertia exports, plus BOM metadata (`part_number`, `description`, `organization`, `bom_type: "purchased"`) and `io.solidcraft.cots` frame coordinates.

---

## Workflow

### 1. Search & Inspect Robotics COTS Parts
Call `cots_search` or `cots_info` on `ares_onshape_local`:
- `cots_search {"query": "5203", "vendor": "gobilda", "source": "local"}`
- `cots_info {"part_key_or_sku": "1611-0514-0008"}`

The response includes:
- `solidcraft_path`: Path to the pre-converted `.sc` library file (`cots/solidcraft/...`).
- `recommended_anchor`: Primary anchor (`bore_center`, `flange_seat`, `output_shaft`, `sense_origin`, `bbox_center`) located at `[0, 0, 0]` along `+Z`.
- `solidcraft_frames`: Exact `[x, y, z]` coordinates and axes of all secondary features (`bore_min`, `bore_max`, `flange_seat`, `output_shaft`, `ear_mount.mount_holes_mm`) in the normalized `.sc` coordinate system.

### 2. Insert & Mate COTS Parts in One Call (`solidcraft_insert_cots`)
Use `solidcraft_insert_cots` on `ares_onshape_local` to insert a COTS part into the live SolidCraft window (`127.0.0.1:27878`) or a headless `.sc` file:
```json
{
  "part_key_or_sku": "1611-0514-0008",
  "joint_type": "rigid",
  "target_occurrence": "SidePlate:1",
  "target_point": [24.0, 0.0, 3.0],
  "target_axis": [0.0, 0.0, 1.0],
  "flip": true
}
```
This automatically:
1. Inserts the `.sc` file via `component.insert_design {"path": "...", "reuse": true}` (reusing the linked component definition across multiple instances).
2. Registers named `joint.origin`s for all functional frames (`<comp>_<occ>_bore_center`, `<comp>_<occ>_flange_seat`, `<comp>_<occ>_mount_hole_1`, etc.).
3. Creates the requested joint (`rigid`, `revolute`, `slider`, `cylindrical`) mating the COTS part's anchor to `target_occurrence`.

### 3. Direct Native Insertion (`component.insert_design`)
You can also insert any `cots/solidcraft/.../*.sc` file directly inside a SolidCraft `batch` call:
```json
{"command": "component.insert_design", "params": {"path": "<workspace>/cots/solidcraft/vendors/gobilda/bearings/1611-0514-0008.sc", "reuse": true}}
```
Because the `.sc` part's semantic anchor is already normalized to `[0, 0, 0]` with its bore/shaft along `+Z`, joining it at `[0, 0, 0]` with `z: [0, 0, 1]` seats the part directly on its anchor!

### 4. CLI & Human Catalog Search (`tools/solidcraft_cots.py` & `solidcraft-cli`)
- **Terminal Table Search:**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_cots.py search "flanged bearing" --bore-mm 8
  ```
- **Inspect Part Frames & Metadata:**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_cots.py info 1611-0514-0008
  ```
- **Insert & Mate from CLI:**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_cots.py insert 1611-0514-0008 --joint-type rigid --target-occurrence Plate:1 --target-point 24 0 2
  ```
- **Floating SolidCraft COTS Dock (Compact Companion Window):**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_cots.py dock
  ```
- **Interactive Web Catalog Browser (with 3D Thumbnails & 1-Click Live Insert):**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_cots.py browser
  ```
  Or open [`cots/solidcraft/index.html`](../../cots/solidcraft/index.html) / [`cots/solidcraft/CATALOG.md`](../../cots/solidcraft/CATALOG.md) directly.
- **Direct `solidcraft-cli` Commands (on `PATH`):**
  ```powershell
  solidcraft-cli eval cots/solidcraft/vendors/gobilda/bearings/1611-0514-0008_1611_Series_Flanged_Ball_Bearing_8Mm_Id_X_14Mm_Od_5Mm_Thickness_2_Pack.sc
  solidcraft-cli snapshot cots/solidcraft/vendors/gobilda/bearings/1611-0514-0008_1611_Series_Flanged_Ball_Bearing_8Mm_Id_X_14Mm_Od_5Mm_Thickness_2_Pack.sc --out preview.png --view iso
  ```

### 5. Compile Any Project Assembly into a Jointed `.sc` Design (`solidcraft_compile_project`)
Call `solidcraft_compile_project` on `ares_onshape_local` or run `solidcraft-compile` on the CLI to compile any project `.step`/`.step.gz` + `*_constraints.json` into a fully jointed, motion-coupled `.sc` assembly using Hybrid In-Place COTS Tagging:
```powershell
.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-compile projects/cycloidal-actuator/cad/cycloidal_actuator_15to1.step --live
```
This automatically outputs:
- `<stem>.sc`: Self-contained jointed SolidCraft assembly (`occurrence.ground`, `joint.rigid_group`, `joint.create`, `joint.motion_link`, COTS `part_number` & physical materials).
- `<stem>_sc_bom.csv` & `<stem>_sc_bom.md`: Grouped Bill of Materials (`assembly.bom`).
- `<stem>_sc_motion.gif`: Driven kinematic animation (`motion.export_animation`).
- `<stem>_sc_verification.json`: Total mass/inertia (`assembly.mass_properties`), DOF audit, and dynamic `joint.sweep` collision verification.

### 6. Parametric Robotics Template Library & Bidirectional Script Sync (`solidcraft-template` & `solidcraft-pull`)
Use [`tools/solidcraft_builder.py`](../../tools/solidcraft_builder.py) or the `⚙️ Parametric Templates (6)` tab in the `Alt+C` HUD Dock to generate expression-parameterized, `inspect.parameter_sweep`-verified native `.sc` parts for standard FTC/FRC hardware interfaces:
- `gobilda_bearing_plate`: 16 mm grid 2-shaft belt/gear carrier plate (`Center_Distance`, `Half_Width`, `Plate_Thickness`, `Bearing_Bore_Dia`, `M4_Clearance_Dia`)
- `gobilda_5203_motor_mount`: goBILDA 5202/5203/5204 planetary motor faceplate (`Pilot_Bore_Dia=22.5 mm`, 4× M4 on 32 mm bolt circle, 16 mm grid mounts)
- `servo_mount_bracket`: Standard 20×40 mm FTC servo saddle mount (`Swyft Titan`, `goBILDA 2000`, `Axon`)
- `vision_sensor_mount`: 90° L-flange bracket for Limelight / REV Color-Range / AprilTag cameras
- `clamping_shaft_hub`: 3D-printed cantilever actuator arm with ribbed `plastic.boss` standoff
- `sheet_metal_u_channel_gusset`: Flanged sheet-metal U-channel gusset built with `sheet.flange`

```powershell
# List all parametric robotics templates and parameter schemas
.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-template --list

# Build a custom parameterized template and optionally insert into live SolidCraft (port 27878)
.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-template gobilda_5203_motor_mount -p Top_Reach=48 -p Plate_Thickness=5 --insert-live

# Pull manual GUI feature-tree & parameter edits back to Git (*_sc_script.json + *_sc_params.json)
.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-pull --live
```

### 7. CAD Git & File Management Plugin (`tools/cad_git_manager.py` & HUD `🔀 Git & Files` Tab)
Use [`tools/cad_git_manager.py`](../../tools/cad_git_manager.py), the `ares_onshape_local` MCP tools (`cad_git_status`, `cad_git_hygiene`, `cad_git_visual_diff`, `cad_git_commit_project`), or the **`🔀 Git & Files`** tab inside the `Alt+C` SolidCraft HUD Dock to manage CAD files and Git history safely:
- **Project-Grouped Git Status (`git-status` / `cad_git_status`)**: Groups all modified, untracked, and deleted files by `projects/<project>/`, reports file sizes in MB, flags `>50 MB` and `>95 MB` binaries, and identifies modified `.sc`/`.step` models with 3D visual diff support.
- **Automated CAD File-Hygiene & `>50 MB` Gzip Guard (`git-hygiene --fix` / `cad_git_hygiene`)**: Moves misplaced `.step` files out of `fabrication/prints/` into `cad/parts/`, removes accidental COTS exports from `fabrication/`, gitignores uncompressed `.step` peers when `.step.gz` is present, and compresses `>50 MB` binaries to `.gz`.
- **3D Visual CAD Diff Against `HEAD` (`git-diff` / `cad_git_visual_diff`)**: Extracts `HEAD:<path>` and runs SolidCraft's `inspect.compare` to compute exact volume deltas ($\Delta\text{mm}^3$), max surface deviations (mm), and a Red/Green 3D visual diff PNG (`cots/solidcraft/diffs/`).
- **Per-Project Staging, Pre-Commit `>95 MB` Guard, & Push (`git-commit` / `cad_git_commit_project`)**: Stages and commits one project scope at a time while blocking any uncompressed file `>95 MiB`.

```powershell
# Audit and auto-fix misplaced CAD/print files & >50MB .gz compression
.venv\Scripts\python.exe tools/cad_pipeline.py git-hygiene --fix

# Run a 3D visual diff against Git HEAD for a modified .sc or .step model
.venv\Scripts\python.exe tools/cad_pipeline.py git-diff projects/ftc-swerve-sprung-intake/cad/SI-EL-001_Removable_Hub_Carrier_Plate.step

# Stage, commit, and push a single project scope with automatic hygiene protection
.venv\Scripts\python.exe tools/cad_pipeline.py git-commit projects/ftc-cots-belt-drive -m "feat(ftc-cots-belt-drive): update carrier plate"
```

---

## Cross-Computer Setup
On any new machine after cloning the repository, run:
```bash
python tools/setup_solidcraft_cots.py
```
This decompresses any `>95 MiB` `.gz` archives, configures `solidcraft`, `solidcraft-live`, and `ares_onshape_local` MCP servers with the local repository paths, and installs this skill.
