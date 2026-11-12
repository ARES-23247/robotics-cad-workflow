---
name: solidcraft-assembly-compiler
description: Compile robotics STEP assemblies and *_constraints.json specs into native, jointed, motion-coupled SolidCraft (.sc) assemblies with BOMs, mass/CoM audits, dynamic sweep checks, and animated motion GIFs.
---

# SolidCraft Project Assembly Compiler (`solidcraft-compile`)

Use this skill whenever building, verifying, or converting a project assembly in `projects/<project>/cad/` into a jointed SolidCraft (`.sc`) assembly with COTS metadata, kinematic joints, gear/belt motion links, BOM rollups, and animated GIFs.

## 1. CLI & Pipeline Commands

- **Auto-Compile During Build (Default):**
  ```powershell
  .venv\Scripts\python.exe tools/cad_pipeline.py build projects/<project>/src/<script.py>
  ```
  Automatically builds the STEP export and, if a `*_constraints.json` exists, compiles the jointed `.sc` assembly + BOM + verification JSON + motion GIF (pass `--no-solidcraft` to skip).

- **Compile Any Existing `.step` or `.step.gz` Assembly:**
  ```powershell
  .venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-compile projects/<project>/cad/<assembly>.step --live
  ```

- **Launch the Native SolidCraft Plugin & HWND-Attached HUD (`Alt+C`):**
  ```powershell
  .venv\Scripts\python.exe tools/solidcraft_plugin.py run
  ```

## 2. Generated Deliverables

1. `projects/<project>/cad/<stem>.sc` — Jointed `.sc` assembly with COTS `part_number`/`description`/`bom_type`, `joint.ground`, `joint.rigid_group`, `joint.create`, `joint.motion_link`, and `Primary_Drive_Cycle` motion study.
2. `projects/<project>/docs/<stem>_sc_bom.csv` & `*_sc_bom.md` — Rolled-up flat BOM table.
3. `projects/<project>/docs/<stem>_sc_verification.json` — Solver conflict check, total mass (`kg`), COTS match count, and `0–360°` dynamic `joint.sweep` report.
4. `projects/<project>/docs/<stem>_sc_motion.gif` — Driven 36-frame kinematic animation.
