"""Tests for the HSR (HANA System Replication) actions and guard-rail checks.

Covers the 5 new guarded actions (enable-primary, register-secondary, takeover,
unregister-cleanup, abap-reconnect) and the 5 new checks (replication-parameters,
pki-ssfs-exchanged, sync-active-verify, sync-monitor, post-takeover-online).

Hard invariants proven here (Exodia safety contract):

* registry auto-discovers every op by name;
* dry-run runs NOTHING (the runner records zero calls) — hdbnsutil is never
  actually invoked in the default (dry-run) mode;
* every action command is argv (list[str]), never a shell string;
* NO secret (sr_password / hdbuserstore password) ever appears in argv or in a
  streamed log line — it is fed over stdin;
* the RPO=0 guard-rail (hsr.sync-active-verify) FAILs when replication is behind
  or async.

All checks/actions are exercised with a FakeRunner — no subprocess, no DB.
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult, Runner
from exodia.modules.system_copy.hsr.actions.post import (
    AbapReconnectAction,
    UnregisterCleanupAction,
)
from exodia.modules.system_copy.hsr.actions.replication import (
    EnablePrimaryAction,
    RegisterSecondaryAction,
    TakeoverAction,
)
from exodia.modules.system_copy.hsr.checks.guardrails import (
    PkiSsfsExchangedCheck,
    ReplicationParametersCheck,
    SyncActiveVerifyCheck,
)
from exodia.modules.system_copy.hsr.checks.monitoring import (
    PostTakeoverOnlineCheck,
    SyncMonitorCheck,
)


class FakeRunner(Runner):
    """Records argv calls (+ any stdin) and replays a canned result.

    ``results`` lets a test script a different CommandResult per call; otherwise
    a single canned (exit_code, stdout, stderr) is replayed for every call.
    """

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        results: list[CommandResult] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._results = list(results or [])

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        self.inputs.append(input_text)
        if self._results:
            return self._results.pop(0)
        return CommandResult(argv, self._exit_code, self._stdout, self._stderr)


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Discovery + metadata
# --------------------------------------------------------------------------- #

_HSR_ACTIONS = [
    "hsr.enable-primary",
    "hsr.register-secondary",
    "hsr.takeover",
    "hsr.unregister-cleanup",
    "hsr.abap-reconnect",
]
_HSR_NEW_CHECKS = [
    "hsr.replication-parameters",
    "hsr.pki-ssfs-exchanged",
    "hsr.sync-active-verify",
    "hsr.sync-monitor",
    "hsr.post-takeover-online",
]


def test_all_hsr_actions_discovered() -> None:
    actions = registry.actions()
    for name in _HSR_ACTIONS:
        assert name in actions, f"{name} not auto-discovered"


def test_all_hsr_new_checks_discovered() -> None:
    checks = registry.checks()
    for name in _HSR_NEW_CHECKS:
        assert name in checks, f"{name} not auto-discovered"


def test_hsr_had_zero_actions_now_five() -> None:
    hsr_actions = [n for n in registry.actions() if n.startswith("hsr.")]
    assert len(hsr_actions) == 5


def test_action_requires_checks_resolve() -> None:
    for name in _HSR_ACTIONS:
        cls = registry.get_action(name)
        assert cls is not None
        for rc in cls.requires_checks:
            assert registry.get_check(rc) is not None, f"{name} -> {rc}"


def test_takeover_guarded_by_sync_active_verify() -> None:
    # The RPO=0 guard-rail MUST gate the takeover.
    assert "hsr.sync-active-verify" in TakeoverAction.requires_checks


def test_register_requires_pki_and_params() -> None:
    reqs = RegisterSecondaryAction.requires_checks
    assert "hsr.pki-ssfs-exchanged" in reqs
    assert "hsr.replication-parameters" in reqs


# --------------------------------------------------------------------------- #
# Dry-run: nothing executes, describes the exact hdbnsutil command
# --------------------------------------------------------------------------- #


def test_enable_primary_dry_run_runs_nothing() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"site_name": "SITE_A"})
    r = EnablePrimaryAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []  # NOTHING executed
    assert r.data["command"] == ["hdbnsutil", "-sr_enable", "--name=SITE_A"]


def test_register_secondary_dry_run_runs_nothing() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        params={
            "site_name": "SITE_B",
            "remote_host": "host1",
            "remote_instance": "00",
            "replication_mode": "sync",
            "operation_mode": "logreplay",
        },
    )
    r = RegisterSecondaryAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    cmd = r.data["command"]
    assert cmd[:2] == ["hdbnsutil", "-sr_register"]
    assert "--remoteHost=host1" in cmd
    assert "--remoteInstance=00" in cmd
    assert "--replicationMode=sync" in cmd
    assert "--operationMode=logreplay" in cmd
    assert "--name=SITE_B" in cmd


def test_takeover_dry_run_runs_nothing() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner)
    r = TakeoverAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    assert r.data["command"] == ["hdbnsutil", "-sr_takeover"]


def test_unregister_cleanup_dry_run_runs_nothing() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"site_name": "SITE_B", "cleanup_mode": "unregister"})
    r = UnregisterCleanupAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    assert r.data["command"] == ["hdbnsutil", "-sr_unregister", "--name=SITE_B"]


def test_unregister_cleanup_disable_variant() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"cleanup_mode": "disable"})
    r = UnregisterCleanupAction().dry_run(ctx)
    assert r.data["command"] == ["hdbnsutil", "-sr_disable"]


def test_abap_reconnect_dry_run_runs_nothing() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"new_db_host": "host1", "instance": "00"})
    r = AbapReconnectAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    assert r.data["port"] == 30013
    assert r.data["new_db_host"] == "host1"


# --------------------------------------------------------------------------- #
# Guarded flow: default dry-run does not execute hdbnsutil
# --------------------------------------------------------------------------- #


def test_guarded_flow_default_dry_run_no_hdbnsutil() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, params={"site_name": "SITE_A"})
    assert ctx.dry_run is True
    results = EnablePrimaryAction().run_guarded(ctx)
    assert len(results) == 1
    assert results[0].name.endswith(".dry-run")
    assert runner.calls == []  # hdbnsutil never invoked in dry-run


def test_guarded_flow_execute_without_yes_awaits_confirmation() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, dry_run=False, params={"site_name": "SITE_A"})
    results = TakeoverAction().run_guarded(ctx)
    assert results[-1].status is Status.SKIP
    assert "confirmation" in results[-1].summary
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# enable-primary: precondition that it is a valid primary
# --------------------------------------------------------------------------- #


def test_enable_primary_execute_rejects_when_already_secondary() -> None:
    # -sr_state reports it is a secondary => enable must refuse.
    runner = FakeRunner(stdout="mode: syncmem\n")
    ctx = _ctx(runner, params={"site_name": "SITE_A"})
    r = EnablePrimaryAction().execute(ctx)
    assert r.status is Status.FAIL
    assert "secondary" in r.summary
    # only the -sr_state probe ran; the -sr_enable was NOT executed
    assert len(runner.calls) == 1
    assert runner.calls[0] == ["hdbnsutil", "-sr_state"]


def test_enable_primary_execute_runs_enable_when_none() -> None:
    # -sr_state probe (mode none) then the enable command.
    runner = FakeRunner(
        results=[
            CommandResult(["hdbnsutil", "-sr_state"], 0, "mode: none\n", ""),
            CommandResult(["hdbnsutil", "-sr_enable", "--name=SITE_A"], 0, "done", ""),
        ]
    )
    ctx = _ctx(runner, params={"site_name": "SITE_A"})
    r = EnablePrimaryAction().execute(ctx)
    assert r.status is Status.PASS
    assert runner.calls[-1] == ["hdbnsutil", "-sr_enable", "--name=SITE_A"]


# --------------------------------------------------------------------------- #
# register-secondary: secret goes over stdin, NEVER argv/logs
# --------------------------------------------------------------------------- #


def test_register_secret_never_in_argv_goes_over_stdin() -> None:
    secret = "S3cr3t-not-in-argv"  # nosec B105 - test literal, not a real credential
    runner = FakeRunner(stdout="registered")
    ctx = _ctx(
        runner,
        params={
            "site_name": "SITE_B",
            "remote_host": "host1",
            "remote_instance": "00",
            "sr_password": secret,
        },
    )
    r = RegisterSecondaryAction().execute(ctx)
    assert r.status is Status.PASS
    # the secret must NOT appear in any argv token
    for call in runner.calls:
        assert all(secret not in tok for tok in call)
    # it MUST have been sent over stdin
    assert any(inp and secret in inp for inp in runner.inputs)


def test_register_no_password_no_stdin() -> None:
    runner = FakeRunner(stdout="registered")
    ctx = _ctx(
        runner,
        params={"site_name": "SITE_B", "remote_host": "host1", "remote_instance": "00"},
    )
    RegisterSecondaryAction().execute(ctx)
    # no password => stdin stays None
    assert all(inp is None for inp in runner.inputs)


def test_register_streamed_log_has_no_secret() -> None:
    secret = "hunter2-secret"  # nosec B105 - test literal, not a real credential
    runner = FakeRunner(stdout="ok")
    ctx = _ctx(
        runner,
        params={
            "site_name": "SITE_B",
            "remote_host": "host1",
            "remote_instance": "00",
            "sr_password": secret,
        },
    )
    action = RegisterSecondaryAction()

    class CaptureMonitor:
        def __init__(self) -> None:
            self.logs: list[str] = []

        def start(self) -> None: ...
        def stop(self) -> None: ...
        def phase(self, name: str, detail: str = "") -> None: ...
        def progress(self, percent: float | None, detail: str = "") -> None: ...
        def log_line(self, line: str) -> None:
            self.logs.append(line)

        def result(self, result: object) -> None: ...
        def handoff(self, message: str, url: str | None = None) -> None: ...
        def __enter__(self) -> CaptureMonitor:
            return self

        def __exit__(self, *exc: object) -> None: ...

    mon = CaptureMonitor()
    action.set_monitor(mon)
    action.execute(ctx)
    assert all(secret not in line for line in mon.logs)


# --------------------------------------------------------------------------- #
# takeover
# --------------------------------------------------------------------------- #


def test_takeover_execute_runs_takeover() -> None:
    runner = FakeRunner(stdout="takeover done")
    ctx = _ctx(runner, dry_run=False, assume_yes=True, params={})
    r = TakeoverAction().execute(ctx)
    assert r.status is Status.PASS
    assert runner.calls[-1] == ["hdbnsutil", "-sr_takeover"]


def test_takeover_failure_surfaces_fail() -> None:
    runner = FakeRunner(exit_code=1, stderr="cannot take over")
    ctx = _ctx(runner, params={})
    r = TakeoverAction().execute(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# abap-reconnect: hdbuserstore password over stdin, never argv
# --------------------------------------------------------------------------- #


def test_abap_reconnect_password_over_stdin_not_argv() -> None:
    secret = "store-pass-xyz"  # nosec B105 - test literal, not a real credential
    runner = FakeRunner(stdout="ok")
    ctx = _ctx(
        runner,
        params={
            "new_db_host": "host1",
            "instance": "00",
            "userstore_password": secret,
        },
    )
    r = AbapReconnectAction().execute(ctx)
    assert r.status is Status.PASS
    for call in runner.calls:
        assert all(secret not in tok for tok in call)
    assert any(inp and secret in inp for inp in runner.inputs)
    # the hdbuserstore SET argv carries host:port + user, NOT the password
    store_calls = [c for c in runner.calls if c and c[0] == "hdbuserstore"]
    assert store_calls
    assert "host1:30013" in store_calls[0]


# --------------------------------------------------------------------------- #
# Checks: replication-parameters
# --------------------------------------------------------------------------- #


def test_replication_parameters_pass() -> None:
    stdout = '"logshipping_timeout","30"\n"operation_mode","logreplay"'
    ctx = _ctx(FakeRunner(stdout=stdout), params={"operation_mode": "logreplay"})
    r = ReplicationParametersCheck().run(ctx)
    assert r.status is Status.PASS


def test_replication_parameters_missing_key_fails() -> None:
    stdout = '"operation_mode","logreplay"'  # logshipping_timeout missing
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = ReplicationParametersCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "logshipping_timeout" in r.summary


def test_replication_parameters_mode_mismatch_fails() -> None:
    stdout = '"logshipping_timeout","30"\n"operation_mode","delta_datashipping"'
    ctx = _ctx(FakeRunner(stdout=stdout), params={"operation_mode": "logreplay"})
    r = ReplicationParametersCheck().run(ctx)
    assert r.status is Status.FAIL


def test_replication_parameters_unreadable_skips() -> None:
    ctx = _ctx(FakeRunner(exit_code=1, stderr="cannot connect"))
    r = ReplicationParametersCheck().run(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# Checks: pki-ssfs-exchanged
# --------------------------------------------------------------------------- #


def test_pki_ssfs_pass_when_both_present() -> None:
    # `test -s` returns 0 for both files.
    ctx = _ctx(FakeRunner(exit_code=0), sid="ABC")
    r = PkiSsfsExchangedCheck().run(ctx)
    assert r.status is Status.PASS


def test_pki_ssfs_fail_when_missing() -> None:
    ctx = _ctx(FakeRunner(exit_code=1), sid="ABC")
    r = PkiSsfsExchangedCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "SSFS" in r.summary


def test_pki_ssfs_skip_without_sid() -> None:
    ctx = _ctx(FakeRunner(exit_code=0))
    r = PkiSsfsExchangedCheck().run(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# Checks: sync-active-verify (RPO=0 guard-rail)
# --------------------------------------------------------------------------- #


def test_sync_active_verify_pass_when_sync_and_active() -> None:
    stdout = '"ACTIVE","SYNC","100","100"'
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = SyncActiveVerifyCheck().run(ctx)
    assert r.status is Status.PASS


def test_sync_active_verify_fail_when_not_active() -> None:
    stdout = '"SYNCING","SYNC","50","100"'
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = SyncActiveVerifyCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "LOSE DATA" in r.summary


def test_sync_active_verify_fail_when_async() -> None:
    # active but ASYNC => non-zero RPO => FAIL
    stdout = '"ACTIVE","ASYNC","100","100"'
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = SyncActiveVerifyCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "RPO" in r.summary


def test_sync_active_verify_fail_when_no_services() -> None:
    ctx = _ctx(FakeRunner(stdout=""))
    r = SyncActiveVerifyCheck().run(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Checks: sync-monitor
# --------------------------------------------------------------------------- #


def test_sync_monitor_pass_when_all_active() -> None:
    stdout = '"ACTIVE","SYNC","1000","1000"'
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = SyncMonitorCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["percent"] == 100.0


def test_sync_monitor_warns_while_syncing() -> None:
    stdout = '"INITIALIZING","SYNC","250","1000"'
    ctx = _ctx(FakeRunner(stdout=stdout))
    r = SyncMonitorCheck().run(ctx)
    assert r.status is Status.WARN
    assert r.data["percent"] == 25.0


def test_sync_monitor_non_blocking() -> None:
    assert SyncMonitorCheck.blocking is False


# --------------------------------------------------------------------------- #
# Checks: post-takeover-online
# --------------------------------------------------------------------------- #


def test_post_takeover_online_pass() -> None:
    # -sr_state says primary, then M_DATABASE says online.
    runner = FakeRunner(
        results=[
            CommandResult(["hdbnsutil", "-sr_state"], 0, "mode: primary\n", ""),
            CommandResult(["hdbsql"], 0, '"SYSTEMDB","YES"', ""),
        ]
    )
    ctx = _ctx(runner)
    r = PostTakeoverOnlineCheck().run(ctx)
    assert r.status is Status.PASS


def test_post_takeover_online_fail_when_still_secondary() -> None:
    runner = FakeRunner(
        results=[
            CommandResult(["hdbnsutil", "-sr_state"], 0, "mode: syncmem\n", ""),
            CommandResult(["hdbsql"], 0, '"SYSTEMDB","YES"', ""),
        ]
    )
    ctx = _ctx(runner)
    r = PostTakeoverOnlineCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "not 'primary'" in r.summary


def test_post_takeover_online_fail_when_not_online() -> None:
    runner = FakeRunner(
        results=[
            CommandResult(["hdbnsutil", "-sr_state"], 0, "mode: primary\n", ""),
            CommandResult(["hdbsql"], 1, "", "cannot connect"),
        ]
    )
    ctx = _ctx(runner)
    r = PostTakeoverOnlineCheck().run(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Phase tagging
# --------------------------------------------------------------------------- #


def test_phases_declared_correctly() -> None:
    from exodia.core.result import Phase

    assert EnablePrimaryAction.phase is Phase.DOWNTIME
    assert RegisterSecondaryAction.phase is Phase.DOWNTIME
    assert TakeoverAction.phase is Phase.DOWNTIME
    assert UnregisterCleanupAction.phase is Phase.POST
    assert AbapReconnectAction.phase is Phase.POST
    assert ReplicationParametersCheck.phase is Phase.PREPARATION
    assert PkiSsfsExchangedCheck.phase is Phase.PREPARATION
    assert SyncActiveVerifyCheck.phase is Phase.RAMP_DOWN
    assert SyncMonitorCheck.phase is Phase.DOWNTIME
    assert PostTakeoverOnlineCheck.phase is Phase.DOWNTIME
