"""
Team CAD Sharing & Zero-Install Interactive 3D WebGL Viewer Packager
====================================================================
Packages any robotics CAD project or `.step` / `.sc` deliverable into:
1. `<name>.glb` — Multi-node binary glTF 2.0 scene exported in-process via OCCT 8.0
   with preserved part names, occurrence transforms, and per-part colors.
2. `<name>_team_viewer.html` — Zero-install, double-clickable interactive 3D WebGL
   engineering viewer (Orbit/Pan/Zoom, Isometric/Ortho presets, radial Explode slider,
   real-time X/Y/Z Section Cut plane, Click-to-Inspect part dimensions, searchable
   Part Tree with Show/Hide/Isolate, and integrated BOM/Mass/Verification HUD).
   Embeds base64 `.glb` automatically up to 35 MiB so teammates can open it directly
   from Discord, Slack, Google Drive, or local disk without running a web server,
   while also supporting GitHub Pages static hosting.
3. `<name>_team_bundle.zip` — Clean, organized team handoff archive (`viewer/`,
   `cad/`, `fabrication/prints/`, `fabrication/laser/`, `docs/README_TEAM.md`) ready
   for GitHub Releases (`gh release create`), Slack/Discord, or shop handoff.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import zipfile

import numpy as np
import trimesh


_FALLBACK_PALETTE: List[Tuple[int, int, int]] = [
    (72, 138, 255),
    (255, 167, 38),
    (102, 187, 106),
    (171, 71, 188),
    (38, 198, 218),
    (239, 83, 80),
    (255, 202, 40),
    (141, 110, 99),
    (120, 144, 156),
]


def _tessellate_shape_to_trimesh(
    shape: Any,
    linear_deflection: float = 0.22,
    angular_deflection: float = 0.35,
) -> Optional[trimesh.Trimesh]:
    """Tessellate an OCCT `TopoDS_Shape` into a `trimesh.Trimesh` in-process."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    face_cast = getattr(TopoDS, "Face", None) or getattr(TopoDS, "Face_s")
    BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection, True)

    verts_chunks: List[np.ndarray] = []
    faces_chunks: List[np.ndarray] = []
    v_offset = 0

    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = face_cast(exp.Current())
        exp.Next()
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        nb_nodes = tri.NbNodes()
        nb_tris = tri.NbTriangles()
        if nb_nodes == 0 or nb_tris == 0:
            continue

        trsf = loc.Transformation()
        is_identity = loc.IsIdentity()
        pts = np.empty((nb_nodes, 3), dtype=np.float64)
        for i in range(1, nb_nodes + 1):
            p = tri.Node(i)
            if not is_identity:
                p = p.Transformed(trsf)
            pts[i - 1, 0] = p.X()
            pts[i - 1, 1] = p.Y()
            pts[i - 1, 2] = p.Z()

        reversed_orient = face.Orientation() == TopAbs_REVERSED
        tris = np.empty((nb_tris, 3), dtype=np.int64)
        for i in range(1, nb_tris + 1):
            t = tri.Triangle(i)
            n1, n2, n3 = t.Get()
            if reversed_orient:
                tris[i - 1] = (n1 - 1 + v_offset, n3 - 1 + v_offset, n2 - 1 + v_offset)
            else:
                tris[i - 1] = (n1 - 1 + v_offset, n2 - 1 + v_offset, n3 - 1 + v_offset)

        verts_chunks.append(pts)
        faces_chunks.append(tris)
        v_offset += nb_nodes

    if not verts_chunks:
        return None
    vertices = np.vstack(verts_chunks)
    faces = np.vstack(faces_chunks)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def export_step_to_glb(
    step_path: Path,
    out_glb_path: Path,
    linear_deflection: float = 0.25,
) -> Dict[str, Any]:
    """
    Read a multi-part `.step` (or `.step.gz`) assembly in-process via `build123d.import_step`
    (OCCT 8.0 XCAF), preserve part names and RGBA colors, write a binary `.glb`, and return
    per-part metadata for the WebGL HUD.
    """
    import gzip
    import tempfile
    from build123d import import_step
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    actual_path = Path(step_path).resolve()
    tmp_unzipped: Optional[Path] = None
    if actual_path.name.lower().endswith(".step.gz"):
        with tempfile.NamedTemporaryFile("wb", suffix=".step", delete=False) as tf:
            with gzip.open(actual_path, "rb") as gz:
                tf.write(gz.read())
            tmp_unzipped = Path(tf.name)
        actual_path = tmp_unzipped

    def _extract_rgb(obj: Any, idx: int) -> Tuple[int, int, int]:
        col = getattr(obj, "color", None)
        if col is not None:
            try:
                rgba_tup = tuple(col)
                if len(rgba_tup) >= 3:
                    vals = [float(x) for x in rgba_tup[:3]]
                    if max(vals) <= 1.001:
                        return (int(round(vals[0] * 255)), int(round(vals[1] * 255)), int(round(vals[2] * 255)))
                    return (int(round(vals[0])), int(round(vals[1])), int(round(vals[2])))
            except Exception:
                pass
        return _FALLBACK_PALETTE[idx % len(_FALLBACK_PALETTE)]

    leaf_items: List[Tuple[str, Any, Tuple[int, int, int]]] = []
    try:
        root = import_step(str(actual_path))
        children = list(getattr(root, "children", []) or [])
        if len(children) >= 2:
            for idx, ch in enumerate(children):
                sols = list(ch.solids())
                if sols:
                    lbl = str(getattr(ch, "label", "") or "").strip() or f"{step_path.stem}_Part_{idx + 1}"
                    leaf_items.append((lbl, ch.wrapped, _extract_rgb(ch, idx)))
        if not leaf_items:
            sols = list(root.solids())
            base_lbl = str(getattr(root, "label", "") or "").strip() or step_path.stem
            if len(sols) == 1:
                leaf_items.append((base_lbl, sols[0].wrapped, _extract_rgb(root, 0)))
            else:
                for idx, sol in enumerate(sols):
                    lbl = str(getattr(sol, "label", "") or "").strip() or f"{base_lbl}_{idx + 1}"
                    leaf_items.append((lbl, sol.wrapped, _extract_rgb(sol, idx)))
    finally:
        if tmp_unzipped is not None:
            tmp_unzipped.unlink(missing_ok=True)

    scene = trimesh.Scene()
    parts_meta: List[Dict[str, Any]] = []
    used_node_names: Dict[str, int] = {}

    all_mins: List[List[float]] = []
    all_maxs: List[List[float]] = []
    total_vol_cm3 = 0.0

    for idx, (raw_name, shape, rgb) in enumerate(leaf_items):
        mesh = _tessellate_shape_to_trimesh(shape, linear_deflection=linear_deflection)
        if mesh is None or len(mesh.faces) == 0:
            continue

        base_node = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in raw_name).strip("_") or f"part_{idx + 1}"
        cnt = used_node_names.get(base_node, 0) + 1
        used_node_names[base_node] = cnt
        node_name = base_node if cnt == 1 else f"{base_node}_{cnt}"

        rgba = np.array([rgb[0], rgb[1], rgb[2], 255], dtype=np.uint8)
        mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=np.tile(rgba, (len(mesh.faces), 1)))
        scene.add_geometry(mesh, node_name=node_name, geom_name=node_name)

        bbox = Bnd_Box()
        BRepBndLib.Add_s(shape, bbox)
        cmin = bbox.CornerMin()
        cmax = bbox.CornerMax()
        bmin = [round(cmin.X(), 2), round(cmin.Y(), 2), round(cmin.Z(), 2)]
        bmax = [round(cmax.X(), 2), round(cmax.Y(), 2), round(cmax.Z(), 2)]
        ext = [round(bmax[0] - bmin[0], 2), round(bmax[1] - bmin[1], 2), round(bmax[2] - bmin[2], 2)]
        centroid = [
            round(0.5 * (bmin[0] + bmax[0]), 2),
            round(0.5 * (bmin[1] + bmax[1]), 2),
            round(0.5 * (bmin[2] + bmax[2]), 2),
        ]

        vol_cm3 = 0.0
        try:
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(shape, props)
            vol_cm3 = round(max(0.0, props.Mass() / 1000.0), 3)
        except Exception:
            pass

        total_vol_cm3 += vol_cm3
        all_mins.append(bmin)
        all_maxs.append(bmax)
        parts_meta.append(
            {
                "node": node_name,
                "name": raw_name,
                "color": f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}",
                "extents_mm": ext,
                "centroid_mm": centroid,
                "volume_cm3": vol_cm3,
                "triangles": int(len(mesh.faces)),
            }
        )

    out_glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_bytes = scene.export(file_type="glb")
    out_glb_path.write_bytes(glb_bytes)

    if all_mins:
        gmin = [min(m[i] for m in all_mins) for i in range(3)]
        gmax = [max(m[i] for m in all_maxs) for i in range(3)]
        assembly_center = [round(0.5 * (gmin[i] + gmax[i]), 2) for i in range(3)]
        assembly_extents = [round(gmax[i] - gmin[i], 2) for i in range(3)]
    else:
        assembly_center = [0.0, 0.0, 0.0]
        assembly_extents = [0.0, 0.0, 0.0]

    return {
        "glb_path": str(out_glb_path),
        "glb_size_bytes": len(glb_bytes),
        "part_count": len(parts_meta),
        "total_volume_cm3": round(total_vol_cm3, 2),
        "assembly_center_mm": assembly_center,
        "assembly_extents_mm": assembly_extents,
        "parts": parts_meta,
    }


