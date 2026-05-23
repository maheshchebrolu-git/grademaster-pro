"""
check_costs.py — GradeMaster-Pro Cost Tracker
Usage: python check_costs.py

Opens the exact Google Cloud billing pages for GCS and Gemini in your browser,
showing real-time charges. Also prints metadata we can pull via API (bucket size,
session counts, active files).

Real billing data lives at:
  GCP Console: https://console.cloud.google.com/billing
  AI Studio:   https://aistudio.google.com/app/billing
"""

import glob
import json
import os
import webbrowser
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()

console = Console()

# ---------------------------------------------------------------------------
# Known project and billing constants (confirmed via API)
# ---------------------------------------------------------------------------
BILLING_ACCOUNT   = "01A2F7-1C069D-76D77D"
GCS_PROJECT_ID    = "sonic-terminal-477719-c9"       # "My Project 16731" in .env
GEMINI_PROJECTS   = [                                 # auto-created by AI Studio
    "gen-lang-client-0074670739",   # GTA-Agent
    "gen-lang-client-0348620407",   # ML-Final
    "gen-lang-client-0887716965",   # TA-Agent
]

# ---------------------------------------------------------------------------
# Console URL builders (open directly to the right filtered view)
# ---------------------------------------------------------------------------

def _billing_url_for_project(project_id: str) -> str:
    return (
        f"https://console.cloud.google.com/billing/{BILLING_ACCOUNT}"
        f"/reports;projects={project_id}"
        f"?authuser=chmahi14@gmail.com"
    )


def _billing_overview_url() -> str:
    """Top-level billing account overview — all projects, current month."""
    month = datetime.now().strftime("%Y-%m")
    return (
        f"https://console.cloud.google.com/billing/{BILLING_ACCOUNT}"
        f"/reports?authuser=chmahi14@gmail.com"
        f"&dateRange=CURRENT_MONTH"
    )


def _aistudio_billing_url() -> str:
    return "https://aistudio.google.com/app/billing"


# ---------------------------------------------------------------------------
# GCS metadata (exact via API)
# ---------------------------------------------------------------------------

def _gcs_bucket_stats(bucket_name: str) -> dict:
    try:
        from google.cloud import storage
        client = storage.Client()
        total_bytes = 0
        total_objects = 0
        for blob in client.list_blobs(bucket_name):
            total_bytes += blob.size or 0
            total_objects += 1
        return {"bytes": total_bytes, "objects": total_objects, "error": None}
    except Exception as exc:
        return {"bytes": 0, "objects": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Gemini metadata (from local session files + Files API)
# ---------------------------------------------------------------------------

def _load_all_session_files() -> list[dict]:
    pattern = os.path.expanduser("~/documents/grading/**/batch_files/session_*.json")
    sessions = []
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["_source_file"] = path
                sessions.append(data)
        except Exception:
            pass
    return sessions


def _gemini_active_files(api_key: str) -> int:
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        return sum(1 for _ in client.files.list())
    except Exception:
        return -1


def _build_session_rows(sessions: list[dict]) -> list[dict]:
    rows = []
    for s in sessions:
        path = Path(s.get("_source_file", ""))
        assignment = "unknown"
        for i, part in enumerate(path.parts):
            if part == "grading" and i + 1 < len(path.parts):
                assignment = path.parts[i + 1]
                break
        rows.append({
            "assignment": assignment,
            "section":    s.get("section", "?"),
            "status":     s.get("status", "?"),
            "job_id":     (s.get("batch_job_id") or "—")[:32],
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    bucket_name    = os.getenv("GCP_BUCKET_NAME", "")
    gemini_api_key = os.getenv("GEMINI_API_KEY", "")

    console.print(Panel("[bold white]💰 GradeMaster-Pro Cost Tracker[/bold white]", expand=False))

    # ── GCS metadata ──────────────────────────────────────────────────────
    console.print("\n[bold cyan]Google Cloud Storage — Bucket Metadata[/bold cyan]")
    if bucket_name:
        stats = _gcs_bucket_stats(bucket_name)
        if stats["error"]:
            console.print(f"  [red]⚠ Could not read bucket: {stats['error']}[/red]")
        else:
            t = Table(show_header=False)
            t.add_column("Metric", style="dim")
            t.add_column("Value", justify="right")
            t.add_row("Bucket",   bucket_name)
            t.add_row("Project",  GCS_PROJECT_ID)
            t.add_row("Objects",  str(stats["objects"]))
            t.add_row("Size",     f"{stats['bytes'] / (1024**3):.4f} GB")
            console.print(t)
    else:
        console.print("  [yellow]GCP_BUCKET_NAME not set in .env[/yellow]")

    # ── Gemini metadata ───────────────────────────────────────────────────
    console.print("\n[bold cyan]Gemini API — Session Metadata[/bold cyan]")
    sessions = _load_all_session_files()
    active_files = _gemini_active_files(gemini_api_key) if gemini_api_key else -1

    total_jobs     = len([s for s in sessions if s.get("batch_job_id")])
    total_students = 0
    for s in sessions:
        prepared = s.get("prepared_batch_path", "")
        if prepared and os.path.exists(prepared):
            with open(prepared, "r", encoding="utf-8") as f:
                total_students += sum(1 for line in f if line.strip())

    t2 = Table(show_header=False)
    t2.add_column("Metric", style="dim")
    t2.add_column("Value", justify="right")
    t2.add_row("Billing account",    BILLING_ACCOUNT)
    t2.add_row("AI Studio projects", ", ".join(GEMINI_PROJECTS))
    t2.add_row("Batch jobs run",     str(total_jobs))
    t2.add_row("Students graded",    str(total_students))
    t2.add_row("Files in AI Studio", str(active_files) if active_files >= 0 else "unavailable")
    console.print(t2)

    if sessions:
        console.print("\n[bold cyan]Session History[/bold cyan]")
        st = Table(show_header=True, header_style="bold")
        st.add_column("Assignment")
        st.add_column("Section")
        st.add_column("Status")
        st.add_column("Job ID", style="dim")
        for row in _build_session_rows(sessions):
            st.add_row(row["assignment"], row["section"], row["status"], row["job_id"])
        console.print(st)

    # ── Open real billing pages ───────────────────────────────────────────
    console.print("\n[bold yellow]📂 Opening real billing pages in your browser...[/bold yellow]")

    overview_url    = _billing_overview_url()
    aistudio_url    = _aistudio_billing_url()

    webbrowser.open(overview_url)
    webbrowser.open(aistudio_url)

    console.print(Panel(
        "[bold]Two tabs opened in your browser:[/bold]\n\n"
        f"[green]1. GCP Billing (all projects, current month)[/green]\n"
        f"   [dim]{overview_url}[/dim]\n\n"
        f"[green]2. Google AI Studio — Billing[/green]\n"
        f"   [dim]{aistudio_url}[/dim]\n\n"
        "[dim]GCP tab: set 'Group by' → Service to see GCS vs Gemini API separately.\n"
        "AI Studio tab: shows exact Gemini API token usage and charges.[/dim]",
        title="💵 Real Charges",
        expand=False,
    ))


if __name__ == "__main__":
    main()
