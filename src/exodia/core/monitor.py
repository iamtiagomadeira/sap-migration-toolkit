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

from collections import deque
from types import TracebackType
from typing import Protocol, runtime_checkable

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .result import Result, Status

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


@runtime_checkable
class Monitor(Protocol):
    """The surface every monitor (Rich now, Textual later) implements."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def phase(self, name: str, detail: str = "") -> None: ...
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
    ) -> None:
        self.title = title
        self.console = console or Console()
        self._log: deque[str] = deque(maxlen=max(1, log_lines))
        self._results: list[Result] = []
        self._phase = "starting…"
        self._phase_detail = ""
        self._handoff: tuple[str, str | None] | None = None
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
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
        self._refresh()

    def log_line(self, line: str) -> None:
        # Accept multi-line chunks (e.g. a block read from a log file).
        for ln in line.rstrip("\n").splitlines() or [""]:
            self._log.append(ln)
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

    def _render(self) -> Group:
        parts: list[RenderableType] = []

        phase = Text()
        phase.append("▶ ", style="cyan")
        phase.append(self._phase, style="bold")
        if self._phase_detail:
            phase.append(f"  {self._phase_detail}", style="dim")
        parts.append(phase)

        if self._log:
            log_text = Text("\n".join(self._log), style="grey70")
            parts.append(Panel(log_text, title="native log", border_style="grey37"))

        if self._results:
            table = Table(show_header=True, header_style="bold", expand=True)
            table.add_column("status", width=8)
            table.add_column("name")
            table.add_column("summary", overflow="fold")
            for r in self._results:
                icon = _STATUS_ICON.get(r.status, "")
                style = _STATUS_STYLE.get(r.status, "")
                table.add_row(
                    Text(f"{icon} {r.status.value}", style=style),
                    r.name,
                    r.summary,
                )
            parts.append(table)

        if self._handoff is not None:
            msg, url = self._handoff
            banner = Text()
            banner.append("⏸  HANDOFF  ", style="bold yellow")
            banner.append(msg)
            if url:
                banner.append("\nOpen: ", style="bold")
                banner.append(url, style="underline cyan")
            parts.append(Panel(banner, border_style="yellow"))

        return Group(
            Panel(Text(self.title, style="bold white"), border_style="cyan"),
            *parts,
        )


def get_monitor(title: str, *, enabled: bool = True) -> Monitor:
    """Factory: a live RichMonitor when enabled, else a silent NullMonitor."""
    return RichMonitor(title) if enabled else NullMonitor()
