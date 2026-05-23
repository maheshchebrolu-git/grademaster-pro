from typing import Annotated, List, TypedDict
import operator


class AgentState(TypedDict):
    """
    My internal state machine. I'm using 'Annotated' so that
    the 'errors' list appends instead of overwriting.
    """
    current_section: str               # 'dl2', 'dl3', or 'dl4'
    local_batch_path: str              # Where the .jsonl lives on my Mac
    gcs_uri: str                       # The temporary Google link
    batch_job_id: str                  # The ID to check status later
    is_wiped: bool                     # Safety flag to ensure I deleted the GCS file
    errors: Annotated[List[str], operator.add]
  