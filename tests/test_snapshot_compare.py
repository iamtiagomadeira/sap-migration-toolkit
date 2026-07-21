"""Tests for portable snapshots and the cross-side comparison (ECS air-gap flow).

Covers: snapshot capture/serialisation, the SHA-256 self-hash + tamper
detection, round-tripping through disk, and the compare engine's verdicts
(match / differ / one-sided / error) plus the aggregate alignment verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

from exodia.core.compare import compare_snapshots
from exodia.core.result import Result, Status
from exodia.core.snapshot import SNAPSHOT_SCHEMA, Snapshot, verify_snapshot


def _src(results: list[Result], label: str = "PRD") -> Snapshot:
    return Snapshot(side="source", label=label, operation="tenant-copy.hana.readiness", results=results)


def _tgt(results: list[Result], label: str = "QAS") -> Snapshot:
    return Snapshot(side="target", label=label, operation="tenant-copy.hana.readiness", results=results)


# --------------------------------------------------------------------------- #
# Snapshot serialisation + integrity
# --------------------------------------------------------------------------- #


def test_snapshot_roundtrip(tmp_path: Path) -> None:
    snap = _src([Result.ok("a", "ok", data={"version": "2.00.067"})])
    p = snap.write(tmp_path / "src.json")
    assert p.is_file()
    loaded = Snapshot.read(p)
    assert loaded.side == "source"
    assert loaded.label == "PRD"
    assert loaded.results[0].name == "a"
    assert loaded.results[0].data["version"] == "2.00.067"


def test_snapshot_has_schema_and_hash(tmp_path: Path) -> None:
    snap = _src([Result.ok("a", "ok")])
    p = snap.write(tmp_path / "src.json")
    data = json.loads(p.read_text())
    assert data["schema"] == SNAPSHOT_SCHEMA
    assert len(data["sha256"]) == 64


def test_snapshot_verify_intact(tmp_path: Path) -> None:
    snap = _src([Result.ok("a", "ok", data={"count": 5})])
    p = snap.write(tmp_path / "src.json")
    assert verify_snapshot(p) == []


def test_snapshot_tamper_detected(tmp_path: Path) -> None:
    snap = _src([Result.ok("a", "ok", data={"count": 5})])
    p = snap.write(tmp_path / "src.json")
    data = json.loads(p.read_text())
    data["results"][0]["data"]["count"] = 999  # tamper
    p.write_text(json.dumps(data))
    problems = verify_snapshot(p)
    assert problems and "hash mismatch" in problems[0]


def test_snapshot_verify_missing_file(tmp_path: Path) -> None:
    assert verify_snapshot(tmp_path / "nope.json")


def test_snapshot_verify_bad_schema(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"schema": "wrong", "sha256": "x"}))
    problems = verify_snapshot(p)
    assert problems and "schema" in problems[0]


def test_snapshot_meta_captures_custody() -> None:
    snap = Snapshot.capture(
        side="source", operation="op", results=[Result.ok("a", "ok")], label="PRD"
    )
    d = snap.to_dict()
    assert "operator" in d["meta"]
    assert "tool_version" in d["meta"]
    assert "captured_at" in d["meta"]


# --------------------------------------------------------------------------- #
# Comparison verdicts
# --------------------------------------------------------------------------- #


def test_compare_all_match() -> None:
    src = _src([Result.ok("version-match", "v", data={"version": "2.00.067"})])
    tgt = _tgt([Result.ok("version-match", "v", data={"version": "2.00.067"})])
    rpt = compare_snapshots(src, tgt)
    assert rpt.aligned is True
    assert rpt.rows[0].verdict == "match"
    assert rpt.verdict_result().status is Status.PASS


def test_compare_data_differ() -> None:
    src = _src([Result.ok("version-match", "v", data={"version": "2.00.067"})])
    tgt = _tgt([Result.ok("version-match", "v", data={"version": "2.00.065"})])
    rpt = compare_snapshots(src, tgt)
    assert rpt.aligned is False
    assert rpt.rows[0].verdict == "differ"
    assert "version" in rpt.rows[0].detail
    assert rpt.verdict_result().status is Status.FAIL


def test_compare_source_only() -> None:
    src = _src([Result.ok("only-here", "x", data={"count": 1})])
    tgt = _tgt([])
    rpt = compare_snapshots(src, tgt)
    assert rpt.rows[0].verdict == "source-only"
    assert rpt.only_one_side


def test_compare_target_only() -> None:
    src = _src([])
    tgt = _tgt([Result.ok("only-there", "x", data={"count": 1})])
    rpt = compare_snapshots(src, tgt)
    assert rpt.rows[0].verdict == "target-only"


def test_compare_error_side() -> None:
    src = _src([Result.error("boom", "read failed")])
    tgt = _tgt([Result.ok("boom", "ok", data={"count": 1})])
    rpt = compare_snapshots(src, tgt)
    assert rpt.rows[0].verdict == "error"
    assert rpt.verdict_result().status is Status.FAIL


def test_compare_no_salient_data_falls_back_to_status() -> None:
    # no salient fields => compare status; same status => match
    src = _src([Result.ok("x", "ok", data={"note": "irrelevant"})])
    tgt = _tgt([Result.ok("x", "ok", data={"note": "different but ignored"})])
    rpt = compare_snapshots(src, tgt)
    assert rpt.rows[0].verdict == "match"


def test_compare_status_differs_without_data() -> None:
    src = _src([Result.ok("x", "ok")])
    tgt = _tgt([Result.warn("x", "meh")])
    rpt = compare_snapshots(src, tgt)
    assert rpt.rows[0].verdict == "differ"


def test_compare_empty_is_skip() -> None:
    rpt = compare_snapshots(_src([]), _tgt([]))
    assert rpt.verdict_result().status is Status.SKIP


def test_compare_tally_counts() -> None:
    src = _src(
        [
            Result.ok("a", "", data={"count": 1}),
            Result.ok("b", "", data={"count": 2}),
            Result.ok("c", "", data={"count": 3}),
        ]
    )
    tgt = _tgt(
        [
            Result.ok("a", "", data={"count": 1}),  # match
            Result.ok("b", "", data={"count": 9}),  # differ
            # c missing => source-only
        ]
    )
    rpt = compare_snapshots(src, tgt)
    tally = rpt.verdict_result().data
    assert tally["matched"] == 1
    assert tally["differing"] == 1
    assert tally["one_sided"] == 1
    assert tally["total"] == 3
