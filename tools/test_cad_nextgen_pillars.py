"""
Comprehensive Test Suite for the 4 Additive Agentic CAD Pipeline Pillars:
1. Pillar 1: Declarative `MateGraphBuilder` (`ag_cad_helpers.py`) + Viewport Pick-to-Code & Bi-Directional Params (`solidcraft_inspector.py`).
2. Pillar 2: Unified `.step` + `*_constraints.json` -> `MJCF` Physics Compiler (`MuJoCo` + `Genesis` backends + `CoACD` collision hulls).
3. Pillar 3: Dual-Tier 3D B-Rep Solid FEA (`Gmsh` STEP meshing + `scikit-fem` with FDM Z-layer delamination + `CalculiX` `ccx.exe`).
4. Pillar 4: Drop-in Fast In-Process OCP 8.0 `inspect` / `export` / `render` + Headless `OrcaSlicer` CLI Print DFM Audit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import time

from PIL import Image
import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


# ---------------------------------------------------------------------------
# Pillar 4A: Fast In-Process OCP 8.0 inspect / export / render Parity Tests
# ---------------------------------------------------------------------------
def test_ocp_fast_inspect_export_and_render_parity(tmp_path: Path):
    """Verify OCP 8.0 in-process inspect/export/render matches schema, detects gaps & overlaps, and renders 6-view sheet."""
    from build123d import Axis, Box, BuildPart, Compound, Cylinder, Locations, Mode, export_step, export_stl
    from tools.ag_cad_helpers import assign_part_metadata
    from tools.cad_pipeline import export_cad, inspect_cad, render_cad

    with BuildPart() as base_bp:
        Box(50, 40, 4)
        Cylinder(radius=4.0, height=4.0, mode=Mode.SUBTRACT)
        # Keep simple clean solid
    with BuildPart() as cap_bp:
        with Locations((0, 0, 3.6)):  # 0.60 mm Z gap above Z=+2.0 top of BasePlate (since cap height=2 -> Z in [2.6, 4.6])
            Box(24, 24, 2.0)

    p_base = assign_part_metadata(base_bp.part, "BasePlate", (58, 122, 218))
    p_cap = assign_part_metadata(cap_bp.part, "TopCap", (224, 72, 62))
    assy = Compound(label="TestFastAssy", children=[p_base, p_cap])

    step_file = tmp_path / "fast_assy.step"
    stl_file = tmp_path / "base_plate.stl"
    export_step(assy, str(step_file))
    export_stl(p_base, str(stl_file))

    t0 = time.perf_counter()
    rep = inspect_cad(step_file, check_interference=True, clearance_threshold=1.0, engine="ocp")
    dt = time.perf_counter() - t0

    assert dt < 5.0, f"OCP inspect_cad took unexpectedly long: {dt:.2f}s"
    assert rep["part_count"] == 2
    labels = {p["label"] for p in rep["parts"]}
    assert "BasePlate" in labels
    assert "TopCap" in labels
    assert rep["invalid_parts"] == []
    assert rep["interferences"] == []
    assert len(rep["clearance_warnings"]) == 1
    assert abs(rep["closest_pairs"][0]["min_distance_mm"] - 0.60) < 0.03

    # Verify interference detection when two solids overlap:
    # BasePlate has R=4 center hole, so Z in [0, 2] intersection is 2 * (10*10 - pi*4^2) = 99.47 mm^3
    with BuildPart() as overlap_bp:
        with Locations((0, 0, 1.0)):
            Box(10, 10, 2.0)
    p_ov = assign_part_metadata(overlap_bp.part, "CollidingBlock", (230, 126, 34))
    ov_step = tmp_path / "overlap_assy.step"
    export_step(Compound(label="OverlapAssy", children=[p_base, p_ov]), str(ov_step))

    ov_rep = inspect_cad(ov_step, check_interference=True, clearance_threshold=0.5, engine="ocp")
    assert len(ov_rep["interferences"]) == 1
    assert abs(ov_rep["interferences"][0]["overlap_mm3"] - 99.47) < 0.5

    # Verify STL watertight inspection in OCP/trimesh path
    stl_rep = inspect_cad(stl_file, engine="ocp")
    assert stl_rep["part_count"] == 1
    assert stl_rep["parts"][0]["is_watertight"] is True
    assert stl_rep["parts"][0]["has_non_manifolds"] is False

    # Verify OCP export_cad (.step -> .stl and .step)
    re_step = tmp_path / "re_exported.step"
    re_stl = tmp_path / "re_exported.stl"
    exp_info = export_cad(step_file, step_path=re_step, stl_path=re_stl, engine="ocp")
    assert re_step.exists() and re_step.stat().st_size > 500
    assert re_stl.exists() and re_stl.stat().st_size > 200
    assert exp_info["solid_count"] == 2

    # Verify vectorized Z-buffer render_cad with OCP tessellation
    ren_dir = tmp_path / "renders"
    views = render_cad(step_file, ren_dir, prefix="fast_test", width=480, height=480, engine="ocp")
    for vk in ("iso_top", "iso_bottom", "top", "front", "left", "section_iso", "overview_5view"):
        assert vk in views
        img_p = Path(views[vk])
        assert img_p.exists()
        with Image.open(img_p) as im:
            assert im.width >= 480 and im.height >= 480


# ---------------------------------------------------------------------------
# Pillar 4B: Headless OrcaSlicer CLI & Print DFM Audit Tests
# ---------------------------------------------------------------------------
def test_slicer_dfm_audit(tmp_path: Path):
    """Verify bed fit (256x256 vs Snapmaker U1 269x260), overhang detection, and OrcaSlicer CLI integration."""
    from build123d import Box, BuildPart, Locations, export_stl
    from tools.cad_slicer_audit import slice_audit_print

    # 1. Wide riser part (263 mm wide: exceeds 256 mm Bambu bed, fits 269 mm Snapmaker U1 bed)
    with BuildPart() as wide_part:
        with Locations((0, 0, 5.0)):
            Box(263.0, 80.0, 10.0)
    wide_stl = tmp_path / "wide_riser.stl"
    export_stl(wide_part.part, str(wide_stl))

    rep_256 = slice_audit_print(wide_stl, bed_profile="bambu_256", run_slicer_cli=False)
    assert rep_256["bed_fit"]["fits_bed"] is False

    rep_u1 = slice_audit_print(wide_stl, bed_profile="snapmaker_u1", run_slicer_cli=False)
    assert rep_u1["bed_fit"]["fits_bed"] is True
    assert rep_u1["mesh"]["is_watertight"] is True

    # 2. Compact printable bracket with real OrcaSlicer CLI execution (if installed)
    with BuildPart() as small_part:
        with Locations((0, 0, 4.0)):
            Box(30.0, 20.0, 8.0)
    small_stl = tmp_path / "small_block.stl"
    export_stl(small_part.part, str(small_stl))

    rep_cli = slice_audit_print(small_stl, bed_profile="bambu_256", run_slicer_cli=True)
    assert rep_cli["bed_fit"]["fits_bed"] is True
    assert rep_cli["estimated_mass_g"] > 0.5


# ---------------------------------------------------------------------------
# Pillar 1: MateGraphBuilder + Viewport Pick-to-Code & Bi-Directional Params
# ---------------------------------------------------------------------------
def test_mate_graph_builder_and_pick_to_code(tmp_path: Path):
    """Verify MateGraphBuilder solves 3D transforms from semantic anchors and auto-emits valid constraints.json."""
    from build123d import Box, BuildPart, Cylinder, Locations, Mode
    from tools.ag_cad_helpers import MateGraphBuilder, assign_part_metadata
    from tools.solidcraft_inspector import map_pick_to_python_source, sync_params_bidirectional

    # Base frame plate in its own canonical frame
    with BuildPart() as base_bp:
        Box(100, 60, 4)
        with Locations((35, 15, 0), (-25, 15, 0)):
            Cylinder(radius=4.0, height=4.0, mode=Mode.SUBTRACT)
    base_part = assign_part_metadata(base_bp.part, "ChassisSidePlate", (58, 122, 218))

    # Swing arm defined locally with pivot bore at (0, 0, 0) extending +50 mm along +X
    with BuildPart() as arm_bp:
        with Locations((25, 0, 0)):
            Box(60, 14, 4)
        Cylinder(radius=4.0, height=4.0, mode=Mode.SUBTRACT)
    arm_part = assign_part_metadata(arm_bp.part, "IntakeSwingArm", (46, 164, 92))

    # Coupled gear wheel defined locally at (0, 0, 0)
    with BuildPart() as gear_bp:
        Cylinder(radius=16.0, height=4.0)
        Cylinder(radius=4.0, height=4.0, mode=Mode.SUBTRACT)
    gear_part = assign_part_metadata(gear_bp.part, "IdlerGear", (238, 174, 24))

    mg = MateGraphBuilder(assembly_name="SprungIntakeAssy")
    mg.add_part(
        "ChassisSidePlate",
        base_part,
        anchors={
            "origin": {"origin_mm": [0.0, 0.0, 0.0], "axis": "Z"},
            "arm_pivot": {"origin_mm": [35.0, 15.0, 2.0], "axis": "Z"},
            "gear_stud": {"origin_mm": [-25.0, 15.0, 2.0], "axis": "Z"},
        },
        grounded=True,
    )
    mg.add_part(
        "IntakeSwingArm",
        arm_part,
        anchors={
            "pivot_seat": {"origin_mm": [0.0, 0.0, -2.0], "axis": "Z"},
            "roller_tip": {"origin_mm": [50.0, 0.0, 0.0], "axis": "Z"},
        },
    )
    mg.add_part(
        "IdlerGear",
        gear_part,
        anchors={"hub_bottom": {"origin_mm": [0.0, 0.0, -2.0], "axis": "Z"}},
    )

    mg.add_mate(
        name="Rev_Arm_Pivot",
        mate_type="REVOLUTE",
        part_a="ChassisSidePlate",
        anchor_a="arm_pivot",
        part_b="IntakeSwingArm",
        anchor_b="pivot_seat",
        limits_deg=(-10.0, 80.0),
    )
    mg.add_mate(
        name="Rev_Idler_Gear",
        mate_type="REVOLUTE",
        part_a="ChassisSidePlate",
        anchor_a="gear_stud",
        part_b="IdlerGear",
        anchor_b="hub_bottom",
    )
    mg.add_gear_relation("Gear_Arm_To_Idler", "Rev_Arm_Pivot", "Rev_Idler_Gear", ratio=2.0, reverse_direction=False)

    step_out = tmp_path / "mate_solved.step"
    constraints_out = tmp_path / "mate_solved_constraints.json"
    result = mg.export(step_path=step_out, constraints_path=constraints_out)

    assert step_out.exists()
    assert constraints_out.exists()

    # Verify IntakeSwingArm's roller_tip landed at world (35 + 50, 15, 2 + 2) = (85, 15, 4)
    tip_world = result["world_anchors"]["IntakeSwingArm"]["roller_tip"]["origin_mm"]
    assert abs(tip_world[0] - 85.0) < 1e-3
    assert abs(tip_world[1] - 15.0) < 1e-3
    assert abs(tip_world[2] - 4.0) < 1e-3

    cspec = json.loads(constraints_out.read_text(encoding="utf-8"))
    assert len(cspec["mates"]) >= 3  # Ground + 2 revolutes
    assert len(cspec["gear_relations"]) == 1

    # Verify Pick-to-Code source mapping & bi-directional params sync
    proj_dir = tmp_path / "mock_proj"
    src_dir = proj_dir / "src"
    src_dir.mkdir(parents=True)
    params_file = src_dir / "params.json"
    params_file.write_text(json.dumps({"arm_pivot_x_mm": 35.0, "plate_thickness_mm": 4.0}, indent=2), encoding="utf-8")
    py_file = src_dir / "build_intake.py"
    py_file.write_text(
        'from build123d import *\n'
        'ARM_PIVOT = (35.0, 15.0, 2.0)\n'
        'part = assign_part_metadata(p, "ChassisSidePlate", (58, 122, 218))\n',
        encoding="utf-8",
    )

    pick_match = map_pick_to_python_source(
        project_dir=proj_dir,
        occurrence_name="ChassisSidePlate:1",
        world_point_mm=[35.0, 15.0, 2.0],
    )
    assert pick_match["matched_files"]
    assert any("build_intake.py" in m["file"] for m in pick_match["matched_files"])
    assert "arm_pivot_x_mm" in pick_match["matching_parameters"]

    sync_res = sync_params_bidirectional(
        params_json_path=params_file,
        gui_parameters=[{"name": "arm_pivot_x_mm", "value": 38.5}, {"name": "new_gui_clearance_mm", "value": 1.2}],
        direction="gui_to_json",
    )
    updated_params = json.loads(params_file.read_text(encoding="utf-8"))
    assert updated_params["arm_pivot_x_mm"] == 38.5
    assert updated_params["new_gui_clearance_mm"] == 1.2
    assert sync_res["updated_count"] >= 1


# ---------------------------------------------------------------------------
# Pillar 3: Dual-Tier 3D B-Rep Solid FEA (Gmsh + scikit-fem & CalculiX ccx)
# ---------------------------------------------------------------------------
def test_dual_tier_3d_brep_fea_and_fdm_delamination(tmp_path: Path):
    """Verify 3D TET FEA cantilever accuracy, FDM print-axis delamination check, and CalculiX ccx execution."""
    from build123d import Box, BuildPart, Locations, export_step
    from tools.cad_fea import run_3d_fea

    # Cantilever beam: L = 80 mm (X in [0, 80]), width b = 16 mm (Y in [-8, 8]), height h = 6 mm (Z in [-3, 3])
    L, b, h = 80.0, 16.0, 6.0
    F_z = -40.0  # 40 N downward load at X = 80 mm
    with BuildPart() as beam_bp:
        with Locations((L / 2.0, 0.0, 0.0)):
            Box(L, b, h)
    beam_step = tmp_path / "cantilever_beam.step"
    export_step(beam_bp.part, str(beam_step))

    # Analytical Euler-Bernoulli deflection for PLA (E = 3100 MPa):
    # I = b * h^3 / 12 = 16 * 216 / 12 = 288 mm^4
    # delta = |F| * L^3 / (3 * E * I) = 40 * 512000 / (3 * 3100 * 288) = 7.646 mm
    # sigma_max = |F| * L * (h/2) / I = 40 * 80 * 3 / 288 = 33.33 MPa
    heatmap_png = tmp_path / "beam_stress.png"

    # Case A: Good print orientation (print_axis="Z" -> filament strands run along X bending stress!)
    res_z = run_3d_fea(
        step_path=beam_step,
        material="PLA",
        print_axis="Z",
        fixed_selector="xmin",
        load_selector="xmax",
        force_n=[0.0, 0.0, F_z],
        mesh_size_mm=3.2,
        backend="skfem",
        out_png=heatmap_png,
    )
    assert res_z["status"] == "OK"
    assert 5.5 <= res_z["max_displacement_mm"] <= 9.5, f"Unexpected deflection: {res_z['max_displacement_mm']}"
    assert res_z["peak_von_mises_mpa"] > 20.0
    assert heatmap_png.exists()

    # Case B: Bad print orientation (print_axis="X" -> bending tension sigma_xx pulls FDM Z-layers directly apart!)
    res_x = run_3d_fea(
        step_path=beam_step,
        material="PLA",
        print_axis="X",
        fixed_selector="xmin",
        load_selector="xmax",
        force_n=[0.0, 0.0, F_z],
        mesh_size_mm=3.2,
        backend="skfem",
    )
    assert res_x["fdm_delamination"]["layer_safety_factor"] < res_z["fdm_delamination"]["layer_safety_factor"]

    # Case C: Tier 2 CalculiX (ccx.exe) backend parity check (when CalculiX is installed)
    from tools.cad_fea import find_calculix_ccx

    if find_calculix_ccx():
        res_ccx = run_3d_fea(
            step_path=beam_step,
            material="6061-T6",
            fixed_selector="xmin",
            load_selector="xmax",
            force_n=[0.0, 0.0, -200.0],
            mesh_size_mm=3.5,
            backend="calculix",
        )
        assert res_ccx["status"] == "OK"
        assert Path(res_ccx["inp_path"]).exists()
        # Analytical 6061-T6 (E = 68900 MPa, F = 200 N): delta = 200 * 512000 / (3 * 68900 * 288) = 1.720 mm
        assert 1.2 <= res_ccx["max_displacement_mm"] <= 2.3, f"CalculiX deflection mismatch: {res_ccx['max_displacement_mm']}"


# ---------------------------------------------------------------------------
# Pillar 2: STEP + constraints.json -> MJCF Physics Sim (MuJoCo + Genesis)
# ---------------------------------------------------------------------------
def test_mujoco_and_genesis_mjcf_physics_compiler(tmp_path: Path):
    """Verify STEP + constraints.json compiles to MJCF and simulates sprung/coupled dynamics + game-piece contact."""
    from build123d import Box, BuildPart, Cylinder, Locations
    from tools.ag_cad_helpers import MateGraphBuilder, assign_part_metadata
    from tools.cad_physics_sim import compile_and_simulate_physics

    with BuildPart() as base_bp:
        Box(120, 80, 6)
    with BuildPart() as paddle_bp:
        with Locations((35, 0, 0)):
            Box(70, 30, 6)
    with BuildPart() as follower_bp:
        Cylinder(radius=12, height=6)

    base = assign_part_metadata(base_bp.part, "SimBase", (95, 110, 128))
    paddle = assign_part_metadata(paddle_bp.part, "SprungPaddle", (46, 164, 92))
    follower = assign_part_metadata(follower_bp.part, "CoupledDrum", (238, 174, 24))

    mg = MateGraphBuilder("IntakeDropSim")
    mg.add_part(
        "SimBase",
        base,
        anchors={
            "pivot": {"origin_mm": [-20.0, 0.0, 35.0], "axis": "Y"},
            "drum_axle": {"origin_mm": [-45.0, 0.0, 35.0], "axis": "Y"},
        },
        grounded=True,
    )
    mg.add_part("SprungPaddle", paddle, anchors={"hinge": {"origin_mm": [0.0, 0.0, 0.0], "axis": "Y"}})
    mg.add_part("CoupledDrum", follower, anchors={"center": {"origin_mm": [0.0, 0.0, 0.0], "axis": "Y"}})

    mg.add_mate("Rev_Paddle", "REVOLUTE", "SimBase", "pivot", "SprungPaddle", "hinge", limits_deg=(-45.0, 45.0))
    mg.add_mate("Rev_Drum", "REVOLUTE", "SimBase", "drum_axle", "CoupledDrum", "center")
    mg.add_gear_relation("Paddle_Drum_Gear", "Rev_Paddle", "Rev_Drum", ratio=2.0, reverse_direction=False)

    step_path = tmp_path / "intake_sim.step"
    constraints_path = tmp_path / "intake_sim_constraints.json"
    mg.export(step_path=step_path, constraints_path=constraints_path)

    sim_gif = tmp_path / "mujoco_sim.gif"
    mj_res = compile_and_simulate_physics(
        step_path=step_path,
        constraints_path=constraints_path,
        engine="mujoco",
        duration_s=0.5,
        spring_configs={"Rev_Paddle": {"stiffness_nm_per_rad": 1.5, "springref_deg": 0.0}},
        game_pieces=[{"name": "FTC_Artifact_Ball", "shape": "sphere", "radius_mm": 25.0, "mass_kg": 0.08, "pos_mm": [20.0, 0.0, 95.0]}],
        out_gif=sim_gif,
    )
    assert mj_res["status"] == "OK"
    assert Path(mj_res["mjcf_path"]).exists()
    assert mj_res["steps_executed"] >= 100
    assert mj_res["contact_events"] >= 1
    assert sim_gif.exists()

    # Also verify optional Genesis backend loads and steps the exact same compiled MJCF when installed
    import importlib.util

    if importlib.util.find_spec("genesis") is not None:
        gs_res = compile_and_simulate_physics(
            step_path=step_path,
            constraints_path=constraints_path,
            engine="genesis",
            duration_s=0.15,
            game_pieces=[{"name": "Soft_Ball", "shape": "sphere", "radius_mm": 25.0, "mass_kg": 0.08, "pos_mm": [20.0, 0.0, 85.0]}],
        )
        assert gs_res["status"] == "OK"
        assert gs_res["steps_executed"] >= 20


# ---------------------------------------------------------------------------
# MCP JSON-RPC Registration Test for New Additive Tools
# ---------------------------------------------------------------------------
def test_mcp_exposes_all_four_additive_tools():
    """Verify tools/cad_mcp_server.py exposes cad_slice_audit, cad_fea, cad_physics_sim, and solidcraft_live_pick_to_code."""
    from tools.cad_mcp_server import handle_request

    resp = handle_request({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
    assert resp is not None
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {
        "cad_slice_audit",
        "cad_fea",
        "cad_physics_sim",
        "solidcraft_live_pick_to_code",
        "cad_share_package",
    }.issubset(names)


# ---------------------------------------------------------------------------
# COTS Agent-First Spec & Category Disambiguation Tests
# ---------------------------------------------------------------------------
def test_cots_agent_spec_and_category_disambiguation():
    """Verify mechanical hubs/brackets do not misclassify as electronics, kinematics compute exact pitch diameter, and agent_index.jsonl exists."""
    from tools.cad_scraper import cots_info, normalize_category

    assert normalize_category("", "hyperhub8") == "motion"
    assert normalize_category("", "Locking Motion Hub - 2 Pack [REV-41-1719]") == "motion"
    assert normalize_category("", "1in Angled Corner Bracket [REV-21-3291]") == "structure"
    assert normalize_category("", "REV Control Hub") == "electronics"
    assert normalize_category("", "Blinkin LED Driver") == "electronics"

    gear_info = cots_info("2302-0014-0096")
    local_gear = gear_info.get("local")
    assert local_gear is not None
    spec = local_gear.get("agent_spec") or {}
    assert spec.get("bore_profile") == "14mm_round_bearing_seat"
    kin = spec.get("kinematics") or {}
    assert kin.get("teeth") == 96
    assert abs(float(kin.get("pitch_diameter_mm", 0.0)) - 76.8) < 0.01
    assert "mate_graph_snippet" in local_gear

    idx_jsonl = Path(__file__).resolve().parents[1] / "cots" / "cots_agent_index.jsonl"
    assert idx_jsonl.exists() and idx_jsonl.stat().st_size > 100_000


# ---------------------------------------------------------------------------
# Team Sharing & Zero-Install Interactive 3D WebGL Viewer Tests
# ---------------------------------------------------------------------------
def test_team_share_package_and_interactive_3d_viewer(tmp_path: Path):
    """Verify build_team_share_package generates .glb, standalone Three.js _team_viewer.html, README_TEAM.md, and _team_bundle.zip."""
    from build123d import Box, BuildPart, Compound, Cylinder, Locations, export_step
    from tools.ag_cad_helpers import assign_part_metadata
    from tools.cad_team_share import build_team_share_package

    with BuildPart() as p1:
        Box(80, 60, 8)
    with BuildPart() as p2:
        with Locations((0, 0, 14)):
            Cylinder(radius=15, height=20)

    part_a = assign_part_metadata(p1.part, "MountPlate", (66, 135, 245))
    part_b = assign_part_metadata(p2.part, "TurretBoss", (245, 158, 66))
    asm = Compound(label="ShareDemoAsm", children=[part_a, part_b])

    step_file = tmp_path / "share_demo_assembly.step"
    export_step(asm, str(step_file))

    res = build_team_share_package(step_file, out_dir=tmp_path / "share_out")
    assert res["status"] == "ok"
    assert res["part_count"] == 2
    assert Path(res["glb_path"]).exists() and Path(res["glb_path"]).stat().st_size > 200
    viewer_html = Path(res["viewer_html"])
    assert viewer_html.exists()
    html_txt = viewer_html.read_text(encoding="utf-8")
    assert "GLTFLoader" in html_txt
    assert "explode-slider" in html_txt
    assert "MountPlate" in html_txt and "TurretBoss" in html_txt
    assert res["bundle_zip"] and Path(res["bundle_zip"]).exists()
