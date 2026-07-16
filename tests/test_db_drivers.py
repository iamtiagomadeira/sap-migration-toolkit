"""Tests for the pluggable DB restore drivers (HANA + ASE) and the factory.

A FakeRunner (subclass of the real Runner) returns pre-fabricated CommandResults
and records every argv it was asked to run, so we can assert on command
sequences without touching a real database — and prove dry-run has no side
effects.
"""

from __future__ import annotations

import pytest

from exodia.core import Context, Status
from exodia.core.shell import CommandResult, Runner
from exodia.modules.backup_restore.db_drivers import (
    DBRestoreDriver,
    get_driver,
    supported_db_types,
)
from exodia.modules.backup_restore.db_drivers.ase import AseRestoreDriver
from exodia.modules.backup_restore.db_drivers.hana import HanaRestoreDriver


class FakeRunner(Runner):
    """Records argv calls and replays canned results (no real subprocess)."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        return CommandResult(argv, self._exit_code, self._stdout, self._stderr)


def _ctx_with_runner(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --- factory ------------------------------------------------------------------


def test_get_driver_returns_correct_type() -> None:
    assert isinstance(get_driver("hana"), HanaRestoreDriver)
    assert isinstance(get_driver("ase"), AseRestoreDriver)
    # case-insensitive / whitespace tolerant
    assert isinstance(get_driver("  HANA "), HanaRestoreDriver)


def test_get_driver_unknown_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown db_type"):
        get_driver("oracle")


def test_get_driver_missing_raises() -> None:
    with pytest.raises(ValueError, match="db_type is required"):
        get_driver(None)


def test_supported_db_types_lists_both() -> None:
    assert supported_db_types() == ["ase", "hana"]


def test_drivers_declare_db_type() -> None:
    assert HanaRestoreDriver.db_type == "hana"
    assert AseRestoreDriver.db_type == "ase"
    assert issubclass(HanaRestoreDriver, DBRestoreDriver)
    assert issubclass(AseRestoreDriver, DBRestoreDriver)


# --- HANA ---------------------------------------------------------------------


def test_hana_plan_builds_recover_command_no_side_effects() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(runner, db_type="hana", source="/backup/DATA", target="TENANT01")
    plan = get_driver("hana").plan(ctx)
    # planning must not run anything
    assert runner.calls == []
    assert len(plan) == 1
    argv = plan[0].argv
    assert argv[0] == "hdbsql"
    joined = " ".join(argv)
    assert "RECOVER DATABASE FOR TENANT01" in joined
    assert "USING CATALOG PATH" in joined
    assert "USING LOG PATH" in joined
    assert "USING DATA PATH" in joined


def test_hana_restore_runs_recover() -> None:
    runner = FakeRunner(exit_code=0, stdout="0 rows affected")
    ctx = _ctx_with_runner(runner, db_type="hana", source="/backup/DATA", target="TENANT01")
    result = get_driver("hana").restore(ctx)
    assert result.status is Status.PASS
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == "hdbsql"
    assert any("RECOVER DATABASE FOR TENANT01" in a for a in runner.calls[0])


def test_hana_restore_without_source_fails_without_running() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(runner, db_type="hana", target="TENANT01")
    result = get_driver("hana").restore(ctx)
    assert result.status is Status.FAIL
    assert runner.calls == []  # no command run when source missing


def test_hana_restore_failure_surfaces_fail() -> None:
    runner = FakeRunner(exit_code=1, stderr="log backup missing")
    ctx = _ctx_with_runner(runner, db_type="hana", source="/backup", target="T01")
    result = get_driver("hana").restore(ctx)
    assert result.status is Status.FAIL
    assert "RECOVER DATABASE failed" in result.summary


def test_hana_verify_online() -> None:
    runner = FakeRunner(exit_code=0, stdout="TENANT01,YES")
    ctx = _ctx_with_runner(runner, db_type="hana", target="TENANT01")
    result = get_driver("hana").verify(ctx)
    assert result.status is Status.PASS
    assert "M_DATABASES" in " ".join(runner.calls[0])


def test_hana_verify_not_reachable_fails() -> None:
    runner = FakeRunner(exit_code=1, stderr="connection refused")
    ctx = _ctx_with_runner(runner, db_type="hana", target="TENANT01")
    result = get_driver("hana").verify(ctx)
    assert result.status is Status.FAIL


def test_hana_no_password_in_argv() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(
        runner,
        db_type="hana",
        source="/backup",
        target="T01",
        params={"hdb_userstore_key": "BACKUPKEY", "hdb_password": "s3cret"},
    )
    plan = get_driver("hana").plan(ctx)
    flat = " ".join(plan[0].argv)
    assert "s3cret" not in flat
    assert "-U" in plan[0].argv and "BACKUPKEY" in plan[0].argv


# --- ASE ----------------------------------------------------------------------


def test_ase_plan_sequence_load_db_then_tran_then_online() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(
        runner,
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"log_dumps": ["/dumps/log1.dmp", "/dumps/log2.dmp"]},
    )
    plan = get_driver("ase").plan(ctx)
    assert runner.calls == []  # planning has no side effects
    sqls = [" ".join(pc.argv) for pc in plan]
    # order: load database -> load transaction (x2) -> online database
    assert "load database PRD from '/dumps/full.dmp'" in sqls[0]
    assert "load transaction PRD from '/dumps/log1.dmp'" in sqls[1]
    assert "load transaction PRD from '/dumps/log2.dmp'" in sqls[2]
    assert "online database PRD" in sqls[3]
    assert len(plan) == 4


def test_ase_plan_no_logs_still_loads_and_onlines() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(runner, db_type="ase", source="/dumps/full.dmp", target="PRD")
    plan = get_driver("ase").plan(ctx)
    sqls = [" ".join(pc.argv) for pc in plan]
    assert len(plan) == 2
    assert "load database PRD" in sqls[0]
    assert "online database PRD" in sqls[1]


def test_ase_restore_runs_full_sequence_in_order() -> None:
    runner = FakeRunner(exit_code=0, stdout="ok")
    ctx = _ctx_with_runner(
        runner,
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"log_dumps": ["/dumps/log1.dmp"]},
    )
    result = get_driver("ase").restore(ctx)
    assert result.status is Status.PASS
    assert len(runner.calls) == 3
    joined = [" ".join(c) for c in runner.calls]
    assert "load database PRD" in joined[0]
    assert "load transaction PRD from '/dumps/log1.dmp'" in joined[1]
    assert "online database PRD" in joined[2]


def test_ase_restore_stops_at_first_failure() -> None:
    runner = FakeRunner(exit_code=1, stderr="device not found")
    ctx = _ctx_with_runner(runner, db_type="ase", source="/dumps/full.dmp", target="PRD")
    result = get_driver("ase").restore(ctx)
    assert result.status is Status.FAIL
    # failed on the very first command, so it must not continue
    assert len(runner.calls) == 1


def test_ase_restore_without_target_fails() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(runner, db_type="ase", source="/dumps/full.dmp")
    result = get_driver("ase").restore(ctx)
    assert result.status is Status.FAIL
    assert runner.calls == []


def test_ase_verify_present() -> None:
    runner = FakeRunner(exit_code=0, stdout="PRD 0")
    ctx = _ctx_with_runner(runner, db_type="ase", target="PRD")
    result = get_driver("ase").verify(ctx)
    assert result.status is Status.PASS
    assert "sysdatabases" in " ".join(runner.calls[0])


def test_ase_no_cleartext_password_in_argv() -> None:
    runner = FakeRunner()
    ctx = _ctx_with_runner(
        runner,
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"ase_password": "hunter2"},
    )
    plan = get_driver("ase").plan(ctx)
    for pc in plan:
        assert "hunter2" not in " ".join(pc.argv)
        assert "-P" not in pc.argv
