"""
Antigravity (AG) Robotics CAD Helper Library for build123d
==========================================================
Provides pre-verified geometric generators and robotics COTS profiles
(goBILDA 8mm REX bores, FRC 1/2" & 3/8" hex bores, epitrochoid cycloidal
lobes, tangent timing-belt pulleys, and STEP AP214 color/label metadata)
to prevent 1st-pass LLM trigonometry or context-manager errors.
"""

import io
import json
import math
from pathlib import Path
from build123d import Color, Locations, Part


def assign_part_metadata(
    part,
    label: str,
    rgb: tuple[int, int, int] | tuple[float, float, float],
    allow_multi_solid: bool = False,
) -> Part:
    """
    Assigns both STEP AP214 product label and RGB color to a build123d Part or BuildPart builder,
    baking transforms into a single world-coordinate Solid.
    Guards against `BuildPart.part` silent disjoint-solid truncation: if a `BuildPart` context or
    multi-solid shape contains >1 disconnected solid and `allow_multi_solid=False`, raises a
    ValueError with the exact count and bounding-box centers of the disjoint islands.
    """
    raw_obj = getattr(part, "_obj", part)
    sols = list(raw_obj.solids()) if hasattr(raw_obj, "solids") else []
    if len(sols) > 1 and not allow_multi_solid:
        island_centers = []
        for s in sols[:5]:
            bb = s.bounding_box()
            island_centers.append(
                (
                    round(0.5 * (bb.min.X + bb.max.X), 2),
                    round(0.5 * (bb.min.Y + bb.max.Y), 2),
                    round(0.5 * (bb.min.Z + bb.max.Z), 2),
                )
            )
        raise ValueError(
            f"DisjointSolidError in custom part '{label}': found {len(sols)} disconnected solids "
            f"at centers {island_centers}. Connect the segments into a single manifold solid "
            f"(e.g., run the shaft core continuously through bores) or pass allow_multi_solid=True."
        )
    if hasattr(part, "_obj") and hasattr(part, "part"):
        from build123d import Compound

        part = Compound(children=sols) if (len(sols) > 1 and allow_multi_solid) else part.part
    part = bake_shape_to_world_solid(part)
    part.label = label
    if any(c > 1.0 for c in rgb):
        r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    else:
        r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    part.color = Color(r, g, b)
    return part


def hex_circumradius(across_flats_mm: float, diametral_clearance_mm: float = 0.15) -> float:
    """
    Converts hexagon Across-Flats (AF) dimension (e.g. 12.7 mm for FRC 1/2" hex,
    8.0 mm for 8mm hex, 5.0 mm for M3 standoff) into the circumradius required by
    build123d `RegularPolygon(radius=..., side_count=6, major_radius=True)`.
    """
    eff_af = across_flats_mm + diametral_clearance_mm
    return eff_af / math.sqrt(3.0)


def rex_bore_polygon_points(
    circle_diameter_mm: float = 8.0,
    flats_af_mm: float = 7.0,
    diametral_clearance_mm: float = 0.20,
    arc_steps: int = 8,
) -> list[tuple[float, float]]:
    """
    Computes a closed 2D polygon approximation of a goBILDA 8mm REX bore
    (the intersection of an 8.0 mm circle and a 7.0 mm across-flats hexagon,
    offset outward by diametral_clearance_mm / 2).
    """
    r = (circle_diameter_mm + diametral_clearance_mm) / 2.0
    a = (flats_af_mm + diametral_clearance_mm) / 2.0
    if a >= r:
        # Degenerates to a circle
        return [
            (r * math.cos(2 * math.pi * i / (6 * arc_steps)), r * math.sin(2 * math.pi * i / (6 * arc_steps)))
            for i in range(6 * arc_steps)
        ]
    beta = math.acos(a / r)
    pts: list[tuple[float, float]] = []
    for k in range(6):
        center_angle = k * (math.pi / 3.0)
        t_start = center_angle - beta
        t_end = center_angle + beta
        # Flat segment endpoints
        pts.append((r * math.cos(t_start), r * math.sin(t_start)))
        pts.append((r * math.cos(t_end), r * math.sin(t_end)))
        # Circular arc between this flat and the next flat
        next_t_start = (k + 1) * (math.pi / 3.0) - beta
        for s in range(1, arc_steps):
            u = s / arc_steps
            ang = t_end + u * (next_t_start - t_end)
            pts.append((r * math.cos(ang), r * math.sin(ang)))
    return pts


def epitrochoid_points(
    R: float,
    Rr_eff: float,
    e: float,
    N: int,
    num_samples: int = 240,
    rot_rad: float = 0.0,
) -> list[tuple[float, float]]:
    """
    Computes exact 2D epitrochoid (equidistant cycloid) coordinates for a cycloidal
    disc with N stationary ring pins (N-1 lobes) and eccentric offset e, rotated in
    the assembly plane by rot_rad (use 0.0 and math.pi for a balanced twin-disc pair).
    """
    if e <= 0 or e * N >= R:
        raise ValueError(f"Invalid eccentricity e={e} for R={R}, N={N} (must satisfy 0 < e*N < R)")
    cos_r = math.cos(rot_rad)
    sin_r = math.sin(rot_rad)
    pts: list[tuple[float, float]] = []
    for i in range(num_samples):
        t = 2.0 * math.pi * i / num_samples
        psi = math.atan2(math.sin((1 - N) * t), (R / (e * N)) - math.cos((1 - N) * t))
        lx = R * math.cos(t) - Rr_eff * math.cos(t + psi) - e * math.cos(N * t) + e
        ly = -R * math.sin(t) + Rr_eff * math.sin(t + psi) + e * math.sin(N * t)
        wx = lx * cos_r - ly * sin_r
        wy = lx * sin_r + ly * cos_r
        pts.append((wx, wy))
    return pts


def gobilda_pattern_coords(
    cols: int,
    rows: int,
    pitch_mm: float = 16.0,
    center: tuple[float, float] = (0.0, 0.0),
) -> list[tuple[float, float]]:
    """Returns centered 2D grid coordinates for standard goBILDA 16mm / 8mm hole patterns."""
    cx, cy = center
    x0 = cx - 0.5 * (cols - 1) * pitch_mm
    y0 = cy - 0.5 * (rows - 1) * pitch_mm
    return [
        (round(x0 + c * pitch_mm, 4), round(y0 + r * pitch_mm, 4))
        for c in range(cols)
        for r in range(rows)
    ]


def resolve_cots_step_path(
    sku_or_query: str,
    vendor: str | None = None,
    auto_download: bool = True,
) -> "Path":
    """
    Resolves a SKU, part_key, or keyword query to a verified local STEP file path
    in `cots/` using the SQLite catalog (`cots/catalog.db`). If the part is indexed
    as `DISCOVERED` with a `cad_url` and `auto_download=True`, downloads, validates
    (`ISO-10303-21`), and B-Rep fingerprints it on demand.
    """
    from pathlib import Path
    from tools.cad_scraper import (
        COTS_DIR,
        DB_PATH,
        VENDORS_DIR,
        WORKSPACE_ROOT,
        PoliteHttpClient,
        cots_info,
        cots_search,
        fingerprint_step_file,
        get_db,
        sanitize_slug,
        save_step_or_zip_bytes,
        upsert_local_part,
    )

    info = cots_info(sku_or_query)
    if "local" in info and info["local"].get("local_path"):
        cand = WORKSPACE_ROOT / info["local"]["local_path"]
        if cand.exists():
            return cand

    search_res = cots_search(sku_or_query, vendor=vendor, source="local", limit=10)
    for row in search_res.get("local_parts", []):
        if row.get("local_path"):
            cand = WORKSPACE_ROOT / row["local_path"]
            if cand.exists():
                return cand

    # On-demand download if indexed as DISCOVERED in local_cots_parts
    if auto_download and "local" in info and info["local"].get("cad_url"):
        loc = info["local"]
        cad_url = loc["cad_url"]
        v_name = loc.get("vendor", "unknown")
        cat = loc.get("category", "structure")
        sku = loc.get("sku")
        dest_dir = VENDORS_DIR / v_name / cat
        base_stem = sanitize_slug(sku or loc.get("title") or sku_or_query)
        http = PoliteHttpClient(min_interval_sec=0.1)
        resp = http.get(cad_url, timeout=25, max_retries=2)
        saved = save_step_or_zip_bytes(resp.content, cad_url, dest_dir, base_stem)
        if saved:
            primary = saved[0]
            rel_path = primary.relative_to(COTS_DIR).as_posix()
            fp = fingerprint_step_file(primary)
            conn = get_db(DB_PATH)
            upd = {
                "part_id": loc["part_id"],
                "vendor": v_name,
                "sku": sku,
                "title": loc.get("title", base_stem),
                "category": cat,
                "local_path": rel_path,
                "status": "VERIFIED_STEP",
                "file_size_bytes": primary.stat().st_size,
            }
            if fp:
                upd.update(fp)
            upsert_local_part(conn, upd)
            conn.commit()
            conn.close()
            return primary

    q_lower = sku_or_query.lower()
    for ext in ("*.step", "*.stp", "*.STEP", "*.STP"):
        for f in COTS_DIR.rglob(ext):
            if q_lower in f.name.lower():
                return f

    raise FileNotFoundError(
        f"Could not resolve local COTS STEP file for '{sku_or_query}' (vendor={vendor}). "
        f"Run `python tools/cad_scraper.py search \"{sku_or_query}\"` or download it first."
    )


def detect_shape_primary_axis(shape) -> dict | None:
    """
    Detects the dominant coaxial cylindrical bore/shaft axis ('X', 'Y', or 'Z'),
    its 3D centerline point `axis_center_mm`, and minimum cylindrical radius
    `min_bore_radius_mm` from a `build123d` Shape (cached per shape instance).
    """
    cached = getattr(shape, "_cached_primary_axis", None)
    if cached is not None:
        return cached
    from tools.cad_scraper import extract_shape_primary_axis

    wrapped = getattr(shape, "wrapped", shape)
    res = extract_shape_primary_axis(wrapped)
    try:
        shape._cached_primary_axis = res
    except Exception:
        pass
    return res


def align_and_place_shape(
    shape,
    rx_deg: float = 0.0,
    ry_deg: float = 0.0,
    rz_deg: float = 0.0,
    bore_axis: str | None = None,
    align: tuple[str | None, str | None, str | None] | None = ("center", "center", "center"),
    at: tuple[float, float, float] = (0.0, 0.0, 0.0),
):
    """
    Optionally rotates a `build123d` Shape/Compound so its primary cylindrical bore/shaft
    axis aligns with `bore_axis` ('X', 'Y', or 'Z'), applies `(rx_deg, ry_deg, rz_deg)`,
    computes its exact B-Rep bounding box (and cylindrical axis center when `'axis'` is
    requested), aligns each axis (`'min'`, `'center'`, `'max'`, `'axis'`, or `'origin'`),
    and translates the aligned anchor to `at = (tx, ty, tz)`.
    """
    from build123d import Pos, Rot

    orig_label = getattr(shape, "label", None)
    orig_color = getattr(shape, "color", None)
    orig_meta = getattr(shape, "_cots_meta", None)
    orig_ax = getattr(shape, "_cached_primary_axis", None)
    orig_frames = getattr(shape, "_cached_functional_frames", None)
    orig_mat4 = getattr(shape, "_accum_mat4", None)
    was_rotated = False

    if bore_axis is not None:
        target_ax = bore_axis.strip().upper()
        ax_info = detect_shape_primary_axis(shape)
        if ax_info and ax_info.get("primary_axis") in ("X", "Y", "Z"):
            src_ax = ax_info["primary_axis"]
            if src_ax != target_ax:
                was_rotated = True
                if src_ax == "Y" and target_ax == "Z":
                    shape = Rot(-90.0, 0.0, 0.0) * shape
                elif src_ax == "X" and target_ax == "Z":
                    shape = Rot(0.0, 90.0, 0.0) * shape
                elif src_ax == "Z" and target_ax == "Y":
                    shape = Rot(90.0, 0.0, 0.0) * shape
                elif src_ax == "X" and target_ax == "Y":
                    shape = Rot(0.0, 0.0, 90.0) * shape
                elif src_ax == "Z" and target_ax == "X":
                    shape = Rot(0.0, -90.0, 0.0) * shape
                elif src_ax == "Y" and target_ax == "X":
                    shape = Rot(0.0, 0.0, -90.0) * shape

    if rx_deg != 0.0 or ry_deg != 0.0 or rz_deg != 0.0:
        was_rotated = True
        shape = Rot(rx_deg, ry_deg, rz_deg) * shape

    dx, dy, dz = float(at[0]), float(at[1]), float(at[2])
    if align is not None:
        bb = shape.bounding_box()
        bmin = (bb.min.X, bb.min.Y, bb.min.Z)
        bmax = (bb.max.X, bb.max.Y, bb.max.Z)
        ax_center = None
        if any((m or "").lower() in ("axis", "bore", "shaft") for m in align):
            ax_info = detect_shape_primary_axis(shape) if was_rotated else (orig_ax or detect_shape_primary_axis(shape))
            if ax_info and ax_info.get("axis_center_mm"):
                ax_center = ax_info["axis_center_mm"]
        offsets = [0.0, 0.0, 0.0]
        for idx, mode in enumerate(align):
            m = (mode or "origin").lower()
            if m in ("axis", "bore", "shaft"):
                if ax_center is not None:
                    offsets[idx] = -float(ax_center[idx])
                else:
                    offsets[idx] = -0.5 * (bmin[idx] + bmax[idx])
            elif m in ("center", "mid", "middle"):
                offsets[idx] = -0.5 * (bmin[idx] + bmax[idx])
            elif m in ("min", "bottom", "left", "front"):
                offsets[idx] = -bmin[idx]
            elif m in ("max", "top", "right", "back"):
                offsets[idx] = -bmax[idx]
        dx += offsets[0]
        dy += offsets[1]
        dz += offsets[2]

    was_translated = dx != 0.0 or dy != 0.0 or dz != 0.0
    if was_translated:
        shape = Pos(dx, dy, dz) * shape

    if was_rotated or was_translated:
        shape = bake_shape_to_world_solid(shape, prev_mat4=orig_mat4)
    elif orig_mat4 is not None:
        try:
            shape._accum_mat4 = orig_mat4
        except Exception:
            pass

    if not was_rotated and not was_translated:
        try:
            if orig_ax is not None:
                shape._cached_primary_axis = orig_ax
            if orig_frames is not None:
                shape._cached_functional_frames = orig_frames
        except Exception:
            pass
    elif not was_rotated and was_translated and orig_frames is not None:
        try:
            translated_frames = {}
            for fk, fv in orig_frames.items():
                if not isinstance(fv, dict):
                    translated_frames[fk] = fv
                    continue
                nfv = dict(fv)
                if "origin_mm" in nfv:
                    nfv["origin_mm"] = [
                        round(float(nfv["origin_mm"][0]) + dx, 3),
                        round(float(nfv["origin_mm"][1]) + dy, 3),
                        round(float(nfv["origin_mm"][2]) + dz, 3),
                    ]
                if fk == "mounting_holes" and nfv.get("centers_2d_mm"):
                    mh_ax = (nfv.get("axis") or "Z").upper()
                    du, dv = (dx, dy) if mh_ax == "Z" else ((dx, dz) if mh_ax == "Y" else (dy, dz))
                    nfv["centers_2d_mm"] = [
                        [round(float(p[0]) + du, 3), round(float(p[1]) + dv, 3)]
                        for p in nfv["centers_2d_mm"]
                    ]
                translated_frames[fk] = nfv
            shape._cached_functional_frames = translated_frames
        except Exception:
            pass

    if orig_label:
        shape.label = orig_label
    if orig_color is not None:
        shape.color = orig_color
    if orig_meta is not None:
        shape._cots_meta = dict(orig_meta)
    return shape


def bake_shape_to_world_solid(shape, prev_mat4=None):
    """
    Converts a placed `build123d` `Shape`/`Compound` into a single `Solid` with
    `Location().IsIdentity() == True` by baking its `TopLoc_Location` directly into
    the B-Rep vertex/surface geometry via OpenCASCADE `BRepBuilderAPI_Transform`
    (and packing multi-solid COTS parts into a multi-shell `TopoDS_Solid` via
    `BRep_Builder` without slow 3D boolean `fuse()` or dropping disjoint sub-solids).
    Guarantees that `export_step()` writes a single `Part` whose `Origin` is
    identical to World `(0, 0, 0)` in <1 ms instead of 5-25 seconds per COTS part.
    """
    from build123d import Compound, Solid
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS_Solid

    orig_label = getattr(shape, "label", None)
    orig_color = getattr(shape, "color", None)
    orig_meta = getattr(shape, "_cots_meta", None)
    orig_frames = getattr(shape, "_cached_functional_frames", None)
    if prev_mat4 is None:
        prev_mat4 = getattr(shape, "_accum_mat4", None)

    sols = list(shape.solids()) if hasattr(shape, "solids") else []
    curr_mat4 = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    trsf_obj = None
    if sols and not sols[0].wrapped.Location().IsIdentity():
        trsf_obj = sols[0].wrapped.Location().Transformation()
    elif hasattr(shape, "wrapped") and not shape.wrapped.Location().IsIdentity():
        trsf_obj = shape.wrapped.Location().Transformation()
    if trsf_obj is not None:
        for r in range(3):
            for c in range(4):
                curr_mat4[r][c] = float(trsf_obj.Value(r + 1, c + 1))
    if prev_mat4 is not None:
        accum_mat4 = [
            [sum(curr_mat4[r][k] * prev_mat4[k][c] for k in range(4)) for c in range(4)]
            for r in range(4)
        ]
    else:
        accum_mat4 = curr_mat4
    if sols:
        try:
            if len(sols) == 1:
                s0 = sols[0]
                w0 = s0.wrapped
                if not w0.Location().IsIdentity():
                    trsf = w0.Location().Transformation()
                    baked_sh = BRepBuilderAPI_Transform(w0.Located(TopLoc_Location()), trsf, True).Shape()
                    shape = Solid(baked_sh)
                else:
                    shape = s0 if isinstance(s0, Solid) else Solid(w0)
            else:
                builder = BRep_Builder()
                out_sol = TopoDS_Solid()
                builder.MakeSolid(out_sol)
                shell_count = 0
                for s in sols:
                    ws = s.wrapped
                    if not ws.Location().IsIdentity():
                        trsf = ws.Location().Transformation()
                        baked_s = Solid(BRepBuilderAPI_Transform(ws.Located(TopLoc_Location()), trsf, True).Shape())
                    else:
                        baked_s = s if isinstance(s, Solid) else Solid(ws)
                    for sh in baked_s.shells():
                        builder.Add(out_sol, sh.wrapped)
                        shell_count += 1
                if shell_count > 0:
                    shape = Solid(out_sol)
        except Exception:
            # Fallback if a degenerate STEP shell cannot be added directly
            try:
                fused = sols[0].fuse(sols[0]) if len(sols) == 1 else sols[0].fuse(*sols[1:])
                if isinstance(fused, Compound) and fused.solids():
                    fused = max(fused.solids(), key=lambda x: x.volume)
                if isinstance(fused, Solid):
                    shape = fused
            except Exception:
                pass

    if orig_label:
        shape.label = orig_label
    if orig_color is not None:
        shape.color = orig_color
    if orig_meta is not None:
        shape._cots_meta = dict(orig_meta)
    if orig_frames is not None:
        try:
            shape._cached_functional_frames = orig_frames
        except Exception:
            pass
    try:
        shape._accum_mat4 = accum_mat4
    except Exception:
        pass
    return shape