def render_team_viewer_html(
    title: str,
    glb_path: Path,
    scene_meta: Dict[str, Any],
    bom_markdown: str = "",
    verification_data: Optional[Dict[str, Any]] = None,
    embed_max_bytes: int = 35 * 1024 * 1024,
) -> str:
    """Generate a standalone interactive Three.js WebGL CAD viewer HTML document."""
    glb_bytes = glb_path.read_bytes()
    embedded_b64 = base64.b64encode(glb_bytes).decode("ascii") if len(glb_bytes) <= embed_max_bytes else ""
    rel_glb_name = glb_path.name
    meta_json = json.dumps(
        {
            "title": title,
            "glbFile": rel_glb_name,
            "embeddedBase64": embedded_b64,
            "scene": scene_meta,
            "bomMarkdown": bom_markdown,
            "verification": verification_data or {},
        }
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} — Interactive 3D CAD Team Viewer</title>
<style>
  :root {{
    --bg: #0f141c;
    --panel: rgba(20, 27, 38, 0.92);
    --border: rgba(148, 163, 184, 0.2);
    --accent: #38bdf8;
    --text: #e2e8f0;
    --muted: #94a3b8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; overflow: hidden; background: radial-gradient(circle at 50% 35%, #1b2536 0%, #0b0f16 100%);
    color: var(--text); font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  #canvas-wrap {{ position: fixed; inset: 0; }}
  #sidebar {{
    position: fixed; top: 14px; left: 14px; bottom: 14px; width: 330px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    backdrop-filter: blur(12px); display: flex; flex-direction: column; z-index: 10;
    box-shadow: 0 16px 40px rgba(0,0,0,0.45);
  }}
  .hdr {{ padding: 14px 16px; border-bottom: 1px solid var(--border); }}
  .hdr h1 {{ margin: 0; font-size: 16px; font-weight: 700; letter-spacing: 0.2px; }}
  .hdr .sub {{ margin-top: 4px; font-size: 12px; color: var(--muted); }}
  .tabs {{ display: flex; border-bottom: 1px solid var(--border); }}
  .tab {{
    flex: 1; padding: 9px 6px; text-align: center; font-size: 12px; font-weight: 600;
    color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent;
  }}
  .tab.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
  .pane {{ flex: 1; overflow-y: auto; padding: 12px; display: none; }}
  .pane.active {{ display: block; }}
  #search {{
    width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border);
    background: rgba(15, 20, 28, 0.8); color: var(--text); font-size: 12px; margin-bottom: 10px;
  }}
  .part-row {{
    display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 7px;
    cursor: pointer; font-size: 12px; margin-bottom: 3px;
  }}
  .part-row:hover {{ background: rgba(56, 189, 248, 0.12); }}
  .part-row.selected {{ background: rgba(56, 189, 248, 0.24); outline: 1px solid var(--accent); }}
  .swatch {{ width: 11px; height: 11px; border-radius: 3px; flex-shrink: 0; }}
  .pname {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .eye-btn {{
    background: none; border: none; color: var(--muted); cursor: pointer; font-size: 12px; padding: 2px 5px;
  }}
  #toolbar {{
    position: fixed; top: 14px; right: 14px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px;
    display: flex; flex-direction: column; gap: 10px; z-index: 10; width: 290px;
    backdrop-filter: blur(12px); box-shadow: 0 12px 32px rgba(0,0,0,0.4);
  }}
  .btn-row {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  button.ctrl {{
    flex: 1; padding: 6px 8px; font-size: 11px; font-weight: 600; color: var(--text);
    background: rgba(30, 41, 59, 0.85); border: 1px solid var(--border); border-radius: 7px; cursor: pointer;
  }}
  button.ctrl:hover {{ border-color: var(--accent); color: var(--accent); }}
  .slider-row {{ display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); }}
  .slider-row input[type=range] {{ width: 100%; accent-color: var(--accent); }}
  #inspector {{
    position: fixed; bottom: 14px; right: 14px; width: 290px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; z-index: 10;
    font-size: 12px; display: none; box-shadow: 0 12px 32px rgba(0,0,0,0.4);
  }}
  pre.bom {{
    white-space: pre-wrap; font-size: 11px; color: var(--text); background: rgba(0,0,0,0.25);
    padding: 10px; border-radius: 8px; overflow-x: auto; line-height: 1.45;
  }}
  .stat-card {{
    padding: 9px 10px; border-radius: 8px; background: rgba(15, 23, 42, 0.7);
    border: 1px solid var(--border); margin-bottom: 8px; font-size: 12px;
  }}
