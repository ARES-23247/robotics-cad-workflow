"""Automated test suite for Antigravity (AG) Unified CAD Pipeline, Helpers & MCP Server."""

from pathlib import Path
import sys
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ag_cad_helpers import (
     epitrochoid_points,
    gobilda_pattern_coords,
    hex_circumradius,
    rex_bore_polygon_points,
)
from tools.cad_mcp_server import handle_request
from tools.cad_pipeline import (
    BUILD123D_PYTHON,
    _resolve_occurrence_id,
    export_cad,
    inspect_cad,
    make_assembly_gear_payload,
    make_assembly_group_payload,
    make_assembly_mate_connector_payload,
    make_assembly_mate_payload,
    render_cad,
    run_build123d_script,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CYCLOIDAL_STEP = WORKSPACE_ROOT / "projects" / "cycloidal-actuator" / "cad" / "cycloidal_actuator_15to1.step"


def test_build123d_environment_ready():
    """Verify the local .venv has build123d and OCP installed."""
    assert Path(BUILD123D_PYTHON).exists(), f"Missing build123d Python at {BUILD123D_PYTHON}"


def test_ag_cad_helpers():
    """Verify pre-validated robotics geometry helper calculations."""
    # 12.7 mm (1/2" hex) + 0.15 mm clearance -> circumradius = 12.85 / sqrt(3)
    r_hex = hex_circumradius(12.7, 0.15)
    assert abs(r_hex - (12.85 / (3.0 ** 0.5))) < 1e-6

    # 8mm REX bore points
    rex_pts = rex_bore_polygon_points(8.0, 7.0, 0.20, arc_steps=6)
    assert len(rex_pts) > 24
    max_r = max((x * x + y * y) ** 0.5 for x, y in rex_pts)
    assert abs(max_r - 4.10) < 0.02

    # Epitrochoid lobe points
    lobe_pts = epitrochoid_points(R=36.0, Rr_eff=2.45, e=1.20, N=16, num_samples=120)
    assert len(lobe_pts) == 120

    # goBILDA 16mm grid pattern
    grid = gobilda_pattern_coords(2, 2, pitch_mm=16.0)
    assert sorted(grid) == [(-8.0, -8.0), (-8.0, 8.0), (8.0, -8.0), (8.0, 8.0)]


def test_build123d_xcaf_labels_nearest_pairs_and_stl_audit(tmp_path: Path):
    """Test XCAF STEP label preservation, closest_pairs table, and STL watertight check."""
    script_path = tmp_path / "sample_bracket.py"
    step_out = tmp_path / "sample_assembly.step"
    stl_out = tmp_path / "sample_bracket.stl"

    script_code = f"""
from build123d import *
from tools.ag_cad_helpers import assign_part_metadata

with BuildPart() as bracket:
    Box(40, 30, 4)
    Cylinder(radius=5, height=4, mode=Mode.SUBTRACT)
    with Locations((14, 9, 0), (-14, 9, 0), (14, -9, 0), (-14, -9, 0)):
        CounterBoreHole(radius=2.1, counter_bore_radius=4.0, counter_bore_depth=1.5)
    fillet(bracket.edges().filter_by(Axis.Z), radius=3.0)

with BuildPart() as sensor_cap:
    with Locations((0, 0, 3.6)):
        Box(20, 20, 2.0)

part_a = assign_part_metadata(bracket.part, "BaseBracket", (58, 122, 218))
part_b = assign_part_metadata(sensor_cap.part, "SensorCap", (224, 72, 62))

assy = Compound(label="BracketAssy", children=[part_a, part_b])
export_step(assy, r"{step_out}")
export_stl(part_a, r"{stl_out}")
"""
    script_path.write_text(script_code, encoding="utf-8")

    # 1. Run build123d script
    run_build123d_script(script_path)
    assert step_out.exists() and step_out.stat().st_size > 1000
    assert stl_out.exists() and stl_out.stat().st_size > 200

    # 2. Verify XCAF labels ("BaseBracket", "SensorCap") and closest_pairs table
    report = inspect_cad(step_out, check_interference=True, clearance_threshold=1.0)
    assert report["part_count"] == 2
    part_labels = {p["label"] for p in report["parts"]}
    assert "BaseBracket" in part_labels
    assert "SensorCap" in part_labels
    assert len(report["interferences"]) == 0
    assert len(report["clearance_warnings"]) == 1
    assert len(report["closest_pairs"]) == 1
    assert abs(report["closest_pairs"][0]["min_distance_mm"] - 0.6) < 0.02

    # 3. Verify STL watertight audit
    stl_report = inspect_cad(stl_out)
    assert stl_report["part_count"] == 1
    assert len(stl_report["invalid_parts"]) == 0
    assert stl_report["parts"][0]["is_watertight"] is True
    assert stl_report["parts"][0]["has_non_manifolds"] is False


@pytest.mark.skipif(not CYCLOIDAL_STEP.exists(), reason="Optional cycloidal-actuator project model not in standalone workspace")
def test_zbuffer_6view_and_section_render(tmp_path: Path):
    """Test Z-buffer 5-view + 3D section cut + contact sheet rendering on Cycloidal Actuator."""
    assert CYCLOIDAL_STEP.exists(), f"Expected test model at {CYCLOIDAL_STEP}"

    render_dir = tmp_path / "renders"
    rendered_files = render_cad(CYCLOIDAL_STEP, render_dir, prefix="test_cycloidal", width=600, height=600)

    expected_views = ["iso_top", "iso_bottom", "top", "front", "left", "section_iso", "overview_5view"]
    for view_name in expected_views:
        assert view_name in rendered_files
        img_path = Path(rendered_files[view_name])
        assert img_path.exists()
        with Image.open(img_path) as im:
            assert im.width >= 600 and im.height >= 600
            extrema = im.getextrema()
            assert any(lo < hi for lo, hi in extrema)


def test_mcp_server_jsonrpc_protocol():
    """Verify tools/cad_mcp_server.py handles initialize and tools/list requests properly."""
    init_resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "ag_cad_local"

    list_resp = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert list_resp is not None
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert {
        "cad_inspect",
        "cad_render",
        "cad_export",
        "cad_build123d",
        "cots_search",
        "cots_info",
        "onshape_push",
        "onshape_constrain",
        "onshape_export",
    }.issubset(tool_names)


def test_cots_catalog_and_helpers(tmp_path: Path):
    """Verify STEP header validation, COTS catalog search/info (FTC & FRC), and build123d load_cots_part."""
    from tools.ag_cad_helpers import load_cots_part, resolve_cots_step_path
    from tools.cad_scraper import cots_info, cots_search, validate_step_file

    # 1. Verify HTML masquerading as .step is rejected by validate_step_file
    fake_step = tmp_path / "fake_page.step"
    fake_step.write_text("<!DOCTYPE html><html><body>Not a STEP file</body></html>", encoding="utf-8")
    assert validate_step_file(fake_step) is False

    # 2. Verify valid local COTS STEP is accepted and fingerprinted
    step_path = resolve_cots_step_path("3417-4008-0024")
    assert step_path.exists()
    assert validate_step_file(step_path) is True

    # 3. Verify cots_search returns both local STEP parts, Onshape insertables, and FTC/FRC variants
    ftc_res = cots_search("3417", program="FTC", source="all", limit=10)
    assert ftc_res["local_count"] >= 1
    assert any("3417-4008-0024" in (p.get("sku") or "") for p in ftc_res["local_parts"])

    frc_res = cots_search("WCP-0750", program="FRC", source="all", limit=10)
    assert frc_res["onshape_count"] >= 1
    assert frc_res["variant_count"] >= 1
    assert any(v.get("sku") == "WCP-0750" and v.get("program") == "FRC" for v in frc_res["part_variants"])

    # 4. Verify cots_info returns B-Rep fingerprint (bbox, volume, extents)
    info = cots_info("3417-4008-0024")
    assert "local" in info
    assert info["local"]["is_valid_step"] == 1
    assert info["local"]["volume_cm3"] > 0
    assert len(info["local"]["extents_mm"]) == 3

    # 5. Verify load_cots_part auto-rotates bore_axis='Z' and aligns cylindrical bore axis
    shape = load_cots_part(
        "1611-0514-4008",
        label="GoBILDABearing8mmREX",
        rgb=(240, 180, 30),
        bore_axis="Z",
        align=("axis", "axis", "min"),
        at=(10.0, 20.0, 5.0),
    )
    assert shape.label == "GoBILDABearing8mmREX"
    assert shape.color is not None
    bb = shape.bounding_box()
    assert abs(bb.min.Z - 5.0) < 0.05
    assert abs(0.5 * (bb.min.X + bb.max.X) - 10.0) < 0.05
    assert abs(0.5 * (bb.min.Y + bb.max.Y) - 20.0) < 0.05


def test_onshape_assembly_constraint_payloads():
    """Verify Origin-anchored Mate Connector, Mate, Group, and Gear Relation builders and Cycloidal spec."""
    import json

    mock_instances = [
        {"id": "occ_stator", "name": "01_Stator_Housing <1>"},
        {"id": "occ_pins", "name": "02_Stationary_Ring_Pins <1>"},
        {"id": "occ_cam", "name": "03_Dual_Eccentric_Camshaft <1>"},
        {"id": "occ_disc_a", "name": "04_Cycloidal_Disc_A_0deg <1>"},
        {"id": "occ_disc_b", "name": "05_Cycloidal_Disc_B_180deg <1>"},
        {"id": "occ_turret", "name": "06_Output_Pin_Carrier_Turret <1>"},
    ]

    assert _resolve_occurrence_id(mock_instances, "WORLD") == []
    assert _resolve_occurrence_id(mock_instances, "01_Stator_Housing") == ["occ_stator"]
    assert _resolve_occurrence_id(mock_instances, "04_Cycloidal_Disc_A_0deg") == ["occ_disc_a"]

    mc_payload = make_assembly_mate_connector_payload(
        "MC | Eccentric Journal A",
        ["occ_cam"],
        tx_mm=1.2,
        ty_mm=0.0,
        tz_mm=8.6,
    )
    assert mc_payload["feature"]["btType"] == "BTMMateConnector-66"
    q = mc_payload["feature"]["parameters"][1]["queries"][0]
    assert q["path"] == ["occ_cam"]
    assert q["featureId"] == "Origin"
    assert q["queryData"] == "ORIGIN_Z"

    mate_payload = make_assembly_mate_payload("REVOLUTE | Input Cam", "REVOLUTE", "mc_1", "mc_2")
    assert mate_payload["feature"]["btType"] == "BTMMate-64"
    assert mate_payload["feature"]["parameters"][0]["value"] == "REVOLUTE"

    group_payload = make_assembly_group_payload("RIGID | Stator", ["occ_stator", "occ_pins"])
    assert group_payload["feature"]["btType"] == "BTMMateGroup-65"
    assert len(group_payload["feature"]["parameters"][0]["queries"]) == 2

    gear_payload = make_assembly_gear_payload("GEAR | 15:1", "m_in", "m_out", ratio_expr="15", reverse=True)
    assert gear_payload["feature"]["btType"] == "BTMMateRelation-1412"

    spec_path = WORKSPACE_ROOT / "projects" / "cycloidal-actuator" / "cad" / "cycloidal_actuator_constraints.json"
    if spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assert len(spec["mate_groups"]) == 1
        assert len(spec["mate_connectors"]) == 15
        assert len(spec["mates"]) == 5
        assert len(spec["gear_relations"]) == 3
        for mc in spec["mate_connectors"]:
            _resolve_occurrence_id(mock_instances, mc["part"])


def test_cots_catalog_and_datum_alignment(tmp_path):
    from build123d import Box
    from ag_cad_helpers import (
        assign_part_metadata,
        cut_cots_mounting_holes,
        export_assembly_constraints,
        export_cots_bom,
        load_cots_part,
    )
    from cad_scraper import cots_search

    res = cots_search("REV-21-1650", source="local", limit=5)
    local_parts = res["local_parts"]
    assert len(local_parts) >= 1
    neo = next(r for r in local_parts if r.get("sku") == "REV-21-1650")
    assert neo["primary_axis"] in ("X", "Y", "Z")

    brg = load_cots_part(
        "BRG-8MM-14MM-01",
        label="02_COTS_8mm_Bearing",
        at=(10.0, -15.0, 4.0),
        align=("axis", "axis", "center"),
        bore_axis="Z",
    )
    bb = brg.bounding_box()
    cx = 0.5 * (bb.min.X + bb.max.X)
    cy = 0.5 * (bb.min.Y + bb.max.Y)
    cz = 0.5 * (bb.min.Z + bb.max.Z)
    assert abs(cx - 10.0) < 0.25
    assert abs(cy - (-15.0)) < 0.25
    assert abs(cz - 4.0) < 0.25
    # Thickness along Z (5 mm) should be smaller than OD (16 mm)
    assert (bb.max.Z - bb.min.Z) < (bb.max.X - bb.min.X)
    assert getattr(brg, "_cots_meta", None) is not None
    assert brg._cots_meta["is_cots"] is True
    assert brg._cots_meta["world_bore_axis"] == "Z"

    raw_plate = assign_part_metadata(Box(80.0, 80.0, 5.0), "01_Custom_Plate", (60, 70, 80))
    vol_before = raw_plate.volume
    neo_part = load_cots_part(
        "REV-21-1650",
        label="04_COTS_NEO",
        at=(0.0, 0.0, -10.0),
        align=("axis", "axis", "max"),
        bore_axis="Z",
    )
    plate = cut_cots_mounting_holes(raw_plate, [neo_part, brg], min_central_bore_radius_mm=5.0)
    assert plate.volume < vol_before - 350.0
    assert plate.label == "01_Custom_Plate"

    wheel = load_cots_part(
        "REV-21-2024",
        label="03_COTS_Wheel",
        at=(10.0, -15.0, 20.0),
        align=("axis", "axis", "center"),
        bore_axis="Z",
    )
    parts = [plate, brg, wheel]
    bom = export_cots_bom(parts, tmp_path, "test_assy", assembly_title="Test Assembly")
    assert bom["cots_unique_count"] == 2
    assert bom["custom_count"] == 1
    assert (tmp_path / "test_assy_bom.md").exists()
    assert (tmp_path / "test_assy_bom.csv").exists()

    c_json = tmp_path / "test_assy_constraints.json"
    c_payload = export_assembly_constraints(parts, c_json)
    assert c_json.exists()
    assert len(c_payload["mates"]) >= 2
    assert any(m["type"] == "REVOLUTE" for m in c_payload["mates"])

    from ag_cad_helpers import compute_assembly_mass_properties, verify_kinematic_sweep
    from cad_pipeline import compile_onshape_browser_bundle, resolve_mate_connector_rotation

    assert neo_part._cots_meta.get("onshape_insertable") is not None
    assert neo_part._cots_meta["onshape_insertable"]["library_id"] == "frc-design-lib"

    assert resolve_mate_connector_rotation({"axis": "X"}) == ("ABOUT_Y", 90.0)
    assert resolve_mate_connector_rotation({"axis": "Y"}) == ("ABOUT_X", -90.0)
    assert resolve_mate_connector_rotation({"axis": "Z"}) == ("ABOUT_Z", 0.0)

    bundle_path = tmp_path / "test_assy_onshape_bundle.json"
    bundle = compile_onshape_browser_bundle(
        c_json,
        out_bundle_path=bundle_path,
        onshape_cloud_instances=c_payload.get("onshape_cloud_instances"),
    )
    assert bundle_path.exists()
    assert bundle["mode"] == "PURE_OFFLINE_ONSHAPE_SESSION_BUNDLE"
    assert bundle["total_steps"] >= 5

    mass_props = compute_assembly_mass_properties(parts)
    assert mass_props["total_mass_g"] > 20.0
    assert len(mass_props["cg_mm"]) == 3
    assert mass_props["j_drive_kg_mm2"] > 0.0

    sweep = verify_kinematic_sweep(parts, c_payload, angles_deg=(-20.0, 0.0, 20.0))
    assert sweep["status"] == "PASS"
    assert len(sweep["poses"]) == 3


def test_milestones_1_to_4_semantic_anchors_macros_and_physics_linter(tmp_path: Path):
    """
    Verify:
    - Milestone 1: Semantic COTS Functional Frames (`anchor="output_shaft"`, `"ear_mount"`, `"flange_seat"`)
    - Milestone 2: Parametric Mating & Axial Stackup Macros (`build_tangent_belt_or_chain`, `build_meshed_spur_gear_pair`, `build_double_shear_shaft_assembly`)
    - Milestone 3: Automated Mechanical Sanity & Physics Linter (`audit_mechanism_physics` catching all 5 error codes + passing all 5 benchmark assemblies)
    - Milestone 4: Pure-offline `build_onshape_browser_sync_js` generator & `cad_lint` MCP tool
    """
    from build123d import Box, Cylinder, Pos, Sphere
    from tools.ag_cad_helpers import (
         assign_part_metadata,
        audit_mechanism_physics,
        build_double_shear_shaft_assembly,
        build_meshed_spur_gear_pair,
        build_tangent_belt_or_chain,
        get_cots_functional_frames,
        load_cots_part,
    )
    from tools.cad_pipeline import build_onshape_browser_sync_js, lint_cad_assembly

    # 1. Milestone 1: Semantic `anchor="output_shaft"` on off-center Swyft Titan Servo
    servo = load_cots_part(
        "Servo_SR-Servo-01",
        label="Test_Titan_Servo",
        anchor="output_shaft",
        at=(64.0, 0.0, 0.0),
    )
    out_shaft = servo._cots_meta["world_output_shaft_mm"]
    assert abs(out_shaft[0] - 64.0) < 0.25
    assert abs(out_shaft[1] - 0.0) < 0.25
    assert abs(out_shaft[2] - 0.0) < 0.25

    # 2. Milestone 2: Parametric Belt, Meshed Spur Gears & Double-Shear Shaft Macros
    belt = build_tangent_belt_or_chain((0.0, 0.0), 15.0, (80.0, 0.0), 15.0, width_mm=6.0, plane="XY", offset_mm=13.5)
    assert belt.volume > 500.0

    gears = build_meshed_spur_gear_pair((0.0, 0.0, 10.0), teeth1=20, teeth2=40, module_mm=1.5, axis="Z")
    assert abs(gears["center_distance_mm"] - 45.0) < 1e-3
    assert gears["ratio"] == "40/20"
    assert gears["gear1"].volume > 100.0 and gears["gear2"].volume > 200.0

    shaft_assy = build_double_shear_shaft_assembly("Z", 0.0, 0.0, 0.0, 40.0, hex_af_mm=12.7, collar_sku=None)
    assert shaft_assy["shaft"].volume > 1000.0

    # 3. Milestone 3: Verify `assign_part_metadata` DisjointSolidError guard and `audit_mechanism_physics` catching all 6 error codes
    from build123d import BuildPart, Locations, Mode

    with BuildPart() as disjoint_bp:
        with Locations((-25.0, 0.0, 0.0), (25.0, 0.0, 0.0)):
            Cylinder(radius=4.0, height=10.0)
    with pytest.raises(ValueError, match="DisjointSolidError"):
        assign_part_metadata(disjoint_bp, "Bad_Disjoint_Shaft", (200, 200, 200))

    with BuildPart() as thin_wall_bp:
        Box(120.0, 80.0, 5.0)
        # Bearing bore R=14.0 mm placed at X=45.2 mm -> wall to X=60.0 edge is only 0.8 mm (< 2.0 mm minimum)
        with Locations((45.2, 0.0, 0.0)):
            Cylinder(radius=14.0, height=8.0, mode=Mode.SUBTRACT)
    base_plate = assign_part_metadata(thin_wall_bp.part, "01_Base_Plate", (50, 60, 70))
    floating_sensor = assign_part_metadata(Pos(0.0, 0.0, 35.0) * Box(20.0, 15.0, 6.0), "02_Floating_Sensor", (40, 180, 90))
    uncoupled_servo = load_cots_part("Servo_SR-Servo-01", label="03_Uncoupled_Servo", anchor="output_shaft", at=(-45.0, -25.0, 0.0))
    misaligned_pulley = assign_part_metadata(Pos(0.0, 0.0, 8.0) * Cylinder(radius=16.0, height=10.0), "04_Driven_Pulley", (220, 180, 40))
    oversized_gear = assign_part_metadata(Pos(40.0, 0.0, 8.0) * Cylinder(radius=54.0, height=8.0), "05_Oversized_Roller_Gear", (200, 160, 40))
    undersized_wheel = assign_part_metadata(Pos(40.0, 0.0, 20.0) * Cylinder(radius=44.0, height=12.0), "06_Compliant_Intake_Wheel", (40, 180, 100))
    far_wheel = assign_part_metadata(Pos(-65.0, 0.0, 55.0) * Cylinder(radius=12.0, height=10.0), "07_Upper_Flywheel_Wheel", (40, 180, 100))
    blocking_bar = assign_part_metadata(Pos(0.0, 0.0, 45.0) * Box(12.0, 80.0, 12.0), "08_Center_Crossbar_Standoff", (120, 120, 130))
    ref_ball = assign_part_metadata(Pos(0.0, 0.0, 45.0) * Sphere(radius=35.0), "09_Ref_GamePiece_Ball", (245, 130, 30))

    bad_audit = audit_mechanism_physics(
        [
            base_plate,
            floating_sensor,
            uncoupled_servo,
            misaligned_pulley,
            oversized_gear,
            undersized_wheel,
            far_wheel,
            blocking_bar,
            ref_ball,
        ]
    )
    assert bad_audit["status"] == "FAIL"
    err_codes = {e["code"] for e in bad_audit["errors"]}
    assert "FLOATING_PART_ERROR" in err_codes
    assert "UNCOUPLED_ACTUATOR_ERROR" in err_codes
    assert "GEAR_EXCEEDS_WHEEL_OD_ERROR" in err_codes
    assert "NON_CONTACTING_WHEEL_ERROR" in err_codes
    assert "BLOCKED_BALL_PATH_ERROR" in err_codes
    assert "THIN_BEARING_WALL_ERROR" in err_codes

    # 4. Verify any present benchmark assemblies PASS `lint_cad_assembly` with 0 errors
    benchmark_steps = [
        WORKSPACE_ROOT / "projects" / "frc-dual-flywheel-hood" / "cad" / "frc_dual_flywheel_hood.step",
        WORKSPACE_ROOT / "projects" / "ftc-cots-belt-drive" / "cad" / "ftc_cots_belt_drive.step",
        WORKSPACE_ROOT / "projects" / "frc-cots-intake-roller" / "cad" / "frc_cots_intake_roller.step",
        WORKSPACE_ROOT / "projects" / "ftc-deadwheel-odometry" / "cad" / "ftc_deadwheel_odometry.step",
        WORKSPACE_ROOT / "projects" / "ftc-coaxial-turret-wrist" / "cad" / "ftc_coaxial_turret_wrist.step",
    ]
    for sp in benchmark_steps:
        if sp.exists():
            rep = lint_cad_assembly(sp)
            assert rep["status"] == "PASS", f"{sp.name} failed physics lint: {rep['errors']}"
            assert rep["error_count"] == 0

    # 5. Milestone 4: Verify `build_onshape_browser_sync_js` offline script generation
    sample_step = benchmark_steps[1] if benchmark_steps[1].exists() else (tmp_path / "mock_sync.step")
    if not sample_step.exists():
        from build123d import export_step

        export_step(base_plate, str(sample_step))
    sample_bundle = WORKSPACE_ROOT / "projects" / "ftc-cots-belt-drive" / "cad" / "ftc_cots_belt_drive_onshape_bundle.json"
    if not sample_bundle.exists():
        sample_bundle = tmp_path / "mock_bundle.json"
        sample_bundle.write_text('{"steps": []}', encoding="utf-8")
    js_code = build_onshape_browser_sync_js(
        did="doc123",
        wid="w123",
        asm_eid="asm123",
        step_path=sample_step,
        bundle_path=sample_bundle,
    )
    assert "async () =>" in js_code
    assert "/api/v6/translations/d/" in js_code


def test_cots_database_completeness_and_semantic_search():
    """Verify 100% of VERIFIED_STEP parts have primary_axis + functional_frames_json, rich titles, and semantic search/info."""
    import sqlite3
    from tools.cad_scraper import DB_PATH, cots_info, cots_search

    conn = sqlite3.connect(DB_PATH)
    total_verified = conn.execute("SELECT COUNT(*) FROM local_cots_parts WHERE status='VERIFIED_STEP'").fetchone()[0]
    null_axes = conn.execute("SELECT COUNT(*) FROM local_cots_parts WHERE status='VERIFIED_STEP' AND primary_axis IS NULL").fetchone()[0]
    null_frames = conn.execute("SELECT COUNT(*) FROM local_cots_parts WHERE status='VERIFIED_STEP' AND functional_frames_json IS NULL").fetchone()[0]
    with_urls = conn.execute("SELECT COUNT(*) FROM local_cots_parts WHERE status='VERIFIED_STEP' AND product_url IS NOT NULL").fetchone()[0]
    no_cad_rows = conn.execute("SELECT COUNT(*) FROM local_cots_parts WHERE status='NO_CAD'").fetchone()[0]
    empty_cfg_os = conn.execute("SELECT COUNT(*) FROM onshape_insertables WHERE is_configurable=1 AND (parameters_json IS NULL OR parameters_json='[]')").fetchone()[0]
    total_variants = conn.execute("SELECT COUNT(*) FROM cots_part_variants").fetchone()[0]
    conn.close()

    assert total_verified >= 2573
    assert null_axes == 0
    assert null_frames == 0
    assert with_urls >= 2190
    assert no_cad_rows == 0
    assert empty_cfg_os == 0
    assert total_variants >= 17800

    # Synonym expansion: "1/2 hex bearing" matches REV-21-1915 / REV-21-1916
    hex_res = cots_search("1/2 hex bearing", limit=5)
    hex_skus = {r.get("sku") for r in hex_res["local_parts"]}
    assert "REV-21-1915" in hex_skus or "REV-21-1916" in hex_skus

    # Natural keyword search: "NEO" ranks REV-21-1650 in top results
    neo_res = cots_search("NEO", limit=5)
    neo_skus = {r.get("sku") for r in neo_res["local_parts"]}
    assert "REV-21-1650" in neo_skus

    # Parametric bore filter: bore_mm=8.0 returns r=4.0 mm bearings
    brg_res = cots_search("bearing", bore_mm=8.0, limit=5)
    assert len(brg_res["local_parts"]) >= 2
    assert all(abs(float(r["min_bore_radius_mm"]) - 4.0) < 0.65 for r in brg_res["local_parts"])

    # Semantic anchor recommendations in cots_info
    neo_info = cots_info("REV-21-1650")["local"]
    assert neo_info["recommended_anchor"] == "output_shaft"
    assert "anchor='output_shaft'" in neo_info["build123d_snippet"]

    brg_info = cots_info("REV-21-1915")["local"]
    assert brg_info["recommended_anchor"] == "flange_seat"
    assert "anchor='flange_seat'" in brg_info["build123d_snippet"]



