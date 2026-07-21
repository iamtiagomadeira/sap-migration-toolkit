"""Tests for the ABAP post-activities actions (no real SAP).

Covers the target re-open flow: start application servers (sapcontrol), resume
the scheduler (BTCTRNS2), unlock business users, and validate online (SM51).
"""

from __future__ import annotations

from typing import Any

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.result import Phase
from exodia.core.shell import CommandResult, Runner
from exodia.modules.abap.post_activities.actions import (
    ResumeBackgroundJobsAction,
    StartApplicationServersAction,
    UnlockBusinessUsersAction,
    ValidateOnlineAction,
)


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


class FakeRunner(Runner):
    def __init__(self, exit_code: int = 0, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    def run(self, argv, timeout=300, input_text=None):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        return CommandResult(argv, self._exit_code, self._stdout, "")


def _runner_ctx(runner: Runner, **params: object) -> Context:
    class _C(Context):
        def runner(self):  # type: ignore[override]
            return runner

    return _C(params=params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Discovery + phase
# --------------------------------------------------------------------------- #


def test_post_actions_discovered() -> None:
    actions = registry.actions()
    for name in (
        "abap.post.start-app-servers",
        "abap.post.resume-jobs",
        "abap.post.unlock-users",
        "abap.post.validate-online",
    ):
        assert name in actions, f"{name} not discovered"


def test_post_actions_are_post_phase() -> None:
    for cls in (
        StartApplicationServersAction,
        ResumeBackgroundJobsAction,
        UnlockBusinessUsersAction,
        ValidateOnlineAction,
    ):
        assert cls.phase is Phase.POST


# --------------------------------------------------------------------------- #
# Start application servers
# --------------------------------------------------------------------------- #


def test_start_app_servers_issues_startsystem() -> None:
    runner = FakeRunner(exit_code=0, stdout="msg, GREEN\n")
    ctx = _runner_ctx(runner, instance_number="00", start_scope="system")
    r = StartApplicationServersAction().execute(ctx)
    assert r.status is Status.PASS
    assert runner.calls[0] == ["sapcontrol", "-nr", "00", "-function", "StartSystem", "ALL"]


def test_start_app_servers_verify_green_is_pass() -> None:
    runner = FakeRunner(exit_code=0, stdout="disp, GREEN\n")
    ctx = _runner_ctx(runner, instance_number="00")
    r = StartApplicationServersAction().verify(ctx)
    assert r.status is Status.PASS


def test_start_app_servers_verify_no_green_warns() -> None:
    runner = FakeRunner(exit_code=0, stdout="disp, GRAY\n")
    ctx = _runner_ctx(runner, instance_number="00")
    r = StartApplicationServersAction().verify(ctx)
    assert r.status is Status.WARN


# --------------------------------------------------------------------------- #
# Resume background jobs (BTCTRNS2)
# --------------------------------------------------------------------------- #


def test_resume_jobs_execute_ok() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"SUBRC": 0})
    r = ResumeBackgroundJobsAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.facts["Scheduler"] == "Resumed"


def test_resume_jobs_subrc_fail() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"SUBRC": 4})
    r = ResumeBackgroundJobsAction().execute(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Unlock business users
# --------------------------------------------------------------------------- #


def test_unlock_users_unlocks_each() -> None:
    calls: list[str] = []

    def responder(fm, kw):  # type: ignore[no-untyped-def]
        calls.append(kw.get("USERNAME", ""))
        return {"RETURN": {"TYPE": "S"}}

    ctx = RfcCtx(params={**_SRC, "business_users": "JSMITH,MARY"}).bind(responder)
    r = UnlockBusinessUsersAction().execute(ctx)
    assert r.status is Status.PASS
    assert calls == ["JSMITH", "MARY"]


def test_unlock_users_skips_without_users() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    r = UnlockBusinessUsersAction().execute(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# Validate online (SM51)
# --------------------------------------------------------------------------- #


def test_validate_online_pass_when_servers_present() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"LIST": [{"NAME": "srv1"}, {"NAME": "srv2"}]})
    r = ValidateOnlineAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.facts["App Servers Online"] == "2"


def test_validate_online_fail_when_no_servers() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {"LIST": []})
    r = ValidateOnlineAction().execute(ctx)
    assert r.status is Status.FAIL


def test_validate_online_not_destructive() -> None:
    assert ValidateOnlineAction.destructive is False