</style>
<script type="importmap">
{{
  "imports": {{
    "three": "https://unpkg.com/three@0.164.1/build/three.module.js",
    "three/addons/": "https://unpkg.com/three@0.164.1/examples/jsm/"
  }}
}}
</script>
</head>
<body>
<div id="canvas-wrap"></div>

<div id="sidebar">
  <div class="hdr">
    <h1 id="doc-title">Assembly Viewer</h1>
    <div class="sub" id="doc-sub">Loading 3D B-Rep scene...</div>
  </div>
  <div class="tabs">
    <div class="tab active" data-pane="parts-pane">Parts</div>
    <div class="tab" data-pane="bom-pane">BOM &amp; Specs</div>
  </div>
  <div class="pane active" id="parts-pane">
    <input id="search" type="text" placeholder="Filter parts by name or SKU..." />
    <div class="btn-row" style="margin-bottom:8px;">
      <button class="ctrl" id="btn-show-all">Show All</button>
      <button class="ctrl" id="btn-isolate">Isolate Selected</button>
    </div>
    <div id="parts-list"></div>
  </div>
  <div class="pane" id="bom-pane">
    <div id="stats-box"></div>
    <pre class="bom" id="bom-box"></pre>
  </div>
</div>

<div id="toolbar">
  <div class="btn-row">
    <button class="ctrl" data-cam="iso">Iso</button>
    <button class="ctrl" data-cam="front">Front</button>
    <button class="ctrl" data-cam="right">Right</button>
    <button class="ctrl" data-cam="top">Top</button>
    <button class="ctrl" id="btn-wire">Edges</button>
  </div>
  <div class="slider-row">
    <div style="display:flex;justify-content:space-between;"><span>Explode Assembly</span><span id="exp-val">0%</span></div>
    <input id="explode-slider" type="range" min="0" max="100" value="0" />
  </div>
  <div class="slider-row">
    <div style="display:flex;justify-content:space-between;">
      <span>Section Cut</span>
      <select id="sec-axis" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:4px;font-size:11px;">
        <option value="off">Off</option>
        <option value="x">X Plane</option>
        <option value="y">Y Plane</option>
        <option value="z">Z Plane</option>
      </select>
    </div>
    <input id="section-slider" type="range" min="0" max="100" value="50" />
  </div>
