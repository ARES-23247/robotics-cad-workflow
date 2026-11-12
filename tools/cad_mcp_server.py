"""
Antigravity (AG) Local-First + Onshape CAD MCP Server (`tools/cad_mcp_server.py`)
=================================================================================
Implements the Model Context Protocol (JSON-RPC 2.0 over stdio) exposing:
  - `cad_inspect`: Headless FreeCAD 1.0 B-Rep & STL audit (manifold validity, volume,
    boolean interference, distToShape clearance warnings, and nearest-part clearances).
  - `cad_render`: 5-view orthographic/isometric + 3D cutaway section Z-buffer renderer.
  - `cad_export`: Local STEP AP214 and watertight STL exporter.
  - `cad_build123d`: Executes parametric `build123d` Python scripts via `.venv`.
  - `onshape_push`: Uploads a local `.step` to personal Onshape + optional cloud render.
  - `onshape_export`: Exports `.step`, `.x_t`, or `.stl` from Onshape via Direct REST API.
"""

import json
import sys
import traceback
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.cad_pipeline import (
    export_cad,
    inspect_cad,
    lint_cad_assembly,
    onshape_constrain_assembly,
    onshape_export,
    onshape_push,
    render_cad,
    run_build123d_script,
)
from tools.cad_scraper import cots_info, cots_search
from tools.solidcraft_cots import solidcraft_insert_cots

