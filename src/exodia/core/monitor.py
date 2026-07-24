"""Live monitor for long-running actions (e.g. SWPM restore).

Long actions — an SWPM/`sapinst` restore can run for hours — need more than a
spinner. This module provides a small monitor abstraction and a Rich-based live
implementation: a header, a progress/phase line, a scrolling tail of the native
log, per-phase results, and an error/handoff banner (SWPM often stops mid-run
and hands off to a browser GUI at a printed URL).

Design goals:

* **Optional.** Rich is a core dependency, so ``RichMonitor`` always works. A
  future ``TextualMonitor`` (extra: ``tui``) can implement the same ``Monitor``
  protocol with panels + key bindings — the call sites never change.
* **Passive.** The monitor observes; it never runs commands. Callers push phase
  changes, log lines and results into it.
* **Null-safe.** ``NullMonitor`` is a no-op used when nothing should render
  (CI, ``--quiet``), so call sites don't need ``if monitor:`` guards.

Upgrade path to Textual (TIA-66):
    A Textual app implements ``Monitor`` with dedicated widgets:
      - progress panel   -> ``phase()`` / ``advance()``
      - log viewer       -> ``log_line()`` (RichLog widget, scrollback)
      - result table     -> ``result()``
      - handoff banner    -> ``handoff()`` (open URL, [o]pen [l]og [s]tatus keys)
    Because callers only touch the protocol, swapping ``RichMonitor`` for the
    Textual app is a one-line change (or an ``--tui`` flag).
"""

from __future__ import annotations

import os
import re
from collections import deque
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, runtime_checkable

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .result import Result, Status, format_duration

_STATUS_STYLE = {
    Status.PASS: "bold green",
    Status.WARN: "bold yellow",
    Status.FAIL: "bold red",
    Status.SKIP: "dim",
    Status.ERROR: "bold red",
}
_STATUS_ICON = {
    Status.PASS: "✅",
    Status.WARN: "⚠️ ",
    Status.FAIL: "❌",
    Status.SKIP: "⏭️ ",
    Status.ERROR: "💥",
}
# ASCII fallbacks for CI / enterprise terminals without UTF-8 (--no-emoji, TIA-76).
_STATUS_ASCII = {
    Status.PASS: "[PASS]",
    Status.WARN: "[WARN]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
    Status.ERROR: "[ERR ]",
}

# Severity highlighting for the native log tail (TIA-74).
_LOG_ERROR_RE = re.compile(r"\b(error|fatal|severe|failed|failure|abend|abort)\b", re.I)
_LOG_WARN_RE = re.compile(r"\b(warn|warning)\b", re.I)


def _no_color_env() -> bool:
    """Honour the NO_COLOR convention (https://no-color.org) — any non-empty value."""
    return bool(os.environ.get("NO_COLOR"))


@runtime_checkable
class Monitor(Protocol):
    """The surface every monitor (Rich now, Textual later) implements."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def phase(self, name: str, detail: str = "") -> None: ...
    def progress(self, percent: float | None, detail: str = "") -> None: ...
    def log_line(self, line: str) -> None: ...
    def result(self, result: Result) -> None: ...
    def handoff(self, message: str, url: str | None = None) -> None: ...
    def __enter__(self) -> Monitor: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class NullMonitor:
    """No-op monitor. Use when nothing should render (CI, --quiet)."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def phase(self, name: str, detail: str = "") -> None:
        pass

    def progress(self, percent: float | None, detail: str = "") -> None:
        pass

    def log_line(self, line: str) -> None:
        pass

    def result(self, result: Result) -> None:
        pass

    def handoff(self, message: str, url: str | None = None) -> None:
        pass

    def __enter__(self) -> NullMonitor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass


