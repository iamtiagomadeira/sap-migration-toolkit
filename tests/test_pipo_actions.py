"""Tests for the guarded Java PI/PO post-copy actions (TIA-65).

Coverage per action (SECSTORE / SLD / RFC-JCo / UME) + the orchestrator:

* discovery — every ``pipo.*`` action registers,
* metadata — destructive + ``requires_checks`` point at the right ``pipo.*`` checks,
* dry_run — runs NOTHING (the FakeRunner records zero calls),
* execute — invokes the correct AS Java tool with the expected argv,
* verify — confirms the result,
* rollback — documented-only SKIP,
* the guarded flow — a blocking precheck aborts before execute.

CRITICAL SECURITY TEST: the secure-store key phrase (and other secrets) reach
the tool ONLY via stdin (``input_text``) and NEVER appear in any recorded argv,
in any ``Result`` (summary/detail/data), or in the ``.display`` command string.
``test_secstore_key_phrase_never_leaks`` proves this exhaustively.
"""

from __future__ import annotations

import json
from pathlib import Path

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.result import Result
from exodia.core.runner import run_action
from exodia.core.shell import CommandResult, Runner
from exodia.modules.pipo.actions.postcopy import (
    FixRfcJcoAction,
    PostCopyAllAction,
    RebuildSecStoreAction,
    ReconfigureUmeAction,
    RegisterSldAction,
)

# The secret used across the secure-store tests. If this string ever shows up in
# an argv, a Result, or a log line, the leak assertions must fail.
SECRET_PHRASE = "Sup3rS3cr3t-KeyPhrase!"  # noqa: S105 - test fixture, not a real credential


class RecordingRunner(Runner):
    """Runner stand-in that records every argv AND input_text it is handed.

    Replays a canned CommandResult (optionally keyed by an argv substring) so a
    single action's execute+verify can return different output per command.
    """

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        by_arg: dict[str, tuple[int, str, str]] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._by_arg = by_arg or {}

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        self.inputs.append(input_text)
        for needle, (code, out, err) in self._by_arg.items():
            if any(needle in a for a in argv):
                return CommandResult(argv, code, out, err)
        return CommandResult(argv, self._exit_code, self._stdout, self._stderr)


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


def _result_blob(result: Result) -> str:
    """Serialise every operator-visible surface of a Result to one string."""
    return json.dumps(
        {
            "name": result.name,
            "summary": result.summary,
            "detail": result.detail,
            "cause": result.cause,
            "fix": result.fix,
            "data": result.data,
        },
        default=str,
    )


# --------------------------------------------------------------------------- #
# discovery + metadata
# --------------------------------------------------------------------------- #
def test_all_postcopy_actions_discovered() -> None:
    expected = {
        "pipo.rebuild-secstore",
        "pipo.register-sld",
        "pipo.fix-rfc-jco",
        "pipo.reconfigure-ume",
        "pipo.postcopy-all",
    }
    assert expected <= set(registry.actions())


def test_metadata_and_requires_checks() -> None:
    cases = {
        RebuildSecStoreAction: {"pipo.secstore-present", "pipo.as-java-up"},
        RegisterSldAction: {"pipo.sld-reachable", "pipo.as-java-up"},
        FixRfcJcoAction: {"pipo.rfc-jco-config", "pipo.as-java-up"},
        ReconfigureUmeAction: {"pipo.as-java-up", "pipo.hana-java-schema"},
    }
    for cls, checks in cases.items():
        action = cls()
        assert action.destructive is True
        assert set(action.requires_checks) == checks
        assert all(isinstance(c, str) for c in action.requires_checks)


def test_requires_checks_resolve_to_registered_checks() -> None:
    # Every referenced check must actually exist in the registry.
    for cls in (
        RebuildSecStoreAction,
        RegisterSldAction,
        FixRfcJcoAction,
        ReconfigureUmeAction,
        PostCopyAllAction,
    ):
        for name in cls().requires_checks:
            assert registry.get_check(name) is not None, f"{name} not registered"


