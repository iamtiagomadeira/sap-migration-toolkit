"""Tests for the guarded RestoreDatabaseAction (driver-based).

Focus: the guarded flow (dry-run default => nothing executes), correct driver
selection per ctx.db_type, dry-run describing commands without side effects,
and execute/verify delegating to the driver.
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.runner import run_action
from exodia.core.shell import CommandResult, Runner
from exodia.modules.backup_restore.actions.restore_database import RestoreDatabaseAction


class FakeRunner(Runner):
    """Records argv calls and replays a canned result."""

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


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --- discovery ----------------------------------------------------------------


def test_action_is_discovered() -> None:
    assert "backup-restore.restore-database" in registry.actions()


def test_action_metadata() -> None:
    action = RestoreDatabaseAction()
    assert action.destructive is True
    assert action.requires_checks  # non-empty list of check names
    assert all(isinstance(c, str) for c in action.requires_checks)


# --- dry-run: no side effects -------------------------------------------------


def test_dry_run_describes_hana_without_running() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, db_type="hana", source="/backup", target="TENANT01")
    result = RestoreDatabaseAction().dry_run(ctx)
    assert result.status is Status.PASS
    assert runner.calls == []  # NOTHING executed
    assert "hana" in result.data["db_type"]
    assert result.data["commands"], "dry-run must describe commands"
    assert any("RECOVER DATABASE" in c for c in result.data["commands"])


def test_dry_run_describes_ase_sequence() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"log_dumps": ["/dumps/log1.dmp"]},
    )
    result = RestoreDatabaseAction().dry_run(ctx)
    assert result.status is Status.PASS
    assert runner.calls == []
    cmds = result.data["commands"]
    assert any("load database PRD" in c for c in cmds)
    assert any("load transaction PRD" in c for c in cmds)
    assert any("online database PRD" in c for c in cmds)


def test_dry_run_unknown_db_type_fails_cleanly() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, db_type="oracle", source="/x", target="Y")
    result = RestoreDatabaseAction().dry_run(ctx)
    assert result.status is Status.FAIL
    assert "unknown db_type" in result.summary
    assert runner.calls == []


# --- guarded flow -------------------------------------------------------------


def test_guarded_flow_dry_run_default_does_not_execute() -> None:
    """In dry-run mode (the default) run_guarded stops after dry-run."""
    runner = FakeRunner()
    ctx = _ctx(runner, db_type="hana", source="/backup", target="T01")  # dry_run=True by default
    assert ctx.dry_run is True
    results = RestoreDatabaseAction().run_guarded(ctx)
    assert len(results) == 1  # only the dry-run phase
    assert results[0].name.endswith(".dry-run")
    assert runner.calls == []  # execute NEVER ran


def test_guarded_flow_execute_without_yes_awaits_confirmation() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, db_type="hana", source="/backup", target="T01", dry_run=False)
    results = RestoreDatabaseAction().run_guarded(ctx)
    # dry-run + a SKIP awaiting confirmation; still no execution
    assert results[-1].status is Status.SKIP
    assert "confirmation" in results[-1].summary
    assert runner.calls == []


def test_guarded_flow_execute_with_yes_runs_and_verifies() -> None:
    runner = FakeRunner(exit_code=0, stdout="TENANT01,YES")
    ctx = _ctx(
        runner, db_type="hana", source="/backup", target="TENANT01", dry_run=False, assume_yes=True
    )
    results = RestoreDatabaseAction().run_guarded(ctx)
    phases = [r.name.rsplit(".", 1)[-1] for r in results]
    # dry-run phase from the action; execute/verify results carry the driver's
    # own result names (backup-restore.hana.restore / .verify).
    assert phases == ["dry-run", "restore", "verify"]
    assert all(r.status is Status.PASS for r in results)
    # execute ran the recover, verify ran the M_DATABASES query
    assert len(runner.calls) == 2


def test_execute_delegates_to_ase_driver() -> None:
    runner = FakeRunner(exit_code=0, stdout="ok")
    ctx = _ctx(
        runner,
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"log_dumps": ["/dumps/log1.dmp"]},
    )
    result = RestoreDatabaseAction().execute(ctx)
    assert result.status is Status.PASS
    joined = [" ".join(c) for c in runner.calls]
    assert "load database PRD" in joined[0]
    assert "online database PRD" in joined[-1]


def test_rollback_is_documented_only() -> None:
    ctx = _ctx(FakeRunner(), db_type="hana")
    result = RestoreDatabaseAction().rollback(ctx)
    assert result.status is Status.SKIP
    assert "rollback" in result.name


def test_full_run_action_with_prechecks_dry_run() -> None:
    """End-to-end via run_action: no prechecks registered => guarded dry-run."""
    runner = FakeRunner()
    ctx = _ctx(runner, db_type="ase", source="/dumps/full.dmp", target="PRD")
    action = RestoreDatabaseAction()
    prechecks = [
        cls() for c in action.requires_checks if (cls := registry.get_check(c)) is not None
    ]
    results = run_action(action, prechecks, ctx)
    assert results[-1].name.endswith(".dry-run")
    assert runner.calls == []
