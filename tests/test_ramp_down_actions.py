"""Tests for the ABAP ramp-down actions (no real SAP).

Covers: discovery, the guarded flow for each action, the RFC-backed suspend /
operation-mode switch, the customer-confirmation gate on stopping application
servers, and the manual inform-customer attestation.
"""

from __future__ import annotations

from typing import Any

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult, Runner
from exodia.modules.abap.ramp_down.actions import (
    AdaptOperationModesAction,
    InformCustomerAction,
    LockBusinessUsersAction,
    StopApplicationServersAction,
    SuspendBackgroundJobsAction,
)


# --- RFC fake (for suspend / operation modes) ------------------------------- #
class FakeRfcClient:
    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self._responder = responder

    def call(self, fm: str, **kw: Any) -> dict:
        return self._responder(fm, kw)

    def close(self) -> None:
        pass


class RfcCtx(Context):
    def bind(self, responder):  # type: ignore[no-untyped-def]
        object.__setattr__(self, "_r", responder)
        return self

    def rfc_client(self, side: str) -> FakeRfcClient:
        return FakeRfcClient(self._r)  # type: ignore[attr-defined]


_SRC = {"source_ashost": "src-host", "source_client": "000"}


# --- OS runner fake (for sapcontrol) ---------------------------------------- #
class FakeRunner(Runner):
    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    def run(self, argv, timeout=300, input_text=None):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        return CommandResult(argv, self._exit_code, self._stdout, "")


def _runner_ctx(runner: Runner, *, dry_run: bool = True, assume_yes: bool = False, **params: object) -> Context:
    class _C(Context):
        def runner(self):  # type: ignore[override]
            return runner

    return _C(params=params, dry_run=dry_run, assume_yes=assume_yes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_rampdown_actions_discovered() -> None:
    actions = registry.actions()
    for name in (
        "abap.rampdown.suspend-jobs",
        "abap.rampdown.adapt-operation-modes",
        "abap.rampdown.stop-app-servers",
        "abap.rampdown.inform-customer",
    ):
        assert name in actions, f"{name} not discovered"


# --------------------------------------------------------------------------- #
# Suspend background jobs (BTCTRNS1)
# --------------------------------------------------------------------------- #


def test_suspend_jobs_execute_ok() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"SUBRC": 0})
    r = SuspendBackgroundJobsAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.facts["Scheduler"] == "Suspended"


def test_suspend_jobs_execute_subrc_fail() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"SUBRC": 8})
    r = SuspendBackgroundJobsAction().execute(ctx)
    assert r.status is Status.FAIL


