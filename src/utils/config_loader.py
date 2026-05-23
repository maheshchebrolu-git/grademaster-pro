import json
import os

from src.utils.prompt_config import parse_prompt_context_file


def load_grading_config(assignment_name):
    config_path = "config.json"

    if not os.path.exists(config_path):
        raise FileNotFoundError("❌ config.json missing! Create it in the root directory.")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assignments = data.get("assignments", {})
    if isinstance(assignments, list):
        # Backward compatibility for older list-based config format.
        assignment_data = None
        for item in assignments:
            name = str(item.get("name", "")).strip().lower().replace(" ", "_")
            item_id = str(item.get("id", "")).strip().lower()
            if assignment_name.lower() in {name, item_id}:
                assignment_data = {
                    "id": item.get("id", ""),
                    "url": item.get("canvas_url", ""),
                    "sections": item.get("sections", ["dl2", "dl3", "dl4"]),
                    "section_urls": item.get("section_urls", {}),
                    "section_meta": item.get("section_meta") or {},
                    "max_points": item.get("max_points", 100),
                    "comment_min_words": item.get("comment_min_words", 6),
                    "comment_max_words": item.get("comment_max_words", 12),
                    "comment_style": item.get("comment_style", "firm but encouraging"),
                }
                break
    else:
        assignment_data = assignments.get(assignment_name.lower())

    if not assignment_data:
        raise ValueError(f"❌ Assignment '{assignment_name}' not found in config.json")

    prompt_fallback = {}
    has_comment_bounds = "comment_min_words" in assignment_data and "comment_max_words" in assignment_data
    has_max_points = "max_points" in assignment_data
    if not has_comment_bounds or not has_max_points:
        candidate_paths = [
            os.path.expanduser(f"~/documents/grading/{assignment_name}/prompts/context.txt"),
            os.path.join("prompts", "context.txt"),
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                prompt_fallback = parse_prompt_context_file(p)
                break

    return {
        "course_id": data.get("course_id", ""),
        "assignment_id": assignment_data["id"],
        "url": assignment_data.get("url", ""),
        "sections": assignment_data.get("sections", ["dl2", "dl3", "dl4"]),
        "section_urls": assignment_data.get("section_urls", {}),
        "section_meta": assignment_data.get("section_meta") or {},
        "max_points": int(assignment_data.get("max_points", prompt_fallback.get("max_points", 100))),
        "comment_min_words": int(
            assignment_data.get("comment_min_words", prompt_fallback.get("comment_min_words", 6))
        ),
        "comment_max_words": int(
            assignment_data.get("comment_max_words", prompt_fallback.get("comment_max_words", 12))
        ),
        "comment_style": str(
            assignment_data.get("comment_style", prompt_fallback.get("comment_style", "firm but encouraging"))
        ),
    }


def resolve_section_canvas_ids(config: dict, section: str) -> tuple[str, str]:
    """
    Return (course_id, canvas_assignment_id) for SpeedGrader deep links.

    When each section maps to a different Canvas course/assignment (section_meta
    from setup_and_run), use those IDs. Otherwise fall back to the global
    course_id and assignment_id on the config object.
    """
    key = (section or "").strip().lower()
    meta_root = config.get("section_meta") or {}
    block = meta_root.get(key) if isinstance(meta_root, dict) else None
    if isinstance(block, dict):
        cid = block.get("course_id")
        aid = block.get("assignment_id")
        if cid is not None and str(cid).strip() and aid is not None and str(aid).strip():
            return str(cid).strip(), str(aid).strip()
    return str(config.get("course_id", "")), str(config.get("assignment_id", ""))
