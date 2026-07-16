"""Tests for the headless SWPM system-copy orchestration.

Covers:
  * dry-run runs NOTHING (no side effects, runner untouched);
  * inifile validation (missing file / missing keys => clean FAIL);
  * correct SAPINST_* env construction + observer-mode GUI default;
  * 'waiting for input' => WARN carrying the GUI URL (observer-mode handoff);
  * error => FAIL (pause, never kill);
  * the instkey.pkey secret NEVER appears in any Result/argv/log.
"""

from __future__ import annotations

from pathlib import Path

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult, Runner
from exodia.modules.backup_restore.actions.swpm.planner import (
    GUI_SERVER_PORT,
    InifileError,
    RunState,
    build_plan,
    build_sapinst_env,
    parse_progress,
    validate_inifile,
)
from exodia.modules.backup_restore.actions.swpm_system_copy import SwpmSystemCopyAction

# A believable secret value that must never leak into any Result/argv/log.
SECRET_PKEY_VALUE = "TOPSECRET-PKEY-DO-NOT-LEAK-abc123"


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


def _write_inifile(tmp_path: Path, *, with_pkey: bool = False, complete: bool = True) -> str:
    """Create a minimal inifile.params for tests."""
    lines = []
    if complete:
        lines += [
            "SAPINST.CD.PACKAGE.LOCATION = /sap/media",
            "NW_System.Code = ABAP",
        ]
    else:
        lines += ["SOMETHING.ELSE = 1"]
    if with_pkey:
        # Secret is embedded inline; its VALUE must never leak.
        lines.append(f"instkey.pkey = {SECRET_PKEY_VALUE}")
    path = tmp_path / "inifile.params"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# --- discovery ----------------------------------------------------------------


def test_action_is_discovered() -> None:
    assert "backup-restore.swpm.system-copy" in registry.actions()


def test_action_metadata() -> None:
    action = SwpmSystemCopyAction()
    assert action.destructive is True
    assert action.name == "backup-restore.swpm.system-copy"


# --- inifile validation -------------------------------------------------------


def test_validate_inifile_missing_path_raises() -> None:
    try:
        validate_inifile(None)
    except InifileError as exc:
        assert "no inifile" in str(exc)
    else:
        raise AssertionError("expected InifileError for missing path")


def test_validate_inifile_nonexistent_raises() -> None:
    try:
        validate_inifile("/no/such/inifile.params")
    except InifileError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected InifileError for nonexistent file")


def test_validate_inifile_missing_keys_raises(tmp_path: Path) -> None:
    path = _write_inifile(tmp_path, complete=False)
    try:
        validate_inifile(path)
    except InifileError as exc:
        assert "missing required key" in str(exc)
    else:
        raise AssertionError("expected InifileError for incomplete inifile")


def test_validate_inifile_ok_detects_pkey_without_reading_value(tmp_path: Path) -> None:
    path = _write_inifile(tmp_path, with_pkey=True)
    info = validate_inifile(path)
    assert info.has_secret_pkey is True
    assert info.keys_found  # minimum keys present
    # The InifileInfo must NOT carry the secret value anywhere.
    assert SECRET_PKEY_VALUE not in repr(info)


def test_validate_inifile_detects_sibling_pkey_file(tmp_path: Path) -> None:
    path = _write_inifile(tmp_path, with_pkey=False)
    (tmp_path / "instkey.pkey").write_text(SECRET_PKEY_VALUE)
    info = validate_inifile(path)
    assert info.has_secret_pkey is True
    assert SECRET_PKEY_VALUE not in repr(info)


# --- env / argv construction --------------------------------------------------


def test_env_observer_mode_default_leaves_guiserver_on() -> None:
    env = build_sapinst_env("/x/inifile.params", "NW_ABAP_SYSTEM_COPY")
    assert env["SAPINST_INPUT_PARAMETERS_URL"] == "/x/inifile.params"
    assert env["SAPINST_EXECUTE_PRODUCT_ID"] == "NW_ABAP_SYSTEM_COPY"
    assert env["SAPINST_SKIP_DIALOGS"] == "true"
    # Observer mode: GUI server stays ON => the var is NOT set to false.
    assert "SAPINST_START_GUISERVER" not in env
    # Never skip error steps.
    assert "SAPINST_SKIP_ERRORSTEP" not in env