</div>

<div id="inspector">
  <div style="font-weight:700;color:var(--accent);margin-bottom:5px;" id="insp-name"></div>
  <div id="insp-dims" style="color:var(--muted);line-height:1.5;"></div>
</div>

<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ GLTFLoader }} from 'three/addons/loaders/GLTFLoader.js';

const PAYLOAD = {meta_json};
document.getElementById('doc-title').textContent = PAYLOAD.title;
const sc = PAYLOAD.scene;
document.getElementById('doc-sub').textContent =
  `${{sc.part_count}} parts · ${{sc.assembly_extents_mm.join(' × ')}} mm · ${{sc.total_volume_cm3}} cm³`;

const statsBox = document.getElementById('stats-box');
statsBox.innerHTML = `
  <div class="stat-card"><b>Envelope:</b> ${{sc.assembly_extents_mm.join(' × ')}} mm<br/>
  <b>Total Volume:</b> ${{sc.total_volume_cm3}} cm³ · <b>Parts:</b> ${{sc.part_count}}</div>
`;
document.getElementById('bom-box').textContent = PAYLOAD.bomMarkdown || 'No external BOM file attached.';

document.querySelectorAll('.tab').forEach(t => {{
  t.addEventListener('click', () => {{
    document.querySelectorAll('.tab,.pane').forEach(el => el.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.pane).classList.add('active');
  }});
}});

