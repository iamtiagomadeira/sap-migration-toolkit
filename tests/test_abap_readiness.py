"""Tests for the Phase-1 ABAP readiness checks (SAP MIG, RFC layer).

No real SAP is needed: a FakeRfcClient returns pre-fabricated RFC responses and
is injected via a Context subclass that exposes ``rfc_client`` — mirroring the
FakeRunner pattern used for the hdbsql-based tenant-copy checks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from exodia.core import Context, Result, Status
from exodia.core.registry import registry
from exodia.modules.abap.readiness import _rfc

# --------------------------------------------------------------------------- #
# Fake RFC client + context plumbing
# --------------------------------------------------------------------------- #

# A responder maps (function_module, kwargs) -> export/table dict.
Responder = Callable[[str, dict[str, Any]], dict[str, Any]]


class FakeRfcClient:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.closed = False

    def call(self, function_module: str, **kwargs: Any) -> dict[str, Any]:
        return self._responder(function_module, kwargs)

    def close(self) -> None:
        self.closed = True


class FakeContext(Context):
    """Context that hands checks a FakeRfcClient instead of a real pyrfc one.

    ``bind`` takes either one responder (used for both sides) or a per-side
    mapping, so a check that reads source AND target can be given different
    fixtures for each — exactly what the CVERS compare needs.
    """

    def bind(self, responder: Responder) -> FakeContext:
        object.__setattr__(self, "_responders", {"source": responder, "target": responder})
        return self

    def bind_sides(self, source: Responder, target: Responder) -> FakeContext:
        object.__setattr__(self, "_responders", {"source": source, "target": target})
        return self

    def rfc_client(self, side: str) -> FakeRfcClient:
        return FakeRfcClient(self._responders[side])  # type: ignore[attr-defined]


# Connection params good enough that has_connection_params() is True.
_SRC = {"source_ashost": "src-host", "source_client": "000"}
_SRC_TGT = {**_SRC, "target_ashost": "tgt-host", "target_client": "000"}


def _run_check(name: str, ctx: Context) -> Result:
    check_cls = registry.get_check(name)
    assert check_cls is not None, f"check {name} not discovered"
    return check_cls().execute(ctx)


def _read_table_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Build an RFC_READ_TABLE response (FIELDS + fixed-width DATA) from field dicts.

    Lays each field out in declaration order with a fixed width so the
    _rfc.read_table offset/length slicing round-trips.
    """
    if not rows:
        return {"FIELDS": [], "DATA": []}
    names = list(rows[0].keys())
    width = 30
    fields = [
        {"FIELDNAME": n, "OFFSET": i * width, "LENGTH": width}
        for i, n in enumerate(names)
    ]
    data = [
        {"WA": "".join(str(r.get(n, "")).ljust(width) for n in names)} for r in rows
    ]
    return {"FIELDS": fields, "DATA": data}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_all_abap_readiness_checks_discovered() -> None:
    checks = registry.checks()
    expected = {
        "abap.readiness.system-info",
        "abap.readiness.component-versions",
        "abap.readiness.update-queues-drained",
        "abap.readiness.app-servers",
        "abap.readiness.client-settings",
        "abap.readiness.background-jobs",
        "abap.readiness.rfc-destinations",
        "abap.readiness.transport-requests",
        "abap.readiness.lock-entries",
        "abap.readiness.active-users",
        "abap.readiness.spool-requests",
        "abap.readiness.short-dumps",
    }
    assert expected <= set(checks)


# --------------------------------------------------------------------------- #
# _rfc helpers
# --------------------------------------------------------------------------- #


def test_read_table_slices_fixed_width() -> None:
    client = FakeRfcClient(
        lambda fm, kw: _read_table_rows(
            [{"COMPONENT": "SAP_BASIS", "RELEASE": "758"}]
        )
    )
    rows = _rfc.read_table(client, "CVERS", fields=["COMPONENT", "RELEASE"])
    assert rows == [{"COMPONENT": "SAP_BASIS", "RELEASE": "758"}]


def test_conn_params_ashost_shape() -> None:
    ctx = FakeContext(params={**_SRC, "source_sysnr": "01", "rfc_user": "MIG"})
    p = _rfc.conn_params(ctx, _rfc.SOURCE)
    assert p["ashost"] == "src-host"
    assert p["sysnr"] == "01"
    assert p["client"] == "000"
    assert p["user"] == "MIG"