def test_env_can_disable_guiserver_explicitly() -> None:
    env = build_sapinst_env("/x/inifile.params", "PID", start_guiserver=False)
    assert env["SAPINST_START_GUISERVER"] == "false"


def test_build_plan_display_is_secret_free() -> None:
    plan = build_plan(
        sapinst_path="/usr/sap/SWPM/sapinst",
        inifile_path="/x/inifile.params",
        product_id="NW_ABAP_SYSTEM_COPY",
        start_guiserver=True,
    )
    assert plan.argv == ["/usr/sap/SWPM/sapinst"]
    assert "observer-mode GUI ON" in plan.display
    assert SECRET_PKEY_VALUE not in plan.display


# --- progress parsing ---------------------------------------------------------


def test_parse_waiting_for_input() -> None:
    report = parse_progress("INFO: waiting for input from the sapinst GUI")
    assert report.state is RunState.WAITING_FOR_INPUT


def test_parse_error_pauses() -> None:
    report = parse_progress("ERROR: phase Preprocessing failed unexpectedly")
    assert report.state is RunState.ERROR
    assert "ERROR" in report.detail


def test_parse_done() -> None:
    report = parse_progress("INFO: Execution of SWPM has completed successfully.")
    assert report.state is RunState.DONE


def test_parse_running_default() -> None:
    report = parse_progress("INFO: executing phase: Import ABAP")
    assert report.state is RunState.RUNNING
    assert "Import ABAP" in report.phase


def test_parse_wait_takes_precedence_over_done() -> None:
    text = "INFO: has been completed successfully\nINFO: waiting for input"
    assert parse_progress(text).state is RunState.WAITING_FOR_INPUT


# --- dry-run: no side effects -------------------------------------------------


def test_dry_run_runs_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path, with_pkey=True)
    ctx = _ctx(
        runner,
        params={"inifile": path, "product_id": "NW_ABAP_SYSTEM_COPY"},
    )
    result = SwpmSystemCopyAction().dry_run(ctx)
    assert result.status is Status.PASS
    assert runner.calls == []  # NOTHING executed
    # Observer mode is default => GUI URL surfaced.
    assert result.data["observer_mode"] is True
    assert str(GUI_SERVER_PORT) in result.data["gui_url"]
    assert result.data["instkey_pkey_present"] is True


def test_dry_run_env_has_correct_sapinst_vars(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID_X"})
    result = SwpmSystemCopyAction().dry_run(ctx)
    env = result.data["env"]
    assert env["SAPINST_INPUT_PARAMETERS_URL"] == path
    assert env["SAPINST_EXECUTE_PRODUCT_ID"] == "PID_X"
    assert env["SAPINST_SKIP_DIALOGS"] == "true"
    assert "SAPINST_START_GUISERVER" not in env  # observer mode


def test_dry_run_missing_inifile_fails_cleanly(tmp_path: Path) -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"inifile": "/no/such/file", "product_id": "PID"})
    result = SwpmSystemCopyAction().dry_run(ctx)
    assert result.status is Status.FAIL
    assert "not found" in result.summary
    assert runner.calls == []


def test_dry_run_missing_product_id_fails(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path})
    result = SwpmSystemCopyAction().dry_run(ctx)
    assert result.status is Status.FAIL
    assert "product_id" in result.summary
    assert runner.calls == []


# --- guarded flow -------------------------------------------------------------


