# Agent-First COTS Hardware Guide (`cots/`)

This workspace contains **2588 verified COTS parts** pre-compiled into **SolidCraft Format 52 (`solidcraft/52` + `solidcore`)** under `cots/solidcraft/` with normalized `+Z` functional frames and pre-baked `joint.origin` mate connectors.

## Category Breakdown
- **`3200-2526-0003.step`**: 1 verified parts
- **`bearings`**: 130 verified parts
- **`biobuzz-2026`**: 1 verified parts
- **`electrical`**: 8 verified parts
- **`electronics`**: 49 verified parts
- **`fasteners`**: 261 verified parts
- **`kits`**: 1 verified parts
- **`linear-guides`**: 1 verified parts
- **`motion`**: 916 verified parts
- **`motors`**: 124 verified parts
- **`rx-m3sx4.0.step`**: 1 verified parts
- **`servos`**: 108 verified parts
- **`structure`**: 799 verified parts
- **`ttb-0329_frcdesignlib_v123.step`**: 1 verified parts
- **`wheels`**: 187 verified parts

## Fast Agent Lookup Order
1. **MCP Tool (`ares_onshape_local`)**: Call `cots_search(query=..., bore_mm=..., category=...)` and `cots_info(identifier=...)` — every result includes `agent_spec` (`bore_profile`, `mounting_pattern`, `axial_stackup_mm`, `kinematics.pitch_diameter_mm`, `mass_estimate_g`) plus ready-to-paste `solidcraft_snippet`, `build123d_snippet`, and `mate_graph_snippet`.
2. **Offline / Subagent Grep (`<50 ms`)**: Search [`cots_agent_index.jsonl`](cots_agent_index.jsonl) directly by SKU, tooth count (`"teeth":96`), bore profile (`8mm_REX_hex`, `1/2in_hex`), or bolt pattern (`goBILDA_16mm_M4_grid`).

## Normalized Frame & Pre-Baked `joint.origin` Convention
Every `cots/solidcraft/*.sc` part has its primary mating anchor at `[0, 0, 0]` with its bore/shaft axis aligned to `+Z` (`[0, 0, 1]`), and includes pre-baked `joint.origin` features:
- `bore_center`, `bore_min`, `bore_max` — axial stackup bounds (`axial_stackup_mm.z_min_mm` .. `z_max_mm`)
- `flange_seat` — bearing/collar flange seating plane
- `output_shaft` — motor/servo output face
- `mount_hole_1..8` — detected M3 / M4 / #10-32 bolt circle centers
