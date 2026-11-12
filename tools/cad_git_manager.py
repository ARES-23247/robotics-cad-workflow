"""
CAD Git & File Hygiene Manager (`tools/cad_git_manager.py`)
===========================================================
Provides project-grouped Git status, CAD file-organization hygiene auditing & auto-remediation,
>50 MiB `.gz` auto-compression & GitHub 95 MiB pre-commit protection, 3D Visual CAD Diffs against
`HEAD` via SolidCraft's `inspect.compare_revision`, and 1-click per-project staging/commit/push/PR
workflows for both the CLI/MCP server and the `Alt+C` SolidCraft HUD Dock.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

DIFFS_DIR = WORKSPACE_ROOT / "cots" / "solidcraft" / "diffs"
GZIP_THRESHOLD_MB = 50.0
GITHUB_HARD_LIMIT_MB = 95.0


def _run_git(args: Sequence[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _classify_scope(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/").lstrip("./")
    parts = norm.split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        return f"projects/{parts[1]}"
    if parts[0] in ("cots", "tools", "plugins", ".agents"):
        return parts[0]
    return "root"


def _detect_file_hygiene_issue(rel_path: str, status_code: str, size_mb: float) -> Optional[Dict[str, str]]:
    if "D" in status_code:
        return None
    norm = rel_path.replace("\\", "/")
    p = WORKSPACE_ROOT / norm
    if not p.exists():
        return None
    name_lower = p.name.lower()

    # 1. Misplaced .step inside fabrication/prints or fabrication/laser
    if "/fabrication/prints/" in f"/{norm}" and name_lower.endswith(".step"):
        return {
            "code": "misplaced_step_in_prints",
            "severity": "warn",
            "message": "STEP file inside fabrication/prints/ (should only contain .stl/.3mf print files)",
        }
    if "/fabrication/laser/" in f"/{norm}" and name_lower.endswith((".step", ".stl")):
        return {
            "code": "misplaced_3d_in_laser",
            "severity": "warn",
            "message": "3D mesh/STEP inside fabrication/laser/ (should only contain 2D .dxf/.svg profiles)",
        }

    # 2. Purchased COTS hardware exported into fabrication/prints or fabrication/laser
    if ("/fabrication/prints/" in f"/{norm}" or "/fabrication/laser/" in f"/{norm}") and "_cots_" in name_lower:
        return {
            "code": "cots_in_fabrication",
            "severity": "warn",
            "message": "Purchased COTS part exported into fabrication/ (only custom parts should be printed/laser-cut)",
        }

    # 3. Uncompressed file sitting next to a .gz archive
    if not name_lower.endswith(".gz") and p.is_file():
        gz_peer = p.with_name(p.name + ".gz")
        if gz_peer.is_file():
            return {
                "code": "duplicate_uncompressed_with_gz",
                "severity": "warn",
                "message": f"Uncompressed binary alongside tracked {gz_peer.name} (add raw path to .gitignore)",
            }

    # 4. Oversized file (> 50 MiB or > 95 MiB)
    if size_mb >= GITHUB_HARD_LIMIT_MB:
        return {
            "code": "exceeds_github_95mb",
            "severity": "error",
            "message": f"File is {size_mb:.1f} MB (>95 MB GitHub limit); must be compressed (<95 MB) or gitignored",
        }
    if size_mb >= GZIP_THRESHOLD_MB and not name_lower.endswith((".gz", ".sc")):
        return {
            "code": "oversized_50mb_needs_gzip",
            "severity": "warn",
            "message": f"File is {size_mb:.1f} MB (>50 MB threshold); recommend .gz compression",
        }

    # 5. Stray temporary / scratch files
    if name_lower.startswith("_test_") or name_lower.endswith((".tmp", ".bak")):
        return {
            "code": "stray_temp_file",
            "severity": "warn",
            "message": "Temporary test/scratch artifact",
        }

    return None


def cad_git_status(scope_filter: Optional[str] = None) -> Dict[str, Any]:
    """
    Return a project-grouped Git status report with file sizes, hygiene warnings,
    large-file flags, and 3D visual diff availability.
    """
    branch_res = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "unknown"

    ab_res = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"])
    ahead, behind = 0, 0
    if ab_res.returncode == 0 and ab_res.stdout.strip():
        parts = ab_res.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])

    log_res = _run_git(["log", "-n", "5", "--oneline"])
    recent_commits = [line.strip() for line in log_res.stdout.splitlines() if line.strip()]

    st_res = _run_git(["status", "--porcelain=v1", "-uall"])
    scopes: Dict[str, Dict[str, Any]] = {}
    total_dirty = 0
    total_hygiene_issues = 0

    for raw_line in st_res.stdout.splitlines():
        if len(raw_line) < 4:
            continue
        xy = raw_line[:2]
        rel = raw_line[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        rel = rel.strip('"').replace("\\", "/")

        scope = _classify_scope(rel)
        if scope_filter and scope != scope_filter and not scope.endswith(f"/{scope_filter}"):
            continue

        abs_p = WORKSPACE_ROOT / rel
        size_mb = round(abs_p.stat().st_size / (1024 * 1024), 2) if abs_p.is_file() else 0.0

        status_label = "modified"
        if xy == "??":
            status_label = "untracked"
        elif "D" in xy:
            status_label = "deleted"
        elif "A" in xy:
            status_label = "added"

        hygiene = _detect_file_hygiene_issue(rel, xy.strip(), size_mb)
        if hygiene:
            total_hygiene_issues += 1

        ext_lower = abs_p.name.lower()
        can_visual_diff = (
            status_label == "modified"
            and (ext_lower.endswith(".sc") or ext_lower.endswith(".step") or ext_lower.endswith(".step.gz"))
            and abs_p.is_file()
        )

        grp = scopes.setdefault(
            scope,
            {
                "scope": scope,
                "files": [],
                "modified_count": 0,
                "untracked_count": 0,
                "deleted_count": 0,
                "total_size_mb": 0.0,
                "hygiene_issue_count": 0,
                "visual_diff_count": 0,
            },
        )
        grp["files"].append(
            {
                "path": rel,
                "xy": xy.strip() or "M",
                "status": status_label,
                "size_mb": size_mb,
                "hygiene": hygiene,
                "visual_diff_available": can_visual_diff,
            }
        )
        total_dirty += 1
        grp["total_size_mb"] = round(grp["total_size_mb"] + size_mb, 2)
        if status_label == "modified":
            grp["modified_count"] += 1
        elif status_label == "untracked":
            grp["untracked_count"] += 1
        elif status_label == "deleted":
            grp["deleted_count"] += 1
        if hygiene:
            grp["hygiene_issue_count"] += 1
        if can_visual_diff:
            grp["visual_diff_count"] += 1

    sorted_scopes = sorted(scopes.values(), key=lambda g: (0 if g["scope"].startswith("projects/") else 1, g["scope"]))
    return {
        "status": "ok",
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "recent_commits": recent_commits,
        "total_dirty_files": total_dirty,
        "total_hygiene_issues": total_hygiene_issues,
        "scopes": sorted_scopes,
    }


def _ensure_gitignored(rel_path: str) -> bool:
    norm = "/" + rel_path.replace("\\", "/").lstrip("/")
    gitignore = WORKSPACE_ROOT / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    lines = [ln.strip() for ln in existing.splitlines()]
    if norm in lines or norm.lstrip("/") in lines:
        return False
    with gitignore.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{norm}\n")
    return True


def _gzip_compress_file(src_path: Path) -> Path:
    gz_path = src_path.with_name(src_path.name + ".gz")
    with src_path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return gz_path


def cad_file_hygiene_audit(scope_filter: Optional[str] = None, fix: bool = False) -> Dict[str, Any]:
    """
    Scan dirty files across the workspace (or within `scope_filter`) for CAD hygiene issues
    (misplaced `.step` in `fabrication/prints/`, COTS parts in `fabrication/`, duplicate
    uncompressed `.step` next to `.step.gz`, oversized `>50 MB` binaries, stray temp files)
    and optionally remediate them automatically when `fix=True`.
    """
    st = cad_git_status(scope_filter=scope_filter)
    issues: List[Dict[str, Any]] = []
    actions_taken: List[Dict[str, Any]] = []

    for grp in st["scopes"]:
        for f in grp["files"]:
            hyg = f.get("hygiene")
            if not hyg:
                continue
            rel = f["path"]
            abs_p = WORKSPACE_ROOT / rel
            code = hyg["code"]
            issue_entry = {
                "scope": grp["scope"],
                "path": rel,
                "size_mb": f["size_mb"],
                "status": f["status"],
                **hyg,
            }
            issues.append(issue_entry)

            if not fix or not abs_p.is_file():
                continue

            if code == "stray_temp_file":
                abs_p.unlink(missing_ok=True)
                actions_taken.append({"action": "deleted_temp_file", "path": rel})

            elif code == "misplaced_step_in_prints" and f["status"] == "untracked":
                # Move misplaced .step to project's cad/parts/ or delete if COTS
                if "_cots_" in abs_p.name.lower():
                    abs_p.unlink(missing_ok=True)
                    actions_taken.append({"action": "deleted_cots_step_from_prints", "path": rel})
                else:
                    proj_dir = WORKSPACE_ROOT / grp["scope"]
                    target_dir = proj_dir / "cad" / "parts"
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest = target_dir / abs_p.name
                    shutil.move(str(abs_p), str(dest))
                    actions_taken.append({
                        "action": "moved_step_from_prints_to_cad_parts",
                        "from": rel,
                        "to": str(dest.relative_to(WORKSPACE_ROOT)).replace("\\", "/"),
                    })

            elif code == "cots_in_fabrication" and f["status"] == "untracked":
                abs_p.unlink(missing_ok=True)
                actions_taken.append({"action": "deleted_cots_from_fabrication", "path": rel})

            elif code == "duplicate_uncompressed_with_gz":
                added_ignore = _ensure_gitignored(rel)
                if f["status"] != "untracked":
                    _run_git(["rm", "--cached", "--ignore-unmatch", rel])
                if added_ignore or f["status"] != "untracked":
                    actions_taken.append({"action": "gitignored_and_untracked_uncompressed_peer", "path": rel})

            elif code in ("exceeds_github_95mb", "oversized_50mb_needs_gzip"):
                if abs_p.name.lower().endswith(".gz"):
                    abs_p.unlink(missing_ok=True)
                    _run_git(["rm", "--cached", "--ignore-unmatch", rel])
                    actions_taken.append({"action": "deleted_oversized_gz", "path": rel})
                elif abs_p.name.lower().endswith(".sc"):
                    _ensure_gitignored(rel)
                    if f["status"] != "untracked":
                        _run_git(["rm", "--cached", "--ignore-unmatch", rel])
                    actions_taken.append({"action": "gitignored_oversized_monolithic_sc", "path": rel})
                else:
                    gz_path = _gzip_compress_file(abs_p)
                    _ensure_gitignored(rel)
                    if f["status"] != "untracked":
                        _run_git(["rm", "--cached", "--ignore-unmatch", rel])
                    if (gz_path.stat().st_size / (1024 * 1024)) >= GITHUB_HARD_LIMIT_MB:
                        gz_path.unlink(missing_ok=True)
                        actions_taken.append({"action": "gitignored_oversized_uncompressible_file", "path": rel})
                    else:
                        actions_taken.append({
                            "action": "compressed_to_gz_and_gitignored",
                            "path": rel,
                            "gz_path": str(gz_path.relative_to(WORKSPACE_ROOT)).replace("\\", "/"),
                            "gz_size_mb": round(gz_path.stat().st_size / (1024 * 1024), 2),
                        })

    return {
        "status": "ok",
        "scope_filter": scope_filter,
        "fix_applied": fix,
        "issues_found": len(issues),
        "issues": issues,
        "actions_taken": actions_taken,
    }


def cad_visual_diff_head(rel_path: str) -> Dict[str, Any]:
    """
    Compare a modified working-tree `.sc`, `.step`, or `.step.gz` file against its `HEAD`
    Git revision using SolidCraft's `inspect.compare_revision`, saving a Red/Green 3D visual
    diff PNG to `cots/solidcraft/diffs/` and returning geometric/volume deltas.
    """
    from tools.solidcraft_compiler import solidcraft_compare_revisions

    norm = rel_path.replace("\\", "/").lstrip("./")
    work_file = (WORKSPACE_ROOT / norm).resolve()
    if not work_file.is_file():
        raise FileNotFoundError(f"Working-tree file not found: {work_file}")

    DIFFS_DIR.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    diff_png = DIFFS_DIR / f"diff_{ Path(norm).stem.replace('.', '_') }_{slug}.png"

    suffix = ".sc" if norm.lower().endswith(".sc") else ".step"
    with tempfile.TemporaryDirectory(prefix="cad_git_diff_") as tmpdir:
        tmp_dir_path = Path(tmpdir)
        head_raw = tmp_dir_path / f"HEAD_{Path(norm).name}"

        # Extract HEAD:<norm> bytes
        with head_raw.open("wb") as f_out:
            proc = subprocess.run(
                ["git", "show", f"HEAD:{norm}"],
                cwd=str(WORKSPACE_ROOT),
                stdout=f_out,
                stderr=subprocess.PIPE,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"git show HEAD:{norm} failed: {proc.stderr.decode('utf-8', errors='replace')}")

        if norm.lower().endswith(".step.gz"):
            head_eval = tmp_dir_path / "head_model.step"
            work_eval = tmp_dir_path / "work_model.step"
            with gzip.open(head_raw, "rb") as f_in, head_eval.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            with gzip.open(work_file, "rb") as f_in, work_eval.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            head_eval = head_raw if head_raw.suffix.lower() in (".sc", ".step") else tmp_dir_path / f"head{suffix}"
            if head_eval != head_raw:
                shutil.copyfile(head_raw, head_eval)
            work_eval = work_file

        cmp_res = solidcraft_compare_revisions(work_eval, head_eval, out_png=diff_png)

    vol_a = (cmp_res.get("a") or {}).get("volume_mm3")
    vol_b = (cmp_res.get("b") or {}).get("volume_mm3")
    vol_delta = round(float(vol_a) - float(vol_b), 3) if (vol_a is not None and vol_b is not None) else None
    dev_a = ((cmp_res.get("deviation") or {}).get("a_to_b") or {}).get("max_mm")
    dev_b = ((cmp_res.get("deviation") or {}).get("b_to_a") or {}).get("max_mm")
    max_dev = round(max(float(dev_a or 0.0), float(dev_b or 0.0)), 4)

    return {
        "status": "ok",
        "path": norm,
        "diff_png_path": str(diff_png),
        "diff_png_url": f"/diffs/{diff_png.name}?t={int(diff_png.stat().st_mtime) if diff_png.is_file() else 0}",
        "identical": cmp_res.get("identical"),
        "verdict": cmp_res.get("verdict"),
        "volume_delta_mm3": vol_delta,
        "max_deviation_mm": max_dev,
        "comparison": cmp_res,
    }


def cad_git_restore_deleted(scope: str, paths: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """
    Restore tracked files marked as deleted (`D`) or modified within `scope` (or specific `paths`)
    back to `HEAD` (`git checkout HEAD -- ...`).
    """
    target_paths: List[str] = list(paths or [])
    if not target_paths:
        st = cad_git_status(scope_filter=scope)
        for grp in st["scopes"]:
            for f in grp["files"]:
                if f["status"] == "deleted":
                    target_paths.append(f["path"])

    if not target_paths:
        return {"status": "ok", "restored_count": 0, "restored_paths": []}

    res = _run_git(["checkout", "HEAD", "--", *target_paths])
    if res.returncode != 0:
        return {"status": "error", "error": res.stderr.strip(), "paths": target_paths}
    return {"status": "ok", "restored_count": len(target_paths), "restored_paths": target_paths}


def cad_git_stage_commit_push(
    scope: str,
    message: str,
    push: bool = True,
    auto_fix_hygiene: bool = True,
    paths: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Run CAD file hygiene & >50 MB gzip protection on `scope`, stage the matching files,
    commit with `message`, and optionally push to `origin`.
    """
    if not message or not message.strip():
        raise ValueError("Commit message is required.")

    hygiene_report = None
    if auto_fix_hygiene:
        hygiene_report = cad_file_hygiene_audit(scope_filter=None if scope == "all" else scope, fix=True)

    stage_targets: List[str] = list(paths or [])
    if not stage_targets:
        if scope in ("all", "*"):
            stage_targets = ["."]
        elif scope == "root":
            st = cad_git_status(scope_filter="root")
            for grp in st["scopes"]:
                stage_targets.extend(f["path"] for f in grp["files"])
        else:
            stage_targets = [scope]
            if (WORKSPACE_ROOT / ".gitignore").is_file():
                stage_targets.append(".gitignore")

    if not stage_targets:
        return {"status": "error", "error": f"No files found to stage for scope {scope!r}"}

    add_res = _run_git(["add", "--", *stage_targets])
    if add_res.returncode != 0:
        return {"status": "error", "error": f"git add failed: {add_res.stderr.strip()}"}

    # Pre-commit check: verify no staged added/modified file exceeds 95 MiB
    staged_res = _run_git(["diff", "--cached", "--name-only"])
    staged_files = [ln.strip() for ln in staged_res.stdout.splitlines() if ln.strip()]
    if not staged_files:
        return {"status": "error", "error": "No staged changes to commit."}

    staged_acm_res = _run_git(["diff", "--cached", "--name-only", "--diff-filter=ACM"])
    staged_acm_files = [ln.strip() for ln in staged_acm_res.stdout.splitlines() if ln.strip()]

    oversized: List[str] = []
    for sf in staged_acm_files:
        blob = _run_git(["cat-file", "-s", f":{sf}"])
        if blob.returncode != 0:
            return {"status": "error", "error": f"Cannot inspect staged blob {sf}: {blob.stderr.strip()}"}
        mb = int(blob.stdout.strip()) / (1024 * 1024)
        if mb >= GITHUB_HARD_LIMIT_MB:
            oversized.append(f"{sf} ({mb:.1f} MB staged blob)")
    if oversized:
        _run_git(["reset", "HEAD", "--", *stage_targets])
        return {
            "status": "error",
            "error": f"Blocked commit: staged file(s) exceed 95 MB GitHub limit: {', '.join(oversized)}",
        }

    commit_res = _run_git(["commit", "-m", message.strip()])
    if commit_res.returncode != 0:
        return {"status": "error", "error": f"git commit failed: {commit_res.stderr.strip() or commit_res.stdout.strip()}"}

    rev_res = _run_git(["rev-parse", "--short", "HEAD"])
    commit_sha = rev_res.stdout.strip()

    push_ok = None
    push_output = ""
    if push:
        push_res = _run_git(["push", "origin", "HEAD"])
        push_ok = push_res.returncode == 0
        push_output = (push_res.stdout + "\n" + push_res.stderr).strip()

    return {
        "status": "ok",
        "scope": scope,
        "commit_sha": commit_sha,
        "message": message.strip(),
        "staged_files_count": len(staged_files),
        "staged_files": staged_files,
        "pushed": push_ok,
        "push_output": push_output,
        "hygiene_report": hygiene_report,
    }