def test_has_connection_params() -> None:
    assert _rfc.has_connection_params(FakeContext(params=_SRC), _rfc.SOURCE)
    assert not _rfc.has_connection_params(FakeContext(params={}), _rfc.TARGET)


# --------------------------------------------------------------------------- #
# system-info
# --------------------------------------------------------------------------- #


def test_system_info_pass() -> None:
    resp = {
        "RFCSI_EXPORT": {
            "RFCSYSID": "PRD",
            "RFCHOST": "src-host",
            "RFCKERNRL": "789",
            "RFCDBSYS": "HDB",
            "RFCCHARTYP": "4103",
        }
    }
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.system-info", ctx)
    assert r.status is Status.PASS
    assert r.data["sid"] == "PRD"
    assert r.data["db_system"] == "HDB"


def test_system_info_skips_without_params() -> None:
    ctx = FakeContext(params={}).bind(lambda fm, kw: {})
    assert _run_check("abap.readiness.system-info", ctx).status is Status.SKIP


def test_system_info_fails_on_rfc_error() -> None:
    def boom(fm: str, kw: dict[str, Any]) -> dict[str, Any]:
        raise _rfc.RfcError("connect refused")

    ctx = FakeContext(params=_SRC).bind(boom)
    assert _run_check("abap.readiness.system-info", ctx).status is Status.FAIL


# --------------------------------------------------------------------------- #
# component-versions
# --------------------------------------------------------------------------- #


def _cvers(components: dict[str, tuple[str, str]]) -> dict[str, Any]:
    rows = [
        {"COMPONENT": c, "RELEASE": rel, "EXTRELEASE": sp}
        for c, (rel, sp) in components.items()
    ]
    return _read_table_rows(rows)


def test_component_versions_match_pass() -> None:
    same = {"SAP_BASIS": ("758", "0001"), "SAP_ABA": ("75I", "0001")}
    ctx = FakeContext(params=_SRC_TGT).bind(lambda fm, kw: _cvers(same))
    r = _run_check("abap.readiness.component-versions", ctx)
    assert r.status is Status.PASS
    assert r.data["source_count"] == 2


def test_component_versions_mismatch_warns() -> None:
    # source: SAP_BASIS at SP2 + extra component; target: SAP_BASIS at SP1 only.
    source = lambda fm, kw: _cvers(  # noqa: E731
        {"SAP_BASIS": ("758", "0002"), "SAP_GWFND": ("758", "0001")}
    )
    target = lambda fm, kw: _cvers({"SAP_BASIS": ("758", "0001")})  # noqa: E731
    ctx = FakeContext(params=_SRC_TGT).bind_sides(source, target)
    r = _run_check("abap.readiness.component-versions", ctx)
    assert r.status is Status.WARN
    assert "SAP_BASIS" in r.data["differing"]
    assert r.data["only_on_source"] == ["SAP_GWFND"]


def test_component_versions_inventory_without_target() -> None:
    ctx = FakeContext(params=_SRC).bind(
        lambda fm, kw: _cvers({"SAP_BASIS": ("758", "0001")})
    )
    r = _run_check("abap.readiness.component-versions", ctx)
    assert r.status is Status.PASS
    assert "source_components" in r.data


# --------------------------------------------------------------------------- #
# update-queues-drained
# --------------------------------------------------------------------------- #


def _drain_responder(updates: int, trfc: int, qout: int, qin: int) -> Responder:
    def responder(fm: str, kw: dict[str, Any]) -> dict[str, Any]:
        if fm == "RFC_READ_TABLE":
            table = kw.get("QUERY_TABLE")
            n = updates if table == "VBDATA" else trfc
            return _read_table_rows([{"X": str(i)} for i in range(n)])
        if fm == "TRFC_QOUT_LIST":
            return {"QVIEW": [{} for _ in range(qout)]}
        if fm == "TRFC_QIN_GET_CURRENT_QUEUES":
            return {"QVIEW": [{} for _ in range(qin)]}
        return {}

    return responder


def test_queues_drained_pass() -> None:
    ctx = FakeContext(params=_SRC).bind(_drain_responder(0, 0, 0, 0))
    r = _run_check("abap.readiness.update-queues-drained", ctx)
    assert r.status is Status.PASS


def test_queues_not_drained_fail() -> None:
    ctx = FakeContext(params=_SRC).bind(_drain_responder(0, 0, 3, 0))
    r = _run_check("abap.readiness.update-queues-drained", ctx)
    assert r.status is Status.FAIL
    assert r.data["qrfc_outbound"] == 3


