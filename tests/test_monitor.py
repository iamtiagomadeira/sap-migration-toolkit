"""Tests for the live monitor (TIA-66)."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from exodia.core.monitor import (
    Monitor,
    NullMonitor,
    RichMonitor,
    get_monitor,
)
from exodia.core.result import Result


def _console() -> Console:
    # Force a fixed width and capture output so rendering is deterministic.
    return Console(file=StringIO(), width=100, force_terminal=True)


def test_null_monitor_is_a_noop() -> None:
    m = NullMonitor()
    with m:
        m.phase("x")
        m.log_line("line")
        m.result(Result.ok("t", "ok"))
        m.handoff("paused", "http://host:4237")
    # Nothing to assert beyond "did not raise" — it's a silent sink.


def test_null_and_rich_satisfy_the_protocol() -> None:
    assert isinstance(NullMonitor(), Monitor)
    assert isinstance(RichMonitor("t", console=_console()), Monitor)


def test_get_monitor_toggles_implementation() -> None:
    assert isinstance(get_monitor("t", enabled=False), NullMonitor)
    assert isinstance(get_monitor("t", enabled=True), RichMonitor)


def test_rich_monitor_renders_lifecycle_without_error() -> None:
    console = _console()
    m = RichMonitor("SWPM restore", console=console)
    with m:
        m.phase("pre-checks", "3 checks")
        m.log_line("INFO: sapinst started\nINFO: unpacking")
        m.result(Result.ok("backup-restore.hana.free-space", "120G free"))
        m.result(Result.fail("x.mount", "mount missing"))
        m.handoff("SWPM handed off to GUI", "https://host:4237/sapinst")
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert "SWPM restore" in out
    assert "free-space" in out


def test_log_tail_is_bounded() -> None:
    m = RichMonitor("t", console=_console(), log_lines=3)
    for i in range(10):
        m.log_line(f"line {i}")
    # deque maxlen keeps only the last 3 lines.
    assert list(m._log) == ["line 7", "line 8", "line 9"]


def test_multiline_log_chunk_is_split() -> None:
    m = RichMonitor("t", console=_console(), log_lines=10)
    m.log_line("a\nb\nc")
    assert list(m._log) == ["a", "b", "c"]