const wrap = document.getElementById('canvas-wrap');
const renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true }});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.localClippingEnabled = true;
wrap.appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 0.5, 50000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.AmbientLight(0xffffff, 0.75));
const dir1 = new THREE.DirectionalLight(0xffffff, 1.15);
dir1.position.set(400, 600, 500);
scene.add(dir1);
const dir2 = new THREE.DirectionalLight(0x93c5fd, 0.55);
dir2.position.set(-400, -300, -250);
scene.add(dir2);

const grid = new THREE.GridHelper(600, 30, 0x334155, 0x1e293b);
scene.add(grid);

const clipPlane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 10000);
const nodeMap = new Map();
const metaByNode = new Map(sc.parts.map(p => [p.node, p]));
let selectedNode = null;
let showWire = false;

function b64ToArrayBuffer(b64) {{
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}}

const loader = new GLTFLoader();
const rootGroup = new THREE.Group();
// Convert CAD Z-up mm to Three.js Y-up
rootGroup.rotation.x = -Math.PI / 2;
scene.add(rootGroup);

function onLoaded(gltf) {{
  const model = gltf.scene;
  rootGroup.add(model);
  model.traverse(obj => {{
    if (obj.isMesh) {{
      obj.material = obj.material.clone();
      obj.material.clippingPlanes = [];
      obj.material.side = THREE.DoubleSide;
      const key = metaByNode.has(obj.name) ? obj.name : (obj.parent && metaByNode.has(obj.parent.name) ? obj.parent.name : obj.name);
      nodeMap.set(key, obj);
      obj.userData.basePos = obj.position.clone();
      obj.userData.partKey = key;
    }}
  }});
  const box = new THREE.Box3().setFromObject(rootGroup);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  grid.position.y = box.min.y - 1;
  fitCamera('iso', center, Math.max(size.x, size.y, size.z, 50));
  renderPartsList('');
}}

if (PAYLOAD.embeddedBase64) {{
  loader.parse(b64ToArrayBuffer(PAYLOAD.embeddedBase64), '', onLoaded);
}} else {{
  loader.load(PAYLOAD.glbFile, onLoaded);
}}

function fitCamera(preset, centerTarget, spanVal) {{
  const box = new THREE.Box3().setFromObject(rootGroup);
  const center = centerTarget || box.getCenter(new THREE.Vector3());
  const sz = box.getSize(new THREE.Vector3());
  const d = (spanVal || Math.max(sz.x, sz.y, sz.z, 80)) * 1.65;
  controls.target.copy(center);
  if (preset === 'front') camera.position.set(center.x, center.y, center.z + d);
  else if (preset === 'right') camera.position.set(center.x + d, center.y, center.z);
  else if (preset === 'top') camera.position.set(center.x, center.y + d, center.z + 0.01);
  else camera.position.set(center.x + d * 0.72, center.y + d * 0.58, center.z + d * 0.72);
  controls.update();
}}

document.querySelectorAll('[data-cam]').forEach(btn => {{
  btn.addEventListener('click', () => fitCamera(btn.dataset.cam));
}});

document.getElementById('btn-wire').addEventListener('click', () => {{
  showWire = !showWire;
  nodeMap.forEach(mesh => {{ mesh.material.wireframe = showWire; }});
}});

const asmCenter = new THREE.Vector3(...sc.assembly_center_mm);
document.getElementById('explode-slider').addEventListener('input', e => {{
  const t = Number(e.target.value) / 100.0;
  document.getElementById('exp-val').textContent = `${{e.target.value}}%`;
  sc.parts.forEach(p => {{
    const mesh = nodeMap.get(p.node);
    if (!mesh) return;
    const c = new THREE.Vector3(...p.centroid_mm);
    const delta = c.sub(asmCenter).multiplyScalar(t * 1.15);
    mesh.position.copy(mesh.userData.basePos).add(delta);
  }});
}});

function updateSection() {{
  const ax = document.getElementById('sec-axis').value;
  const pct = Number(document.getElementById('section-slider').value) / 100.0;
  const box = new THREE.Box3().setFromObject(rootGroup);
  nodeMap.forEach(mesh => {{
    mesh.material.clippingPlanes = ax === 'off' ? [] : [clipPlane];
  }});
  if (ax === 'off') return;
  if (ax === 'x') {{
    clipPlane.normal.set(-1, 0, 0);
    clipPlane.constant = THREE.MathUtils.lerp(box.min.x, box.max.x, pct);
  }} else if (ax === 'y') {{
    clipPlane.normal.set(0, -1, 0);
    clipPlane.constant = THREE.MathUtils.lerp(box.min.y, box.max.y, pct);
  }} else {{
    clipPlane.normal.set(0, 0, -1);
    clipPlane.constant = THREE.MathUtils.lerp(box.min.z, box.max.z, pct);
  }}
}}
document.getElementById('sec-axis').addEventListener('change', updateSection);
document.getElementById('section-slider').addEventListener('input', updateSection);

