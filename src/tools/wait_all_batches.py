"""
Poll Gemini until every batch job for this assignment's configured sections
has succeeded, then send a Pushover notification.

Usage:
    python -m src.tools.wait_all_batches problem_set_3

Or:
    python -m src.main problem_set_3 --phase wait-batches
"""

from __future__ import annotations

import json
import os
import sys
import time

from google import genai
from dotenv import load_dotenv

from src.utils.config_loader import load_grading_config
from src.utils.notify import notify

load_dotenv()


def _client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY missing in environment.")
    return genai.Client(api_key=api_key)


def _session_path(assignment: str, section: str) -> str:
    return os.path.expanduser(f"~/documents/grading/{assignment}/batch_files/session_{section}.json")


def run_wait_all_batches(assignment: str, poll_seconds: int = 60) -> int:
    """
    Returns 0 when all jobs succeeded and notification sent, 1 on failure.
    """
    assignment = assignment.lower()
    config = load_grading_config(assignment)
    sections = [s.lower() for s in config.get("sections", [])]
    if not sections:
        print("❌ No sections in config.json for this assignment.")
        return 1

    client = _client()
    job_by_section: dict[str, str] = {}

    for section in sections:
        path = _session_path(assignment, section)
        if not os.path.exists(path):
            print(f"❌ Missing session metadata: {path}")
            print("   Run harvest + upload for this section first.")
            return 1
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        job_id = meta.get("batch_job_id")
        if not job_id:
            print(f"❌ No batch_job_id in session for {section.upper()}. Upload phase may have failed.")
            return 1
        job_by_section[section] = job_id

    print(
        f"⏳ Waiting for {len(job_by_section)} Gemini batch job(s): "
        f"{', '.join(s.upper() for s in job_by_section)}. Polling every {poll_seconds}s...\n"
    )

    while True:
        statuses: dict[str, str] = {}
        failed_section = None
        failed_state = None

        for section, job_name in job_by_section.items():
            job = client.batches.get(name=job_name)
            state = job.state.name
            statuses[section] = state
            if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
                failed_section = section
                failed_state = state
                break

        if failed_section:
            print(f"❌ Batch job for {failed_section.upper()} ended with {failed_state}.")
            print(f"   Status snapshot: {statuses}")
            notify(
                title="GradeMaster — Batch failed",
                message=f"{assignment} section {failed_section.upper()}: {failed_state}. Check AI Studio / logs.",
                priority=1,
            )
            return 1

        pending = [s for s, st in statuses.items() if st != "JOB_STATE_SUCCEEDED"]
        if not pending:
            break

        print(f"⏳ {statuses} — sleeping {poll_seconds}s...")
        time.sleep(poll_seconds)

    label = ", ".join(s.upper() for s in sections)
    notify(
        title="GradeMaster — Batches complete",
        message=(
            f"All Gemini batch jobs succeeded for {assignment} ({label}). "
            f"Run --phase sync for each section, then --phase deliver."
        ),
        priority=0,
    )
    print(f"\n✅ All batch jobs succeeded for {assignment}. Push notification sent.")
    print("   Next: python -m src.main <assignment> <section> --phase sync   (each section)")
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m src.tools.wait_all_batches <assignment_name>")
        sys.exit(1)
    sys.exit(run_wait_all_batches(sys.argv[1]))


if __name__ == "__main__":
    main()
