"""
Unified Robotics COTS CAD Catalog, Scraper & B-Rep Fingerprinter
================================================================
Manages both:
1. Local STEP CAD models (`cots/vendors/{gobilda,rev,swyft}` + preserved legacy folders)
   with automated OpenCASCADE (OCP) B-Rep bounding-box, volume, and solid-count
   fingerprinting for Route A (`build123d` / FreeCAD) AI CAD development.
2. Native Onshape COTS insertables synced from `FRCDesignApp` (`https://app.frcdesign.org/api/`)
   across `ftc-design-lib`, `frc-design-lib`, and `mkcad`, recording `documentId`,
   `versionId`, `elementId`, `microversionId`, and configuration schemas for Route B
   (Native Onshape assemblies).

Storage:
- SQLite WAL database: `cots/catalog.db`
- JSON manifest snapshot: `cots/cots_manifest.json`
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

logger = logging.getLogger("cad_scraper")

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
COTS_DIR = WORKSPACE_ROOT / "cots"
VENDORS_DIR = COTS_DIR / "vendors"
DB_PATH = COTS_DIR / "catalog.db"
MANIFEST_PATH = COTS_DIR / "cots_manifest.json"

FRCDESIGN_API_BASE = "https://app.frcdesign.org/api"
FRCDESIGN_LIBRARIES = ["ftc-design-lib", "frc-design-lib", "mkcad"]

NON_CAD_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "dropbox.com",
    "a360.co",
    "github.com",
    "basicmicro.com",
]

NON_CAD_EXTS = [
    ".pdf",
    ".exe",
    ".msi",
    ".dmg",
    ".pkg",
    ".apk",
    ".hex",
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".mp4",
    ".mov",
]

# Normalized engineering categories (word-boundary safe; mechanical hubs/brackets evaluated cleanly)
CATEGORY_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("motors", ["motor", "yellow jacket", "5202", "5203", "5204", "neo", "vortex", "falcon", "kraken", "planetary gearmotor", "spike"]),
    ("servos", ["servo", "servoblock", "servo horn", "axon", "titan"]),
    ("bearings", ["bearing", "pillow block", "turntable", "thrust", "v-groove", "bushing"]),
    ("wheels", ["wheel", "mecanum", "omni", "traction", "odometry", "wheeltracker", "deadwheel"]),
    ("fasteners", ["screw", "nut", "washer", "shim", "bolt", "rivet", "heat-set", "heatset", "ruthex", "threadlock", "ziptie"]),
    (
        "motion",
        [
            "gear", "sprocket", "pulley", "belt", "chain", "shaft", "axle", "collar", "coupler",
            "hyperhub", "sonic hub", "maxhub", "hex hub", "motion hub", "clamping hub", "pattern hub", "sr-hub",
            "lead screw", "rack", "pinion", "maxspline", "spline", "hex adapter", "flywheel", "linear", "slide", "rail", "carriage", "elevator", "ascend",
        ],
    ),
    (
        "electronics",
        [
            "sensor", "camera", "encoder", "limit switch", "switch cable", "cable", "wire",
            "control hub", "expansion hub", "servo power", "usb hub", "ultrausb", "power distribution",
            "spark max", "spark flex", "pinpoint", "prism", "ranger", "swyft sense", "magnetic limit",
            "canender", "jst", "driver station", "battery", "imu", "potentiometer", "blinkin", "logic level",
        ],
    ),
    ("structure", ["channel", "tube", "gotube", "extrusion", "bracket", "mount", "plate", "standoff", "spacer", "beam", "endcap", "connector", "bumper", "sign", "chassis", "frame", "funnel"]),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_category(raw_cat: str, title: str = "") -> str:
    """Map noisy vendor breadcrumbs/titles into a clean 1-level engineering category without substring collisions."""
    combined = f"{raw_cat} {title}".lower()
    # Explicit word-boundary LED / PCB check after mechanical checks
    for norm_cat, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if " " in kw or "-" in kw:
                if kw in combined:
                    return norm_cat
            elif re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?:s|es)?(?![a-z])", combined):
                return norm_cat
    if re.search(r"\b(led|pcb|imu|usb|xt30|pdh|pdp|roborio)\b", combined):
        return "electronics"
    if re.search(r"\bhub\b", combined):
        return "motion"
    return "structure"


def sanitize_slug(text: str, max_len: int = 90) -> str:
    """Convert arbitrary text into a clean filesystem-safe slug."""
    text = re.sub(r"goBILDA®?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"REV Robotics", "", text, flags=re.IGNORECASE)
    text = re.sub(r"SWYFT Robotics|SWYFT", "", text, flags=re.IGNORECASE)
    text = text.replace("™", "").replace("®", "").replace("©", "").replace("°", "deg")
    text = text.replace('"', "").replace("'", "")
    text = re.sub(r"[\\/*?:\"<>|\r\n\t]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._")
    text = re.sub(r"_+", "_", text)
    return (text[:max_len].strip(" ._")) or "part"


def extract_sku(title: str, url: str = "") -> Optional[str]:
    """Extract standard SKU from title or URL (goBILDA, REV, Swyft)."""
    for source in (url, title):
        # goBILDA 4-4-4 SKU
        m = re.search(r"\b(\d{4}-\d{4}-\d{4})\b", source)
        if m:
            return m.group(1)
        # REV SKU (e.g. REV-41-1291 or REV-21-2328)
        m = re.search(r"\b(REV-\d{2}-\d{4}(?:-PK\d+)?)\b", source, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        # Swyft SKU (allow dots for sizes like 0.5in)
        m = re.search(
            r"\b(SR-[A-Z0-9][A-Z0-9.\-_]*|JCB-\d+|BRG-[A-Z0-9.\-_]+|SHC-\d+)\b",
            source,
            re.IGNORECASE,
        )
        if m:
            sku = m.group(1).upper().rstrip("._-")
            sku = re.sub(r"\.(STEP|STP|ZIP)$", "", sku, flags=re.IGNORECASE)
            return sku
    return None


def is_valid_step_bytes(head: bytes) -> bool:
    """Verify STEP magic header (`ISO-10303-21`) in first 1024 bytes."""
    return b"ISO-10303-21" in head[:1024]


def validate_step_file(path: Path) -> bool:
    """Verify a file on disk is a genuine ISO-10303-21 STEP file."""
    if not path.exists() or not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
        return is_valid_step_bytes(head)
    except Exception:
        return False


def extract_shape_primary_axis(shape: Any) -> Optional[Dict[str, Any]]:
    """
    Detects the dominant coaxial cylindrical bore/shaft axis ('X', 'Y', or 'Z'),
    its 3D centerline point `axis_center_mm`, minimum cylindrical radius
    `min_bore_radius_mm`, and M3/M4/#10-32 mounting hole pattern `mounting_holes_json`
    from an OpenCASCADE `TopoDS_Shape` in a single face traversal.
    """
    from collections import defaultdict
    import math
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    exp = TopExp_Explorer(shape, TopAbs_FACE)
    groups: Dict[Tuple[str, float, float], Dict[str, Any]] = defaultdict(
        lambda: {"weight": 0.0, "radii": [], "pts": []}
    )
    fastener_holes: Dict[str, Dict[Tuple[float, float], List[float]]] = {
        "X": defaultdict(list),
        "Y": defaultdict(list),
        "Z": defaultdict(list),
    }

    while exp.More():
        face = TopoDS.Face(exp.Current())
        exp.Next()
        surf = BRepAdaptor_Surface(face, False)
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        cyl = surf.Cylinder()
        r = float(cyl.Radius())
        if r < 1.15 or r > 120.0:
            continue
        ax = cyl.Axis()
        d = ax.Direction()
        p = ax.Location()
        dx, dy, dz = abs(d.X()), abs(d.Y()), abs(d.Z())
        if dz > 0.98:
            ax_name = "Z"
            c2d = (round(p.X(), 1), round(p.Y(), 1))
            pt3 = (p.X(), p.Y(), 0.0)
        elif dy > 0.98:
            ax_name = "Y"
            c2d = (round(p.X(), 1), round(p.Z(), 1))
            pt3 = (p.X(), 0.0, p.Z())
        elif dx > 0.98:
            ax_name = "X"
            c2d = (round(p.Y(), 1), round(p.Z(), 1))
            pt3 = (0.0, p.Y(), p.Z())
        else:
            continue

        # Fastener holes: M3 (r~1.25-1.70), M4 (r~1.65-2.20), #10-32/5mm (r~2.20-2.75)
        if 1.15 <= r <= 2.75:
            fastener_holes[ax_name][c2d].append(round(r, 3))

        # Primary bore / shaft / boss cylinders (>= 2.25 mm radius)
        if r >= 2.25:
            key = (ax_name, c2d[0], c2d[1])
            u_span = abs(surf.LastUParameter() - surf.FirstUParameter())
            v_span = min(50.0, abs(surf.LastVParameter() - surf.FirstVParameter()))
            w = r * u_span * max(1.0, v_span)
            groups[key]["weight"] += w
            groups[key]["radii"].append(round(r, 3))
            groups[key]["pts"].append(pt3)

    if groups:
        best_key, best_val = max(groups.items(), key=lambda kv: kv[1]["weight"])
        primary_ax = best_key[0]
        pts = best_val["pts"]
        cx = round(sum(pt[0] for pt in pts) / len(pts), 3)
        cy = round(sum(pt[1] for pt in pts) / len(pts), 3)
        cz = round(sum(pt[2] for pt in pts) / len(pts), 3)
        min_r = round(min(best_val["radii"]), 3)
        center_2d = (best_key[1], best_key[2])
    else:
        # Fallback for brackets/plates with fastener holes but no large central bore
        best_ax, best_holes = max(fastener_holes.items(), key=lambda kv: len(kv[1]))
        if not best_holes:
            return None
        primary_ax = best_ax
        u_avg = round(sum(c[0] for c in best_holes) / len(best_holes), 3)
        v_avg = round(sum(c[1] for c in best_holes) / len(best_holes), 3)
        if primary_ax == "Z":
            cx, cy, cz = u_avg, v_avg, 0.0
        elif primary_ax == "Y":
            cx, cy, cz = u_avg, 0.0, v_avg
        else:
            cx, cy, cz = 0.0, u_avg, v_avg
        all_r = [r for rl in best_holes.values() for r in rl]
        min_r = round(min(all_r), 3)
        center_2d = (round(u_avg, 1), round(v_avg, 1))

    # Extract mounting hole pattern parallel to primary_ax (excluding the central bore itself)
    ax_holes = {
        c2d: rl
        for c2d, rl in fastener_holes[primary_ax].items()
        if math.hypot(c2d[0] - center_2d[0], c2d[1] - center_2d[1]) > 3.0
    }
    if not ax_holes:
        # Check if mounting holes lie on another dominant axis (e.g. side-mounted servo bracket)
        other_ax, other_holes = max(fastener_holes.items(), key=lambda kv: len(kv[1]))
        if len(other_holes) >= 2:
            hole_ax = other_ax
            ax_holes = other_holes
        else:
            hole_ax = primary_ax
    else:
        hole_ax = primary_ax

    mounting_info = None
    if ax_holes:
        hole_radii = sorted(min(rl) for rl in ax_holes.values())
        median_r = round(hole_radii[len(hole_radii) // 2], 3)
        pitch_radii = sorted(
            {
                round(math.hypot(c2d[0] - center_2d[0], c2d[1] - center_2d[1]), 2)
                for c2d in ax_holes
                if hole_ax == primary_ax
            }
        )
        sorted_keys = sorted(
            ax_holes.keys(),
            key=lambda c: (round(math.hypot(c[0] - center_2d[0], c[1] - center_2d[1]), 1), c[0], c[1]),
        )
        centers_list = [[c2d[0], c2d[1]] for c2d in sorted_keys[:48]]
        mounting_info = {
            "axis": hole_ax,
            "count": len(ax_holes),
            "hole_radius_mm": median_r,
            "bolt_circle_radii_mm": pitch_radii[:10],
            "centers_2d_mm": centers_list,
        }

    return {
        "primary_axis": primary_ax,
        "axis_center_mm": [cx, cy, cz],
        "min_bore_radius_mm": min_r,
        "mounting_holes_json": json.dumps(mounting_info) if mounting_info else None,
    }



_STEP_PT_RE = re.compile(
    rb"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\)"
)
_STEP_CYL_RE = re.compile(
    rb"CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#\d+\s*,\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\)"
)


def _fast_step_entity_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    """Fast ASCII STEP entity scanner for large (>6 MB) multi-body STEP assemblies."""
    try:
        raw = path.read_bytes()
        pts = [
            (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            for m in _STEP_PT_RE.finditer(raw)
        ]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        bmin = [round(min(xs), 3), round(min(ys), 3), round(min(zs), 3)]
        bmax = [round(max(xs), 3), round(max(ys), 3), round(max(zs), 3)]
        extents = [
            round(bmax[0] - bmin[0], 3),
            round(bmax[1] - bmin[1], 3),
            round(bmax[2] - bmin[2], 3),
        ]
        com_mm = [
            round(0.5 * (bmin[0] + bmax[0]), 3),
            round(0.5 * (bmin[1] + bmax[1]), 3),
            round(0.5 * (bmin[2] + bmax[2]), 3),
        ]
        solid_count = max(1, raw.count(b"MANIFOLD_SOLID_BREP("))
        vol_cm3 = round((extents[0] * extents[1] * extents[2]) * 0.25 / 1000.0, 4)
        radii = [
            round(float(m.group(1)), 3)
            for m in _STEP_CYL_RE.finditer(raw)
            if 1.2 <= float(m.group(1)) <= 25.0
        ]
        min_r = min(radii) if radii else 4.0
        # Default primary axis along shortest or standard Z axis
        prim_ax = ("X", "Y", "Z")[extents.index(min(extents))] if max(extents) > 0 else "Z"
        return {
            "solid_count": solid_count,
            "volume_cm3": vol_cm3,
            "bbox_min_mm": bmin,
            "bbox_max_mm": bmax,
            "extents_mm": extents,
            "center_of_mass_mm": com_mm,
            "primary_axis": prim_ax,
            "axis_center_mm": list(com_mm),
            "min_bore_radius_mm": min_r,
            "mounting_holes_json": None,
        }
    except Exception:
        return None


def fingerprint_step_file(path: Path) -> Optional[Dict[str, Any]]:
    """
    Compute fast B-Rep geometric metadata (bounding box, extents, solid count,
    volume, center of mass, and primary cylindrical bore/shaft axis) using OpenCASCADE (OCP),
    falling back to fast STEP entity extraction for giant (> 6 MB) multi-body files.
    """
    if not validate_step_file(path):
        return None
    fsize = path.stat().st_size
    if fsize > 6 * 1024 * 1024:
        return _fast_step_entity_fingerprint(path)
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer

        reader = STEPControl_Reader()
        status = reader.ReadFile(str(path))
        if int(status) != 1:  # IFSelect_RetDone == 1
            return _fast_step_entity_fingerprint(path)
        reader.TransferRoots()
        shape = reader.OneShape()

        bbox = Bnd_Box()
        BRepBndLib.Add_s(shape, bbox)
        if bbox.IsVoid():
            return _fast_step_entity_fingerprint(path)
        cmin = bbox.CornerMin()
        cmax = bbox.CornerMax()
        bmin = [round(cmin.X(), 3), round(cmin.Y(), 3), round(cmin.Z(), 3)]
        bmax = [round(cmax.X(), 3), round(cmax.Y(), 3), round(cmax.Z(), 3)]
        extents = [
            round(bmax[0] - bmin[0], 3),
            round(bmax[1] - bmin[1], 3),
            round(bmax[2] - bmin[2], 3),
        ]

        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        solid_count = 0
        while exp.More():
            solid_count += 1
            exp.Next()

        vol_cm3 = round((extents[0] * extents[1] * extents[2]) * 0.35 / 1000.0, 4)
        com_mm = [
            round(0.5 * (bmin[0] + bmax[0]), 3),
            round(0.5 * (bmin[1] + bmax[1]), 3),
            round(0.5 * (bmin[2] + bmax[2]), 3),
        ]
        # Compute exact volume & CoM for single/compact parts (< 2 MB) to keep bulk indexing fast
        if fsize < 2 * 1024 * 1024:
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(shape, props)
            vol_cm3 = round(props.Mass() / 1000.0, 4)
            com = props.CentreOfMass()
            com_mm = [round(com.X(), 3), round(com.Y(), 3), round(com.Z(), 3)]

        out: Dict[str, Any] = {
            "solid_count": solid_count,
            "volume_cm3": vol_cm3,
            "bbox_min_mm": bmin,
            "bbox_max_mm": bmax,
            "extents_mm": extents,
            "center_of_mass_mm": com_mm,
        }
        ax_info = extract_shape_primary_axis(shape)
        if ax_info:
            out.update(ax_info)
        else:
            prim_ax = ("X", "Y", "Z")[extents.index(max(extents))] if max(extents) > 0 else "Z"
            out.update(
                {
                    "primary_axis": prim_ax,
                    "axis_center_mm": list(com_mm),
                    "min_bore_radius_mm": round(max(1.5, min(extents) * 0.25), 3),
                    "mounting_holes_json": None,
                }
            )
        return out
    except Exception as exc:
        logger.warning("OCP fingerprint failed on %s: %s", path, exc)
        return _fast_step_entity_fingerprint(path)


# ==============================================================================
# Program Classification (FTC vs FRC vs FTC/FRC) & SQLite Database Layer
# ==============================================================================


def classify_program(
    vendor: str,
    sku: Optional[str] = None,
    title: str = "",
    library_id: str = "",
) -> str:
    """
    Classify a COTS part or Onshape insertable into 'FTC', 'FRC', or 'FTC/FRC'.
    """
    v_low = (vendor or "").lower()
    s_up = (sku or "").upper()
    t_low = (title or "").lower()
    lib_low = (library_id or "").lower()

    if lib_low == "ftc-design-lib":
        return "FTC"
    if lib_low in ("frc-design-lib", "mkcad"):
        # Shared FTC/FRC vendors inside FRC libraries (e.g. goBILDA, REV DUO, Swyft FTC)
        if any(k in v_low for k in ("gb", "gobilda", "axon")):
            return "FTC/FRC"
        return "FRC"

    if "gobilda" in v_low:
        if any(k in t_low for k in ("m3", "m4", "washer", "nut", "socket head", "button head")):
            return "FTC/FRC"
        return "FTC"

    if v_low == "rev":
        if s_up.startswith(("REV-41-", "REV-31-")) or any(
            k in t_low for k in ("duo", "expansion hub", "control hub", "smart robot servo", "15mm", "hd hex", "core hex", "ultraplanetary")
        ):
            if any(k in t_low for k in ("through bore", "color sensor", "distance sensor", "blinkin", "servo", "sensor cable")):
                return "FTC/FRC"
            return "FTC"
        if s_up.startswith(("REV-21-", "REV-11-")) or any(
            k in t_low for k in ("ion", "maxswerve", "maxplanetary", "maxtube", "spark max", "spark flex", "neo", "vortex", "pdh", "pneumatic")
        ):
            if any(k in t_low for k in ("through bore", "blinkin", "laser")):
                return "FTC/FRC"
            return "FRC"
        return "FTC/FRC"

    if v_low == "swyft":
        if any(k in t_low or k in s_up.lower() for k in ("0.5in", "1/2", "easyswerve", "bumper", "thrifty", "frc")):
            return "FRC"
        if any(k in t_low or k in s_up.lower() for k in ("8mm", "m3", "ftc", "7mm")):
            return "FTC"
        return "FTC/FRC"

    if v_low == "swerve-cots":
        return "FRC"

    if any(k in v_low for k in ("wcp", "am", "andymark", "sds", "ctre", "ttb", "thrifty", "vex", "redux", "norgren", "lai")):
        return "FRC"

    return "FTC/FRC"


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS local_cots_parts (
            part_id TEXT PRIMARY KEY,
            program TEXT NOT NULL DEFAULT 'FTC/FRC',
            vendor TEXT NOT NULL,
            sku TEXT,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            product_url TEXT,
            cad_url TEXT,
            local_path TEXT,
            status TEXT NOT NULL DEFAULT 'DISCOVERED',
            file_size_bytes INTEGER,
            solid_count INTEGER,
            volume_cm3 REAL,
            bbox_min_mm TEXT,
            bbox_max_mm TEXT,
            extents_mm TEXT,
            center_of_mass_mm TEXT,
            primary_axis TEXT,
            axis_center_mm TEXT,
            min_bore_radius_mm REAL,
            mounting_holes_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_local_vendor ON local_cots_parts(vendor);
        CREATE INDEX IF NOT EXISTS idx_local_sku ON local_cots_parts(sku);
        CREATE INDEX IF NOT EXISTS idx_local_status ON local_cots_parts(status);
        CREATE INDEX IF NOT EXISTS idx_local_cat ON local_cots_parts(category);

        CREATE TABLE IF NOT EXISTS onshape_insertables (
            insertable_id TEXT PRIMARY KEY,
            program TEXT NOT NULL DEFAULT 'FRC',
            library_id TEXT NOT NULL,
            group_id TEXT,
            group_name TEXT,
            name TEXT NOT NULL,
            document_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            element_id TEXT NOT NULL,
            microversion_id TEXT,
            element_type TEXT NOT NULL,
            supports_fasten INTEGER NOT NULL DEFAULT 0,
            is_configurable INTEGER NOT NULL DEFAULT 0,
            vendors_json TEXT,
            parameters_json TEXT,
            records_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_os_lib ON onshape_insertables(library_id);
        CREATE INDEX IF NOT EXISTS idx_os_name ON onshape_insertables(name);

        CREATE TABLE IF NOT EXISTS cots_part_variants (
            variant_id TEXT PRIMARY KEY,
            program TEXT NOT NULL,
            source_type TEXT NOT NULL,
            vendor TEXT NOT NULL,
            sku TEXT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            local_part_id TEXT,
            insertable_id TEXT,
            configuration_key TEXT,
            product_url TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_var_program ON cots_part_variants(program);
        CREATE INDEX IF NOT EXISTS idx_var_vendor ON cots_part_variants(vendor);
        CREATE INDEX IF NOT EXISTS idx_var_sku ON cots_part_variants(sku);
        CREATE INDEX IF NOT EXISTS idx_var_cat ON cots_part_variants(category);
        """
    )
    # Migrate existing tables if created before new columns were added
    local_cols = {c[1] for c in conn.execute("PRAGMA table_info(local_cots_parts)").fetchall()}
    if "program" not in local_cols:
        conn.execute("ALTER TABLE local_cots_parts ADD COLUMN program TEXT NOT NULL DEFAULT 'FTC/FRC'")
    if "primary_axis" not in local_cols:
        conn.execute("ALTER TABLE local_cots_parts ADD COLUMN primary_axis TEXT")
    if "axis_center_mm" not in local_cols:
        conn.execute("ALTER TABLE local_cots_parts ADD COLUMN axis_center_mm TEXT")
    if "min_bore_radius_mm" not in local_cols:
        conn.execute("ALTER TABLE local_cots_parts ADD COLUMN min_bore_radius_mm REAL")
    if "mounting_holes_json" not in local_cols:
        conn.execute("ALTER TABLE local_cots_parts ADD COLUMN mounting_holes_json TEXT")
    os_cols = {c[1] for c in conn.execute("PRAGMA table_info(onshape_insertables)").fetchall()}
    if "program" not in os_cols:
        conn.execute("ALTER TABLE onshape_insertables ADD COLUMN program TEXT NOT NULL DEFAULT 'FRC'")
    conn.commit()


