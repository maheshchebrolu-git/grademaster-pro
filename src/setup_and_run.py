import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import set_key
from src.utils.prompt_config import parse_prompt_context_file, parse_rubric_questions
from src.utils.notify import notify


def _clean_speedgrader_url(url: str) -> str:
    parts = urlsplit(url.strip())
    kept_query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in {"student_id", "anonymous_id"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept_query), ""))


def get_ids_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract Course ID and Assignment ID from Canvas SpeedGrader URL."""
    try:
        course_id = re.search(r"/courses/(\d+)", url).group(1)
        assignment_id = re.search(r"assignment_id=(\d+)", url).group(1)
        return course_id, assignment_id
    except AttributeError:
        print(f"❌ Could not parse IDs from URL: {url}")
        return None, None


def _load_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {"course_id": "", "assignments": {}}
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("course_id", "")
    assignments = data.get("assignments", {})
    if isinstance(assignments, list):
        converted = {}
        for item in assignments:
            key = str(item.get("id", "")).lower()
            if not key:
                continue
            converted[key] = {
                "id": item.get("id", ""),
                "url": item.get("canvas_url", ""),
                "section_urls": {},
                "sections": item.get("sections", ["dl2", "dl3", "dl4"]),
            }
        data["assignments"] = converted
    else:
        data.setdefault("assignments", {})
    return data


def _build_system_prompt(assignment_name: str, instructions: str, prompt_cfg: dict) -> str:
    max_points = int(prompt_cfg.get("max_points", 100))
    comment_min = int(prompt_cfg.get("comment_min_words", 6))
    comment_max = int(prompt_cfg.get("comment_max_words", 12))

    rubric = prompt_cfg.get("rubric")
    if rubric and rubric.questions:
        schema_example = rubric.to_prompt_schema()
    else:
        rubric = parse_rubric_questions(instructions)
        if rubric.questions:
            schema_example = rubric.to_prompt_schema()
        else:
            schema_example = (
                '{\n  "questions": [\n'
                '    {"id": "<question identifier>", "max_points": <n>, '
                '"awarded": <0-n>, "status": "correct|partial|missing|incorrect", '
                f'"issue": "<one complete sentence, {comment_min}-{comment_max} words max, or empty if correct>"}}\n'
                '  ]\n}'
            )

    return f"""You are an AI Graduate TA for a Computer Science course.

REFERENCE CONTEXT:
{instructions}

YOUR GOAL:
Compare student submission against the provided solutions.pdf.

ISSUE FIELD (mandatory when status is not "correct"):
- Write exactly one complete English sentence; grammatically finished — never truncated mid-thought.
- Use {comment_min}-{comment_max} words; never more than {comment_max} words.
- Do not end on a dangling preposition or conjunction (e.g. not on "to", "and", "or", "the"). Compress the idea if needed so the sentence reads complete.

OUTPUT FORMAT:
Return ONLY valid JSON (no markdown fences, no prose) with a per-question breakdown:
{schema_example}

Rules for each question:
- "awarded": integer points earned (0 to max_points for that question).
- "status": "correct" if fully right, "partial" for partial credit, "missing" if unanswered, "incorrect" if wrong.
- "issue": if status is NOT "correct", write **one complete sentence** naming the mistake or missing item ({comment_min}-{comment_max} words, never more than {comment_max} words). Empty string if correct.
- Total points across all questions must not exceed {max_points}.

