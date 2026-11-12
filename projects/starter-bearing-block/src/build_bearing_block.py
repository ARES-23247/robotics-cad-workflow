"""
Starter Parametric CAD Example: 8mm REX Twin-Bearing Pillow Block
=================================================================
Builds a 3D-printable twin-bearing block on the standard 16 mm M4 grid, seats two
verified goBILDA 8 mm REX flanged bearings (`1611-0514-4008`) and a rotating shaft,
and exports `.step`, `.stl`, BOM, and `*_constraints.json` for SolidCraft `.sc` compilation.
"""

from pathlib import Path
import sys

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from build123d import Axis, Box, BuildPart, Compound, CounterBoreHole, Cylinder, Locations, Mode, export_step, export_stl, fillet
from tools.ag_cad_helpers import (
    assign_part_metadata,
    export_assembly_constraints,
    export_cots_bom,
    gobilda_pattern_coords,
    hex_circumradius,
    load_cots_part,
)

PARAMS = {
    "block_length_mm": 48.0,
    "block_width_mm": 32.0,
    "block_thickness_mm": 14.0,
    "bearing_od_clearance_mm": 14.20,  # 14.0 mm OD + 0.20 mm printed fit clearance
    "m4_clearance_dia_mm": 4.20,
    "shaft_length_mm": 44.0,
}


def main() -> None:
    proj_dir = Path(__file__).resolve().parents[1]
    cad_dir = proj_dir / "cad"
    prints_dir = proj_dir / "fabrication" / "prints"
    docs_dir = proj_dir / "docs"
    for d in (cad_dir, prints_dir, docs_dir):
        d.mkdir(parents=True, exist_ok=True)

    L = PARAMS["block_length_mm"]
    W = PARAMS["block_width_mm"]
    H = PARAMS["block_thickness_mm"]
    r_bore = 0.5 * PARAMS["bearing_od_clearance_mm"]

    # 1. Custom 3D-printed housing block centered at origin (Z in [-H/2, +H/2])
    with BuildPart() as housing_bp:
        Box(L, W, H)
        fillet(housing_bp.edges().filter_by(Axis.Z), radius=3.0)
        Cylinder(radius=r_bore, height=H, mode=Mode.SUBTRACT)
        mount_pts = [(x, y, H / 2.0) for (x, y) in gobilda_pattern_coords(3, 2, pitch_mm=16.0) if abs(x) > 8.0]
        with Locations(*mount_pts):
            CounterBoreHole(
                radius=0.5 * PARAMS["m4_clearance_dia_mm"],
                counter_bore_radius=3.8,
                counter_bore_depth=2.5,
                depth=H,
            )

    housing = assign_part_metadata(housing_bp.part, "01_Printed_Pillow_Block", (58, 122, 218))

    # 2. Two COTS 8mm REX Flanged Bearings seated on top (+Z) and bottom (-Z) faces
    top_bearing = load_cots_part(
        "1611-0514-4008",
        label="02_COTS_Top_Bearing_8mmREX",
        bore_axis="Z",
        anchor="flange_seat",
        at=(0.0, 0.0, H / 2.0),
    )
    bot_bearing = load_cots_part(
        "1611-0514-4008",
        label="03_COTS_Bottom_Bearing_8mmREX",
        bore_axis="-Z",
        anchor="flange_seat",
        at=(0.0, 0.0, -H / 2.0),
    )

    # 3. Rotating shaft passing cleanly through the 7mm/8mm bearing bore
    with BuildPart() as shaft_bp:
        Cylinder(radius=3.45, height=PARAMS["shaft_length_mm"])
    shaft = assign_part_metadata(shaft_bp.part, "04_Output_Shaft", (238, 174, 24))

    parts = [housing, top_bearing, bot_bearing, shaft]
    assy = Compound(label="StarterBearingBlock", children=parts)

    step_path = cad_dir / "starter_bearing_block.step"
    stl_path = prints_dir / "bearing_pillow_block.stl"
    constraints_path = cad_dir / "starter_bearing_block_constraints.json"

    export_step(assy, str(step_path))
    export_stl(housing, str(stl_path))
    export_cots_bom(parts, docs_dir, "starter_bearing_block", assembly_title="Starter Twin-Bearing Pillow Block")
    export_assembly_constraints(
        parts,
        constraints_path,
        stationary_parts=[
            "01_Printed_Pillow_Block",
            "02_COTS_Top_Bearing_8mmREX",
            "03_COTS_Bottom_Bearing_8mmREX",
        ],
    )
    print(f"Built {step_path} and {stl_path}")


if __name__ == "__main__":
    main()