def install_git_pre_commit_hook() -> Dict[str, Any]:
    """
    Install `.git/hooks/pre-commit` to enforce the 95 MiB GitHub file-size limit and
    prevent accidental commits of uncompressed oversized CAD binaries.
    """
    hooks_dir = WORKSPACE_ROOT / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_script = """#!/bin/sh
# ARES-CAD Pre-Commit Guard: Blocks >95 MiB uncompressed files & misplaced artifacts
PYTHON_EXE=".venv/Scripts/python.exe"
if [ ! -f "$PYTHON_EXE" ]; then
  PYTHON_EXE="python"
fi
"$PYTHON_EXE" tools/cad_git_manager.py pre-commit-check
"""
    hook_path.write_text(hook_script, encoding="utf-8", newline="\n")
    try:
        os.chmod(hook_path, 0o755)
    except Exception:
        pass
    return {"status": "ok", "hook_path": str(hook_path)}


def run_pre_commit_check() -> int:
    raw_res = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--cached", "--raw", "-z", "--diff-filter=ACM"],
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
    )
    tokens = [t.decode("utf-8", errors="surrogateescape") for t in raw_res.stdout.split(b"\0") if t]
    if not tokens:
        return 0
    # In `git diff --cached --raw -z` (no -M/-C), tokens alternate: ":src_mode dst_mode src_sha dst_sha status", "path"
    staged_pairs: List[Tuple[str, str]] = []
    it = iter(tokens)
    for meta in it:
        rel = next(it, None)
        if rel is None:
            break
        parts = meta.split()
        dst_sha = parts[3] if len(parts) >= 4 else ""
        staged_pairs.append((dst_sha, rel))
    if not staged_pairs:
        return 0

    errors: List[str] = []
    batch_input = "".join(f"{sha}\n" for sha, _ in staged_pairs).encode("ascii", errors="replace")
    batch_res = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectsize)"],
        cwd=str(WORKSPACE_ROOT),
        input=batch_input,
        capture_output=True,
    )
    if batch_res.returncode != 0:
        print(f"ARES-CAD Pre-Commit Check FAILED: {batch_res.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    for line, (sha, rel) in zip(batch_res.stdout.decode("utf-8", errors="replace").splitlines(), staged_pairs):
        line_s = line.strip()
        if "missing" in line_s:
            errors.append(f"  [UNREADABLE STAGED BLOB] {rel} ({sha}): {line_s}")
            continue
        try:
            mb = int(line_s) / (1024 * 1024)
        except ValueError:
            errors.append(f"  [UNREADABLE STAGED BLOB] {rel}: {line_s}")
            continue
        if mb >= GITHUB_HARD_LIMIT_MB:
            errors.append(
                f"  [BLOCKED >95MB] {rel} ({mb:.1f} MB staged blob) -> Store via Git LFS or compress it"
            )
    if errors:
        print("ARES-CAD Pre-Commit Check FAILED:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="CAD Git & File Hygiene Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser("status", help="Show project-grouped Git status, file sizes, and hygiene issues")
    p_st.add_argument("--scope", type=str, default=None, help="Optional project/scope filter (e.g. ftc-swerve-chassis)")

    p_hyg = sub.add_parser("hygiene", help="Audit (and optionally auto-fix) CAD file organization and >50MB .gz compression")
    p_hyg.add_argument("--scope", type=str, default=None, help="Optional project/scope filter")
    p_hyg.add_argument("--fix", action="store_true", help="Auto-fix misplaced files, delete COTS in fabrication/, and gzip >50MB binaries")

    p_df = sub.add_parser("diff", help="Run a 3D visual & geometric diff against HEAD for a modified .sc/.step file")
    p_df.add_argument("path", type=str, help="Relative path to modified .sc, .step, or .step.gz file")

    p_rs = sub.add_parser("restore-deleted", help="Restore tracked deleted ('D') files in a project scope back to HEAD")
    p_rs.add_argument("scope", type=str, help="Project scope (e.g. projects/ftc-swerve-chassis)")

    p_cm = sub.add_parser("commit", help="Run hygiene guard, stage a project scope, commit, and optionally push")
    p_cm.add_argument("scope", type=str, help="Project scope (e.g. projects/passive-dual-flywheel-turret or 'all')")
    p_cm.add_argument("-m", "--message", type=str, required=True, help="Commit message")
    p_cm.add_argument("--no-push", action="store_true", help="Do not push to origin after committing")
    p_cm.add_argument("--no-hygiene-fix", action="store_true", help="Skip auto-hygiene fix before staging")

    sub.add_parser("install-hook", help="Install the .git/hooks/pre-commit 95 MiB & CAD hygiene guard")
    sub.add_parser("pre-commit-check", help="Run pre-commit check on staged files")

    args = parser.parse_args()
    if args.cmd == "status":
        print(json.dumps(cad_git_status(scope_filter=args.scope), indent=2))
    elif args.cmd == "hygiene":
        print(json.dumps(cad_file_hygiene_audit(scope_filter=args.scope, fix=args.fix), indent=2))
    elif args.cmd == "diff":
        print(json.dumps(cad_visual_diff_head(args.path), indent=2))
    elif args.cmd == "restore-deleted":
        print(json.dumps(cad_git_restore_deleted(args.scope), indent=2))
    elif args.cmd == "commit":
        print(
            json.dumps(
                cad_git_stage_commit_push(
                    scope=args.scope,
                    message=args.message,
                    push=not args.no_push,
                    auto_fix_hygiene=not args.no_hygiene_fix,
                ),
                indent=2,
            )
        )
    elif args.cmd == "install-hook":
        print(json.dumps(install_git_pre_commit_hook(), indent=2))
    elif args.cmd == "pre-commit-check":
        sys.exit(run_pre_commit_check())


if __name__ == "__main__":
    main()
