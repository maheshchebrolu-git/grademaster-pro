"""
Tracks cross-section pipeline progress on disk so we can notify once when
all configured sections have completed sync (ready for Canvas delivery).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.utils.config_loader import load_grading_config
from src.utils.notify import notify


def _state_path(assignment: str) -> Path:
    return Path(os.path.expanduser(f"~/documents/grading/{assignment}/batch_files/pipeline_state.json"))


def mark_section_sync_completed(assignment: str, section: str) -> None:
    """
    Call after a successful sync_batch_to_db for this section.
    When every section in config.json has synced at least once, sends one
    Pushover notification (until pipeline_state.json is deleted or edited).
    """
    assignment = assignment.lower()
    section = section.lower()
    config = load_grading_config(assignment)
    expected = {s.lower() for s in config.get("sections", [])}

    path = _state_path(assignment)
    path.parent.mkdir(parents=True, exist_ok=True)

    state: dict = {}
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    synced = set(state.get("synced_sections", []))
    synced.add(section)
    state["synced_sections"] = sorted(synced)

    if synced >= expected and expected and not state.get("delivery_push_sent"):
        sections_label = ", ".join(sorted(expected)).upper()
        notify(
            title="GradeMaster — Ready for delivery",
            message=(
                f"All {len(expected)} section(s) synced to Supabase for {assignment}: {sections_label}. "
                f"Run --phase deliver for each section when you're ready."
            ),
            priority=0,
        )
        state["delivery_push_sent"] = True

    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
