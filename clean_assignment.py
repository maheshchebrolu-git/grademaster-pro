import argparse
import json
import os
import shutil
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


def _init_clients():
    global client, supabase
    if not GEMINI_API_KEY:
        raise ValueError("❌ GEMINI_API_KEY is missing. Add it to your .env file.")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("❌ SUPABASE_URL or SUPABASE_KEY/SUPABASE_SERVICE_ROLE_KEY is missing in .env.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def _read_config() -> dict:
    path = Path("config.json")
    if not path.exists():
        return {"assignments": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_assignment(target: str) -> tuple[str, str]:
    """
    Returns (assignment_name, assignment_id).
    Accepts either assignment key (problem_set_3) or numeric Canvas assignment id.
    """
    raw = target.strip().lower()
    cfg = _read_config()
    assignments = cfg.get("assignments", {}) if isinstance(cfg, dict) else {}

    # target as assignment name key
    if raw in assignments:
        item = assignments[raw] or {}
        return raw, str(item.get("id", raw)).strip()

    # target as assignment id
    for name, item in assignments.items():
        if str((item or {}).get("id", "")).strip().lower() == raw:
            return name, raw

    # fallback: treat same token for both dimensions
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


def targeted_clean(target: str, remove_config_file: bool = False):
    _init_clients()
    assignment_name, assignment_id = _resolve_assignment(target)
    print(f"🧹 Starting targeted clean for: name={assignment_name}, id={assignment_id}")

    # --- 1. Gemini AI Studio ---
    tags = {assignment_name.lower(), assignment_id.lower()}
    print(f"☁️ Scanning Gemini for tags: {sorted(tags)}")
    deleted = 0
    for file_obj in client.files.list():
        display_name = (getattr(file_obj, "display_name", "") or "").lower()
        name = (getattr(file_obj, "name", "") or "").lower()
        if any(tag and (tag in display_name or tag in name) for tag in tags):
            client.files.delete(name=file_obj.name)
            deleted += 1
            print(f"  - Deleted from Gemini: {getattr(file_obj, 'display_name', file_obj.name)}")
    print(f"  - Gemini deletions: {deleted}")

    # --- 2. Supabase ---
    print(f"🗄️ Deleting Supabase records for assignment_id={assignment_id}...")
    try:
        supabase.table("grading_audit").delete().eq("assignment_id", assignment_id).execute()
        print("  - Records cleared from database.")
    except Exception as exc:
        print(f"  - Supabase clean failed or no records found: {exc}")

    # --- 3. GCS ---
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
        print(f"🪣 Removed known GCS prefixes from gs://{GCP_BUCKET}")
    else:
        print("⚠️ GCP_BUCKET_NAME missing in .env. Skipping GCS cleanup.")

    # --- 4. Local workspace folders ---
    local_candidates = [
        os.path.expanduser(f"~/documents/grading/{assignment_name}"),
        os.path.expanduser(f"~/documents/grading/{assignment_id}"),
    ]
    for local_path in dict.fromkeys(local_candidates):
        if os.path.exists(local_path):
            print(f"💻 Wiping local directory: {local_path}")
            shutil.rmtree(local_path)

    # --- 5. Config cleanup ---
    if remove_config_file and Path("config.json").exists():
        Path("config.json").unlink()
        print("  - Removed config.json")
    else:
        _remove_config_assignment_entry(assignment_name)

    print(f"✅ Clean complete for {assignment_name} ({assignment_id}).")


def purge_all_gemini_files():
    """Delete every file currently in AI Studio for this API key."""
    _init_clients()
    print("☁️ Listing all files in AI Studio...")
    files = list(client.files.list())
    if not files:
        print("  - No files found. Nothing to delete.")
        return
    print(f"  - Found {len(files)} files. Deleting all...")
    deleted = 0
    for file_obj in files:
        try:
            client.files.delete(name=file_obj.name)
            deleted += 1
            label = getattr(file_obj, "display_name", None) or file_obj.name
            print(f"  - Deleted: {label}")
        except Exception as exc:
            print(f"  - Failed to delete {file_obj.name}: {exc}")
    print(f"✅ Purged {deleted}/{len(files)} files from AI Studio.")


def main():
    parser = argparse.ArgumentParser(description="Targeted cleanup for one assignment.")
    parser.add_argument(
        "target",
        nargs="?",
        help="Assignment key or assignment id (e.g., problem_set_3 or 1680628). "
             "Not required when using --purge-gemini.",
    )
    parser.add_argument(
        "--remove-config-file",
        action="store_true",
        help="Delete entire config.json instead of removing one assignment entry.",
    )
    parser.add_argument(
        "--purge-gemini",
        action="store_true",
        help="Delete ALL files from AI Studio (clears all orphaned uploads).",
    )
    args = parser.parse_args()

    if args.purge_gemini:
        purge_all_gemini_files()
        return

    if not args.target:
        parser.error("target is required unless using --purge-gemini")

    targeted_clean(args.target, remove_config_file=args.remove_config_file)


if __name__ == "__main__":
    main()
