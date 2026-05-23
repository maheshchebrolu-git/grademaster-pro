import argparse
import os
import sys
from dotenv import load_dotenv
from google.cloud import storage
from supabase import create_client
from src.tools.harvester import run_harvester
from src.tools.batch_uploader import cloud_ephemeral_flow, unregister_solution_file
from src.tools.sync_results import sync_cloud_to_db
from src.tools.delivery_agent import run_delivery_agent
from src.tools.status_report import run_status_report
from src.utils.config_loader import load_grading_config, resolve_section_canvas_ids

load_dotenv()

# --- DEVELOPER NOTES ---
# This is my 'Command Center'. I've built this so I don't have to
# remember 5 different script names. I just tell it the lab,
# the section, and the phase, and it handles the rest.


def run_cleanup(assignment: str, section: str):
    """
    Cloud-Ephemeral cleanup:
    1) verify section records are completed in Supabase
    2) delete section screenshots/outputs in GCS
    3) unregister solution PDF from Gemini File API
    """
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    assignment_id = load_grading_config(assignment)["assignment_id"]

    pending = (
        supabase.table("grading_audit")
        .select("id")
        .eq("assignment_id", assignment_id)
        .eq("section", section)
        .neq("status", "completed")
        .execute()
    )

    if pending.data:
        print(f"⚠️ Cleanup blocked: {len(pending.data)} records still not completed.")
        return

    bucket_name = os.getenv("GCP_BUCKET_NAME")
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    prefixes = [
        f"screenshots/{assignment}/{section}/",
        f"outputs/{assignment}/{section}/",
    ]

    deleted = 0
    for prefix in prefixes:
        for blob in bucket.list_blobs(prefix=prefix):
            blob.delete()
            deleted += 1
    print(f"🧹 Deleted {deleted} cloud files for {assignment}/{section}.")

    unregister_solution_file(assignment, section)


def _load_delivery_rows(assignment_id: str, section: str):
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    rows = (
        supabase.table("grading_audit")
        .select("student_name,canvas_student_id,final_score,ta_comment,status")
        .eq("assignment_id", assignment_id)
        .eq("section", section)
        .eq("status", "completed")
        .execute()
    )

    section_data = []
    for row in rows.data or []:
        canvas_id = row.get("canvas_student_id")
        score = row.get("final_score")
        comment = row.get("ta_comment") or ""
        if not canvas_id or score is None:
            continue
        section_data.append(
            {
                "name": row.get("student_name") or "unknown",
                "canvas_student_id": str(canvas_id),
                "final_score": score,
                "ta_comment": comment,
            }
        )
    return section_data


def main():
    parser = argparse.ArgumentParser(description="GradeMaster-Pro: AI TA Orchestrator")

    # Required Arguments
    parser.add_argument("assignment", nargs="?", help="The name of the lab (e.g., lab2)")
    parser.add_argument("section", nargs="?", help="The section code (e.g., dl2, dl3, dl4)")

    # Phase Selector (Default to 'harvest')
    parser.add_argument(
        "--phase",
        choices=["harvest", "upload", "sync", "deliver", "wait-batches"],
        default="harvest",
        help="Choose which part of the pipeline to run (wait-batches: assignment only, polls Gemini)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Run cloud-ephemeral cleanup after selected phase",
    )
    parser.add_argument(
        "--total-students",
        type=int,
        default=45,
        help="Expected number of students for harvesting",
    )
    parser.add_argument(
        "--force",
        action="store_false",
        dest="resume",
        help="Force re-grading of all students, even if already completed",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show GTA dashboard status and exit",
    )

    args = parser.parse_args()

    if args.status:
        assignment_id = None
        if args.assignment:
            assignment_id = load_grading_config(args.assignment.lower())["assignment_id"]
        run_status_report(assignment_id=assignment_id)
        sys.exit()

    if args.phase == "wait-batches":
        if not args.assignment:
            parser.error("assignment is required for --phase wait-batches")
        from src.tools.wait_all_batches import run_wait_all_batches

        sys.exit(run_wait_all_batches(args.assignment.lower()))

    if not args.assignment or not args.section:
        parser.error("assignment and section are required unless using --status or --phase wait-batches")

    # All lowercase to match our naming convention
    assignment = args.assignment.lower()
    section = args.section.lower()
    phase = args.phase
    cleanup = args.cleanup
    total_students = args.total_students
    resume = args.resume

    config = load_grading_config(assignment)

    if section not in config["sections"]:
        raise ValueError(f"❌ Section '{section}' is not configured for '{assignment}'.")

    print(f"🚀 GradeMaster-Pro starting: {assignment} | {section} | Phase: {phase}")

    if phase == "harvest":
        # Phase 1: The 'Eyes' (Canvas Scraper)
        print("📸 Starting Harvester...")
        section_url = config.get("section_urls", {}).get(section, config["url"])
        run_harvester(
            assignment,
            section,
            section_url,
            total_students,
            config.get("max_points", 100),
            config.get("comment_min_words", 6),
            config.get("comment_max_words", 12),
        )

    elif phase == "upload":
        # Phase 2: The 'Economy' (GCP Batch Uploader)
        print("☁️ Uploading to GCP & Wiping Ephemeral files...")
        cloud_ephemeral_flow(assignment, section)

    elif phase == "sync":
        # Phase 3: The 'Brain' (LangGraph Refiner + Supabase Sync)
        print("🧠 Syncing results from Google Cloud to Supabase...")
        sync_cloud_to_db(assignment, section, resume=resume)

    elif phase == "deliver":
        # Phase 4: The 'Hands' (Canvas Grade Typist)
        print("🤖 Delivering grades to Canvas SpeedGrader...")
        section_data = _load_delivery_rows(config["assignment_id"], section)
        if not section_data:
            print("⚠️ No completed rows with canvas_student_id/final_score/comment found for delivery.")
            return
        canvas_course_id, canvas_assignment_id = resolve_section_canvas_ids(config, section)
        print(
            f"📍 Using Canvas course {canvas_course_id}, assignment {canvas_assignment_id} "
            f"for section {section.upper()} (SpeedGrader deep links)."
        )
        run_delivery_agent(canvas_course_id, canvas_assignment_id, section_data)

    if cleanup:
        print("🧼 Running cleanup checks and cloud wipe...")
        run_cleanup(assignment, section)


if __name__ == "__main__":
    main()
