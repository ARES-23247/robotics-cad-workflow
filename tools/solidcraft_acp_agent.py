#!/usr/bin/env python3
"""
SolidCraft Native In-App ACP Parts Library & Robotics Toolkit (`tools/solidcraft_acp_agent.py`)
==============================================================================================

Implements the Agent Client Protocol (ACP v1, JSON-RPC 2.0 over stdin/stdout) to provide a
**100% native `egui` Parts Library, Auto-Mate, Parametric Template & Robotics Audit interface**
directly inside stock SolidCraft's right-hand **Assistant** panel (`crates/ui-egui/src/agent_chat/`),
without recompiling or modifying `solidcraft.exe`.

How It Works Inside SolidCraft's Native `egui` Panel:
----------------------------------------------------
1. **Native `egui` Dropdowns & Searchable Picker (`configOptions`):**
   - `vendor` (`category: "vendor"`): Filter 2,588 COTS parts by manufacturer (`goBILDA`, `REV`, `ISO`, `Swyft`, `ThriftyBot`, `McMaster`, `Ruthex`).
   - `category` (`category: "category"`): Dynamically filtered categories with live part counts (`Motors`, `Bearings`, `Gears`, `Beams`, `Channels`, `Pulleys`, `Fasteners`, etc.).
   - `action` (`category: "mode"`): Choose what happens when clicking a part in the picker:
     * `insert`: ⚡ 1-Click Insert Part into live 3D viewport (`component.insert_design {"reuse": true}`)
     * `mate_rigid`: 🔩 1-Click Insert + Rigid Mate to current 3D viewport selection
     * `mate_revolute`: 🔄 1-Click Insert + Revolute Mate to current 3D viewport selection
     * `preview`: 🔍 Inspect Part Specs Only
   - `part` (`category: "model"` -> rendered by SolidCraft as `ui_kit::search_picker`):
     * Native `egui` searchable dropdown with live filter box, category groups, hover tooltips, and custom query input.
     * Clicking a listed part immediately executes the selected `action` (inserting/mating in ~1 ms) and posts a native Tool Call card + spec summary in the panel.
     * Typing any custom query into the picker search box and pressing Enter dynamically populates the picker with the top 120 database matches.

2. **Native `Pick in model` / `Attach selection` + Instant Commands (`session/prompt`):**
   - Type any search term (`5203`, `8mm hex bearing`, `m4x12`, `1120 channel`) to filter the dropdown and render a Markdown table.
   - `insert <SKU/query>` or `mate <SKU/query>` (uses `Pick in model` / `Attach selection` or live viewport selection).
   - `templates` or `template <id> [param=val ...]` to build parametric `build123d` -> `.sc` parts live.
   - `bom` / `mass` / `clearance` / `fab` to run live assembly audits and fabrication exports.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

DB_PATH = WORKSPACE_ROOT / "cots" / "catalog.db"
COTS_DIR = WORKSPACE_ROOT / "cots"


def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reply(req_id: Any, result: Dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _reply_error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _update(session_id: str, update_obj: Dict[str, Any]) -> None:
    _send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": update_obj,
        },
    })


def _say(session_id: str, text: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    )


def _tool_event(
    session_id: str,
    tool_id: str,
    title: str,
    kind: str,
    status: str,
    raw_input: Optional[Dict[str, Any]] = None,
    output_text: Optional[str] = None,
) -> None:
    u: Dict[str, Any] = {
        "sessionUpdate": "tool_call" if status == "in_progress" else "tool_call_update",
        "toolCallId": tool_id,
        "title": title,
        "kind": kind,
        "status": status,
    }
    if raw_input is not None:
        u["rawInput"] = raw_input
    if output_text is not None:
        u["content"] = [{"type": "content", "content": {"type": "text", "text": output_text[:4000]}}]
    _update(session_id, u)


class CotsCatalogIndex:
    """Fast in-memory + SQLite index of all 2,588 normalized `.sc` COTS parts."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.parts: List[Dict[str, Any]] = []
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.by_sku_lower: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.db_path.is_file():
            return
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT part_id, vendor, category, sku, title, solidcraft_path,
                   solidcraft_material, solidcraft_anchor, min_bore_radius_mm,
                   bbox_min_mm, bbox_max_mm
            FROM local_cots_parts
            WHERE status = 'VERIFIED_STEP'
              AND solidcraft_path IS NOT NULL
              AND solidcraft_path != ''
            ORDER BY
              CASE vendor
                WHEN 'goBILDA' THEN 1
                WHEN 'REV' THEN 2
                WHEN 'ISO' THEN 3
                WHEN 'Swyft' THEN 4
                WHEN 'ThriftyBot' THEN 5
                WHEN 'McMaster' THEN 6
                WHEN 'Ruthex' THEN 7
                ELSE 8
              END,
              category ASC, sku ASC, title ASC
            """
        ).fetchall()
        conn.close()

        for r in rows:
            pid = str(r["part_id"])
            raw_v = str(r["vendor"] or "other").lower()
            if "gobilda" in raw_v:
                vendor = "goBILDA"
            elif raw_v == "rev":
                vendor = "REV"
            elif raw_v == "swyft":
                vendor = "Swyft"
            elif "thrifty" in raw_v:
                vendor = "ThriftyBot"
            elif "ruthex" in raw_v:
                vendor = "Ruthex"
            elif "fastener" in raw_v or raw_v == "iso":
                vendor = "ISO / Fasteners"
            else:
                vendor = raw_v.upper()

            raw_cat = str(r["category"] or "misc").lower()
            if raw_cat.endswith(".step") or raw_cat == "biobuzz-2026":
                if "rx-" in raw_cat:
                    cat = "fasteners"
                elif "ttb-" in raw_cat:
                    cat = "wheels"
                else:
                    cat = "motors"
            else:
                cat = raw_cat
            sku = str(r["sku"] or "").strip()
            title = str(r["title"] or sku or pid).strip()
            sc_rel = str(r["solidcraft_path"])
            full_rel = f"cots/{sc_rel}" if not sc_rel.startswith("cots/") else sc_rel
            mat = str(r["solidcraft_material"] or "Aluminum")
            anc = str(r["solidcraft_anchor"] or "bore_center")
            bore_r = r["min_bore_radius_mm"]
            bore_d = round(float(bore_r) * 2.0, 2) if bore_r is not None and float(bore_r) > 0 else None

            dims_str = ""
            try:
                bmin = json.loads(r["bbox_min_mm"]) if r["bbox_min_mm"] else None
                bmax = json.loads(r["bbox_max_mm"]) if r["bbox_max_mm"] else None
                if isinstance(bmin, list) and isinstance(bmax, list) and len(bmin) == 3 and len(bmax) == 3:
                    dx = round(abs(float(bmax[0]) - float(bmin[0])), 1)
                    dy = round(abs(float(bmax[1]) - float(bmin[1])), 1)
                    dz = round(abs(float(bmax[2]) - float(bmin[2])), 1)
                    dims_str = f"{dx}×{dy}×{dz}"
            except Exception:
                pass

            short_key = sku if sku else pid
            label = f"{sku} · {title}" if (sku and not title.lower().startswith(sku.lower())) else title
            bore_txt = f"Ø{bore_d:g}mm" if bore_d is not None else "—"
            desc = (
                f"{title}\n"
                f"Vendor: {vendor} | Category: {cat} | SKU: {sku or '—'}\n"
                f"Bore: {bore_txt} | Size: {dims_str or '—'} mm | Material: {mat} | Anchor: @{anc}\n"
                f"Path: {full_rel}"
            )
            hay = f"{vendor} {cat} {sku} {title} {mat} {bore_txt} {pid}".lower()
            entry = {
                "part_id": pid,
                "key": short_key,
                "vendor": vendor,
                "category": cat,
                "sku": sku,
                "title": title,
                "label": label[:96],
                "description": desc[:900],
                "material": mat,
                "anchor": anc,
                "bore_mm": bore_d,
                "dims_mm": dims_str,
                "rel_path": full_rel,
                "haystack": hay,
            }
            self.parts.append(entry)
            self.by_id[pid] = entry
            if sku:
                self.by_sku_lower[sku.lower()] = entry
            self.by_sku_lower[pid.lower()] = entry

    def find_exact(self, key: str) -> Optional[Dict[str, Any]]:
        k = (key or "").strip()
        if not k:
            return None
        if k in self.by_id:
            return self.by_id[k]
        return self.by_sku_lower.get(k.lower())

    def filter_parts(
        self,
        vendor: str = "ALL",
        category: str = "ALL",
        query: str = "",
        limit: int = 115,
    ) -> Tuple[List[Dict[str, Any]], int]:
        tokens = [t for t in query.lower().strip().split() if t]
        matched: List[Dict[str, Any]] = []
        for p in self.parts:
            if vendor != "ALL" and p["vendor"].lower() != vendor.lower():
                continue
            if category != "ALL" and p["category"].lower() != category.lower():
                continue
            if tokens and not all(tok in p["haystack"] for tok in tokens):
                continue
            matched.append(p)

        # When browsing All/All with no search query, provide a balanced showcase across categories
        if vendor == "ALL" and category == "ALL" and not tokens and len(matched) > limit:
            by_grp: Dict[str, List[Dict[str, Any]]] = {}
            for p in matched:
                g = f"{p['vendor']} · {p['category']}"
                by_grp.setdefault(g, []).append(p)
            showcase: List[Dict[str, Any]] = []
            priority_groups = [
                "goBILDA · motors",
                "goBILDA · servos",
                "goBILDA · structure",
                "goBILDA · bearings",
                "goBILDA · wheels",
                "goBILDA · motion",
                "goBILDA · fasteners",
                "goBILDA · electronics",
                "REV · motors",
                "REV · servos",
                "REV · structure",
                "REV · bearings",
                "REV · wheels",
                "Swyft · structure",
                "Swyft · wheels",
                "ThriftyBot · wheels",
                "Ruthex · fasteners",
            ]
            for g in priority_groups:
                for item in by_grp.get(g, [])[:8]:
                    if len(showcase) < limit:
                        showcase.append(item)
            return showcase, len(matched)

        return matched[:limit], len(matched)


class AresSolidCraftAcpServer:
    """Stateful ACP session handler for SolidCraft's native `egui` Assistant panel."""

    def __init__(self) -> None:
        self.catalog = CotsCatalogIndex()
        self.session_id = "ares-cots-session-1"
        self.vendor = "ALL"
        self.category = "ALL"
        self.action = "insert"
        self.search_query = ""
        self.selected_part_id = self.catalog.parts[0]["part_id"] if self.catalog.parts else "none"
        self.tool_seq = 0

    def _next_tool_id(self) -> str:
        self.tool_seq += 1
        return f"ares-tool-{self.tool_seq}"

    def build_config_options(self) -> List[Dict[str, Any]]:
        """
        Build the 4 native `egui` dropdown/search controls rendered by SolidCraft's
        `config_selects` (`crates/ui-egui/src/agent_chat/panel.rs`).
        Note: SolidCraft caps each ConfigOption at 128 choices (`acp.rs#L70`).
        """
        # 1. Vendor dropdown
        vendor_counts: Dict[str, int] = {}
        for p in self.catalog.parts:
            v = p["vendor"]
            vendor_counts[v] = vendor_counts.get(v, 0) + 1

        vendor_choices = [
            {
                "value": "ALL",
                "name": f"Vendor: All ({len(self.catalog.parts):,})",
                "description": "Show parts across all 7 robotics hardware suppliers",
            }
        ]
        for v, cnt in sorted(vendor_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            vendor_choices.append({
                "value": v,
                "name": f"Vendor: {v} ({cnt:,})",
                "description": f"Filter catalog to {v} ({cnt:,} verified .sc parts)",
            })
        if not any(c["value"] == self.vendor for c in vendor_choices):
            self.vendor = "ALL"

        # 2. Category dropdown (filtered by active vendor)
        cat_counts: Dict[str, int] = {}
        vendor_total = 0
        for p in self.catalog.parts:
            if self.vendor != "ALL" and p["vendor"] != self.vendor:
                continue
            vendor_total += 1
            c = p["category"]
            cat_counts[c] = cat_counts.get(c, 0) + 1

        cat_choices = [
            {
                "value": "ALL",
                "name": f"Category: All ({vendor_total:,})",
                "description": "Show all hardware categories",
            }
        ]
        for c, cnt in sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            cat_choices.append({
                "value": c,
                "name": f"Cat: {c} ({cnt})",
                "description": f"Filter to {c} ({cnt} parts)",
            })
        if not any(c["value"] == self.category for c in cat_choices):
            self.category = "ALL"

        # 3. Click Action dropdown (`category: "mode"`)
        action_choices = [
            {
                "value": "insert",
                "name": "⚡ Click: Insert Part",
                "description": "Picking a part in the COTS Part Picker immediately inserts it into the active SolidCraft viewport",
            },
            {
                "value": "mate_rigid",
                "name": "🔩 Click: Insert + Rigid Mate",
                "description": "Picking a part inserts it and creates a Rigid joint to the face/edge/occurrence selected in the 3D viewport",
            },
            {
                "value": "mate_revolute",
                "name": "🔄 Click: Insert + Revolute Mate",
                "description": "Picking a part inserts it and creates a Revolute joint aligned to the bore/shaft/edge selected in the 3D viewport",
            },
            {
                "value": "preview",
                "name": "🔍 Click: Inspect Specs Only",
                "description": "Picking a part displays its full specification card in the panel without modifying the CAD model",
            },
        ]
        if not any(c["value"] == self.action for c in action_choices):
            self.action = "insert"

        # 4. Searchable COTS Part Picker (`category: "model"` -> SolidCraft renders `ui_kit::search_picker`!)
        filtered, total_matches = self.catalog.filter_parts(
            vendor=self.vendor,
            category=self.category,
            query=self.search_query,
            limit=110,
        )
        part_choices: List[Dict[str, Any]] = []
        if self.search_query:
            part_choices.append({
                "value": "__CLEAR_SEARCH__",
                "name": f"✕ Clear filter \"{self.search_query}\" ({total_matches} matches)",
                "description": "Reset custom search filter and show full category list",
            })

        # Ensure currently selected part is present in the options list so `acp::config_options` validates
        selected_entry = self.catalog.find_exact(self.selected_part_id)
        for p in filtered:
            part_choices.append({
                "value": p["part_id"],
                "name": p["label"],
                "description": p["description"],
                "group": f"{p['vendor']} · {p['category']}",
            })

        if selected_entry and not any(c["value"] == selected_entry["part_id"] for c in part_choices):
            if len(part_choices) >= 120:
                part_choices.pop()
            part_choices.insert(
                0,
                {
                    "value": selected_entry["part_id"],
                    "name": selected_entry["label"],
                    "description": selected_entry["description"],
                    "group": f"{selected_entry['vendor']} · {selected_entry['category']}",
                },
            )

        flat_vals = [c["value"] for c in part_choices]
        if not flat_vals:
            part_choices.append({
                "value": "none",
                "name": "No matching parts (type a new search or reset filters)",
                "description": "Type any keyword or SKU in the search box or reset Vendor/Category",
                "group": "Status",
            })
            self.selected_part_id = "none"
        elif self.selected_part_id not in flat_vals:
            self.selected_part_id = flat_vals[0]

        grouped_part_options: List[Dict[str, Any]] = []
        groups_map: Dict[str, List[Dict[str, Any]]] = {}
        for c in part_choices:
            grp = str(c.get("group") or "Filter / Reset")
            groups_map.setdefault(grp, []).append({
                "value": c["value"],
                "name": c["name"],
                "description": c.get("description", ""),
            })
        for grp_name, grp_items in groups_map.items():
            grouped_part_options.append({
                "group": grp_name,
                "name": grp_name,
                "options": grp_items,
            })

        return [
            {
                "id": "vendor",
                "name": "Vendor",
                "description": "Filter 2,588 COTS parts by manufacturer",
                "category": "vendor",
                "type": "select",
                "currentValue": self.vendor,
                "options": vendor_choices[:64],
            },
            {
                "id": "category",
                "name": "Category",
                "description": "Filter COTS parts by hardware category",
                "category": "category",
                "type": "select",
                "currentValue": self.category,
                "options": cat_choices[:64],
            },
            {
                "id": "action",
                "name": "Click Action",
                "description": "Action performed when you select a part in the COTS Part Picker",
                "category": "mode",
                "type": "select",
                "currentValue": self.action,
                "options": action_choices,
            },
            {
                "id": "part",
                "name": "COTS Part Picker (Search / Pick)",
                "description": "Click any COTS part to insert/mate, or type a search query in the box and press Enter",
                "category": "model",
                "type": "select",
                "currentValue": self.selected_part_id,
                "options": grouped_part_options[:64],
            },
        ]

    def _resolve_live_viewport_selection(self) -> Dict[str, Any]:
        """
        Inspect the user's current 3D viewport selection in live SolidCraft (`ui.selection` +
        `model.faces` / `component.list`) to extract a target occurrence, 3D point `[x, y, z]`,
        and axis `[zx, zy, zz]` for concentric auto-mating.
        """
        from tools.solidcraft_plugin import send_solidcraft_control_rpc

        sel_resp = send_solidcraft_control_rpc("ui.selection", {}, timeout=1.0)
        if not sel_resp.get("ok"):
            return {}
        res = sel_resp.get("result") or {}
        items = list(res.get("selection") or [])
        if not items and isinstance(res.get("dialog"), list):
            for d_in in res["dialog"]:
                items.extend(d_in.get("items") or [])
        if not items and isinstance(res.get("hover"), dict):
            items.append(res["hover"])
        if not items:
            return {}

        first = items[0]
        if not isinstance(first, dict):
            return {}

        sel_type = str(first.get("type") or "").lower()
        point = first.get("point") if isinstance(first.get("point"), list) and len(first.get("point")) == 3 else [0.0, 0.0, 0.0]
        axis = [0.0, 0.0, 1.0]
        body_name = str(first.get("body") or "")
        face_idx = first.get("index")

        # If a face was selected, query model.faces to get exact cylinder axis/center or plane normal/centroid
        if sel_type == "face" and body_name and face_idx is not None:
            f_resp = send_solidcraft_control_rpc(
                "engine.execute",
                {"command": "model.faces", "params": {"body": body_name}},
                timeout=1.0,
            )
            if f_resp.get("ok") and isinstance(f_resp.get("result"), dict):
                for face in f_resp["result"].get("faces") or []:
                    if face.get("index") == face_idx:
                        if isinstance(face.get("centroid"), list) and len(face["centroid"]) == 3:
                            point = [float(x) for x in face["centroid"]]
                        cyl = face.get("cylinder")
                        if isinstance(cyl, dict):
                            if isinstance(cyl.get("axis"), list) and len(cyl["axis"]) == 3:
                                axis = [float(x) for x in cyl["axis"]]
                            if isinstance(cyl.get("origin"), list) and len(cyl["origin"]) == 3:
                                # Project centroid onto cylinder axis for exact bore center
                                orig = [float(x) for x in cyl["origin"]]
                                d = [point[i] - orig[i] for i in range(3)]
                                proj = d[0] * axis[0] + d[1] * axis[1] + d[2] * axis[2]
                                point = [round(orig[i] + proj * axis[i], 4) for i in range(3)]
                        elif isinstance(face.get("plane_normal"), list) and len(face["plane_normal"]) == 3:
                            axis = [float(x) for x in face["plane_normal"]]
                        break

        # Map body or occurrence path to a component occurrence name if possible
        target_occ: Optional[str] = None
        c_resp = send_solidcraft_control_rpc(
            "engine.execute",
            {"command": "component.list", "params": {"compact": True}},
            timeout=1.0,
        )
        if c_resp.get("ok") and isinstance(c_resp.get("result"), dict):
            all_occs: List[Tuple[Any, str]] = []
            for comp in c_resp["result"].get("components") or []:
                for occ in comp.get("occurrences") or []:
                    if occ.get("name"):
                        all_occs.append((occ.get("id"), str(occ["name"])))
            if sel_type == "occurrence" and isinstance(first.get("path"), list) and first["path"]:
                occ_id = first["path"][-1]
                for oid, oname in all_occs:
                    if oid == occ_id:
                        target_occ = oname
                        break
            if not target_occ and body_name:
                for _, oname in all_occs:
                    base = oname.split(":")[0]
                    if base.lower() in body_name.lower() or body_name.lower() in base.lower():
                        target_occ = oname
                        break
            if not target_occ and all_occs:
                target_occ = all_occs[0][1]

        return {
            "selection_type": sel_type,
            "body": body_name,
            "target_occurrence": target_occ,
            "target_point": [round(float(x), 4) for x in point],
            "target_axis": [round(float(x), 4) for x in axis],
        }

    def _parse_prompt_context_selection(self, prompt_text: str) -> Dict[str, Any]:
        """
        Parse `[Selected in SolidCraft when this message was sent: ...]` appended by
        SolidCraft's `Attach selection` or `Pick in model` (`crates/ui-egui/src/agent_chat/context.rs`).
        """
        if "[Selected in SolidCraft" not in prompt_text:
            return {}
        ctx_block = prompt_text.split("[Selected in SolidCraft", 1)[1]
        pt_match = re.search(r"centroid\s*\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)", ctx_block)
        if not pt_match:
            pt_match = re.search(r"pick point\s*\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)", ctx_block)
        if not pt_match:
            pt_match = re.search(r"at\s*\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)", ctx_block)
        point = (
            [float(pt_match.group(1)), float(pt_match.group(2)), float(pt_match.group(3))]
            if pt_match
            else [0.0, 0.0, 0.0]
        )

        norm_match = re.search(r"normal\s*\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)", ctx_block)
        axis = (
            [float(norm_match.group(1)), float(norm_match.group(2)), float(norm_match.group(3))]
            if norm_match
            else [0.0, 0.0, 1.0]
        )

        body_match = re.search(r"·\s*([^\n:]+)", ctx_block)
        body_name = body_match.group(1).strip() if body_match else ""
        live_sel = self._resolve_live_viewport_selection()
        return {
            "body": body_name or live_sel.get("body", ""),
            "target_occurrence": live_sel.get("target_occurrence"),
            "target_point": point if pt_match else live_sel.get("target_point", [0.0, 0.0, 0.0]),
            "target_axis": axis if norm_match else live_sel.get("target_axis", [0.0, 0.0, 1.0]),
        }

    def _execute_part_action(
        self,
        part_entry: Dict[str, Any],
        mode: str,
        explicit_selection: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Execute `insert`, `mate_rigid`, `mate_revolute`, or `preview` for `part_entry`
        against live SolidCraft (`127.0.0.1:27878`), emitting native ACP tool cards.
        """
        key = part_entry["sku"] or part_entry["part_id"]
        title = part_entry["title"]
        vendor = part_entry["vendor"]
        cat = part_entry["category"]
        bore_txt = f"Ø{part_entry['bore_mm']:g} mm" if part_entry["bore_mm"] is not None else "—"

        if mode == "preview":
            return (
                f"### 📦 `{key}` — {title}\n"
                f"- **Vendor / Category:** `{vendor}` / `{cat}`\n"
                f"- **Bore:** `{bore_txt}` | **Envelope:** `{part_entry['dims_mm'] or '—'} mm`\n"
                f"- **Material:** `{part_entry['material']}` | **Normalized Anchor:** `@{part_entry['anchor']}`\n"
                f"- **Native `.sc` File:** `{part_entry['rel_path']}`\n\n"
                f"*Tip: Set **Click Action** to `⚡ Click: Insert Part` (or type `insert {key}`) to place this part into your active viewport.*"
            )

        from tools.solidcraft_cots import solidcraft_insert_cots
        from tools.solidcraft_plugin import detect_live_solidcraft_port

        port = detect_live_solidcraft_port(27878)
        sel = explicit_selection or self._resolve_live_viewport_selection()
        target_occ = sel.get("target_occurrence")
        target_pt = sel.get("target_point") or [0.0, 0.0, 0.0]
        target_ax = sel.get("target_axis") or [0.0, 0.0, 1.0]

        joint_type: Optional[str] = None
        if mode == "mate_rigid":
            joint_type = "rigid"
        elif mode == "mate_revolute":
            joint_type = "revolute"

        tid = self._next_tool_id()
        tool_title = (
            f"component.insert_design + joint.create ({joint_type}): {key} ({title})"
            if (joint_type and target_occ)
            else f"component.insert_design: {key} ({title})"
        )
        raw_in = {
            "key": key,
            "path": part_entry["rel_path"],
            "reuse": True,
            "joint_type": joint_type,
            "target_occurrence": target_occ,
            "target_point": target_pt,
            "target_axis": target_ax,
        }
        _tool_event(self.session_id, tid, tool_title, "edit", "in_progress", raw_input=raw_in)

        try:
            res = solidcraft_insert_cots(
                key,
                translate=None if (joint_type and target_occ) else target_pt,
                joint_type=joint_type if target_occ else None,
                target_occurrence=target_occ if joint_type else None,
                target_point=target_pt if (joint_type and target_occ) else None,
                target_axis=target_ax if (joint_type and target_occ) else None,
                connect_port=port,
            )
            _tool_event(
                self.session_id,
                tid,
                tool_title,
                "edit",
                "completed",
                raw_input=raw_in,
                output_text=json.dumps(res, indent=2),
            )
            j_msg = (
                f"\n- **Auto-Mated Joint:** `{res.get('joint_created')}` (`{joint_type}`) → `{target_occ}` at `{target_pt}`"
                if res.get("joint_created")
                else f"\n- **Placed At:** `{target_pt} mm` (Anchor `@{res.get('recommended_anchor')}`)"
            )
            origins = list((res.get("registered_joint_origins") or {}).values())
            orig_preview = ", ".join(f"`{o}`" for o in origins[:4])
            return (
                f"✅ **Inserted `{key}` — {title}**\n"
                f"- **Component / Occurrence:** `{res.get('component_name')}` (occ `#{res.get('occurrence_id')}`){j_msg}\n"
                f"- **Material:** `{res.get('material')}` | **Bore:** `{bore_txt}`\n"
                f"- **Registered Joint Connectors:** {orig_preview or '—'}"
            )
        except Exception as exc:
            _tool_event(
                self.session_id,
                tid,
                tool_title,
                "edit",
                "failed",
                raw_input=raw_in,
                output_text=str(exc),
            )
            return f"❌ **Failed to insert `{key}`:** `{exc}`"

    def handle_set_config_option(self, req_id: Any, config_id: str, value: str) -> None:
        """
        Handle native `egui` dropdown selections (`session/set_config_option`) from
        SolidCraft's Assistant header controls.
        """
        notice_md: Optional[str] = None

        if config_id == "vendor":
            self.vendor = value
            self.category = "ALL"
            filtered, total = self.catalog.filter_parts(vendor=self.vendor, category="ALL", query=self.search_query, limit=1)
            if filtered:
                self.selected_part_id = filtered[0]["part_id"]
        elif config_id == "category":
            self.category = value
            filtered, total = self.catalog.filter_parts(vendor=self.vendor, category=self.category, query=self.search_query, limit=1)
            if filtered:
                self.selected_part_id = filtered[0]["part_id"]
        elif config_id == "action":
            self.action = value
        elif config_id == "part":
            if value == "__CLEAR_SEARCH__":
                self.search_query = ""
                filtered, _ = self.catalog.filter_parts(vendor=self.vendor, category=self.category, query="", limit=1)
                if filtered:
                    self.selected_part_id = filtered[0]["part_id"]
            else:
                exact = self.catalog.find_exact(value)
                if exact is not None:
                    self.selected_part_id = exact["part_id"]
                    notice_md = self._execute_part_action(exact, self.action)
                else:
                    # User typed a custom search string into the native `egui` SearchPicker!
                    self.search_query = value.strip()
                    filtered, total = self.catalog.filter_parts(
                        vendor=self.vendor,
                        category=self.category,
                        query=self.search_query,
                        limit=110,
                    )
                    if filtered:
                        self.selected_part_id = filtered[0]["part_id"]
                        rows_md = "\n".join(
                            f"| `{p['sku'] or p['part_id']}` | {p['vendor']}/{p['category']} | {p['title']} | "
                            + (f"Ø{p['bore_mm']:g}mm" if p["bore_mm"] else "—")
                            + f" | `{p['dims_mm'] or '—'}` |"
                            for p in filtered[:12]
                        )
                        notice_md = (
                            f"🔎 **Filtered COTS Part Picker to `\"{self.search_query}\"` ({total} matches)** — "
                            f"open the **COTS Part Picker** dropdown above to 1-click insert any match:\n\n"
                            f"| SKU | Vendor/Cat | Title | Bore | Envelope (mm) |\n"
                            f"| :--- | :--- | :--- | :--- | :--- |\n"
                            f"{rows_md}\n"
                        )
                    else:
                        notice_md = f"⚠️ No COTS parts matched `\"{self.search_query}\"` (Vendor: `{self.vendor}`, Category: `{self.category}`). Resetting filter."
                        self.search_query = ""

        options = self.build_config_options()
        _reply(req_id, {"configOptions": options})
        if notice_md:
            _say(self.session_id, "\n\n" + notice_md + "\n")

    def handle_prompt(self, req_id: Any, prompt_blocks: List[Dict[str, Any]]) -> None:
        """
        Handle interactive commands & queries submitted in the Assistant composer
        (supporting `Pick in model` and `Attach selection`).
        """
        raw_text = "\n".join(
            str(b.get("text") or "") for b in prompt_blocks if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        user_line = raw_text.split("[Selected in SolidCraft", 1)[0].strip()
        cmd_low = user_line.lower()
        ctx_sel = self._parse_prompt_context_selection(raw_text)

        # 1. Help / overview
        if cmd_low in ("", "help", "?", "/help", "commands"):
            _say(
                self.session_id,
                "### 🛠️ ARES Native SolidCraft Parts Library & Toolkit\n"
                "Use the **4 native dropdowns above** for 1-click browsing & insertion, or type any command below:\n"
                "- **`<search query>`** (e.g. `5203`, `8mm hex bearing`, `1120 channel`, `m4x12`) — Filters the top **COTS Part Picker** dropdown and shows a spec table.\n"
                "- **`insert <SKU or query>`** — Inserts the pre-built `.sc` part into the active viewport (at `[0,0,0]` or at your `Pick in model` / `Attach selection` coordinates).\n"
                "- **`mate <SKU or query>`** (or `revolute <SKU>`, `rigid <SKU>`) — Inserts and concentric-mates the part to the face/edge/occurrence selected via **Pick in model** or in the 3D viewport.\n"
                "- **`templates`** or **`template <id>`** — Builds a parametric `build123d` -> `.sc` robot component (`gobilda_planet_plate`, `motor_mount_plate`, `shaft_spacer`, `hex_shaft`, `sensor_bracket`, `belt_pulley_htd5`).\n"
                "- **`bom`** / **`clearance`** / **`fab`** — Runs live assembly BOM + mass/CoM audit, COTS-filtered B-Rep clearance check, or exports print-oriented STLs + laser DXFs.\n",
            )
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 2. Live BOM & Mass Audit
        if cmd_low in ("bom", "mass", "inspect", "status"):
            from tools.solidcraft_inspector import live_inspect_state
            from tools.solidcraft_plugin import detect_live_solidcraft_port

            port = detect_live_solidcraft_port(27878)
            tid = self._next_tool_id()
            _tool_event(self.session_id, tid, "assembly.bom + assembly.mass_properties", "read", "in_progress")
            st = live_inspect_state(port=port)
            _tool_event(self.session_id, tid, "assembly.bom + assembly.mass_properties", "read", "completed", output_text=json.dumps(st, indent=2))
            if not st.get("connected"):
                _say(self.session_id, f"⚠️ Could not connect to live SolidCraft port `{port}`: `{st.get('error')}`")
            else:
                mp = st.get("mass_properties") or {}
                bom_rows = st.get("bom") or []
                bom_md = "\n".join(
                    f"| `{r.get('part_number') or '—'}` | {r.get('name') or '—'} | {r.get('quantity') or 1} | {r.get('bom_type') or 'custom'} |"
                    for r in bom_rows[:25]
                )
                _say(
                    self.session_id,
                    f"### 📊 Live Assembly Audit: `{st.get('design_name')}`\n"
                    f"- **Bodies:** `{st.get('body_count')}` | **Occurrences:** `{st.get('occurrence_count')}` | **Joints:** `{len(st.get('joints') or [])}`\n"
                    f"- **Total Mass:** `{mp.get('total_mass_g', 'n/a')} g` | **Center of Mass:** `{mp.get('center_of_mass_mm', [0, 0, 0])} mm`\n\n"
                    + (
                        f"| Part Number | Component | Qty | Type |\n| :--- | :--- | ---: | :--- |\n{bom_md}\n"
                        if bom_md
                        else "*No component occurrences in BOM yet.*"
                    ),
                )
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 3. Live COTS-Filtered Clearance Audit
        if cmd_low.startswith("clearance") or cmd_low.startswith("audit"):
            from tools.solidcraft_inspector import live_run_clearance_audit
            from tools.solidcraft_plugin import detect_live_solidcraft_port

            port = detect_live_solidcraft_port(27878)
            tid = self._next_tool_id()
            _tool_event(self.session_id, tid, "inspect.clearance (COTS-filtered)", "read", "in_progress")
            res = live_run_clearance_audit(port=port, clearance_mm=1.0, custom_only=True)
            _tool_event(self.session_id, tid, "inspect.clearance (COTS-filtered)", "read", "completed", output_text=json.dumps(res, indent=2))
            _say(
                self.session_id,
                f"### 🔍 COTS-Filtered Custom Clearance Audit\n"
                f"- **Verdict:** `{res.get('verdict', 'UNKNOWN')}`\n"
                f"- **Custom Overlaps (>0.5 mm³):** `{len(res.get('custom_overlaps') or [])}`\n"
                f"- **Filtered Expected COTS Fastener/Bearing Pairs:** `{len(res.get('ignored_cots_overlaps') or [])}`\n",
            )
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 4. Fabrication Export (`fab`)
        if cmd_low in ("fab", "export fab", "fabrication"):
            from tools.solidcraft_inspector import live_export_fabrication_pack
            from tools.solidcraft_plugin import detect_live_solidcraft_port

            port = detect_live_solidcraft_port(27878)
            tid = self._next_tool_id()
            _tool_event(self.session_id, tid, "body.print_face + file.save_mesh + file.export", "edit", "in_progress")
            res = live_export_fabrication_pack(port=port)
            status = "completed" if res.get("status") == "ok" else "failed"
            _tool_event(self.session_id, tid, "body.print_face + file.save_mesh + file.export", "edit", status, output_text=json.dumps(res, indent=2))
            if res.get("status") == "ok":
                _say(
                    self.session_id,
                    f"### 🖨️ Fabrication Pack Exported (`{res.get('project')}`)\n"
                    f"- **3D Prints (`.stl`):** `{res.get('prints_count')}` files\n"
                    f"- **Laser Profiles (`.dxf`):** `{res.get('laser_count')}` files\n"
                    f"- **BOM Markdown:** `{res.get('bom_markdown')}`\n",
                )
            else:
                _say(self.session_id, f"❌ Fabrication export error: `{res.get('error')}`")
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 5. Parametric Templates (`templates` or `template <id>`)
        if cmd_low == "templates" or cmd_low.startswith("template "):
            from tools.solidcraft_builder import build_template_part, list_parametric_templates
            from tools.solidcraft_plugin import detect_live_solidcraft_port

            tpls = list_parametric_templates()
            if cmd_low == "templates":
                rows = "\n".join(
                    f"- **`template {t['id']}`** — *{t['name']}*: {t['description']}"
                    for t in tpls
                )
                _say(self.session_id, f"### ⚙️ Parametric Robotics Templates (6)\n{rows}\n")
                _reply(req_id, {"stopReason": "end_turn"})
                return

            parts_cmd = user_line.split()
            tpl_id = parts_cmd[1] if len(parts_cmd) > 1 else ""
            overrides: Dict[str, float] = {}
            for tok in parts_cmd[2:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        overrides[k.strip()] = float(v.strip())
                    except ValueError:
                        pass
            port = detect_live_solidcraft_port(27878)
            tid = self._next_tool_id()
            _tool_event(self.session_id, tid, f"build_template_part: {tpl_id}", "edit", "in_progress", raw_input={"template_id": tpl_id, "overrides": overrides})
            try:
                res = build_template_part(
                    tpl_id,
                    params_override=overrides or None,
                    insert_live=True,
                    target_occurrence=ctx_sel.get("target_occurrence"),
                    target_point=ctx_sel.get("target_point"),
                    connect_port=port,
                )
                _tool_event(self.session_id, tid, f"build_template_part: {tpl_id}", "edit", "completed", output_text=json.dumps(res, indent=2))
                _say(self.session_id, f"✅ Built & inserted parametric template **`{tpl_id}`** (`{res.get('sc_path')}`).")
            except Exception as exc:
                _tool_event(self.session_id, tid, f"build_template_part: {tpl_id}", "edit", "failed", output_text=str(exc))
                _say(self.session_id, f"❌ Template build error: `{exc}`")
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 6. Explicit Insert / Mate command (`insert <query>`, `mate <query>`, `revolute <query>`, `rigid <query>`)
        m_act = re.match(r"^(insert|add|place|mate|revolute|rigid|fasten)\s+(.+)$", user_line, flags=re.IGNORECASE)
        if m_act:
            verb = m_act.group(1).lower()
            q_str = m_act.group(2).strip()
            part = self.catalog.find_exact(q_str)
            if part is None:
                matches, _ = self.catalog.filter_parts(query=q_str, limit=5)
                part = matches[0] if matches else None
            if part is None:
                _say(self.session_id, f"⚠️ No COTS part found matching `\"{q_str}\"`.")
                _reply(req_id, {"stopReason": "end_turn"})
                return

            self.selected_part_id = part["part_id"]
            if verb in ("revolute", "mate"):
                mode = "mate_revolute"
            elif verb in ("rigid", "fasten"):
                mode = "mate_rigid"
            else:
                mode = "mate_rigid" if ctx_sel.get("target_occurrence") else "insert"

            msg_md = self._execute_part_action(part, mode, explicit_selection=ctx_sel if ctx_sel else None)
            _update(
                self.session_id,
                {"sessionUpdate": "config_option_update", "configOptions": self.build_config_options()},
            )
            _say(self.session_id, msg_md)
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 7. If the user typed an exact SKU or part_id directly, execute `self.action` (or insert if selection attached)
        exact = self.catalog.find_exact(user_line)
        if exact is not None:
            self.selected_part_id = exact["part_id"]
            mode = "mate_rigid" if (ctx_sel.get("target_occurrence") and self.action == "insert") else self.action
            msg_md = self._execute_part_action(exact, mode, explicit_selection=ctx_sel if ctx_sel else None)
            _update(
                self.session_id,
                {"sessionUpdate": "config_option_update", "configOptions": self.build_config_options()},
            )
            _say(self.session_id, msg_md)
            _reply(req_id, {"stopReason": "end_turn"})
            return

        # 8. Otherwise treat as a search query: update the top `COTS Part Picker` dropdown & print a Markdown table
        q_clean = re.sub(r"^(search|find|list|show)\s+", "", user_line, flags=re.IGNORECASE).strip()
        self.search_query = q_clean
        matches, total = self.catalog.filter_parts(
            vendor=self.vendor,
            category=self.category,
            query=self.search_query,
            limit=110,
        )
        if not matches and (self.vendor != "ALL" or self.category != "ALL"):
            # Fallback to searching across all vendors/categories
            self.vendor = "ALL"
            self.category = "ALL"
            matches, total = self.catalog.filter_parts(query=self.search_query, limit=110)

        if matches:
            self.selected_part_id = matches[0]["part_id"]
            _update(
                self.session_id,
                {"sessionUpdate": "config_option_update", "configOptions": self.build_config_options()},
            )
            table_rows = "\n".join(
                f"| `{p['sku'] or p['part_id']}` | {p['vendor']}/{p['category']} | {p['title']} | "
                + (f"Ø{p['bore_mm']:g}mm" if p["bore_mm"] else "—")
                + f" | `{p['dims_mm'] or '—'}` |"
                for p in matches[:15]
            )
            _say(
                self.session_id,
                f"### 🔎 Found **{total}** COTS parts matching `\"{q_clean}\"`\n"
                f"The **COTS Part Picker** dropdown at the top of this panel now lists these {min(total, 110)} parts — "
                f"**click any part in the dropdown above to {self.action}**, or type `insert <SKU>` / `mate <SKU>`:\n\n"
                f"| SKU / ID | Vendor / Cat | Title | Bore | Envelope (mm) |\n"
                f"| :--- | :--- | :--- | :--- | :--- |\n"
                f"{table_rows}\n",
            )
        else:
            self.search_query = ""
            _say(self.session_id, f"⚠️ No COTS parts matched `\"{q_clean}\"` across all 2,588 parts.")
        _reply(req_id, {"stopReason": "end_turn"})

    def serve_stdio(self) -> int:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue

            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "initialize":
                _reply(
                    req_id,
                    {
                        "protocolVersion": 1,
                        "agentInfo": {
                            "name": "ares-parts-library",
                            "title": "ARES COTS Parts Library (2,588 Parts)",
                            "version": "1.0.0",
                        },
                        "agentCapabilities": {
                            "promptCapabilities": {
                                "image": True,
                                "embeddedContext": True,
                            }
                        },
                        "authMethods": [],
                    },
                )
            elif method == "session/new":
                options = self.build_config_options()
                _reply(
                    req_id,
                    {
                        "sessionId": self.session_id,
                        "configOptions": options,
                    },
                )
                _say(
                    self.session_id,
                    "📦 **ARES COTS Parts Library Ready (2,588 `.sc` Parts)** — "
                    "Pick a **Vendor**, **Category**, **Click Action**, and **COTS Part** from the native dropdowns above "
                    "to 1-click insert or auto-mate into your active viewport, or type any SKU/search below (`help` for commands).",
                )
            elif method == "session/set_config_option":
                cid = str(params.get("configId") or "")
                val = str(params.get("value") or "")
                self.handle_set_config_option(req_id, cid, val)
            elif method == "session/prompt":
                prompt_blocks = params.get("prompt") or []
                self.handle_prompt(req_id, prompt_blocks)
            elif method in ("session/set_mode", "session/set_model", "authenticate"):
                if req_id is not None:
                    _reply(req_id, {})
            elif method == "session/cancel":
                pass
            elif req_id is not None:
                _reply_error(req_id, -32601, f"Method not found: {method}")
        return 0


def register_in_live_solidcraft(port: int = 27878, activate: bool = False) -> Dict[str, Any]:
    """
    Register `ARES COTS Parts Library` as a custom ACP agent in SolidCraft's native
    Assistant switcher (`ui.agent_chat` -> `add_agent`), and link `cots/` into
    SolidCraft's `agent-chat/work/` directory so both `ARES COTS Parts Library` and
    `Google Antigravity 1.4.5` can access all 2,588 `.sc` parts inside the sandbox.
    """
    from tools.solidcraft_plugin import detect_live_solidcraft_port, send_solidcraft_control_rpc

    actual_port = detect_live_solidcraft_port(port)
    py_exe = (WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    script_path = Path(__file__).resolve()
    cmd_str = f'"{py_exe}" "{script_path}"'

    # 1. Create directory junction `cots` inside SolidCraft's `agent-chat/work` folder
    # so `Google Antigravity 1.4.5` can also read `cots/solidcraft/*.sc` directly inside its cwd!
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    work_dir = Path(appdata) / "SolidCraft" / "data" / "agent-chat" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    cots_link = work_dir / "cots"
    if not cots_link.exists():
        import subprocess
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(cots_link), str(COTS_DIR.resolve())],
            capture_output=True,
            text=True,
        )

    agents_md = work_dir / "AGENTS.md"
    agents_md.write_text(
        "# ARES SolidCraft COTS Hardware Library (`cots/solidcraft/`)\n\n"
        "- All 2,588 FTC/FRC COTS parts (goBILDA, REV, Swyft, ThriftyBot, McMaster, ISO, Ruthex) are pre-compiled as native `.sc` designs inside `cots/solidcraft/<Vendor>/<Category>/<SKU>_<Title>.sc`.\n"
        "- Catalog index: `cots/solidcraft/CATALOG.md` and `cots/solidcraft/index.json`.\n"
        "- To insert a COTS part into the active SolidCraft design via the `solidcraft-app` MCP server, call `execute` with:\n"
        '  `{"command": "component.insert_design", "params": {"path": "cots/solidcraft/<Vendor>/<Category>/<File>.sc", "reuse": true}}`.\n',
        encoding="utf-8",
    )

    # 2. Register `ares-parts-library` in live SolidCraft
    rpc_params: Dict[str, Any] = {
        "add_agent": {
            "id": "ares-parts-library",
            "name": "📦 ARES COTS Parts Library (2,588 Parts)",
            "kind": "acp",
            "command": cmd_str,
        },
        "open": True,
    }
    if activate:
        rpc_params["agent"] = "ares-parts-library"
        rpc_params["connect"] = True

    res = send_solidcraft_control_rpc("ui.agent_chat", rpc_params, port=actual_port, timeout=3.0)
    return {
        "status": "ok" if res.get("ok") else "error",
        "command": cmd_str,
        "work_dir_cots_linked": cots_link.exists(),
        "solidcraft_reply": res,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        activate_flag = "--activate" in sys.argv
        print(json.dumps(register_in_live_solidcraft(activate=activate_flag), indent=2))
        raise SystemExit(0)
    raise SystemExit(AresSolidCraftAcpServer().serve_stdio())
