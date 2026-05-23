import os
import re
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, field_validator

from .state import AgentState

REFINER_MODEL = os.getenv("REFINER_MODEL", "gemini-2.5-flash")


class GradeOutput(BaseModel):
    score: int = Field(description="The numeric grade for this assignment")
    comment: str = Field(description="Issues-only feedback comment; empty string when no issues")

    @field_validator("comment")
    @classmethod
    def check_word_count(cls, value: str) -> str:
        return value


def refiner_node(state: AgentState):
    llm = ChatGoogleGenerativeAI(model=REFINER_MODEL, temperature=0)
    max_points = int(state.get("max_points", 100))

    structured_llm = llm.with_structured_output(GradeOutput)

    prompt = ChatPromptTemplate.from_template("""
        You are an expert TA. Review this raw grading analysis: {text}
        Extract the score and produce issues-only feedback.
        Feedback rules:
        - Include ONLY missing or incorrect items.
        - Format each issue as: "Ch <n> <Ex|Prob> <n>: issue".
        - If everything relevant is correct, return an empty string for comment.
        - Do not include praise, encouragement, or summary text.
        Score must be an integer between 0 and {max_points}.
        Previous Errors: {errors}
    """)

    chain = prompt | structured_llm
    result = chain.invoke(
        {
            "text": state['raw_ai_text'],
            "errors": state['errors'],
            "max_points": max_points,
        }
    )

    return {
        "extracted_score": max(0, min(max_points, int(result.score))),
        "short_comment": result.comment,
        "attempts": state['attempts'] + 1
    }


def _human_interrupt_node(state: AgentState):
    section_count = state.get("section_refined_count", 0)
    needs_pause = section_count == 2 and not state.get("hitl_approved", False)

    if not needs_pause:
        return {"is_valid": True}

    print("⏸️ Check the first two grades in Supabase. Correct? [Y/n]")
    choice = input("> ").strip().lower()
    if choice in ("", "y", "yes"):
        return {"hitl_approved": True, "is_valid": True}

    return {
        "is_valid": False,
        "errors": ["HITL rejected first-two grades; adjust prompt/state then resume."],
    }


def build_refiner_graph():
    graph = StateGraph(AgentState)
    graph.add_node("refine", refiner_node)
    graph.add_node("human_interrupt", _human_interrupt_node)
    graph.add_edge(START, "refine")
    graph.add_edge("refine", "human_interrupt")
    graph.add_edge("human_interrupt", END)

    return graph.compile()


app = build_refiner_graph()


# ---------------------------------------------------------------------------
# Structured per-question validation (new pipeline)
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"correct", "partial", "missing", "incorrect"}

# Soft target from assignment config (comment_max_words). Hard cap prevents runaway text in Canvas.
_ISSUE_HARD_WORD_CAP = int(os.getenv("GRADEMASTER_ISSUE_HARD_WORD_CAP", "28"))