function selectPart(nodeKey) {{
  selectedNode = nodeKey;
  nodeMap.forEach((mesh, k) => {{
    mesh.material.emissive = new THREE.Color(k === nodeKey ? 0x1d4ed8 : 0x000000);
  }});
  document.querySelectorAll('.part-row').forEach(el => {{
    el.classList.toggle('selected', el.dataset.node === nodeKey);
  }});
  const insp = document.getElementById('inspector');
  const p = metaByNode.get(nodeKey);
  if (!p) {{ insp.style.display = 'none'; return; }}
  insp.style.display = 'block';
  document.getElementById('insp-name').textContent = p.name;
  document.getElementById('insp-dims').innerHTML =
    `Size: <b>${{p.extents_mm.join(' × ')}} mm</b><br/>Volume: <b>${{p.volume_cm3}} cm³</b><br/>Center: [${{p.centroid_mm.join(', ')}}] mm`;
}}

function renderPartsList(filterTxt) {{
  const q = filterTxt.trim().toLowerCase();
  const box = document.getElementById('parts-list');
  box.innerHTML = '';
  sc.parts.forEach(p => {{
    if (q && !p.name.toLowerCase().includes(q) && !p.node.toLowerCase().includes(q)) return;
    const mesh = nodeMap.get(p.node);
    const row = document.createElement('div');
    row.className = 'part-row' + (selectedNode === p.node ? ' selected' : '');
    row.dataset.node = p.node;
    row.innerHTML = `
      <span class="swatch" style="background:${{p.color}}"></span>
      <span class="pname" title="${{p.name}}">${{p.name}}</span>
      <button class="eye-btn" title="Toggle visibility">${{mesh && !mesh.visible ? '○' : '●'}}</button>
    `;
    row.addEventListener('click', ev => {{
      if (ev.target.classList.contains('eye-btn')) {{
        if (mesh) {{ mesh.visible = !mesh.visible; ev.target.textContent = mesh.visible ? '●' : '○'; }}
        return;
      }}
      selectPart(p.node);
    }});
    box.appendChild(row);
  }});
}}

document.getElementById('search').addEventListener('input', e => renderPartsList(e.target.value));
document.getElementById('btn-show-all').addEventListener('click', () => {{
  nodeMap.forEach(m => {{ m.visible = true; }});
  renderPartsList(document.getElementById('search').value);
}});
document.getElementById('btn-isolate').addEventListener('click', () => {{
  if (!selectedNode) return;
  nodeMap.forEach((m, k) => {{ m.visible = (k === selectedNode); }});
  renderPartsList(document.getElementById('search').value);
}});

const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();
renderer.domElement.addEventListener('pointerdown', e => {{
  if (e.button !== 0) return;
  const startX = e.clientX, startY = e.clientY;
  const onUp = upEv => {{
    window.removeEventListener('pointerup', onUp);
    if (Math.hypot(upEv.clientX - startX, upEv.clientY - startY) > 5) return;
    mouse.x = (upEv.clientX / window.innerWidth) * 2 - 1;
    mouse.y = -(upEv.clientY / window.innerHeight) * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    const visibleMeshes = [...nodeMap.values()].filter(m => m.visible);
    const hits = raycaster.intersectObjects(visibleMeshes, false);
    if (hits.length > 0) selectPart(hits[0].object.userData.partKey);
    else selectPart(null);
  }};
  window.addEventListener('pointerup', onUp);
}});

window.addEventListener('resize', () => {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}});