# --------------------------------------------------------------------------- #
# 1. SECSTORE — the security-critical action
# --------------------------------------------------------------------------- #
def test_secstore_dry_run_runs_nothing() -> None:
    runner = RecordingRunner()
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})
    r = RebuildSecStoreAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []  # NOTHING executed
    # even in dry-run the phrase must not surface
    assert SECRET_PHRASE not in _result_blob(r)


def test_secstore_execute_calls_tool_and_feeds_phrase_via_stdin() -> None:
    runner = RecordingRunner(exit_code=0)
    ctx = _ctx(
        runner,
        sid="PIX",
        params={"key_phrase": SECRET_PHRASE, "secstore_tool": "/opt/secure-store.sh"},
    )
    r = RebuildSecStoreAction().execute(ctx)
    assert r.status is Status.PASS
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "/opt/secure-store.sh"
    assert "-mode" in argv and "rekey" in argv
    # the phrase went over stdin, exactly once
    assert runner.inputs == [SECRET_PHRASE]


def test_secstore_execute_without_phrase_fails_cleanly() -> None:
    runner = RecordingRunner()
    ctx = _ctx(runner, sid="PIX")  # no key phrase anywhere
    r = RebuildSecStoreAction().execute(ctx)
    assert r.status is Status.FAIL
    assert runner.calls == []  # never invoked the tool without a phrase
    assert "command line" in r.summary


def test_secstore_verify_confirms_store_opens() -> None:
    runner = RecordingRunner(exit_code=0)
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})
    r = RebuildSecStoreAction().verify(ctx)
    assert r.status is Status.PASS
    assert any("check" in a for a in runner.calls[0])
    assert runner.inputs == [SECRET_PHRASE]  # phrase via stdin for the read-only open


def test_secstore_verify_wrong_phrase_fails_enriched() -> None:
    runner = RecordingRunner(exit_code=1, stderr="could not decrypt secure store")
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})
    r = RebuildSecStoreAction().execute(ctx)  # execute returns FAIL on non-zero
    assert r.status is Status.FAIL
    # enrich runs through the guarded flow; here we assert the summary is KB-matchable
    from exodia.core.knowledge import lookup

    entry = lookup(r.summary)
    assert entry is not None and entry.sap_note == "1642148"


def test_secstore_key_phrase_via_protected_file_never_on_argv(tmp_path: Path) -> None:
    key_file = tmp_path / "keyphrase.txt"
    key_file.write_text(SECRET_PHRASE + "\n", encoding="utf-8")  # trailing newline stripped
    runner = RecordingRunner(exit_code=0)
    ctx = _ctx(runner, sid="PIX", params={"key_phrase_file": str(key_file)})
    r = RebuildSecStoreAction().execute(ctx)
    assert r.status is Status.PASS
    assert runner.inputs == [SECRET_PHRASE]  # file content read and piped via stdin
    for argv in runner.calls:
        assert SECRET_PHRASE not in " ".join(argv)


def test_secstore_key_phrase_never_leaks() -> None:
    """THE critical invariant: the key phrase never appears in argv/Result/log.

    Drives dry_run -> execute -> verify for the secure-store action and asserts
    the secret is absent from every recorded argv, every argv .display string,
    and every operator-visible Result field — while confirming it WAS delivered
    over stdin (the only permitted channel).
    """
    runner = RecordingRunner(exit_code=0)
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})
    action = RebuildSecStoreAction()

    results = [action.dry_run(ctx), action.execute(ctx), action.verify(ctx)]

    # 1. never in any recorded argv (nor its shell-quoted display form)
    for argv in runner.calls:
        joined = " ".join(argv)
        assert SECRET_PHRASE not in joined
        assert SECRET_PHRASE not in CommandResult(argv, 0, "", "").display

    # 2. never in any Result surface (summary/detail/cause/fix/data)
    for r in results:
        assert SECRET_PHRASE not in _result_blob(r)

    # 3. it WAS delivered over stdin for the execute + verify commands
    fed = [i for i in runner.inputs if i is not None]
    assert fed and all(i == SECRET_PHRASE for i in fed)