Additional constraints:
- Never compare students across sections.
- Follow assignment instructions exactly.
- Issue text must name the concrete mistake or missing item — no praise, no vague phrases.
"""


def prepare_assignment_space(assignment_id: str) -> tuple[bool, Path, Path, Path]:
    """
    User-managed inputs:
    ~/documents/grading/<assignment_id>/prompts/context.txt
    ~/documents/grading/<assignment_id>/prompts/solutions.pdf

    Script-managed folders:
    batch_files, assets, outputs, logs
    """
    base_dir = Path(f"~/documents/grading/{assignment_id}").expanduser()
    prompt_dir = base_dir / "prompts"
    context_file = prompt_dir / "context.txt"
    solution_pdf = prompt_dir / "solutions.pdf"

    if not prompt_dir.exists() or not context_file.exists() or not solution_pdf.exists():
        print(
            f"❌ Error: Please create {prompt_dir} and add context.txt + solutions.pdf"
        )
        return False, base_dir, context_file, solution_pdf

    for folder in ["batch_files", "assets", "outputs", "logs"]:
        (base_dir / folder).mkdir(parents=True, exist_ok=True)

    print(f"✅ Workspace for {assignment_id} is ready.")
    return True, base_dir, context_file, solution_pdf


def setup_environment():
    print("🚀 --- GTA Master Automator: Launch Control --- 🚀\n")

    assignment_name = input("Enter Assignment Name (e.g., lab2 or problem_set_3): ").strip().lower()
    smoke_limit = input("Students per section for harvest smoke test [3]: ").strip()
    total_students = smoke_limit or "3"
    default_sections = ["dl2", "dl3", "dl4"]
    section_data = {}

    for section in default_sections:
        url = input(f"🔗 Enter SpeedGrader URL for {section.upper()}: ").strip()
        if not url:
            continue

        course_id, assignment_id = get_ids_from_url(url)
        if not course_id or not assignment_id:
            continue

        section_data[section] = {
            "course_id": course_id,
            "assignment_id": assignment_id,
            "url": _clean_speedgrader_url(url),
        }

    if not section_data:
        print("❌ No valid section URLs provided. Aborting setup.")
        return

    sections = list(section_data.keys())
    section_urls = {sec: info["url"] for sec, info in section_data.items()}
    assignment_id = next(iter(section_data.values()))["assignment_id"]
    course_ids = {info["course_id"] for info in section_data.values()}
    course_id = next(iter(course_ids))
    if len(course_ids) > 1:
        print("⚠️ Multiple course IDs detected across sections.")

    is_ready, base_path, context_file, prompt_solution = prepare_assignment_space(assignment_name)
    if not is_ready:
        return

    # Copy the manual solution file into assets as a working copy.
    target_solution = base_path / "assets" / "solutions.pdf"
    shutil.copy(prompt_solution, target_solution)
    print(f"✅ Solution copied to working assets: {target_solution}")

    instructions = context_file.read_text(encoding="utf-8")
    prompt_cfg = parse_prompt_context_file(context_file)
    print(
        "🧭 Parsed prompt config: "
        f"max_points={prompt_cfg['max_points']}, "
        f"comment_words={prompt_cfg['comment_min_words']}-{prompt_cfg['comment_max_words']}"
    )
    system_prompt = _build_system_prompt(assignment_name, instructions, prompt_cfg)
    active_prompt_path = base_path / "assets" / "active_system_prompt.txt"
    active_prompt_path.write_text(system_prompt, encoding="utf-8")
    print(f"✅ System prompt generated: {active_prompt_path}")

    env_path = Path(".env")
    set_key(str(env_path), "SOLUTION_PDF_LOCAL_PATH", str(target_solution))
    set_key(str(env_path), "CURRENT_ASSIGNMENT", assignment_name)
    set_key(str(env_path), "ASSIGNMENT_CONTEXT_PATH", str(context_file))
    set_key(str(env_path), "ACTIVE_SYSTEM_PROMPT_PATH", str(active_prompt_path))

    config_path = Path("config.json")
    config_data = _load_config(config_path)
    config_data["course_id"] = course_id or config_data.get("course_id", "")
    config_data["assignments"][assignment_name] = {
        "id": assignment_id,
        "max_points": int(prompt_cfg.get("max_points", 100)),
        "comment_min_words": int(prompt_cfg.get("comment_min_words", 6)),
        "comment_max_words": int(prompt_cfg.get("comment_max_words", 12)),
        "comment_style": str(prompt_cfg.get("comment_style", "firm but encouraging")),
        "url": next(iter(section_urls.values())),
        "section_urls": section_urls,
        "sections": sections,
        "section_meta": section_data,
    }
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
    print("✅ config.json updated.")

    confirm = input("\nReady to start Harvesting and Uploading? (y/n): ").strip().lower()
    if confirm != "y":
        print("⏸️ Launch cancelled. Setup is complete.")
        return

    for section in sections:
        print(f"\n🌾 Harvesting {section.upper()}...")
        harvest_result = subprocess.run(
            [
                "python",
                "-m",
                "src.main",
                assignment_name,
                section,
                "--phase",
                "harvest",
                "--total-students",
                total_students,
            ],
            check=False,
        )
        if harvest_result.returncode != 0:
            print(f"❌ Harvest failed for {section.upper()} (exit {harvest_result.returncode}). Aborting launch.")
            return

        print(f"📤 Uploading {section.upper()} to AI Studio...")
        upload_result = subprocess.run(
            ["python", "-m", "src.main", assignment_name, section, "--phase", "upload"],
            check=False,
        )
        if upload_result.returncode != 0:
            print(f"❌ Upload failed for {section.upper()} (exit {upload_result.returncode}). Aborting launch.")
            return

    print("\n✨ All batches submitted! Check back later to run --phase sync.")
    notify(
        title="GradeMaster — Batches Submitted",
        message=f"All harvest+upload batches for {assignment_name} are submitted to Gemini. You can sync results when the jobs complete.",
        priority=0,
    )


if __name__ == "__main__":
    setup_environment()
