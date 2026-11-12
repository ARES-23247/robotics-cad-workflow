# SolidCraft Robotics COTS & Assembly Compiler Rules

- **Prefer Pre-Built COTS `.sc` Parts (`cots/solidcraft/`):** Never re-import raw supplier STEP files when assembling COTS hardware in SolidCraft; query `cots_search` (or `tools/solidcraft_cots.py search`) and insert via `solidcraft_insert_cots` (`component.insert_design {"reuse": true}`).
- **Compile Project Assemblies to Jointed `.sc`:** Whenever a project produces a `.step` and `*_constraints.json`, ensure its corresponding jointed `.sc` assembly, `*_sc_bom.md`, `*_sc_verification.json`, and `*_sc_motion.gif` are compiled and conflict-free (`solver_conflicts == []`).
