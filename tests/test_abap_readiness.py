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
