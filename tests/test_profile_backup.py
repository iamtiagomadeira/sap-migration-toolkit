"""Tests for the ABAP profile & global-directory backup action (no real SAP).

A fake runner records the argv sequence and replays canned results so the
guarded flow (dry-run -> execute -> verify) is exercised without SSH or a
filesystem: profile scope vs global scope, the copy commands, and verify.
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult, Runner
from exodia.modules.abap.profiles.backup import ProfileBackupAction


class FakeRunner(Runner):
    """Records argv calls; replays ok output (non-empty for `ls -A`)."""

    def __init__(self, exit_code: int = 0, stdout: str = "file1\nfile2\n") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    def run(self, argv, timeout=300, input_text=None):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        return CommandResult(argv, self._exit_code, self._stdout, "")


def _ctx(runner: Runner, **params: object) -> Context:
    class _FakeCtx(Context):
        def runner(self):  # type: ignore[override]
            return runner

    return _FakeCtx(params=params)  # type: ignore[arg-type]


def test_backup_action_discovered() -> None:
    assert registry.get_action("abap.profile-backup") is not None


def test_dry_run_profile_scope_lists_one_dir() -> None:
    ctx = _ctx(FakeRunner(), backup_sid="PRD", backup_scope="profile")
    r = ProfileBackupAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert r.data["scope"] == "profile"
    assert r.data["sources"] == ["/sapmnt/PRD/profile"]
    assert "/sapmnt/PRD/profile" in r.detail


def test_dry_run_global_scope_lists_profile_and_global() -> None:
    ctx = _ctx(FakeRunner(), backup_sid="QAS", backup_scope="global")
    r = ProfileBackupAction().dry_run(ctx)
    assert r.data["sources"] == ["/sapmnt/QAS/profile", "/sapmnt/QAS/global"]
    assert r.facts["Scope"] == "global"


def test_dry_run_requires_sid() -> None:
    ctx = _ctx(FakeRunner())
    r = ProfileBackupAction().dry_run(ctx)
    assert r.status is Status.FAIL


def test_execute_copies_each_source() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, backup_sid="PRD", backup_scope="global", backup_dir="/backups/PRD")
    r = ProfileBackupAction().execute(ctx)
    assert r.status is Status.PASS
    # mkdir + 2 copies (profile + global)
    assert runner.calls[0][0] == "mkdir"
    cp_calls = [c for c in runner.calls if c[0] == "cp"]
    assert len(cp_calls) == 2
    assert cp_calls[0] == ["cp", "-a", "/sapmnt/PRD/profile", "/backups/PRD/profile"]
    assert cp_calls[1] == ["cp", "-a", "/sapmnt/PRD/global", "/backups/PRD/global"]


def test_execute_fails_and_pauses_on_copy_error() -> None:
    runner = FakeRunner(exit_code=1, stdout="")
    # mkdir also fails here (exit 1) -> fail before copying
    ctx = _ctx(runner, backup_sid="PRD")
    r = ProfileBackupAction().execute(ctx)
    assert r.status is Status.FAIL


def test_verify_passes_when_backup_nonempty() -> None:
    runner = FakeRunner(exit_code=0, stdout="DEFAULT.PFL\n")
    ctx = _ctx(runner, backup_sid="PRD", backup_dir="/backups/PRD")
    r = ProfileBackupAction().verify(ctx)
    assert r.status is Status.PASS
    assert r.data["verified"]


def test_verify_fails_when_backup_empty() -> None:
    runner = FakeRunner(exit_code=0, stdout="")
    ctx = _ctx(runner, backup_sid="PRD")
    r = ProfileBackupAction().verify(ctx)
    assert r.status is Status.FAIL


def test_rollback_is_documented_only() -> None:
    ctx = _ctx(FakeRunner(), backup_sid="PRD")
    r = ProfileBackupAction().rollback(ctx)
    assert r.status is Status.SKIP
    assert "non-destructive" in r.summary


def test_backup_action_carries_phase_and_title() -> None:
    from exodia.core.result import Phase

    action = ProfileBackupAction()
    assert action.phase is Phase.PREPARATION
    assert "Profile" in action.title