def test_queues_check_is_blocking() -> None:
    cls = registry.get_check("abap.readiness.update-queues-drained")
    assert cls is not None
    assert cls.blocking is True


# --------------------------------------------------------------------------- #
# app-servers
# --------------------------------------------------------------------------- #


def test_app_servers_pass() -> None:
    def responder(fm: str, kw: dict[str, Any]) -> dict[str, Any]:
        if fm == "TH_SERVER_LIST":
            return {"LIST": [{"NAME": "prd_PRD_00", "HOST": "src-host", "SERVICES": "DIA"}]}
        if fm == "RFC_READ_TABLE":
            return _read_table_rows(
                [{"CLASSNAME": "SPACE", "APPLSERVER": "prd_PRD_00"}]
            )
        return {}

    ctx = FakeContext(params=_SRC).bind(responder)
    r = _run_check("abap.readiness.app-servers", ctx)
    assert r.status is Status.PASS
    assert r.data["app_server_count"] == 1
    assert r.data["logon_groups"] == ["SPACE"]


def test_app_servers_no_servers_warns() -> None:
    def responder(fm: str, kw: dict[str, Any]) -> dict[str, Any]:
        if fm == "TH_SERVER_LIST":
            return {"LIST": []}
        return _read_table_rows([])

    ctx = FakeContext(params=_SRC).bind(responder)
    assert _run_check("abap.readiness.app-servers", ctx).status is Status.WARN


# --------------------------------------------------------------------------- #
# client-settings (SCC4 / T000)
# --------------------------------------------------------------------------- #