TOOLS_SCHEMA = [
    {
        "name": "cad_inspect",
        "description": (
            "Audit a .FCStd, .step, .step.gz, or .stl file locally (in-process OCP 8.0 default, FreeCAD 1.0 for .FCStd). "
            "Returns solid counts, manifold validity, XCAF part labels, bounding boxes, "
            "boolean interferences (>5 mm^3), clearance warnings, and top nearest-part gaps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to .FCStd, .step, .step.gz, or .stl file"},
                "check_interference": {"type": "boolean", "default": True},
                "clearance_threshold_mm": {"type": "number", "default": 0.5},
                "engine": {"type": "string", "enum": ["auto", "ocp", "freecad"], "default": "auto"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "cad_lint",
        "description": (
            "Run the Automated Mechanical Sanity & Physics Linter on a multi-body STEP assembly. "
            "Audits 3D structural support connectivity (FLOATING_PART_ERROR), actuator output shaft "
            "coaxial coupling (UNCOUPLED_ACTUATOR_ERROR), game-piece swept tunnel / wheel contact / "
            "coaxial gear OD clearance (BLOCKED_BALL_PATH_ERROR, NON_CONTACTING_WHEEL_ERROR, "
            "GEAR_EXCEEDS_WHEEL_OD_ERROR), and multi-pose revolute sweep collisions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to assembly .step file"},
                "constraints_path": {"type": "string", "description": "Optional path to *_constraints.json"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "cad_render",
        "description": (
            "Tessellate a .FCStd, .step, .step.gz, or .stl file (in-process OCP 8.0 default) and render 5 standard views "
            "(iso_top, iso_bottom, top, front, left), a 3D mid-plane cutaway section view (section_iso), "
            "and a 6-panel engineering contact sheet (overview_5view) using a per-pixel Z-buffer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to .FCStd, .step, .step.gz, or .stl file"},
                "out_dir": {"type": "string", "description": "Output directory for PNG renders"},
                "prefix": {"type": "string", "description": "Optional filename prefix"},
                "engine": {"type": "string", "enum": ["auto", "ocp", "freecad"], "default": "auto"},
            },
            "required": ["file_path", "out_dir"],
        },
    },
    {
        "name": "cad_export",
        "description": "Export STEP AP214 and/or watertight STL locally (in-process OCP 8.0 default, FreeCAD 1.0 for .FCStd).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Input .FCStd, .step, or .step.gz file"},
                "step_path": {"type": "string", "description": "Optional output .step file path"},
                "stl_path": {"type": "string", "description": "Optional output .stl file path"},
                "engine": {"type": "string", "enum": ["auto", "ocp", "freecad"], "default": "auto"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "cad_build123d",
        "description": "Execute a parametric build123d Python script using the repository .venv (Python 3.12 + OCP).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script_path": {"type": "string", "description": "Path to the build123d .py script"},
                "args": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["script_path"],
        },
    },
    {
        "name": "cots_search",
        "description": (
            "Search the local COTS STEP catalog (goBILDA, REV DUO FTC, REV ION FRC, Swyft, legacy), "
            "Onshape FRCDesignApp libraries (ftc-design-lib, frc-design-lib, mkcad), and the 7,000+ "
            "FRC/FTC SKU variant index by SKU, name, vendor, category, program (FTC/FRC), or B-Rep geometry "
            "(bore_mm, max_extent_mm, has_mounting_holes) with automatic engineering synonym expansion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword or SKU (e.g. '1/2 hex bearing', 'NEO', '5203', 'WCP-0750', 'MAXSwerve')"},
                "vendor": {"type": "string", "description": "Optional vendor/library filter (e.g. 'gobilda', 'rev', 'swyft', 'wcp', 'am', 'sds', 'ctre', 'ftc-design-lib', 'frc-design-lib', 'mkcad')"},
                "category": {"type": "string", "description": "Optional category filter"},
                "program": {"type": "string", "enum": ["all", "FTC", "FRC"], "default": "all", "description": "Filter by robotics program: 'FTC', 'FRC', or 'all'"},
                "source": {"type": "string", "enum": ["all", "local", "onshape", "variants"], "default": "all"},
                "limit": {"type": "integer", "default": 25},
                "bore_mm": {"type": "number", "description": "Optional filter for target bore diameter in mm (within 0.65mm tolerance)"},
                "max_extent_mm": {"type": "number", "description": "Optional filter for maximum bounding box dimension in mm"},
                "has_mounting_holes": {"type": "boolean", "description": "Optional filter requiring detected mounting holes in functional_frames_json"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cots_info",
        "description": (
            "Inspect full B-Rep geometric fingerprint (bounding box, extents, volume, center of mass, "
            "functional frames, recommended semantic anchor, local STEP path, and normalized SolidCraft .sc path/frames) "
            "or Onshape FRCDesignApp insertable configuration schema for a COTS part."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "part_key_or_sku": {"type": "string", "description": "Part key, SKU, or insertable ID"},
            },
            "required": ["part_key_or_sku"],
        },
    },
    {
        "name": "solidcraft_insert_cots",
        "description": (
            "Insert a robotics COTS part (goBILDA, REV DUO/ION, Swyft, ThriftyBot, Ruthex, McMaster) into the active "
            "SolidCraft design (live GUI on port 27878 or headless .sc file) from the normalized cots/solidcraft/*.sc "
            "library with `reuse: true`, automatically registering named `joint.origin`s for its functional frames "
            "(bore_center, flange_seat, output_shaft, mount_hole_1..N) and optionally mating it to a target occurrence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "part_key_or_sku": {"type": "string", "description": "SKU, part_id, or search query (e.g. '5203-2402-0019', '1611-0514-0008', 'REV-21-3005', 'collar8')"},
                "component_name": {"type": "string", "description": "Optional custom component name"},
                "translate": {"type": "array", "items": {"type": "number"}, "description": "Optional [x, y, z] translation in mm"},
                "anchor": {"type": "string", "description": "Semantic anchor frame on the COTS part ('bore_center', 'flange_seat', 'output_shaft', 'sense_origin', 'bbox_center')"},
                "joint_type": {"type": "string", "enum": ["rigid", "revolute", "slider", "cylindrical", "planar", "ball"], "description": "Optional joint type to mate the COTS part immediately"},
                "target_occurrence": {"type": "string", "description": "Target occurrence in the assembly to mate to (e.g. 'Frame:1')"},
                "target_point": {"type": "array", "items": {"type": "number"}, "description": "World [x, y, z] point on target_occurrence"},
                "target_axis": {"type": "array", "items": {"type": "number"}, "description": "World [ax, ay, az] axis on target_occurrence (default [0, 0, 1])"},
                "flip": {"type": "boolean", "default": True, "description": "Align Z axes in the same direction (default True)"},
                "limits": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}, "description": "Optional joint limits [[min, max]]"},
                "connect_port": {"type": "integer", "default": 27878, "description": "Live SolidCraft control port (default 27878)"},
                "design_path": {"type": "string", "description": "Optional headless .sc file path instead of live GUI"},
                "register_joint_origins": {"type": "boolean", "default": True, "description": "Register named joint.origins for all functional frames of the inserted part"},
            },
            "required": ["part_key_or_sku"],
        },
    },
    {
        "name": "solidcraft_compile_project",
        "description": (
            "Compile a project STEP assembly (.step or .step.gz) and *_constraints.json into a "
            "self-contained, jointed, motion-coupled SolidCraft .sc assembly using Hybrid In-Place COTS Tagging. "
            "Automatically generates the .sc assembly, grouped BOM CSV/Markdown, mass/inertia report, "
            "driven kinematic motion .gif animation, and dynamic joint.sweep collision check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_path": {"type": "string", "description": "Path to project .step or .step.gz file"},
                "constraints_path": {"type": "string", "description": "Optional path to *_constraints.json (auto-discovered if omitted)"},
                "out_sc_path": {"type": "string", "description": "Optional output .sc path"},
                "export_gif": {"type": "boolean", "default": True, "description": "Export animated .gif of primary driven mechanism"},
                "live_push": {"type": "boolean", "default": False, "description": "Open the compiled .sc assembly in the live SolidCraft GUI"},
                "connect_port": {"type": "integer", "default": 27878, "description": "Live SolidCraft --control port"},
            },
            "required": ["step_path"],
        },
    },
    {
        "name": "onshape_push",
        "description": (
            "Upload a local .step file to a new or existing personal Onshape Document, "
            "automatically constrain the Assembly (or apply constraints_path), and optionally render."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to local .step file"},
                "doc_name": {"type": "string", "description": "Name for new Onshape document"},
                "did": {"type": "string", "description": "Optional existing Onshape Document ID"},
                "wid": {"type": "string", "description": "Optional existing Onshape Workspace ID"},
                "constraints_path": {"type": "string", "description": "Optional path to JSON constraint specification"},
                "auto_ground": {"type": "boolean", "default": True},
                "render_out": {"type": "string", "description": "Optional output path for cloud shaded view PNG"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "onshape_constrain",
        "description": (
            "Add deterministic Origin-anchored Mate Connectors (BTMMateConnector-66), "
            "Mate Groups (BTMMateGroup-65), Mates (BTMMate-64), and Gear Relations (BTMMateRelation-1412) "
            "to an Onshape Assembly, and optionally emit a browser-session JSON bundle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "did": {"type": "string"},
                "wid": {"type": "string"},
                "eid_assembly": {"type": "string"},
                "spec": {"type": "object", "description": "Optional constraint specification dict"},
                "clear_existing": {"type": "boolean", "default": False},
                "emit_browser_bundle": {"type": "string", "description": "Optional path to save browser session requests"},
            },
            "required": ["did", "wid", "eid_assembly"],
        },
    },
    {
        "name": "onshape_export",
        "description": "Export STEP, PARASOLID, or STL from an Onshape Part Studio via Direct REST API.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "did": {"type": "string"},
                "wid": {"type": "string"},
                "eid": {"type": "string"},
                "format": {"type": "string", "enum": ["STEP", "PARASOLID", "STL"], "default": "STEP"},
                "out_path": {"type": "string"},
            },
            "required": ["did", "wid", "eid", "out_path"],
        },
    },
    {
        "name": "cad_git_status",
        "description": "Project-grouped Git status report with file sizes (MB), >50MB/>95MB large-file flags, CAD file-hygiene warnings, and 3D visual diff availability.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Optional project/scope filter (e.g. 'projects/ftc-swerve-chassis')"},
            },
        },
    },
    {
        "name": "cad_git_hygiene",
        "description": "Audit and optionally auto-fix CAD file-organization issues (misplaced .step in fabrication/prints/, COTS files in fabrication/, duplicate uncompressed .step alongside .step.gz, and >50MB binaries needing .gz compression).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Optional project/scope filter"},
                "fix": {"type": "boolean", "default": False, "description": "Auto-fix detected hygiene & >50MB gzip issues"},
            },
        },
    },
    {
        "name": "cad_git_visual_diff",
        "description": "Compare a modified working-tree .sc, .step, or .step.gz file against Git HEAD in 3D via SolidCraft inspect.compare, returning volume delta, max surface deviation, and a Red/Green 3D visual diff PNG.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to modified .sc, .step, or .step.gz file"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "cad_git_commit_project",
        "description": "Run CAD file hygiene & >95MB GitHub protection, stage a project scope, commit with message, and optionally push to origin.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "Project scope (e.g. 'projects/ftc-cots-belt-drive' or 'all')"},
                "message": {"type": "string", "description": "Commit message"},
                "push": {"type": "boolean", "default": True},
                "auto_fix_hygiene": {"type": "boolean", "default": True},
            },
            "required": ["scope", "message"],
        },
    },
    {
        "name": "solidcraft_live_inspect",
        "description": "Inspect the live SolidCraft GUI scene on 127.0.0.1:27878 in <5 ms: active file, world body count, occurrence count, mass/CoM, driven joints with limits & 3D origins, motion links, and user parameters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "solidcraft_live_clearance_audit",
        "description": "Run a parallel B-Rep clearance & interference audit on the live SolidCraft GUI scene, classifying pairs into COLLISION_FAIL, NEAR_CLEARANCE_WARN, and EXPECTED_INTERFACE with 3D focus coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "clearance_mm": {"type": "number", "default": 1.0},
                "custom_only": {"type": "boolean", "default": True},
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "solidcraft_live_focus",
        "description": "Focus the live SolidCraft 3D camera on a [x, y, z] point, apply a view preset, or slice an XY/XZ/YZ section cut at the target point.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_xyz": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] in mm"},
                "section_plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
                "clear_section": {"type": "boolean", "default": False},
                "view_preset": {"type": "string", "enum": ["iso", "front", "back", "left", "right", "top", "bottom"]},
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "solidcraft_live_drive_joint",
        "description": "Drive a joint (and any motion-linked follower joints) in the live SolidCraft GUI viewport, automatically redirecting follower joints to their master driver joint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "joint": {"type": "string", "description": "Joint ID or name"},
                "value": {"type": "number", "description": "Target angle (deg) or distance (mm)"},
                "port": {"type": "integer", "default": 27878},
            },
            "required": ["joint", "value"],
        },
    },
    {
        "name": "solidcraft_live_sweep_joint",
        "description": "Sweep a joint across [from_val, to_val] in the live SolidCraft GUI, reporting minimum clearance gap and auto-parking/sectioning at the exact 3D contact point if blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "joint": {"type": "string"},
                "from_val": {"type": "number", "default": 0.0},
                "to_val": {"type": "number", "default": 360.0},
                "steps": {"type": "integer", "default": 24},
                "auto_focus_on_contact": {"type": "boolean", "default": True},
                "port": {"type": "integer", "default": 27878},
            },
            "required": ["joint"],
        },
    },
    {
        "name": "solidcraft_live_change_param",
        "description": "Update a user parameter in the live SolidCraft GUI and optionally run a ±15% inspect.parameter_sweep check.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "expression": {"type": "string"},
                "run_sweep": {"type": "boolean", "default": False},
                "port": {"type": "integer", "default": 27878},
            },
            "required": ["name", "expression"],
        },
    },
    {
        "name": "solidcraft_live_sync_and_pull",
        "description": "Open a .sc file, hot-reload linked subassemblies (component.update_links), save the active .sc file, and pull *_sc_script.json + *_sc_params.json to Git.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "update_links": {"type": "boolean", "default": True},
                "save_design": {"type": "boolean", "default": True},
                "pull_to_git_json": {"type": "boolean", "default": True},
                "open_sc_path": {"type": "string"},
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "solidcraft_live_export_fab",
        "description": "Export the full project fabrication pack from the live SolidCraft GUI assembly: fine-mesh 3D print STLs in fabrication/prints/, laser DXFs in fabrication/laser/, and *_sc_bom.md in docs/.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "solidcraft_live_pick_to_code",
        "description": "Query the active 3D viewport selection in the live SolidCraft GUI (port 27878 / 27878) and map the picked occurrence and 3D coordinates back to build123d Python source lines and params.json keys.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Optional project directory (auto-inferred from open .sc path if omitted)"},
                "port": {"type": "integer", "default": 27878},
            },
        },
    },
    {
        "name": "cad_slice_audit",
        "description": "Headless OrcaSlicer CLI & mesh DFM audit on a .stl or .3mf print file/plate: checks build-plate envelope (bambu_256, snapmaker_u1, prusa_mk4), manifold integrity, first-layer contact area, steep overhangs, and filament mass/time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to .stl or .3mf print file or plate"},
                "bed_profile": {"type": "string", "enum": ["bambu_256", "snapmaker_u1", "prusa_mk4"], "default": "bambu_256"},
                "filament": {"type": "string", "default": "PLA"},
                "run_slicer_cli": {"type": "boolean", "default": True},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "cad_fea",
        "description": "Run dual-tier 3D B-Rep tetrahedral solid FEA on a .step file using Gmsh + Tier 1 scikit-fem (ElementTetP2 + FDM Z-layer delamination safety factor + deformed Von Mises stress PNG) or Tier 2 CalculiX (ccx.exe static & *BUCKLE).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_path": {"type": "string", "description": "Path to .step file"},
                "material": {"type": "string", "default": "PLA", "description": "Material key (PLA, PETG, PA-CF, ABS, 6061-T6, 7075-T6, POLYCARBONATE, STEEL_4140)"},
                "fixed_selector": {"type": "string", "default": "xmin", "description": "Fixed boundary selector (xmin, xmax, ymin, ymax, zmin, zmax)"},
                "load_selector": {"type": "string", "default": "xmax", "description": "Loaded boundary selector (xmin, xmax, ymin, ymax, zmin, zmax)"},
                "force_n": {"type": "array", "items": {"type": "number"}, "description": "Applied total force vector [Fx, Fy, Fz] in N"},
                "g_load_vec_g": {"type": "array", "items": {"type": "number"}, "description": "Optional body G-load vector [Gx, Gy, Gz] in multiples of g"},
                "print_axis": {"type": "string", "enum": ["X", "Y", "Z"], "default": "Z", "description": "FDM build-plate normal axis for Z-layer delamination audit"},
                "mesh_size_mm": {"type": "number", "description": "Optional tetrahedral element size in mm"},
                "backend": {"type": "string", "enum": ["skfem", "calculix"], "default": "skfem"},
                "buckle_modes": {"type": "integer", "default": 0},
                "out_png": {"type": "string", "description": "Optional output PNG path for deformed Von Mises stress heatmap"},
                "out_inp": {"type": "string", "description": "Optional output CalculiX .inp deck path"},
            },
            "required": ["step_path"],
        },
    },
    {
        "name": "cad_physics_sim",
        "description": "Compile a .step assembly + *_constraints.json to MJCF XML (with CoACD concave collision hulls, gear/belt equality constraints, springs, and game pieces) and step multibody/soft-body physics in MuJoCo or Genesis (CUDA/CPU).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step_path": {"type": "string", "description": "Path to assembly .step file"},
                "constraints_path": {"type": "string", "description": "Optional path to *_constraints.json"},
                "engine": {"type": "string", "enum": ["mujoco", "genesis"], "default": "mujoco"},
                "duration_s": {"type": "number", "default": 0.6},
                "spring_configs": {"type": "object", "description": "Optional map of joint name -> {stiffness_nm_per_rad, springref_deg}"},
                "actuator_configs": {"type": "object", "description": "Optional map of joint name -> {gear, ctrl}"},
                "game_pieces": {"type": "array", "items": {"type": "object"}, "description": "Optional dynamic game pieces [{name, shape, radius_mm, mass_kg, pos_mm}]"},
                "out_mjcf": {"type": "string", "description": "Optional output MJCF .xml path"},
                "out_gif": {"type": "string", "description": "Optional output animated .gif path"},
                "interactive": {"type": "boolean", "default": False},
            },
            "required": ["step_path"],
        },
    },
    {
        "name": "cad_share_package",
        "description": "Package any project folder or .step assembly into a zero-install interactive 3D WebGL HTML viewer (<name>_team_viewer.html with Orbit/Explode/Section-Cut/Part-Tree/BOM HUD), binary .glb, and portable <name>_team_bundle.zip (plus optional GitHub Release).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "Path to project folder (projects/<name>) or .step/.sc assembly"},
                "out_dir": {"type": "string", "description": "Optional output directory"},
                "linear_deflection": {"type": "number", "default": 0.25},
                "create_zip": {"type": "boolean", "default": True},
                "gh_release_tag": {"type": "string", "description": "Optional GitHub Release tag to publish via gh release create"},
            },
            "required": ["target_path"],
        },
    },
]


