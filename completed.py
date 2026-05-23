#!/usr/bin/env python3
"""
Archive a finished assignment: export Supabase grading_audit to Excel under
~/documents/grading/<assignment>/grades/, then clean Gemini, GCS, Supabase,
and local clutter — keeping only the prompts/ and grades/ folders.

Usage (from project root):
    pip install openpyxl   # once
    python completed.py problem_set_3
    python completed.py problem_set_4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from supabase import create_client

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GCP_BUCKET = os.getenv("GCP_BUCKET_NAME")

client = None
supabase = None

_KEEP_DIR_NAMES = frozenset({"prompts", "grades"})


def _init_clients():
    global client, supabase
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is missing. Add it to your .env file.")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing in .env.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def _read_config() -> dict:
    path = Path("config.json")
    if not path.exists():
        return {"assignments": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_assignment(target: str) -> tuple[str, str]:
    raw = target.strip().lower()
    cfg = _read_config()
    assignments = cfg.get("assignments", {}) if isinstance(cfg, dict) else {}
    if raw in assignments:
        item = assignments[raw] or {}
        return raw, str(item.get("id", raw)).strip()
    for name, item in assignments.items():
        if str((item or {}).get("id", "")).strip().lower() == raw:
            return name, raw
    return raw, raw


def _remove_config_assignment_entry(assignment_name: str):
    path = Path("config.json")
    if not path.exists():
        return
    cfg = _read_config()
    assignments = cfg.get("assignments")
    if not isinstance(assignments, dict):
        return
    if assignment_name in assignments:
        assignments.pop(assignment_name, None)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"  - Removed config entry: {assignment_name}")


def _cell_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _export_grading_audit_to_excel(assignment_name: str, assignment_id: str) -> str | None:
    try:
        from openpyxl import Workbook
    except ImportError:
        print(
            "openpyxl is required for Excel export. Install with:\n"
            "  pip install openpyxl"
        )
        return None

    res = (
        supabase.table("grading_audit")
        .select("*")
        .eq("assignment_id", assignment_id)
        .execute()
    )
    rows = res.data or []

    base = Path.home() / "documents" / "grading" / assignment_name
    grades_dir = base / "grades"
    grades_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = grades_dir / f"grading_audit_export_{ts}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "grading_audit"

    if not rows:
        ws.append(["(no rows for this assignment_id)"])
        wb.save(out_path)
        print(f"Exported 0 rows to {out_path}")
        return str(out_path)

    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    ws.append(keys)
    for row in rows:
        ws.append([_cell_value(row.get(k)) for k in keys])
    wb.save(out_path)
    print(f"Exported {len(rows)} row(s) to {out_path}")
    return str(out_path)


def _prune_local_keep_prompts_and_grades(assignment_name: str) -> None:
    """
    Under ~/documents/grading/<assignment>/, remove everything except prompts/ and grades/.
    """
    base = Path.home() / "documents" / "grading" / assignment_name
    if not base.exists():
        (base / "prompts").mkdir(parents=True, exist_ok=True)
        (base / "grades").mkdir(parents=True, exist_ok=True)
        print(f"Created {base / 'prompts'} and {base / 'grades'}")
        return

    for name in _KEEP_DIR_NAMES:
        (base / name).mkdir(parents=True, exist_ok=True)

    for child in list(base.iterdir()):
        if child.name in _KEEP_DIR_NAMES:
            continue
        if child.is_dir():
            print(f"  - Removing directory: {child}")
            shutil.rmtree(child)
        else:
            print(f"  - Removing file: {child}")
            child.unlink()

    print(f"Kept only {sorted(_KEEP_DIR_NAMES)}/ under {base}")


def run_completed(assignment_target: str) -> None:
    _init_clients()
    assignment_name, assignment_id = _resolve_assignment(assignment_target)
    print(f"Completed workflow: {assignment_name} (assignment_id={assignment_id})")

    if _export_grading_audit_to_excel(assignment_name, assignment_id) is None:
        print("Aborting before Supabase delete (install openpyxl).")
        return

    print(f"Deleting Supabase rows for assignment_id={assignment_id}...")
    try:
        supabase.table("grading_audit").delete().eq("assignment_id", assignment_id).execute()
        print("  - Supabase rows deleted.")
    except Exception as exc:
        print(f"  - Supabase delete failed: {exc}")

    tags = {assignment_name.lower(), assignment_id.lower()}
    print(f"Scanning Gemini for tags: {sorted(tags)}")
    deleted = 0
    for file_obj in client.files.list():
        display_name = (getattr(file_obj, "display_name", "") or "").lower()
        name = (getattr(file_obj, "name", "") or "").lower()
        if any(tag and (tag in display_name or tag in name) for tag in tags):
            client.files.delete(name=file_obj.name)
            deleted += 1
            print(f"  - Deleted from Gemini: {getattr(file_obj, 'display_name', file_obj.name)}")
    print(f"  - Gemini deletions: {deleted}")

    if GCP_BUCKET:
        prefixes = [
            f"batches/{assignment_name}/",
            f"outputs/{assignment_name}/",
            f"screenshots/{assignment_name}/",
            f"{assignment_name}/",
            f"{assignment_id}/",
        ]
        for prefix in prefixes:
            os.system(f"gsutil -m rm -r gs://{GCP_BUCKET}/{prefix} > /dev/null 2>&1")
        print(f"Removed GCS prefixes under gs://{GCP_BUCKET}")
    else:
        print("GCP_BUCKET_NAME missing. Skipping GCS cleanup.")

    _prune_local_keep_prompts_and_grades(assignment_name)
    _remove_config_assignment_entry(assignment_name)
    print(f"Done: {assignment_name}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Export grading_audit to Excel in grades/, then clean cloud resources; "
            "keep only prompts/ and grades/ locally."
        )
    )
    parser.add_argument(
        "assignment",
        help="Assignment key (e.g. problem_set_3)",
    )
    args = parser.parse_args()
    run_completed(args.assignment)


if __name__ == "__main__":
    main()