def test_client_settings_locked_pass() -> None:
    rows = [
        {"MANDT": "100", "MTEXT": "Prod", "CCCATEGORY": "P", "CCCORACTIV": "3", "CCNOCLIIND": "3"},
        {"MANDT": "000", "MTEXT": "SAP", "CCCATEGORY": "C", "CCCORACTIV": "1", "CCNOCLIIND": "1"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.client-settings", ctx)
    assert r.status is Status.PASS
    assert r.data["productive_open_for_changes"] == []


def test_client_settings_productive_open_warns() -> None:
    rows = [
        {"MANDT": "100", "MTEXT": "Prod", "CCCATEGORY": "P", "CCCORACTIV": "1", "CCNOCLIIND": "1"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.client-settings", ctx)
    assert r.status is Status.WARN
    assert r.data["productive_open_for_changes"] == ["100"]


# --------------------------------------------------------------------------- #
# background-jobs (SM37 / TBTCO)
# --------------------------------------------------------------------------- #


def test_background_jobs_clean_pass() -> None:
    rows = [
        {"JOBNAME": "OLD_JOB", "JOBCOUNT": "1", "STATUS": "F"},
        {"JOBNAME": "CANCELLED", "JOBCOUNT": "2", "STATUS": "A"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.background-jobs", ctx)
    assert r.status is Status.PASS


def test_background_jobs_active_fails() -> None:
    rows = [{"JOBNAME": "RUNNING_NOW", "JOBCOUNT": "9", "STATUS": "R"}]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.background-jobs", ctx)
    assert r.status is Status.FAIL
    assert r.data["active_count"] == 1


def test_background_jobs_ready_warns() -> None:
    rows = [{"JOBNAME": "SCHEDULED", "JOBCOUNT": "3", "STATUS": "Y"}]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.background-jobs", ctx)
    assert r.status is Status.WARN
    assert r.data["ready_count"] == 1


def test_background_jobs_is_blocking() -> None:
    cls = registry.get_check("abap.readiness.background-jobs")
    assert cls is not None
    assert cls.blocking is True


# --------------------------------------------------------------------------- #
# rfc-destinations (SM59 / RFCDES)
# --------------------------------------------------------------------------- #


def test_rfc_destinations_inventory_pass() -> None:
    rows = [
        {"RFCDEST": "SAP_PRD_001", "RFCTYPE": "3"},
        {"RFCDEST": "EXT_PAYROLL", "RFCTYPE": "T"},
        {"RFCDEST": "LOGICAL_A", "RFCTYPE": "L"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.rfc-destinations", ctx)
    assert r.status is Status.PASS
    assert r.data["total"] == 3
    assert r.data["abap_and_external"] == 2


def test_rfc_destinations_empty_warns() -> None:
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows([]))
    r = _run_check("abap.readiness.rfc-destinations", ctx)
    assert r.status is Status.WARN


# --------------------------------------------------------------------------- #
# transport-requests (STMS / E070)
# --------------------------------------------------------------------------- #


def test_transport_requests_all_released_pass() -> None:
    rows = [
        {"TRKORR": "DEVK900001", "TRFUNCTION": "K", "TRSTATUS": "R"},
        {"TRKORR": "DEVK900002", "TRFUNCTION": "W", "TRSTATUS": "R"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.transport-requests", ctx)
    assert r.status is Status.PASS


def test_transport_requests_modifiable_warns() -> None:
    rows = [
        {"TRKORR": "DEVK900003", "TRFUNCTION": "K", "TRSTATUS": "D"},
        {"TRKORR": "DEVK900004", "TRFUNCTION": "W", "TRSTATUS": "L"},
        {"TRKORR": "DEVK900005", "TRFUNCTION": "K", "TRSTATUS": "R"},
    ]
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows(rows))
    r = _run_check("abap.readiness.transport-requests", ctx)
    assert r.status is Status.WARN
    assert r.data["modifiable_count"] == 2


# --------------------------------------------------------------------------- #
# lock-entries (SM12 / ENQUEUE_READ)
# --------------------------------------------------------------------------- #


def test_lock_entries_none_pass() -> None:
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: {"ENQ": [], "SUBRC": 0})
    r = _run_check("abap.readiness.lock-entries", ctx)
    assert r.status is Status.PASS
    assert r.data["lock_count"] == 0


def test_lock_entries_held_fails() -> None:
    resp = {"ENQ": [{"GUNAME": "USER1"}, {"GUNAME": "USER2"}], "SUBRC": 0}
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.lock-entries", ctx)
    assert r.status is Status.FAIL
    assert r.data["holders"] == ["USER1", "USER2"]


def test_lock_entries_is_blocking() -> None:
    cls = registry.get_check("abap.readiness.lock-entries")
    assert cls is not None
    assert cls.blocking is True


# --------------------------------------------------------------------------- #
# Timing is stamped (the UI-timing contract must hold for these too)
# --------------------------------------------------------------------------- #


def test_readiness_results_are_timed() -> None:
    ctx = FakeContext(params=_SRC).bind(_drain_responder(0, 0, 0, 0))
    r = _run_check("abap.readiness.update-queues-drained", ctx)
    assert r.started_at is not None
    assert r.ended_at is not None
    assert r.duration_seconds is not None and r.duration_seconds >= 0.0


# --------------------------------------------------------------------------- #
# active-users (SM04 / TH_USER_LIST)
# --------------------------------------------------------------------------- #


def test_active_users_only_technical_pass() -> None:
    resp = {"USRLIST": [{"BNAME": "DDIC"}, {"BNAME": "TMSADM"}]}
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.active-users", ctx)
    assert r.status is Status.PASS
    assert r.data["unexpected"] == []


def test_active_users_interactive_fails() -> None:
    resp = {"USRLIST": [{"BNAME": "DDIC"}, {"BNAME": "JSMITH"}, {"BNAME": "JSMITH"}]}
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.active-users", ctx)
    assert r.status is Status.FAIL
    assert r.data["unexpected"] == ["JSMITH"]


def test_active_users_allowlist_extension() -> None:
    resp = {"USRLIST": [{"BNAME": "BATCHUSR"}]}
    ctx = FakeContext(params={**_SRC, "allowed_users": "batchusr"}).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.active-users", ctx)
    assert r.status is Status.PASS


def test_active_users_is_blocking() -> None:
    cls = registry.get_check("abap.readiness.active-users")
    assert cls is not None and cls.blocking is True


# --------------------------------------------------------------------------- #
# spool-requests (SP01 / TSP01)
# --------------------------------------------------------------------------- #


def test_spool_all_completed_pass() -> None:
    rows = _read_table_rows([{"RQIDENT": "1", "RQFINAL": "Y"}, {"RQIDENT": "2", "RQFINAL": "Y"}])
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: rows)
    r = _run_check("abap.readiness.spool-requests", ctx)
    assert r.status is Status.PASS
    assert r.data["unfinished"] == 0


def test_spool_backlog_warns() -> None:
    rows = _read_table_rows([{"RQIDENT": "1", "RQFINAL": "Y"}, {"RQIDENT": "2", "RQFINAL": ""}])
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: rows)
    r = _run_check("abap.readiness.spool-requests", ctx)
    assert r.status is Status.WARN
    assert r.data["unfinished"] == 1


# --------------------------------------------------------------------------- #
# short-dumps (ST22 / SNAP)
# --------------------------------------------------------------------------- #


def test_short_dumps_none_pass() -> None:
    ctx = FakeContext(params={**_SRC, "dumps_since": "20260101"}).bind(
        lambda fm, kw: _read_table_rows([])
    )
    r = _run_check("abap.readiness.short-dumps", ctx)
    assert r.status is Status.PASS
    assert r.data["dump_count"] == 0


def test_short_dumps_present_warns() -> None:
    rows = _read_table_rows(
        [
            {"DATUM": "20260721", "UNAME": "JSMITH", "AHOST": "src"},
            {"DATUM": "20260721", "UNAME": "JSMITH", "AHOST": "src"},
            {"DATUM": "20260721", "UNAME": "BATCH", "AHOST": "src"},
        ]
    )
    ctx = FakeContext(params={**_SRC, "dumps_since": "20260101"}).bind(lambda fm, kw: rows)
    r = _run_check("abap.readiness.short-dumps", ctx)
    assert r.status is Status.WARN
    assert r.data["dump_count"] == 3
    assert r.data["by_user"]["JSMITH"] == 2


# --------------------------------------------------------------------------- #
# Runbook — abap.cutover-readiness (aggregate verdict)
# --------------------------------------------------------------------------- #


def test_cutover_runbook_discovered() -> None:
    rb_cls = registry.get_runbook("abap.cutover-readiness")
    assert rb_cls is not None
    # every step must resolve to a registered check
    for step in rb_cls.steps:
        assert registry.get_check(step) is not None, f"unresolved step {step}"


def test_cutover_runbook_verdict_skip_without_params() -> None:
    from exodia.core.runner import run_runbook

    rb = registry.get_runbook("abap.cutover-readiness")()
    ctx = FakeContext(params={}).bind(lambda fm, kw: {})
    results = run_runbook(rb, ctx)
    verdict = results[-1]
    assert verdict.name.endswith(".verdict")
    assert verdict.status is Status.SKIP


def test_runbook_aggregate_worst_status_wins() -> None:
    from exodia.core.runbook import Runbook

    pass_r = Result.ok("a", "ok")
    warn_r = Result.warn("b", "meh")
    fail_r = Result.fail("c", "bad")
    assert Runbook.aggregate([pass_r, warn_r]) is Status.WARN
    assert Runbook.aggregate([pass_r, warn_r, fail_r]) is Status.FAIL
    assert Runbook.aggregate([pass_r, pass_r]) is Status.PASS
    assert Runbook.aggregate([Result.skip("s", "skip")]) is Status.SKIP


def test_runbook_verdict_tally_counts() -> None:
    from exodia.core.runbook import Runbook

    results = [Result.ok("a", ""), Result.warn("b", ""), Result.fail("c", "")]
    v = Runbook.verdict_result("demo", results)
    assert v.status is Status.FAIL
    assert v.data["tally"] == {"pass": 1, "warn": 1, "fail": 1}
    assert v.data["blocking"] == ["c"]


def test_runbook_stop_on_blocking_halts_early() -> None:
    """A stop_on_blocking runbook stops at the first blocking step."""
    from exodia.core.runbook import Runbook
    from exodia.core.runner import run_runbook

    class _StopRunbook(Runbook):
        name = "test.stop-early"
        description = "test"
        stop_on_blocking = True
        # lock-entries is blocking and will FAIL with locks held; a step after
        # it must therefore not run.
        steps = ["abap.readiness.lock-entries", "abap.readiness.system-info"]

    resp = {"ENQ": [{"GUNAME": "U1"}], "SUBRC": 0}
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    results = run_runbook(_StopRunbook(), ctx)
    step_names = [r.name for r in results]
    assert "abap.readiness.lock-entries" in step_names
    assert "abap.readiness.system-info" not in step_names
    assert results[-1].status is Status.FAIL


# --------------------------------------------------------------------------- #
# Phase 2 — new Tier A checks
# --------------------------------------------------------------------------- #


def test_source_profiles_capture_ok() -> None:
    from exodia.core.shell import CommandResult

    class _RunnerCtx(Context):
        def runner(self):  # type: ignore[override]
            class _R:
                def run(self, argv, timeout=300, input_text=None):
                    # `ls -1 /sapmnt/PRD/profile`
                    return CommandResult(argv, 0, "DEFAULT.PFL\nPRD_D00_host\nPRD_ASCS01_host\n", "")

            return _R()

    ctx = _RunnerCtx(params={"source_sid": "PRD"})
    r = _run_check("abap.readiness.source-profiles", ctx)
    assert r.status is Status.PASS
    assert r.data["default_profile_present"] is True
    assert r.data["instance_profile_count"] == 2
    assert r.facts["DEFAULT.PFL"] == "present"


def test_source_profiles_missing_default_warns() -> None:
    from exodia.core.shell import CommandResult

    class _RunnerCtx(Context):
        def runner(self):  # type: ignore[override]
            class _R:
                def run(self, argv, timeout=300, input_text=None):
                    return CommandResult(argv, 0, "PRD_D00_host\n", "")

            return _R()

    ctx = _RunnerCtx(params={"source_sid": "PRD"})
    r = _run_check("abap.readiness.source-profiles", ctx)
    assert r.status is Status.WARN
    assert r.data["default_profile_present"] is False


def test_source_profiles_skip_without_sid() -> None:
    ctx = FakeContext(params={}).bind(lambda fm, kw: {})
    r = _run_check("abap.readiness.source-profiles", ctx)
    assert r.status is Status.SKIP


def test_target_profiles_capture_ok() -> None:
    from exodia.core.shell import CommandResult

    class _RunnerCtx(Context):
        def runner(self):  # type: ignore[override]
            class _R:
                def run(self, argv, timeout=300, input_text=None):
                    return CommandResult(argv, 0, "DEFAULT.PFL\nQAS_D00_host\n", "")

            return _R()

    ctx = _RunnerCtx(params={"target_sid": "QAS"})
    r = _run_check("abap.readiness.target-profiles", ctx)
    assert r.status is Status.PASS
    assert r.facts["Side"] == "Target"


def test_system_change_option_reads_flag() -> None:
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows([{"GLOBAL": "X"}]))
    r = _run_check("abap.readiness.system-change-option", ctx)
    assert r.status is Status.PASS
    assert r.data["modifiable"] is True
    assert r.facts["System Change Option"] == "Modifiable"


def test_system_change_option_not_modifiable() -> None:
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: _read_table_rows([{"GLOBAL": " "}]))
    r = _run_check("abap.readiness.system-change-option", ctx)
    assert r.data["modifiable"] is False


def test_batch_input_no_open_sessions_pass() -> None:
    rows = _read_table_rows([{"GROUPID": "G1", "QSTATE": "F"}, {"GROUPID": "G2", "QSTATE": "F"}])
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: rows)
    r = _run_check("abap.readiness.batch-input-sessions", ctx)
    assert r.status is Status.PASS
    assert r.data["open_sessions"] == 0


def test_batch_input_open_sessions_warn() -> None:
    rows = _read_table_rows([{"GROUPID": "G1", "QSTATE": "F"}, {"GROUPID": "G2", "QSTATE": "E"}])
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: rows)
    r = _run_check("abap.readiness.batch-input-sessions", ctx)
    assert r.status is Status.WARN
    assert r.data["open_sessions"] == 1


def test_installation_consistency_clean_pass() -> None:
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: {"ET_MESSAGES": []})
    r = _run_check("abap.readiness.installation-consistency", ctx)
    assert r.status is Status.PASS