def handle_tool_call(name: str, arguments: dict) -> dict:
    if name == "cad_inspect":
        return inspect_cad(
            Path(arguments["file_path"]),
            check_interference=bool(arguments.get("check_interference", True)),
            clearance_threshold=float(arguments.get("clearance_threshold_mm", 0.5)),
            engine=str(arguments.get("engine", "auto")),
        )
    elif name == "cad_lint":
        return lint_cad_assembly(
            Path(arguments["file_path"]),
            constraints_path=Path(arguments["constraints_path"]) if arguments.get("constraints_path") else None,
        )
    elif name == "cad_render":
        return render_cad(
            Path(arguments["file_path"]),
            Path(arguments["out_dir"]),
            prefix=arguments.get("prefix"),
            engine=str(arguments.get("engine", "auto")),
        )
    elif name == "cad_export":
        return export_cad(
            Path(arguments["file_path"]),
            step_path=Path(arguments["step_path"]) if arguments.get("step_path") else None,
            stl_path=Path(arguments["stl_path"]) if arguments.get("stl_path") else None,
            engine=str(arguments.get("engine", "auto")),
        )
    elif name == "cad_build123d":
        out = run_build123d_script(Path(arguments["script_path"]), arguments.get("args", []))
        return {"status": "ok", "stdout": out}
    elif name == "cots_search":
        return cots_search(
            query=arguments.get("query", ""),
            vendor=arguments.get("vendor"),
            category=arguments.get("category"),
            program=arguments.get("program"),
            source=arguments.get("source", "all"),
            limit=int(arguments.get("limit", 25)),
            bore_mm=float(arguments["bore_mm"]) if arguments.get("bore_mm") is not None else None,
            max_extent_mm=float(arguments["max_extent_mm"]) if arguments.get("max_extent_mm") is not None else None,
            has_mounting_holes=bool(arguments["has_mounting_holes"]) if arguments.get("has_mounting_holes") is not None else None,
        )
    elif name == "cots_info":
        return cots_info(arguments["part_key_or_sku"])
    elif name == "solidcraft_insert_cots":
        return solidcraft_insert_cots(
            part_key_or_sku=arguments["part_key_or_sku"],
            component_name=arguments.get("component_name"),
            translate=arguments.get("translate"),
            anchor=arguments.get("anchor"),
            joint_type=arguments.get("joint_type"),
            target_occurrence=arguments.get("target_occurrence"),
            target_point=arguments.get("target_point"),
            target_axis=arguments.get("target_axis"),
            flip=bool(arguments.get("flip", True)),
            limits=arguments.get("limits"),
            connect_port=int(arguments.get("connect_port", 27878)),
            design_path=arguments.get("design_path"),
            register_joint_origins=bool(arguments.get("register_joint_origins", True)),
        )
    elif name == "solidcraft_compile_project":
        from tools.solidcraft_compiler import compile_project_to_solidcraft

        return compile_project_to_solidcraft(
            step_path=Path(arguments["step_path"]),
            constraints_path=Path(arguments["constraints_path"]) if arguments.get("constraints_path") else None,
            out_sc_path=Path(arguments["out_sc_path"]) if arguments.get("out_sc_path") else None,
            export_gif=bool(arguments.get("export_gif", True)),
            live_push=bool(arguments.get("live_push", False)),
            connect_port=int(arguments.get("connect_port", 27878)),
        )
    elif name == "onshape_push":
        return onshape_push(
            Path(arguments["file_path"]),
            doc_name=arguments.get("doc_name"),
            did=arguments.get("did"),
            wid=arguments.get("wid"),
            render_out=Path(arguments["render_out"]) if arguments.get("render_out") else None,
            constraints_path=Path(arguments["constraints_path"]) if arguments.get("constraints_path") else None,
            auto_ground=bool(arguments.get("auto_ground", True)),
        )
    elif name == "onshape_constrain":
        return onshape_constrain_assembly(
            did=arguments["did"],
            wid=arguments["wid"],
            eid_asm=arguments["eid_assembly"],
            spec=arguments.get("spec"),
            clear_existing=bool(arguments.get("clear_existing", False)),
            emit_browser_bundle=Path(arguments["emit_browser_bundle"]) if arguments.get("emit_browser_bundle") else None,
        )
    elif name == "onshape_export":
        out_p = Path(arguments["out_path"])
        fmt = arguments.get("format", "STEP")
        onshape_export(arguments["did"], arguments["wid"], arguments["eid"], fmt, out_p)
        return {"status": "ok", "out_path": str(out_p), "format": fmt}
    elif name == "cad_git_status":
        from tools.cad_git_manager import cad_git_status

        return cad_git_status(scope_filter=arguments.get("scope"))
    elif name == "cad_git_hygiene":
        from tools.cad_git_manager import cad_file_hygiene_audit

        return cad_file_hygiene_audit(scope_filter=arguments.get("scope"), fix=bool(arguments.get("fix", False)))
    elif name == "cad_git_visual_diff":
        from tools.cad_git_manager import cad_visual_diff_head

        return cad_visual_diff_head(arguments["path"])
    elif name == "cad_git_commit_project":
        from tools.cad_git_manager import cad_git_stage_commit_push

        return cad_git_stage_commit_push(
            scope=arguments["scope"],
            message=arguments["message"],
            push=bool(arguments.get("push", True)),
            auto_fix_hygiene=bool(arguments.get("auto_fix_hygiene", True)),
        )
    elif name == "solidcraft_live_inspect":
        from tools.solidcraft_inspector import live_inspect_state

        return live_inspect_state(port=int(arguments.get("port", 27878)))
    elif name == "solidcraft_live_clearance_audit":
        from tools.solidcraft_inspector import live_run_clearance_audit

        return live_run_clearance_audit(
            port=int(arguments.get("port", 27878)),
            clearance_mm=float(arguments.get("clearance_mm", 1.0)),
            custom_only=bool(arguments.get("custom_only", True)),
        )
    elif name == "solidcraft_live_focus":
        from tools.solidcraft_inspector import live_focus_viewport

        return live_focus_viewport(
            target_xyz=arguments.get("target_xyz"),
            section_plane=arguments.get("section_plane"),
            clear_section=bool(arguments.get("clear_section", False)),
            view_preset=arguments.get("view_preset"),
            port=int(arguments.get("port", 27878)),
        )
    elif name == "solidcraft_live_drive_joint":
        from tools.solidcraft_inspector import live_drive_joint

        return live_drive_joint(
            joint=str(arguments["joint"]),
            value=float(arguments["value"]),
            port=int(arguments.get("port", 27878)),
        )
    elif name == "solidcraft_live_sweep_joint":
        from tools.solidcraft_inspector import live_sweep_joint

        return live_sweep_joint(
            joint=str(arguments["joint"]),
            from_val=float(arguments.get("from_val", 0.0)),
            to_val=float(arguments.get("to_val", 360.0)),
            steps=int(arguments.get("steps", 24)),
            auto_focus_on_contact=bool(arguments.get("auto_focus_on_contact", True)),
            port=int(arguments.get("port", 27878)),
        )
    elif name == "solidcraft_live_change_param":
        from tools.solidcraft_inspector import live_change_parameter

        return live_change_parameter(
            name=str(arguments["name"]),
            expression=str(arguments["expression"]),
            run_sweep=bool(arguments.get("run_sweep", False)),
            port=int(arguments.get("port", 27878)),
        )
    elif name == "solidcraft_live_sync_and_pull":
        from tools.solidcraft_inspector import live_sync_and_pull

        return live_sync_and_pull(
            update_links=bool(arguments.get("update_links", True)),
            save_design=bool(arguments.get("save_design", True)),
            pull_to_git_json=bool(arguments.get("pull_to_git_json", True)),
            open_sc_path=arguments.get("open_sc_path"),
            port=int(arguments.get("port", 27878)),
        )
    elif name == "solidcraft_live_export_fab":
        from tools.solidcraft_inspector import live_export_fabrication_pack

        return live_export_fabrication_pack(port=int(arguments.get("port", 27878)))
    elif name == "solidcraft_live_pick_to_code":
        from tools.solidcraft_inspector import live_pick_to_source

        return live_pick_to_source(
            project_dir=Path(arguments["project_dir"]) if arguments.get("project_dir") else None,
            port=int(arguments.get("port", 27878)),
        )
    elif name == "cad_slice_audit":
        from tools.cad_slicer_audit import slice_audit_print

        return slice_audit_print(
            file_path=Path(arguments["file_path"]),
            bed_profile=str(arguments.get("bed_profile", "bambu_256")),
            filament=str(arguments.get("filament", "PLA")),
            run_slicer_cli=bool(arguments.get("run_slicer_cli", True)),
        )
    elif name == "cad_fea":
        from tools.cad_fea import run_3d_fea

        return run_3d_fea(
            step_path=Path(arguments["step_path"]),
            material=str(arguments.get("material", "PLA")),
            fixed_selector=arguments.get("fixed_selector", "xmin"),
            load_selector=arguments.get("load_selector", "xmax"),
            force_n=arguments.get("force_n"),
            g_load_vec_g=arguments.get("g_load_vec_g"),
            print_axis=str(arguments.get("print_axis", "Z")),
            mesh_size_mm=float(arguments["mesh_size_mm"]) if arguments.get("mesh_size_mm") is not None else None,
            backend=str(arguments.get("backend", "skfem")),
            buckle_modes=int(arguments.get("buckle_modes", 0)),
            out_png=Path(arguments["out_png"]) if arguments.get("out_png") else None,
            out_inp=Path(arguments["out_inp"]) if arguments.get("out_inp") else None,
        )
    elif name == "cad_physics_sim":
        from tools.cad_physics_sim import compile_and_simulate_physics

        return compile_and_simulate_physics(
            step_path=Path(arguments["step_path"]),
            constraints_path=Path(arguments["constraints_path"]) if arguments.get("constraints_path") else None,
            engine=str(arguments.get("engine", "mujoco")),
            duration_s=float(arguments.get("duration_s", 0.6)),
            spring_configs=arguments.get("spring_configs"),
            actuator_configs=arguments.get("actuator_configs"),
            game_pieces=arguments.get("game_pieces"),
            out_mjcf=Path(arguments["out_mjcf"]) if arguments.get("out_mjcf") else None,
            out_gif=Path(arguments["out_gif"]) if arguments.get("out_gif") else None,
            interactive=bool(arguments.get("interactive", False)),
        )
    elif name == "cad_share_package":
        from tools.cad_team_share import build_team_share_package

        return build_team_share_package(
            target_path=Path(arguments["target_path"]),
            out_dir=Path(arguments["out_dir"]) if arguments.get("out_dir") else None,
            linear_deflection=float(arguments.get("linear_deflection", 0.25)),
            create_zip=bool(arguments.get("create_zip", True)),
            gh_release_tag=arguments.get("gh_release_tag"),
        )
    else:
        raise ValueError(f"Unknown tool: {name}")


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ag_cad_local", "version": "2.0.0"},
            },
        }
    elif method in ("notifications/initialized", "initialized"):
        return None
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS_SCHEMA},
        }
    elif method == "tools/call":
        params = req.get("params", {})
        tname = params.get("name", "")
        targs = params.get("arguments", {})
        try:
            res = handle_tool_call(tname, targs)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(res, indent=2)}],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}\n{traceback.format_exc()}"}],
                    "isError": True,
                },
            }
    elif req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