def _compute_solidcraft_world_matrix_4x4(accum_mat4, solidcraft_matrix_json) -> list[float] | None:
    """
    Computes the 16-element row-major 4x4 world placement matrix `T_world_from_sc`
    for a normalized `cots/solidcraft/*.sc` component:
        T_world_from_sc = T_world_from_step @ inv(M_sc_from_step)
    """
    if accum_mat4 is None and not solidcraft_matrix_json:
        return None
    t_ws = (
        [list(row) for row in accum_mat4]
        if accum_mat4 is not None
        else [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )

    m_inv = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    if solidcraft_matrix_json:
        raw_m = (
            json.loads(solidcraft_matrix_json)
            if isinstance(solidcraft_matrix_json, str)
            else list(solidcraft_matrix_json)
        )
        if len(raw_m) >= 12:
            r00, r01, r02, tx = float(raw_m[0]), float(raw_m[1]), float(raw_m[2]), float(raw_m[3])
            r10, r11, r12, ty = float(raw_m[4]), float(raw_m[5]), float(raw_m[6]), float(raw_m[7])
            r20, r21, r22, tz = float(raw_m[8]), float(raw_m[9]), float(raw_m[10]), float(raw_m[11])
            m_inv[0] = [r00, r10, r20, -(r00 * tx + r10 * ty + r20 * tz)]
            m_inv[1] = [r01, r11, r21, -(r01 * tx + r11 * ty + r21 * tz)]
            m_inv[2] = [r02, r12, r22, -(r02 * tx + r12 * ty + r22 * tz)]

    out_16: list[float] = []
    for r in range(4):
        for c in range(4):
            val = sum(t_ws[r][k] * m_inv[k][c] for k in range(4))
            out_16.append(round(0.0 if abs(val) < 1e-9 else val, 6))
    return out_16


def attach_native_solidcraft_part(
    part,
    sc_path: str | Path,
    script_json_path: str | Path | None = None,
    params_json_path: str | Path | None = None,
    world_matrix_4x4: list[float] | None = None,
):
    """
    Tags a custom `build123d` part with its authoritative Native SolidCraft
    feature-tree `.sc` file (`projects/<project>/cad/solidcraft/<name>.sc`) and
    exported timeline/parameter JSON sidecars so `solidcraft-compile` links the
    parametric `.sc` component directly instead of importing a dead STEP solid.
    """
    sc_p = Path(sc_path)
    part._native_sc_meta = {
        "part_label": getattr(part, "label", None) or sc_p.stem,
        "solidcraft_path": str(sc_p.as_posix()),
        "script_json_path": str(Path(script_json_path).as_posix()) if script_json_path else None,
        "params_json_path": str(Path(params_json_path).as_posix()) if params_json_path else None,
        "world_matrix_4x4": list(world_matrix_4x4) if world_matrix_4x4 else [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ],
    }
    return part


def _resolve_onshape_insertable(sku: str, title: str, vendor: str, category: str) -> dict | None:
    """
    Cross-links a local COTS part (`sku`, `title`, `vendor`, `category`) against the
    indexed `onshape_insertables` table (`ftc-design-lib`, `frc-design-lib`, `mkcad`)
    in `cots/catalog.db` so assemblies can reference native Onshape cloud instances.
    """
    from tools.cad_scraper import DB_PATH, get_db

    if not DB_PATH.exists():
        return None

    sku_u = (sku or "").strip().upper()
    title_l = (title or "").strip().lower()
    cat_l = (category or "").strip().lower()

    # Deterministic family mapping for parametric FRCDesignApp libraries
    target_names: list[str] = []
    if sku_u.startswith("1120-") or sku_u.startswith("1121-"):
        target_names.append("S - Channel (GB)")
    elif sku_u.startswith("1106-"):
        target_names.append("S - Flat Beam (GB)")
    elif sku_u.startswith("1116-"):
        target_names.append("S - Grid Plates (GB)")
    elif sku_u.startswith("1201-0043") or sku_u.startswith("1222-"):
        target_names.append("S - Quad Block Mount (GB)")
    elif sku_u.startswith("1611-") or "flanged bearing" in title_l or "ball bearing" in title_l:
        target_names.extend(["S - Radial Bearing (GB)", "Custom Bearing"])
    elif sku_u.startswith("2000-") or "servo" in title_l:
        target_names.append("Gobilda Servo (GB)")
    elif sku_u.startswith("2106-") or "rex shaft" in title_l or "d-shaft" in title_l:
        target_names.append("Custom Shaft")
    elif sku_u.startswith("3110-"):
        target_names.append("Swingarm Odo Pod (GB)")
    elif sku_u.startswith("3212-") or "turret" in title_l:
        target_names.append("Turret (GB)")
    elif sku_u.startswith("3309-") or sku_u.startswith("3414-") or sku_u.startswith("3417-") or "pulley" in title_l:
        target_names.append("S - Pulley (GB)")
    elif sku_u.startswith("3600-") or sku_u.startswith("3603-") or "omni" in title_l or "roller" in title_l:
        target_names.extend(["S - Rollers (GB)", "S - Boot Wheels (GB)"])
    elif sku_u.startswith("5202-") or sku_u.startswith("5203-") or sku_u.startswith("5204-"):
        target_names.append("Motors (GB)")
    elif sku_u.startswith("REV-21-1650") or "neo brushless" in title_l:
        target_names.extend(["NEO Brushless Motor", "NEO 2.0 Brushless Motor"])
    elif sku_u.startswith("REV-31-1596") or "through bore encoder" in title_l:
        target_names.append("Throughbore Encoders")
    elif sku_u.startswith("REV-21-2158") or sku_u.startswith("REV-21-3005"):
        target_names.append('Aluminum Box Tube (WCP)')
    elif sku_u.startswith("REV-41-1300") or sku_u.startswith("REV-41-1600"):
        target_names.append("Motors (GB)")
    elif sku_u.startswith("REV-41-1359") or sku_u.startswith("REV-41-1683"):
        target_names.append("S - Extrusion (REV)")
    elif "hex" in title_l and ("shaft" in title_l or "axle" in title_l):
        target_names.append('1/2" Hex Shaft Spacer (WCP)')
    elif "collar" in title_l or "spacer" in title_l:
        target_names.extend(["S - Clamping Collars (GB)", "Shaft Collar (WCP)"])
    elif "compliant" in title_l or cat_l == "wheels":
        target_names.extend(["Compliant Wheel (AM)", "Stealth Wheel Flywheel (AM)", "S - Flywheels (GB)"])

    conn = get_db(DB_PATH)
    try:
        row = None
        if sku_u:
            row = conn.execute(
                "SELECT library_id, name, document_id, version_id, element_id, microversion_id, element_type "
                "FROM onshape_insertables WHERE name LIKE ? OR records_json LIKE ? LIMIT 1",
                (f"%{sku_u}%", f"%{sku_u}%"),
            ).fetchone()
        if not row:
            for tname in target_names:
                row = conn.execute(
                    "SELECT library_id, name, document_id, version_id, element_id, microversion_id, element_type "
                    "FROM onshape_insertables WHERE name = ? LIMIT 1",
                    (tname,),
                ).fetchone()
                if row:
                    break
        if not row and target_names:
            for tname in target_names:
                row = conn.execute(
                    "SELECT library_id, name, document_id, version_id, element_id, microversion_id, element_type "
                    "FROM onshape_insertables WHERE name LIKE ? LIMIT 1",
                    (f"%{tname}%",),
                ).fetchone()
                if row:
                    break
        if not row:
            return None
        d = dict(row)
        d["onshape_url"] = (
            f"https://cad.onshape.com/documents/{d['document_id']}/v/{d['version_id']}/e/{d['element_id']}"
        )
        return d
    finally:
        conn.close()


def _lookup_cots_catalog_row(sku_or_query: str, step_path: Path, vendor: str | None = None) -> dict:
    """Resolve catalog metadata (`vendor`, `sku`, `title`, `category`, `program`, `product_url`) for a loaded COTS STEP file."""
    from tools.cad_scraper import COTS_DIR, cots_info, cots_search

    try:
        info = cots_info(sku_or_query)
        if "local" in info and info["local"]:
            return dict(info["local"])
    except Exception:
        pass
    try:
        res = cots_search(sku_or_query, vendor=vendor, source="local", limit=5)
        for row in res.get("local_parts", []):
            return dict(row)
    except Exception:
        pass
    try:
        rel = step_path.relative_to(COTS_DIR).as_posix()
    except Exception:
        rel = step_path.name
    return {
        "part_id": step_path.stem,
        "vendor": vendor or "cots",
        "sku": sku_or_query,
        "title": step_path.stem,
        "category": "hardware",
        "program": "FTC/FRC",
        "product_url": None,
        "local_path": f"cots/{rel}",
    }


def get_cots_functional_frames(shape, sku_or_query: str = "") -> dict:
    """
    Computes semantic mechanical functional frames (`output_shaft`, `ear_mount`,
    `flange_seat`, `sense_origin`, `bore_min`, `bore_center`, `bore_max`) in the
    current coordinate frame of `shape`. Works across arbitrary 3D orientations
    and caches results on `shape._cached_functional_frames`.
    """
    cached = getattr(shape, "_cached_functional_frames", None)
    if cached is not None:
        return cached
    meta = getattr(shape, "_cots_meta", None)
    if meta and meta.get("functional_frames"):
        return meta["functional_frames"]

    from build123d import GeomType

    bb = shape.bounding_box()
    cx = 0.5 * (bb.min.X + bb.max.X)
    cy = 0.5 * (bb.min.Y + bb.max.Y)
    cz = 0.5 * (bb.min.Z + bb.max.Z)
    dims = {"X": bb.max.X - bb.min.X, "Y": bb.max.Y - bb.min.Y, "Z": bb.max.Z - bb.min.Z}
    bmin = {"X": bb.min.X, "Y": bb.min.Y, "Z": bb.min.Z}
    bmax = {"X": bb.max.X, "Y": bb.max.Y, "Z": bb.max.Z}
    sku_u = str(sku_or_query or getattr(shape, "label", "") or "").upper()

    frames = {
        "bbox_center": {"origin_mm": [round(cx, 3), round(cy, 3), round(cz, 3)], "axis": "Z"},
        "bore_min": {"origin_mm": [round(cx, 3), round(cy, 3), round(bb.min.Z, 3)], "axis": "Z"},
        "bore_center": {"origin_mm": [round(cx, 3), round(cy, 3), round(cz, 3)], "axis": "Z"},
        "bore_max": {"origin_mm": [round(cx, 3), round(cy, 3), round(bb.max.Z, 3)], "axis": "Z"},
    }

    ax_info = detect_shape_primary_axis(shape) or {}
    prim_ax = ax_info.get("primary_axis") or "Z"
    ax_c = ax_info.get("axis_center_mm") or [cx, cy, cz]
    frames["bore_center"] = {
        "origin_mm": [round(float(v), 3) for v in ax_c],
        "axis": prim_ax,
        "radius_mm": ax_info.get("min_bore_radius_mm"),
    }
    frames["mounting_holes"] = (
        json.loads(ax_info["mounting_holes_json"])
        if ax_info.get("mounting_holes_json")
        else None
    )
    ax_idx = {"X": 0, "Y": 1, "Z": 2}.get(prim_ax, 2)
    bmin_pt = [round(float(v), 3) for v in ax_c]
    bmax_pt = [round(float(v), 3) for v in ax_c]
    bmin_pt[ax_idx] = round(bmin[prim_ax], 3)
    bmax_pt[ax_idx] = round(bmax[prim_ax], 3)
    frames["bore_min"] = {"origin_mm": bmin_pt, "axis": prim_ax}
    frames["bore_max"] = {"origin_mm": bmax_pt, "axis": prim_ax}

    sorted_dims = sorted(dims.values())
    is_rc_servo = (
        "SERVO_SR-SERVO-01" in sku_u
        or "SR-SERVO-01" in sku_u
        or (
            abs(sorted_dims[0] - 20.2) < 2.0
            and abs(sorted_dims[1] - 46.2) < 2.0
            and abs(sorted_dims[2] - 55.0) < 2.0
        )
    )

    if is_rc_servo:
        # Identify which world axis corresponds to the 46.2 mm height (spline axis)
        spline_ax = min(("X", "Y", "Z"), key=lambda a: abs(dims[a] - 46.2))
        other_axes = [a for a in ("X", "Y", "Z") if a != spline_ax]
        # Determine which end (min or max along spline_ax) has the narrow 25T spline boss
        # by inspecting faces in the outer 4.2 mm slice at both ends.
        min_slice_faces = []
        max_slice_faces = []
        for f in shape.faces():
            fbb = f.bounding_box()
            fmin = {"X": fbb.min.X, "Y": fbb.min.Y, "Z": fbb.min.Z}
            fmax = {"X": fbb.max.X, "Y": fbb.max.Y, "Z": fbb.max.Z}
            if fmax[spline_ax] <= bmin[spline_ax] + 4.5:
                min_slice_faces.append((fmin, fmax))
            if fmin[spline_ax] >= bmax[spline_ax] - 4.5:
                max_slice_faces.append((fmin, fmax))

        def _slice_span(flist):
            if not flist:
                return 999.0
            a0, a1 = other_axes
            s0 = max(x[1][a0] for x in flist) - min(x[0][a0] for x in flist)
            s1 = max(x[1][a1] for x in flist) - min(x[0][a1] for x in flist)
            return max(s0, s1)

        use_max_end = _slice_span(max_slice_faces) <= _slice_span(min_slice_faces)
        tip_faces = max_slice_faces if use_max_end else min_slice_faces
        pt = {"X": cx, "Y": cy, "Z": cz}
        if tip_faces:
            for a in other_axes:
                pt[a] = 0.5 * (min(x[0][a] for x in tip_faces) + max(x[1][a] for x in tip_faces))
        if use_max_end:
            pt[spline_ax] = bmax[spline_ax] - 5.70
            ear_coord = bmin[spline_ax] + 31.52
        else:
            pt[spline_ax] = bmin[spline_ax] + 5.70
            ear_coord = bmax[spline_ax] - 31.52
        frames["output_shaft"] = {
            "origin_mm": [round(pt["X"], 3), round(pt["Y"], 3), round(pt["Z"], 3)],
            "axis": spline_ax,
        }
        ear_pt = dict(pt)
        ear_pt[spline_ax] = ear_coord
        frames["ear_mount"] = {
            "origin_mm": [round(ear_pt["X"], 3), round(ear_pt["Y"], 3), round(ear_pt["Z"], 3)],
            "axis": spline_ax,
        }
    elif len(shape.faces()) > 250:
        frames["output_shaft"] = dict(frames["bore_center"])
        frames["flange_seat"] = dict(frames["bore_min"])
    else:
        top_cyls = []
        flange_cyls = []
        for f in shape.faces():
            if f.geom_type in (GeomType.CYLINDER, GeomType.CONE):
                fbb = f.bounding_box()
                rx = 0.5 * (fbb.max.X - fbb.min.X)
                ry = 0.5 * (fbb.max.Y - fbb.min.Y)
                if abs(rx - ry) < 0.8 and 1.2 <= rx <= 14.0:
                    fcx = 0.5 * (fbb.min.X + fbb.max.X)
                    fcy = 0.5 * (fbb.min.Y + fbb.max.Y)
                    if fbb.max.Z >= bb.max.Z - 5.5 and rx <= 7.5:
                        top_cyls.append((fcx, fcy, fbb.min.Z, fbb.max.Z, rx))
                    flange_cyls.append((fcx, fcy, fbb.min.Z, fbb.max.Z, rx))
        if top_cyls and prim_ax == "Z":
            sx = sorted(c[0] for c in top_cyls)[len(top_cyls) // 2]
            sy = sorted(c[1] for c in top_cyls)[len(top_cyls) // 2]
            sz = min(c[2] for c in top_cyls)
            frames["output_shaft"] = {"origin_mm": [round(sx, 3), round(sy, 3), round(sz, 3)], "axis": "Z"}
        else:
            frames["output_shaft"] = dict(frames["bore_center"])

        if flange_cyls:
            max_r = max(c[4] for c in flange_cyls)
            outer_flanges = [c for c in flange_cyls if c[4] >= max_r - 0.5]
            body_cyls = [c for c in flange_cyls if max_r - 3.5 <= c[4] <= max_r - 0.6]
            if outer_flanges and body_cyls:
                fz_min = min(c[2] for c in outer_flanges)
                fz_max = max(c[3] for c in outer_flanges)
                seat_z = fz_max if abs(fz_min - bb.min.Z) < abs(fz_max - bb.max.Z) else fz_min
                frames["flange_seat"] = {
                    "origin_mm": [round(float(ax_c[0]), 3), round(float(ax_c[1]), 3), round(seat_z, 3)],
                    "axis": prim_ax,
                }
            else:
                frames["flange_seat"] = dict(frames["bore_min"])
        else:
            frames["flange_seat"] = dict(frames["bore_min"])

    if "ear_mount" not in frames:
        frames["ear_mount"] = dict(frames["bore_center"])
    if "flange_seat" not in frames:
        frames["flange_seat"] = dict(frames["bore_min"])
    frames["sense_origin"] = {
        "origin_mm": [round(cx, 3), round(cy, 3), round(bb.min.Z, 3)],
        "axis": "Z",
    }
    try:
        shape._cached_functional_frames = frames
    except Exception:
        pass
    return frames


_COTS_MEM_CACHE: dict[tuple[str, float], tuple] = {}
_COTS_ORIENTED_FRAMES_CACHE: dict[tuple[str, float, str | None, float, float, float], tuple] = {}


def _load_or_build_cots_binbrep(step_path: Path, min_solid_volume_mm3: float = 50.0):
    """
    Fast two-tier (in-memory + OpenCASCADE `BinTools` binary B-Rep + JSON sidecar)
    loader for COTS STEP files. Reduces a 16-second ASCII STEP import + solid volume
    filter + primary-axis scan down to ~150 ms on disk cache hit and <1 ms in memory.
    """
    import hashlib
    from build123d import Compound, Shape, Solid, import_step
    from OCP.BinTools import BinTools
    from OCP.TopoDS import TopoDS_Shape

    key = (str(step_path.resolve()), float(min_solid_volume_mm3))
    if key in _COTS_MEM_CACHE:
        cached_shape, cached_ax, cached_frames = _COTS_MEM_CACHE[key]
        sh_copy = Solid(cached_shape.wrapped) if isinstance(cached_shape, Solid) else Shape(cached_shape.wrapped)
        try:
            sh_copy._cached_primary_axis = cached_ax
            if cached_frames is not None:
                sh_copy._cached_functional_frames = cached_frames
        except Exception:
            pass
        return sh_copy

    st = step_path.stat()
    cache_dir = step_path.parents[2] / "cache" / "binbrep" if "vendors" in step_path.parts else step_path.parent / "cache" / "binbrep"
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(f"{step_path.name}:{st.st_size}:{int(st.st_mtime)}:{min_solid_volume_mm3}".encode("utf-8")).hexdigest()[:16]
    bin_file = cache_dir / f"{step_path.stem[:48]}_{h}.binbrep"
    meta_file = cache_dir / f"{step_path.stem[:48]}_{h}.json"

    if bin_file.is_file() and meta_file.is_file():
        try:
            raw = TopoDS_Shape()
            BinTools.Read_s(raw, str(bin_file))
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            sh = Solid(raw) if meta.get("is_solid") else Shape(raw)
            ax_info = meta.get("primary_axis_info")
            frames = meta.get("functional_frames")
            try:
                sh._cached_primary_axis = ax_info
                if frames is not None:
                    sh._cached_functional_frames = frames
            except Exception:
                pass
            _COTS_MEM_CACHE[key] = (sh, ax_info, frames)
            return sh
        except Exception:
            pass

    shape = import_step(str(step_path))
    sols = shape.solids()
    if min_solid_volume_mm3 > 0.0 and len(sols) > 15:
        kept = [sol for sol in sols if sol.volume >= min_solid_volume_mm3]
        if kept:
            shape = Compound(children=kept)
    shape = bake_shape_to_world_solid(shape)
    try:
        shape._accum_mat4 = None
    except Exception:
        pass
    ax_info = detect_shape_primary_axis(shape)
    frames = get_cots_functional_frames(shape, sku_or_query=step_path.stem)

    try:
        BinTools.Write_s(shape.wrapped, str(bin_file))
        meta_file.write_text(
            json.dumps(
                {
                    "is_solid": isinstance(shape, Solid),
                    "primary_axis_info": ax_info,
                    "functional_frames": frames,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    _COTS_MEM_CACHE[key] = (shape, ax_info, frames)
    return shape


def load_cots_part(
    sku_or_query: str,
    label: str | None = None,
    rgb: tuple[int, int, int] | tuple[float, float, float] | None = None,
    vendor: str | None = None,
    rx_deg: float = 0.0,
    ry_deg: float = 0.0,
    rz_deg: float = 0.0,
    bore_axis: str | None = None,
    align: tuple[str | None, str | None, str | None] | None = None,
    anchor: str | None = None,
    at: tuple[float, float, float] = (0.0, 0.0, 0.0),
    min_solid_volume_mm3: float = 50.0,
):
    """
    Imports a verified local COTS STEP file via `_load_or_build_cots_binbrep()` by SKU,
    part_key, or keyword search query, optionally filters microscopic internal
    bearing balls/fasteners (`< min_solid_volume_mm3` when `len(solids) > 15`),
    rotates its primary bore/shaft axis to `bore_axis` ('X', 'Y', or 'Z'),
    aligns either by semantic `anchor` (`'output_shaft'`, `'ear_mount'`, `'flange_seat'`,
    `'sense_origin'`, `'bore_min'`, `'bore_center'`, `'bore_max'`) or by
    B-Rep bounding box / cylindrical axis (`align=('axis', 'axis', 'min')`)
    to world coordinates `at=(x, y, z)`, bakes the solid into world coordinates,
    assigns STEP AP214 label and RGB color, and attaches `_cots_meta`.
    """
    from build123d import Compound, Pos, Rot, Solid

    step_path = resolve_cots_step_path(sku_or_query, vendor=vendor)
    cat_row = _lookup_cots_catalog_row(sku_or_query, step_path, vendor=vendor)
    shape = _load_or_build_cots_binbrep(step_path, min_solid_volume_mm3=min_solid_volume_mm3)

    translated_world_frames = None
    if anchor is not None:
        orient_key = (str(step_path.resolve()), float(min_solid_volume_mm3), bore_axis, float(rx_deg), float(ry_deg), float(rz_deg))
        if orient_key in _COTS_ORIENTED_FRAMES_CACHE:
            oriented_shape, frames_pre = _COTS_ORIENTED_FRAMES_CACHE[orient_key]
            shape = Solid(oriented_shape.wrapped)
            try:
                shape._accum_mat4 = getattr(oriented_shape, "_accum_mat4", None)
            except Exception:
                pass
        else:
            if bore_axis is not None or rx_deg != 0.0 or ry_deg != 0.0 or rz_deg != 0.0:
                shape = align_and_place_shape(
                    shape,
                    rx_deg=rx_deg,
                    ry_deg=ry_deg,
                    rz_deg=rz_deg,
                    bore_axis=bore_axis,
                    align=None,
                    at=(0.0, 0.0, 0.0),
                )
            frames_pre = get_cots_functional_frames(shape, sku_or_query=cat_row.get("sku") or sku_or_query)
            _COTS_ORIENTED_FRAMES_CACHE[orient_key] = (shape, frames_pre)
        target_frame = frames_pre.get(anchor) or frames_pre["bore_center"]
        orig = target_frame["origin_mm"]
        dx = float(at[0]) - float(orig[0])
        dy = float(at[1]) - float(orig[1])
        dz = float(at[2]) - float(orig[2])
        orient_mat4 = getattr(shape, "_accum_mat4", None)
        shape = bake_shape_to_world_solid(Pos(dx, dy, dz) * shape, prev_mat4=orient_mat4)
        translated_world_frames = {}
        for fk, fv in frames_pre.items():
            if not isinstance(fv, dict):
                translated_world_frames[fk] = fv
                continue
            nfv = dict(fv)
            if "origin_mm" in nfv:
                nfv["origin_mm"] = [
                    round(float(nfv["origin_mm"][0]) + dx, 3),
                    round(float(nfv["origin_mm"][1]) + dy, 3),
                    round(float(nfv["origin_mm"][2]) + dz, 3),
                ]
            if fk == "mounting_holes" and nfv.get("centers_2d_mm"):
                mh_ax = (nfv.get("axis") or "Z").upper()
                du, dv = (dx, dy) if mh_ax == "Z" else ((dx, dz) if mh_ax == "Y" else (dy, dz))
                nfv["centers_2d_mm"] = [
                    [round(float(p[0]) + du, 3), round(float(p[1]) + dv, 3)]
                    for p in nfv["centers_2d_mm"]
                ]
            translated_world_frames[fk] = nfv
        try:
            shape._cached_functional_frames = translated_world_frames
        except Exception:
            pass
    elif (
        bore_axis is not None
        or rx_deg != 0.0
        or ry_deg != 0.0
        or rz_deg != 0.0
        or align is not None
        or any(v != 0.0 for v in at)
    ):
        shape = align_and_place_shape(
            shape,
            rx_deg=rx_deg,
            ry_deg=ry_deg,
            rz_deg=rz_deg,
            bore_axis=bore_axis,
            align=align,
            at=at,
        )
    else:
        shape = bake_shape_to_world_solid(shape)

    if label:
        shape.label = label
    elif not getattr(shape, "label", None):
        shape.label = step_path.stem
    if rgb is not None:
        if any(c > 1.0 for c in rgb):
            r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
        else:
            r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
        shape.color = Color(r, g, b)

    sku_val = cat_row.get("sku") or sku_or_query
    title_val = cat_row.get("title") or step_path.stem
    vendor_val = cat_row.get("vendor", vendor or "cots")
    cat_val = cat_row.get("category", "hardware")
    os_ins = _resolve_onshape_insertable(sku_val, title_val, vendor_val, cat_val)
    world_frames = translated_world_frames or get_cots_functional_frames(shape, sku_or_query=sku_val)
    bb_center = list((world_frames.get("bbox_center") or {}).get("origin_mm") or [0.0, 0.0, 0.0])
    world_ax = world_frames["bore_center"]["axis"]
    world_center = list(world_frames["bore_center"]["origin_mm"])
    sc_mat4 = _compute_solidcraft_world_matrix_4x4(
        getattr(shape, "_accum_mat4", None),
        cat_row.get("solidcraft_matrix_json"),
    )

    shape._cots_meta = {
        "is_cots": True,
        "query": sku_or_query,
        "part_id": cat_row.get("part_id", step_path.stem),
        "vendor": vendor_val,
        "sku": sku_val,
        "title": title_val,
        "category": cat_val,
        "program": cat_row.get("program", "FTC/FRC"),
        "product_url": cat_row.get("product_url"),
        "local_path": cat_row.get("local_path", str(step_path)),
        "solidcraft_path": cat_row.get("solidcraft_path"),
        "solidcraft_material": cat_row.get("solidcraft_material"),
        "solidcraft_world_matrix_4x4": sc_mat4,
        "placement_spec": {
            "rx_deg": rx_deg,
            "ry_deg": ry_deg,
            "rz_deg": rz_deg,
            "bore_axis": bore_axis,
            "align": list(align) if align is not None else None,
            "anchor": anchor,
            "at": [round(float(v), 3) for v in at],
        },
        "world_bore_axis": world_ax,
        "world_axis_center_mm": [round(float(v), 3) for v in world_center],
        "world_bbox_center_mm": bb_center,
        "world_output_shaft_mm": world_frames["output_shaft"]["origin_mm"],
        "functional_frames": world_frames,
        "min_bore_radius_mm": world_frames["bore_center"].get("radius_mm") or cat_row.get("min_bore_radius_mm"),
        "mounting_holes": world_frames.get("mounting_holes") or cat_row.get("mounting_holes"),
        "onshape_insertable": os_ins,
    }
    return shape



def _estimate_part_mass_and_material(part) -> tuple[float, str, float]:
    """
    Returns `(mass_g, material_label, effective_density_g_cm3)` for a `build123d` Shape,
    combining known COTS hardware masses (motors, servos, encoders, bearings) with
    material density rules for aluminum/steel/polyurethane COTS parts and custom
    3D-printed/laser-cut components.
    """
    vol_cm3 = max(float(getattr(part, "volume", 0.0)) / 1000.0, 0.01)
    lbl = (getattr(part, "label", "") or "").lower()
    meta = getattr(part, "_cots_meta", None) or {}
    sku = (meta.get("sku") or "").upper()
    title = (meta.get("title") or "").lower()
    cat = (meta.get("category") or "").lower()

    if meta.get("is_cots"):
        # Known datasheet masses for COTS motors, servos, sensors, and bearings
        if sku.startswith("5202-") or sku.startswith("5203-") or sku.startswith("5204-"):
            return 415.0, "Motor Assembly (Steel/Copper/Al)", round(415.0 / vol_cm3, 2)
        if sku.startswith("REV-21-1650"):
            return 426.0, "NEO Brushless Motor (Steel/Copper/Al)", round(426.0 / vol_cm3, 2)
        if sku.startswith("REV-41-1300") or sku.startswith("REV-41-1600"):
            return 390.0, "HD Hex Motor (Steel/Copper/Al)", round(390.0 / vol_cm3, 2)
        if sku.startswith("2000-") or "servo" in title or "servo" in lbl:
            return 62.0, "2000-Series Dual Mode Servo", round(62.0 / vol_cm3, 2)
        if sku.startswith("REV-31-1596") or "encoder" in title or "encoder" in lbl:
            return 28.0, "Through Bore Optical Encoder", round(28.0 / vol_cm3, 2)
        if sku.startswith("1611-") or "bearing" in title or "brg_" in lbl:
            return max(vol_cm3 * 4.5, 6.2), "Chrome Steel 52100 Bearing", 4.50
        if sku.startswith("2106-") or ("shaft" in lbl or "axle" in lbl):
            return vol_cm3 * 7.85, "Alloy Steel (7.85 g/cm³)", 7.85
        if cat == "wheels" or any(k in title or k in lbl for k in ("wheel", "roller", "omni", "flywheel")):
            return vol_cm3 * 1.18, "Polyurethane / Delrin Core (1.18 g/cm³)", 1.18
        if sku.startswith("3212-"):
            return vol_cm3 * 3.20, "Aluminum + Steel Crossed-Roller Bearing", 3.20
        return vol_cm3 * 2.70, "6061-T6 Aluminum (2.70 g/cm³)", 2.70

    bb = part.bounding_box()
    dims = (bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z)
    if min(dims) <= 8.5 and max(dims) >= 25.0:
        return vol_cm3 * 2.70, "6061-T6 Aluminum Plate (2.70 g/cm³)", 2.70
    return vol_cm3 * 1.25, "3D Print PETG / PLA-CF (1.25 g/cm³)", 1.25


def compute_assembly_mass_properties(parts: list) -> dict:
    """
    Computes exact B-Rep volume, estimated mass (`g`, `kg`, `lb`), 3D Center-of-Gravity
    `cg_mm = [x, y, z]`, world-frame inertia diagonal `inertia_cg_kg_mm2 = [Ixx, Iyy, Izz]`
    via OpenCASCADE `BRepGProp.VolumeProperties_s` + parallel-axis theorem, and total
    rotating drive inertia `J_drive_kg_m2` / `J_drive_kg_mm2` for ARES feedforward control.
    """
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    part_rows = []
    total_mass_g = 0.0
    wx, wy, wz = 0.0, 0.0, 0.0

    for idx, p in enumerate(parts, 1):
        lbl = getattr(p, "label", None) or f"Part_{idx:02d}"
        wrapped = getattr(p, "wrapped", p)
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(wrapped, props)
        vol_mm3 = max(float(props.Mass()), 1.0)
        com = props.CentreOfMass()
        cx, cy, cz = float(com.X()), float(com.Y()), float(com.Z())
        mat = props.MatrixOfInertia()
        # Geometric inertia tensor diagonal (in mm^5) at part COM
        g_ixx = float(mat.Value(1, 1))
        g_iyy = float(mat.Value(2, 2))
        g_izz = float(mat.Value(3, 3))

        mass_g, mat_name, dens = _estimate_part_mass_and_material(p)
        mass_kg = mass_g / 1000.0
        scale = mass_kg / vol_mm3
        ixx_kg_mm2 = g_ixx * scale
        iyy_kg_mm2 = g_iyy * scale
        izz_kg_mm2 = g_izz * scale

        is_rot = _is_rotating_part(p)
        meta = getattr(p, "_cots_meta", None) or {}
        rot_ax = meta.get("world_bore_axis")
        if not rot_ax:
            ax_info = detect_shape_primary_axis(p) or {}
            rot_ax = ax_info.get("primary_axis", "Z")
        j_axis_kg_mm2 = {"X": ixx_kg_mm2, "Y": iyy_kg_mm2, "Z": izz_kg_mm2}.get(rot_ax, izz_kg_mm2)

        total_mass_g += mass_g
        wx += mass_g * cx
        wy += mass_g * cy
        wz += mass_g * cz

        part_rows.append(
            {
                "label": lbl,
                "material": mat_name,
                "density_g_cm3": dens,
                "volume_cm3": round(vol_mm3 / 1000.0, 2),
                "mass_g": round(mass_g, 2),
                "cg_mm": [round(cx, 2), round(cy, 2), round(cz, 2)],
                "inertia_com_kg_mm2": [round(ixx_kg_mm2, 2), round(iyy_kg_mm2, 2), round(izz_kg_mm2, 2)],
                "is_rotating": is_rot,
                "rot_axis": rot_ax,
                "j_axis_kg_mm2": round(j_axis_kg_mm2, 2),
            }
        )

    if total_mass_g > 0.0:
        cg_x, cg_y, cg_z = wx / total_mass_g, wy / total_mass_g, wz / total_mass_g
    else:
        cg_x, cg_y, cg_z = 0.0, 0.0, 0.0

    # Parallel-axis theorem to assembly CG
    tot_ixx, tot_iyy, tot_izz = 0.0, 0.0, 0.0
    j_drive_kg_mm2 = 0.0
    for pr in part_rows:
        m_kg = pr["mass_g"] / 1000.0
        dx = pr["cg_mm"][0] - cg_x
        dy = pr["cg_mm"][1] - cg_y
        dz = pr["cg_mm"][2] - cg_z
        tot_ixx += pr["inertia_com_kg_mm2"][0] + m_kg * (dy * dy + dz * dz)
        tot_iyy += pr["inertia_com_kg_mm2"][1] + m_kg * (dx * dx + dz * dz)
        tot_izz += pr["inertia_com_kg_mm2"][2] + m_kg * (dx * dx + dy * dy)
        if pr["is_rotating"]:
            j_drive_kg_mm2 += pr["j_axis_kg_mm2"]

    total_mass_kg = total_mass_g / 1000.0
    return {
        "total_mass_g": round(total_mass_g, 1),
        "total_mass_kg": round(total_mass_kg, 4),
        "total_mass_lb": round(total_mass_kg * 2.20462262, 3),
        "cg_mm": [round(cg_x, 2), round(cg_y, 2), round(cg_z, 2)],
        "inertia_cg_kg_mm2": [round(tot_ixx, 1), round(tot_iyy, 1), round(tot_izz, 1)],
        "inertia_cg_kg_m2": [round(tot_ixx * 1e-6, 6), round(tot_iyy * 1e-6, 6), round(tot_izz * 1e-6, 6)],
        "j_drive_kg_mm2": round(j_drive_kg_mm2, 2),
        "j_drive_kg_m2": round(j_drive_kg_mm2 * 1e-6, 6),
        "parts": part_rows,
    }


def _rotate_shape_about_axis(shape, axis: str, center_mm: tuple[float, float, float], angle_deg: float):
    """Rotates `shape` by `angle_deg` around world axis `'X'`, `'Y'`, or `'Z'` passing through `center_mm`."""
    if abs(angle_deg) < 1e-6:
        return shape
    from build123d import Pos, Rot

    cx, cy, cz = float(center_mm[0]), float(center_mm[1]), float(center_mm[2])
    ax = (axis or "Z").upper()
    if ax == "X":
        r = Rot(angle_deg, 0.0, 0.0)
    elif ax == "Y":
        r = Rot(0.0, angle_deg, 0.0)
    else:
        r = Rot(0.0, 0.0, angle_deg)
    return Pos(cx, cy, cz) * r * Pos(-cx, -cy, -cz) * shape


def verify_kinematic_sweep(
    parts: list,
    constraints: dict,
    angles_deg: tuple[float, ...] = (-20.0, 0.0, 20.0),
    max_overlap_mm3: float = 5.0,
) -> dict:
    """
    Multi-Pose Kinematic Sweep Collision Auditor:
    Applies rotational transforms across `angles_deg` to every `REVOLUTE` joint in
    `constraints["mates"]` around its world datum (`mc2`) and checks that rotating
    parts do not collide (`overlap_mm3 > max_overlap_mm3`) with any non-coaxial
    stationary or moving part across the entire motion envelope.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    part_by_label = {}
    for idx, p in enumerate(parts, 1):
        lbl = getattr(p, "label", None) or f"Part_{idx:02d}"
        part_by_label[lbl] = p

    mc_by_key = {mc["key"]: mc for mc in constraints.get("mate_connectors", [])}
    revolute_joints = []
    for m in constraints.get("mates", []):
        if str(m.get("type", "")).upper() != "REVOLUTE":
            continue
        mc2 = mc_by_key.get(m.get("mc2"))
        if not mc2:
            continue
        plbl = mc2.get("part")
        if plbl in part_by_label:
            revolute_joints.append(
                {
                    "mate_name": m.get("name", m.get("key")),
                    "part": plbl,
                    "axis": str(mc2.get("axis", "Z")).upper(),
                    "center_mm": (
                        float(mc2.get("tx_mm", 0.0)),
                        float(mc2.get("ty_mm", 0.0)),
                        float(mc2.get("tz_mm", 0.0)),
                    ),
                }
            )

    poses_report = []
    all_passed = True

    base_bbs = {lbl: sh.bounding_box() for lbl, sh in part_by_label.items()}
    for deg in angles_deg:
        posed_shapes = dict(part_by_label)
        bbs = dict(base_bbs)
        rotated_labels = set()
        for rj in revolute_joints:
            plbl = rj["part"]
            rl_low = plbl.lower()
            is_axisymmetric_rotor = any(
                k in rl_low
                for k in ("pulley", "collar", "wheel", "flywheel", "roller", "gear", "sprocket", "shaft", "axle", "bearing")
            )
            if is_axisymmetric_rotor and abs(float(deg)) > 1e-6:
                rotated_labels.add(plbl)
                continue
            posed_shapes[plbl] = _rotate_shape_about_axis(
                part_by_label[plbl], rj["axis"], rj["center_mm"], float(deg)
            )
            if abs(float(deg)) > 1e-6:
                bbs[plbl] = posed_shapes[plbl].bounding_box()
            rotated_labels.add(plbl)

        # Map each part to all mate-connector centers defined on it in constraints
        part_mc_centers: dict[str, list[tuple[float, float, float]]] = {}
        for mc in constraints.get("mate_connectors", []):
            p_mc = mc.get("part")
            if p_mc:
                part_mc_centers.setdefault(p_mc, []).append(
                    (float(mc.get("tx_mm", 0.0)), float(mc.get("ty_mm", 0.0)), float(mc.get("tz_mm", 0.0)))
                )

        # Check collisions between rotated parts and stationary/other parts with non-identical axis center
        pose_collisions = []
        pairs_checked = 0

        for rj in revolute_joints:
            rlbl = rj["part"]
            rshape = posed_shapes[rlbl]
            rbb = bbs[rlbl]
            rcx, rcy, rcz = rj["center_mm"]
            rax = rj["axis"]

            for olbl, oshape in posed_shapes.items():
                if olbl == rlbl:
                    continue
                obb = bbs[olbl]
                if (
                    rbb.max.X < obb.min.X
                    or obb.max.X < rbb.min.X
                    or rbb.max.Y < obb.min.Y
                    or obb.max.Y < rbb.min.Y
                    or rbb.max.Z < obb.min.Z
                    or obb.max.Z < rbb.min.Z
                ):
                    continue

                # Skip coaxial bearing/shaft/motor/wheel/plate-bore interfaces that share the same shaft axis line
                ometa = getattr(part_by_label[olbl], "_cots_meta", None) or {}
                candidate_centers = list(part_mc_centers.get(olbl, []))
                if ometa.get("world_axis_center_mm"):
                    candidate_centers.append(tuple(ometa["world_axis_center_mm"]))
                candidate_centers.append(
                    (
                        0.5 * (obb.min.X + obb.max.X),
                        0.5 * (obb.min.Y + obb.max.Y),
                        0.5 * (obb.min.Z + obb.max.Z),
                    )
                )
                is_coaxial = False
                for oc in candidate_centers:
                    if rax == "X" and math.hypot(oc[1] - rcy, oc[2] - rcz) < 4.5:
                        is_coaxial = True
                        break
                    if rax == "Y" and math.hypot(oc[0] - rcx, oc[2] - rcz) < 4.5:
                        is_coaxial = True
                        break
                    if rax == "Z" and math.hypot(oc[0] - rcx, oc[1] - rcy) < 4.5:
                        is_coaxial = True
                        break
                if is_coaxial:
                    continue

                rl_low = rlbl.lower()
                ol_low = olbl.lower()
                if "belt" in rl_low or "belt" in ol_low or "chain" in rl_low or "chain" in ol_low:
                    continue
                is_axisymmetric_rotor = any(
                    k in rl_low
                    for k in ("pulley", "collar", "wheel", "flywheel", "roller", "gear", "sprocket", "shaft", "axle", "bearing")
                )
                if is_axisymmetric_rotor and abs(float(deg)) > 1e-6:
                    continue
                if len(rshape.faces()) * len(oshape.faces()) > 2000:
                    continue

                pairs_checked += 1
                try:
                    common = BRepAlgoAPI_Common(
                        getattr(rshape, "wrapped", rshape),
                        getattr(oshape, "wrapped", oshape),
                    )
                    if common.IsDone():
                        props = GProp_GProps()
                        BRepGProp.VolumeProperties_s(common.Shape(), props)
                        vol = float(props.Mass())
                        if vol > max_overlap_mm3:
                            pose_collisions.append(
                                {
                                    "part_a": rlbl,
                                    "part_b": olbl,
                                    "overlap_mm3": round(vol, 2),
                                }
                            )
                except Exception:
                    pass

        pose_ok = len(pose_collisions) == 0
        if not pose_ok:
            all_passed = False
        poses_report.append(
            {
                "angle_deg": float(deg),
                "revolute_parts_swept": len(rotated_labels),
                "pairs_checked": pairs_checked,
                "collisions": pose_collisions,
                "status": "PASS" if pose_ok else "FAIL",
            }
        )

    return {
        "status": "PASS" if all_passed else "FAIL",
        "revolute_joints_count": len(revolute_joints),
        "angles_deg": list(angles_deg),
        "poses": poses_report,
    }


def export_cots_bom(
    parts: list,
    out_dir: str | Path,
    stem: str,
    assembly_title: str | None = None,
    mass_properties: dict | None = None,
    kinematic_sweep: dict | None = None,
) -> dict:
    """
    Generates `<stem>_bom.csv` and `<stem>_bom.md` from a list of `build123d` `Shape`
    instances (separating COTS purchased parts with `_cots_meta` from custom fabricated parts,
    including `FRCDesignApp` Onshape Cloud Library links, Subsystem Mass/CG/Inertia,
    and Multi-Pose Kinematic Sweep verification).
    """
    import csv

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    csv_file = out_path / f"{stem}_bom.csv"
    md_file = out_path / f"{stem}_bom.md"

    if mass_properties is None:
        mass_properties = compute_assembly_mass_properties(parts)

    cots_groups: dict[tuple[str, str], dict] = {}
    custom_items: list[dict] = []

    for idx, p in enumerate(parts, 1):
        lbl = getattr(p, "label", None) or f"Part_{idx:02d}"
        meta = getattr(p, "_cots_meta", None)
        bb = p.bounding_box()
        dims = (
            round(bb.max.X - bb.min.X, 2),
            round(bb.max.Y - bb.min.Y, 2),
            round(bb.max.Z - bb.min.Z, 2),
        )
        dim_str = f"{dims[0]:.1f} x {dims[1]:.1f} x {dims[2]:.1f}"
        if meta and meta.get("is_cots"):
            key = (str(meta.get("vendor", "cots")).upper(), str(meta.get("sku", lbl)))
            if key not in cots_groups:
                cots_groups[key] = {
                    "vendor": str(meta.get("vendor", "cots")).upper(),
                    "sku": str(meta.get("sku", lbl)),
                    "title": str(meta.get("title", lbl)),
                    "category": str(meta.get("category", "hardware")),
                    "program": str(meta.get("program", "FTC/FRC")),
                    "qty": 0,
                    "dims_mm": dim_str,
                    "bore_axis": str(meta.get("world_bore_axis", "Z")),
                    "product_url": meta.get("product_url") or "",
                    "local_path": meta.get("local_path") or "",
                    "onshape_insertable": meta.get("onshape_insertable"),
                }
            cots_groups[key]["qty"] += 1
        else:
            min_d = min(dims)
            max_d = max(dims)
            fab_type = "2D Laser / CNC Plate" if (min_d <= 8.5 and max_d >= 25.0) else "3D Print (FDM / SLA)"
            vol_cm3 = round(float(getattr(p, "volume", 0.0)) / 1000.0, 2)
            custom_items.append(
                {
                    "label": lbl,
                    "fab_type": fab_type,
                    "qty": 1,
                    "dims_mm": dim_str,
                    "volume_cm3": vol_cm3,
                }
            )

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Type",
                "Vendor",
                "SKU_or_PartID",
                "Title",
                "Category_or_Process",
                "Program",
                "Quantity",
                "Bounding_Box_mm",
                "Bore_Axis",
                "Product_URL",
                "Onshape_Library_URL",
            ]
        )
        for item in cots_groups.values():
            os_ins = item.get("onshape_insertable") or {}
            writer.writerow(
                [
                    "COTS",
                    item["vendor"],
                    item["sku"],
                    item["title"],
                    item["category"],
                    item["program"],
                    item["qty"],
                    item["dims_mm"],
                    item["bore_axis"],
                    item["product_url"],
                    os_ins.get("onshape_url", ""),
                ]
            )
        for citem in custom_items:
            writer.writerow(
                [
                    "CUSTOM",
                    "In-House",
                    citem["label"],
                    citem["label"],
                    citem["fab_type"],
                    "Custom",
                    citem["qty"],
                    citem["dims_mm"],
                    "-",
                    "",
                    "",
                ]
            )

    title_hdr = assembly_title or stem.replace("_", " ").title()
    md_lines = [
        f"# Bill of Materials & Mass/Kinematic Audit — {title_hdr}",
        "",
        "## 1. Purchased COTS Hardware",
        "",
        "| Vendor | SKU | Part Name | Category | Program | Qty | Envelope (mm) | Bore Axis | Source Link | Onshape Cloud Library |",
        "| :--- | :--- | :--- | :--- | :---: | :-: | :---: | :---: | :--- | :--- |",
    ]
    for item in cots_groups.values():
        link_md = f"[Product Page]({item['product_url']})" if item["product_url"] else f"`{item['local_path']}`"
        os_ins = item.get("onshape_insertable")
        if os_ins and os_ins.get("onshape_url"):
            os_md = f"[{os_ins['library_id']}: {os_ins['name']}]({os_ins['onshape_url']})"
        else:
            os_md = "`Local STEP`"
        md_lines.append(
            f"| **{item['vendor']}** | `{item['sku']}` | {item['title']} | `{item['category']}` | `{item['program']}` | **{item['qty']}** | `{item['dims_mm']}` | `{item['bore_axis']}` | {link_md} | {os_md} |"
        )
    md_lines.extend(
        [
            "",
            "## 2. Custom Fabricated Components",
            "",
            "| Part Label | Recommended Process | Qty | Bounding Box (mm) | Volume (cm³) |",
            "| :--- | :--- | :-: | :---: | :---: |",
        ]
    )
    for citem in custom_items:
        md_lines.append(
            f"| `{citem['label']}` | {citem['fab_type']} | **{citem['qty']}** | `{citem['dims_mm']}` | `{citem['volume_cm3']:.2f}` |"
        )

    if mass_properties:
        cg = mass_properties["cg_mm"]
        icg = mass_properties["inertia_cg_kg_mm2"]
        md_lines.extend(
            [
                "",
                "## 3. Subsystem Mass, Center-of-Gravity (CG), & Rotational Inertia (ARES Control)",
                "",
                f"- **Total Subsystem Mass**: **{mass_properties['total_mass_g']:.1f} g** (**{mass_properties['total_mass_kg']:.4f} kg** / **{mass_properties['total_mass_lb']:.3f} lb**)",
                f"- **Subsystem 3D Center of Gravity (CG)**: `[{cg[0]:.2f}, {cg[1]:.2f}, {cg[2]:.2f}] mm`",
                f"- **Subsystem Inertia Diagonal about CG ($I_{{xx}}, I_{{yy}}, I_{{zz}}$)**: `[{icg[0]:.1f}, {icg[1]:.1f}, {icg[2]:.1f}] kg·mm²`",
                f"- **Rotating Drive-Axis Inertia ($J_{{\\text{{drive}}}}$)**: **`{mass_properties['j_drive_kg_mm2']:.2f} kg·mm²`** (**`{mass_properties['j_drive_kg_m2']:.6f} kg·m²`**)",
                "",
                "| Part Label | Material / Spec | Role | Mass (g) | COM `[x, y, z]` (mm) | $J_{\\text{axis}}$ (kg·mm²) |",
                "| :--- | :--- | :---: | :---: | :---: | :---: |",
            ]
        )
        for pr in mass_properties.get("parts", []):
            role_str = f"Rotating (`{pr['rot_axis']}`)" if pr["is_rotating"] else "Stationary"
            c = pr["cg_mm"]
            md_lines.append(
                f"| `{pr['label']}` | {pr['material']} | {role_str} | **{pr['mass_g']:.1f}** | `[{c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f}]` | `{pr['j_axis_kg_mm2']:.2f}` |"
            )

    if kinematic_sweep:
        md_lines.extend(
            [
                "",
                f"## 4. Multi-Pose Kinematic Sweep Collision Audit (`{kinematic_sweep['status']}`)",
                "",
                "| Sweep Pose Angle | Revolute Parts Swept | B-Rep Pairs Checked | Collisions (`> 5 mm³`) | Status |",
                "| :---: | :-: | :-: | :-: | :---: |",
            ]
        )
        for pose in kinematic_sweep.get("poses", []):
            md_lines.append(
                f"| `{pose['angle_deg']:+.1f}°` | {pose['revolute_parts_swept']} | {pose['pairs_checked']} | **{len(pose['collisions'])}** | **{pose['status']}** |"
            )

    md_lines.append("")
    md_file.write_text("\n".join(md_lines), encoding="utf-8")

    return {
        "csv_path": str(csv_file),
        "md_path": str(md_file),
        "cots_unique_count": len(cots_groups),
        "cots_total_qty": sum(i["qty"] for i in cots_groups.values()),
        "custom_count": len(custom_items),
        "mass_properties": mass_properties,
    }


def _is_rotating_part(part) -> bool:
    """Heuristic classification of rotating/moving parts vs stationary frame/stator parts."""
    lbl = (getattr(part, "label", "") or "").lower()
    meta = getattr(part, "_cots_meta", None) or {}
    cat = (meta.get("category") or "").lower()
    title = (meta.get("title") or "").lower()
    sku = (meta.get("sku") or "").lower()

    # Stationary COTS stators (motors, servos, sensors, bearings)
    if cat in ("motors", "servos", "sensors", "electronics") or any(
        k in lbl or k in title or k in sku
        for k in ("motor", "servo", "sensor", "sense", "brg-", "bearing", "1611-", "1222-", "rev-21-1650", "rev-41-1300", "5203-")
    ):
        # Exception: bearing pillow block / flange outer body is stationary, whereas a turret/wrist yoke or wheel is rotating
        if not any(k in lbl for k in ("wheel", "pulley", "roller", "flywheel", "collar", "shaft", "yoke", "swingarm", "turret_plate")):
            return False

    if cat in ("wheels",) or any(
        k in lbl or k in title or k in sku
        for k in (
            "wheel",
            "pulley",
            "roller",
            "flywheel",
            "collar",
            "shaft",
            "axle",
            "gear",
            "sprocket",
            "swingarm",
            "fork",
            "yoke",
            "wrist",
        )
    ):
        return True
    return False


def export_assembly_constraints(
    parts: list,
    out_json_path: str | Path,
    stationary_parts: list[str] | None = None,
    revolute_specs: list[dict] | None = None,
    gear_relations: list[dict] | None = None,
    mate_groups: list[dict] | None = None,
) -> dict:
    """
    Automatically builds an Onshape-compatible `*_constraints.json` (`mate_groups`,
    `mate_connectors`, `mates`, `gear_relations`, and `onshape_cloud_instances`) from
    the assembly's `parts` and their world-space cylindrical bore/shaft datums.
    """
    out_file = Path(out_json_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    part_by_label = {}
    ordered_labels = []
    onshape_cloud_instances = []
    native_solidcraft_components = []
    for idx, p in enumerate(parts, 1):
        lbl = getattr(p, "label", None) or f"Part_{idx:02d}"
        part_by_label[lbl] = p
        ordered_labels.append(lbl)
        native_sc = getattr(p, "_native_sc_meta", None)
        if native_sc and isinstance(native_sc, dict):
            n_entry = dict(native_sc)
            n_entry["part_label"] = lbl
            native_solidcraft_components.append(n_entry)
        meta = getattr(p, "_cots_meta", None) or {}
        if meta.get("is_cots"):
            ins = meta.get("onshape_insertable") or {}
            onshape_cloud_instances.append(
                {
                    "part_label": lbl,
                    "vendor": meta.get("vendor"),
                    "sku": meta.get("sku"),
                    "title": meta.get("title"),
                    "category": meta.get("category"),
                    "solidcraft_path": meta.get("solidcraft_path"),
                    "solidcraft_material": meta.get("solidcraft_material"),
                    "solidcraft_world_matrix_4x4": meta.get("solidcraft_world_matrix_4x4"),
                    "placement_spec": meta.get("placement_spec"),
                    "world_bbox_center_mm": meta.get("world_bbox_center_mm"),
                    "library_id": ins.get("library_id"),
                    "insertable_name": ins.get("name"),
                    "documentId": ins.get("document_id"),
                    "versionId": ins.get("version_id"),
                    "elementId": ins.get("element_id"),
                    "elementType": ins.get("element_type"),
                    "onshape_url": ins.get("onshape_url"),
                    "world_axis_center_mm": meta.get("world_axis_center_mm"),
                    "world_bore_axis": meta.get("world_bore_axis"),
                }
            )

    if stationary_parts is None:
        if revolute_specs is not None:
            rot_set = {r["part"] for r in revolute_specs}
            stationary_parts = [lbl for lbl in ordered_labels if lbl not in rot_set]
        else:
            stationary_parts = [lbl for lbl in ordered_labels if not _is_rotating_part(part_by_label[lbl])]
        if not stationary_parts and ordered_labels:
            stationary_parts = [ordered_labels[0]]

    base_part = stationary_parts[0]
    if mate_groups is None:
        mate_groups = []
        if len(stationary_parts) > 1:
            mate_groups.append(
                {
                    "name": "RIGID | Stationary Frame & Stators",
                    "parts": list(stationary_parts),
                }
            )
    else:
        mate_groups = list(mate_groups)

    mate_connectors = [
        {
            "key": "mc_world",
            "name": "MC | World Frame Origin (0, 0, 0)",
            "part": "WORLD",
            "tx_mm": 0.0,
            "ty_mm": 0.0,
            "tz_mm": 0.0,
        },
        {
            "key": "mc_base_origin",
            "name": f"MC | {base_part} Ground Anchor (0, 0, 0)",
            "part": base_part,
            "tx_mm": 0.0,
            "ty_mm": 0.0,
            "tz_mm": 0.0,
        },
    ]
    mates = [
        {
            "key": "m_ground",
            "name": f"FASTENED | {base_part} to World Origin",
            "type": "FASTENED",
            "mc1": "mc_world",
            "mc2": "mc_base_origin",
        }
    ]

    if revolute_specs is None:
        revolute_specs = []
        for lbl in ordered_labels:
            if lbl in stationary_parts:
                continue
            p = part_by_label[lbl]
            meta = getattr(p, "_cots_meta", None)
            if meta and meta.get("world_axis_center_mm"):
                center = meta["world_axis_center_mm"]
                axis = meta.get("world_bore_axis", "Z")
                min_r = meta.get("min_bore_radius_mm")
            else:
                bb = p.bounding_box()
                bb_c = [
                    round(0.5 * (bb.min.X + bb.max.X), 3),
                    round(0.5 * (bb.min.Y + bb.max.Y), 3),
                    round(0.5 * (bb.min.Z + bb.max.Z), 3),
                ]
                ax_info = detect_shape_primary_axis(p) or {}
                axis = ax_info.get("primary_axis", "Z")
                raw_c = ax_info.get("axis_center_mm")
                if raw_c:
                    ax_idx = {"X": 0, "Y": 1, "Z": 2}.get(axis, 2)
                    center = list(raw_c)
                    center[ax_idx] = bb_c[ax_idx]
                else:
                    center = bb_c
                min_r = ax_info.get("min_bore_radius_mm")
            revolute_specs.append(
                {
                    "part": lbl,
                    "axis": axis,
                    "center_mm": [round(float(v), 3) for v in center],
                    "min_bore_radius_mm": min_r,
                }
            )

    auto_mates_info = []
    for idx, rspec in enumerate(revolute_specs, 1):
        plbl = rspec["part"]
        parent_lbl = rspec.get("parent_part") or base_part
        cx, cy, cz = rspec["center_mm"]
        ax = rspec.get("axis", "Z")
        slug = rspec.get("key_slug") or f"rot_{idx:02d}"
        mc_stat_key = f"mc_stator_{slug}"
        mc_part_key = f"mc_part_{slug}"
        mate_key = rspec.get("mate_key") or f"m_{slug}"

        mate_connectors.append(
            {
                "key": mc_stat_key,
                "name": f"MC | Stator Axis Anchor on {parent_lbl} for {plbl} ({ax} @ {cx:.1f},{cy:.1f},{cz:.1f})",
                "part": parent_lbl,
                "axis": ax,
                "tx_mm": cx,
                "ty_mm": cy,
                "tz_mm": cz,
            }
        )
        mate_connectors.append(
            {
                "key": mc_part_key,
                "name": f"MC | {plbl} Bore/Shaft Datum ({ax} @ {cx:.1f},{cy:.1f},{cz:.1f})",
                "part": plbl,
                "axis": ax,
                "tx_mm": cx,
                "ty_mm": cy,
                "tz_mm": cz,
            }
        )
        mate_entry = {
            "key": mate_key,
            "name": rspec.get("name") or f"REVOLUTE | {plbl} ({ax}-Axis)",
            "type": rspec.get("type", "REVOLUTE"),
            "mc1": mc_stat_key,
            "mc2": mc_part_key,
        }
        if rspec.get("limits") is not None:
            mate_entry["limits"] = rspec["limits"]
        mates.append(mate_entry)
        auto_mates_info.append(
            {
                "mate_key": mate_key,
                "part": plbl,
                "parent_part": parent_lbl,
                "axis": ax,
                "center_mm": (cx, cy, cz),
            }
        )

    if gear_relations is None:
        gear_relations = []
        if len(auto_mates_info) >= 2:
            lead = auto_mates_info[0]
            for follower in auto_mates_info[1:]:
                if follower["axis"] == lead["axis"]:
                    gear_relations.append(
                        {
                            "name": f"GEAR | Coupled Drive ({lead['part']} -> {follower['part']})",
                            "mate1": lead["mate_key"],
                            "mate2": follower["mate_key"],
                            "ratio": "1",
                            "reverse": False,
                        }
                    )

    payload = {
        "mate_groups": mate_groups,
        "mate_connectors": mate_connectors,
        "mates": mates,
        "gear_relations": gear_relations,
        "onshape_cloud_instances": onshape_cloud_instances,
        "native_solidcraft_components": native_solidcraft_components,
    }
    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def export_fabrication_files(parts: list, project_dir: str | Path) -> dict:
    """
    Exports custom (non-COTS) parts in `parts` to:
    - `<project_dir>/fabrication/prints/<label>.stl`
    - `<project_dir>/cad/parts/<label>.step`
    - `<project_dir>/fabrication/laser/<label>.dxf` (for flat plates with min thickness <= 8.5 mm)
    """
    from build123d import ExportDXF, Plane, export_step, export_stl, section

    proj_path = Path(project_dir)
    prints_dir = proj_path / "fabrication" / "prints"
    cad_parts_dir = proj_path / "cad" / "parts"
    laser_dir = proj_path / "fabrication" / "laser"
    prints_dir.mkdir(parents=True, exist_ok=True)
    cad_parts_dir.mkdir(parents=True, exist_ok=True)

    exported_prints = []
    exported_laser = []

    for idx, p in enumerate(parts, 1):
        meta = getattr(p, "_cots_meta", None)
        lbl = getattr(p, "label", None) or f"Custom_Part_{idx:02d}"
        lbl_low = lbl.lower()
        if (
            (meta and meta.get("is_cots"))
            or "_cots_" in lbl_low
            or "ref_gamepiece" in lbl_low
            or "_field_" in lbl_low
            or lbl_low.startswith("field_")
        ):
            continue
        safe_lbl = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in lbl)

        stl_path = prints_dir / f"{safe_lbl}.stl"
        legacy_print_step = prints_dir / f"{safe_lbl}.step"
        step_path = cad_parts_dir / f"{safe_lbl}.step"
        if stl_path.exists():
            try:
                stl_path.unlink()
            except OSError:
                pass
        if legacy_print_step.exists():
            try:
                legacy_print_step.unlink()
            except OSError:
                pass
        if step_path.exists():
            try:
                step_path.unlink()
            except OSError:
                pass
        export_stl(p, str(stl_path))
        buf = io.BytesIO()
        export_step(p, buf)
        step_path.write_bytes(buf.getvalue())
        exported_prints.append(str(stl_path))

        bb = p.bounding_box()
        dims = (
            bb.max.X - bb.min.X,
            bb.max.Y - bb.min.Y,
            bb.max.Z - bb.min.Z,
        )
        min_d = min(dims)
        max_d = max(dims)
        if min_d <= 8.5 and max_d >= 25.0:
            try:
                from build123d import Pos, Rot

                laser_dir.mkdir(parents=True, exist_ok=True)
                if dims[2] == min_d:
                    z_mid = 0.5 * (bb.min.Z + bb.max.Z)
                    sec = Pos(0.0, 0.0, -z_mid) * section(p, Plane.XY.offset(z_mid))
                elif dims[1] == min_d:
                    y_mid = 0.5 * (bb.min.Y + bb.max.Y)
                    sec = Rot(-90.0, 0.0, 0.0) * Pos(0.0, -y_mid, 0.0) * section(p, Plane.XZ.offset(y_mid))
                else:
                    x_mid = 0.5 * (bb.min.X + bb.max.X)
                    sec = Rot(0.0, -90.0, 0.0) * Pos(-x_mid, 0.0, 0.0) * section(p, Plane.YZ.offset(x_mid))
                dxf_path = laser_dir / f"{safe_lbl}.dxf"
                exporter = ExportDXF()
                exporter.add_shape(sec)
                exporter.write(str(dxf_path))
                exported_laser.append(str(dxf_path))
            except Exception:
                pass

    return {
        "prints": exported_prints,
        "laser_dxf": exported_laser,
    }


def export_step_assembly(parts: list, step_path: str | Path, label: str = "Assembly") -> str:
    """Builds a multi-body STEP AP214 `Compound` from `parts` and writes it to `step_path`."""
    from build123d import Compound, export_step
    from OCP.Interface import Interface_Static

    Interface_Static.SetIVal_s("write.step.pcurve.mode", 0)
    out = Path(step_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    assy = Compound(label=label, children=list(parts))
    export_step(assy, str(out))
    try:
        if out.stat().st_size > 480 * 1024 * 1024:
            raw_b = out.read_bytes()
            comp_b = raw_b.replace(b"\r\n", b"\n").replace(b"\n    ", b"\n").replace(b"\n  ", b"\n")
            out.write_bytes(comp_b)
    except Exception:
        pass
    return str(out)


def export_project_deliverables(
    parts: list,
    project_dir: str | Path,
    stem: str,
    assembly_title: str | None = None,
    stationary_parts: list[str] | None = None,
    revolute_specs: list[dict] | None = None,
    gear_relations: list[dict] | None = None,
    mate_groups: list[dict] | None = None,
    write_step: bool = True,
) -> dict:
    """
    Complete local-first deliverable pipeline for a COTS + Custom assembly:
    1. Multi-body STEP AP214 assembly (`<project_dir>/cad/<stem>.step`)
    2. Onshape assembly constraints JSON (`<project_dir>/cad/<stem>_constraints.json`)
    3. Pure-offline Onshape Session Bundle JSON (`<project_dir>/cad/<stem>_onshape_bundle.json`)
    4. Multi-Pose Kinematic Sweep Collision Audit (`-20°`, `0°`, `+20°`)
    5. Subsystem Mass, 3D CG, & Rotational Inertia ($J_{\text{drive}}$) + BOM (`<project_dir>/docs/<stem>_bom.md` & `.csv`)
    6. Custom 3D-print `.stl`/`.step` and 2D laser `.dxf` files (`<project_dir>/fabrication/`)
    """
    from tools.cad_pipeline import compile_onshape_browser_bundle

    proj_path = Path(project_dir)
    cad_dir = proj_path / "cad"
    docs_dir = proj_path / "docs"
    cad_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    step_path = cad_dir / f"{stem}.step"
    constraints_path = cad_dir / f"{stem}_constraints.json"
    bundle_path = cad_dir / f"{stem}_onshape_bundle.json"

    import os

    fab = export_fabrication_files(parts, proj_path)
    custom_only_env = os.environ.get("AG_CAD_CUSTOM_ONLY_STEP", "").strip() in ("1", "true", "yes")
    custom_brep_parts = [
        p
        for p in parts
        if not (getattr(p, "_cots_meta", None) or {}).get("is_cots")
        and not getattr(p, "_native_sc_meta", None)
    ]
    custom_step_path: Path | None = None
    if custom_brep_parts and len(custom_brep_parts) < len(parts):
        custom_step_path = cad_dir / f"{stem}_custom.step"
        export_step_assembly(custom_brep_parts, custom_step_path, label=f"{stem}_Custom")
    if write_step and not (custom_only_env and (custom_step_path is not None or not custom_brep_parts)):
        export_step_assembly(parts, step_path, label=f"{stem}_Assy")
    constraints = export_assembly_constraints(
        parts,
        constraints_path,
        stationary_parts=stationary_parts,
        revolute_specs=revolute_specs,
        gear_relations=gear_relations,
        mate_groups=mate_groups,
    )
    if custom_step_path is not None:
        constraints["custom_step_path"] = str(custom_step_path.as_posix())
        constraints_path.write_text(json.dumps(constraints, indent=2), encoding="utf-8")
    bundle = compile_onshape_browser_bundle(
        constraints_path=constraints_path,
        out_bundle_path=bundle_path,
        onshape_cloud_instances=constraints.get("onshape_cloud_instances"),
    )
    mass_props = compute_assembly_mass_properties(parts)
    kinematic_sweep = verify_kinematic_sweep(parts, constraints)
    physics_audit = audit_mechanism_physics(parts, constraints, kinematic_sweep=kinematic_sweep)
    bom = export_cots_bom(
        parts,
        docs_dir,
        stem,
        assembly_title=assembly_title,
        mass_properties=mass_props,
        kinematic_sweep=kinematic_sweep,
    )

    return {
        "step_path": str(step_path),
        "constraints_path": str(constraints_path),
        "onshape_bundle_path": str(bundle_path),
        "mate_connectors_count": len(constraints["mate_connectors"]),
        "mates_count": len(constraints["mates"]),
        "onshape_bundle_steps": bundle["total_steps"],
        "onshape_cloud_instances_count": len(constraints.get("onshape_cloud_instances", [])),
        "mass_properties": mass_props,
        "kinematic_sweep": kinematic_sweep,
        "physics_audit": physics_audit,
        "bom": bom,
        "fabrication": fab,
    }


def _make_axis_cylinder(axis: str, u: float, v: float, span_min: float, span_max: float, radius: float):
    """Creates a `build123d` `Cylinder` aligned with `axis` ('X', 'Y', or 'Z') at 2D coordinates `(u, v)`."""
    from build123d import Align, Cylinder, Pos, Rot

    length = (span_max - span_min) + 4.0
    mid = 0.5 * (span_min + span_max)
    cyl = Cylinder(radius=radius, height=length, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    if axis == "Z":
        return Pos(u, v, mid) * cyl
    if axis == "Y":
        return Pos(u, mid, v) * Rot(90.0, 0.0, 0.0) * cyl
    return Pos(mid, u, v) * Rot(0.0, 90.0, 0.0) * cyl


def cut_cots_mounting_holes(
    plate,
    cots_parts,
    cut_central_bore: bool = True,
    bore_radial_clearance_mm: float = 1.0,
    min_central_bore_radius_mm: float | None = None,
    cut_fastener_holes: bool = True,
    fastener_radial_clearance_mm: float = 0.20,
    max_bolt_circle_radius_mm: float | None = None,
    max_holes_per_part: int | None = None,
):
    """
    Automatically drills the central shaft/boss clearance bore (`min_bore_radius_mm`)
    and M3/M4/#10-32 mounting bolt pattern (`mounting_holes`) of one or more placed
    COTS parts (`load_cots_part(...)`) directly through a custom `build123d` `plate`
    or bracket, skipping any holes that fall outside the plate's bounding box.
    """
    from build123d import Compound

    if not isinstance(cots_parts, (list, tuple)):
        cots_parts = [cots_parts]

    orig_label = getattr(plate, "label", None)
    orig_color = getattr(plate, "color", None)
    bb = plate.bounding_box()
    bmin = {"X": bb.min.X, "Y": bb.min.Y, "Z": bb.min.Z}
    bmax = {"X": bb.max.X, "Y": bb.max.Y, "Z": bb.max.Z}

    def _uv_axes(ax: str) -> tuple[str, str]:
        if ax == "Z":
            return ("X", "Y")
        if ax == "Y":
            return ("X", "Z")
        return ("Y", "Z")

    cutters = []
    for cp in cots_parts:
        meta = getattr(cp, "_cots_meta", None)
        if not meta:
            ax_info = detect_shape_primary_axis(cp) or {}
            bore_ax = ax_info.get("primary_axis", "Z")
            ax_center = ax_info.get("axis_center_mm", [0.0, 0.0, 0.0])
            min_bore_r = ax_info.get("min_bore_radius_mm")
            mh = json.loads(ax_info["mounting_holes_json"]) if ax_info.get("mounting_holes_json") else None
        else:
            bore_ax = meta.get("world_bore_axis", "Z")
            ax_center = meta.get("world_axis_center_mm", [0.0, 0.0, 0.0])
            min_bore_r = meta.get("min_bore_radius_mm")
            mh = meta.get("mounting_holes")

        u_ax, v_ax = _uv_axes(bore_ax)
        idx_map = {"X": 0, "Y": 1, "Z": 2}
        u0 = float(ax_center[idx_map[u_ax]])
        v0 = float(ax_center[idx_map[v_ax]])

        if cut_central_bore and min_bore_r is not None:
            r_bore = max(float(min_bore_r) + bore_radial_clearance_mm, float(min_central_bore_radius_mm or 0.0))
            if (bmin[u_ax] + r_bore <= u0 <= bmax[u_ax] - r_bore) and (
                bmin[v_ax] + r_bore <= v0 <= bmax[v_ax] - r_bore
            ):
                cutters.append(_make_axis_cylinder(bore_ax, u0, v0, bmin[bore_ax], bmax[bore_ax], r_bore))

        if cut_fastener_holes and mh and mh.get("centers_2d_mm"):
            hole_ax = mh.get("axis", bore_ax)
            hu_ax, hv_ax = _uv_axes(hole_ax)
            r_hole = float(mh.get("hole_radius_mm", 2.0)) + fastener_radial_clearance_mm
            added = 0
            for c2d in mh["centers_2d_mm"]:
                hu, hv = float(c2d[0]), float(c2d[1])
                if hole_ax == bore_ax and max_bolt_circle_radius_mm is not None:
                    if math.hypot(hu - u0, hv - v0) > max_bolt_circle_radius_mm + 0.5:
                        continue
                margin = r_hole + 0.8
                if (bmin[hu_ax] + margin <= hu <= bmax[hu_ax] - margin) and (
                    bmin[hv_ax] + margin <= hv <= bmax[hv_ax] - margin
                ):
                    cutters.append(_make_axis_cylinder(hole_ax, hu, hv, bmin[hole_ax], bmax[hole_ax], r_hole))
                    added += 1
                    if max_holes_per_part is not None and added >= max_holes_per_part:
                        break

    if cutters:
        plate = plate - Compound(children=cutters)
        if orig_label:
            plate.label = orig_label
        if orig_color is not None:
            plate.color = orig_color
    return plate


# ---------------------------------------------------------------------------
# Milestone 2: Parametric Mating & Axial Stackup Macros
# ---------------------------------------------------------------------------


def build_tangent_belt_or_chain(
    center1_2d: tuple[float, float],
    r1_mm: float,
    center2_2d: tuple[float, float],
    r2_mm: float,
    width_mm: float = 9.0,
    thickness_mm: float = 1.8,
    inner_clearance_mm: float = 0.25,
    plane: str = "XY",
    offset_mm: float = 0.0,
    label: str = "Drive_Belt_Loop",
    color: str = "#1A1D20",
):
    """
    Generates an exact closed tangent belt or chain loop around two pulleys/sprockets
    at 2D coordinates `center1_2d` (pitch/outer radius `r1_mm`) and `center2_2d`
    (pitch/outer radius `r2_mm`) on `plane` ('XY', 'XZ', or 'YZ') offset by `offset_mm`.
    """
    from build123d import BuildPart, BuildSketch, Circle, Color, Locations, Mode, Plane, extrude, make_hull

    pl_map = {"XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}
    work_plane = pl_map.get(plane.upper(), Plane.XY).offset(float(offset_mm))
    r1_in = float(r1_mm) + float(inner_clearance_mm)
    r2_in = float(r2_mm) + float(inner_clearance_mm)
    r1_out = r1_in + float(thickness_mm)
    r2_out = r2_in + float(thickness_mm)

    with BuildPart() as belt_bp:
        with BuildSketch(work_plane):
            with Locations(center1_2d):
                Circle(r1_out)
            with Locations(center2_2d):
                Circle(r2_out)
            make_hull()
            with BuildSketch(work_plane, mode=Mode.SUBTRACT):
                with Locations(center1_2d):
                    Circle(r1_in)
                with Locations(center2_2d):
                    Circle(r2_in)
                make_hull()
        extrude(amount=float(width_mm) * 0.5, both=True)

    belt = belt_bp.part
    belt.label = label
    belt.color = Color(color)
    return belt


def build_meshed_spur_gear_pair(
    center1_3d: tuple[float, float, float],
    teeth1: int,
    teeth2: int,
    module_mm: float = 1.5,
    width_mm: float = 8.0,
    bore_hex_af_mm: float = 12.7,
    axis: str = "Y",
    mesh_angle_deg: float = 0.0,
    radial_clearance_mm: float = 0.25,
    label1: str = "Spur_Gear_1",
    label2: str = "Spur_Gear_2",
    color1: str = "#B87333",
    color2: str = "#8C92AC",
) -> dict:
    """
    Constructs a tangent, interference-free meshed spur gear pair with pitch radii
    `Rp1 = 0.5 * module_mm * teeth1` and `Rp2 = 0.5 * module_mm * teeth2`, exact
    center-to-center distance `C = Rp1 + Rp2`, hex bores, and lightening web pockets.
    Returns `{"gear1": g1, "gear2": g2, "center1_mm": ..., "center2_mm": ..., "center_distance_mm": C, "ratio": ...}`.
    """
    from build123d import Align, BuildPart, BuildSketch, Circle, Color, Cylinder, Locations, Plane, PolarLocations, Pos, RegularPolygon, extrude

    rp1 = 0.5 * float(module_mm) * int(teeth1)
    rp2 = 0.5 * float(module_mm) * int(teeth2)
    c_dist = rp1 + rp2
    ro1 = rp1 - 0.5 * float(radial_clearance_mm)
    ro2 = rp2 - 0.5 * float(radial_clearance_mm)
    ang_rad = math.radians(float(mesh_angle_deg))

    ax = axis.upper()
    x1, y1, z1 = [float(v) for v in center1_3d]
    if ax == "Z":
        x2 = x1 + c_dist * math.cos(ang_rad)
        y2 = y1 + c_dist * math.sin(ang_rad)
        z2 = z1
        work_plane = Plane.XY
    elif ax == "Y":
        x2 = x1 + c_dist * math.cos(ang_rad)
        y2 = y1
        z2 = z1 + c_dist * math.sin(ang_rad)
        work_plane = Plane.XZ
    else:
        x2 = x1
        y2 = y1 + c_dist * math.cos(ang_rad)
        z2 = z1 + c_dist * math.sin(ang_rad)
        work_plane = Plane.YZ

    def _build_single_gear(cx, cy, cz, r_outer, lbl, col):
        if ax == "Z":
            pl = Plane.XY.offset(cz)
            loc2d = (cx, cy)
        elif ax == "Y":
            pl = Plane.XZ.offset(cy)
            loc2d = (cx, cz)
        else:
            pl = Plane.YZ.offset(cx)
            loc2d = (cy, cz)

        with BuildPart() as gbp:
            with BuildSketch(pl):
                with Locations(loc2d):
                    Circle(r_outer)
            extrude(amount=0.5 * float(width_mm), both=True)
        g = gbp.part
        if bore_hex_af_mm and bore_hex_af_mm > 0.0:
            with BuildPart() as hex_bp:
                with BuildSketch(pl):
                    with Locations(loc2d):
                        RegularPolygon(radius=hex_circumradius(float(bore_hex_af_mm)), side_count=6)
                extrude(amount=float(width_mm), both=True)
            g = g - hex_bp.part
        if r_outer >= 18.0:
            with BuildPart() as web_bp:
                with BuildSketch(pl):
                    with Locations(loc2d):
                        with PolarLocations(0.56 * r_outer, 6):
                            Circle(0.16 * r_outer)
                extrude(amount=float(width_mm), both=True)
            g = g - web_bp.part
        g.label = lbl
        g.color = Color(col)
        return g

    g1 = _build_single_gear(x1, y1, z1, ro1, label1, color1)
    g2 = _build_single_gear(x2, y2, z2, ro2, label2, color2)
    return {
        "gear1": g1,
        "gear2": g2,
        "center1_mm": [round(x1, 3), round(y1, 3), round(z1, 3)],
        "center2_mm": [round(x2, 3), round(y2, 3), round(z2, 3)],
        "center_distance_mm": round(c_dist, 3),
        "pitch_radius1_mm": round(rp1, 3),
        "pitch_radius2_mm": round(rp2, 3),
        "ratio": f"{teeth2}/{teeth1}",
    }


def build_double_shear_shaft_assembly(
    axis: str,
    u_mm: float,
    v_mm: float,
    span_min_mm: float,
    span_max_mm: float,
    hex_af_mm: float = 12.7,
    collar_sku: str | None = "REV-21-1432",
    collar_positions_mm: tuple[float, float] | None = None,
    shaft_overhang_mm: float = 8.0,
    shaft_label: str = "Hex_Drive_Shaft",
    shaft_color: str = "#C5CBD3",
) -> dict:
    """
    Constructs a double-shear hexagonal drive shaft along `axis` ('X', 'Y', or 'Z')
    at 2D coordinates `(u_mm, v_mm)` spanning `[span_min_mm - shaft_overhang_mm, span_max_mm + shaft_overhang_mm]`,
    plus optional COTS clamping collars at `collar_positions_mm`.
    """
    from build123d import BuildPart, BuildSketch, Color, Locations, Plane, RegularPolygon, extrude

    ax = axis.upper()
    s_min = float(span_min_mm) - float(shaft_overhang_mm)
    s_max = float(span_max_mm) + float(shaft_overhang_mm)
    s_mid = 0.5 * (s_min + s_max)
    s_len = s_max - s_min

    if ax == "Z":
        pl = Plane.XY.offset(s_mid)
        loc2d = (float(u_mm), float(v_mm))
    elif ax == "Y":
        pl = Plane.XZ.offset(s_mid)
        loc2d = (float(u_mm), float(v_mm))
    else:
        pl = Plane.YZ.offset(s_mid)
        loc2d = (float(u_mm), float(v_mm))

    with BuildPart() as sbp:
        with BuildSketch(pl):
            with Locations(loc2d):
                RegularPolygon(radius=hex_circumradius(float(hex_af_mm)), side_count=6)
        extrude(amount=0.5 * s_len, both=True)
    shaft = sbp.part
    shaft.label = shaft_label
    shaft.color = Color(shaft_color)

    collars = []
    if collar_sku and collar_positions_mm:
        for idx, cpos in enumerate(collar_positions_mm, 1):
            if ax == "Z":
                at_pt = (float(u_mm), float(v_mm), float(cpos))
                al = ("axis", "axis", "center")
            elif ax == "Y":
                at_pt = (float(u_mm), float(cpos), float(v_mm))
                al = ("axis", "center", "axis")
            else:
                at_pt = (float(cpos), float(u_mm), float(v_mm))
                al = ("center", "axis", "axis")
            col = load_cots_part(
                collar_sku,
                label=f"{shaft_label}_Collar_{idx:02d}_{collar_sku}",
                bore_axis=ax,
                align=al,
                at=at_pt,
                rgb=(42, 46, 52),
            )
            collars.append(col)

    return {
        "shaft": shaft,
        "collars": collars,
        "parts": [shaft] + collars,
        "axis": ax,
        "span_mm": [round(s_min, 3), round(s_max, 3)],
    }


# ---------------------------------------------------------------------------
# Milestone 3: Automated Mechanical Sanity & Physics Linter
# ---------------------------------------------------------------------------


def _shape_min_distance_mm(s1, s2) -> float:
    """Computes 3D minimum distance in mm between two build123d shapes, falling back to fast AABB distance for heavy COTS B-Reps."""
    bb1 = s1.bounding_box()
    bb2 = s2.bounding_box()
    dx = max(0.0, max(bb1.min.X - bb2.max.X, bb2.min.X - bb1.max.X))
    dy = max(0.0, max(bb1.min.Y - bb2.max.Y, bb2.min.Y - bb1.max.Y))
    dz = max(0.0, max(bb1.min.Z - bb2.max.Z, bb2.min.Z - bb1.max.Z))
    aabb_dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if aabb_dist > 5.0:
        return aabb_dist
    if len(s1.faces()) * len(s2.faces()) > 1200:
        return aabb_dist

    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    dss = BRepExtrema_DistShapeShape(s1.wrapped, s2.wrapped)
    dss.Perform()
    if dss.IsDone() and dss.NbSolution() > 0:
        return float(dss.Value())
    return aabb_dist


def _is_game_piece_label(lbl: str) -> bool:
    ll = (lbl or "").lower()
    if any(
        k in ll
        for k in (
            "hood",
            "bearing",
            "guide",
            "shoe",
            "trough",
            "funnel",
            "chute",
            "gate",
            "plate",
            "mount",
            "bracket",
            "frame",
            "wheel",
            "roller",
            "motor",
            "servo",
            "screw",
        )
    ):
        return False
    return any(
        k in ll
        for k in ("game_piece", "ref_gamepiece", "ball", "cargo", "artifact", "note_", "pixel", "cone", "cube", "sample", "specimen")
    )


def _is_actuator_part(p, lbl: str) -> bool:
    ll = (lbl or "").lower()
    if any(
        k in ll
        for k in (
            "mount",
            "bracket",
            "plate",
            "horn",
            "hub",
            "spacer",
            "standoff",
            "shaft",
            "axle",
            "collar",
            "pulley",
            "gear",
            "wheel",
            "roller",
            "fork",
            "yoke",
            "swingarm",
            "bridge",
            "pedestal",
            "coupler",
            "adapter",
            "retainer",
        )
    ):
        return False
    meta = getattr(p, "_cots_meta", None) or {}
    cat = (meta.get("category") or "").lower()
    if cat in ("motors", "servos"):
        return True
    return any(k in ll for k in ("motor", "servo", "neo_", "vortex", "kraken", "falcon", "5203-", "sr-servo"))


def audit_mechanism_physics(
    parts: list,
    constraints: dict | None = None,
    support_gap_tol_mm: float = 3.5,
    actuator_radial_tol_mm: float = 4.5,
    actuator_axial_gap_tol_mm: float = 18.0,
    kinematic_sweep: dict | None = None,
) -> dict:
    """
    Automated Mechanical Sanity & Physics Linter (`Milestone 3`).
    Executes 4 domain-specific 3D B-Rep physics checks:
    1. Structural Support Connectivity (`FLOATING_PART_ERROR`)
    2. Actuator-to-Rotor Coaxial Coupling (`UNCOUPLED_ACTUATOR_ERROR`)
    3. Game-Piece Swept Tunnel, Active Wheel Pinch & Coaxial Gear OD (`BLOCKED_BALL_PATH_ERROR`, `NON_CONTACTING_WHEEL_ERROR`, `GEAR_EXCEEDS_WHEEL_OD_ERROR`)
    4. Multi-Pose Revolute Sweep Collision Check (`REVOLUTE_SWEEP_COLLISION_ERROR`)
    """
    from collections import deque

    if constraints is None:
        # Build lightweight in-memory constraints if not supplied
        part_by_lbl = {}
        ordered_lbls = []
        for idx, p in enumerate(parts, 1):
            l = getattr(p, "label", None) or f"Part_{idx:02d}"
            part_by_lbl[l] = p
            ordered_lbls.append(l)
        stat_parts = [l for l in ordered_lbls if not _is_rotating_part(part_by_lbl[l])]
        if not stat_parts and ordered_lbls:
            stat_parts = [ordered_lbls[0]]
        constraints = {
            "mate_groups": [{"parts": stat_parts}] if stat_parts else [],
            "mate_connectors": [],
            "mates": [],
        }

    errors = []
    warnings = []
    checks_summary = {}

    labeled_parts = []
    for idx, p in enumerate(parts, 1):
        lbl = getattr(p, "label", None) or f"Part_{idx:02d}"
        labeled_parts.append((lbl, p))

    # Separate mechanical parts from reference game pieces and external field elements
    def _is_field_ref(l_str: str) -> bool:
        ll = (l_str or "").lower()
        return "_field_" in ll or ll.startswith("field_")

    mech_parts = [(lbl, p) for lbl, p in labeled_parts if not _is_game_piece_label(lbl) and not _is_field_ref(lbl)]
    game_pieces = [(lbl, p) for lbl, p in labeled_parts if _is_game_piece_label(lbl) and not _is_field_ref(lbl)]

    # ------------------------------------------------------------------
    # Check 1: Structural Support Graph (`FLOATING_PART_ERROR`)
    # ------------------------------------------------------------------
    n_mech = len(mech_parts)
    adj = {i: set() for i in range(n_mech)}
    bbs = [p.bounding_box() for _, p in mech_parts]

    for i in range(n_mech):
        b1 = bbs[i]
        for j in range(i + 1, n_mech):
            b2 = bbs[j]
            if (
                b1.max.X + support_gap_tol_mm < b2.min.X
                or b2.max.X + support_gap_tol_mm < b1.min.X
                or b1.max.Y + support_gap_tol_mm < b2.min.Y
                or b2.max.Y + support_gap_tol_mm < b1.min.Y
                or b1.max.Z + support_gap_tol_mm < b2.min.Z
                or b2.max.Z + support_gap_tol_mm < b1.min.Z
            ):
                continue
            if len(mech_parts) > 25 or len(mech_parts[i][1].faces()) > 250 or len(mech_parts[j][1].faces()) > 250:
                adj[i].add(j)
                adj[j].add(i)
                continue
            dist = _shape_min_distance_mm(mech_parts[i][1], mech_parts[j][1])
            if dist <= support_gap_tol_mm:
                adj[i].add(j)
                adj[j].add(i)

    floating_parts = []
    if n_mech > 0:
        stat_set = set()
        for mg in constraints.get("mate_groups", []):
            stat_set.update(mg.get("parts", []))
        root_idx = 0
        for i, (lbl, _) in enumerate(mech_parts):
            if lbl in stat_set:
                root_idx = i
                break
        visited = {root_idx}
        q = deque([root_idx])
        while q:
            curr = q.popleft()
            for nxt in adj[curr]:
                if nxt not in visited:
                    visited.add(nxt)
                    q.append(nxt)
        for i, (lbl, p) in enumerate(mech_parts):
            if i not in visited:
                min_d = min(
                    (_shape_min_distance_mm(p, mech_parts[j][1]) for j in visited),
                    default=999.0,
                )
                floating_parts.append({"part": lbl, "min_gap_to_grounded_mm": round(min_d, 3)})
                errors.append(
                    {
                        "code": "FLOATING_PART_ERROR",
                        "part": lbl,
                        "message": f"Part '{lbl}' is structurally disconnected from the grounded assembly (nearest gap = {min_d:.2f} mm > {support_gap_tol_mm:.1f} mm).",
                    }
                )
    checks_summary["structural_connectivity"] = {
        "status": "PASS" if not floating_parts else "FAIL",
        "mechanical_part_count": n_mech,
        "floating_parts": floating_parts,
    }

    # ------------------------------------------------------------------
    # Check 2: Actuator-to-Rotor Coaxial Coupling (`UNCOUPLED_ACTUATOR_ERROR`)
    # ------------------------------------------------------------------
    actuators = [(lbl, p) for lbl, p in mech_parts if _is_actuator_part(p, lbl)]
    rotor_candidates = []
    for lbl, p in mech_parts:
        if _is_actuator_part(p, lbl):
            continue
        ll = lbl.lower()
        if _is_rotating_part(p) or any(
            k in ll
            for k in ("pulley", "gear", "sprocket", "shaft", "axle", "roller", "wheel", "flywheel", "horn", "hub", "fork", "yoke", "wrist", "swingarm", "turret", "coupler", "adapter")
        ):
            frames = get_cots_functional_frames(p, sku_or_query=lbl)
            bc = frames["bore_center"]
            rotor_candidates.append((lbl, p, bc["origin_mm"], bc["axis"]))

    uncoupled_actuators = []
    coupled_actuators = []
    for act_lbl, act_p in actuators:
        meta = getattr(act_p, "_cots_meta", None) or {}
        act_frames = meta.get("functional_frames") or get_cots_functional_frames(act_p, sku_or_query=meta.get("sku") or act_lbl)
        out_frame = act_frames.get("output_shaft") or act_frames["bore_center"]
        out_pt = out_frame["origin_mm"]
        out_ax = out_frame.get("axis", "Z")
        uv_indices = [i for i, a in enumerate(("X", "Y", "Z")) if a != out_ax]

        best_rotor = None
        best_radial_err = 999.0
        act_bb = act_p.bounding_box()
        act_bmin = [act_bb.min.X, act_bb.min.Y, act_bb.min.Z]
        act_bmax = [act_bb.max.X, act_bb.max.Y, act_bb.max.Z]
        act_faces_n = len(act_p.faces())
        for r_lbl, r_p, r_pt, _ in rotor_candidates:
            r_bb = r_p.bounding_box()
            r_bmin = [r_bb.min.X, r_bb.min.Y, r_bb.min.Z]
            r_bmax = [r_bb.max.X, r_bb.max.Y, r_bb.max.Z]
            r_bb_c = [0.5 * (r_bmin[k] + r_bmax[k]) for k in range(3)]
            du_axis = float(r_pt[uv_indices[0]]) - float(out_pt[uv_indices[0]])
            dv_axis = float(r_pt[uv_indices[1]]) - float(out_pt[uv_indices[1]])
            rad_err_axis = math.hypot(du_axis, dv_axis)
            du_bb = float(r_bb_c[uv_indices[0]]) - float(out_pt[uv_indices[0]])
            dv_bb = float(r_bb_c[uv_indices[1]]) - float(out_pt[uv_indices[1]])
            rad_err_bb = math.hypot(du_bb, dv_bb)
            rad_err = min(rad_err_axis, rad_err_bb)
            if rad_err >= best_radial_err:
                continue

            # Fast 3D bounding-box gap pre-filter before expensive OCCT BRepExtrema
            bbox_gap_sq = 0.0
            for k in range(3):
                g_k = max(0.0, act_bmin[k] - r_bmax[k], r_bmin[k] - act_bmax[k])
                bbox_gap_sq += g_k * g_k
            bbox_dist_3d = math.sqrt(bbox_gap_sq)
            if bbox_dist_3d > actuator_axial_gap_tol_mm:
                continue

            if act_faces_n + len(r_p.faces()) > 200:
                dist_3d = bbox_dist_3d
            else:
                dist_3d = _shape_min_distance_mm(act_p, r_p)
            if dist_3d <= actuator_axial_gap_tol_mm and rad_err < best_radial_err:
                best_radial_err = rad_err
                best_rotor = r_lbl

        if best_rotor is None or best_radial_err > actuator_radial_tol_mm:
            uncoupled_actuators.append(
                {
                    "actuator": act_lbl,
                    "output_shaft_mm": out_pt,
                    "output_axis": out_ax,
                    "nearest_rotor": best_rotor,
                    "radial_misalignment_mm": round(best_radial_err, 3),
                }
            )
            errors.append(
                {
                    "code": "UNCOUPLED_ACTUATOR_ERROR",
                    "part": act_lbl,
                    "message": (
                        f"Actuator '{act_lbl}' output shaft at {out_pt} ({out_ax}-axis) is not coaxially coupled to any driven rotor/shaft "
                        f"(nearest rotor '{best_rotor}' radial misalignment = {best_radial_err:.2f} mm > {actuator_radial_tol_mm:.1f} mm)."
                    ),
                }
            )
        else:
            coupled_actuators.append(
                {
                    "actuator": act_lbl,
                    "coupled_to": best_rotor,
                    "output_shaft_mm": out_pt,
                    "radial_error_mm": round(best_radial_err, 3),
                }
            )
    checks_summary["actuator_coupling"] = {
        "status": "PASS" if not uncoupled_actuators else "FAIL",
        "coupled": coupled_actuators,
        "uncoupled": uncoupled_actuators,
    }

    # ------------------------------------------------------------------
    # Check 3: Game-Piece Path, Wheel Contact & Coaxial Gear OD Clearance
    # ------------------------------------------------------------------
    ball_issues = []
    if game_pieces:
        from build123d import Align, Cylinder, Pos, Rot

        for gp_lbl, gp_solid in game_pieces:
            gbb = gp_solid.bounding_box()
            gx = 0.5 * (gbb.min.X + gbb.max.X)
            gy = 0.5 * (gbb.min.Y + gbb.max.Y)
            gz = 0.5 * (gbb.min.Z + gbb.max.Z)
            r_ball = 0.5 * min(gbb.max.X - gbb.min.X, gbb.max.Y - gbb.min.Y, gbb.max.Z - gbb.min.Z)

            # Determine active wheels in the assembly (excluding shafts, axles, plates, gears)
            wheels = [
                (lbl, p)
                for lbl, p in mech_parts
                if any(k in lbl.lower() for k in ("wheel", "flywheel", "roller", "compliant"))
                and not any(
                    k in lbl.lower()
                    for k in ("plate", "mount", "bracket", "hood", "shaft", "axle", "gear", "pulley", "sprocket", "collar", "spacer", "standoff")
                )
            ]
            # Infer feed tunnel direction from wheel shaft axes:
            # - If wheels rotate about Z (vertical shafts at +/-X), the ball feeds along Y.
            # - Otherwise (horizontal shafts along Y), the ball feeds along X.
            z_axis_wheels = 0
            for w_lbl, w_p in wheels:
                w_ax = (get_cots_functional_frames(w_p, sku_or_query=w_lbl)["bore_center"]["axis"]) or "Z"
                if w_ax == "Z":
                    z_axis_wheels += 1
            feed_along_y = z_axis_wheels > len(wheels) // 2
            tunnel_rot = Rot(90.0, 0.0, 0.0) if feed_along_y else Rot(0.0, 90.0, 0.0)

            core_r = r_ball * 0.84
            tunnel = Pos(gx, gy, gz) * tunnel_rot * Cylinder(
                radius=core_r,
                height=r_ball * 2.1,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
            )
            outer_tunnel = Pos(gx, gy, gz) * tunnel_rot * Cylinder(
                radius=r_ball + 3.0,
                height=r_ball * 2.2,
                align=(Align.CENTER, Align.CENTER, Align.CENTER),
            )

            # Check active wheels contact the ball or its feed tunnel
            for w_lbl, w_p in wheels:
                d_w = min(_shape_min_distance_mm(w_p, gp_solid), _shape_min_distance_mm(w_p, outer_tunnel))
                if d_w > 4.0:
                    ball_issues.append({"type": "NON_CONTACTING_WHEEL_ERROR", "part": w_lbl, "gap_mm": round(d_w, 3)})
                    errors.append(
                        {
                            "code": "NON_CONTACTING_WHEEL_ERROR",
                            "part": w_lbl,
                            "message": f"Active wheel '{w_lbl}' does not contact game piece '{gp_lbl}' (gap = {d_w:.2f} mm > 4.0 mm).",
                        }
                    )

            # Check stationary crossbars/standoffs/plates do not block the inner swept ball core tunnel
            for s_lbl, s_p in mech_parts:
                sl = s_lbl.lower()
                if any(
                    k in sl
                    for k in (
                        "wheel",
                        "flywheel",
                        "roller",
                        "compliant",
                        "hood",
                        "guide",
                        "ramp",
                        "cradle",
                        "trough",
                        "funnel",
                        "chute",
                        "shoe",
                        "gate",
                    )
                ):
                    continue
                if len(s_p.faces()) > 250:
                    continue
                if _shape_min_distance_mm(s_p, tunnel) <= 0.01:
                    try:
                        common = s_p.intersect(tunnel)
                        vol = common.volume if common else 0.0
                    except Exception:
                        vol = 10.0
                    if vol > 5.0:
                        ball_issues.append({"type": "BLOCKED_BALL_PATH_ERROR", "part": s_lbl, "blocked_volume_mm3": round(vol, 2)})
                        errors.append(
                            {
                                "code": "BLOCKED_BALL_PATH_ERROR",
                                "part": s_lbl,
                                "message": f"Structural part '{s_lbl}' blocks the swept transit tunnel of '{gp_lbl}' (interference = {vol:.1f} mm³).",
                            }
                        )

    # Check coaxial gear/pulley/collar OD vs intake/contact roller wheel OD
    is_contact_roller_assy = bool(game_pieces) or any(
        any(k in lbl.lower() for k in ("intake", "roller", "shooter", "flywheel"))
        for lbl, _ in mech_parts
    )
    if is_contact_roller_assy:
        wheel_infos = []
        gear_infos = []
        for lbl, p in mech_parts:
            ll = lbl.lower()
            frames = get_cots_functional_frames(p, sku_or_query=lbl)
            bc = frames["bore_center"]
            ax = bc["axis"]
            pt = bc["origin_mm"]
            bb = p.bounding_box()
            spans = {"X": bb.max.X - bb.min.X, "Y": bb.max.Y - bb.min.Y, "Z": bb.max.Z - bb.min.Z}
            rad_axes = [a for a in ("X", "Y", "Z") if a != ax]
            r_outer = 0.25 * (spans[rad_axes[0]] + spans[rad_axes[1]])
            if any(k in ll for k in ("wheel", "roller", "flywheel", "compliant")) and not any(
                k in ll for k in ("gear", "pulley", "sprocket", "collar", "shaft", "axle", "plate", "mount", "bracket", "hood")
            ):
                wheel_infos.append((lbl, ax, pt, r_outer))
            elif any(k in ll for k in ("gear", "pulley", "sprocket")) and not any(
                k in ll for k in ("plate", "mount", "bracket", "shaft", "axle")
            ):
                gear_infos.append((lbl, ax, pt, r_outer))

        for g_lbl, g_ax, g_pt, g_r in gear_infos:
            uv_idx = [i for i, a in enumerate(("X", "Y", "Z")) if a != g_ax]
            for w_lbl, w_ax, w_pt, w_r in wheel_infos:
                if g_ax != w_ax:
                    continue
                dist_2d = math.hypot(float(g_pt[uv_idx[0]]) - float(w_pt[uv_idx[0]]), float(g_pt[uv_idx[1]]) - float(w_pt[uv_idx[1]]))
                if dist_2d <= 3.5 and g_r > w_r - 1.5:
                    ball_issues.append(
                        {
                            "type": "GEAR_EXCEEDS_WHEEL_OD_ERROR",
                            "gear": g_lbl,
                            "wheel": w_lbl,
                            "gear_radius_mm": round(g_r, 2),
                            "wheel_radius_mm": round(w_r, 2),
                        }
                    )
                    errors.append(
                        {
                            "code": "GEAR_EXCEEDS_WHEEL_OD_ERROR",
                            "part": g_lbl,
                            "message": (
                                f"Coaxial transmission part '{g_lbl}' (R={g_r:.1f} mm) exceeds or matches the contact wheel "
                                f"'{w_lbl}' outer radius (R={w_r:.1f} mm), preventing game-piece contact."
                            ),
                        }
                    )
                    break

    checks_summary["game_piece_and_wheel_envelope"] = {
        "status": "PASS" if not ball_issues else "FAIL",
        "issues": ball_issues,
    }

    # ------------------------------------------------------------------
    # Check 4: Multi-Pose Revolute Sweep Collision Check
    # ------------------------------------------------------------------
    sweep_res = kinematic_sweep if kinematic_sweep is not None else verify_kinematic_sweep(parts, constraints)
    if sweep_res.get("status") != "PASS":
        for pose in sweep_res.get("poses", []):
            for ov in pose.get("overlaps", []):
                errors.append(
                    {
                        "code": "REVOLUTE_SWEEP_COLLISION_ERROR",
                        "part": f"{ov['part_a']} vs {ov['part_b']}",
                        "message": f"Revolute sweep collision at {pose['angle_deg']}° between '{ov['part_a']}' and '{ov['part_b']}' ({ov['overlap_volume_mm3']} mm³).",
                    }
                )
    checks_summary["revolute_sweep"] = sweep_res

    # ------------------------------------------------------------------
    # Check 5: Custom Fabricated Part DFM & Bearing Boss Wall Thickness (`THIN_BEARING_WALL_ERROR`)
    # ------------------------------------------------------------------
    from build123d import GeomType

    dfm_issues = []
    min_bearing_wall_mm = 2.0
    for lbl, p in mech_parts:
        ll = lbl.lower()
        meta = getattr(p, "_cots_meta", None) or {}
        if meta.get("is_cots") or "cots" in ll:
            continue
        if not any(k in ll for k in ("plate", "carrier", "bridge", "cheek", "mount", "bracket", "frame")):
            continue
        pbb = p.bounding_box()
        p_dx = pbb.max.X - pbb.min.X
        p_dy = pbb.max.Y - pbb.min.Y
        for f in p.faces():
            if f.geom_type != GeomType.CYLINDER:
                continue
            fbb = f.bounding_box()
            fdx = fbb.max.X - fbb.min.X
            fdy = fbb.max.Y - fbb.min.Y
            # Full 360-deg Z-axis bearing bore (exclude 90-deg corner fillets whose fdx == R instead of 2*R, and exclude external pillars)
            if abs(fdx - fdy) < 0.4 and 13.6 <= fdx <= 36.0 and p_dx > fdx * 2.2 and p_dy > fdy * 2.2:
                r_bore = 0.25 * (fdx + fdy)
                bcx = 0.5 * (fbb.min.X + fbb.max.X)
                bcy = 0.5 * (fbb.min.Y + fbb.max.Y)
                # Ensure this is an internal bore through the plate, not a tall standoff pillar
                fdz = fbb.max.Z - fbb.min.Z
                if fdz > 12.0:
                    continue
                wall_mm = min(
                    bcx - pbb.min.X - r_bore,
                    pbb.max.X - bcx - r_bore,
                    bcy - pbb.min.Y - r_bore,
                    pbb.max.Y - bcy - r_bore,
                )
                if -0.2 <= wall_mm < min_bearing_wall_mm:
                    dfm_issues.append(
                        {
                            "part": lbl,
                            "bore_center_xy_mm": [round(bcx, 2), round(bcy, 2)],
                            "bore_radius_mm": round(r_bore, 2),
                            "min_wall_thickness_mm": round(wall_mm, 2),
                        }
                    )
                    errors.append(
                        {
                            "code": "THIN_BEARING_WALL_ERROR",
                            "part": lbl,
                            "message": (
                                f"Custom part '{lbl}' has a bearing bore (R={r_bore:.1f} mm at X={bcx:.1f}, Y={bcy:.1f}) "
                                f"with only {wall_mm:.2f} mm wall thickness to the outer edge (< {min_bearing_wall_mm:.1f} mm minimum)."
                            ),
                        }
                    )
                    break

    checks_summary["dfm_bearing_wall"] = {
        "status": "PASS" if not dfm_issues else "FAIL",
        "min_required_wall_mm": min_bearing_wall_mm,
        "issues": dfm_issues,
    }

    return {
        "status": "PASS" if not errors else "FAIL",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "checks": checks_summary,
    }


def precompute_cots_functional_frames_catalog() -> dict:
    """
    Pre-computes `functional_frames_json` for all verified local COTS parts in
    `cots/catalog.db` (`local_cots_parts`) from their indexed B-Rep geometry columns
    (`bbox_min_mm`, `bbox_max_mm`, `primary_axis`, `axis_center_mm`, `mounting_holes_json`)
    in O(N) time without re-parsing all 2,500+ STEP files in OCCT.
    """
    import sqlite3
    from tools.cad_scraper import DB_PATH

    if not DB_PATH.exists():
        return {"updated_db_rows": 0}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(local_cots_parts)").fetchall()}
    if "functional_frames_json" not in cols:
        cur.execute("ALTER TABLE local_cots_parts ADD COLUMN functional_frames_json TEXT")
        conn.commit()

    rows = cur.execute(
        "SELECT part_id, bbox_min_mm, bbox_max_mm, center_of_mass_mm, primary_axis, axis_center_mm, mounting_holes_json "
        "FROM local_cots_parts WHERE bbox_min_mm IS NOT NULL AND bbox_max_mm IS NOT NULL"
    ).fetchall()

    updates = []
    ax_idx_map = {"X": 0, "Y": 1, "Z": 2}
    for r in rows:
        try:
            bmin = json.loads(r["bbox_min_mm"])
            bmax = json.loads(r["bbox_max_mm"])
            bmid = [round(0.5 * (bmin[i] + bmax[i]), 3) for i in range(3)]
            com = json.loads(r["center_of_mass_mm"]) if r["center_of_mass_mm"] else list(bmid)
            pax = (r["primary_axis"] or "Z").upper()
            ax_i = ax_idx_map.get(pax, 2)
            ac = json.loads(r["axis_center_mm"]) if r["axis_center_mm"] else list(bmid)

            c_min = list(ac)
            c_min[ax_i] = round(float(bmin[ax_i]), 3)
            c_ctr = list(ac)
            c_ctr[ax_i] = round(float(bmid[ax_i]), 3)
            c_max = list(ac)
            c_max[ax_i] = round(float(bmax[ax_i]), 3)

            # Motor/Servo output shaft sits opposite the heavy motor body (COM)
            out_end = c_max if com[ax_i] <= bmid[ax_i] else c_min
            raw_mh = json.loads(r["mounting_holes_json"]) if r["mounting_holes_json"] else {}
            if isinstance(raw_mh, dict):
                mholes = (raw_mh.get("centers_2d_mm") or [])[:8]
            elif isinstance(raw_mh, list):
                mholes = raw_mh[:8]
            else:
                mholes = []

            frames = {
                "bbox_center": {"origin_mm": bmid, "axis": pax},
                "com_mm": [round(float(v), 3) for v in com],
                "bore_min": {"origin_mm": c_min, "axis": pax},
                "bore_center": {"origin_mm": c_ctr, "axis": pax},
                "bore_max": {"origin_mm": c_max, "axis": pax},
                "output_shaft": {"origin_mm": out_end, "axis": pax},
                "ear_mount": {"origin_mm": c_ctr, "axis": pax, "mount_holes_mm": mholes},
                "flange_seat": {"origin_mm": c_min, "axis": pax},
                "sense_origin": {"origin_mm": [bmid[0], bmid[1], round(float(bmin[2]), 3)], "axis": "Z"},
            }
            updates.append((json.dumps(frames), r["part_id"]))
        except Exception:
            continue

    cur.executemany(
        "UPDATE local_cots_parts SET functional_frames_json = ? WHERE part_id = ?",
        updates,
    )
    conn.commit()
    conn.close()
    return {"updated_db_rows": len(updates)}


# ==============================================================================
# Declarative Mate-Graph Assembly Solver (`MateGraphBuilder`)
# ==============================================================================
def _unit_axis_vec(axis_spec) -> tuple[float, float, float]:
    if isinstance(axis_spec, (list, tuple)) and len(axis_spec) == 3:
        ax, ay, az = float(axis_spec[0]), float(axis_spec[1]), float(axis_spec[2])
        n = math.sqrt(ax * ax + ay * ay + az * az)
        if n > 1e-9:
            return (ax / n, ay / n, az / n)
    s = str(axis_spec or "Z").strip().upper()
    if s in ("X", "+X", "PLUS_X"):
        return (1.0, 0.0, 0.0)
    if s in ("-X", "MINUS_X"):
        return (-1.0, 0.0, 0.0)
    if s in ("Y", "+Y", "PLUS_Y"):
        return (0.0, 1.0, 0.0)
    if s in ("-Y", "MINUS_Y"):
        return (0.0, -1.0, 0.0)
    if s in ("-Z", "MINUS_Z"):
        return (0.0, 0.0, -1.0)
    return (0.0, 0.0, 1.0)


def _canonical_axis_label(vec: tuple[float, float, float]) -> str:
    ax, ay, az = vec
    if abs(ax) >= abs(ay) and abs(ax) >= abs(az):
        return "X" if ax >= 0 else "-X"
    if abs(ay) >= abs(ax) and abs(ay) >= abs(az):
        return "Y" if ay >= 0 else "-Y"
    return "Z" if az >= 0 else "-Z"


def _rodrigues_align_matrix(u_src: tuple[float, float, float], u_dst: tuple[float, float, float]):
    """Return 3x3 rotation matrix (as nested list) rotating unit vector `u_src` onto `u_dst`."""
    import numpy as np

    a = np.asarray(u_src, dtype=np.float64)
    b = np.asarray(u_dst, dtype=np.float64)
    a /= max(float(np.linalg.norm(a)), 1e-12)
    b /= max(float(np.linalg.norm(b)), 1e-12)
    c = float(np.dot(a, b))
    if c > 1.0 - 1e-10:
        return np.eye(3, dtype=np.float64)
    if c < -1.0 + 1e-10:
        # 180-deg flip around an axis orthogonal to `a`
        ortho = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        k = np.cross(a, ortho)
        k /= float(np.linalg.norm(k))
        return 2.0 * np.outer(k, k) - np.eye(3, dtype=np.float64)
    v = np.cross(a, b)
    s = float(np.linalg.norm(v))
    vx = np.array(
        [
            [0.0, -v[2], v[1]],
            [v[2], 0.0, -v[0]],
            [-v[1], v[0], 0.0],
        ],
        dtype=np.float64,
    )
    return np.eye(3, dtype=np.float64) + vx + (vx @ vx) * ((1.0 - c) / (s * s))


def _axis_angle_matrix(axis_u: tuple[float, float, float], angle_deg: float):
    import numpy as np

    if abs(angle_deg) < 1e-9:
        return np.eye(3, dtype=np.float64)
    k = np.asarray(axis_u, dtype=np.float64)
    k /= max(float(np.linalg.norm(k)), 1e-12)
    th = math.radians(float(angle_deg))
    c, s = math.cos(th), math.sin(th)
    kx = np.array(
        [
            [0.0, -k[2], k[1]],
            [k[2], 0.0, -k[0]],
            [-k[1], k[0], 0.0],
        ],
        dtype=np.float64,
    )
    return c * np.eye(3, dtype=np.float64) + s * kx + (1.0 - c) * np.outer(k, k)


def _transform_build123d_part(part_obj, rot_3x3, trans_3, label: str, color_rgb: tuple[float, float, float]):
    """Apply an exact rigid 3D `(R, t)` transform to a `build123d` shape while preserving label, color & COTS metadata."""
    from build123d import Compound, Shape, Solid
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf

    trsf = gp_Trsf()
    trsf.SetValues(
        float(rot_3x3[0, 0]), float(rot_3x3[0, 1]), float(rot_3x3[0, 2]), float(trans_3[0]),
        float(rot_3x3[1, 0]), float(rot_3x3[1, 1]), float(rot_3x3[1, 2]), float(trans_3[1]),
        float(rot_3x3[2, 0]), float(rot_3x3[2, 1]), float(rot_3x3[2, 2]), float(trans_3[2]),
    )
    wrapped = getattr(part_obj, "wrapped", part_obj)
    moved_tds = BRepBuilderAPI_Transform(wrapped, trsf, True).Shape()
    moved_shape = Shape.cast(moved_tds)
    sols = list(moved_shape.solids())
    out = Compound(children=sols) if len(sols) > 1 else (sols[0] if len(sols) == 1 else moved_shape)
    out.label = label
    out.color = Color(*color_rgb)
    if hasattr(part_obj, "_cots_meta"):
        out._cots_meta = dict(getattr(part_obj, "_cots_meta"))
    return out


class MateGraphBuilder:
    """
    Opt-in Declarative Assembly Solver for `build123d` scripts.
    Places custom & COTS parts by solving semantic anchor-to-anchor mates (`FASTENED`,
    `REVOLUTE`, `SLIDER`, `CYLINDRICAL`) from grounded root parts, eliminating manual
    world `(x, y, z)` trigonometry drift and automatically generating a matching
    `*_constraints.json` specification for SolidCraft, `cad_lint`, `cad_physics_sim`, and Onshape.
    """

    def __init__(self, assembly_name: str = "Assembly") -> None:
        self.assembly_name = assembly_name
        self._parts: dict[str, dict] = {}
        self._mates: list[dict] = []
        self._mate_groups: list[dict] = []
        self._gear_relations: list[dict] = []

    def add_part(
        self,
        name: str,
        part_obj,
        anchors: dict[str, dict] | None = None,
        grounded: bool = False,
        world_pose_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> "MateGraphBuilder":
        """
        Register a `build123d` part in its local coordinate frame along with named anchors:
        `anchors={"bore": {"origin_mm": [0, 0, 0], "axis": "Z"}, ...}`.
        Automatically inherits COTS `functional_frames` if present on `part_obj._cots_meta`.
        """
        merged_anchors: dict[str, dict] = {
            "origin": {"origin_mm": [0.0, 0.0, 0.0], "axis": "Z"},
        }
        cots_meta = getattr(part_obj, "_cots_meta", None) or {}
        for k, v in (cots_meta.get("functional_frames") or {}).items():
            if isinstance(v, dict) and "origin_mm" in v:
                merged_anchors[k] = {
                    "origin_mm": [float(x) for x in v["origin_mm"]],
                    "axis": v.get("axis", "Z"),
                }
        for k, v in (anchors or {}).items():
            merged_anchors[k] = {
                "origin_mm": [float(x) for x in v.get("origin_mm", [0.0, 0.0, 0.0])],
                "axis": v.get("axis", "Z"),
            }

        col = getattr(part_obj, "color", None)
        if col is not None and hasattr(col, "wrapped"):
            rgba = col.wrapped.GetRGB()
            rgb = (float(rgba.Red()), float(rgba.Green()), float(rgba.Blue()))
        else:
            rgb = (0.35, 0.55, 0.82)

        self._parts[name] = {
            "name": name,
            "part_obj": part_obj,
            "anchors": merged_anchors,
            "grounded": bool(grounded),
            "world_pose_mm": tuple(float(v) for v in world_pose_mm),
            "rgb": rgb,
        }
        return self

    def add_mate(
        self,
        name: str,
        mate_type: str,
        part_a: str,
        anchor_a: str,
        part_b: str,
        anchor_b: str,
        axial_offset_mm: float = 0.0,
        clock_deg: float = 0.0,
        flip_axis: bool = False,
        limits_deg: tuple[float, float] | None = None,
        limits_mm: tuple[float, float] | None = None,
    ) -> "MateGraphBuilder":
        self._mates.append(
            {
                "key": f"m_{name}",
                "name": name,
                "type": mate_type.strip().upper(),
                "part_a": part_a,
                "anchor_a": anchor_a,
                "part_b": part_b,
                "anchor_b": anchor_b,
                "axial_offset_mm": float(axial_offset_mm),
                "clock_deg": float(clock_deg),
                "flip_axis": bool(flip_axis),
                "limits_deg": list(limits_deg) if limits_deg is not None else None,
                "limits_mm": list(limits_mm) if limits_mm is not None else None,
            }
        )
        return self

    def add_rigid_group(self, name: str, parts: list[str]) -> "MateGraphBuilder":
        self._mate_groups.append({"name": name, "parts": list(parts)})
        return self

    def add_gear_relation(
        self,
        name: str,
        mate_1: str,
        mate_2: str,
        ratio: float | str = 1.0,
        reverse_direction: bool = False,
    ) -> "MateGraphBuilder":
        k1 = mate_1 if mate_1.startswith("m_") else f"m_{mate_1}"
        k2 = mate_2 if mate_2.startswith("m_") else f"m_{mate_2}"
        self._gear_relations.append(
            {
                "name": name,
                "mate1": k1,
                "mate2": k2,
                "ratio": str(ratio),
                "reverse": bool(reverse_direction),
            }
        )
        return self

    def solve(self) -> dict:
        """Solve 3D world poses for all registered parts and compile the `constraints.json` dictionary."""
        import numpy as np
        from build123d import Compound

        if not self._parts:
            raise ValueError("MateGraphBuilder has no parts registered.")

        solved_pose: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        grounded_names = [n for n, p in self._parts.items() if p["grounded"]]
        if not grounded_names:
            first_name = next(iter(self._parts))
            grounded_names = [first_name]
            self._parts[first_name]["grounded"] = True

        for gn in grounded_names:
            wp = self._parts[gn]["world_pose_mm"]
            solved_pose[gn] = (np.eye(3, dtype=np.float64), np.asarray(wp, dtype=np.float64))

        unresolved = list(self._mates)
        progressed = True
        while progressed and unresolved:
            progressed = False
            for m in list(unresolved):
                pa, pb = m["part_a"], m["part_b"]
                if pa in solved_pose and pb not in solved_pose:
                    R_a, t_a = solved_pose[pa]
                    anc_a = self._parts[pa]["anchors"][m["anchor_a"]]
                    anc_b = self._parts[pb]["anchors"][m["anchor_b"]]

                    p_a_w = R_a @ np.asarray(anc_a["origin_mm"], dtype=np.float64) + t_a
                    d_a_w = R_a @ np.asarray(_unit_axis_vec(anc_a["axis"]), dtype=np.float64)
                    d_target = -d_a_w if m["flip_axis"] else d_a_w

                    d_b_loc = _unit_axis_vec(anc_b["axis"])
                    R_align = _rodrigues_align_matrix(d_b_loc, tuple(d_target.tolist()))
                    R_clock = _axis_angle_matrix(tuple(d_target.tolist()), m["clock_deg"])
                    R_b = R_clock @ R_align
                    t_b = p_a_w + m["axial_offset_mm"] * d_a_w - (R_b @ np.asarray(anc_b["origin_mm"], dtype=np.float64))

                    solved_pose[pb] = (R_b, t_b)
                    unresolved.remove(m)
                    progressed = True
                elif pb in solved_pose and pa not in solved_pose:
                    R_b, t_b = solved_pose[pb]
                    anc_b = self._parts[pb]["anchors"][m["anchor_b"]]
                    anc_a = self._parts[pa]["anchors"][m["anchor_a"]]

                    p_b_w = R_b @ np.asarray(anc_b["origin_mm"], dtype=np.float64) + t_b
                    d_b_w = R_b @ np.asarray(_unit_axis_vec(anc_b["axis"]), dtype=np.float64)
                    d_target = -d_b_w if m["flip_axis"] else d_b_w

                    d_a_loc = _unit_axis_vec(anc_a["axis"])
                    R_align = _rodrigues_align_matrix(d_a_loc, tuple(d_target.tolist()))
                    R_a = R_align
                    t_a = p_b_w - m["axial_offset_mm"] * d_target - (R_a @ np.asarray(anc_a["origin_mm"], dtype=np.float64))

                    solved_pose[pa] = (R_a, t_a)
                    unresolved.remove(m)
                    progressed = True
                elif pa in solved_pose and pb in solved_pose:
                    unresolved.remove(m)
                    progressed = True

        unsolved_parts = [n for n in self._parts if n not in solved_pose]
        if unsolved_parts:
            raise ValueError(f"Disconnected parts in MateGraphBuilder (no mate path to grounded base): {unsolved_parts}")

        placed_parts = []
        world_anchors: dict[str, dict[str, dict]] = {}
        for pname, pinfo in self._parts.items():
            R_w, t_w = solved_pose[pname]
            moved = _transform_build123d_part(pinfo["part_obj"], R_w, t_w, label=pname, color_rgb=pinfo["rgb"])
            placed_parts.append(moved)

            w_ancs: dict[str, dict] = {}
            for ak, av in pinfo["anchors"].items():
                pw = R_w @ np.asarray(av["origin_mm"], dtype=np.float64) + t_w
                dw = R_w @ np.asarray(_unit_axis_vec(av["axis"]), dtype=np.float64)
                w_ancs[ak] = {
                    "origin_mm": [round(float(pw[0]), 4), round(float(pw[1]), 4), round(float(pw[2]), 4)],
                    "axis_vec": [round(float(dw[0]), 5), round(float(dw[1]), 5), round(float(dw[2]), 5)],
                    "axis": _canonical_axis_label((float(dw[0]), float(dw[1]), float(dw[2]))),
                }
            world_anchors[pname] = w_ancs

        # Build canonical `constraints.json`
        mate_connectors = [
            {
                "key": "mc_world",
                "name": "MC | World Origin",
                "part": "WORLD",
                "axis": "Z",
                "tx_mm": 0.0,
                "ty_mm": 0.0,
                "tz_mm": 0.0,
            }
        ]
        mates_json = []

        for gn in grounded_names:
            mc_g = f"mc_{gn}_ground"
            gp = solved_pose[gn][1]
            mate_connectors.append(
                {
                    "key": mc_g,
                    "name": f"MC | {gn} Ground",
                    "part": gn,
                    "axis": "Z",
                    "tx_mm": round(float(gp[0]), 4),
                    "ty_mm": round(float(gp[1]), 4),
                    "tz_mm": round(float(gp[2]), 4),
                }
            )
            mates_json.append(
                {
                    "key": f"m_ground_{gn}",
                    "name": f"FASTENED | Ground {gn}",
                    "type": "FASTENED",
                    "mc1": "mc_world",
                    "mc2": mc_g,
                }
            )

        for idx, m in enumerate(self._mates):
            wa = world_anchors[m["part_a"]][m["anchor_a"]]
            wb = world_anchors[m["part_b"]][m["anchor_b"]]
            mc1_key = f"mc_{idx}_{m['part_a']}_{m['anchor_a']}"
            mc2_key = f"mc_{idx}_{m['part_b']}_{m['anchor_b']}"
            mate_connectors.append(
                {
                    "key": mc1_key,
                    "name": f"MC | {m['part_a']}.{m['anchor_a']}",
                    "part": m["part_a"],
                    "axis": wa["axis"].lstrip("-"),
                    "tx_mm": wa["origin_mm"][0],
                    "ty_mm": wa["origin_mm"][1],
                    "tz_mm": wa["origin_mm"][2],
                }
            )
            mate_connectors.append(
                {
                    "key": mc2_key,
                    "name": f"MC | {m['part_b']}.{m['anchor_b']}",
                    "part": m["part_b"],
                    "axis": wb["axis"].lstrip("-"),
                    "tx_mm": wb["origin_mm"][0],
                    "ty_mm": wb["origin_mm"][1],
                    "tz_mm": wb["origin_mm"][2],
                }
            )
            m_entry: dict = {
                "key": m["key"],
                "name": f"{m['type']} | {m['name']}",
                "type": m["type"],
                "mc1": mc1_key,
                "mc2": mc2_key,
            }
            if m["limits_deg"] is not None:
                m_entry["limits_deg"] = m["limits_deg"]
            if m["limits_mm"] is not None:
                m_entry["limits_mm"] = m["limits_mm"]
            mates_json.append(m_entry)

        constraints_spec = {
            "schema_version": "1.0",
            "assembly": self.assembly_name,
            "grounded_parts": grounded_names,
            "mate_groups": list(self._mate_groups),
            "mate_connectors": mate_connectors,
            "mates": mates_json,
            "gear_relations": list(self._gear_relations),
        }

        compound = Compound(label=self.assembly_name, children=placed_parts)
        return {
            "compound": compound,
            "parts": placed_parts,
            "world_anchors": world_anchors,
            "constraints": constraints_spec,
        }

    def export(self, step_path: Path | str, constraints_path: Path | str | None = None) -> dict:
        """Solve the mate graph and write both `<assembly>.step` and `*_constraints.json`."""
        from build123d import export_step

        solved = self.solve()
        sp = Path(step_path).resolve()
        sp.parent.mkdir(parents=True, exist_ok=True)
        cp = Path(constraints_path).resolve() if constraints_path else sp.with_name(f"{sp.stem}_constraints.json")
        cp.parent.mkdir(parents=True, exist_ok=True)

        export_step(solved["compound"], str(sp))
        cp.write_text(json.dumps(solved["constraints"], indent=2), encoding="utf-8")
        return {
            "step_path": str(sp),
            "constraints_path": str(cp),
            **solved,
        }