function animate() {{
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}}
animate();
</script>
</body>
</html>
"""


def build_team_share_package(
    target_path: str | Path,
    out_dir: Optional[str | Path] = None,
    linear_deflection: float = 0.25,
    create_zip: bool = True,
    gh_release_tag: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a complete, portable team-sharing package from a project folder or `.step` file:
    - Interactive zero-install 3D browser viewer (`<name>_team_viewer.html` + `<name>.glb`)
    - Portable CAD masters (`.sc` Format 52 + `.step` + `.urdf` / `*_mjcf.xml` if present)
    - Fabrication print plates (`.3mf` / `.stl`) & laser DXFs (`.dxf`)
    - Documentation (`README_TEAM.md`, BOM, verification JSON, motion GIF)
    - Single `<name>_team_bundle.zip` archive + ready-to-run `gh release create` command.
    """
    target = Path(target_path).resolve()
    project_dir: Optional[Path] = None
    step_file: Optional[Path] = None

    if target.is_dir():
        project_dir = target
        cad_dir = project_dir / "cad"
        step_candidates = sorted(
            cad_dir.rglob("*.step") if cad_dir.exists() else project_dir.rglob("*.step"),
            key=lambda p: (0 if "assembly" in p.stem.lower() else 1, -p.stat().st_size),
        )
        if not step_candidates:
            raise FileNotFoundError(f"No .step files found under {project_dir}")
        step_file = step_candidates[0]
    elif target.suffix.lower() in (".step", ".stp"):
        step_file = target
        for parent in target.parents:
            if (parent / "cad").exists() and parent.parent.name == "projects":
                project_dir = parent
                break
    elif target.suffix.lower() == ".sc":
        sibling_step = target.with_suffix(".step")
        if sibling_step.exists():
            step_file = sibling_step
        else:
            raise ValueError(f"Pass a project directory or .step file (no sibling .step next to {target})")
        for parent in target.parents:
            if (parent / "cad").exists() and parent.parent.name == "projects":
                project_dir = parent
                break
    else:
        raise ValueError(f"Unsupported share target: {target}")

    assert step_file is not None
    base_name = step_file.stem
    pkg_root = (
        Path(out_dir).resolve()
        if out_dir
        else (project_dir / "docs" / "team_share" / base_name if project_dir else step_file.parent / f"{base_name}_team_share")
    )
    viewer_dir = pkg_root / "viewer"
    cad_out = pkg_root / "cad"
    fab_prints_out = pkg_root / "fabrication" / "prints"
    fab_laser_out = pkg_root / "fabrication" / "laser"
    docs_out = pkg_root / "docs"
    for d in (viewer_dir, cad_out, docs_out):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Export binary GLB + standalone interactive HTML viewer
    glb_path = viewer_dir / f"{base_name}.glb"
    scene_meta = export_step_to_glb(step_file, glb_path, linear_deflection=linear_deflection)

    bom_md = ""
    verif_data: Dict[str, Any] = {}
    for bom_cand in [
        step_file.with_name(f"{base_name}_sc_bom.md"),
        *(list((project_dir / "docs").glob("*bom*.md")) if project_dir and (project_dir / "docs").exists() else []),
    ]:
        if bom_cand.exists():
            bom_md = bom_cand.read_text(encoding="utf-8", errors="replace")
            shutil.copy2(bom_cand, docs_out / bom_cand.name)
            break

    verif_cand = step_file.with_name(f"{base_name}_sc_verification.json")
    if verif_cand.exists():
        try:
            verif_data = json.loads(verif_cand.read_text(encoding="utf-8"))
            shutil.copy2(verif_cand, docs_out / verif_cand.name)
        except Exception:
            pass

    viewer_html_path = viewer_dir / f"{base_name}_team_viewer.html"
    viewer_html_path.write_text(
        render_team_viewer_html(
            title=base_name.replace("_", " "),
            glb_path=glb_path,
            scene_meta=scene_meta,
            bom_markdown=bom_md,
            verification_data=verif_data,
        ),
        encoding="utf-8",
    )

    # 2. Copy primary CAD deliverables (.step, .sc, .urdf, *_constraints.json, *_mjcf.xml)
    copied_files: List[str] = []
    for ext_cand in [
        step_file,
        step_file.with_suffix(".sc"),
        step_file.with_suffix(".urdf"),
        step_file.with_name(f"{base_name}_constraints.json"),
        step_file.with_name(f"{base_name}_mjcf.xml"),
    ]:
        if ext_cand.exists() and ext_cand.stat().st_size < 250 * 1024 * 1024:
            dest = cad_out / ext_cand.name
            if ext_cand.resolve() != dest.resolve():
                shutil.copy2(ext_cand, dest)
            copied_files.append(f"cad/{ext_cand.name}")

    # 3. Copy motion GIF & renders if present
    gif_cand = step_file.with_name(f"{base_name}_sc_motion.gif")
    if gif_cand.exists():
        shutil.copy2(gif_cand, docs_out / gif_cand.name)
        copied_files.append(f"docs/{gif_cand.name}")

    # 4. Copy fabrication outputs (3MF/STL print plates & laser DXFs)
    if project_dir:
        prints_src = project_dir / "fabrication" / "prints"
        if prints_src.exists():
            for fp in sorted(prints_src.glob("*")):
                if fp.suffix.lower() in (".3mf", ".stl") and fp.is_file():
                    fab_prints_out.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(fp, fab_prints_out / fp.name)
                    copied_files.append(f"fabrication/prints/{fp.name}")
        laser_src = project_dir / "fabrication" / "laser"
        if laser_src.exists():
            for lp in sorted(laser_src.glob("*")):
                if lp.suffix.lower() in (".dxf", ".svg") and lp.is_file():
                    fab_laser_out.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(lp, fab_laser_out / lp.name)
                    copied_files.append(f"fabrication/laser/{lp.name}")

    # 5. Write clear README_TEAM.md handoff guide
    readme_md = f"""# {base_name} — Team CAD & Fabrication Handoff Package

## 1. View in 3D Immediately (No CAD Install Needed)
Double-click **`viewer/{viewer_html_path.name}`** in Chrome, Edge, Safari, or Firefox:
- **Left-drag**: Orbit 3D · **Right-drag**: Pan · **Scroll**: Zoom
- **Explode Slider**: Separate parts radially to inspect internal bearings, spacers, and stackups
- **Section Cut**: Slice across `X / Y / Z` planes in real time
- **Click any 3D part** (or search in the left sidebar) to inspect exact dimensions (`mm`), volume, and isolate/hide subsystems.

## 2. Edit or Inspect in CAD
- **SolidCraft (`v0.40.0-rc.4+`, Format 52)**: Open `cad/{base_name}.sc` (100% self-contained — all COTS geometry, materials, and kinematic joints are embedded inside the `.sc` container).
- **Onshape / Fusion / SolidWorks**: Import `cad/{base_name}.step`.
- **Simulation (MuJoCo / Genesis / ROS2)**: Use `cad/{base_name}_mjcf.xml` or `cad/{base_name}.urdf`.

## 3. Fabrication
- **3D Printing**: Slice ready-to-print `.3mf` / `.stl` files in `fabrication/prints/` with OrcaSlicer / Bambu Studio.
- **Laser / CNC Routing**: Cut `.dxf` flat profiles in `fabrication/laser/`.

## Summary Metrics
- **Parts**: `{scene_meta['part_count']}`
- **Envelope**: `{scene_meta['assembly_extents_mm'][0]} × {scene_meta['assembly_extents_mm'][1]} × {scene_meta['assembly_extents_mm'][2]} mm`
- **Total Solid Volume**: `{scene_meta['total_volume_cm3']} cm³`
"""
    (pkg_root / "README_TEAM.md").write_text(readme_md, encoding="utf-8")

    zip_path: Optional[Path] = None
    if create_zip:
        zip_path = pkg_root.parent / f"{base_name}_team_bundle.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for item in sorted(pkg_root.rglob("*")):
                if item.is_file():
                    zf.write(item, arcname=f"{base_name}_team_bundle/{item.relative_to(pkg_root).as_posix()}")

    gh_cmd = (
        f'gh release create {gh_release_tag or f"v1.0-{base_name}"} '
        f'"{zip_path or pkg_root}" "{viewer_html_path}" '
        f'--title "{base_name} CAD & Fabrication Release" '
        f'--notes "Interactive 3D HTML viewer + Format 52 SolidCraft (.sc) + STEP + Print/Laser files"'
    )
    gh_published_url: Optional[str] = None
    if gh_release_tag and zip_path and zip_path.exists():
        res = subprocess.run(
            [
                "gh",
                "release",
                "create",
                gh_release_tag,
                str(zip_path),
                str(viewer_html_path),
                "--title",
                f"{base_name} ({gh_release_tag})",
                "--notes",
                readme_md,
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            gh_published_url = res.stdout.strip()

    return {
        "status": "ok",
        "package_dir": str(pkg_root),
        "viewer_html": str(viewer_html_path),
        "glb_path": str(glb_path),
        "bundle_zip": str(zip_path) if zip_path else None,
        "bundle_zip_mb": round(zip_path.stat().st_size / (1024 * 1024), 2) if zip_path and zip_path.exists() else None,
        "part_count": scene_meta["part_count"],
        "assembly_extents_mm": scene_meta["assembly_extents_mm"],
        "included_files": copied_files,
        "gh_release_command": gh_cmd,
        "gh_release_url": gh_published_url,
    }
