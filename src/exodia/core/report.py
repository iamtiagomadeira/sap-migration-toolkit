"""Report rendering — turn Results into human tables or machine JSON."""

from __future__ import annotations

import json
import os

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .result import Result, Status, format_duration

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
# ASCII fallbacks for CI / enterprise terminals without UTF-8 (--no-emoji, TIA-76).
_ASCII = {
    Status.PASS: "[PASS]",
    Status.WARN: "[WARN]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
    Status.ERROR: "[ERR ]",
}


def _no_color() -> bool:
    """Honour the NO_COLOR convention (https://no-color.org)."""
    return bool(os.environ.get("NO_COLOR"))


def _glyph(status: Status, no_emoji: bool) -> str:
    if no_emoji:
        return _ASCII.get(status, f"[{status.value.upper()}]")
    return _ICON.get(status, "?")


def render_table(
    results: list[Result],
    title: str,
    console: Console | None = None,
    *,
    no_emoji: bool = False,
) -> None:
    no_color = _no_color()
    no_emoji = no_emoji or no_color
    console = console or Console(no_color=no_color)
    table = Table(title=title, expand=True)
    table.add_column("", width=6 if no_emoji else 3)
    table.add_column("Check / Phase", style="" if no_color else "cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Duration", justify="right")
    table.add_column("Summary")
    for r in results:
        status_cell = (
            r.status.value.upper()
            if no_color
            else f"[{_STYLE[r.status]}]{r.status.value.upper()}[/]"
        )
        table.add_row(
            _glyph(r.status, no_emoji),
            r.name,
            status_cell,
            r.duration_str,
            r.summary,
        )
    console.print(table)

    # Verdict footer: per-status counts + total duration (TIA-75).
    total = _total_duration(results)
    footer = verdict_line(results, no_emoji=no_emoji)
    if total is not None:
        footer += f"\n[dim]Total duration: {format_duration(total)}[/]"
    console.print(footer)

    # Remediation panels for anything that failed and carries KB guidance.
    for r in results:
        if r.status.is_blocking and (r.cause or r.fix):
            body = ""
            if r.cause:
                body += f"[bold]Cause:[/] {r.cause}\n"
            if r.fix:
                body += "[bold]Fix:[/]\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(r.fix))
            if r.sap_note:
                body += f"\n[bold]SAP Note:[/] {r.sap_note}"
            console.print(Panel(body, title=f"🔧 {r.name}", border_style="red"))


def _total_duration(results: list[Result]) -> float | None:
    """Sum of per-result durations, or None when nothing was timed."""
    vals = [r.duration_seconds for r in results if r.duration_seconds is not None]
    return sum(vals) if vals else None


def render_json(results: list[Result]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str)


def worst_status(results: list[Result]) -> Status:
    order = [Status.PASS, Status.SKIP, Status.WARN, Status.FAIL, Status.ERROR]
    worst = Status.PASS
    for r in results:
        if order.index(r.status) > order.index(worst):
            worst = r.status
    return worst


def tally(results: list[Result]) -> dict[Status, int]:
    """Count results by status."""
    counts: dict[Status, int] = dict.fromkeys(Status, 0)
    for r in results:
        counts[r.status] += 1
    return counts


def verdict_line(results: list[Result], *, no_emoji: bool = False) -> str:
    """A one-line summary + go/no-go verdict for a batch of checks.

    Returns Rich markup: a per-status count line followed by a clear verdict —
    green when nothing blocks, yellow when only warnings remain, red when a
    blocking result means the operator must NOT proceed yet. Under ``no_emoji``
    (or NO_COLOR) the glyphs collapse to ASCII tags.
    """
    if not results:
        return "[dim]No checks ran.[/]"
    no_emoji = no_emoji or _no_color()
    c = tally(results)
    parts = [
        f"[green]{c[Status.PASS]} passed[/]",
        f"[yellow]{c[Status.WARN]} warnings[/]",
        f"[red]{c[Status.FAIL] + c[Status.ERROR]} failed[/]",
        f"[dim]{c[Status.SKIP]} skipped[/]",
    ]
    counts_line = "  ·  ".join(parts)
    blocking = c[Status.FAIL] + c[Status.ERROR]
    if blocking:
        marker = "[FAIL]" if no_emoji else "⛔"
        verdict = (
            f"[bold red]{marker} NOT ready — resolve {blocking} blocking "
            f"result{'s' if blocking != 1 else ''} before proceeding.[/]"
        )
    elif c[Status.WARN]:
        marker = "[WARN]" if no_emoji else "⚠️ "
        verdict = (
            f"[bold yellow]{marker} Ready with caveats — review the warnings, "
            "then you may proceed.[/]"
        )
    else:
        marker = "[PASS]" if no_emoji else "✅"
        verdict = f"[bold green]{marker} Ready to proceed — all checks passed.[/]"
    return f"{counts_line}\n{verdict}"


def exit_code(results: list[Result]) -> int:
    """0 if nothing blocking, 1 otherwise — for CI/automation."""
    return 1 if any(r.status.is_blocking for r in results) else 0
