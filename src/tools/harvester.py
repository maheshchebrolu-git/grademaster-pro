import json
import random
import time
import os
import sys
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse
from playwright.sync_api import sync_playwright
from src.utils.config_loader import load_grading_config
from src.utils.prompt_config import parse_prompt_context_text, parse_rubric_questions
from src.utils.notify import notify_and_wait

def human_jitter(min_s=3, max_s=6):
    """Mimics a tired TA reading a PDF to stay under the radar."""
    time.sleep(random.uniform(min_s, max_s))


def _micro_pause():
    """Very short pause mimicking a mouse move or focus shift."""
    time.sleep(random.uniform(0.3, 0.9))


def _reading_pause():
    """Medium pause as if reading the student's submission."""
    time.sleep(random.uniform(3.0, 8.0))


def _between_students_pause():
    """Longer pause between students — like a TA sipping coffee and moving on."""
    time.sleep(random.uniform(5.0, 12.0))


def _safe_student_slug(raw_name: str, fallback: str) -> str:
    """
    Convert UI text into filesystem-safe ASCII slug.
    Prevents weird Unicode/control chars in folder names.
    """
    cleaned = unicodedata.normalize("NFKD", raw_name)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or fallback


def _filename_from_href_and_response(href: str, resp, index: int) -> str:
    """
    Build a stable filename from Content-Disposition first, then href fallback.
    Avoids collisions from Canvas download URLs that often end with '/download'.
    """
    header = (resp.headers.get("content-disposition", "") or "").strip()
    if "filename=" in header:
        candidate = header.split("filename=", 1)[1].strip().strip('"').strip("'")
        candidate = os.path.basename(candidate)
        if candidate:
            return candidate

    parsed = urlparse(href)
    path = unquote(parsed.path)
    file_id_match = re.search(r"/files/(\d+)/download", path)
    file_id = file_id_match.group(1) if file_id_match else str(index)

    ext = ".bin"
    content_type = (resp.headers.get("content-type", "") or "").lower()
    if "pdf" in content_type:
        ext = ".pdf"
    elif "wordprocessingml" in content_type:
        ext = ".docx"
    elif "msword" in content_type:
        ext = ".doc"
    elif "png" in content_type:
        ext = ".png"
    elif "jpeg" in content_type or "jpg" in content_type:
        ext = ".jpg"

    return f"file_{file_id}{ext}"


def _extract_canvas_student_id(page_url: str) -> str:
    """
    Extract Canvas student_id from SpeedGrader URL fragment/query when present.
    """
    match = re.search(r"student_id['\":= ]+(\d+)", page_url)
    if match:
        return match.group(1)
    return ""


def harvest_all_student_files(page, student_name, dl_folder):
    """Downloads every file submitted by the student for this attempt."""
    student_slug = _safe_student_slug(student_name, "unknown_student")
    student_path = os.path.join(dl_folder, student_slug)
    os.makedirs(student_path, exist_ok=True)

    # Exact SpeedGrader download buttons (from provided DOM), plus fallback list links.
    file_links = page.locator(
        'aside a[data-cid="BaseButton IconButton"][href*="/files/"][href*="/download"], '
        '.submission-file-list a[href*="/files/"][href*="/download"]'
    )
    count = file_links.count()
    hrefs = []
    for i in range(count):
        href = file_links.nth(i).get_attribute("href") or ""
        if not href:
            continue
        hrefs.append(href)
    # Deduplicate while preserving order.
    hrefs = list(dict.fromkeys(hrefs))

    if not hrefs:
        print(f"⚠️ No file links discovered for {student_name}.")
        return student_path, []

    os.makedirs(student_path, exist_ok=True)
    downloaded_paths = []

    for i, href in enumerate(hrefs, start=1):
        try:
            # Small pause before each file request — like clicking a link manually.
            _micro_pause()

            # First try direct authenticated request (more reliable than click-download).
            resp = page.context.request.get(href)
            if resp.ok:
                filename = _filename_from_href_and_response(href, resp, i)
                save_path = os.path.join(student_path, filename)
                with open(save_path, "wb") as f:
                    f.write(resp.body())
                downloaded_paths.append(save_path)
                _micro_pause()
                continue

            # Fallback to browser download event if direct request fails.
            with page.expect_download(timeout=5000) as download_info:
                page.locator(f'a[href="{href}"]').first.click()
            download = download_info.value
            save_path = os.path.join(student_path, download.suggested_filename)
            download.save_as(save_path)
            downloaded_paths.append(save_path)
            _micro_pause()
        except Exception as exc:
            print(f"⚠️ Download failed for {student_name}, file #{i}: {exc}")

    print(f"✅ Downloaded {len(downloaded_paths)} files for {student_name}")
    return student_path, downloaded_paths


def _load_context_and_rubric(assignment: str) -> tuple[str, str]:
    """
    Load context.txt for this assignment and build the per-question output
    schema from its rubric. Returns (context_text, output_schema_example).
    """
    candidates = [
        Path(f"~/documents/grading/{assignment}/prompts/context.txt").expanduser(),
        Path("prompts/context.txt"),
    ]
    context_text = ""
    for p in candidates:
        if p.exists():
            context_text = p.read_text(encoding="utf-8")
            break

    rubric = parse_rubric_questions(context_text)
    if rubric.questions:
        schema = rubric.to_prompt_schema()
    else:
        schema = (
            '{\n  "questions": [\n'
            '    {"id": "<question identifier>", "max_points": <n>, '
            '"awarded": <0-n>, "status": "correct|partial|missing|incorrect", '
            '"issue": "<one complete sentence, ≤8 words, or empty if correct>"}\n'
            "  ]\n}"
        )

    return context_text, schema


