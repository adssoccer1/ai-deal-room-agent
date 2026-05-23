"""CLI entrypoint for the AI Deal Room Agent.

Commands:
  python main.py demo
  python main.py run --company "Tesla" --website https://www.tesla.com \
      --docs sample_docs/tesla_deal_notes.txt
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent import run_agent

load_dotenv()

app = typer.Typer(add_completion=False, help="AI Deal Room Agent — CLI prototype.")
console = Console()


PROJECT_ROOT = Path(__file__).parent
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


def _check_keys(require_parallel: bool) -> None:
    missing: list[str] = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if require_parallel and not os.environ.get("PARALLEL_API_KEY"):
        missing.append("PARALLEL_API_KEY")
    if missing:
        console.print(
            Panel.fit(
                f"[red]Missing required environment variable(s): "
                f"{', '.join(missing)}[/red]\n"
                "Copy .env.example to .env and add your keys.",
                title="Configuration error",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)


def _print_summary(update, run_dir: Path) -> None:
    cp = update.company_profile

    console.print(Panel.fit(
        f"[bold]{cp.company_name}[/bold]  ([dim]{update.run_id}[/dim])",
        title="AI Deal Room Run",
        border_style="cyan",
    ))

    # Documents
    if update.document_classifications:
        t = Table(title="Documents Processed", show_lines=False)
        t.add_column("Name")
        t.add_column("Type")
        t.add_column("Confidence", justify="right")
        for dc in update.document_classifications:
            t.add_row(dc.document_name, dc.document_type, f"{dc.confidence:.2f}")
        console.print(t)

    # Metrics
    if update.metric_observations:
        t = Table(title=f"Metric Observations ({len(update.metric_observations)})")
        t.add_column("Metric")
        t.add_column("Value")
        t.add_column("Period")
        t.add_column("Source")
        t.add_column("Type")
        t.add_column("Status")
        t.add_column("Conf.", justify="right")
        for o in update.metric_observations:
            src = (o.source.document_name or o.source.source_name
                   or o.source.source_type)
            status_color = {
                "proposed": "green",
                "needs_review": "yellow",
                "rejected": "red",
                "accepted_candidate": "cyan",
            }.get(o.review_status, "white")
            t.add_row(
                o.metric_name, o.value, o.period or "—", src or "—",
                o.observation_type,
                f"[{status_color}]{o.review_status}[/{status_color}]",
                f"{o.confidence:.2f}",
            )
        console.print(t)

    # Computed
    if update.computed_metrics:
        t = Table(title=f"Computed Metrics ({len(update.computed_metrics)})")
        t.add_column("Metric")
        t.add_column("Value")
        t.add_column("Formula")
        t.add_column("Conf.", justify="right")
        for cm in update.computed_metrics:
            t.add_row(cm.metric_name, cm.value, cm.formula, f"{cm.confidence:.2f}")
        console.print(t)
    else:
        console.print("[yellow]No derived metrics computed.[/yellow]")

    # Review tasks
    if update.review_tasks:
        t = Table(title=f"Review Tasks ({len(update.review_tasks)})", show_lines=True)
        t.add_column("Severity")
        t.add_column("Type")
        t.add_column("Title")
        for rt in update.review_tasks:
            color = {"high": "red", "medium": "yellow", "low": "white"}.get(
                rt.severity, "white"
            )
            t.add_row(f"[{color}]{rt.severity}[/{color}]", rt.task_type, rt.title)
        console.print(t)
    else:
        console.print("[green]No review tasks created.[/green]")

    # Outputs
    console.print(Panel.fit(
        f"[bold]Outputs[/bold]\n"
        f"  • {run_dir / 'deal_profile_update.json'}\n"
        f"  • {run_dir / 'deal_summary.md'}\n"
        f"  • {run_dir / 'agent_trace.json'}\n"
        f"[bold]SQLite:[/bold] run {update.run_id} persisted.",
        title="Saved",
        border_style="green",
    ))
    console.print(f"\n[dim]{update.summary}[/dim]\n")


@app.command()
def run(
    company: str = typer.Option(..., "--company", help="Company name."),
    website: Optional[str] = typer.Option(None, "--website", help="Company website."),
    docs: List[str] = typer.Option(
        [], "--docs", help="Paths to deal documents (.txt/.md).",
    ),
    no_research: bool = typer.Option(
        False, "--no-research",
        help="Skip Parallel web research (useful when offline).",
    ),
):
    """Run the agent on an arbitrary company."""
    _check_keys(require_parallel=not no_research)

    for p in docs:
        if not Path(p).exists():
            console.print(f"[red]Document not found:[/red] {p}")
            raise typer.Exit(code=1)

    console.print(f"[cyan]Running agent for {company}...[/cyan]")
    update, run_dir = run_agent(
        company_name=company,
        website=website,
        doc_paths=list(docs),
        do_research=not no_research,
    )
    _print_summary(update, run_dir)


@app.command()
def demo(
    no_research: bool = typer.Option(
        False, "--no-research",
        help="Skip Parallel web research (useful when offline).",
    ),
):
    """Run the Acme Robotics demo with the three bundled documents."""
    _check_keys(require_parallel=not no_research)

    doc_paths = [
        str(SAMPLE_DOCS / "acme_pitch_deck.txt"),
        str(SAMPLE_DOCS / "acme_term_sheet.txt"),
        str(SAMPLE_DOCS / "acme_financials.txt"),
    ]
    missing = [p for p in doc_paths if not Path(p).exists()]
    if missing:
        console.print(f"[red]Missing sample docs:[/red] {missing}")
        raise typer.Exit(code=1)

    console.print("[cyan]Running Acme Robotics demo...[/cyan]")
    update, run_dir = run_agent(
        company_name="Acme Robotics",
        website="https://acmerobotics.example",
        doc_paths=doc_paths,
        do_research=not no_research,
    )
    _print_summary(update, run_dir)


if __name__ == "__main__":
    app()
