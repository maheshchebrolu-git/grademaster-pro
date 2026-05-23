import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_PROMPT_CONFIG = {
    "max_points": 100,
    "comment_min_words": 6,
    # Soft ceiling for length shaping in refiner (complete sentences preferred; see refiner._finalize_issue_text).
    "comment_max_words": 12,
    "comment_style": "firm but encouraging",
}


@dataclass
class RubricQuestion:
    qid: str
    max_points: int
    section_label: str = ""
    chapter: int = 0
    kind: str = ""       # "Ex" or "Prob" or ""
    number: int = 0


@dataclass
class ParsedRubric:
    questions: list[RubricQuestion] = field(default_factory=list)
    total_points: int = 0

    def to_prompt_schema(self) -> str:
        """Build the JSON schema example the model should return."""
        lines = []
        for q in self.questions:
            lines.append(
                f'    {{"id": "{q.qid}", "max_points": {q.max_points}, '
                f'"awarded": <0-{q.max_points}>, "status": "correct|partial|missing|incorrect", '
                f'"issue": "<one complete sentence, ≤8 words, or empty if correct>"}}'
            )
        joined = ",\n".join(lines)
        return f'{{\n  "questions": [\n{joined}\n  ]\n}}'


def parse_rubric_questions(text: str) -> ParsedRubric:
    """
    Extract per-question rubric items from context.txt.
    Recognizes patterns like:
      - **Ex 1**: ... (2 pts)
      - **Prb 7 (2 pts)**: ...
      - SECTION A: COMPUTER ACTIVITY 1 (10 PTS)
      - SECTION B: CHAPTER 5 EXERCISES (8 PTS TOTAL / 2 PTS EACH)
    """
    questions: list[RubricQuestion] = []

    current_chapter = 0
    current_section_label = ""
    per_question_default_pts = 0

    # "CHAPTER 5 EXERCISES" standalone or inside section header
    chapter_header_re = re.compile(
        r"CHAPTER\s+(\d+)\s+(EXERCISES?|PROBLEMS?)",
        re.IGNORECASE,
    )
    # "SECTION A: ... (10 PTS)" — may also include "N PTS EACH"
    section_header_re = re.compile(
        r"SECTION\s+([A-Z]):\s*(.+?)\s*\((\d+)\s*(?:PTS?|POINTS?)",
        re.IGNORECASE,
    )
    # "N PTS EACH" or "N POINTS EACH" in section header
    each_pts_re = re.compile(
        r"(\d+)\s*(?:PTS?|POINTS?)\s+EACH",
        re.IGNORECASE,
    )
    # Explicit per-question points: "Ex 1 (2 pts)", "Prb 7 (2 pts)"
    question_re = re.compile(
        r"\b(Ex|Prob|Prb)\s+(\d+)\s*\((\d+)\s*(?:pts?|points?)\)",
        re.IGNORECASE,
    )
    # Inline questions without explicit points: "- **Ex 1**: ..."
    inline_question_re = re.compile(
        r"-\s*\*{0,2}(Ex|Prob|Prb)\s+(\d+)\*{0,2}\s*(?:\((\d+)\s*(?:pts?|points?)\))?",
        re.IGNORECASE,
    )

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        sec_match = section_header_re.search(stripped)
        if sec_match:
            sec_letter = sec_match.group(1).upper()
            sec_name = sec_match.group(2).strip()
            sec_pts = int(sec_match.group(3))
            current_section_label = f"Sec {sec_letter}"

            # Check for embedded "CHAPTER N ..." and "N PTS EACH"
            ch_in_sec = chapter_header_re.search(stripped)
            if ch_in_sec:
                current_chapter = int(ch_in_sec.group(1))

            each_match = each_pts_re.search(stripped)
            if each_match:
                per_question_default_pts = int(each_match.group(1))
            else:
                per_question_default_pts = 0

            # Only add as a standalone question if there's no "EACH"
            # (meaning it's one big block, like SECTION A activity).
            if not each_match and not ch_in_sec:
                qid = f"Sec {sec_letter} {sec_name}"
                questions.append(RubricQuestion(
                    qid=qid,
                    max_points=sec_pts,
                    section_label=current_section_label,
                ))
            continue

        ch_match = chapter_header_re.search(stripped)
        if ch_match and not sec_match:
            current_chapter = int(ch_match.group(1))
            per_question_default_pts = 0
            continue

        # Try explicit per-question points first.
        explicit_found = set()
        for m in question_re.finditer(stripped):
            kind_raw = m.group(1)
            q_num = int(m.group(2))
            pts = int(m.group(3))
            kind = "Ex" if kind_raw.lower() == "ex" else "Prob"
            qid = f"Ch {current_chapter} {kind} {q_num}"
            if not any(q.qid == qid for q in questions):
                questions.append(RubricQuestion(
                    qid=qid,
                    max_points=pts,
                    section_label=current_section_label,
                    chapter=current_chapter,
                    kind=kind,
                    number=q_num,
                ))
            explicit_found.add(q_num)

        # Try inline questions (use per_question_default_pts if no explicit pts).
        if not explicit_found:
            for m in inline_question_re.finditer(stripped):
                kind_raw = m.group(1)
                q_num = int(m.group(2))
                pts = int(m.group(3)) if m.group(3) else per_question_default_pts
                kind = "Ex" if kind_raw.lower() == "ex" else "Prob"
                qid = f"Ch {current_chapter} {kind} {q_num}"
                if not any(q.qid == qid for q in questions) and pts > 0:
                    questions.append(RubricQuestion(
                        qid=qid,
                        max_points=pts,
                        section_label=current_section_label,
                        chapter=current_chapter,
                        kind=kind,
                        number=q_num,
                    ))

    total = sum(q.max_points for q in questions)
    return ParsedRubric(questions=questions, total_points=total)


def _extract_max_points(text: str) -> int | None:
    patterns = [
        r"total\s+(\d+)\s*points",
        r"scale\s*:\s*total\s+(\d+)",
        r"out\s+of\s+(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_comment_bounds(text: str) -> tuple[int, int] | None:
    patterns = [
        r"exactly\s+(\d+)\s*(?:to|-)\s*(\d+)\s*words?",
        r"(\d+)\s*(?:to|-)\s*(\d+)\s*words?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            lo = int(match.group(1))
            hi = int(match.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return lo, hi
    return None


def _extract_comment_style(text: str) -> str | None:
    style_patterns = [
        r"style\s*:\s*([^\n]+)",
        r"comment\s+style\s*:\s*([^\n]+)",
    ]
    for pattern in style_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("`").strip()
    return None


def parse_prompt_context_text(text: str) -> dict:
    parsed = dict(DEFAULT_PROMPT_CONFIG)
    raw = text or ""

    max_points = _extract_max_points(raw)
    if max_points:
        parsed["max_points"] = max_points

    bounds = _extract_comment_bounds(raw)
    if bounds:
        parsed["comment_min_words"], parsed["comment_max_words"] = bounds

    style = _extract_comment_style(raw)
    if style:
        parsed["comment_style"] = style

    rubric = parse_rubric_questions(raw)
    if rubric.questions:
        parsed["rubric"] = rubric

    return parsed


def parse_prompt_context_file(path: str | Path) -> dict:
    context_path = Path(path)
    text = context_path.read_text(encoding="utf-8")
    return parse_prompt_context_text(text)
