# Robotics CAD workflow

This is the maintained workflow for Codex and Antigravity in this CAD workspace repository. Read [AGENTS.md](AGENTS.md) and the selected project's `docs/AGENTS.md` before modeling. [README.md](README.md) describes the self-contained folder layout. All CAD files, COTS libraries (`cots/`), and 3D-printed parts/plates (`projects/<project>/fabrication/prints/`) live exclusively in this workspace repository and sync with GitHub; never store or mirror files in `Documents` or `OneDrive`.

## 1. Select the source and modeling route

Start with the engineering question, owning design, existing source model, and required output. Keep recorded project choices and verified models intact. Local work does not require creating a second native model unless that component's chosen route calls for it.

| Route | Use | Authoritative source |
| --- | --- | --- |
| A: Local master (SolidCraft `.sc` + `build123d`/FreeCAD) | Default day-to-day CAD iteration, AI co-design, custom printed/laser parts, modular subassemblies, and full-robot kinematic assemblies | Parametric `build123d`/FreeCAD recipe + modular SolidCraft `.sc` sub-designs & top-level `.sc` assembly (`solidcraft` / `solidcraft-live`), with Onshape browser sync at major release milestones |
| B: Native Onshape | Cloud-shared milestone releases, sheet metal, team-editable parametric cloud parts, and existing native designs | Native document, named standard features, parameters, and constrained assemblies |
| C: Selected FeatureScript generator | Reusable algorithmic features when specifically requested | Version-controlled `.fs` source and its identified cloud feature; API restrictions still apply |

### Route A: Local master (SolidCraft-First Iteration + Onshape Milestone Dual-Sync)

Keep recipes in `projects/<project>/src/`, local masters (`.sc`, `.step`, `.FCStd`) and constraint specifications in `projects/<project>/cad/`, verification records and renders in `projects/<project>/docs/`, and fabrication exports (including individual `.stl` files and arranged `.3mf`/`.stl` 3D print plates) in `projects/<project>/fabrication/prints/` and `projects/<project>/fabrication/laser/`. Source recipes, documentation, `.sc` designs (with `.sc.gz` + `.gitignore` for any file $>95\text{ MiB}$), and project fabrication deliverables are tracked in Git and synced with GitHub.

Use `.venv/Scripts/python.exe` for `build123d` algorithmic geometry (e.g., Warren-truss plates, laser DXFs, and 3D-printed STLs) paired with **SolidCraft (`.sc`)** (`solidcraft` headless MCP and `solidcraft-live` desktop GUI on `127.0.0.1:7878`) for interactive AI co-design, 1–2 second incremental subassembly reloads (`build_solidcraft_r3.py --group <GROUP>` / `component.update_links`), COTS placement from `cots/solidcraft/*.sc`, kinematic joint trees (`joint.as_built`, `joint.gear`, `set_parameter`), and physical mass/CoM verification (`component.properties`). When a local module or complete robot reaches a major release milestone for cloud sharing, push its verified STEP modules to Onshape through the signed-in browser session and apply assembly constraints.

Label feasibility and bench-test exports distinctly until their owning project's release criteria are met.

### Route B: Native Onshape

Use named sketches, meaningful dimensions/variables, standard features, native sheet metal where needed, and proper mates. Keep document/workspace/element/configuration and version or microversion identities in local records. Request-preparation scripts belong in `src/`; verification records and receipts belong in the active project.

Use the signed-in browser or session-authenticated official API Explorer. Preserve source documents, existing part identities, verified hardware, and recoverable versions. A local study may resolve an uncertain mechanism, but it need not become a second maintained model after native handoff.

### Route C: Selected FeatureScript generator

Existing `.fs` sources remain recoverable. Choose this route only when the user requests or has recorded approval for the component. The workflow does not authorize FeatureScript evaluation or Feature Studio APIs, or bypass a project's standard-native-feature requirement. Use Routes A or B for ordinary work covered by those requirements.

## 2. Purchased hardware

For native Onshape assemblies, prefer suitable FTCDesignLib, FRCDesignLib, or MKCad entries using the signed-in FRCDesignApp panel. Verify configuration, dimensions, interfaces, and inserted occurrence; record source document/version/element/configuration and keep library sources read-only.

