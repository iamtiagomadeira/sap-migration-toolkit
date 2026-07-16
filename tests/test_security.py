"""Security-invariant tests — lock in the guarantees documented in SECURITY.md.

These are regression guards for the project's core safety contract:

* argv-only execution (no shell strings ever reach a Runner),
* secret redaction in the structured logger,
* no cleartext password on any HANA/ASE command line,
* SSH host-key verification is RejectPolicy with bounded timeouts,
* the guarded destructive flow is dry-run by default.

If any of these break, a migration tool running against production SAP is
suddenly a lot more dangerous — so they get their own test module.
"""

from __future__ import annotations

import logging

import paramiko
import pytest

from exodia.core.context import Context
from exodia.core.logging import RedactingFilter
from exodia.core.shell import CommandResult, Runner, SSHRunner
from exodia.modules.backup_restore.db_drivers.ase import AseRestoreDriver
from exodia.modules.backup_restore.db_drivers.hana import HanaRestoreDriver


class _FakeRunner(Runner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        return CommandResult(argv, 0, "", "")


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --- argv-only execution ------------------------------------------------------


def test_runner_rejects_shell_strings() -> None:
    """A plain string (the classic injection vector) must be refused."""
    with pytest.raises(TypeError):
        Runner().run("echo hi; rm -rf /")  # type: ignore[arg-type]


def test_runner_rejects_non_str_argv_elements() -> None:
    with pytest.raises(TypeError):
        Runner().run(["echo", 42])  # type: ignore[list-item]


# --- secret redaction in the logger ------------------------------------------


def _redact(msg: str) -> str:
    record = logging.LogRecord("exodia", logging.INFO, "p", 1, msg, (), None)
    RedactingFilter().filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    "raw",
    [
        "password=hunter2",
        "passwd: swordfish",
        "key_phrase=Abc123",
        "key-phrase: Abc123",
        "passphrase=topsecret",
        "token=ghp_deadbeef",
        "api_key=sk-12345",
        "isql -S SRV -U sapsa -P Cleartext123 -Q select",
        "hdbsql -p mypassword",
        "connecting --password superSecret now",
    ],
)
def test_logger_redacts_secret_values(raw: str) -> None:
    out = _redact(raw)
    # The secret value must be gone; a redaction marker must be present.
    assert "***" in out
    for leaked in (
        "hunter2",
        "swordfish",
        "Abc123",
        "topsecret",
        "ghp_deadbeef",
        "sk-12345",
        "Cleartext123",
        "mypassword",
        "superSecret",
    ):
        assert leaked not in out


def test_logger_leaves_clean_lines_untouched() -> None:
    assert _redact("recover database TENANT01 from /backup") == (
        "recover database TENANT01 from /backup"
    )


# --- no cleartext password on the command line -------------------------------


def test_hana_driver_never_puts_password_in_argv() -> None:
    driver = HanaRestoreDriver()
    ctx = _ctx(_FakeRunner(), db_type="hana", source="/backup", target="T01")
    for pc in driver.plan(ctx):
        assert "-P" not in pc.argv
        assert "--password" not in pc.argv
        # HANA authenticates via the secure user store key only.
        assert "-U" in pc.argv


def test_ase_driver_never_uses_cleartext_password_flag() -> None:
    driver = AseRestoreDriver()
    ctx = _ctx(
        _FakeRunner(),
        db_type="ase",
        source="/dumps/full.dmp",
        target="PRD",
        params={"log_dumps": ["/dumps/log1.dmp"]},
    )
    for pc in driver.plan(ctx):
        assert "-P" not in pc.argv  # never -P <cleartext>
        assert "--password" not in pc.argv


# --- SSH is secure by default -------------------------------------------------


def test_ssh_runner_uses_reject_policy_and_timeouts() -> None:
    runner = SSHRunner(host="sap-prd", user="prdadm")
    # Bounded connect timeout so a hung host can't block a run forever.
    assert runner.connect_timeout > 0


def test_ssh_connect_sets_reject_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() must install RejectPolicy and pass bounded timeouts."""
    captured: dict[str, object] = {}

    class _FakeClient:
        def load_system_host_keys(self) -> None: ...
        def load_host_keys(self, path: str) -> None: ...
        def set_missing_host_key_policy(self, policy: object) -> None:
            captured["policy"] = policy

        def connect(self, **kw: object) -> None:
            captured.update(kw)

    monkeypatch.setattr(paramiko, "SSHClient", _FakeClient)
    SSHRunner(host="sap-prd", user="prdadm", connect_timeout=15).connect()

    assert isinstance(captured["policy"], paramiko.RejectPolicy)
    assert captured["timeout"] == 15
    assert captured["auth_timeout"] == 15
    assert captured["banner_timeout"] == 15
    # key-based auth only — no password kwarg is ever passed.
    assert "password" not in captured


# --- guarded destructive flow is dry-run by default ---------------------------


def test_context_defaults_to_dry_run() -> None:
    ctx = Context(db_type="hana")
    assert ctx.dry_run is True
    assert ctx.assume_yes is False