def upsert_local_part(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    row = dict(row)
    row.setdefault("updated_at", utc_now_iso())
    vendor = row.get("vendor", "unknown")
    sku = row.get("sku")
    title = row.get("title", "")
    row.setdefault("program", classify_program(vendor, sku, title))
    for k in ("bbox_min_mm", "bbox_max_mm", "extents_mm", "center_of_mass_mm", "axis_center_mm", "mounting_holes_json"):
        if isinstance(row.get(k), (list, tuple, dict)):
            row[k] = json.dumps(row[k])

    conn.execute(
        """
        INSERT INTO local_cots_parts (
            part_id, program, vendor, sku, title, category, product_url, cad_url,
            local_path, status, file_size_bytes, solid_count, volume_cm3,
            bbox_min_mm, bbox_max_mm, extents_mm, center_of_mass_mm,
            primary_axis, axis_center_mm, min_bore_radius_mm, mounting_holes_json, updated_at
        ) VALUES (
            :part_id, :program, :vendor, :sku, :title, :category, :product_url, :cad_url,
            :local_path, :status, :file_size_bytes, :solid_count, :volume_cm3,
            :bbox_min_mm, :bbox_max_mm, :extents_mm, :center_of_mass_mm,
            :primary_axis, :axis_center_mm, :min_bore_radius_mm, :mounting_holes_json, :updated_at
        )
        ON CONFLICT(part_id) DO UPDATE SET
            program = excluded.program,
            vendor = excluded.vendor,
            sku = COALESCE(excluded.sku, local_cots_parts.sku),
            title = CASE
                WHEN local_cots_parts.title IS NOT NULL
                     AND LENGTH(local_cots_parts.title) > LENGTH(excluded.title)
                     AND local_cots_parts.title LIKE '% %'
                THEN local_cots_parts.title
                ELSE excluded.title
            END,
            category = excluded.category,
            product_url = COALESCE(excluded.product_url, local_cots_parts.product_url),
            cad_url = COALESCE(excluded.cad_url, local_cots_parts.cad_url),
            local_path = COALESCE(excluded.local_path, local_cots_parts.local_path),
            status = excluded.status,
            file_size_bytes = COALESCE(excluded.file_size_bytes, local_cots_parts.file_size_bytes),
            solid_count = COALESCE(excluded.solid_count, local_cots_parts.solid_count),
            volume_cm3 = COALESCE(excluded.volume_cm3, local_cots_parts.volume_cm3),
            bbox_min_mm = COALESCE(excluded.bbox_min_mm, local_cots_parts.bbox_min_mm),
            bbox_max_mm = COALESCE(excluded.bbox_max_mm, local_cots_parts.bbox_max_mm),
            extents_mm = COALESCE(excluded.extents_mm, local_cots_parts.extents_mm),
            center_of_mass_mm = COALESCE(excluded.center_of_mass_mm, local_cots_parts.center_of_mass_mm),
            primary_axis = COALESCE(excluded.primary_axis, local_cots_parts.primary_axis),
            axis_center_mm = COALESCE(excluded.axis_center_mm, local_cots_parts.axis_center_mm),
            min_bore_radius_mm = COALESCE(excluded.min_bore_radius_mm, local_cots_parts.min_bore_radius_mm),
            mounting_holes_json = COALESCE(excluded.mounting_holes_json, local_cots_parts.mounting_holes_json),
            updated_at = excluded.updated_at
        """,
        {
            "part_id": row["part_id"],
            "program": row["program"],
            "vendor": vendor,
            "sku": sku,
            "title": title,
            "category": row.get("category", "structure"),
            "product_url": row.get("product_url"),
            "cad_url": row.get("cad_url"),
            "local_path": row.get("local_path"),
            "status": row.get("status", "DISCOVERED"),
            "file_size_bytes": row.get("file_size_bytes"),
            "solid_count": row.get("solid_count"),
            "volume_cm3": row.get("volume_cm3"),
            "bbox_min_mm": row.get("bbox_min_mm"),
            "bbox_max_mm": row.get("bbox_max_mm"),
            "extents_mm": row.get("extents_mm"),
            "center_of_mass_mm": row.get("center_of_mass_mm"),
            "primary_axis": row.get("primary_axis"),
            "axis_center_mm": row.get("axis_center_mm"),
            "min_bore_radius_mm": row.get("min_bore_radius_mm"),
            "mounting_holes_json": row.get("mounting_holes_json"),
            "updated_at": row["updated_at"],
        },
    )


def rebuild_frc_ftc_index(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    Rebuilds the unified `cots_part_variants` table across all FTC and FRC sources:
    - Local STEP parts (`local_cots_parts`: goBILDA, REV DUO FTC, REV ION FRC, Swyft FTC/FRC, legacy)
    - Onshape `ftc-design-lib` insertables, configuration records, and enum options
    - Onshape `frc-design-lib` insertables, 3,982+ SKU configuration records, and enum options
    - Onshape `mkcad` insertables and 2,703+ SKU configuration records
    Cross-links `local_part_id` and `insertable_id` whenever SKUs match.
    """
    now = utc_now_iso()

    # 1. Backfill `program` on existing local_cots_parts & build SKU -> local_part_id lookup
    local_rows = conn.execute("SELECT part_id, vendor, sku, title, category, product_url FROM local_cots_parts").fetchall()
    sku_to_local_id: Dict[str, str] = {}
    for r in local_rows:
        prog = classify_program(r["vendor"], r["sku"], r["title"])
        conn.execute("UPDATE local_cots_parts SET program = ? WHERE part_id = ?", (prog, r["part_id"]))
        if r["sku"]:
            s_up = r["sku"].upper()
            sku_to_local_id[s_up] = r["part_id"]
            if s_up.startswith("REV-"):
                sku_to_local_id[s_up[4:]] = r["part_id"]

    # 2. Rebuild cots_part_variants
    conn.execute("DELETE FROM cots_part_variants")
    variant_rows: List[Tuple[Any, ...]] = []

    # 2a. Insert all local COTS parts as variants
    for r in local_rows:
        prog = classify_program(r["vendor"], r["sku"], r["title"])
        variant_rows.append(
            (
                f"local:{r['part_id']}",
                prog,
                "local",
                r["vendor"].lower(),
                r["sku"],
                r["title"],
                r["category"],
                r["part_id"],
                None,
                None,
                r["product_url"],
                now,
            )
        )

    # 2b. Insert all Onshape insertables + records_json + enum options
    os_rows = conn.execute("SELECT * FROM onshape_insertables").fetchall()
    for os_r in os_rows:
        ins_id = os_r["insertable_id"]
        lib_id = os_r["library_id"]
        grp_name = os_r["group_name"] or ""
        ins_name = os_r["name"] or ""
        vendors = json.loads(os_r["vendors_json"] or "[]")
        primary_vendor = (vendors[0] if vendors else lib_id).lower()
        prog = classify_program(primary_vendor, None, ins_name, library_id=lib_id)
        conn.execute("UPDATE onshape_insertables SET program = ? WHERE insertable_id = ?", (prog, ins_id))
        cat = normalize_category(grp_name, ins_name)

        records = json.loads(os_r["records_json"] or "[]")
        if records:
            for idx, rec in enumerate(records):
                pnum = (rec.get("partNumber") or "").strip() or extract_sku(rec.get("name", ""), rec.get("url", ""))
                rname = rec.get("name") or ins_name
                rurl = rec.get("url")
                cfg_key = rec.get("configurationKey") or ""
                rec_prog = classify_program(primary_vendor, pnum, rname, library_id=lib_id)
                linked_local = sku_to_local_id.get(pnum.upper()) if pnum else None
                vid = f"{lib_id}:{ins_id}:rec:{pnum or idx}"
                variant_rows.append(
                    (
                        vid,
                        rec_prog,
                        lib_id,
                        primary_vendor,
                        pnum,
                        rname,
                        cat,
                        linked_local,
                        ins_id,
                        cfg_key,
                        rurl,
                        now,
                    )
                )
        else:
            pnum = extract_sku(ins_name)
            linked_local = sku_to_local_id.get(pnum.upper()) if pnum else None
            variant_rows.append(
                (
                    f"{lib_id}:{ins_id}",
                    prog,
                    lib_id,
                    primary_vendor,
                    pnum,
                    ins_name,
                    cat,
                    linked_local,
                    ins_id,
                    None,
                    None,
                now,
                )
            )

        # Also index enum option variants from parameters_json (e.g. FTC Game Elements, motor ratios)
        params = json.loads(os_r["parameters_json"] or "[]")
        for p in params:
            if p.get("type") == "enum":
                param_id = p.get("id", "")
                param_name = p.get("name", "")
                for opt in p.get("options", []):
                    opt_id = opt.get("id", "")
                    opt_name = opt.get("name", "")
                    if not opt_name or opt_name.lower() in ("default", "true", "false"):
                        continue
                    opt_sku = extract_sku(opt_name)
                    linked_local = sku_to_local_id.get(opt_sku.upper()) if opt_sku else None
                    vid = f"{lib_id}:{ins_id}:opt:{param_id}:{opt_id}"
                    variant_rows.append(
                        (
                            vid,
                            prog,
                            lib_id,
                            primary_vendor,
                            opt_sku,
                            f"{ins_name} — {param_name}: {opt_name}",
                            cat,
                            linked_local,
                            ins_id,
                            f"{param_id}={opt_id}",
                            None,
                            now,
                        )
                    )

    conn.executemany(
        """
        INSERT OR REPLACE INTO cots_part_variants (
            variant_id, program, source_type, vendor, sku, name, category,
            local_part_id, insertable_id, configuration_key, product_url, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        variant_rows,
    )
    conn.commit()

    by_prog = dict(conn.execute("SELECT program, COUNT(*) FROM cots_part_variants GROUP BY program").fetchall())
    logger.info("Rebuilt FRC/FTC variant index: %d total variants (%s)", len(variant_rows), by_prog)
    return {"total_variants": len(variant_rows), "by_program": by_prog}


def export_manifest_json(conn: sqlite3.Connection, manifest_path: Path = MANIFEST_PATH) -> None:
    """Write a clean JSON summary of all verified local STEP parts + Onshape library stats."""
    rows = conn.execute(
        "SELECT * FROM local_cots_parts WHERE status = 'VERIFIED_STEP' ORDER BY vendor, category, part_id"
    ).fetchall()
    manifest: Dict[str, Any] = {}
    for r in rows:
        d = dict(r)
        for k in ("bbox_min_mm", "bbox_max_mm", "extents_mm", "center_of_mass_mm"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        manifest[d["part_id"]] = d
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ==============================================================================
# Polite Chrome-Impersonated HTTP Client
# ==============================================================================


class PoliteHttpClient:
    """Persistent HTTP/2 Chrome-impersonated session with per-host pacing and backoff."""

    def __init__(self, min_interval_sec: float = 0.25):
        self.session = cffi_requests.Session(impersonate="chrome")
        self.min_interval_sec = min_interval_sec
        self._last_req_by_host: Dict[str, float] = {}

    def get(self, url: str, timeout: int = 20, max_retries: int = 3) -> Any:
        host = urlparse(url).netloc.lower()
        now = time.time()
        elapsed = now - self._last_req_by_host.get(host, 0.0)
        if elapsed < self.min_interval_sec:
            time.sleep(self.min_interval_sec - elapsed)

        last_err: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                self._last_req_by_host[host] = time.time()
                resp = self.session.get(url, timeout=timeout)
                if resp.status_code in (429, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp
            except Exception as exc:
                last_err = exc
                time.sleep(1.0 * (attempt + 1))
        raise last_err or RuntimeError(f"Failed GET {url}")


# ==============================================================================
# 1. FRCDesignApp Cloud Catalog Sync (`ftc-design-lib`, `frc-design-lib`, `mkcad`)
# ==============================================================================


def sync_frcdesign_libraries(
    conn: sqlite3.Connection,
    http: PoliteHttpClient,
    libraries: Optional[List[str]] = None,
    fetch_ftc_parameters: bool = True,
    fetch_all_parameters: bool = False,
) -> Dict[str, int]:
    """
    Syncs native Onshape COTS part metadata from `https://app.frcdesign.org/api/`
    into `onshape_insertables` and rebuilds the unified FRC/FTC `cots_part_variants` index.
    """
    libs = libraries or FRCDESIGN_LIBRARIES
    counts: Dict[str, int] = {}

    for lib_id in libs:
        logger.info("[FRCDesignApp] Syncing library '%s'...", lib_id)
        ver_resp = http.get(f"{FRCDESIGN_API_BASE}/library-version/library/{lib_id}", timeout=15).json()
        ver = ver_resp.get("version", 1)

        lib_data = http.get(f"{FRCDESIGN_API_BASE}/library-data/library/{lib_id}?v={ver}", timeout=25).json()
        search_db = http.get(f"{FRCDESIGN_API_BASE}/search-db/library/{lib_id}?v={ver}", timeout=25).json()

        groups_map = lib_data.get("groups", {})
        insertables_map = lib_data.get("insertables", {})
        stored_fields = search_db.get("storedFields", {})

        # Map insertable_id -> stored search record
        search_by_id: Dict[str, Dict[str, Any]] = {}
        for sf_entry in stored_fields.values():
            if isinstance(sf_entry, dict) and sf_entry.get("id"):
                search_by_id[sf_entry["id"]] = sf_entry

        lib_count = 0
        for ins_id, ins in insertables_map.items():
            grp = groups_map.get(ins.get("groupId", ""), {})
            sf_entry = search_by_id.get(ins_id, {})
            records = sf_entry.get("records", [])
            vendors = ins.get("vendors") or sf_entry.get("vendors") or []
            params = []

            should_fetch_params = ins.get("isConfigurable") and (
                fetch_all_parameters or (fetch_ftc_parameters and lib_id == "ftc-design-lib")
            )
            if should_fetch_params:
                mv = ins.get("microversionId", "1")
                try:
                    c_resp = http.get(
                        f"{FRCDESIGN_API_BASE}/configuration/insertable/{ins_id}?v={mv}",
                        timeout=12,
                    ).json()
                    params = c_resp.get("parameters", [])
                    if c_resp.get("records"):
                        records = c_resp["records"]
                except Exception as exc:
                    logger.warning("Could not fetch parameters for %s (%s): %s", ins.get("name"), ins_id, exc)

            primary_vendor = (vendors[0] if vendors else lib_id).lower()
            prog = classify_program(primary_vendor, None, ins.get("name", ""), library_id=lib_id)

            conn.execute(
                """
                INSERT INTO onshape_insertables (
                    insertable_id, program, library_id, group_id, group_name, name,
                    document_id, version_id, element_id, microversion_id,
                    element_type, supports_fasten, is_configurable,
                    vendors_json, parameters_json, records_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(insertable_id) DO UPDATE SET
                    program = excluded.program,
                    library_id = excluded.library_id,
                    group_id = excluded.group_id,
                    group_name = excluded.group_name,
                    name = excluded.name,
                    document_id = excluded.document_id,
                    version_id = excluded.version_id,
                    element_id = excluded.element_id,
                    microversion_id = excluded.microversion_id,
                    element_type = excluded.element_type,
                    supports_fasten = excluded.supports_fasten,
                    is_configurable = excluded.is_configurable,
                    vendors_json = excluded.vendors_json,
                    parameters_json = COALESCE(excluded.parameters_json, onshape_insertables.parameters_json),
                    records_json = excluded.records_json,
                    updated_at = excluded.updated_at
                """,
                (
                    ins_id,
                    prog,
                    lib_id,
                    ins.get("groupId"),
                    grp.get("name") or sf_entry.get("groupName") or "",
                    ins.get("name", ""),
                    ins.get("documentId", ""),
                    ins.get("versionId", ""),
                    ins.get("elementId", ""),
                    ins.get("microversionId", ""),
                    ins.get("elementType", "PARTSTUDIO"),
                    1 if ins.get("supportsFasten") else 0,
                    1 if ins.get("isConfigurable") else 0,
                    json.dumps(vendors),
                    json.dumps(params) if params else None,
                    json.dumps(records),
                    utc_now_iso(),
                ),
            )
            lib_count += 1

        conn.commit()
        counts[lib_id] = lib_count
        logger.info("[FRCDesignApp] Indexed %d insertables for '%s'.", lib_count, lib_id)

    rebuild_frc_ftc_index(conn)
    return counts


# ==============================================================================
# 2. Existing Local COTS Cataloger + Cleanup
# ==============================================================================


def catalog_existing_files(conn: sqlite3.Connection, fingerprint: bool = True) -> int:
    """
    Scan `cots/` (preserving `gobilda-and-fasteners`, `servos-and-hardware`, `swerve-cots`,
    and `vendors/`), validate every `.step`/`.stp` file, and record its B-Rep fingerprint.
    """
    from concurrent.futures import ProcessPoolExecutor

    seen_resolved: Set[str] = set()
    existing_by_path: Dict[str, sqlite3.Row] = {}
    used_part_ids: Dict[str, str] = {}
    for r in conn.execute(
        "SELECT part_id, local_path, file_size_bytes, solid_count, bbox_min_mm, primary_axis FROM local_cots_parts WHERE local_path IS NOT NULL"
    ).fetchall():
        lp_norm = str(r["local_path"]).lower()
        existing_by_path[lp_norm] = r
        used_part_ids[str(r["part_id"]).lower()] = lp_norm

    pending_rows: List[Tuple[Path, Dict[str, Any], bool]] = []
    for p in sorted(COTS_DIR.rglob("*")):
        if not p.is_file() or "solidcraft" in p.parts:
            continue
        if p.suffix.lower() not in (".step", ".stp") or p.name.startswith("~$"):
            continue
        resolved_key = str(p.resolve()).lower()
        if resolved_key in seen_resolved:
            continue
        seen_resolved.add(resolved_key)

        if not validate_step_file(p):
            logger.warning("Skipping invalid/non-STEP file: %s", p)
            continue

        rel_path = p.relative_to(COTS_DIR).as_posix()
        rel_low = rel_path.lower()
        parts = rel_path.split("/")
        if parts[0] == "vendors" and len(parts) >= 3:
            vendor = parts[1].lower()
            category = parts[2].lower()
        else:
            vendor = parts[0].lower()
            category = normalize_category(rel_path, p.stem)

        gb_skus = re.findall(r"\b(\d{4}-\d{4}-\d{4})\b", p.stem)
        sku = gb_skus[-1] if gb_skus else extract_sku(p.stem, rel_path)
        existing = existing_by_path.get(rel_low)
        if existing:
            part_id = str(existing["part_id"])
        else:
            candidate_id = f"{vendor}:{sku or p.stem}"
            if candidate_id.lower() in used_part_ids and used_part_ids[candidate_id.lower()] != rel_low:
                part_id = f"{vendor}:{p.stem}"
            else:
                part_id = candidate_id
            used_part_ids[part_id.lower()] = rel_low

        fsize = p.stat().st_size
        needs_fp = bool(
            fingerprint
            and (
                not existing
                or existing["file_size_bytes"] != fsize
                or existing["solid_count"] is None
                or existing["bbox_min_mm"] is None
                or existing["primary_axis"] is None
            )
        )
        human_stem = p.stem.replace("_", " ").strip()
        base_row: Dict[str, Any] = {
            "part_id": part_id,
            "program": classify_program(vendor, sku, human_stem),
            "vendor": vendor,
            "sku": sku,
            "title": human_stem,
            "category": category,
            "product_url": None,
            "cad_url": None,
            "local_path": rel_path,
            "status": "VERIFIED_STEP",
            "file_size_bytes": fsize,
        }
        pending_rows.append((p, base_row, needs_fp))

    to_fp_paths = [p for p, _, needs_fp in pending_rows if needs_fp]
    fp_results: Dict[str, Optional[Dict[str, Any]]] = {}
    if to_fp_paths:
        logger.info("Fingerprinting %d unindexed STEP files across worker pool...", len(to_fp_paths))
        with ProcessPoolExecutor(max_workers=8) as pool:
            for p_item, fp_res in zip(to_fp_paths, pool.map(fingerprint_step_file, to_fp_paths, chunksize=8)):
                fp_results[str(p_item)] = fp_res

    valid_count = 0
    for p, row, needs_fp in pending_rows:
        if needs_fp:
            fp_data = fp_results.get(str(p))
            if fp_data:
                row.update(fp_data)
        upsert_local_part(conn, row)
        valid_count += 1

    conn.commit()
    rebuild_frc_ftc_index(conn)
    export_manifest_json(conn)
    logger.info("Cataloged %d verified local STEP files.", valid_count)
    return valid_count


# ==============================================================================
# 3. Download & Extraction Engine (STEP / ZIP / Google Drive)
# ==============================================================================


def save_step_or_zip_bytes(
    content: bytes,
    source_url: str,
    dest_dir: Path,
    base_stem: str,
) -> List[Path]:
    """
    Given raw bytes from a STEP or ZIP download, validate `ISO-10303-21` header
    and write one or more `.step` files into `dest_dir`. Never writes HTML/garbage.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []

    # Check if ZIP archive (PK\x03\x04 magic bytes)
    if content[:4] == b"PK\x03\x04":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path, "r") as zf:
                step_members = [
                    m for m in zf.namelist()
                    if m.lower().endswith((".step", ".stp")) and not Path(m).name.startswith("._")
                ]
                for idx, member in enumerate(step_members):
                    raw_step = zf.read(member)
                    if not is_valid_step_bytes(raw_step):
                        continue
                    member_stem = sanitize_slug(Path(member).stem)
                    out_stem = base_stem if len(step_members) == 1 else f"{base_stem}_{member_stem}"
                    out_path = dest_dir / f"{out_stem}.step"
                    out_path.write_bytes(raw_step)
                    saved.append(out_path)
        except Exception as exc:
            logger.warning("Invalid zip archive from %s: %s", source_url, exc)
        finally:
            tmp_path.unlink(missing_ok=True)
    elif is_valid_step_bytes(content):
        out_path = dest_dir / f"{base_stem}.step"
        out_path.write_bytes(content)
        saved.append(out_path)
    else:
        logger.warning("Rejected non-STEP payload from %s (head=%r)", source_url, content[:40])

    return saved


def list_gdrive_folder_step_files(
    http: PoliteHttpClient,
    folder_id: str,
    max_depth: int = 2,
) -> List[Tuple[str, str]]:
    """
    Recursively list `(filename, file_id)` for all `.step`/`.stp` files inside a
    public Google Drive folder using `embeddedfolderview`.
    """
    if max_depth < 0:
        return []
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    results: List[Tuple[str, str]] = []
    try:
        resp = http.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        for entry in soup.select(".flip-entry"):
            eid = entry.get("id", "").replace("entry-", "").strip()
            if not eid:
                continue
            title_el = entry.select_one(".flip-entry-title")
            fname = title_el.text.strip() if title_el else eid
            a_el = entry.select_one("a[href]")
            href = a_el["href"] if a_el else ""
            if "drive/folders/" in href:
                results.extend(list_gdrive_folder_step_files(http, eid, max_depth=max_depth - 1))
            elif fname.lower().endswith((".step", ".stp", ".zip")):
                results.append((fname, eid))
    except Exception as exc:
        logger.warning("Failed listing Google Drive folder %s: %s", folder_id, exc)
    return results


# ==============================================================================
# 4. Vendor Scrapers (Swyft Robotics, REV Robotics, goBILDA)
# ==============================================================================


def discover_swyft(conn: sqlite3.Connection, http: PoliteHttpClient) -> int:
    """Discover all Swyft Robotics direct STEP/ZIP links and Google Drive CAD folders."""
    logger.info("[Swyft] Fetching sitemap...")
    resp = http.get("https://swyftrobotics.com/sitemap.xml", timeout=15)
    root = ET.fromstring(resp.content)
    urls = [
        e.text for e in root.iter()
        if e.tag.endswith("loc") and e.text and any(c in e.text for c in ["/motion/", "/structure/", "/electrical/", "/kits/"])
    ]

    discovered = 0
    for pu in urls:
        try:
            pr = http.get(pu, timeout=12)
            soup = BeautifulSoup(pr.text, "html.parser")
            h1 = soup.find("h1")
            page_title = h1.text.strip() if h1 else pu.rstrip("/").split("/")[-1]
            raw_cat = urlparse(pu).path.strip("/").split("/")[0]

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                label = a.text.strip()
                full_url = urljoin(pu, href)
                low_url = full_url.lower()

                if any(d in low_url for d in NON_CAD_DOMAINS):
                    continue
                if "lowexposureswyftusbcameraserial" in low_url:
                    continue

                # Case 1: Google Drive Folder link
                m_folder = re.search(r"drive\.google\.com/drive/folders/([A-Za-z0-9_\-]+)", full_url)
                if m_folder:
                    fid = m_folder.group(1)
                    gdrive_files = list_gdrive_folder_step_files(http, fid)
                    for fname, file_id in gdrive_files:
                        dl_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
                        sku = extract_sku(fname, fname) or extract_sku(page_title, fname)
                        stem = sanitize_slug(Path(fname).stem)
                        part_id = f"swyft:{sku or stem}"
                        cat = normalize_category(raw_cat, f"{page_title} {fname}")
                        upsert_local_part(
                            conn,
                            {
                                "part_id": part_id,
                                "vendor": "swyft",
                                "sku": sku,
                                "title": f"{page_title} - {Path(fname).stem}",
                                "category": cat,
                                "product_url": pu,
                                "cad_url": dl_url,
                                "status": "DISCOVERED",
                            },
                        )
                        discovered += 1
                    continue

                # Case 2: Google Drive single file link
                m_file = re.search(r"drive\.google\.com/file/d/([A-Za-z0-9_\-]+)", full_url)
                if m_file and "cad" in label.lower():
                    file_id = m_file.group(1)
                    dl_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
                    sku = extract_sku(page_title, full_url)
                    stem = sanitize_slug(f"{sku}_{page_title}" if sku else page_title)
                    part_id = f"swyft:{sku or stem}"
                    upsert_local_part(
                        conn,
                        {
                            "part_id": part_id,
                            "vendor": "swyft",
                            "sku": sku,
                            "title": page_title,
                            "category": normalize_category(raw_cat, page_title),
                            "product_url": pu,
                            "cad_url": dl_url,
                            "status": "DISCOVERED",
                        },
                    )
                    discovered += 1
                    continue

                # Case 3: Direct .step / .stp / .zip on Shopify CDN
                if any(ext in low_url for ext in [".step", ".stp", ".zip"]):
                    if any(low_url.endswith(bad) or f"{bad}?" in low_url for bad in NON_CAD_EXTS):
                        continue
                    url_path_stem = Path(urlparse(full_url).path).stem
                    # Strip UUID suffix if Shopify appended one
                    url_path_stem = re.sub(r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "", url_path_stem, flags=re.I)
                    sku = extract_sku(url_path_stem, full_url) or extract_sku(page_title, label)
                    stem = sanitize_slug(url_path_stem)
                    part_id = f"swyft:{sku or stem}"
                    title = f"{page_title} ({label})" if label and label.lower() != "cad" else f"{page_title} - {url_path_stem}"
                    upsert_local_part(
                        conn,
                        {
                            "part_id": part_id,
                            "vendor": "swyft",
                            "sku": sku,
                            "title": title,
                            "category": normalize_category(raw_cat, title),
                            "product_url": pu,
                            "cad_url": full_url,
                            "status": "DISCOVERED",
                        },
                    )
                    discovered += 1
        except Exception as exc:
            logger.warning("[Swyft] Error on %s: %s", pu, exc)

    conn.commit()
    logger.info("[Swyft] Discovered %d downloadable CAD items.", discovered)
    return discovered


def discover_rev(conn: sqlite3.Connection, http: PoliteHttpClient, limit: Optional[int] = None) -> int:
    """Discover REV Robotics STEP/ZIP files from product sitemap."""
    logger.info("[REV] Fetching product sitemap...")
    resp = http.get("https://www.revrobotics.com/xmlsitemap.php?type=products&page=1", timeout=15)
    root = ET.fromstring(resp.content)
    urls = [e.text for e in root.iter() if e.tag.endswith("loc") and e.text]
    if limit:
        urls = urls[:limit]

    logger.info("[REV] Scanning %d product pages...", len(urls))
    discovered = 0
    for idx, pu in enumerate(urls, 1):
        try:
            pr = http.get(pu, timeout=15)
            soup = BeautifulSoup(pr.text, "html.parser")
            title_el = soup.select_one("h1.productView-title")
            page_title = title_el.text.strip() if title_el else pu.rstrip("/").split("/")[-1]
            bcs = " ".join(a.text.strip() for a in soup.select(".breadcrumbs .breadcrumb a"))

            for a in soup.select("a[href]"):
                href = a["href"].strip()
                label = a.text.strip()
                if not href or href.startswith("javascript"):
                    continue
                full_url = urljoin("https://www.revrobotics.com", href)
                # Normalize revrobotics.com -> www.revrobotics.com
                full_url = re.sub(r"^https?://revrobotics\.com/", "https://www.revrobotics.com/", full_url)
                low_url = full_url.lower()
                low_label = label.lower()

                if low_url.endswith((".step", ".stp", ".zip")) or "content/cad/" in low_url:
                    if any(low_url.endswith(bad) for bad in NON_CAD_EXTS):
                        continue
                    if "step" not in low_label and not low_url.endswith((".step", ".stp")) and "all-parts" not in low_url and "cad" not in low_url:
                        continue
                    asset_stem = sanitize_slug(Path(urlparse(full_url).path).stem)
                    sku = extract_sku(asset_stem, full_url) or extract_sku(page_title, pu)
                    part_id = f"rev:{asset_stem}"
                    title = f"{page_title} ({label})" if label and "step" not in label.lower() else f"{page_title} [{asset_stem}]"
                    upsert_local_part(
                        conn,
                        {
                            "part_id": part_id,
                            "vendor": "rev",
                            "sku": sku,
                            "title": title,
                            "category": normalize_category(bcs, title),
                            "product_url": pu,
                            "cad_url": full_url,
                            "status": "DISCOVERED",
                        },
                    )
                    discovered += 1
        except Exception as exc:
            logger.warning("[REV] Error on %s: %s", pu, exc)
        if idx % 25 == 0:
            conn.commit()
            logger.info("[REV] Progress: %d/%d pages scanned (%d CAD links found)", idx, len(urls), discovered)

    conn.commit()
    logger.info("[REV] Discovery complete: %d CAD links recorded.", discovered)
    return discovered


def discover_gobilda_via_categories(
    conn: sqlite3.Connection,
    http: PoliteHttpClient,
    limit_categories: Optional[int] = None,
) -> int:
    """
    Fast goBILDA discovery via category pages: each category page exposes 10-50
    product cards (`a[href]` with 4-4-4 SKU), and goBILDA STEP archives follow
    `https://www.gobilda.com/content/step_files/<SKU>.zip`.
    """
    logger.info("[goBILDA] Fetching category sitemap...")
    resp = http.get("https://www.gobilda.com/xmlsitemap.php?type=categories&page=1", timeout=15)
    root = ET.fromstring(resp.content)
    cat_urls = [e.text for e in root.iter() if e.tag.endswith("loc") and e.text]

    # Filter out merch/apparel/gift-certificates/tools categories
    skip_slugs = ("apparel", "merch", "swag", "gift", "sticker", "banner", "poster", "lubricant", "loctite", "hand-tools", "charger", "battery", "discontinued")
    cat_urls = [u for u in cat_urls if not any(s in u.lower() for s in skip_slugs)]

    if limit_categories:
        cat_urls = cat_urls[:limit_categories]

    logger.info("[goBILDA] Scanning %d engineering category pages...", len(cat_urls))
    discovered = 0
    seen_skus: Set[str] = set()

    for idx, cu in enumerate(cat_urls, 1):
        try:
            cr = http.get(cu, timeout=15)
            soup = BeautifulSoup(cr.text, "html.parser")
            bcs = " ".join(a.text.strip() for a in soup.select(".breadcrumbs .breadcrumb a"))
            cat_title_el = soup.find("h1")
            cat_title = cat_title_el.text.strip() if cat_title_el else cu.rstrip("/").split("/")[-1]

            # Extract product cards in main content area
            main_el = soup.select_one("main, #main-content, .body") or soup
            for a in main_el.find_all("a", href=True):
                txt = " ".join(a.text.split())
                m = re.search(r"\b(\d{4}-\d{4}-\d{4})\b", txt)
                if not m:
                    continue
                sku = m.group(1)
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)

                prod_url = urljoin("https://www.gobilda.com", a["href"])
                slug_title = urlparse(prod_url).path.strip("/").replace("-", " ").title()
                title = slug_title if len(slug_title) > 8 else f"{cat_title} ({sku})"
                cad_url = f"https://www.gobilda.com/content/step_files/{sku}.zip"

                upsert_local_part(
                    conn,
                    {
                        "part_id": f"gobilda:{sku}",
                        "vendor": "gobilda",
                        "sku": sku,
                        "title": title,
                        "category": normalize_category(f"{bcs} {cat_title}", title),
                        "product_url": prod_url,
                        "cad_url": cad_url,
                        "status": "DISCOVERED",
                    },
                )
                discovered += 1
        except Exception as exc:
            logger.warning("[goBILDA] Error on category %s: %s", cu, exc)

        if idx % 25 == 0:
            conn.commit()
            logger.info("[goBILDA] Progress: %d/%d categories (%d unique SKUs)", idx, len(cat_urls), discovered)

    conn.commit()
    logger.info("[goBILDA] Category discovery complete: %d unique SKUs recorded.", discovered)
    return discovered


def download_discovered_parts(
    conn: sqlite3.Connection,
    http: PoliteHttpClient,
    vendor: Optional[str] = None,
    category: Optional[str] = None,
    limit: Optional[int] = None,
    fingerprint: bool = True,
) -> Dict[str, int]:
    """Download pending `DISCOVERED` parts from SQLite catalog, validate STEP, and fingerprint."""
    sql = "SELECT * FROM local_cots_parts WHERE status = 'DISCOVERED' AND cad_url IS NOT NULL"
    params: List[Any] = []
    if vendor and vendor != "all":
        sql += " AND vendor = ?"
        params.append(vendor.lower())
    if category:
        sql += " AND category = ?"
        params.append(category.lower())
    sql += " ORDER BY vendor, category, part_id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    logger.info("Downloading %d pending CAD items (vendor=%s, category=%s)...", len(rows), vendor or "all", category or "all")

    stats = {"downloaded": 0, "skipped_existing": 0, "no_cad_or_invalid": 0, "failed": 0}

    for idx, r in enumerate(rows, 1):
        part_id = r["part_id"]
        v_name = r["vendor"]
        cat = r["category"]
        sku = r["sku"]
        cad_url = r["cad_url"]
        title = r["title"]

        dest_dir = VENDORS_DIR / v_name / cat
        url_stem = sanitize_slug(Path(urlparse(cad_url).path).stem)
        url_stem = re.sub(r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", "", url_stem, flags=re.I)
        if sku and not url_stem.upper().startswith(sku.upper()):
            base_stem = sanitize_slug(f"{sku}_{url_stem}")
        else:
            base_stem = sanitize_slug(url_stem if url_stem != "download" else f"{sku or ''}_{title}")

        expected_file = dest_dir / f"{base_stem}.step"
        if validate_step_file(expected_file):
            rel_path = expected_file.relative_to(COTS_DIR).as_posix()
            fp = fingerprint_step_file(expected_file) if fingerprint else None
            upd = {
                "part_id": part_id,
                "vendor": v_name,
                "sku": sku,
                "title": title,
                "category": cat,
                "local_path": rel_path,
                "status": "VERIFIED_STEP",
                "file_size_bytes": expected_file.stat().st_size,
            }
            if fp:
                upd.update(fp)
            upsert_local_part(conn, upd)
            stats["skipped_existing"] += 1
            continue

        try:
            resp = http.get(cad_url, timeout=25, max_retries=2)
            saved_files = save_step_or_zip_bytes(resp.content, cad_url, dest_dir, base_stem)
            if saved_files:
                primary = saved_files[0]
                rel_path = primary.relative_to(COTS_DIR).as_posix()
                fp = fingerprint_step_file(primary) if fingerprint else None
                upd = {
                    "part_id": part_id,
                    "vendor": v_name,
                    "sku": sku,
                    "title": title,
                    "category": cat,
                    "local_path": rel_path,
                    "status": "VERIFIED_STEP",
                    "file_size_bytes": primary.stat().st_size,
                }
                if fp:
                    upd.update(fp)
                upsert_local_part(conn, upd)
                stats["downloaded"] += 1
                logger.info(
                    "  [%d/%d] [OK] %s -> %s (%.1f KB)",
                    idx,
                    len(rows),
                    part_id,
                    rel_path,
                    primary.stat().st_size / 1024.0,
                )
            else:
                conn.execute(
                    "UPDATE local_cots_parts SET status = 'NO_CAD', updated_at = ? WHERE part_id = ?",
                    (utc_now_iso(), part_id),
                )
                stats["no_cad_or_invalid"] += 1
        except Exception as exc:
            err_str = str(exc)
            new_status = "NO_CAD" if "404" in err_str else "FAILED"
            conn.execute(
                "UPDATE local_cots_parts SET status = ?, updated_at = ? WHERE part_id = ?",
                (new_status, utc_now_iso(), part_id),
            )
            if new_status == "NO_CAD":
                stats["no_cad_or_invalid"] += 1
            else:
                stats["failed"] += 1
                logger.warning("  [%d/%d] [FAIL] %s (%s): %s", idx, len(rows), part_id, cad_url, exc)

        if idx % 10 == 0:
            conn.commit()

    conn.commit()
    rebuild_frc_ftc_index(conn)
    export_manifest_json(conn)
    return stats


# ==============================================================================
# 5. Query & MCP Interface (`cots_search`, `cots_info`, `catalog_status`)
# ==============================================================================


def _format_local_row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    d["name"] = d.get("title", "")
    d["is_valid_step"] = 1 if d.get("status") == "VERIFIED_STEP" else 0
    if d.get("local_path") and not str(d["local_path"]).startswith("cots/"):
        d["cots_rel_path"] = d["local_path"]
        d["local_path"] = f"cots/{d['local_path']}"
    if d.get("solidcraft_path") and not str(d["solidcraft_path"]).startswith("cots/"):
        d["solidcraft_path"] = f"cots/{d['solidcraft_path']}"
    for k in ("bbox_min_mm", "bbox_max_mm", "extents_mm", "center_of_mass_mm", "axis_center_mm"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    if d.get("mounting_holes_json"):
        try:
            d["mounting_holes"] = json.loads(d["mounting_holes_json"])
        except Exception:
            pass
    if d.get("functional_frames_json"):
        try:
            d["functional_frames"] = json.loads(d["functional_frames_json"])
        except Exception:
            pass
    if d.get("solidcraft_frames_json"):
        try:
            d["solidcraft_frames"] = json.loads(d["solidcraft_frames_json"])
        except Exception:
            pass
    if d.get("solidcraft_matrix_json"):
        try:
            d["solidcraft_matrix_3x4"] = json.loads(d["solidcraft_matrix_json"])
        except Exception:
            pass
    d["agent_spec"] = compute_agent_cots_spec(d)
    return d


_MATERIAL_DENSITY_G_CM3: Dict[str, float] = {
    "Aluminum": 2.70,
    "Steel": 7.85,
    "Stainless Steel": 7.93,
    "Brass": 8.50,
    "Nylon": 1.15,
    "Polycarbonate": 1.20,
    "ABS Plastic": 1.05,
}


def compute_agent_cots_spec(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Derive deterministic engineering mating & kinematic metadata for AI agents:
    - `bore_profile`: standardized shaft/bore interface (8mm_REX_hex, 1/2in_hex, 6mm_D, etc.)
    - `mounting_pattern`: standardized bolt pattern (goBILDA_16mm_M4, REV_8mm_M3, MAXPattern_2in, etc.)
    - `axial_stackup_mm`: exact Z-span & thickness in the normalized +Z frame
    - `kinematics`: teeth, pitch/module/DP, and exact pitch_diameter_mm for gears/sprockets/pulleys
    - `mass_estimate_g`: estimated mass in grams from B-Rep volume and assigned material
    """
    sku = str(d.get("sku") or "")
    title = str(d.get("title") or "")
    vendor = str(d.get("vendor") or "").lower()
    combined = f"{sku} {title}".lower()
    min_r = float(d["min_bore_radius_mm"]) if d.get("min_bore_radius_mm") is not None else None

    # 1. Bore / shaft profile classification
    bore_profile = "none"
    if re.search(r"\b(8\s*mm\s*rex|rex\b|7\s*mm\s*hex)", combined):
        bore_profile = "8mm_REX_hex"
    elif re.search(r"\b(1/2\s*(?:in|\")?\s*hex|0\.5\s*in\s*hex|12\.7\s*mm\s*hex)", combined):
        bore_profile = "1/2in_hex"
    elif re.search(r"\b(3/8\s*(?:in|\")?\s*hex|0\.375\s*in\s*hex)", combined):
        bore_profile = "3/8in_hex"
    elif re.search(r"\b5\s*mm\s*hex\b", combined):
        bore_profile = "5mm_hex"
    elif re.search(r"\bmaxspline\b", combined):
        bore_profile = "MAXSpline"
    elif re.search(r"\b6\s*mm\s*d[- ]?(?:shaft|bore)\b", combined):
        bore_profile = "6mm_D_shaft"
    elif min_r is not None and min_r > 0:
        diam = round(2.0 * min_r, 2)
        if abs(diam - 14.0) <= 0.25:
            bore_profile = "14mm_round_bearing_seat"
        elif abs(diam - 32.0) <= 0.35:
            bore_profile = "32mm_round_bearing_seat"
        elif abs(diam - 8.0) <= 0.22:
            bore_profile = "8mm_round_or_REX"
        elif abs(diam - 6.0) <= 0.22:
            bore_profile = "6mm_round"
        elif abs(diam - 4.0) <= 0.25:
            bore_profile = "M4_clearance_4mm"
        elif abs(diam - 3.0) <= 0.25:
            bore_profile = "M3_clearance_3mm"
        else:
            bore_profile = f"round_{diam:g}mm"

    # 2. Mounting bolt pattern classification
    mh = d.get("mounting_holes") or {}
    radii = mh.get("bolt_circle_radii_mm") or []
    hole_cnt = int(mh.get("count") or 0)
    hole_r = mh.get("hole_radius_mm")
    mounting_pattern = "none"
    if any(abs(float(r) - 11.31) <= 0.35 or abs(float(r) - 8.0) <= 0.25 for r in radii) and ("gobilda" in vendor or (hole_r and abs(float(hole_r) - 2.0) <= 0.3)):
        mounting_pattern = "goBILDA_16mm_M4_grid (PCD_22.63mm_or_16mm)"
    elif any(abs(float(r) - 16.0) <= 0.35 for r in radii) and ("gobilda" in vendor or (hole_r and abs(float(hole_r) - 2.0) <= 0.3)):
        mounting_pattern = "goBILDA_32mm_M4_pattern (PCD_32.0mm)"
    elif "maxpattern" in combined or any(abs(float(r) - 25.4) <= 0.35 for r in radii):
        mounting_pattern = "MAXPattern_2in_PCD_50.8mm"
    elif "rev" in vendor and hole_r and abs(float(hole_r) - 1.6) <= 0.25:
        mounting_pattern = f"REV_M3_pattern ({hole_cnt}x_M3)"
    elif hole_cnt > 0 and hole_r:
        mounting_pattern = f"{hole_cnt}x_D{round(2.0 * float(hole_r), 2):g}mm_PCD_{radii[:3]}"

    # 3. Axial Z-stackup in normalized SolidCraft frame (+Z along bore/shaft)
    sc_frames = d.get("solidcraft_frames") or {}
    ext = d.get("extents_mm") or []
    axial_stackup: Dict[str, Any] = {"anchor": d.get("solidcraft_anchor") or "bore_center"}
    b_min = (sc_frames.get("bore_min") or {}).get("origin_mm")
    b_max = (sc_frames.get("bore_max") or {}).get("origin_mm")
    if isinstance(b_min, list) and isinstance(b_max, list) and len(b_min) == 3 and len(b_max) == 3:
        z_lo = round(min(float(b_min[2]), float(b_max[2])), 3)
        z_hi = round(max(float(b_min[2]), float(b_max[2])), 3)
        axial_stackup.update({"z_min_mm": z_lo, "z_max_mm": z_hi, "thickness_mm": round(z_hi - z_lo, 3)})
    elif isinstance(ext, list) and len(ext) == 3:
        axial_stackup["thickness_mm"] = round(min(float(x) for x in ext), 3)

    # 4. Gear / Sprocket / Pulley kinematics
    kinematics: Optional[Dict[str, Any]] = None
    m_teeth = re.search(r"\b(\d{1,3})\s*(?:tooth|teeth|t)\b", combined)
    if m_teeth and any(k in combined for k in ("gear", "pinion", "sprocket", "pulley")):
        teeth = int(m_teeth.group(1))
        kinematics = {"teeth": teeth}
        if re.search(r"mod\s*0[._]8", combined) or sku.startswith(("2302-", "2303-", "2304-", "2305-", "2306-", "2307-", "2308-", "2309-", "2315-")):
            pd = round(0.8 * teeth, 3)
            kinematics.update({"standard": "Mod_0.8_gear", "module_mm": 0.8, "pitch_diameter_mm": pd})
        elif re.search(r"mod\s*1[._]25", combined):
            pd = round(1.25 * teeth, 3)
            kinematics.update({"standard": "Mod_1.25_gear", "module_mm": 1.25, "pitch_diameter_mm": pd})
        elif re.search(r"\b20\s*dp\b", combined):
            pd = round(25.4 * teeth / 20.0, 3)
            kinematics.update({"standard": "20DP_gear", "dp": 20, "pitch_diameter_mm": pd})
        elif re.search(r"\b32\s*dp\b", combined):
            pd = round(25.4 * teeth / 32.0, 3)
            kinematics.update({"standard": "32DP_gear", "dp": 32, "pitch_diameter_mm": pd})
        elif "#25" in combined or "0.25in" in combined or "6.35mm" in combined or "sprocket" in combined:
            import math
            pitch = 6.35 if ("#25" in combined or "0.25" in combined or "gobilda" in vendor or "rev" in vendor) else 8.0
            pd = round(pitch / math.sin(math.pi / max(3, teeth)), 3)
            kinematics.update({"standard": "chain_sprocket", "pitch_mm": pitch, "pitch_diameter_mm": pd})
        elif "htd" in combined and "5mm" in combined:
            import math
            pd = round(5.0 * teeth / math.pi, 3)
            kinematics.update({"standard": "HTD_5mm_belt", "pitch_mm": 5.0, "pitch_diameter_mm": pd})
        elif "gt2" in combined:
            import math
            pitch = 3.0 if "3mm" in combined else 2.0
            pd = round(pitch * teeth / math.pi, 3)
            kinematics.update({"standard": f"GT2_{pitch:g}mm_belt", "pitch_mm": pitch, "pitch_diameter_mm": pd})

    # 5. Mass estimate from B-Rep volume and material
    mat = str(d.get("solidcraft_material") or "Aluminum")
    vol_cm3 = float(d["volume_cm3"]) if d.get("volume_cm3") is not None else 0.0
    dens = _MATERIAL_DENSITY_G_CM3.get(mat, 2.70)
    mass_g = round(vol_cm3 * dens, 2) if vol_cm3 > 0 else None

    spec: Dict[str, Any] = {
        "bore_profile": bore_profile,
        "mounting_pattern": mounting_pattern,
        "axial_stackup_mm": axial_stackup,
        "material": mat,
        "mass_estimate_g": mass_g,
    }
    if kinematics:
        spec["kinematics"] = kinematics
    return spec


_TOKEN_SYNONYMS: Dict[str, Tuple[List[str], Optional[Tuple[float, float]]]] = {
    "1/2": (["1/2", "0.5in", "0.5\"", "05in", "12.7mm", "hex"], (5.5, 7.5)),
    "1/2in": (["1/2", "0.5in", "0.5\"", "05in", "12.7mm", "hex"], (5.5, 7.5)),
    "0.5in": (["1/2", "0.5in", "0.5\"", "05in", "12.7mm", "hex"], (5.5, 7.5)),
    "12.7mm": (["1/2", "0.5in", "05in", "12.7mm"], (5.5, 7.5)),
    "3/8": (["3/8", "0.375", "0375", "9.5mm"], (4.3, 5.6)),
    "8mm": (["8mm", "8 mm", "rex"], (3.75, 4.35)),
    "rex": (["rex", "8mm", "7mm hex"], (3.4, 4.35)),
    "6mm": (["6mm", "6 mm"], (2.75, 3.25)),
    "5mm": (["5mm", "5 mm"], (2.25, 2.75)),
    "yellowjacket": (["yellow jacket", "5202", "5203", "5204"], None),
    "deadwheel": (["deadwheel", "odometry", "omni", "wheeltracker"], None),
    "heatset": (["heatset", "heat-set", "insert", "ruthex"], None),
}


def _score_local_part(
    d: Dict[str, Any],
    query_low: str,
    tokens_low: List[str],
    bore_mm: Optional[float] = None,
) -> float:
    score = 0.0
    pid = str(d.get("part_id") or "").lower()
    sku = str(d.get("sku") or "").lower()
    title = str(d.get("title") or "").lower()
    cat = str(d.get("category") or "").lower()

    if query_low and (sku == query_low or pid == query_low or pid.endswith(f":{query_low}")):
        score += 250.0
    elif query_low and (sku.startswith(query_low) or query_low in sku):
        score += 120.0

    for tok in tokens_low:
        if tok == sku:
            score += 90.0
        elif tok in sku:
            score += 45.0
        if re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", title):
            score += 75.0
        elif tok in title:
            score += 20.0
        if tok in pid:
            score += 20.0
        elif tok in cat:
            score += 6.0

    if bore_mm is not None and d.get("min_bore_radius_mm") is not None:
        r_val = float(d["min_bore_radius_mm"])
        target_r = 0.5 * float(bore_mm)
        err = abs(r_val - target_r)
        score += max(0.0, 120.0 - err * 200.0)

    accessory_words = (
        "horn",
        "hub",
        "block",
        "coupler",
        "spacer",
        "washer",
        "screw",
        "nut",
        "wire",
        "cable",
        "extension",
        "sticker",
        "label",
        "plate",
        "bracket",
        "mount",
        "clamp",
        "arm",
        "adaptor",
        "adapter",
        "magnet",
        "pinion",
        "pulley",
        "hat",
        "conversion",
    )
    user_asked_accessory = any(aw in query_low for aw in accessory_words)

    if any(t in ("servo", "servos") for t in tokens_low) and not user_asked_accessory:
        if any(k in sku or k in title for k in ("2000-0025", "sr-servo", "rev-41-1097", "axon", "smart robot servo", "dual mode servo")):
            score += 110.0
        if any(aw in title for aw in accessory_words) or sku.startswith(("1800-", "1801-", "1802-", "1803-", "1905-", "3217-")):
            score -= 65.0

    if any(t in ("motor", "motors", "neo", "vortex", "kraken", "falcon", "5202", "5203", "5204") for t in tokens_low) and not user_asked_accessory:
        if cat == "motors" or any(k in sku or k in title for k in ("5202-", "5203-", "5204-", "rev-21-1650", "rev-21-1652", "rev-41-1300", "rev-41-1600", "motor6000", "brushless motor", "planetary gear motor")):
            score += 115.0
        if any(aw in title for aw in accessory_words):
            score -= 75.0

    if any(t in ("bearing", "bearings", "brg") for t in tokens_low) and not user_asked_accessory:
        if cat == "bearings" or any(k in sku or k in title for k in ("brg-", "1611-", "1610-", "1612-", "1600-", "rev-21-1915", "rev-21-1916", "rev-21-1914", "rev-41-1317", "flanged bearing", "ball bearing")):
            score += 95.0
        if any(aw in title for aw in ("block", "pillow", "mount", "plate", "hat", "chassis", "drivetrain")):
            score -= 65.0

    if d.get("functional_frames"):
        score += 8.0
    if d.get("product_url"):
        score += 5.0
    return score


def cots_search(
    query: str = "",
    vendor: Optional[str] = None,
    category: Optional[str] = None,
    program: Optional[str] = None,
    source: str = "all",
    bore_mm: Optional[float] = None,
    max_extent_mm: Optional[float] = None,
    has_mounting_holes: bool = False,
    limit: int = 15,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """
    Search verified local STEP models (Route A `build123d`/FreeCAD), native Onshape
    `FRCDesignApp` insertables (`ftc-design-lib`, `frc-design-lib`, `mkcad` for Route B),
    and the unified 10,000+ FRC/FTC `cots_part_variants` SKU index with engineering
    synonym expansion, parametric bore/envelope filtering, and relevance ranking.
    """
    conn = get_db(db_path)
    raw_tokens = [t.strip() for t in query.split() if t.strip()]
    tokens_low = [t.lower() for t in raw_tokens]
    query_low = query.strip().lower()
    prog_up = program.upper() if program and program.lower() != "all" else None

    local_results: List[Dict[str, Any]] = []
    if source in ("all", "local"):
        sql = "SELECT * FROM local_cots_parts WHERE status = 'VERIFIED_STEP'"
        params: List[Any] = []
        if prog_up in ("FTC", "FRC"):
            sql += " AND program IN (?, 'FTC/FRC')"
            params.append(prog_up)
        if vendor:
            sql += " AND LOWER(vendor) LIKE ?"
            params.append(f"%{vendor.lower()}%")
        if category:
            sql += " AND LOWER(category) = ?"
            params.append(category.lower())
        if bore_mm is not None:
            r_min = 0.5 * float(bore_mm) - 0.45
            r_max = 0.58 * float(bore_mm) + 0.45
            sql += " AND min_bore_radius_mm BETWEEN ? AND ?"
            params.extend([r_min, r_max])
        if has_mounting_holes:
            sql += " AND mounting_holes_json IS NOT NULL"

        for tok in tokens_low:
            syn_info = _TOKEN_SYNONYMS.get(tok)
            if syn_info:
                syns, r_bounds = syn_info
                clauses = []
                for s in syns:
                    pat = f"%{s.lower()}%"
                    clauses.append(
                        "(LOWER(part_id) LIKE ? OR LOWER(COALESCE(sku,'')) LIKE ? OR LOWER(title) LIKE ? OR LOWER(category) LIKE ?)"
                    )
                    params.extend([pat, pat, pat, pat])
                if r_bounds is not None:
                    clauses.append("(min_bore_radius_mm BETWEEN ? AND ?)")
                    params.extend([r_bounds[0], r_bounds[1]])
                sql += " AND (" + " OR ".join(clauses) + ")"
            else:
                sql += " AND (LOWER(part_id) LIKE ? OR LOWER(COALESCE(sku,'')) LIKE ? OR LOWER(title) LIKE ? OR LOWER(category) LIKE ?)"
                pat = f"%{tok}%"
                params.extend([pat, pat, pat, pat])

        fetch_cap = max(int(limit) * 15, 250)
        sql += f" LIMIT {fetch_cap}"
        candidates: List[Dict[str, Any]] = []
        for r in conn.execute(sql, params).fetchall():
            d = _format_local_row(r)
            if max_extent_mm is not None and isinstance(d.get("extents_mm"), list) and d["extents_mm"]:
                if max(float(x) for x in d["extents_mm"]) > float(max_extent_mm):
                    continue
            d["_relevance"] = round(_score_local_part(d, query_low, tokens_low, bore_mm=bore_mm), 2)
            candidates.append(d)

        candidates.sort(key=lambda item: (-item["_relevance"], item.get("vendor", ""), item.get("part_id", "")))
        for item in candidates[: int(limit)]:
            item.pop("_relevance", None)
            local_results.append(item)

    onshape_results: List[Dict[str, Any]] = []
    if source in ("all", "onshape"):
        os_sql = "SELECT * FROM onshape_insertables WHERE 1=1"
        os_params: List[Any] = []
        if prog_up in ("FTC", "FRC"):
            os_sql += " AND program IN (?, 'FTC/FRC')"
            os_params.append(prog_up)
        if vendor:
            os_sql += " AND (LOWER(COALESCE(vendors_json,'')) LIKE ? OR LOWER(library_id) LIKE ?)"
            os_params.extend([f"%{vendor.lower()}%", f"%{vendor.lower()}%"])
        if category:
            os_sql += " AND LOWER(COALESCE(group_name,'')) LIKE ?"
            os_params.append(f"%{category.lower()}%")
        for tok in tokens_low:
            syn_info = _TOKEN_SYNONYMS.get(tok)
            if syn_info:
                syns, _ = syn_info
                clauses = []
                for s in syns:
                    pat = f"%{s.lower()}%"
                    clauses.append(
                        "(LOWER(name) LIKE ? OR LOWER(COALESCE(group_name,'')) LIKE ? OR LOWER(COALESCE(records_json,'')) LIKE ? OR LOWER(COALESCE(vendors_json,'')) LIKE ?)"
                    )
                    os_params.extend([pat, pat, pat, pat])
                os_sql += " AND (" + " OR ".join(clauses) + ")"
            else:
                os_sql += " AND (LOWER(name) LIKE ? OR LOWER(COALESCE(group_name,'')) LIKE ? OR LOWER(COALESCE(records_json,'')) LIKE ? OR LOWER(COALESCE(vendors_json,'')) LIKE ?)"
                pat = f"%{tok}%"
                os_params.extend([pat, pat, pat, pat])
        os_sql += f" ORDER BY library_id, name LIMIT {int(limit)}"
        for r in conn.execute(os_sql, os_params).fetchall():
            d = dict(r)
            records = json.loads(d.pop("records_json") or "[]")
            if tokens_low:
                matching_recs = [
                    rec for rec in records
                    if any(
                        tok in f"{rec.get('partNumber','')} {rec.get('name','')}".lower()
                        for tok in tokens_low
                    )
                ]
            else:
                matching_recs = records[:5]
            onshape_results.append(
                {
                    "insertable_id": d["insertable_id"],
                    "program": d.get("program", "FRC"),
                    "library_id": d["library_id"],
                    "group_name": d["group_name"],
                    "name": d["name"],
                    "document_id": d["document_id"],
                    "version_id": d["version_id"],
                    "element_id": d["element_id"],
                    "microversion_id": d["microversion_id"],
                    "element_type": d["element_type"],
                    "supports_fasten": bool(d["supports_fasten"]),
                    "is_configurable": bool(d["is_configurable"]),
                    "vendors": json.loads(d["vendors_json"] or "[]"),
                    "matching_records": matching_recs[:8],
                    "total_records": len(records),
                }
            )

    variant_results: List[Dict[str, Any]] = []
    if source in ("all", "onshape", "variants"):
        v_sql = "SELECT * FROM cots_part_variants WHERE 1=1"
        v_params: List[Any] = []
        if prog_up in ("FTC", "FRC"):
            v_sql += " AND program IN (?, 'FTC/FRC')"
            v_params.append(prog_up)
        if vendor:
            v_sql += " AND (LOWER(vendor) LIKE ? OR LOWER(source_type) LIKE ?)"
            v_params.extend([f"%{vendor.lower()}%", f"%{vendor.lower()}%"])
        if category:
            v_sql += " AND LOWER(category) = ?"
            v_params.append(category.lower())
        for tok in tokens_low:
            syn_info = _TOKEN_SYNONYMS.get(tok)
            if syn_info:
                syns, _ = syn_info
                clauses = []
                for s in syns:
                    pat = f"%{s.lower()}%"
                    clauses.append(
                        "(LOWER(COALESCE(sku,'')) LIKE ? OR LOWER(name) LIKE ? OR LOWER(vendor) LIKE ? OR LOWER(category) LIKE ?)"
                    )
                    v_params.extend([pat, pat, pat, pat])
                v_sql += " AND (" + " OR ".join(clauses) + ")"
            else:
                v_sql += " AND (LOWER(COALESCE(sku,'')) LIKE ? OR LOWER(name) LIKE ? OR LOWER(vendor) LIKE ? OR LOWER(category) LIKE ?)"
                pat = f"%{tok}%"
                v_params.extend([pat, pat, pat, pat])
        v_sql += f" ORDER BY CASE WHEN local_part_id IS NOT NULL THEN 0 ELSE 1 END, program, vendor, name LIMIT {int(limit)}"
        for r in conn.execute(v_sql, v_params).fetchall():
            variant_results.append(dict(r))

    conn.close()
    return {
        "query": query,
        "program_filter": prog_up or "ALL",
        "local_count": len(local_results),
        "onshape_count": len(onshape_results),
        "variant_count": len(variant_results),
        "local_parts": local_results,
        "local_step_parts": local_results,
        "onshape_insertables": onshape_results,
        "part_variants": variant_results,
    }


def cots_info(identifier: str, db_path: Path = DB_PATH) -> Dict[str, Any]:
    """
    Retrieve detailed B-Rep geometry fingerprint + `build123d` import snippet for a local
    part, OR full Onshape configuration parameters & instance payload for an `onshape_insertable`.
    """
    conn = get_db(db_path)
    ident = identifier.strip()

    # 1. Check local_cots_parts by part_id, sku, local_path, or substring match
    row = conn.execute(
        """
        SELECT * FROM local_cots_parts
        WHERE LOWER(part_id) = LOWER(?)
           OR LOWER(COALESCE(sku,'')) = LOWER(?)
           OR LOWER(COALESCE(local_path,'')) = LOWER(?)
           OR LOWER(title) = LOWER(?)
           OR LOWER(part_id) LIKE ?
           OR LOWER(title) LIKE ?
        ORDER BY
            CASE WHEN status = 'VERIFIED_STEP' THEN 0 ELSE 1 END,
            CASE WHEN LOWER(COALESCE(sku,'')) = LOWER(?) THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (ident, ident, ident, ident, f"%{ident.lower()}%", f"%{ident.lower()}%", ident),
    ).fetchone()

    result: Dict[str, Any] = {}
    if row:
        d = _format_local_row(row)
        cat_low = str(d.get("category") or "").lower()
        t_low = f"{d.get('sku') or ''} {d.get('title') or ''} {ident}".lower()
        if cat_low in ("motors", "servos") or any(k in t_low for k in ("servo", "motor", "5202", "5203", "5204", "rev-21-1650")):
            rec_anchor = "output_shaft"
        elif cat_low == "bearings" or any(k in t_low for k in ("bearing", "brg-")):
            rec_anchor = "flange_seat"
        elif any(k in t_low for k in ("sensor", "encoder", "camera", "limelight", "odometry")):
            rec_anchor = "sense_origin"
        else:
            rec_anchor = "bore_center"
        d["recommended_anchor"] = rec_anchor
        if d.get("local_path"):
            abs_p = (WORKSPACE_ROOT / d["local_path"]).as_posix()
            d["build123d_snippet"] = (
                f"from tools.ag_cad_helpers import load_cots_part\n"
                f"part = load_cots_part({(d.get('sku') or ident)!r}, bore_axis='Z', anchor={rec_anchor!r}, at=(0.0, 0.0, 0.0))\n"
                f"# Or directly: import_step({abs_p!r})"
            )
            sc_rel_p = d.get("solidcraft_path") or f"cots/solidcraft/{d.get('cots_rel_path', d['local_path']).rsplit('.', 1)[0]}.sc"
            abs_sc_p = (WORKSPACE_ROOT / sc_rel_p).as_posix()
            d["solidcraft_snippet"] = {
                "mcp_tool": "solidcraft_insert_cots",
                "arguments": {
                    "part_key_or_sku": d.get("sku") or d.get("part_id") or ident,
                    "anchor": rec_anchor,
                },
                "native_command": {
                    "command": "component.insert_design",
                    "params": {"path": abs_sc_p, "reuse": True},
                },
            }
            d["mate_graph_snippet"] = (
                f"# Declarative MateGraphBuilder (build123d + SolidCraft + MJCF)\n"
                f"mg.add_part({(d.get('sku') or 'cots_part')!r}, load_cots_part({(d.get('sku') or ident)!r}, bore_axis='Z', anchor={rec_anchor!r}))\n"
                f"mg.add_connector({(d.get('sku') or 'cots_part')!r}, {rec_anchor!r}, origin=(0.0, 0.0, 0.0), z_axis=(0.0, 0.0, 1.0))"
            )
        result["local"] = d
        result["local_part"] = d

    # 2. Check onshape_insertables by insertable_id, name, or SKU inside records_json
    os_row = conn.execute(
        """
        SELECT * FROM onshape_insertables
        WHERE LOWER(insertable_id) = LOWER(?)
           OR LOWER(name) = LOWER(?)
           OR LOWER(COALESCE(records_json,'')) LIKE ?
        LIMIT 1
        """,
        (ident, ident, f"%{ident.lower()}%"),
    ).fetchone()

    if os_row:
        od = dict(os_row)
        params = json.loads(od.get("parameters_json") or "[]")
        records = json.loads(od.get("records_json") or "[]")
        os_info = {
            "insertable_id": od["insertable_id"],
            "program": od.get("program", "FRC"),
            "library_id": od["library_id"],
            "group_name": od["group_name"],
            "name": od["name"],
            "document_id": od["document_id"],
            "version_id": od["version_id"],
            "element_id": od["element_id"],
            "microversion_id": od["microversion_id"],
            "element_type": od["element_type"],
            "supports_fasten": bool(od["supports_fasten"]),
            "is_configurable": bool(od["is_configurable"]),
            "vendors": json.loads(od["vendors_json"] or "[]"),
            "parameters": params,
            "records": records[:25],
            "onshape_assembly_instance_payload": {
                "documentId": od["document_id"],
                "versionId": od["version_id"],
                "elementId": od["element_id"],
                "isAssembly": od["element_type"] == "ASSEMBLY",
                "isWholePartStudio": od["element_type"] == "PARTSTUDIO",
            },
        }
        result["onshape"] = os_info
        result["onshape_insertable"] = os_info

    # 3. Also include matching rows from cots_part_variants
    var_rows = conn.execute(
        """
        SELECT * FROM cots_part_variants
        WHERE LOWER(COALESCE(sku,'')) = LOWER(?)
           OR LOWER(variant_id) = LOWER(?)
           OR LOWER(name) LIKE ?
        LIMIT 15
        """,
        (ident, ident, f"%{ident.lower()}%"),
    ).fetchall()
    if var_rows:
        result["variants"] = [dict(r) for r in var_rows]

    conn.close()
    if not result:
        return {"error": f"No COTS part or Onshape insertable found matching '{identifier}'"}
    return result


def get_catalog_status(db_path: Path = DB_PATH) -> Dict[str, Any]:
    conn = get_db(db_path)
    local_rows = conn.execute(
        "SELECT vendor, status, COUNT(*) as cnt, SUM(COALESCE(file_size_bytes,0)) as bytes FROM local_cots_parts GROUP BY vendor, status"
    ).fetchall()
    local_prog_rows = conn.execute(
        "SELECT program, status, COUNT(*) as cnt FROM local_cots_parts GROUP BY program, status"
    ).fetchall()
    os_rows = conn.execute(
        "SELECT library_id, program, COUNT(*) as cnt FROM onshape_insertables GROUP BY library_id, program"
    ).fetchall()
    var_rows = conn.execute(
        "SELECT program, source_type, COUNT(*) as cnt FROM cots_part_variants GROUP BY program, source_type"
    ).fetchall()
    conn.close()

    local_summary: Dict[str, Dict[str, Any]] = {}
    total_verified = 0
    total_bytes = 0
    for r in local_rows:
        v = r["vendor"]
        local_summary.setdefault(v, {})[r["status"]] = r["cnt"]
        if r["status"] == "VERIFIED_STEP":
            total_verified += r["cnt"]
            total_bytes += r["bytes"] or 0

    local_by_program: Dict[str, Dict[str, int]] = {}
    for r in local_prog_rows:
        local_by_program.setdefault(r["program"], {})[r["status"]] = r["cnt"]

    variants_by_program: Dict[str, int] = {}
    variants_by_source: Dict[str, int] = {}
    for r in var_rows:
        variants_by_program[r["program"]] = variants_by_program.get(r["program"], 0) + r["cnt"]
        variants_by_source[r["source_type"]] = variants_by_source.get(r["source_type"], 0) + r["cnt"]

    os_summary: Dict[str, int] = {}
    for r in os_rows:
        os_summary[r["library_id"]] = os_summary.get(r["library_id"], 0) + r["cnt"]

    return {
        "database_path": str(db_path),
        "total_verified_step_files": total_verified,
        "total_verified_size_mb": round(total_bytes / (1024 * 1024), 2),
        "local_by_vendor": local_summary,
        "local_by_program": local_by_program,
        "onshape_libraries": os_summary,
        "indexed_variants_by_program": variants_by_program,
        "indexed_variants_by_source": variants_by_source,
    }


# ==============================================================================
# CLI Entrypoint
# ==============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Robotics COTS CAD Scraper & Catalog")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show local STEP & Onshape FRCDesignApp catalog statistics")
    sub.add_parser("rebuild-index", help="Rebuild unified FRC & FTC cots_part_variants index")

    p_os = sub.add_parser("sync-frcdesign", help="Sync FTCDesignLib, FRCDesignLib, and MKCad from app.frcdesign.org")
    p_os.add_argument("--libraries", nargs="+", default=FRCDESIGN_LIBRARIES, choices=FRCDESIGN_LIBRARIES)
    p_os.add_argument("--all-params", action="store_true", help="Also fetch configuration schemas for FRCDesignLib & MKCad")

    p_cat = sub.add_parser("catalog-local", help="Validate and fingerprint all existing .step files in cots/")
    p_cat.add_argument("--no-fingerprint", action="store_true")

    p_disc = sub.add_parser("discover", help="Discover vendor CAD links into cots/catalog.db")
    p_disc.add_argument("--vendor", required=True, choices=["swyft", "rev", "gobilda", "all"])
    p_disc.add_argument("--limit", type=int, default=None)

    p_dl = sub.add_parser("download", help="Download and validate pending STEP files from cots/catalog.db")
    p_dl.add_argument("--vendor", default="all", choices=["swyft", "rev", "gobilda", "all"])
    p_dl.add_argument("--category", default=None)
    p_dl.add_argument("--limit", type=int, default=None)
    p_dl.add_argument("--no-fingerprint", action="store_true")

    p_search = sub.add_parser("search", help="Search local STEP parts, Onshape insertables, and FRC/FTC variants")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--vendor", default=None)
    p_search.add_argument("--category", default=None)
    p_search.add_argument("--program", default=None, choices=["FTC", "FRC", "ftc", "frc", "all"])
    p_search.add_argument("--source", default="all", choices=["all", "local", "onshape", "variants"])
    p_search.add_argument("--limit", type=int, default=15)
    p_search.add_argument("--bore", type=float, default=None, help="Filter local parts by bore diameter in mm")
    p_search.add_argument("--max-extent", type=float, default=None, help="Filter local parts by maximum bounding box dimension in mm")
    p_search.add_argument("--has-mounting-holes", action="store_true", default=None, help="Require detected mounting holes in functional_frames_json")

    p_info = sub.add_parser("info", help="Show detailed geometry fingerprint or Onshape parameters for a SKU/ID")
    p_info.add_argument("identifier")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = get_db(DB_PATH)
    http = PoliteHttpClient(min_interval_sec=0.25)

    if args.cmd == "status":
        print(json.dumps(get_catalog_status(DB_PATH), indent=2))
    elif args.cmd == "rebuild-index":
        res = rebuild_frc_ftc_index(conn)
        print(json.dumps(res, indent=2))
    elif args.cmd == "sync-frcdesign":
        res = sync_frcdesign_libraries(conn, http, libraries=args.libraries, fetch_all_parameters=args.all_params)
        print(json.dumps(res, indent=2))
    elif args.cmd == "catalog-local":
        cnt = catalog_existing_files(conn, fingerprint=not args.no_fingerprint)
        print(json.dumps({"cataloged_verified_step_files": cnt}, indent=2))
    elif args.cmd == "discover":
        vendors = ["swyft", "rev", "gobilda"] if args.vendor == "all" else [args.vendor]
        out = {}
        for v in vendors:
            if v == "swyft":
                out["swyft"] = discover_swyft(conn, http)
            elif v == "rev":
                out["rev"] = discover_rev(conn, http, limit=args.limit)
            elif v == "gobilda":
                out["gobilda"] = discover_gobilda_via_categories(conn, http, limit_categories=args.limit)
        rebuild_frc_ftc_index(conn)
        print(json.dumps(out, indent=2))
    elif args.cmd == "download":
        stats = download_discovered_parts(
            conn,
            http,
            vendor=args.vendor,
            category=args.category,
            limit=args.limit,
            fingerprint=not args.no_fingerprint,
        )
        print(json.dumps(stats, indent=2))
    elif args.cmd == "search":
        print(
            json.dumps(
                cots_search(
                    args.query,
                    vendor=args.vendor,
                    category=args.category,
                    program=args.program,
                    source=args.source,
                    limit=args.limit,
                    bore_mm=args.bore,
                    max_extent_mm=args.max_extent,
                    has_mounting_holes=args.has_mounting_holes,
                ),
                indent=2,
            )
        )
    elif args.cmd == "info":
        print(json.dumps(cots_info(args.identifier), indent=2))


if __name__ == "__main__":
    main()