For local assemblies, reuse verified manufacturer CAD in `cots/vendors/` (`gobilda`, `rev`, `swyft`), `cots/gobilda-and-fasteners`, `cots/servos-and-hardware`, or `cots/swerve-cots`. All 2,574 local `.step` files, 299 Onshape `FRCDesignApp` insertables, and 11,193 FRC/FTC SKU variants are indexed in `cots/catalog.db` with precomputed B-Rep bounding boxes, primary bore/shaft axes, mounting hole circles, and semantic functional frames (`functional_frames_json`).

Query the catalog via `tools/cad_scraper.py` (`search` / `info`), `tools/solidcraft_cots.py`, `tools/cad_pipeline.py` (`cots-search` / `cots-info` / `solidcraft-insert` / `solidcraft-compile` / `solidcraft-dock`), or the MCP tools `cots_search` / `cots_info` / `solidcraft_insert_cots` / `solidcraft_compile_project`:

- `cots_search` supports automatic engineering synonym expansion (`1/2` $\leftrightarrow$ `0.5in`/`12.7mm`/`hex`, `8mm` $\leftrightarrow$ `rex`, `3/8` $\leftrightarrow$ `0.375`/`9.5mm`), relevance ranking that prioritizes primary actuators/bearings over accessories, and parametric filters (`bore_mm`, `max_extent_mm`, `has_mounting_holes`).
- `cots_info` returns the full B-Rep fingerprint, functional frames, `recommended_anchor` (`output_shaft`, `flange_seat`, `sense_origin`, or `bore_center`), pre-built SolidCraft `.sc` component path (`solidcraft.sc_path`, `<SKU>_<Clean_Title>.sc` under `cots/solidcraft/`), ready-to-paste `solidcraft_insert_snippet` (`component.insert_design {"reuse": true}`), and `build123d_snippet` using `load_cots_part()`.
- **SolidCraft Native Plugin & HWND-Attached HUD (`tools/solidcraft_plugin.py`):** Installs `plugins/ares-robotics-cots/plugin.json` into SolidCraft's native **Preferences → Plugins** system and docks a live COTS visual browser & 1-click concentric bore auto-mate HUD directly to the right edge of the SolidCraft window (toggleable via `Alt+C` or the native Preferences → Plugins switch).
- **SolidCraft Project Assembly Compiler (`solidcraft-compile`):** Automatically compiles any project `.step`/`.step.gz` + `*_constraints.json` into a self-contained, jointed, motion-coupled `.sc` assembly (`joint.ground`, `joint.rigid_group`, `joint.create`, `joint.motion_link`, `Primary_Drive_Cycle`), rolled-up BOM (`*_sc_bom.csv` & `*_sc_bom.md`), mass/solver verification JSON (`*_sc_verification.json`), and driven kinematic animation (`*_sc_motion.gif`). Running `tools/cad_pipeline.py build <script.py>` runs this compiler automatically whenever a constrained STEP is built (pass `--no-solidcraft` to skip).

Obtain manufacturer CAD when no suitable library model exists. Use simplified envelopes during exploration, then exact hardware where shaft stacks, retention, clearances, or service access depend on it. Preserve already verified instances and mates.

## 3. Verification and release checks

Apply the relevant checks to the selected deliverable. Repeat checks affected by changes; complete integration and release checks before claiming a finished mechanism. A documentation edit does not require regenerating CAD.

### Check 1: Engineering and shop constraints

Compute shaft stacks, center distances, belt pitch lengths, reduction ratios, load paths, and critical clearances before detailed geometry. Record units, datums, source identities, and unresolved assumptions.

Use the recorded shop constraints, with project-specific refinements:

- Gweike M3 Ultra: demonstrated aluminum laser thickness limit 0.125 in (3.175 mm); nominal bed 1380 x 920 mm, subject to actual clamp margins.
- Small Tormach, provisionally PCNC 440: planning travel 254 x 159 x 254 mm; verify the machine and workholding before release.
- Printer planning envelope: 256 x 256 mm XY; include support/brim and establish actual Z/material/process limits. Existing local recipes use at least 0.20 mm diametral printed fit clearance and 13 mm plate spacing; qualify actual fits with coupons rather than treating these as universal tolerances.

