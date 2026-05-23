from typing import TypedDict, List, Annotated, NotRequired
import operator


class AgentState(TypedDict):
    # This is the 'Shared Memory' of our agent
    raw_ai_text: str             # The messy output from the Batch API
    extracted_score: int         # The final number we want
    short_comment: str           # The 6-7 word feedback
    is_valid: bool               # Did it pass the Guardrail?
    attempts: int                # How many times have we tried to fix it?
    errors: Annotated[List[str], operator.add] # Keeps a log of what went wrong
    section_refined_count: NotRequired[int]
    hitl_approved: NotRequired[bool]
    max_points: NotRequired[int]
    comment_min_words: NotRequired[int]
    comment_max_words: NotRequired[int]
    comment_style: NotRequired[str]