def run_harvester(
    assignment,
    section,
    course_url,
    total_students=45,
    max_points=100,
    comment_min_words=6,
    comment_max_words=12,
):
    assignment = assignment.lower()
    section = section.lower()

    base_dir = os.path.expanduser(f"~/documents/grading/{assignment}")
    section_dir = os.path.join(base_dir, section)
    batch_dir = os.path.join(base_dir, "batch_files")

    os.makedirs(section_dir, exist_ok=True)
    os.makedirs(batch_dir, exist_ok=True)

    context_text, output_schema = _load_context_and_rubric(assignment)

    grading_instruction = (
        "You are an expert AI Graduate TA. Grade this student's submission "
        "against the provided solutions.pdf and the rubric below.\n\n"
        "--- RUBRIC & CONTEXT ---\n"
        f"{context_text}\n"
        "--- END RUBRIC ---\n\n"
        "CRITICAL OUTPUT INSTRUCTIONS:\n"
        "Return ONLY valid JSON (no markdown fences, no prose) using this exact structure.\n"
        "For each rubric question, evaluate the student's work and fill in:\n"
        '  - "awarded": integer points earned (0 to max_points for that question)\n'
        '  - "status": one of "correct", "partial", "missing", "incorrect"\n'
        f'  - "issue": if status is NOT "correct", write ONE complete English sentence '
        f'({comment_min_words}-{comment_max_words} words, never more than {comment_max_words} words) '
        "stating what is wrong or missing; it must be grammatically finished, not cut off mid-thought. "
        'Do not end on "to", "and", "or", or "the". If correct, use empty string.\n\n'
        f"OUTPUT SCHEMA:\n{output_schema}\n\n"
        "Rules:\n"
        "- Compare ONLY against solutions.pdf.\n"
        '- Award partial credit for logical attempts (status="partial").\n'
        '- Use status="missing" ONLY if question is completely unanswered.\n'
        "- Issue text must be specific: name the concrete mistake or missing item.\n"
        "- Do NOT include praise, encouragement, or summary text in issues.\n"
        "- Each issue must read as one full sentence; compress wording to stay within the word limit.\n"
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            'playwright_session', 
            headless=False,
            args=['--start-maximized']
        )
        page = context.new_page()
        page.goto(course_url)

        print(f"📂 Saving student files to: {section_dir}")
        notify_and_wait(
            title="GradeMaster — Action Required",
            message=f"Log in to Canvas for {assignment.upper()} {section.upper()}. Once you see Student #1, press ENTER.",
            prompt="🚀 Logged in and ready? Press ENTER to start harvesting: ",
        )

        batch_requests = []

        for i in range(total_students):
            print(f"📦 [Student {i+1}/{total_students}] Processing...")

            # Simulate a TA landing on the page and reading before acting.
            _reading_pause()

            try:
                trigger = page.locator('button[data-testid="student-select-trigger"]')
                raw_student_name = trigger.inner_text().strip()
            except:
                raw_student_name = f"unknown_{i}"

            student_name = _safe_student_slug(raw_student_name, f"unknown_{i}")
            canvas_student_id = _extract_canvas_student_id(page.url)

            no_sub = page.locator("text='This student does not have a submission'")
            if no_sub.is_visible():
                student_folder = os.path.join(section_dir, student_name)
                os.makedirs(student_folder, exist_ok=True)
                print(f"⚠️ {student_name} has no submission. Created empty student folder.")
                _micro_pause()
                page.locator("#next-student-button").click()
                _micro_pause()
                continue

            student_folder, downloaded_paths = harvest_all_student_files(
                page, student_name, section_dir
            )
            if not downloaded_paths:
                print(f"⚠️ No downloadable files found for {student_name}.")
                _micro_pause()
                page.locator("#next-student-button").click()
                _micro_pause()
                continue

            request_parts = [{"text": grading_instruction}]
            request_key = f"{assignment}:{section}:{i + 1:03d}:{canvas_student_id or 'unknown'}:{student_name}"

            batch_requests.append({
                "key": request_key,
                "student_folder": student_folder,
                "local_file_paths": downloaded_paths,
                "student_slug": student_name,
                "canvas_student_id": canvas_student_id,
                "section": section,
                "assignment": assignment,
                "request": {
                    "contents": [
                        {
                            "role": "user",
                            "parts": request_parts,
                        }
                    ]
                },
            })

            # Hover then pause before clicking — mimics human mouse movement.
            next_btn = page.locator("#next-student-button")
            next_btn.hover()
            _micro_pause()
            next_btn.click()

            # Pause after click while page loads, before moving to next student.
            _between_students_pause()

        batch_out = os.path.join(batch_dir, f"batch_input_{section}.jsonl")
        with open(batch_out, "w") as f:
            for entry in batch_requests:
                f.write(json.dumps(entry) + "\n")
        
        print(f"✅ Created: {batch_out}")
        context.close()

if __name__ == "__main__":
    # usage: python src/tools/harvester.py <assignment_name> <section_name> [total_students]
    if len(sys.argv) < 3:
        print("❌ Usage: python src/tools/harvester.py <assignment_name> <section_name> [total_students]")
        sys.exit(1)

    assignment_arg = sys.argv[1].lower()
    section_arg = sys.argv[2].lower()
    total_students_arg = int(sys.argv[3]) if len(sys.argv) > 3 else 45

    config = load_grading_config(assignment_arg)
    if section_arg not in config["sections"]:
        print(f"❌ Section '{section_arg}' is not configured for {assignment_arg}.")
        sys.exit(1)

    run_harvester(
        assignment_arg,
        section_arg,
        config["url"],
        total_students_arg,
        config.get("max_points", 100),
        config.get("comment_min_words", 6),
        config.get("comment_max_words", 12),
    )