### Check 2: Regenerate the parametric source

For local recipes, keep dimensions in an explicit parameter dictionary or dataclass. Reuse [tools/ag_cad_helpers.py](tools/ag_cad_helpers.py):

- `assign_part_metadata()` preserves intended part names and colors, and raises `DisjointSolidError` if a custom non-COTS part consists of multiple disconnected solids (unless `allow_multi_solid=True` is explicitly passed for intentional multi-body parts).
- `load_cots_part(identifier, bore_axis="Z", anchor=..., at=...)` loads a verified COTS STEP model from `cots/catalog.db`, aligns its primary axis, and snaps its semantic datum (`bore_center`, `flange_seat`, `output_shaft`, `sense_origin`, `bottom`, or `top`) to `at`.
- `cut_cots_mounting_holes(plate, identifier, ...)` automatically drills both the central shaft/boss clearance hole and the exact COTS bolt-circle mounting pattern into a custom plate or bracket using the part's precomputed `functional_frames_json`.
- `build_tangent_belt_or_chain()`, `build_meshed_spur_gear_pair()`, and `build_double_shear_shaft_assembly()` synthesize tangent belts/chains, phase-aligned involute spur gear pairs at exact pitch center distance, and double-shear shaft/bearing/spacer/collar stacks.
- `hex_circumradius()` converts across-flats dimensions for a circumscribed hex polygon.
- `rex_bore_polygon_points()`, `gobilda_pattern_coords()`, and `epitrochoid_points()` implement the existing REX, mounting-pattern, and cycloidal recipes.
- `MateGraphBuilder` (opt-in declarative assembly solver) solves 3D part transforms from semantic anchor frames (`add_part`, `add_mate`, `add_rigid_group`, `add_gear_relation`, `export`) and emits both the world-placed `.step` and canonical `*_constraints.json` automatically.
- Construct compounds outside active build123d builder contexts.
- For the established Onshape STEP route, split closed spline profiles into multiple non-periodic edges; single-edge periodic spline extrusions have caused surface imports. Verify imported bodies are solids.

For native Onshape, verify actual feature regeneration and dimensions in the intended document/configuration. Inspect request responses and geometry; HTTP success alone is insufficient.

### Check 3: Geometry, interference, and clearance

Inspect local `.FCStd`, `.step`, or `.stl` outputs with [tools/cad_pipeline.py](tools/cad_pipeline.py) (`inspect` and `lint` / MCP `cad_inspect` and `cad_lint`). Require valid solids and appropriate watertight meshes. Review pairwise intersections and the nearest-clearance table against the specific interface requirements. The pipeline's overlap reporting threshold is not permission for unintended interference, and `--clearance 0.8` below is an example inspection threshold, not a design tolerance.

Run `tools/cad_pipeline.py lint <step_file> --constraints <constraints.json>` (or MCP `cad_lint`) to execute the 5-stage automated physics and kinematic linter (`audit_mechanism_physics()` + `verify_kinematic_sweep()`):

1. B-Rep solid validity and disjoint-solid checks.
2. Static interference filtering (separating expected thread/press/gear-mesh pairs from unexpected solid collisions).
3. Fastener engagement depth and stackup verification (`FASTENER_STACKUP_ERROR`) plus motor/servo/bearing mounting hole confrontation (`MISSING_BOLT_PATTERN_ERROR`).
4. Double-shear vs. cantilevered shaft load-path checks (`CANTILEVER_SHAFT_WARNING`) and minimum 3D-printed bearing boss wall thickness checks (`THIN_BEARING_WALL_ERROR`, $\ge 2.2\text{ mm}$).
5. Multi-pose kinematic sweep collision verification across the mechanism's constraint travel limits.

For native assemblies, verify the corresponding interfaces and moving clearances in Onshape. Record intentional press fits separately from collisions. A static pose does not establish clearance throughout travel.

### Check 4: Visual and section inspection

Inspect the external views and section cut for shaft/bearing stackups, housing walls, retention, fastener access, and assembly order. The local renderer produces five external views plus a section view (`section_iso`) and a six-panel contact sheet retaining the historical `overview_5view.png` suffix. The current CLI exposes `--out` and `--prefix`; older `--section-axis` and `--section-offset` examples do not match its parser.

