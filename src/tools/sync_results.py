import time
import json
import os
import re
from google import genai
from supabase import create_client
from dotenv import load_dotenv
from src.agents.refiner import validate_and_refine_structured, validate_and_refine
from src.utils.config_loader import load_grading_config
from src.utils.pipeline_state import mark_section_sync_completed

load_dotenv()


def _get_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ GEMINI_API_KEY missing in environment.")
    return genai.Client(api_key=api_key)


def _get_supabase():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("❌ SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing in environment.")
    return create_client(url, key)


def _extract_response_text(entry: dict) -> str:
    response = entry.get("response", {})
    candidates = response.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates in response.")
    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not parts:
        raise ValueError("No content parts in response.")
    text = parts[0].get("text")
    if not text:
        raise ValueError("No text field in first part.")
    return text


def _parse_grading_json_text(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Empty model response text.")

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    matches = re.findall(r"\{.*\}", text, flags=re.DOTALL)
    for candidate in matches:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError("Could not parse JSON from model response text.")


def _extract_score_comment_loose(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    score_match = re.search(r'"?score"?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)', text, flags=re.IGNORECASE)
    comment_match = re.search(
        r'"?(comment|feedback)"?\s*[:=]\s*"([^"]+)"',
        text,
        flags=re.IGNORECASE,
    )
    if not comment_match:
        comment_match = re.search(
            r'"?(comment|feedback)"?\s*[:=]\s*([^\n,}]+)',
            text,
            flags=re.IGNORECASE,
        )

    if not score_match and not comment_match:
        raise ValueError("Loose extraction failed.")

    out = {}
    if score_match:
        out["score"] = float(score_match.group(1))
    if comment_match:
        out["comment"] = comment_match.group(2).strip().strip('"').strip("'")
    return out


def _parse_key_identity(key: str) -> dict:
    parts = key.split(":")
    if len(parts) >= 5:
        return {
            "assignment_from_key": parts[0],
            "section": parts[1],
            "canvas_student_id": parts[3] if parts[3] != "unknown" else None,
            "student_slug": parts[4],
        }
    return {
        "assignment_from_key": None,
        "section": "",
        "canvas_student_id": None,
        "student_slug": key,
    }


def _normalize_score(raw_score, max_points: int) -> int:
    try:
        score = float(raw_score)
    except Exception:
        score = 0.0

    if max_points < 100 and score > max_points and score <= 100:
        score = (score / 100.0) * max_points

    return int(round(max(0.0, min(float(max_points), score))))


def _process_structured_output(grading_data: dict, max_points: int, comment_min_words: int, comment_max_words: int) -> tuple[int, str, dict]:
    """
    Process a per-question structured response:
    - Sum awarded points for deterministic score
    - Build comment lines from issues
    - Return (final_score, ta_comment, refined_data)
    """
    questions = grading_data.get("questions", [])
    if not questions:
        raise ValueError("No 'questions' array in structured output.")

    refined_questions = validate_and_refine_structured(questions, comment_min_words, comment_max_words)

    total_awarded = 0
    comment_lines = []
    for q in refined_questions:
        awarded = int(q.get("awarded", 0))
        q_max = int(q.get("max_points", 0))
        awarded = max(0, min(q_max, awarded))
        total_awarded += awarded

        status = (q.get("status") or "").lower()
        issue = (q.get("issue") or "").strip()
        qid = (q.get("id") or "").strip()

        if status != "correct" and issue:
            comment_lines.append(f"{qid}: {issue}")

    final_score = max(0, min(max_points, total_awarded))

    ta_comment = "\n".join(comment_lines) if comment_lines else ""

    refined_data = {
        "questions": refined_questions,
        "score": final_score,
        "comment": ta_comment,
    }
    return final_score, ta_comment, refined_data


def _process_flat_output(grading_data: dict, max_points: int, comment_min_words: int, comment_max_words: int, comment_style: str) -> tuple[int, str, dict]:
    """
    Backward-compatible flat output processing for old {score, comment} format.
    """
    grading_data = validate_and_refine(
        grading_data,
        constraints={
            "min_words": comment_min_words,
            "max_words": comment_max_words,
            "tone": comment_style,
            "issue_only": True,
        },
    )
    final_score = _normalize_score(grading_data.get("score"), max_points)
    short_comment = grading_data.get("comment") or grading_data.get("feedback") or ""
    return final_score, short_comment, grading_data


def sync_batch_to_db(assignment, section, resume=True):
    client = _get_client()
    supabase = _get_supabase()

    meta_path = os.path.expanduser(f"~/documents/grading/{assignment}/batch_files/session_{section}.json")
    if not os.path.exists(meta_path):
        print(f"❌ Session metadata not found: {meta_path}")
        return
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    job_name = meta["batch_job_id"]
    if not job_name:
        print("❌ batch_job_id missing. Upload phase likely failed to create a batch job.")
        return

    config = load_grading_config(assignment)
    assignment_id = config["assignment_id"]
    max_points = int(config.get("max_points", 100))
    comment_min_words = int(config.get("comment_min_words", 6))
    comment_max_words = int(config.get("comment_max_words", 12))
    comment_style = str(config.get("comment_style", "firm but encouraging"))
    print(f"📡 Checking status for Job: {job_name}...")

    while True:
        job = client.batches.get(name=job_name)
        state = job.state.name
        if state == "JOB_STATE_SUCCEEDED":
            break
        if state in ["JOB_STATE_FAILED", "JOB_STATE_CANCELLED"]:
            print(f"❌ Job failed with state: {state}")
            return

        print(f"⏳ Current State: {state}. Retrying in 60s...")
        time.sleep(60)

    print("✅ Job Succeeded! Downloading results...")
    dest = getattr(job, "dest", None)
    result_file = getattr(dest, "file_name", None)
    if not result_file:
        print("❌ Batch destination file missing from job response.")
        return
    content_bytes = client.files.download(file=result_file)

    processed = 0
    failed = 0
    flagged = 0
    for line in content_bytes.decode("utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            request_key = entry.get("key")
            if not request_key:
                raise ValueError("Missing key in result record.")

            identity = _parse_key_identity(request_key)
            student_slug = identity["student_slug"]
            section_value = identity["section"] or section
            canvas_student_id = identity["canvas_student_id"]

            if resume:
                existing = (
                    supabase.table("grading_audit")
                    .select("status")
                    .eq("assignment_id", assignment_id)
                    .eq("student_id", request_key)
                    .execute()
                )
                if existing.data and existing.data[0].get("status") == "completed":
                    print(f"⏩ Skipping {request_key} (already completed).")
                    continue

            if "error" in entry:
                error_obj = entry.get("error")
                supabase.table("grading_audit").upsert(
                    {
                        "student_id": request_key,
                        "student_name": student_slug,
                        "assignment_id": assignment_id,
                        "section": section_value,
                        "canvas_student_id": canvas_student_id,
                        "raw_output": entry,
                        "status": "model_error",
                        "internal_ai_justification": json.dumps(error_obj),
                    },
                    on_conflict="assignment_id,student_id",
                ).execute()
                failed += 1
                continue

            raw_response = _extract_response_text(entry)
            try:
                grading_data = _parse_grading_json_text(raw_response)
            except ValueError:
                try:
                    grading_data = _extract_score_comment_loose(raw_response)
                except ValueError:
                    grading_data = {"score": 0, "comment": ""}

            # Route to structured or flat processing.
            if "questions" in grading_data and isinstance(grading_data["questions"], list):
                final_score, short_comment, refined_data = _process_structured_output(
                    grading_data, max_points, comment_min_words, comment_max_words
                )
                print(f"✅ [Structured] {student_slug}: {final_score}/{max_points}")
            else:
                final_score, short_comment, refined_data = _process_flat_output(
                    grading_data, max_points, comment_min_words, comment_max_words, comment_style
                )
                print(f"✅ [Flat/Legacy] {student_slug}: {final_score}/{max_points}")

            # Safety net: non-perfect score with empty comment → flag for review.
            status = "completed"
            if final_score < max_points and not short_comment.strip():
                status = "needs_review"
                flagged += 1
                print(f"🔍 Flagged {student_slug}: score {final_score}/{max_points} but no comment — needs manual review.")

            supabase.table("grading_audit").upsert(
                {
                    "student_id": request_key,
                    "student_name": student_slug,
                    "assignment_id": assignment_id,
                    "section": section_value,
                    "canvas_student_id": canvas_student_id,
                    "final_score": final_score,
                    "ta_comment": short_comment or None,
                    "internal_ai_justification": raw_response,
                    "raw_analysis_json": refined_data,
                    "raw_output": entry,
                    "status": status,
                },
                on_conflict="assignment_id,student_id",
            ).execute()
            processed += 1
        except Exception as exc:
            failed += 1
            print(f"⚠️ Failed to process one result line: {exc}")
            try:
                if "entry" in locals() and isinstance(entry, dict):
                    request_key = entry.get("key")
                    if request_key:
                        identity = _parse_key_identity(request_key)
                        supabase.table("grading_audit").upsert(
                            {
                                "student_id": request_key,
                                "student_name": identity["student_slug"],
                                "assignment_id": assignment_id,
                                "section": identity["section"] or section,
                                "canvas_student_id": identity["canvas_student_id"],
                                "raw_output": entry,
                                "status": "parse_failed",
                            },
                            on_conflict="assignment_id,student_id",
                        ).execute()
            except Exception:
                pass

    print(f"🚀 Synced {processed} records for {section.upper()}; failed: {failed}; flagged for review: {flagged}.")
    mark_section_sync_completed(assignment, section)


# Backward-compatible entrypoint for existing main.py call site.
def sync_cloud_to_db(assignment, section, resume=True):
    return sync_batch_to_db(assignment, section, resume=resume)


if __name__ == "__main__":
    sync_batch_to_db("lab2", "dl2")
