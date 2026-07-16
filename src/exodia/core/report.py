"""Report rendering — turn Results into human tables or machine JSON."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .result import Result, Status

_STYLE = {
    Status.PASS: "green",
    Status.WARN: "yellow",
    Status.FAIL: "red",
    Status.SKIP: "dim",
    Status.ERROR: "bold red",
}
_ICON = {
    Status.PASS: "✅",
    Status.WARN: "⚠️ ",
    Status.FAIL: "❌",
    Status.SKIP: "⏭️ ",
    Status.ERROR: "💥",
}


def render_table(results: list[Result], title: str, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title=title, expand=True)
    table.add_column("", width=3)
    table.add_column("Check / Phase", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Summary")
    for r in results:
        table.add_row(
            _ICON.get(r.status, "?"),
            r.name,
            f"[{_STYLE[r.status]}]{r.status.value.upper()}[/]",
            r.summary,
        )
    console.print(table)

    # Remediation panels for anything that failed and carries KB guidance.
    for r in results:
        if r.status.is_blocking and (r.cause or r.fix):
            body = ""
            if r.cause:
                body += f"[bold]Cause:[/] {r.cause}\n"
            if r.fix:
                body += "[bold]Fix:[/]\n" + "\n".join(
                    f"  {i + 1}. {s}" for i, s in enumerate(r.fix)
                )
            if r.sap_note:
                body += f"\n[bold]SAP Note:[/] {r.sap_note}"
            console.print(Panel(body, title=f"🔧 {r.name}", border_style="red"))


def render_json(results: list[Result]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str)


def worst_status(results: list[Result]) -> Status:
    order = [Status.PASS, Status.SKIP, Status.WARN, Status.FAIL, Status.ERROR]
    worst = Status.PASS
    for r in results:
        if order.index(r.status) > order.index(worst):
            worst = r.status
    return worst


def exit_code(results: list[Result]) -> int:
    """0 if nothing blocking, 1 otherwise — for CI/automation."""
    return 1 if any(r.status.is_blocking for r in results) else 0