### Check 5: Assembly, fabrication, and GitHub sync

For Onshape deployments, ground the stationary base, group fixed parts, add functional mate connectors, and constrain moving degrees of freedom and couplings. Follow [AGENTS.md](AGENTS.md) for origin-frame and gear-relation conventions. Verify feature status, actual regeneration, intended motion, limits, and clearances; do not leave a translated assembly floating.

Export from the verified authoritative source into `projects/<project>/cad/` and `projects/<project>/fabrication/` (keeping all 3D-printed parts and ready-to-print build plates inside `projects/<project>/fabrication/prints/`), confirm completed translations and usable files, and record revision, units, material, and quantity. Keep mathematical checks, CAD verification, motion animation, and physical testing distinct. Successful animation does not prove printed fit, belt tracking, loaded stiffness, or game-piece handling.

Never store or mirror files into `Documents` or `OneDrive`. Stage intended source, documentation, and project fabrication deliverables and push them to the GitHub repository (`git push origin main`).

## 4. Onshape transport and request preparation

Use the signed-in browser or the official API Explorer authenticated with an Onshape session. Do not extract cookies or credentials into scripts. Do not silently switch to API-key/private OAuth clients or FeatureScript evaluation/Feature Studio APIs. The local-first modeling route does not change these access rules.

The current pipeline and MCP implementation require care:

| Tool or function | Current behavior |
| --- | --- |
| `inspect`, `lint`, `render`, `export` | Fast in-process OCP 8.0 default for `.step`/`.step.gz`/`.stl` (with automatic `FreeCADCmd.exe` fallback for `.FCStd` or `--engine freecad`), mechanical/kinematic linting, 6-panel Z-buffer rendering, and STEP/STL export |
| `slice-audit` / MCP `cad_slice_audit` | Headless OrcaSlicer CLI + mesh DFM audit (`bambu_256`, `snapmaker_u1`, `prusa_mk4` build-plate envelope, first-layer contact, steep overhangs, and filament mass/time) |
| `fea` / MCP `cad_fea` | Dual-tier 3D B-Rep tetrahedral solid FEA (`Gmsh` + Tier 1 `scikit-fem` `ElementTetP2` with anisotropic FDM Z-layer delamination safety factor & stress PNG, or Tier 2 `CalculiX` `ccx.exe` static & `*BUCKLE`) |
| `physics-sim` / MCP `cad_physics_sim` | Unified `.step` + `*_constraints.json` -> `MJCF` XML + `CoACD` collision hulls simulated in `MuJoCo` or `Genesis` (CUDA/CPU) |
| `solidcraft-live-pick` / MCP `solidcraft_live_pick_to_code` | Maps active 3D viewport selection in live SolidCraft (`7878` / `27878`) back to `build123d` source lines and `params.json` |
| `share` / MCP `cad_share_package` | Packages any project or `.step` into a zero-install interactive 3D WebGL `<name>_team_viewer.html` (Orbit/Explode/Section-Cut/Part-Tree/BOM HUD), binary `.glb`, and portable `<name>_team_bundle.zip` (plus optional `gh release create`) |
| `cots_search`, `cots_info` | Local SQLite catalog search (`--bore-mm`, `--max-extent-mm`, `--has-mounting-holes`) returning `agent_spec` (`bore_profile`, `mounting_pattern`, `axial_stackup_mm`, `kinematics`, `mass_estimate_g`), `mate_graph_snippet`, and Format 52 `.sc` paths (also indexed offline in `cots/cots_agent_index.jsonl`) |
| `solidcraft-insert`, `solidcraft-compile`, `solidcraft-dock` | Local SolidCraft COTS component insertion + concentric bore auto-mating, project STEP + `*_constraints.json` -> jointed `.sc` assembly + BOM + motion GIF compilation, and live HWND-attached COTS HUD dock |
| `build` | Runs the chosen Python recipe and auto-compiles any produced constrained STEP into a jointed `.sc` assembly (`--no-solidcraft` to skip); inspect its side effects and output paths first |
| `make_assembly_*_payload` helpers | Build request dictionaries from supplied identities and parameters |
| `build_onshape_browser_sync_js` | Pure offline helper that generates a browser-session `fetch()` script (using `X-XSRF-TOKEN`) to upload a STEP or apply `constraints.json` inside the signed-in Onshape browser tab |
| `onshape-push`, `onshape-constrain`, `onshape-export` | Live direct API-key operations, not session-authenticated browser calls |
| `onshape-constrain --emit-browser-bundle` | Executes live reads and mutations before writing the bundle; not an offline or zero-quota mode |
| `onshape-constrain --clear` | Deletes existing assembly features before creating replacements |

