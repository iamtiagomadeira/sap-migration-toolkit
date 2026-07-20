"""Smoke tests for the Exodia core — real execution, no mocks needed for these."""

from __future__ import annotations

from exodia.core import Context, Result, Status
from exodia.core.knowledge import enrich, lookup
from exodia.core.registry import registry
from exodia.core.report import exit_code, tally, verdict_line, worst_status
from exodia.core.runner import run_checks


def test_result_helpers() -> None:
    assert Result.ok("x").status is Status.PASS
    assert Result.fail("x", "bad").status.is_blocking
    assert not Result.warn("x", "meh").status.is_blocking


def test_registry_discovers_free_space() -> None:
    checks = registry.checks()
    assert "core.free-space" in checks


def test_free_space_check_runs_locally() -> None:
    """Runs the real df-based check against the local root filesystem."""
    ctx = Context(params={"path": "/", "min_gb": 0})  # 0 GB threshold always passes
    check = registry.get_check("core.free-space")
    assert check is not None
    results = run_checks([check()], ctx)
    assert len(results) == 1
    assert results[0].status is Status.PASS
    assert results[0].data["avail_gb"] >= 0


def test_free_space_blocking_fail_stops_pipeline() -> None:
    ctx = Context(params={"path": "/", "min_gb": 10**9})  # impossible threshold
    check = registry.get_check("core.free-space")
    assert check is not None
    results = run_checks([check()], ctx)
    assert results[0].status is Status.FAIL


def test_kb_lookup_hana_log_backup() -> None:
    entry = lookup("recovery could not be completed: log backup 1247 missing")
    assert entry is not None
    assert entry.sap_note == "1642148"


def test_kb_enrich_attaches_fix() -> None:
    r = Result.fail("hana.recover", "log backup 1247 missing")
    enrich(r)
    assert r.sap_note == "1642148"
    assert r.fix


def test_exit_code_and_worst_status() -> None:
    good = [Result.ok("a"), Result.warn("b", "m")]
    bad = [Result.ok("a"), Result.fail("b", "x")]
    assert exit_code(good) == 0
    assert exit_code(bad) == 1
    assert worst_status(bad) is Status.FAIL


def test_tally_counts_by_status() -> None:
    results = [Result.ok("a"), Result.ok("b"), Result.warn("c", "m"), Result.fail("d", "x")]
    counts = tally(results)
    assert counts[Status.PASS] == 2
    assert counts[Status.WARN] == 1
    assert counts[Status.FAIL] == 1
    assert counts[Status.SKIP] == 0


def test_verdict_ready_when_all_pass() -> None:
    line = verdict_line([Result.ok("a"), Result.ok("b")])
    assert "Ready to proceed" in line
    assert "2 passed" in line


def test_verdict_caveats_when_only_warnings() -> None:
    line = verdict_line([Result.ok("a"), Result.warn("b", "m")])
    assert "Ready with caveats" in line
    assert "1 warnings" in line


def test_verdict_blocks_on_failure() -> None:
    line = verdict_line([Result.ok("a"), Result.fail("b", "x"), Result.fail("c", "y")])
    assert "NOT ready" in line
    assert "2 blocking" in line


def test_verdict_empty_is_safe() -> None:
    assert "No checks ran" in verdict_line([])