def test_installation_consistency_errors_fail() -> None:
    resp = {"ET_MESSAGES": [{"TYPE": "E", "MESSAGE": "bad"}, {"TYPE": "W", "MESSAGE": "meh"}]}
    ctx = FakeContext(params=_SRC).bind(lambda fm, kw: resp)
    r = _run_check("abap.readiness.installation-consistency", ctx)
    assert r.status is Status.FAIL
    assert r.data["errors"]


# --------------------------------------------------------------------------- #
# Phase 2 — the full pre-migration runbook
# --------------------------------------------------------------------------- #


def test_pre_migration_runbook_discovered_and_resolves() -> None:
    rb_cls = registry.get_runbook("abap.pre-migration-checks")
    assert rb_cls is not None
    for step in rb_cls().steps:
        assert registry.get_check(step) is not None, f"unresolved step {step}"


def test_pre_migration_runbook_covers_all_phases() -> None:
    from exodia.core.result import Phase

    rb_cls = registry.get_runbook("abap.pre-migration-checks")
    phases = set()
    for step in rb_cls().steps:
        check = registry.get_check(step)
        assert check is not None
        phases.add(check.phase)
    # spans preparation, ramp-down and post-activities
    assert Phase.PREPARATION in phases
    assert Phase.RAMP_DOWN in phases
    assert Phase.POST in phases
