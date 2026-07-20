"""Tests for report rendering — Duration column, verdict footer, NO_COLOR (TIA-75/76)."""

from __future__ import annotations

import re
from io import StringIO

from rich.console import Console

from exodia.core import report
from exodia.core.result import Result, Status

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(s: str) -> str:
    return _ANSI.sub("", s)


def _capture(results: list[Result], *, no_emoji: bool = False) -> str:
    buf = StringIO()
    console = Console(file=buf, width=200, force_terminal=True)
    report.render_table(results, "Test", console, no_emoji=no_emoji)
    return _strip(buf.getvalue())


def _timed(r: Result, seconds: float) -> Result:
    r.duration_seconds = seconds
    return r


# -- TIA-75: Duration column + verdict footer with counts + total ------------- #
def test_table_has_duration_column() -> None:
    out = _capture([_timed(Result.ok("a", "fine"), 12.0)])
    assert "Duration" in out


def test_verdict_footer_shows_counts_and_total() -> None:
    results = [
        _timed(Result.ok("a", "fine"), 10.0),
        _timed(Result.warn("b", "meh"), 2.0),
        _timed(Result.fail("c", "bad"), 5.0),
    ]
    out = _capture(results)
    assert "1 passed" in out
    assert "1 warnings" in out
    assert "1 failed" in out
    assert "Total duration" in out


def test_verdict_line_blocking_is_not_ready() -> None:
    line = report.verdict_line([Result.fail("c", "bad")])
    assert "NOT ready" in line


def test_verdict_line_all_pass_is_ready() -> None:
    line = report.verdict_line([Result.ok("a", "ok")])
    assert "Ready to proceed" in line


def test_verdict_line_empty() -> None:
    assert "No checks ran" in report.verdict_line([])


# -- TIA-76: --no-emoji collapses glyphs to ASCII tags ------------------------ #
def test_no_emoji_table_uses_ascii() -> None:
    out = _capture([Result.ok("a", "ok"), Result.fail("b", "no")], no_emoji=True)
    assert "[PASS]" in out
    assert "[FAIL]" in out
    assert "✅" not in out
    assert "❌" not in out


def test_no_emoji_verdict_marker() -> None:
    line = report.verdict_line([Result.fail("c", "bad")], no_emoji=True)
    assert "[FAIL]" in line
    assert "⛔" not in line


def test_no_color_env_honoured(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NO_COLOR", "1")
    # No explicit console: render_table builds one that honours NO_COLOR.
    report.render_table([Result.ok("a", "ok")], "Test", no_emoji=False)
    out = capsys.readouterr().out
    # NO_COLOR strips ANSI escape sequences and forces ASCII tags.
    assert "\x1b[" not in out
    assert "[PASS]" in out


# -- helpers ------------------------------------------------------------------ #
def test_total_duration_none_when_untimed() -> None:
    assert report._total_duration([Result.ok("a", "ok")]) is None


def test_total_duration_sums_timed() -> None:
    results = [_timed(Result.ok("a", "ok"), 3.0), _timed(Result.ok("b", "ok"), 4.5)]
    assert report._total_duration(results) == 7.5


def test_tally_counts() -> None:
    results = [Result.ok("a", "ok"), Result.ok("b", "ok"), Result.fail("c", "no")]
    counts = report.tally(results)
    assert counts[Status.PASS] == 2
    assert counts[Status.FAIL] == 1