class RichMonitor:
    """A live terminal dashboard for a single long-running operation.

    Renders four stacked regions inside one ``rich.live.Live``:
      1. title / operation name
      2. current phase line
      3. a scrolling tail of the native log (last ``log_lines`` lines)
      4. a compact results table
    Plus an optional handoff banner when the operation hands off to a GUI.
    """

    def __init__(
        self,
        title: str,
        *,
        console: Console | None = None,
        log_lines: int = 12,
        refresh_per_second: int = 8,
        no_emoji: bool = False,
    ) -> None:
        self.title = title
        self._no_color = _no_color_env()
        self.console = console or Console(no_color=self._no_color)
        # --no-emoji, or NO_COLOR, drops the status glyphs for plain ASCII tags.
        self._no_emoji = no_emoji or self._no_color
        # Each log entry keeps its arrival time so the tail lines up with the
        # audit clock (TIA-73): (HH:MM:SS, text).
        self._log: deque[tuple[str, str]] = deque(maxlen=max(1, log_lines))
        self._results: list[Result] = []
        self._phase = "starting…"
        self._phase_detail = ""
        self._percent: float | None = None
        self._progress_detail = ""
        self._handoff: tuple[str, str | None] | None = None
        # Wall-clock timing so an auditor can see exactly when the operation
        # started, how long it has been running, and how long each phase took.
        self._started_at = datetime.now(UTC)
        self._phase_started_at = self._started_at
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )

    def _status_glyph(self, status: Status) -> str:
        """Emoji glyph normally; ASCII tag under --no-emoji / NO_COLOR."""
        if self._no_emoji:
            return _STATUS_ASCII.get(status, f"[{status.value.upper()}]")
        return _STATUS_ICON.get(status, "")

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        now = datetime.now(UTC)
        self._started_at = now
        self._phase_started_at = now
        self._live.start()

    def stop(self) -> None:
        self._live.update(self._render())
        self._live.stop()

    def __enter__(self) -> RichMonitor:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -- updates ------------------------------------------------------------ #
    def phase(self, name: str, detail: str = "") -> None:
        self._phase = name
        self._phase_detail = detail
        self._phase_started_at = datetime.now(UTC)
        # A new phase resets the progress bar (percent is phase-scoped).
        self._percent = None
        self._progress_detail = ""
        self._refresh()

    def progress(self, percent: float | None, detail: str = "") -> None:
        """Set the completion percentage of the current phase (0–100).

        Call with the value parsed from the native tool (e.g. SWPM prints
        ``... 42%`` during recovery). ``None`` hides the bar (indeterminate).
        """
        self._percent = None if percent is None else max(0.0, min(100.0, percent))
        self._progress_detail = detail
        self._refresh()

    def log_line(self, line: str) -> None:
        # Accept multi-line chunks (e.g. a block read from a log file). Each line
        # is stamped with its arrival time (TIA-73) so the tail aligns with the
        # phase stopwatch and the audit clock.
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        for ln in line.rstrip("\n").splitlines() or [""]:
            self._log.append((ts, ln))
        self._refresh()

    def result(self, result: Result) -> None:
        self._results.append(result)
        self._refresh()

    def handoff(self, message: str, url: str | None = None) -> None:
        self._handoff = (message, url)
        self._refresh()

    # -- rendering ---------------------------------------------------------- #
    def _refresh(self) -> None:
        if self._live.is_started:
            self._live.update(self._render())

    def _verdict_line(self, now: datetime) -> Text:
        """A compact verdict footer: per-status counts + total elapsed (TIA-75)."""
        counts = dict.fromkeys(Status, 0)
        for r in self._results:
            counts[r.status] += 1
        line = Text()
        order = [Status.PASS, Status.WARN, Status.FAIL, Status.SKIP, Status.ERROR]
        segments = [(s, counts[s]) for s in order if counts[s]]
        for i, (s, n) in enumerate(segments):
            if i:
                line.append(" · ", style="dim")
            style = "" if self._no_color else _STATUS_STYLE.get(s, "")
            line.append(f"{n} {s.value}", style=style)
        elapsed = (now - self._started_at).total_seconds()
        line.append(f"  —  took {format_duration(elapsed)}", style="dim")
        return line

    def _render(self) -> Group:
        parts: list[RenderableType] = []
        now = datetime.now(UTC)

        phase = Text()
        phase.append("▶ ", style="cyan")
        phase.append(self._phase, style="bold")
        if self._phase_detail:
            phase.append(f" — {self._phase_detail}", style="dim")
        # Per-phase stopwatch: how long the current phase has been running.
        phase_elapsed = (now - self._phase_started_at).total_seconds()
        phase.append(f"  ({format_duration(phase_elapsed)})", style="cyan")
        parts.append(phase)

        # Determinate progress bar when the native tool reports a percentage.
        if self._percent is not None:
            bar = ProgressBar(total=100, completed=self._percent, width=40)
            label = Text(f" {self._percent:5.1f}%", style="bold cyan")
            if self._progress_detail:
                label.append(f"  {self._progress_detail}", style="dim")
            parts.append(Group(bar, label))

        if self._log:
            log_text = Text()
            for i, (ts, ln) in enumerate(self._log):
                if i:
                    log_text.append("\n")
                log_text.append(f"{ts}  ", style="dim" if not self._no_color else "")
                # Highlight severity so failures pop in a long SWPM tail (TIA-74).
                if _LOG_ERROR_RE.search(ln):
                    line_style = "" if self._no_color else "bold red"
                elif _LOG_WARN_RE.search(ln):
                    line_style = "" if self._no_color else "yellow"
                else:
                    line_style = "" if self._no_color else "grey70"
                log_text.append(ln, style=line_style)
            parts.append(Panel(log_text, title="native log", border_style="grey37"))

        if self._results:
            table = Table(show_header=True, header_style="bold", expand=True)
            table.add_column("status", width=8)
            table.add_column("check")
            table.add_column("duration", width=10, justify="right")
            table.add_column("summary", overflow="fold")
            for r in self._results:
                glyph = self._status_glyph(r.status)
                style = "" if self._no_color else _STATUS_STYLE.get(r.status, "")
                if r.display_title != r.name:
                    name_cell = Text(r.display_title)
                    name_cell.append(f"\n{r.name}", style="dim")
                else:
                    name_cell = Text(r.name)
                table.add_row(
                    Text(f"{glyph} {r.status.value}", style=style),
                    name_cell,
                    Text(r.duration_str, style="dim"),
                    r.summary,
                )
            parts.append(table)
            # Verdict footer: status counts + total operation duration (TIA-75).
            parts.append(self._verdict_line(now))

        if self._handoff is not None:
            msg, url = self._handoff
            banner = Text()
            banner.append("⏸  HANDOFF  ", style="bold yellow")
            banner.append(msg)
            if url:
                banner.append("\nOpen: ", style="bold")
                banner.append(url, style="underline cyan")
            parts.append(Panel(banner, border_style="yellow"))

        # Title bar carries the audit clock: exact start + running elapsed.
        elapsed = (now - self._started_at).total_seconds()
        header = Text()
        header.append(self.title, style="bold white")
        header.append(
            f"\n⏱ started {self._started_at.strftime('%Y-%m-%d %H:%M:%SZ')}"
            f" · elapsed {format_duration(elapsed)}",
            style="cyan",
        )
        return Group(
            Panel(header, border_style="cyan"),
            *parts,
        )


def get_monitor(title: str, *, enabled: bool = True, no_emoji: bool = False) -> Monitor:
    """Factory: a live RichMonitor when enabled, else a silent NullMonitor."""
    return RichMonitor(title, no_emoji=no_emoji) if enabled else NullMonitor()