def test_secstore_rollback_documented_only() -> None:
    ctx = _ctx(RecordingRunner(), sid="PIX")
    r = RebuildSecStoreAction().rollback(ctx)
    assert r.status is Status.SKIP
    assert "rollback" in r.name
    assert "1642148" in r.summary


# --------------------------------------------------------------------------- #
# 2. SLD
# --------------------------------------------------------------------------- #
def test_sld_dry_run_no_exec() -> None:
    runner = RecordingRunner()
    ctx = _ctx(runner, sid="PIX", params={"sld_host": "sldtgt", "sld_port": "50000"})
    r = RegisterSldAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    assert r.data["sld_host"] == "sldtgt"


def test_sld_dry_run_missing_host_fails() -> None:
    runner = RecordingRunner()
    r = RegisterSldAction().dry_run(_ctx(runner, sid="PIX"))
    assert r.status is Status.FAIL
    assert runner.calls == []


def test_sld_execute_and_verify() -> None:
    runner = RecordingRunner(
        by_arg={"-configure": (0, "", ""), "-status": (0, "SLD host: sldtgt:50000 OK", "")}
    )
    ctx = _ctx(runner, sid="PIX", params={"sld_host": "sldtgt", "sld_port": "50000"})
    ex = RegisterSldAction().execute(ctx)
    assert ex.status is Status.PASS
    assert any("-configure" in a for a in runner.calls[0])
    vr = RegisterSldAction().verify(ctx)
    assert vr.status is Status.PASS


def test_sld_verify_missing_target_host_fails() -> None:
    runner = RecordingRunner(by_arg={"-status": (0, "SLD host: OTHER:50000", "")})
    ctx = _ctx(runner, sid="PIX", params={"sld_host": "sldtgt"})
    vr = RegisterSldAction().verify(ctx)
    assert vr.status is Status.FAIL


# --------------------------------------------------------------------------- #
# 3. RFC / JCo
# --------------------------------------------------------------------------- #
def test_rfc_execute_repoints_and_verify_flags_lingering_source() -> None:
    runner = RecordingRunner(by_arg={"-repoint": (0, "", ""), "-list": (0, "DEST -> abaptgt", "")})
    ctx = _ctx(
        runner,
        sid="PIX",
        params={"target_ashost": "abaptgt", "source_ashost": "abapsrc"},
    )
    ex = FixRfcJcoAction().execute(ctx)
    assert ex.status is Status.PASS
    assert any("-repoint" in a for a in runner.calls[0])
    vr = FixRfcJcoAction().verify(ctx)
    assert vr.status is Status.PASS


def test_rfc_verify_detects_source_host_leftover() -> None:
    runner = RecordingRunner(by_arg={"-list": (0, "DEST -> abapsrc.corp", "")})
    ctx = _ctx(runner, sid="PIX", params={"target_ashost": "abaptgt", "source_ashost": "abapsrc"})
    vr = FixRfcJcoAction().verify(ctx)
    assert vr.status is Status.FAIL


def test_rfc_dry_run_missing_target_fails() -> None:
    runner = RecordingRunner()
    r = FixRfcJcoAction().dry_run(_ctx(runner, sid="PIX"))
    assert r.status is Status.FAIL
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# 4. UME
# --------------------------------------------------------------------------- #
def test_ume_execute_and_verify() -> None:
    ds = "dataSourceConfiguration_database_only.xml"
    runner = RecordingRunner(by_arg={"-set": (0, "", ""), "-get": (0, f"value={ds}", "")})
    ctx = _ctx(runner, sid="PIX", params={"ume_datasource": ds})
    ex = ReconfigureUmeAction().execute(ctx)
    assert ex.status is Status.PASS
    assert any("-set" in a for a in runner.calls[0])
    assert ex.data["schema"] == "SAPPIXDB"
    vr = ReconfigureUmeAction().verify(ctx)
    assert vr.status is Status.PASS


