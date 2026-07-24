"""Exception report — the exportable advisory artifact that circulates with the customer.

The real Cutover Plan manages advisories as **documented exceptions**: findings
that don't fail the copy but that the customer must acknowledge (clean vs ignore)
and sign off. This module renders that artifact — both to the terminal (clean,
padded, legible) and to a portable Markdown document for download / email.

Structure (mirrors ``COP_model.md`` §6):
  1. Header — system, method, phase, window.
  2. Gate summary — each gate's GO / NO-GO / PENDING with criteria status.
  3. Per-phase table — check | side | status | role | owner | note.
  4. Exceptions — every advisory + every override (the audit trail).
  5. Blocking-open — any 🔴 still open (empty to pass a gate).

Nothing is ever silently ignored: every deviation leaves a trail. The override
log doubles as the handover exception template the operator produces anyway.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table

from .gate import GateVerdict, Override
from .result import Result, Status
from .severity import Severity


def _no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


_SEV_ASCII = {
    Severity.BLOCKING: "[BLOCK]",
    Severity.ADVISORY: "[ADVIS]",
    Severity.INFO: "[INFO ]",
}
_STATUS_ASCII = {
    Status.PASS: "[PASS]",
    Status.WARN: "[WARN]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
    Status.ERROR: "[ERR ]",
}


def _sev_glyph(sev: Severity, no_emoji: bool) -> str:
    return _SEV_ASCII[sev] if no_emoji else sev.icon


class ExceptionReport:
    """An exportable advisory / exception report for one migration run.

    Aggregates per-phase gate verdicts, the graded results, and the recorded
    overrides into a single artifact renderable to terminal or Markdown.
    """

    def __init__(
        self,
        results: list[Result],
        verdicts: list[GateVerdict],
        overrides: list[Override] | None = None,
        *,
        system: str = "",
        method: str = "",
        window: str = "",
    ) -> None:
        self.results = results
        self.verdicts = sorted(verdicts, key=lambda v: v.phase.order)
        self.overrides = list(overrides or [])
        self.system = system
        self.method = method
        self.window = window
        self.generated_at = datetime.now(UTC)

    # -- data helpers ------------------------------------------------------- #

    def _advisories(self) -> list[Result]:
        """Non-pass, non-skip results whose effective role is not blocking-open.

        These are the findings the customer decides clean-vs-ignore: ADVISORY
        failures/warnings. Sorted by phase for a stable report.
        """
        out = [
            r
            for r in self.results
            if r.status in (Status.WARN, Status.FAIL, Status.ERROR)
            and r.severity is not Severity.BLOCKING
        ]
        return sorted(out, key=lambda r: r.phase.order)

    def _blocking_open(self) -> list[str]:
        seen: list[str] = []
        for v in self.verdicts:
            seen.extend(v.blocking_open)
        return seen

    # -- terminal rendering ------------------------------------------------- #

    def render_terminal(self, console: Console | None = None, *, no_emoji: bool = False) -> None:
        no_color = _no_color()
        no_emoji = no_emoji or no_color
        console = console or Console(no_color=no_color)

        # 1. Header
        hdr = "SAP Migration Toolkit — Exception & Advisory Report"
        meta = []
        if self.system:
            meta.append(f"System: {self.system}")
        if self.method:
            meta.append(f"Method: {self.method}")
        if self.window:
            meta.append(f"Window: {self.window}")
        meta.append(f"Generated: {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        console.print(f"[bold]{hdr}[/]" if not no_color else hdr)
        console.print("  ·  ".join(meta))
        console.print("")

        # 2. Gate summary
        gt = Table(title="Gate Summary", expand=True)
        gt.add_column("Phase", no_wrap=True)
        gt.add_column("Decision")
        gt.add_column("Passed", justify="right")
        gt.add_column("Detail")
        for v in self.verdicts:
            icon = v.decision.value.upper().replace("_", " ") if no_emoji else v.decision.icon
            gt.add_row(
                v.phase.label,
                f"{icon} {v.decision.value.upper().replace('_', ' ')}"
                if no_emoji
                else f"{icon} {v.decision.value.upper().replace('_', ' ')}",
                f"{v.passed}/{v.total_graded}",
                v.summary,
            )
        console.print(gt)
        console.print("")

        # 3. Blocking-open (should be empty to pass)
        blocking = self._blocking_open()
        if blocking:
            marker = "[BLOCK]" if no_emoji else "🔴"
            console.print(f"[bold red]{marker} Blocking issues OPEN — gate cannot pass:[/]")
            for name in blocking:
                console.print(f"    • {name}")
            console.print("")

        # 4. Exceptions (advisories) table
        advisories = self._advisories()
        at = Table(title="Advisories — customer decides clean vs ignore", expand=True)
        at.add_column("", width=8 if no_emoji else 3)
        at.add_column("Check", no_wrap=True)
        at.add_column("Phase")
        at.add_column("Side")
        at.add_column("Owner")
        at.add_column("Finding")
        if advisories:
            for r in advisories:
                at.add_row(
                    _sev_glyph(Severity.ADVISORY, no_emoji),
                    f"{r.display_title}\n[dim]{r.name}[/]"
                    if r.display_title != r.name
                    else r.name,
                    r.phase.label,
                    r.side.label if r.side else "—",
                    r.responsible or "—",
                    r.summary,
                )
            console.print(at)
        else:
            console.print("[green]No advisories — nothing for the customer to acknowledge.[/]")
        console.print("")

        # 5. Override audit trail
        if self.overrides:
            ot = Table(title="Override Audit Trail — conscious decisions on file", expand=True)
            ot.add_column("When", no_wrap=True)
            ot.add_column("Who")
            ot.add_column("Check")
            ot.add_column("Reason")
            for o in self.overrides:
                ot.add_row(
                    o.when.strftime("%Y-%m-%d %H:%M UTC"),
                    o.who,
                    o.check,
                    o.reason,
                )
            console.print(ot)

    # -- markdown export ---------------------------------------------------- #

    def to_markdown(self) -> str:
        """Render the report as a portable Markdown document (download / email)."""
        lines: list[str] = []
        lines.append("# SAP Migration Toolkit — Exception & Advisory Report")
        lines.append("")
        meta = []
        if self.system:
            meta.append(f"**System:** {self.system}")
        if self.method:
            meta.append(f"**Method:** {self.method}")
        if self.window:
            meta.append(f"**Window:** {self.window}")
        meta.append(f"**Generated:** {self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        lines.append("  ·  ".join(meta))
        lines.append("")

        # Gate summary
        lines.append("## Gate Summary")
        lines.append("")
        lines.append("| Phase | Decision | Passed | Detail |")
        lines.append("|---|---|---|---|")
        for v in self.verdicts:
            dec = f"{v.decision.icon} {v.decision.value.upper().replace('_', ' ')}"
            lines.append(f"| {v.phase.label} | {dec} | {v.passed}/{v.total_graded} | {v.summary} |")
        lines.append("")

        # Blocking-open
        blocking = self._blocking_open()
        if blocking:
            lines.append("## 🔴 Blocking Issues Open")
            lines.append("")
            lines.append("> These must be resolved or consciously overridden before the gate passes.")
            lines.append("")
            for name in blocking:
                lines.append(f"- `{name}`")
            lines.append("")

        # Advisories
        lines.append("## Advisories — customer decides clean vs ignore")
        lines.append("")
        advisories = self._advisories()
        if advisories:
            lines.append("| Role | Check | Phase | Side | Owner | Finding |")
            lines.append("|---|---|---|---|---|---|")
            for r in advisories:
                side = r.side.label if r.side else "—"
                owner = r.responsible or "—"
                check = (
                    f"{r.display_title} (`{r.name}`)"
                    if r.display_title != r.name
                    else f"`{r.name}`"
                )
                lines.append(
                    f"| {Severity.ADVISORY.icon} | {check} | {r.phase.label} | "
                    f"{side} | {owner} | {r.summary} |"
                )
        else:
            lines.append("_No advisories — nothing for the customer to acknowledge._")
        lines.append("")

        # Override audit trail
        if self.overrides:
            lines.append("## Override Audit Trail")
            lines.append("")
            lines.append("> Conscious decisions to proceed past a blocking finding — on file.")
            lines.append("")
            lines.append("| When | Who | Check | Reason |")
            lines.append("|---|---|---|---|")
            for o in self.overrides:
                lines.append(
                    f"| {o.when.strftime('%Y-%m-%d %H:%M UTC')} | {o.who} | "
                    f"`{o.check}` | {o.reason} |"
                )
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(
            "_Generated by SAP Migration Toolkit. Advisories are system hygiene / "
            "go-live quality findings that do not fail the copy; the customer decides "
            "whether to remediate or accept each one. Every override is recorded above._"
        )
        return "\n".join(lines)

    def write_markdown(self, path: str) -> str:
        """Write the Markdown report to ``path``; return the path."""
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_markdown(), encoding="utf-8")
        return str(p)
