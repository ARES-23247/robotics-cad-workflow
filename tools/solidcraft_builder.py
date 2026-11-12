"""
Native SolidCraft Parametric Feature-Tree Builder & Bidirectional Script Sync (`solidcraft_builder.py`)
======================================================================================================
Provides a clean Python DSL (`SolidCraftPartBuilder`) that compiles custom fabricated parts
(2D plates, brackets, sensor mounts, 3DP enclosures with bosses/snap-fits, and sheet-metal
parts) directly into native SolidCraft feature-tree commands (`parameters.add`, `sketch.*`,
`solid.extrude`, `solid.hole`, `solid.fillet`, `plastic.boss`, `plastic.snap_fit`, `sheet.flange`).

Unlike static STEP imports, `.sc` parts built with `SolidCraftPartBuilder` have:
1. A live, human-editable **Parameters Table** (`parameters.list` / `parameters.change`) in the SolidCraft GUI.
2. A full **Parametric Feature & Sketch Timeline** (`timeline.inspect` / `timeline.export_script`).
3. Automated **Parameter Robustness Sweep Verification** (`inspect.parameter_sweep`) proving that
   changing driving parameters (`Center_Distance`, `Plate_Thickness`, `Bearing_Bore_Dia`, etc.)
   never flips a sketch normal, folds a constraint, or breaks a feature across the operating range.
4. **Bidirectional Script Sync (`pull_solidcraft_script`)**: Exports `*_sc_script.json`
   (`timeline.export_script`) and `*_sc_params.json` (`parameters.list`) so manual edits made in
   the SolidCraft desktop GUI can be pulled directly back into Git.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.solidcraft_cots import find_solidcraft_cli

CoordExpr = Union[float, int, str]
Point2DExpr = Tuple[CoordExpr, CoordExpr]
Point3DExpr = Tuple[CoordExpr, CoordExpr, CoordExpr]


def _rgb_to_hex(rgb: Union[str, Sequence[Union[int, float]]]) -> str:
    if isinstance(rgb, str):
        return rgb if rgb.startswith("#") else f"#{rgb}"
    vals = list(rgb)
    if any(float(v) > 1.0 for v in vals[:3]):
        r, g, b = int(vals[0]), int(vals[1]), int(vals[2])
    else:
        r, g, b = int(round(float(vals[0]) * 255)), int(round(float(vals[1]) * 255)), int(round(float(vals[2]) * 255))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


class SolidCraftPartBuilder:
    """
    Declarative Python DSL for authoring native SolidCraft feature-tree parts (`.sc`)
    with user parameters, expression-driven sketches, 3D features, plastic/sheet-metal
    primitives, joint origins, and automated `inspect.parameter_sweep` verification.
    """

    def __init__(
        self,
        name: str,
        part_number: Optional[str] = None,
        description: Optional[str] = None,
        material: str = "Aluminum 6061",
        color: Optional[Union[str, Sequence[Union[int, float]]]] = None,
    ) -> None:
        self.name = name
        self.part_number = part_number or name
        self.description = description or name
        self.material = material
        self.color_hex = _rgb_to_hex(color) if color is not None else None
        self.commands: List[Dict[str, Any]] = []
        self.sweeps: List[Dict[str, Any]] = []
        self.joint_origins: List[Dict[str, Any]] = []
        self.print_face_points: List[Tuple[Optional[str], List[float]]] = []

    def cmd(self, command: str, params: Optional[Dict[str, Any]] = None) -> "SolidCraftPartBuilder":
        """Append a raw SolidCraft engine command (`{command, params}`) to the build queue."""
        self.commands.append({"command": command, "params": params or {}})
        return self

    # ------------------------------------------------------------------
    # 1. User Parameters & Robustness Sweeps
    # ------------------------------------------------------------------
    def add_parameter(
        self,
        name: str,
        expression: Union[float, int, str],
        unit: str = "mm",
        comment: str = "",
        favorite: bool = True,
        sweep_values: Optional[Sequence[Union[float, int, str]]] = None,
        sweep_range: Optional[Tuple[float, float, int]] = None,
    ) -> "SolidCraftPartBuilder":
        """
        Define a named user parameter (`parameters.add`) usable in any subsequent
        sketch coordinate, circle diameter, extrude distance, hole depth, or fillet.
        Optionally registers an `inspect.parameter_sweep` range/list to verify on build.
        """
        params: Dict[str, Any] = {
            "name": name,
            "expression": str(expression),
            "unit": unit,
            "favorite": bool(favorite),
        }
        if comment:
            params["comment"] = comment
        self.cmd("parameters.add", params)
        if sweep_values is not None:
            self.add_parameter_sweep(name, values=list(sweep_values))
        elif sweep_range is not None:
            self.add_parameter_sweep(name, from_val=sweep_range[0], to_val=sweep_range[1], steps=sweep_range[2])
        return self

    def add_parameter_sweep(
        self,
        parameter: str,
        values: Optional[Sequence[Union[float, int, str]]] = None,
        from_val: Optional[float] = None,
        to_val: Optional[float] = None,
        steps: int = 5,
    ) -> "SolidCraftPartBuilder":
        """Register a non-destructive `inspect.parameter_sweep` check for `parameter`."""
        spec: Dict[str, Any] = {"parameter": parameter}
        if values is not None:
            spec["values"] = list(values)
        elif from_val is not None and to_val is not None:
            spec["from"] = float(from_val)
            spec["to"] = float(to_val)
            spec["steps"] = int(steps)
        else:
            raise ValueError("Provide either `values` or `(from_val, to_val)` for `add_parameter_sweep`.")
        self.sweeps.append(spec)
        return self

    # ------------------------------------------------------------------
    # 2. 2D Sketches (Supports Parameter Expressions in Coordinates & Sizes!)
    # ------------------------------------------------------------------
    def start_sketch(
        self,
        plane: Union[str, Dict[str, Any]] = "XY",
        offset: Optional[CoordExpr] = None,
        flip: bool = False,
        name: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {"plane": plane}
        if offset is not None:
            params["offset"] = str(offset)
        if flip:
            params["flip"] = True
        if name:
            params["name"] = name
        return self.cmd("sketch.create", params)

    def sketch_rounded_rect(
        self,
        p0: Point2DExpr,
        p1: Point2DExpr,
        radius: CoordExpr,
        construction: bool = False,
    ) -> "SolidCraftPartBuilder":
        """
        Draw a 2-point rounded rectangle (`sketch.rectangle.rounded` when corners are numeric,
        or expression-driven `sketch.rectangle.two_point` when `p0`/`p1` use parameter expressions).
        """
        if any(isinstance(c, str) for c in (p0[0], p0[1], p1[0], p1[1])):
            return self.sketch_two_point_rect(p0, p1, construction=construction)
        params: Dict[str, Any] = {
            "p0": [float(p0[0]), float(p0[1])],
            "p1": [float(p1[0]), float(p1[1])],
            "radius": str(radius),
        }
        if construction:
            params["construction"] = True
        return self.cmd("sketch.rectangle.rounded", params)

    def sketch_two_point_rect(
        self,
        p0: Point2DExpr,
        p1: Point2DExpr,
        construction: bool = False,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {"p0": [p0[0], p0[1]], "p1": [p1[0], p1[1]]}
        if construction:
            params["construction"] = True
        return self.cmd("sketch.rectangle.two_point", params)

    def sketch_center_rect(
        self,
        center: Point2DExpr,
        corner: Point2DExpr,
        construction: bool = False,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {"center": [center[0], center[1]], "corner": [corner[0], corner[1]]}
        if construction:
            params["construction"] = True
        return self.cmd("sketch.rectangle.center", params)

    def sketch_circle(
        self,
        center: Point2DExpr,
        diameter: Optional[CoordExpr] = None,
        radius: Optional[CoordExpr] = None,
        construction: bool = False,
    ) -> "SolidCraftPartBuilder":
        """Draw a circle (`sketch.circle.center`). `center` and `diameter`/`radius` may be parameter expressions."""
        params: Dict[str, Any] = {"center": [center[0], center[1]]}
        if diameter is not None:
            params["diameter"] = diameter if isinstance(diameter, (int, float)) else str(diameter)
        elif radius is not None:
            params["radius"] = radius if isinstance(radius, (int, float)) else str(radius)
        else:
            raise ValueError("Provide either `diameter` or `radius` to `sketch_circle`.")
        if construction:
            params["construction"] = True
        return self.cmd("sketch.circle.center", params)

    def sketch_circle_pattern(
        self,
        centers: Sequence[Point2DExpr],
        diameter: CoordExpr,
    ) -> "SolidCraftPartBuilder":
        """Draw multiple circles (e.g. a goBILDA 16mm/8mm bolt circle or grid pattern) in the active sketch."""
        for pt in centers:
            self.sketch_circle(pt, diameter=diameter)
        return self

    def sketch_slot(
        self,
        center: Point2DExpr,
        end: Point2DExpr,
        width: CoordExpr,
    ) -> "SolidCraftPartBuilder":
        return self.cmd(
            "sketch.slot.center_point",
            {
                "center": [center[0], center[1]],
                "end": [end[0], end[1]],
                "width": width if isinstance(width, (int, float)) else str(width),
            },
        )

    def sketch_line(
        self,
        points: Sequence[Point2DExpr],
        closed: bool = False,
        construction: bool = False,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "points": [[p[0], p[1]] for p in points],
            "closed": bool(closed),
        }
        if construction:
            params["construction"] = True
        return self.cmd("sketch.line", params)

    def finish_sketch(self) -> "SolidCraftPartBuilder":
        return self.cmd("sketch.finish", {})

    # ------------------------------------------------------------------
    # 3. 3D Solid Features (Extrude, Revolve, Hole, Fillet, Chamfer)
    # ------------------------------------------------------------------
    def extrude(
        self,
        distance: CoordExpr,
        direction: str = "positive",
        operation: str = "auto",
        sketch: Optional[str] = None,
        name: Optional[str] = None,
        body_name: Optional[str] = None,
        start_offset: Optional[CoordExpr] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "distance": str(distance),
            "direction": direction,
            "operation": operation,
        }
        if sketch:
            params["sketch"] = sketch
        if name:
            params["name"] = name
        if body_name or (operation in ("auto", "new") and not getattr(self, "_has_primary_body", False)):
            params["body_name"] = body_name or self.name
            self._has_primary_body = True
        if start_offset is not None:
            params["start_offset"] = str(start_offset)
        return self.cmd("solid.extrude", params)

    def revolve(
        self,
        axis: Union[str, Dict[str, Any]] = "Z",
        angle: CoordExpr = "360 deg",
        operation: str = "auto",
        sketch: Optional[str] = None,
        name: Optional[str] = None,
        body_name: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "axis": axis,
            "angle": str(angle),
            "operation": operation,
        }
        if sketch:
            params["sketch"] = sketch
        if name:
            params["name"] = name
        if body_name:
            params["body_name"] = body_name
        return self.cmd("solid.revolve", params)

    def hole(
        self,
        position: Point3DExpr,
        diameter: Optional[CoordExpr] = None,
        clearance: Optional[str] = None,
        fit: str = "normal",
        depth: Optional[CoordExpr] = None,
        hole_type: str = "simple",
        cb_diameter: Optional[CoordExpr] = None,
        cb_depth: Optional[CoordExpr] = None,
        cs_diameter: Optional[CoordExpr] = None,
        cs_angle: Optional[CoordExpr] = None,
        thread: Optional[str] = None,
        targets: Optional[Sequence[str]] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "position": [position[0], position[1], position[2]],
            "type": hole_type,
        }
        if diameter is not None:
            params["diameter"] = str(diameter)
        if clearance is not None:
            params["clearance"] = clearance
            params["fit"] = fit
        if depth is not None:
            params["depth"] = str(depth)
        if cb_diameter is not None:
            params["cb_diameter"] = str(cb_diameter)
        if cb_depth is not None:
            params["cb_depth"] = str(cb_depth)
        if cs_diameter is not None:
            params["cs_diameter"] = str(cs_diameter)
        if cs_angle is not None:
            params["cs_angle"] = str(cs_angle)
        if thread is not None:
            params["thread"] = thread
        if targets:
            params["targets"] = list(targets)
        return self.cmd("solid.hole", params)

    def fillet(
        self,
        edges: Sequence[Union[Sequence[float], Dict[str, Any]]],
        radius: CoordExpr,
        body: Optional[str] = None,
        fit: str = "reduce",
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "edges": list(edges),
            "radius": str(radius),
            "fit": fit,
        }
        if body:
            params["body"] = body
        return self.cmd("solid.fillet", params)

    def chamfer(
        self,
        edges: Sequence[Union[Sequence[float], Dict[str, Any]]],
        distance: CoordExpr,
        body: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "edges": list(edges),
            "distance": str(distance),
        }
        if body:
            params["body"] = body
        return self.cmd("solid.chamfer", params)

    # ------------------------------------------------------------------
    # 4. Native Plastic / 3DP Enclosure & Sheet Metal Features
    # ------------------------------------------------------------------
    def plastic_boss(
        self,
        position: Sequence[float],
        diameter: CoordExpr,
        height: CoordExpr,
        hole_diameter: Optional[CoordExpr] = None,
        hole_depth: Optional[CoordExpr] = None,
        ribs: int = 0,
        rib_thickness: Optional[CoordExpr] = None,
        rib_length: Optional[CoordExpr] = None,
        draft: Optional[CoordExpr] = None,
        fillet: Optional[CoordExpr] = None,
        body: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "position": list(position),
            "diameter": str(diameter),
            "height": str(height),
        }
        if hole_diameter is not None:
            params["hole_diameter"] = str(hole_diameter)
        if hole_depth is not None:
            params["hole_depth"] = str(hole_depth)
        if ribs > 0:
            params["ribs"] = int(ribs)
        if rib_thickness is not None:
            params["rib_thickness"] = str(rib_thickness)
        if rib_length is not None:
            params["rib_length"] = str(rib_length)
        if draft is not None:
            params["draft"] = str(draft)
        if fillet is not None:
            params["fillet"] = str(fillet)
        if body:
            params["body"] = body
        return self.cmd("plastic.boss", params)

    def plastic_snap_fit(
        self,
        position: Sequence[float],
        hook: Sequence[float],
        length: CoordExpr,
        thickness: CoordExpr,
        width: CoordExpr,
        catch_depth: CoordExpr,
        catch_length: CoordExpr,
        direction: Optional[Sequence[float]] = None,
        body: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "position": list(position),
            "hook": list(hook),
            "length": str(length),
            "thickness": str(thickness),
            "width": str(width),
            "catch_depth": str(catch_depth),
            "catch_length": str(catch_length),
        }
        if direction is not None:
            params["direction"] = list(direction)
        if body:
            params["body"] = body
        return self.cmd("plastic.snap_fit", params)

    def sheet_base_flange(
        self,
        sketch: Optional[str] = None,
        rule: Optional[str] = None,
        flip: bool = False,
        name: Optional[str] = None,
        body_name: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {"type": "base", "flip": bool(flip)}
        if sketch:
            params["sketch"] = sketch
        if rule:
            params["rule"] = rule
        if name:
            params["name"] = name
        if body_name or not getattr(self, "_has_primary_body", False):
            params["body_name"] = body_name or self.name
            self._has_primary_body = True
        return self.cmd("sheet.flange", params)

    def sheet_edge_flange(
        self,
        edges: Sequence[Union[Sequence[float], Dict[str, Any]]],
        height: CoordExpr,
        angle: CoordExpr = "90 deg",
        position: str = "inside",
        flip: bool = False,
        body: Optional[str] = None,
        name: Optional[str] = None,
    ) -> "SolidCraftPartBuilder":
        params: Dict[str, Any] = {
            "type": "edge",
            "edges": list(edges),
            "height": str(height),
            "angle": str(angle),
            "position": position,
            "flip": bool(flip),
        }
        if body:
            params["body"] = body
        if name:
            params["name"] = name
        return self.cmd("sheet.flange", params)

    # ------------------------------------------------------------------
    # 5. Joint Origins & Print Orientation
    # ------------------------------------------------------------------
    def add_joint_origin(
        self,
        name: str,
        point: Sequence[float],
        z_axis: Sequence[float] = (0.0, 0.0, 1.0),
        x_axis: Sequence[float] = (1.0, 0.0, 0.0),
    ) -> "SolidCraftPartBuilder":
        self.joint_origins.append(
            {
                "name": name,
                "point": [float(v) for v in point],
                "z": [float(v) for v in z_axis],
                "x": [float(v) for v in x_axis],
            }
        )
        return self

    def set_print_face(self, point: Sequence[float], body: Optional[str] = None) -> "SolidCraftPartBuilder":
        self.print_face_points.append((body, [float(v) for v in point]))
        return self

    # ------------------------------------------------------------------
    # 6. Build, Verify (`inspect.parameter_sweep`), & Export (`.sc`, `*_sc_script.json`, `.step`)
    # ------------------------------------------------------------------
    def build(
        self,
        out_sc_path: Union[str, Path],
        export_script_path: Optional[Union[str, Path]] = None,
        export_params_path: Optional[Union[str, Path]] = None,
        export_step_path: Optional[Union[str, Path]] = None,
        export_thumb_path: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """
        Execute the native feature-tree command queue in SolidCraft's Rust engine,
        apply material/appearance/BOM properties, run all registered `inspect.parameter_sweep`
        checks, export the command script (`timeline.export_script`) and parameters JSON,
        optionally export a `.step` file for `build123d` compatibility, and save `out_sc_path`.
        """
        t0 = time.perf_counter()
        cli_path = find_solidcraft_cli()
        out_sc = Path(out_sc_path).resolve()
        out_sc.parent.mkdir(parents=True, exist_ok=True)

        if export_script_path is None:
            export_script_path = out_sc.with_name(f"{out_sc.stem}_sc_script.json")
        if export_params_path is None:
            export_params_path = out_sc.with_name(f"{out_sc.stem}_sc_params.json")

        proc = subprocess.Popen(
            [cli_path, "--max-file-size", "2GB", "mcp", "--headless", "--allow-any-path"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        req_id = 0

        def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal req_id
            req_id += 1
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                stderr_out = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"solidcraft-cli EOF on {method}: {stderr_out}")
            return json.loads(line)

        try:
            _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-builder", "version": "1.0"}})
            _rpc("tools/call", {"name": "new_design", "arguments": {"name": self.name, "units": "mm"}})

            # Execute feature-building commands via `batch`
            r_batch = _rpc(
                "tools/call",
                {
                    "name": "batch",
                    "arguments": {
                        "commands": self.commands,
                        "rollback": True,
                    },
                },
            )
            if "result" not in r_batch or r_batch["result"].get("isError"):
                raise RuntimeError(f"SolidCraftPartBuilder batch failed for {self.name}: {json.dumps(r_batch, indent=2)}")

            # Inspect generated bodies and assign material, appearance, and print face
            r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
            insp_data = json.loads(r_insp["result"]["content"][0]["text"])
            bodies = [b.get("name") for b in (insp_data.get("bodies") or []) if isinstance(b, dict) and b.get("name")]

            post_cmds: List[Dict[str, Any]] = [
                {
                    "command": "design.set_properties",
                    "params": {
                        "part_number": self.part_number,
                        "description": self.description,
                        "bom_type": "manufactured",
                    },
                }
            ]
            for bname in bodies:
                if self.material:
                    post_cmds.append({"command": "body.set_material", "params": {"body": bname, "material": self.material}})
                if self.color_hex:
                    post_cmds.append(
                        {
                            "command": "body.set_appearance",
                            "params": {"body": bname, "color": self.color_hex, "roughness": 0.35, "metallic": 0.15},
                        }
                    )
            for jo in self.joint_origins:
                post_cmds.append(
                    {
                        "command": "joint.origin.create",
                        "params": {
                            "name": jo["name"],
                            "origin": jo["point"],
                            "z_axis": jo["z"],
                            "x_axis": jo["x"],
                        },
                    }
                )
            for pf_body, pf_pt in self.print_face_points:
                target_b = pf_body or (bodies[0] if bodies else None)
                if target_b:
                    post_cmds.append({"command": "body.print_face", "params": {"body": target_b, "point": pf_pt}})
            if not self.print_face_points and bodies:
                bb0 = (insp_data.get("bodies") or [{}])[0].get("bbox")
                if isinstance(bb0, dict) and bb0.get("min") and bb0.get("max"):
                    cx = 0.5 * (float(bb0["min"][0]) + float(bb0["max"][0]))
                    cy = 0.5 * (float(bb0["min"][1]) + float(bb0["max"][1]))
                    z_min = float(bb0["min"][2])
                    post_cmds.append({"command": "body.print_face", "params": {"body": bodies[0], "point": [round(cx, 3), round(cy, 3), round(z_min, 3)]}})

            if post_cmds:
                _rpc("tools/call", {"name": "batch", "arguments": {"commands": post_cmds}})

            # Run registered `inspect.parameter_sweep` checks
            sweep_reports: List[Dict[str, Any]] = []
            for sw in self.sweeps:
                r_sw = _rpc("tools/call", {"name": "execute", "arguments": {"command": "inspect.parameter_sweep", "params": sw}})
                if "result" in r_sw and not r_sw["result"].get("isError"):
                    sw_data = json.loads(r_sw["result"]["content"][0]["text"])
                    summary = sw_data.get("summary") or {}
                    sweep_reports.append({"parameter": sw["parameter"], "summary": summary})
                    if (
                        summary.get("failed", 0) > 0
                        or summary.get("sketches_failed", 0) > 0
                        or summary.get("sketches_flipped", 0) > 0
                        or summary.get("joints_lost", 0) > 0
                    ):
                        raise RuntimeError(
                            f"Parameter sweep failed for {self.name}.{sw['parameter']}: {json.dumps(sw_data, indent=2)}"
                        )

            # Export command script (`timeline.export_script`) and user parameters (`parameters.list`)
            script_path = Path(export_script_path).resolve()
            params_path = Path(export_params_path).resolve()
            _rpc("tools/call", {"name": "execute", "arguments": {"command": "timeline.export_script", "params": {"path": str(script_path)}}})
            r_params = _rpc("tools/call", {"name": "execute", "arguments": {"command": "parameters.list", "params": {"filter": "all"}}})
            if "result" in r_params and not r_params["result"].get("isError"):
                params_data = json.loads(r_params["result"]["content"][0]["text"])
                params_path.write_text(json.dumps(params_data, indent=2), encoding="utf-8")

            # Optional STEP export for `build123d` compatibility
            if export_step_path is not None:
                step_out = Path(export_step_path).resolve()
                step_out.parent.mkdir(parents=True, exist_ok=True)
                _rpc("tools/call", {"name": "export", "arguments": {"path": str(step_out)}})

            # Optional 3D isometric thumbnail PNG export
            if export_thumb_path is not None:
                thumb_out = Path(export_thumb_path).resolve()
                thumb_out.parent.mkdir(parents=True, exist_ok=True)
                _rpc(
                    "tools/call",
                    {
                        "name": "screenshot",
                        "arguments": {
                            "path": str(thumb_out),
                            "view": "iso",
                            "width": 360,
                            "height": 270,
                        },
                    },
                )

            # Save `.sc` container
            _rpc("tools/call", {"name": "save", "arguments": {"path": str(out_sc)}})
            elapsed_ms = round(1000.0 * (time.perf_counter() - t0), 1)
            return {
                "status": "PASS",
                "name": self.name,
                "sc_path": str(out_sc),
                "script_json_path": str(script_path),
                "params_json_path": str(params_path),
                "step_path": str(Path(export_step_path).resolve()) if export_step_path else None,
                "thumb_path": str(Path(export_thumb_path).resolve()) if export_thumb_path else None,
                "bodies": bodies,
                "parameter_sweeps": sweep_reports,
                "build_ms": elapsed_ms,
            }
        finally:
            proc.terminate()


def pull_solidcraft_script(
    sc_path: Optional[Union[str, Path]] = None,
    out_script_path: Optional[Union[str, Path]] = None,
    out_params_path: Optional[Union[str, Path]] = None,
    live: bool = False,
    connect_port: int = 27878,
) -> Dict[str, Any]:
    """
    Bidirectional GUI -> Code Sync:
    Reads either the currently open design in the live SolidCraft GUI (`--live`, port 27878)
    or a saved `.sc` file on disk, exports its full feature timeline script
    (`timeline.export_script` -> `*_sc_script.json`) and parameter table (`parameters.list`
    -> `*_sc_params.json`), and runs `inspect.parameter_sweep` (±20% around current value)
    on every user length parameter to verify parametric health.
    """
    cli_path = find_solidcraft_cli()
    cmd = [cli_path, "--max-file-size", "2GB", "mcp", "--allow-any-path"]
    if live:
        cmd.extend(["--connect", f"127.0.0.1:{connect_port}"])
    else:
        cmd.append("--headless")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-pull", "version": "1.0"}})
        if not live:
            if not sc_path:
                raise ValueError("Provide `sc_path` when `live=False`.")
            sc_file = Path(sc_path).resolve()
            _rpc("tools/call", {"name": "open", "arguments": {"path": str(sc_file)}})
        else:
            r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
            insp = json.loads(r_insp["result"]["content"][0]["text"])
            sc_file = Path(insp.get("path") or sc_path or (WORKSPACE_ROOT / f"{insp.get('name', 'design')}.sc")).resolve()

        script_out = Path(out_script_path).resolve() if out_script_path else sc_file.with_name(f"{sc_file.stem}_sc_script.json")
        params_out = Path(out_params_path).resolve() if out_params_path else sc_file.with_name(f"{sc_file.stem}_sc_params.json")
        script_out.parent.mkdir(parents=True, exist_ok=True)

        r_sc = _rpc("tools/call", {"name": "execute", "arguments": {"command": "timeline.export_script", "params": {"path": str(script_out)}}})
        sc_res = json.loads(r_sc["result"]["content"][0]["text"]) if ("result" in r_sc and not r_sc["result"].get("isError")) else {}

        r_pr = _rpc("tools/call", {"name": "execute", "arguments": {"command": "parameters.list", "params": {"filter": "all"}}})
        pr_res = json.loads(r_pr["result"]["content"][0]["text"]) if ("result" in r_pr and not r_pr["result"].get("isError")) else {}
        params_out.write_text(json.dumps(pr_res, indent=2), encoding="utf-8")

        sweeps: List[Dict[str, Any]] = []
        for p in pr_res.get("parameters") or []:
            if p.get("source") == "user" and p.get("unit") in ("mm", "cm", "m", "in") and isinstance(p.get("value"), (int, float)):
                val = float(p["value"])
                if val > 0.5:
                    r_sw = _rpc(
                        "tools/call",
                        {
                            "name": "execute",
                            "arguments": {
                                "command": "inspect.parameter_sweep",
                                "params": {"parameter": p["name"], "from": round(val * 0.85, 3), "to": round(val * 1.15, 3), "steps": 4},
                            },
                        },
                    )
                    if "result" in r_sw and not r_sw["result"].get("isError"):
                        sw_data = json.loads(r_sw["result"]["content"][0]["text"])
                        sweeps.append({"parameter": p["name"], "summary": sw_data.get("summary")})

        return {
            "status": "ok",
            "source": f"live:127.0.0.1:{connect_port}" if live else str(sc_file),
            "script_json_path": str(script_out),
            "params_json_path": str(params_out),
            "steps_exported": sc_res.get("steps"),
            "user_parameters": [p for p in (pr_res.get("parameters") or []) if p.get("source") == "user"],
            "parameter_sweeps": sweeps,
        }
    finally:
        proc.terminate()


# ----------------------------------------------------------------------
# 7. Parametric Robotics Template Library (FTC / FRC Standard Interfaces)
# ----------------------------------------------------------------------
PARAMETRIC_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "gobilda_bearing_plate": {
        "id": "gobilda_bearing_plate",
        "title": "goBILDA 16mm Grid 2-Shaft Carrier Plate",
        "category": "plates",
        "material": "Aluminum 6061",
        "color": (0.22, 0.25, 0.30),
        "description": (
            "Parametric 2-axis belt/gear carrier plate on the 16.0 mm goBILDA pattern with dual "
            "flanged bearing bores (1611-0514-0008), 8× M4 grid holes, and a center lightening pocket."
        ),
        "anchors": ["shaft_a_top", "shaft_b_top", "origin_bottom"],
        "parameters": [
            {"name": "Center_Distance", "label": "Shaft Center Distance", "default": 96.0, "min": 48.0, "max": 192.0, "step": 8.0, "unit": "mm"},
            {"name": "Half_Width", "label": "Plate Half-Width", "default": 24.0, "min": 18.0, "max": 40.0, "step": 2.0, "unit": "mm"},
            {"name": "End_Margin", "label": "End Margin Past Shafts", "default": 26.0, "min": 20.0, "max": 36.0, "step": 2.0, "unit": "mm"},
            {"name": "Plate_Thickness", "label": "Plate Thickness", "default": 4.0, "min": 2.0, "max": 8.0, "step": 0.5, "unit": "mm"},
            {"name": "Bearing_Bore_Dia", "label": "Bearing Bore Ø", "default": 14.1, "min": 6.0, "max": 22.5, "step": 0.1, "unit": "mm"},
            {"name": "M4_Clearance_Dia", "label": "M4 Grid Hole Ø", "default": 4.2, "min": 3.2, "max": 5.5, "step": 0.1, "unit": "mm"},
        ],
    },
    "gobilda_5203_motor_mount": {
        "id": "gobilda_5203_motor_mount",
        "title": "goBILDA 5202/5203/5204 Planetary Motor Faceplate",
        "category": "mounts",
        "material": "Aluminum 6061",
        "color": (0.18, 0.22, 0.28),
        "description": (
            "Parametric motor mount faceplate for goBILDA 5202/5203/5204 Yellow Jacket Planetary "
            "Gearmotors with 22.5 mm pilot boss clearance, 4× M4 holes on a 32 mm bolt circle, and 16 mm grid mounts."
        ),
        "anchors": ["motor_pilot", "motor_face_top", "chassis_grid_center"],
        "parameters": [
            {"name": "Half_Width", "label": "Plate Half-Width", "default": 24.0, "min": 20.0, "max": 48.0, "step": 4.0, "unit": "mm"},
            {"name": "Bottom_Reach", "label": "Bottom Reach Below Shaft", "default": 24.0, "min": 20.0, "max": 48.0, "step": 4.0, "unit": "mm"},
            {"name": "Top_Reach", "label": "Top Reach Above Shaft", "default": 40.0, "min": 28.0, "max": 64.0, "step": 4.0, "unit": "mm"},
            {"name": "Plate_Thickness", "label": "Plate Thickness", "default": 4.0, "min": 3.0, "max": 8.0, "step": 0.5, "unit": "mm"},
            {"name": "Pilot_Bore_Dia", "label": "Motor Pilot Bore Ø", "default": 22.5, "min": 14.0, "max": 28.0, "step": 0.5, "unit": "mm"},
            {"name": "M4_Clearance_Dia", "label": "M4 Mounting Hole Ø", "default": 4.2, "min": 3.2, "max": 5.5, "step": 0.1, "unit": "mm"},
        ],
    },
    "servo_mount_bracket": {
        "id": "servo_mount_bracket",
        "title": "Standard 20×40mm FTC Servo Saddle Bracket",
        "category": "actuators",
        "material": "Aluminum 6061",
        "color": (0.25, 0.28, 0.34),
        "description": (
            "Parametric saddle frame for standard 20×40 mm FTC servos (Swyft Titan, goBILDA 2000, Axon) "
            "with a 41.5×20.5 mm body window, 48×10 mm ear bolt pattern, and 16 mm goBILDA corner mounts."
        ),
        "anchors": ["servo_seat", "servo_output_offset", "origin_bottom"],
        "parameters": [
            {"name": "Outer_Half_X", "label": "Outer Half-Width X", "default": 32.0, "min": 28.0, "max": 48.0, "step": 2.0, "unit": "mm"},
            {"name": "Outer_Half_Y", "label": "Outer Half-Height Y", "default": 16.0, "min": 14.0, "max": 28.0, "step": 2.0, "unit": "mm"},
            {"name": "Servo_Half_X", "label": "Servo Window Half-X", "default": 20.75, "min": 19.5, "max": 22.0, "step": 0.25, "unit": "mm"},
            {"name": "Servo_Half_Y", "label": "Servo Window Half-Y", "default": 10.25, "min": 9.5, "max": 11.5, "step": 0.25, "unit": "mm"},
            {"name": "Plate_Thickness", "label": "Bracket Thickness", "default": 5.0, "min": 3.0, "max": 10.0, "step": 0.5, "unit": "mm"},
            {"name": "M4_Clearance_Dia", "label": "M4 Ear/Grid Hole Ø", "default": 4.2, "min": 3.2, "max": 5.0, "step": 0.1, "unit": "mm"},
        ],
    },
    "vision_sensor_mount": {
        "id": "vision_sensor_mount",
        "title": "Vision / Distance Sensor L-Flange Bracket",
        "category": "sensors",
        "material": "PETG",
        "color": (0.14, 0.42, 0.68),
        "description": (
            "Parametric 3D-printed 90° L-bracket for Limelight 3A, REV 2m Distance/Color Sensor, or "
            "AprilTag camera modules with a 16 mm grid foot and adjustable sensor hole pitch."
        ),
        "anchors": ["sensor_face_center", "chassis_mount_left", "origin_bottom"],
        "parameters": [
            {"name": "Base_Depth", "label": "Foot Depth Y", "default": 28.0, "min": 22.0, "max": 48.0, "step": 2.0, "unit": "mm"},
            {"name": "Riser_Height", "label": "Riser Height Z", "default": 32.0, "min": 26.0, "max": 60.0, "step": 2.0, "unit": "mm"},
            {"name": "Wall_Thickness", "label": "Wall Thickness", "default": 4.0, "min": 3.0, "max": 6.0, "step": 0.5, "unit": "mm"},
            {"name": "Sensor_Hole_Spacing", "label": "Sensor Hole Pitch", "default": 16.0, "min": 10.0, "max": 32.0, "step": 1.0, "unit": "mm"},
            {"name": "M3_Clearance_Dia", "label": "Sensor Hole Ø (M3)", "default": 3.2, "min": 2.5, "max": 4.5, "step": 0.1, "unit": "mm"},
        ],
    },
    "clamping_shaft_hub": {
        "id": "clamping_shaft_hub",
        "title": "3D-Printed Shaft Actuator Arm with Ribbed Boss",
        "category": "actuators",
        "material": "PA12-CF",
        "color": (0.16, 0.18, 0.22),
        "description": (
            "Parametric 3D-printed cantilever actuator arm with a 6 mm / 8 mm shaft bore, 16 mm clamping "
            "bolt pattern, native ribbed `plastic.boss` standoff, and distal wrist linkage pivot."
        ),
        "anchors": ["shaft_bore_bottom", "wrist_pivot_top"],
        "parameters": [
            {"name": "Arm_Length", "label": "Center-to-Tip Arm Length", "default": 48.0, "min": 32.0, "max": 96.0, "step": 4.0, "unit": "mm"},
            {"name": "Hub_Half_Width", "label": "Arm Half-Width", "default": 14.0, "min": 11.0, "max": 22.0, "step": 1.0, "unit": "mm"},
            {"name": "Hub_Thickness", "label": "Arm Thickness", "default": 8.0, "min": 5.0, "max": 14.0, "step": 0.5, "unit": "mm"},
            {"name": "Shaft_Bore_Dia", "label": "Shaft Bore Ø", "default": 6.0, "min": 4.0, "max": 12.0, "step": 0.5, "unit": "mm"},
            {"name": "Tip_Hole_Dia", "label": "Distal Pivot Hole Ø", "default": 4.2, "min": 3.0, "max": 8.0, "step": 0.1, "unit": "mm"},
        ],
    },
    "sheet_metal_u_channel_gusset": {
        "id": "sheet_metal_u_channel_gusset",
        "title": "Sheet-Metal Flanged U-Channel Gusset Bracket",
        "category": "sheet_metal",
        "material": "Aluminum 5052",
        "color": (0.55, 0.60, 0.66),
        "description": (
            "Parametric sheet-metal gusset built with native `sheet.flange` base + twin edge flanges "
            "and a 4-hole goBILDA 16 mm mounting pattern."
        ),
        "anchors": ["grid_center", "flange_top_center"],
        "parameters": [
            {"name": "Flange_Height", "label": "Edge Flange Height", "default": 18.0, "min": 10.0, "max": 36.0, "step": 2.0, "unit": "mm"},
            {"name": "Bend_Angle_Deg", "label": "Bend Angle", "default": 90.0, "min": 45.0, "max": 120.0, "step": 5.0, "unit": "deg"},
            {"name": "M4_Clearance_Dia", "label": "M4 Grid Hole Ø", "default": 4.2, "min": 3.2, "max": 5.5, "step": 0.1, "unit": "mm"},
        ],
    },
}


def list_parametric_templates() -> List[Dict[str, Any]]:
    """Return metadata, parameter schemas, and Python snippets for all parametric robotics templates."""
    out: List[Dict[str, Any]] = []
    for tid, spec in PARAMETRIC_TEMPLATES.items():
        item = dict(spec)
        default_params = {p["name"]: p["default"] for p in spec["parameters"]}
        thumb_rel = f"thumbs/tpl_{tid}.png"
        item["thumb"] = thumb_rel
        item["python_snippet"] = (
            f"from tools.solidcraft_builder import build_template_part\n"
            f"from tools.ag_cad_helpers import attach_native_solidcraft_part\n\n"
            f"tpl = build_template_part(\n"
            f"    {tid!r},\n"
            f"    params_override={json.dumps(default_params)},\n"
            f"    out_sc_path='cad/solidcraft/{tid}.sc',\n"
            f"    export_step_path='cad/solidcraft/{tid}.step',\n"
            f")\n"
        )
        out.append(item)
    return out


def insert_custom_sc_into_live_solidcraft(
    sc_path: Union[str, Path],
    translate: Optional[Sequence[float]] = None,
    target_occurrence: Optional[str] = None,
    joint_type: Optional[str] = None,
    anchor: Optional[str] = None,
    target_point: Optional[Sequence[float]] = None,
    connect_port: int = 27878,
) -> Dict[str, Any]:
    """
    Insert a custom parametric `.sc` part into the active SolidCraft GUI session (`--connect 127.0.0.1:27878`),
    and optionally mate or translate it.
    """
    cli_path = find_solidcraft_cli()
    sc_file = Path(sc_path).resolve()
    proc = subprocess.Popen(
        [cli_path, "--max-file-size", "2GB", "mcp", "--connect", f"127.0.0.1:{connect_port}", "--allow-any-path"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    req_id = 0

    def _rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal req_id
        req_id += 1
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sc-tpl-insert", "version": "1.0"}})
        r_ins = _rpc(
            "tools/call",
            {
                "name": "execute",
                "arguments": {
                    "command": "component.insert_design",
                    "params": {"path": str(sc_file), "reuse": True},
                },
            },
        )
        if "result" not in r_ins or r_ins["result"].get("isError"):
            return {"status": "error", "error": f"Failed to insert {sc_file.name}: {r_ins}"}
        ins_data = json.loads(r_ins["result"]["content"][0]["text"])
        occ_id = ins_data.get("occurrence_id", 1)
        comp_name = ins_data.get("component_name", sc_file.stem)

        # Find full occurrence label (e.g. "gobilda_bearing_plate:1")
        r_insp = _rpc("tools/call", {"name": "inspect_design", "arguments": {}})
        insp_data = json.loads(r_insp["result"]["content"][0]["text"]) if "result" in r_insp else {}
        occ_label = f"{comp_name}:1"
        for oc in insp_data.get("occurrences") or []:
            if isinstance(oc, dict) and oc.get("id") == occ_id:
                occ_label = f"{oc.get('component', comp_name)}:{oc.get('instance', 1)}"
                break

        joint_created = None
        if target_occurrence and joint_type:
            t_pt = [float(v) for v in (target_point or [0.0, 0.0, 0.0])[:3]]
            j_name = f"Mate_{comp_name}_{occ_id}"
            r_j = _rpc(
                "tools/call",
                {
                    "name": "execute",
                    "arguments": {
                        "command": "joint.create",
                        "params": {
                            "name": j_name,
                            "type": joint_type,
                            "occurrence_a": str(target_occurrence),
                            "occurrence_b": occ_label,
                            "anchor_a": {"point": t_pt, "z": [0.0, 0.0, 1.0], "x": [1.0, 0.0, 0.0]},
                            "anchor_b": {"point": [0.0, 0.0, 0.0], "z": [0.0, 0.0, 1.0], "x": [1.0, 0.0, 0.0]},
                        },
                    },
                },
            )
            if "result" in r_j and not r_j["result"].get("isError"):
                joint_created = j_name
        elif translate and any(abs(float(v)) > 1e-6 for v in translate[:3]):
            _rpc(
                "tools/call",
                {
                    "name": "execute",
                    "arguments": {
                        "command": "occurrence.move",
                        "params": {"occurrence": occ_label, "translate": [float(v) for v in translate[:3]]},
                    },
                },
            )

        return {
            "status": "ok",
            "component_name": comp_name,
            "occurrence_id": occ_id,
            "occurrence_label": occ_label,
            "joint_created": joint_created,
            "sc_path": str(sc_file),
        }
    finally:
        proc.terminate()


def build_template_part(
    template_id: str,
    params_override: Optional[Dict[str, Union[float, int, str]]] = None,
    out_sc_path: Optional[Union[str, Path]] = None,
    export_step_path: Optional[Union[str, Path]] = None,
    export_thumb_path: Optional[Union[str, Path]] = None,
    part_name: Optional[str] = None,
    insert_live: bool = False,
    target_occurrence: Optional[str] = None,
    joint_type: Optional[str] = None,
    anchor: Optional[str] = None,
    target_point: Optional[Sequence[float]] = None,
    connect_port: int = 27878,
) -> Dict[str, Any]:
    """
    Build one of the 6 standard parametric robotics templates in `PARAMETRIC_TEMPLATES`
    using `SolidCraftPartBuilder`, run `inspect.parameter_sweep` verification, save its
    `.sc`, `*_sc_script.json`, `*_sc_params.json`, optional `.step` and thumbnail `.png`,
    and optionally insert + mate it into the live SolidCraft GUI on port `connect_port`.
    """
    if template_id not in PARAMETRIC_TEMPLATES:
        raise ValueError(f"Unknown template_id {template_id!r}. Available: {sorted(PARAMETRIC_TEMPLATES)}")

    spec = PARAMETRIC_TEMPLATES[template_id]
    overrides = params_override or {}
    vals: Dict[str, float] = {}
    for p in spec["parameters"]:
        pname = p["name"]
        raw = overrides.get(pname, p["default"])
        vals[pname] = float(raw)

    name = part_name or template_id
    out_sc = (
        Path(out_sc_path).resolve()
        if out_sc_path
        else (WORKSPACE_ROOT / "cots" / "solidcraft" / "generated" / f"{name}.sc").resolve()
    )
    if export_thumb_path is None:
        export_thumb_path = WORKSPACE_ROOT / "cots" / "solidcraft" / "thumbs" / f"tpl_{template_id}.png"

    b = SolidCraftPartBuilder(
        name=name,
        part_number=f"TPL-{template_id.upper()}",
        description=spec["description"],
        material=spec["material"],
        color=spec["color"],
    )

    if template_id == "gobilda_bearing_plate":
        cd = vals["Center_Distance"]
        hw = vals["Half_Width"]
        em = vals["End_Margin"]
        th = vals["Plate_Thickness"]
        bd = vals["Bearing_Bore_Dia"]
        m4 = vals["M4_Clearance_Dia"]

        b.add_parameter("Center_Distance", cd, "mm", "Shaft-to-shaft spacing", sweep_range=(max(48.0, cd - 16.0), cd + 16.0, 3))
        b.add_parameter("Half_Width", hw, "mm", "Plate half-width")
        b.add_parameter("End_Margin", em, "mm", "End margin past shaft centers")
        b.add_parameter("Plate_Thickness", th, "mm", "Carrier plate thickness", sweep_values=[max(2.0, th - 1.0), th, th + 2.0])
        b.add_parameter("Bearing_Bore_Dia", bd, "mm", "Flanged bearing bore diameter")
        b.add_parameter("M4_Clearance_Dia", m4, "mm", "goBILDA M4 clearance hole diameter")

        b.start_sketch("XY", name="Plate_Master_Sketch")
        b.sketch_two_point_rect(("-End_Margin", "-Half_Width"), ("Center_Distance + End_Margin", "Half_Width"))
        b.sketch_circle((0.0, 0.0), diameter="Bearing_Bore_Dia")
        b.sketch_circle(("Center_Distance", 0.0), diameter="Bearing_Bore_Dia")
        b.sketch_circle_pattern(
            [
                (-16.0, -16.0),
                (-16.0, 16.0),
                (16.0, -16.0),
                (16.0, 16.0),
                ("Center_Distance - 16 mm", -16.0),
                ("Center_Distance - 16 mm", 16.0),
                ("Center_Distance + 16 mm", -16.0),
                ("Center_Distance + 16 mm", 16.0),
            ],
            diameter="M4_Clearance_Dia",
        )
        if cd >= 64.0:
            b.sketch_two_point_rect((24.0, -10.0), ("Center_Distance - 24 mm", 10.0))
        b.finish_sketch()
        b.extrude("Plate_Thickness", name="Plate_Base_Extrude")
        b.add_joint_origin("shaft_a_top", [0.0, 0.0, th])
        b.add_joint_origin("shaft_b_top", [cd, 0.0, th])
        b.add_joint_origin("origin_bottom", [0.0, 0.0, 0.0])

    elif template_id == "gobilda_5203_motor_mount":
        hw = vals["Half_Width"]
        br = vals["Bottom_Reach"]
        tr = vals["Top_Reach"]
        th = vals["Plate_Thickness"]
        pb = vals["Pilot_Bore_Dia"]
        m4 = vals["M4_Clearance_Dia"]

        b.add_parameter("Half_Width", hw, "mm", "Faceplate half-width")
        b.add_parameter("Bottom_Reach", br, "mm", "Reach below motor axis")
        b.add_parameter("Top_Reach", tr, "mm", "Reach above motor axis", sweep_range=(max(28.0, tr - 8.0), tr + 8.0, 3))
        b.add_parameter("Plate_Thickness", th, "mm", "Faceplate thickness")
        b.add_parameter("Pilot_Bore_Dia", pb, "mm", "5202/5203/5204 22mm boss clearance")
        b.add_parameter("M4_Clearance_Dia", m4, "mm", "M4 mounting hole diameter")

        b.start_sketch("XY", name="Motor_Mount_Sketch")
        b.sketch_two_point_rect(("-Half_Width", "-Bottom_Reach"), ("Half_Width", "Top_Reach"))
        b.sketch_circle((0.0, 0.0), diameter="Pilot_Bore_Dia")
        # 4x M4 on 32mm bolt circle at 45 deg (16 * cos(45) = 11.314 mm)
        b.sketch_circle_pattern(
            [
                (-11.314, -11.314),
                (-11.314, 11.314),
                (11.314, -11.314),
                (11.314, 11.314),
                (-16.0, "Top_Reach - 8 mm"),
                (0.0, "Top_Reach - 8 mm"),
                (16.0, "Top_Reach - 8 mm"),
            ],
            diameter="M4_Clearance_Dia",
        )
        b.finish_sketch()
        b.extrude("Plate_Thickness", name="Motor_Mount_Extrude")
        b.add_joint_origin("motor_pilot", [0.0, 0.0, 0.0])
        b.add_joint_origin("motor_face_top", [0.0, 0.0, th])
        b.add_joint_origin("chassis_grid_center", [0.0, tr - 8.0, 0.0])

    elif template_id == "servo_mount_bracket":
        ox = vals["Outer_Half_X"]
        oy = vals["Outer_Half_Y"]
        sx = vals["Servo_Half_X"]
        sy = vals["Servo_Half_Y"]
        th = vals["Plate_Thickness"]
        m4 = vals["M4_Clearance_Dia"]

        b.add_parameter("Outer_Half_X", ox, "mm", "Outer half-width", sweep_range=(max(28.0, ox - 4.0), ox + 8.0, 3))
        b.add_parameter("Outer_Half_Y", oy, "mm", "Outer half-height")
        b.add_parameter("Servo_Half_X", sx, "mm", "Servo pocket half-width")
        b.add_parameter("Servo_Half_Y", sy, "mm", "Servo pocket half-height")
        b.add_parameter("Plate_Thickness", th, "mm", "Frame thickness")
        b.add_parameter("M4_Clearance_Dia", m4, "mm", "M4 ear and grid hole diameter")

        b.start_sketch("XY", name="Servo_Saddle_Sketch")
        b.sketch_two_point_rect(("-Outer_Half_X", "-Outer_Half_Y"), ("Outer_Half_X", "Outer_Half_Y"))
        b.sketch_two_point_rect(("-Servo_Half_X", "-Servo_Half_Y"), ("Servo_Half_X", "Servo_Half_Y"))
        b.sketch_circle_pattern(
            [
                (-24.0, -5.0),
                (-24.0, 5.0),
                (24.0, -5.0),
                (24.0, 5.0),
                (-24.0, -12.0),
                (-24.0, 12.0),
                (24.0, -12.0),
                (24.0, 12.0),
            ],
            diameter="M4_Clearance_Dia",
        )
        b.finish_sketch()
        b.extrude("Plate_Thickness", name="Servo_Saddle_Extrude")
        b.add_joint_origin("servo_seat", [0.0, 0.0, th])
        b.add_joint_origin("servo_output_offset", [10.0, 0.0, th])
        b.add_joint_origin("origin_bottom", [0.0, 0.0, 0.0])

    elif template_id == "vision_sensor_mount":
        bd = vals["Base_Depth"]
        rh = vals["Riser_Height"]
        wt = vals["Wall_Thickness"]
        sh = vals["Sensor_Hole_Spacing"]
        m3 = vals["M3_Clearance_Dia"]

        b.add_parameter("Base_Depth", bd, "mm", "Foot depth Y", sweep_range=(max(22.0, bd - 4.0), bd + 8.0, 3))
        b.add_parameter("Riser_Height", rh, "mm", "Riser height Z", sweep_range=(max(26.0, rh - 4.0), rh + 8.0, 3))
        b.add_parameter("Wall_Thickness", wt, "mm", "Wall thickness")
        b.add_parameter("Sensor_Hole_Spacing", sh, "mm", "Sensor hole spacing")
        b.add_parameter("M3_Clearance_Dia", m3, "mm", "Sensor M3 clearance diameter")

        b.start_sketch("XY", name="Foot_Sketch")
        b.sketch_two_point_rect((-24.0, 0.0), (24.0, "Base_Depth"))
        b.sketch_circle_pattern([(-16.0, 16.0), (16.0, 16.0)], diameter=4.2)
        b.finish_sketch()
        b.extrude("Wall_Thickness", name="Foot_Extrude")

        b.start_sketch("XZ", name="Riser_Sketch")
        b.sketch_two_point_rect((-24.0, 0.0), (24.0, "Riser_Height"))
        b.sketch_circle((0.0, 18.0), diameter=8.0)
        b.sketch_circle_pattern([("-Sensor_Hole_Spacing / 2", 18.0), ("Sensor_Hole_Spacing / 2", 18.0)], diameter="M3_Clearance_Dia")
        b.finish_sketch()
        b.extrude("Wall_Thickness", operation="join", name="Riser_Extrude")
        b.add_joint_origin("sensor_face_center", [0.0, 0.0, 18.0], z_axis=[0.0, -1.0, 0.0], x_axis=[1.0, 0.0, 0.0])
        b.add_joint_origin("chassis_mount_left", [-16.0, 16.0, 0.0])
        b.add_joint_origin("origin_bottom", [0.0, 0.0, 0.0])

    elif template_id == "clamping_shaft_hub":
        al = vals["Arm_Length"]
        hw = vals["Hub_Half_Width"]
        ht = vals["Hub_Thickness"]
        sb = vals["Shaft_Bore_Dia"]
        th = vals["Tip_Hole_Dia"]

        b.add_parameter("Arm_Length", al, "mm", "Center-to-tip arm length", sweep_range=(max(32.0, al - 8.0), al + 12.0, 3))
        b.add_parameter("Hub_Half_Width", hw, "mm", "Hub half-width")
        b.add_parameter("Hub_Thickness", ht, "mm", "Arm thickness")
        b.add_parameter("Shaft_Bore_Dia", sb, "mm", "Center shaft bore diameter")
        b.add_parameter("Tip_Hole_Dia", th, "mm", "Distal pivot hole diameter")

        b.start_sketch("XY", name="Arm_Sketch")
        b.sketch_two_point_rect((-14.0, "-Hub_Half_Width"), ("Arm_Length + 12 mm", "Hub_Half_Width"))
        b.sketch_circle((0.0, 0.0), diameter="Shaft_Bore_Dia")
        b.sketch_circle_pattern([(0.0, -8.0), (0.0, 8.0)], diameter=4.2)
        b.sketch_circle(("Arm_Length", 0.0), diameter="Tip_Hole_Dia")
        b.finish_sketch()
        b.extrude("Hub_Thickness", name="Arm_Extrude")
        b.plastic_boss(
            position=[20.0, 0.0, ht],
            diameter=10.0,
            height=4.0,
            hole_diameter=3.2,
            ribs=2,
            rib_thickness=1.8,
            rib_length=4.0,
        )
        b.add_joint_origin("shaft_bore_bottom", [0.0, 0.0, 0.0])
        b.add_joint_origin("wrist_pivot_top", [al, 0.0, ht])

    elif template_id == "sheet_metal_u_channel_gusset":
        fh = vals["Flange_Height"]
        ang = vals["Bend_Angle_Deg"]
        m4 = vals["M4_Clearance_Dia"]

        b.add_parameter("Flange_Height", fh, "mm", "Edge flange height", sweep_range=(max(10.0, fh - 4.0), fh + 6.0, 3))
        b.add_parameter("M4_Clearance_Dia", m4, "mm", "goBILDA M4 hole diameter")

        b.start_sketch("XY", name="Gusset_Base_Profile")
        b.sketch_two_point_rect((-24.0, -16.0), (24.0, 16.0))
        b.finish_sketch()
        b.sheet_base_flange(name="Base_Flange")
        b.sheet_edge_flange(
            edges=[[0.0, 16.0, 2.0], [0.0, -16.0, 2.0]],
            height="Flange_Height",
            angle=f"{ang} deg",
            name="Side_Flanges",
        )
        for hx, hy in [(-16.0, -8.0), (-16.0, 8.0), (16.0, -8.0), (16.0, 8.0)]:
            b.hole(position=(hx, hy, 2.0), diameter="M4_Clearance_Dia")
        b.add_joint_origin("grid_center", [0.0, 0.0, 0.0])
        b.add_joint_origin("flange_top_center", [0.0, 0.0, fh])

    res = b.build(
        out_sc_path=out_sc,
        export_step_path=export_step_path,
        export_thumb_path=export_thumb_path,
    )
    res["template_id"] = template_id
    res["parameters"] = vals
    res["anchors"] = spec["anchors"]

    if insert_live:
        live_res = insert_custom_sc_into_live_solidcraft(
            out_sc,
            translate=target_point if not target_occurrence else None,
            target_occurrence=target_occurrence,
            joint_type=joint_type,
            anchor=anchor,
            target_point=target_point,
            connect_port=connect_port,
        )
        res["live_insert"] = live_res

    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="SolidCraft Native Parametric Builder, Template Library & Script Puller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser("pull", help="Pull timeline command script (*_sc_script.json) and parameters (*_sc_params.json) from a .sc file or live GUI")
    p_pull.add_argument("file", nargs="?", type=Path, default=None, help="Path to .sc file (optional if --live)")
    p_pull.add_argument("--live", action="store_true", help="Pull from the currently active design in the live SolidCraft GUI")
    p_pull.add_argument("--port", type=int, default=27878, help="Live SolidCraft --control port (default 27878)")
    p_pull.add_argument("--script-out", type=Path, default=None, help="Output path for *_sc_script.json")
    p_pull.add_argument("--params-out", type=Path, default=None, help="Output path for *_sc_params.json")

    sub.add_parser("templates", help="List available parametric robotics part templates and their parameter schemas")

    p_bt = sub.add_parser("build-template", help="Build a parametric robotics template (.sc + *_sc_script.json + parameter sweep)")
    p_bt.add_argument("template_id", choices=sorted(PARAMETRIC_TEMPLATES.keys()), help="Template ID to build")
    p_bt.add_argument("--param", "-p", action="append", default=[], help="Parameter override KEY=VAL (repeatable, e.g. -p Center_Distance=112)")
    p_bt.add_argument("--out", type=Path, default=None, help="Output .sc path")
    p_bt.add_argument("--step-out", type=Path, default=None, help="Optional .step output path")
    p_bt.add_argument("--name", type=str, default=None, help="Custom part name")
    p_bt.add_argument("--insert-live", action="store_true", help="Also insert the built .sc part into the live SolidCraft session on port 27878")
    p_bt.add_argument("--port", type=int, default=27878, help="Live SolidCraft port (default 27878)")

    args = parser.parse_args()
    if args.cmd == "pull":
        res = pull_solidcraft_script(
            sc_path=args.file,
            out_script_path=args.script_out,
            out_params_path=args.params_out,
            live=args.live,
            connect_port=args.port,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "templates":
        print(json.dumps(list_parametric_templates(), indent=2))
    elif args.cmd == "build-template":
        overrides: Dict[str, Union[float, int, str]] = {}
        for kv in args.param:
            if "=" in kv:
                k, v = kv.split("=", 1)
                overrides[k.strip()] = float(v.strip())
        res = build_template_part(
            args.template_id,
            params_override=overrides,
            out_sc_path=args.out,
            export_step_path=args.step_out,
            part_name=args.name,
            insert_live=args.insert_live,
            connect_port=args.port,
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