def test_guarded_flow_dry_run_default_does_not_execute(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID"})
    assert ctx.dry_run is True
    results = SwpmSystemCopyAction().run_guarded(ctx)
    assert len(results) == 1
    assert results[0].name.endswith(".dry-run")
    assert runner.calls == []


def test_guarded_flow_execute_without_yes_awaits_confirmation(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID"}, dry_run=False)
    results = SwpmSystemCopyAction().run_guarded(ctx)
    assert results[-1].status is Status.SKIP
    assert "confirmation" in results[-1].summary
    assert runner.calls == []


# --- execute: launch + monitor ------------------------------------------------


def test_execute_builds_env_argv_and_launches_detached(tmp_path: Path) -> None:
    runner = FakeRunner(exit_code=0, stdout="INFO: executing phase: Import ABAP")
    path = _write_inifile(tmp_path, with_pkey=True)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID_X"}, assume_yes=True)
    result = SwpmSystemCopyAction().execute(ctx)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    # Detached launch strategy: setsid + nohup + env.
    assert argv[:3] == ["setsid", "nohup", "env"]
    assert "SAPINST_EXECUTE_PRODUCT_ID=PID_X" in argv
    assert "SAPINST_SKIP_DIALOGS=true" in argv
    assert argv[-1].endswith("sapinst")
    # Running => WARN with observer-mode GUI URL.
    assert result.status is Status.WARN
    assert str(GUI_SERVER_PORT) in result.data["gui_url"]


def test_execute_waiting_for_input_warns_with_gui_url(tmp_path: Path) -> None:
    runner = FakeRunner(exit_code=0, stdout="INFO: waiting for input")
    path = _write_inifile(tmp_path)
    ctx = _ctx(
        runner, params={"inifile": path, "product_id": "PID"}, host="sapci", assume_yes=True
    )
    result = SwpmSystemCopyAction().execute(ctx)
    assert result.status is Status.WARN
    assert result.data["state"] == RunState.WAITING_FOR_INPUT.value
    assert "sapci" in result.data["gui_url"]
    assert str(GUI_SERVER_PORT) in result.data["gui_url"]


def test_execute_launch_failure_pauses_not_kills(tmp_path: Path) -> None:
    runner = FakeRunner(exit_code=127, stderr="sapinst: not found")
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID"}, assume_yes=True)
    result = SwpmSystemCopyAction().execute(ctx)
    assert result.status is Status.FAIL
    assert "paused" in result.summary.lower()


def test_execute_missing_product_id_fails_no_run(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path)
    ctx = _ctx(runner, params={"inifile": path}, assume_yes=True)
    result = SwpmSystemCopyAction().execute(ctx)
    assert result.status is Status.FAIL
    assert runner.calls == []  # validation aborts before launch


# --- verify (monitor sub-phase) -----------------------------------------------


def test_verify_detects_error_in_log_pauses(tmp_path: Path) -> None:
    log = tmp_path / "sapinst_dev.log"
    log.write_text("INFO: started\nERROR: phase Import failed\n")
    runner = FakeRunner()
    ctx = _ctx(runner, params={"product_id": "PID", "sapinst_log": str(log)})
    result = SwpmSystemCopyAction().verify(ctx)
    assert result.status is Status.FAIL
    assert "paused" in result.summary.lower()


def test_verify_detects_success(tmp_path: Path) -> None:
    log = tmp_path / "sapinst_dev.log"
    log.write_text("INFO: Execution of SWPM has completed successfully.\n")
    runner = FakeRunner()
    ctx = _ctx(runner, params={"product_id": "PID", "sapinst_log": str(log)})
    result = SwpmSystemCopyAction().verify(ctx)
    assert result.status is Status.PASS


def test_verify_no_log_is_observer_handoff(tmp_path: Path) -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"product_id": "PID"})
    result = SwpmSystemCopyAction().verify(ctx)
    assert result.status is Status.WARN
    assert "gui_url" in result.data


# --- rollback -----------------------------------------------------------------


def test_rollback_is_documented_only(tmp_path: Path) -> None:
    ctx = _ctx(FakeRunner(), params={"product_id": "PID"})
    result = SwpmSystemCopyAction().rollback(ctx)
    assert result.status is Status.SKIP
    assert result.sap_note == "2230669"


# --- SECRET SAFETY: instkey.pkey must never leak ------------------------------


def _flatten(result_obj: object) -> str:
    """Serialise a Result fully for secret-leak scanning."""
    from exodia.core import Result

    assert isinstance(result_obj, Result)
    return result_obj.model_dump_json()


def test_secret_never_leaks_in_dry_run(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = _write_inifile(tmp_path, with_pkey=True)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID"})
    result = SwpmSystemCopyAction().dry_run(ctx)
    assert SECRET_PKEY_VALUE not in _flatten(result)


def test_secret_never_leaks_in_execute(tmp_path: Path) -> None:
    runner = FakeRunner(exit_code=0, stdout="INFO: executing phase: X")
    path = _write_inifile(tmp_path, with_pkey=True)
    ctx = _ctx(runner, params={"inifile": path, "product_id": "PID"}, assume_yes=True)
    result = SwpmSystemCopyAction().execute(ctx)
    # Not in the Result...
    assert SECRET_PKEY_VALUE not in _flatten(result)
    # ...nor in any argv the runner was asked to run.
    for argv in runner.calls:
        assert all(SECRET_PKEY_VALUE not in a for a in argv)