def test_suspend_jobs_dry_run_skips_without_params() -> None:
    ctx = RfcCtx(params={}).bind(lambda fm, kw: {})
    r = SuspendBackgroundJobsAction().dry_run(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# Adapt operation modes (SM63)
# --------------------------------------------------------------------------- #


def test_adapt_operation_modes_switch() -> None:
    ctx = RfcCtx(params={**_SRC, "operation_mode": "RAMPDOWN"}).bind(lambda fm, kw: {"OK": "X"})
    r = AdaptOperationModesAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.facts["Active Operation Mode"] == "RAMPDOWN"


def test_adapt_operation_modes_intent_only_without_mode() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    r = AdaptOperationModesAction().execute(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# Stop application servers — customer confirmation gate
# --------------------------------------------------------------------------- #


def test_stop_app_servers_requires_customer_confirmation() -> None:
    """Without customer_confirmed, run_guarded must SKIP execute (never stop)."""
    runner = FakeRunner()
    ctx = _runner_ctx(runner, instance_number="00", dry_run=False, assume_yes=True)
    results = StopApplicationServersAction().run_guarded(ctx)
    phases = {r.name.rsplit(".", 1)[-1]: r.status for r in results}
    assert phases["execute"] is Status.SKIP
    # sapcontrol was NEVER invoked
    assert runner.calls == []


def test_stop_app_servers_runs_after_customer_confirmation() -> None:
    runner = FakeRunner(exit_code=0, stdout="GetProcessList\nname, dispstatus\nmsg, GRAY\n")
    ctx = _runner_ctx(
        runner,
        instance_number="00",
        stop_scope="system",
        customer_confirmed="true",
        dry_run=False,
        assume_yes=True,
    )
    results = StopApplicationServersAction().run_guarded(ctx)
    phases = {r.name.rsplit(".", 1)[-1]: r.status for r in results}
    assert phases["execute"] is Status.PASS
    assert phases["verify"] is Status.PASS
    # StopSystem ALL was issued
    stop_calls = [c for c in runner.calls if "StopSystem" in c]
    assert stop_calls and stop_calls[0] == [
        "sapcontrol", "-nr", "00", "-function", "StopSystem", "ALL"
    ]


def test_stop_app_servers_flag_is_set() -> None:
    assert StopApplicationServersAction.requires_customer_confirmation is True


def test_stop_app_servers_verify_warns_if_still_green() -> None:
    runner = FakeRunner(exit_code=0, stdout="msg, GREEN\n")
    ctx = _runner_ctx(runner, instance_number="00", customer_confirmed="true")
    r = StopApplicationServersAction().verify(ctx)
    assert r.status is Status.WARN


# --------------------------------------------------------------------------- #
# Inform customer — manual attestation
# --------------------------------------------------------------------------- #


def test_inform_customer_is_manual() -> None:
    assert InformCustomerAction.manual is True
    assert InformCustomerAction.destructive is False


def test_inform_customer_skips_until_attested() -> None:
    ctx = Context(params={})
    r = InformCustomerAction().execute(ctx)
    assert r.status is Status.SKIP
    assert r.facts["Attested"] == "No"


def test_inform_customer_records_attestation() -> None:
    ctx = Context(params={"attested": "true", "attested_note": "emailed ops@customer 09:00"})
    r = InformCustomerAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.facts["Attested"] == "Yes"
    assert "emailed" in r.data["note"]


def test_inform_customer_dry_run_explains_manual() -> None:
    ctx = Context(params={})
    r = InformCustomerAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert "MANUAL" in r.summary or "Manual" in r.facts.get("Type", "")


# --------------------------------------------------------------------------- #
# Phase tagging
# --------------------------------------------------------------------------- #


def test_rampdown_actions_are_ramp_down_phase() -> None:
    from exodia.core.result import Phase

    for cls in (
        SuspendBackgroundJobsAction,
        AdaptOperationModesAction,
        StopApplicationServersAction,
        InformCustomerAction,
        LockBusinessUsersAction,
    ):
        assert cls.phase is Phase.RAMP_DOWN


# --------------------------------------------------------------------------- #
# Lock business users (SU10 / BAPI_USER_LOCK)
# --------------------------------------------------------------------------- #


def test_lock_users_spares_technical_users() -> None:
    ctx = RfcCtx(params={**_SRC, "business_users": "JSMITH,DDIC,TMSADM,MARY"}).bind(
        lambda fm, kw: {"RETURN": {"TYPE": "S"}}
    )
    r = LockBusinessUsersAction().dry_run(ctx)
    assert r.status is Status.PASS
    # DDIC + TMSADM must be excluded; only JSMITH + MARY remain
    assert set(r.data["to_lock"]) == {"JSMITH", "MARY"}


def test_lock_users_execute_locks_each() -> None:
    calls: list[str] = []

    def responder(fm, kw):  # type: ignore[no-untyped-def]
        calls.append(kw.get("USERNAME", ""))
        return {"RETURN": {"TYPE": "S"}}

    ctx = RfcCtx(params={**_SRC, "business_users": "JSMITH,MARY"}).bind(responder)
    r = LockBusinessUsersAction().execute(ctx)
    assert r.status is Status.PASS
    assert calls == ["JSMITH", "MARY"]
    assert r.facts["Users Locked"] == "2"


def test_lock_users_execute_fails_on_bapi_error() -> None:
    ctx = RfcCtx(params={**_SRC, "business_users": "JSMITH"}).bind(
        lambda fm, kw: {"RETURN": {"TYPE": "E", "MESSAGE": "no such user"}}
    )
    r = LockBusinessUsersAction().execute(ctx)
    assert r.status is Status.FAIL


def test_lock_users_skips_without_users() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    r = LockBusinessUsersAction().execute(ctx)
    assert r.status is Status.SKIP


def test_lock_users_extra_keep_unlocked() -> None:
    ctx = RfcCtx(
        params={**_SRC, "business_users": "JSMITH,BATCHUSR", "keep_unlocked": "batchusr"}
    ).bind(lambda fm, kw: {"RETURN": {"TYPE": "S"}})
    r = LockBusinessUsersAction().dry_run(ctx)
    assert r.data["to_lock"] == ["JSMITH"]
