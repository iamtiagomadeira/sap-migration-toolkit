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
    # deque maxlen keeps only the last 3 lines; entries are (timestamp, text).
    assert [text for _ts, text in m._log] == ["line 7", "line 8", "line 9"]
    # Each entry carries an HH:MM:SS arrival stamp (TIA-73).
    assert all(len(ts) == 8 and ts.count(":") == 2 for ts, _ in m._log)


def test_multiline_log_chunk_is_split() -> None:
    m = RichMonitor("t", console=_console(), log_lines=10)
    m.log_line("a\nb\nc")
    assert [text for _ts, text in m._log] == ["a", "b", "c"]


# -- TIA-74: severity highlighting in the native log tail --------------------- #
def test_log_severity_highlighting() -> None:
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=True)
    m = RichMonitor("restore", console=console)
    with m:
        m.log_line("INFO everything nominal")
        m.log_line("ERROR recovery failed on volume 3")
        m.log_line("WARNING slow io detected")
    out = buf.getvalue()
    # The error and warning lines are rendered with ANSI colour codes.
    assert "\x1b[" in out  # colour was emitted
    assert "recovery failed" in out
    assert "slow io detected" in out


# -- TIA-73: each log line is timestamped ------------------------------------- #
def test_log_lines_are_timestamped() -> None:
    m = RichMonitor("t", console=_console())
    m.log_line("hello")
    ts, text = next(iter(m._log))
    assert text == "hello"
    assert len(ts) == 8 and ts.count(":") == 2


# -- TIA-75: verdict footer with counts + total duration ---------------------- #
def test_verdict_line_counts_and_duration() -> None:
    from datetime import UTC, datetime

    m = RichMonitor("t", console=_console())
    r_pass = Result.ok("a", "ok")
    r_pass.duration_seconds = 10.0
    r_fail = Result.fail("b", "nope")
    r_fail.duration_seconds = 5.0
    m._results = [r_pass, r_fail]
    line = m._verdict_line(datetime.now(UTC)).plain
    assert "1 pass" in line
    assert "1 fail" in line
    assert "took" in line


# -- TIA-76: --no-emoji uses ASCII status tags -------------------------------- #
def test_no_emoji_uses_ascii_tags() -> None:
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=True)
    m = RichMonitor("t", console=console, no_emoji=True)
    assert m._status_glyph(Result.ok("a", "ok").status) == "[PASS]"
    assert m._status_glyph(Result.fail("b", "no").status) == "[FAIL]"
    with m:
        m.result(Result.ok("a", "ok"))
        m.result(Result.fail("b", "no"))
    out = buf.getvalue()
    assert "[PASS]" in out
    assert "[FAIL]" in out
    # No emoji glyphs leak through.
    assert "✅" not in out
    assert "❌" not in out


def test_get_monitor_forwards_no_emoji() -> None:
    m = get_monitor("t", enabled=True, no_emoji=True)
    assert isinstance(m, RichMonitor)
    assert m._no_emoji is True