def test_ume_db_password_via_stdin_never_on_argv() -> None:
    db_secret = "db-pw-9000"  # noqa: S105 - test fixture
    runner = RecordingRunner(exit_code=0)
    ctx = _ctx(runner, sid="PIX", params={"db_password": db_secret})
    ex = ReconfigureUmeAction().execute(ctx)
    assert ex.status is Status.PASS
    assert runner.inputs == [db_secret]  # over stdin
    for argv in runner.calls:
        assert db_secret not in " ".join(argv)
    assert db_secret not in _result_blob(ex)


def test_ume_verify_mismatch_fails() -> None:
    runner = RecordingRunner(by_arg={"-get": (0, "value=someOtherDatasource.xml", "")})
    ctx = _ctx(runner, sid="PIX")
    vr = ReconfigureUmeAction().verify(ctx)
    assert vr.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def test_postcopy_all_dry_run_runs_nothing() -> None:
    runner = RecordingRunner()
    ctx = _ctx(
        runner,
        sid="PIX",
        params={"key_phrase": SECRET_PHRASE, "sld_host": "sldtgt", "target_ashost": "abaptgt"},
    )
    r = PostCopyAllAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert runner.calls == []
    assert r.data["steps"] == [
        "pipo.rebuild-secstore",
        "pipo.register-sld",
        "pipo.fix-rfc-jco",
        "pipo.reconfigure-ume",
    ]
    assert SECRET_PHRASE not in _result_blob(r)


def test_postcopy_all_aborts_at_first_failure() -> None:
    # SECSTORE re-key fails -> the sequence must stop before SLD/RFC/UME.
    runner = RecordingRunner(exit_code=1, stderr="key phrase does not match")
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})
    r = PostCopyAllAction().execute(ctx)
    assert r.status is Status.FAIL
    assert r.data["failed_at"] == "pipo.rebuild-secstore"
    assert r.data["completed"] == []
    # only the secstore rekey (execute) ran; no later step invoked
    assert all("-configure" not in " ".join(c) for c in runner.calls)
    assert SECRET_PHRASE not in _result_blob(r)


def test_postcopy_all_requires_union_of_checks() -> None:
    reqs = PostCopyAllAction().requires_checks
    assert set(reqs) == {
        "pipo.secstore-present",
        "pipo.as-java-up",
        "pipo.sld-reachable",
        "pipo.rfc-jco-config",
        "pipo.hana-java-schema",
    }
    # union is de-duplicated (as-java-up appears in several sub-actions)
    assert len(reqs) == len(set(reqs))


# --------------------------------------------------------------------------- #
# Guarded flow: a blocking precheck aborts before execute
# --------------------------------------------------------------------------- #
def test_guarded_flow_dry_run_default_does_not_execute() -> None:
    runner = RecordingRunner()
    ctx = _ctx(runner, sid="PIX", params={"key_phrase": SECRET_PHRASE})  # dry_run=True default
    results = RebuildSecStoreAction().run_guarded(ctx)
    assert len(results) == 1
    assert results[0].name.endswith(".dry-run")
    assert runner.calls == []


def test_guarded_flow_blocking_precheck_aborts_before_execute() -> None:
    """A failing blocking precheck must abort the action before any execution."""
    runner = RecordingRunner()  # bare runner -> secstore-present/as-java-up FAIL
    ctx = _ctx(
        runner,
        sid="PIX",
        dry_run=False,
        assume_yes=True,
        params={"key_phrase": SECRET_PHRASE},
    )
    action = RebuildSecStoreAction()
    prechecks = [
        cls() for c in action.requires_checks if (cls := registry.get_check(c)) is not None
    ]
    assert prechecks, "requires_checks should resolve to registered checks"
    results = run_action(action, prechecks, ctx)
    assert results[-1].status is Status.SKIP
    assert "pre-checks" in results[-1].summary.lower()
    # the secure-store tool was NEVER invoked (rekey argv absent)
    assert all("rekey" not in " ".join(c) for c in runner.calls)