def _sanitize_text(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = cleaned.replace("**", " ").replace("__", " ").replace("`", " ")
    return " ".join(cleaned.split())


def _finalize_issue_text(issue: str, max_words: int) -> str:
    """
    Prefer complete sentences; avoid cutting mid-thought at an arbitrary word index.

    1. If total words <= max_words, keep as-is.
    2. Else greedily include full sentences (split on . ! ?) while word count <= max_words.
    3. If the first sentence alone exceeds max_words but is <= hard cap, keep the full first sentence.
    4. Last resort: trim to hard cap at word boundary and append an ellipsis.
    """
    cleaned = _sanitize_text(issue)
    if not cleaned:
        return ""

    words = cleaned.split()
    n = len(words)
    soft = max(1, int(max_words))

    if n <= soft:
        return cleaned

    # Greedy full sentences within soft word budget
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    built: list[str] = []
    total = 0
    for part in parts:
        p = part.strip()
        if not p:
            continue
        wc = len(p.split())
        if total + wc <= soft:
            built.append(p)
            total += wc
        else:
            break
    if built:
        return " ".join(built).strip()

    # Single over-long sentence (or no sentence boundary): keep one sentence up to hard cap
    first = parts[0].strip() if parts else cleaned
    fw = first.split()
    if len(fw) <= _ISSUE_HARD_WORD_CAP:
        return first
    return " ".join(fw[:_ISSUE_HARD_WORD_CAP]).rstrip(",;:") + "…"


def _is_positive_only(text: str) -> bool:
    t = (text or "").lower()
    positive = ["excellent", "great job", "well done", "good work", "nice work", "keep up"]
    negative = ["missing", "wrong", "incorrect", "error", "miscalculated", "incomplete", "not answered", "did not"]
    return any(tok in t for tok in positive) and not any(tok in t for tok in negative)


def validate_and_refine_structured(
    questions: list[dict],
    comment_min_words: int = 6,
    comment_max_words: int = 8,
) -> list[dict]:
    """
    Validate and clean each question in a per-question grading breakdown.
    Pure deterministic — no LLM calls.
    """
    refined = []
    for q in questions:
        rq = dict(q)

        rq["id"] = _sanitize_text(rq.get("id", ""))
        rq["max_points"] = int(rq.get("max_points", 0))
        awarded = int(rq.get("awarded", 0))
        rq["awarded"] = max(0, min(rq["max_points"], awarded))

        status = (rq.get("status") or "").lower().strip()
        if status not in _VALID_STATUSES:
            status = "incorrect" if rq["awarded"] < rq["max_points"] else "correct"
        rq["status"] = status

        issue = _sanitize_text(rq.get("issue", ""))

        if status == "correct":
            rq["issue"] = ""
            rq["awarded"] = rq["max_points"]
        elif _is_positive_only(issue):
            if rq["awarded"] < rq["max_points"]:
                rq["issue"] = "Missing required details for this question."
            else:
                rq["status"] = "correct"
                rq["issue"] = ""
        elif not issue and rq["awarded"] < rq["max_points"]:
            if status == "missing":
                rq["issue"] = "Question not answered in submission."
            else:
                rq["issue"] = "Missing required details for this question."
        else:
            rq["issue"] = _finalize_issue_text(issue, comment_max_words)

        refined.append(rq)
    return refined


# ---------------------------------------------------------------------------
# Legacy flat-output validation (backward-compatible)
# ---------------------------------------------------------------------------

_ISSUE_LINE_RE = re.compile(
    r"^ch\s+(\d+)\s+(ex|prob)\s+(\d+):\s+(.+)$",
    flags=re.IGNORECASE,
)


def _normalize_issue_feedback(feedback: str) -> str:
    cleaned = _sanitize_text(feedback)
    if not cleaned or _is_positive_only(cleaned):
        return ""

    chunks = [c.strip(" -•") for c in re.split(r"[|\n;]+", cleaned) if c.strip(" -•")]
    normalized = []
    for chunk in chunks:
        c = _sanitize_text(chunk)
        if not c or _is_positive_only(c):
            continue
        normalized.append(c)
    return " | ".join(normalized)


def _enforce_issue_format(feedback: str, constraints: dict) -> str:
    min_words = int(constraints.get("min_words", 6))
    max_words = int(constraints.get("max_words", 8))
    cleaned = _normalize_issue_feedback(feedback)
    if not cleaned:
        return ""

    lines = [p.strip() for p in re.split(r"\s*\|\s*|\n+", cleaned) if p.strip()]
    fixed = []
    for line in lines:
        if _is_positive_only(line):
            continue
        line = re.sub(r"^general,\s*q:\s*", "", line, flags=re.IGNORECASE).strip()
        line = re.sub(r"^chapter\s+", "Ch ", line, flags=re.IGNORECASE)
        line = re.sub(r"\bproblem\b", "Prob", line, flags=re.IGNORECASE)

        m = _ISSUE_LINE_RE.match(line)
        if m:
            ch = int(m.group(1))
            kind = "Ex" if m.group(2).lower() == "ex" else "Prob"
            qn = int(m.group(3))
            issue_text = _finalize_issue_text(m.group(4), max_words)
            if ch > 0 and qn > 0:
                fixed.append(f"Ch {ch} {kind} {qn}: {issue_text}")
                continue

        # Best-effort: normalize line length without mid-sentence truncation where possible.
        fixed.append(_finalize_issue_text(line, max_words))

    return "\n".join(dict.fromkeys(fixed))


def validate_and_refine(raw_response: dict, constraints: dict) -> dict:
    """
    Guardrail for flat {score, comment/feedback} outputs.
    Kept for backward compatibility with legacy batch results.
    """
    feedback_key = "feedback" if "feedback" in raw_response else "comment"
    original_feedback = str(raw_response.get(feedback_key, "")).strip()
    issue_only = bool(constraints.get("issue_only", False))

    if issue_only:
        try:
            enforced = _enforce_issue_format(original_feedback, constraints)
        except Exception:
            enforced = ""
        raw_response[feedback_key] = enforced
        raw_response["guardrail_triggered"] = _sanitize_text(original_feedback) != enforced
        return raw_response

    feedback = _normalize_issue_feedback(original_feedback)
    max_words = constraints.get("max_words", 8)

    finalized = _finalize_issue_text(feedback, max_words)
    if finalized == feedback:
        raw_response[feedback_key] = feedback
        raw_response["guardrail_triggered"] = False
        return raw_response

    raw_response[feedback_key] = finalized or feedback
    raw_response["guardrail_triggered"] = True
    return raw_response
