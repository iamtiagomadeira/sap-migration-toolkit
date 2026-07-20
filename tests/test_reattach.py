"""Tests for --reattach event replay (TIA-77)."""

from __future__ import annotations

import json
from pathlib import Path

from exodia.core.evidence import (
    find_active_bundle,
    read_events,
    replay_events,
)
from exodia.core.monitor import RichMonitor


def _write_bundle(
    root: Path, name: str, *, sealed: bool, events: list[dict]
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    manifest = {"methodology": "tenant-copy", "operation": "copy-tenant"}
    if sealed:
        manifest["sealed"] = "2026-07-20T10:00:00+00:00"
    (d / "manifest.json").write_text(json.dumps(manifest))
    with (d / "run.jsonl").open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return d


def test_read_events_skips_partial_line(tmp_path: Path) -> None:
    d = tmp_path / "b"
    d.mkdir()
    (d / "run.jsonl").write_text(
        '{"ts":"t1","kind":"result","name":"a","status":"pass"}\n'
        '{"ts":"t2","kind":"result","name":"b"}\n'
        '{"ts":"t3","kind":"result","name":"c",'  # truncated (crash mid-flush)
    )
    events = read_events(d)
    assert len(events) == 2
    assert events[0]["name"] == "a"


def test_read_events_missing_file(tmp_path: Path) -> None:
    assert read_events(tmp_path / "nope") == []


def test_find_active_bundle_ignores_sealed(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path, "sealed_run", sealed=True,
        events=[{"ts": "2026-07-20T09:00:00", "kind": "result", "name": "x"}],
    )
    active = _write_bundle(
        tmp_path, "live_run", sealed=False,
        events=[{"ts": "2026-07-20T11:00:00", "kind": "result", "name": "y"}],
    )
    found = find_active_bundle(tmp_path)
    assert found == active


def test_find_active_bundle_none_when_all_sealed(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path, "sealed_run", sealed=True,
        events=[{"ts": "t", "kind": "result", "name": "x"}],
    )
    assert find_active_bundle(tmp_path) is None


def test_find_active_bundle_picks_newest(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path, "old", sealed=False,
        events=[{"ts": "2026-07-20T08:00:00", "kind": "result", "name": "a"}],
    )
    newer = _write_bundle(
        tmp_path, "new", sealed=False,
        events=[{"ts": "2026-07-20T12:00:00", "kind": "result", "name": "b"}],
    )
    assert find_active_bundle(tmp_path) == newer


def test_replay_events_rebuilds_results(tmp_path: Path) -> None:
    d = _write_bundle(
        tmp_path, "run", sealed=False,
        events=[
            {"ts": "t1", "kind": "run.start"},
            {"ts": "t2", "kind": "result", "name": "hana.free-space",
             "status": "pass", "summary": "142G free", "duration_seconds": 8.0},
            {"ts": "t3", "kind": "result", "name": "hana.license",
             "status": "fail", "summary": "expired", "duration_seconds": 2.0},
        ],
    )
    from io import StringIO

    from rich.console import Console

    mon = RichMonitor("re", console=Console(file=StringIO(), force_terminal=True))
    n = replay_events(d, mon)
    assert n == 3  # all events consumed
    # Two result rows rebuilt with their persisted timing.
    assert len(mon._results) == 2
    assert mon._results[0].name == "hana.free-space"
    assert mon._results[0].duration_seconds == 8.0
    assert mon._results[1].status.value == "fail"


def test_replay_tolerates_unknown_status(tmp_path: Path) -> None:
    d = _write_bundle(
        tmp_path, "run", sealed=False,
        events=[{"ts": "t", "kind": "result", "name": "x", "status": "bogus"}],
    )
    from io import StringIO

    from rich.console import Console

    mon = RichMonitor("re", console=Console(file=StringIO(), force_terminal=True))
    replay_events(d, mon)
    assert mon._results[0].status.value == "pass"  # falls back safely
