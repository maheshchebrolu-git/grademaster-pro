import os
from supabase import create_client
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from dotenv import load_dotenv

load_dotenv()
console = Console()


def _get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def get_section_stats(section_prefix, assignment_id=None):
    # Fetch counts directly from Supabase
    supabase = _get_supabase()
    query = supabase.table("grading_audit").select("status").eq("section", section_prefix)
    if assignment_id:
        query = query.eq("assignment_id", assignment_id)
    res = query.execute()
    data = res.data

    total = len(data)
    completed = len([r for r in data if r["status"] == "completed"])
    pending = total - completed
    percent = (completed / total * 100) if total > 0 else 0

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "percent": f"{percent:.1f}%",
    }


def run_status_report(assignment_id=None):
    sections = ["dl2", "dl3", "dl4"]

    table = Table(title="📊 IT 342 Grading Status", header_style="bold magenta")
    table.add_column("Section", justify="center")
    table.add_column("Completed", justify="right", style="green")
    table.add_column("Pending", justify="right", style="yellow")
    table.add_column("Total", justify="right")
    table.add_column("Progress", justify="right", style="cyan")

    for sec in sections:
        stats = get_section_stats(sec, assignment_id=assignment_id)
        table.add_row(
            sec.upper(),
            str(stats["completed"]),
            str(stats["pending"]),
            str(stats["total"]),
            stats["percent"],
        )

    console.print(Panel("[bold white]GTA Dashboard v1.0[/bold white]", expand=False))
    console.print(table)


if __name__ == "__main__":
    run_status_report()