The corresponding cloud wrappers in [tools/cad_mcp_server.py](tools/cad_mcp_server.py) retain the pipeline's authentication and side effects. An MCP name does not establish whether it is read-only, local, or session-authenticated. Historical preparation-only MCP/cache instructions may refer to an earlier implementation; inspect the connected tool before using it.

For session work:

1. Observe the current document/workspace/element, configuration, source geometry, and assembly occurrence IDs through the signed-in browser or Explorer.
2. Prepare standard feature/mate JSON locally from those observations (or generate a self-contained browser-session sync snippet with `build_onshape_browser_sync_js()`). Pure payload construction is allowed; invoking the live pipeline commands is not an offline substitute.
3. Submit requests through the session-authenticated surface. Use returned feature IDs for dependent mates/relations; save receipts and verify the target head between logical work batches.
4. Read regeneration status and inspect the actual geometry/motion. If a mutation times out ambiguously, observe before retrying to avoid duplicate features.

URL-encode feature IDs in request paths. The direct client's retry/backoff behavior does not change its authentication or annual-allowance use.

## 5. Local command examples

Run from the workspace repository root. Check the selected recipe before execution; these examples are not instructions to rebuild or publish an existing model during unrelated work.

```powershell
.\.venv\Scripts\python.exe tools/cad_scraper.py search "1/2 hex bearing" --limit 5
.\.venv\Scripts\python.exe tools/cad_scraper.py search "bearing" --bore 8.0 --limit 5
.\.venv\Scripts\python.exe tools/cad_scraper.py info REV-21-1650
.\.venv\Scripts\python.exe tools/cad_pipeline.py inspect projects/cycloidal-actuator/cad/cycloidal_actuator_15to1.step --interference --clearance 0.8
.\.venv\Scripts\python.exe tools/cad_pipeline.py lint projects/ftc-cots-belt-drive/cad/ftc_cots_belt_drive.step --constraints projects/ftc-cots-belt-drive/cad/constraints.json
.\.venv\Scripts\python.exe tools/cad_pipeline.py render projects/cycloidal-actuator/cad/cycloidal_actuator_15to1.step --out projects/cycloidal-actuator/docs
.\.venv\Scripts\python.exe tools/cad_pipeline.py build projects/cycloidal-actuator/src/build_cycloidal_actuator.py
.\.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-compile projects/ftc-cots-belt-drive/cad/ftc_cots_belt_drive.step --live
.\.venv\Scripts\python.exe tools/cad_pipeline.py solidcraft-insert "1611-0514-0008" --target-occurrence "Plate:1" --joint-type rigid --anchor flange_seat
.\.venv\Scripts\python.exe tools/solidcraft_plugin.py run
.\.venv\Scripts\python.exe tools/cad_pipeline.py slice-audit projects/kinesis-freestyle-edge-riser/fabrication/prints/left_tent_riser.stl --bed-profile snapmaker_u1
.\.venv\Scripts\python.exe tools/cad_pipeline.py fea projects/cycloidal-actuator/cad/cycloidal_actuator_15to1.step --material PLA --print-axis Z --backend skfem
.\.venv\Scripts\python.exe tools/cad_pipeline.py physics-sim projects/ftc-cots-belt-drive/cad/ftc_cots_belt_drive.step --constraints projects/ftc-cots-belt-drive/cad/constraints.json --engine mujoco
.\.venv\Scripts\python.exe tools/cad_pipeline.py share projects/cycloidal-actuator
.\.venv\Scripts\python.exe tools/cad_pipeline.py export projects/dual-flywheel-shooter/cad/dual_flywheel_shooter.FCStd --step projects/dual-flywheel-shooter/cad/dual_flywheel_shooter.step --stl projects/dual-flywheel-shooter/fabrication/prints/dual_flywheel_shooter.stl
```

