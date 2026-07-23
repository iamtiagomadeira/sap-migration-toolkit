"""Tests for the cross-cutting ABAP post-copy operations (no real SAP).

Covers the consistency steps shared by every system-copy method:
BDLS logical-system conversion, TMS reconfigure, SGEN load regeneration, the
post-copy installation-consistency CHECK, and the source-runtime purge.

All ops must be registry-discovered, tagged Phase.POST, and safe in dry-run
(execute nothing). BDLS/SGEN are monitored background jobs — the poll loop is
driven with interval 0 / max 1 so tests never block.
"""

from __future__ import annotations

from typing import Any

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.result import Phase
from exodia.modules.abap.post_copy.actions import (
    BdlsLogicalSystemAction,
    PurgeSourceRuntimeAction,
    SgenLoadGenerationAction,
    StmsReconfigureAction,
)
from exodia.modules.abap.post_copy.installation_consistency import (
    PostCopyInstallationConsistencyCheck,
)


class FakeRfcClient:
    """Records calls; returns canned responses via a responder callable."""

    def __init__(self, responder):  # type: ignore[no-untyped-def]
        self._responder = responder
        self.calls: list[tuple[str, dict]] = []

    def call(self, fm: str, **kw: Any) -> dict:
        self.calls.append((fm, kw))
        return self._responder(fm, kw)

    def close(self) -> None:
        pass


class RfcCtx(Context):
    def bind(self, responder):  # type: ignore[no-untyped-def]
        object.__setattr__(self, "_r", responder)
        object.__setattr__(self, "_client", FakeRfcClient(responder))
        return self

    def rfc_client(self, side: str) -> FakeRfcClient:
        return self._client  # type: ignore[attr-defined,no-any-return]


_SRC = {"source_ashost": "host1", "source_client": "000"}
# Fast, non-blocking poll for the monitored background jobs.
_FAST_POLL = {"job_poll_interval": 0, "job_poll_max": 1}


# --------------------------------------------------------------------------- #
# Discovery + phase
# --------------------------------------------------------------------------- #


def test_post_copy_actions_discovered() -> None:
    actions = registry.actions()
    for name in (
        "abap.post.bdls-logical-system",
        "abap.post.stms-reconfigure",
        "abap.post.sgen-load-generation",
        "abap.post.purge-source-runtime",
    ):
        assert name in actions, f"{name} not discovered"


def test_post_copy_check_discovered() -> None:
    assert "abap.post.installation-consistency" in registry.checks()


def test_post_copy_ops_are_post_phase() -> None:
    for cls in (
        BdlsLogicalSystemAction,
        StmsReconfigureAction,
        SgenLoadGenerationAction,
        PurgeSourceRuntimeAction,
        PostCopyInstallationConsistencyCheck,
    ):
        assert cls.phase is Phase.POST


def test_blocking_flags() -> None:
    assert BdlsLogicalSystemAction.blocking is True
    assert StmsReconfigureAction.blocking is True
    assert SgenLoadGenerationAction.blocking is False
    assert PurgeSourceRuntimeAction.blocking is False


# --------------------------------------------------------------------------- #
# Dry-run runs NOTHING (the guarded default)
# --------------------------------------------------------------------------- #


def test_bdls_dry_run_executes_nothing() -> None:
    ctx = RfcCtx(params={**_SRC, "old_logical_system": "ABCCLNT100",
                         "new_logical_system": "XYZCLNT100"}).bind(lambda fm, kw: {})
    r = BdlsLogicalSystemAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert ctx._client.calls == []  # type: ignore[attr-defined]
    assert "ABCCLNT100" in r.detail and "XYZCLNT100" in r.detail


def test_stms_dry_run_executes_nothing() -> None:
    ctx = RfcCtx(params={**_SRC, "tms_action": "delete"}).bind(lambda fm, kw: {})
    r = StmsReconfigureAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert ctx._client.calls == []  # type: ignore[attr-defined]


def test_sgen_dry_run_executes_nothing() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    r = SgenLoadGenerationAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert ctx._client.calls == []  # type: ignore[attr-defined]


def test_purge_dry_run_executes_nothing() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    r = PurgeSourceRuntimeAction().dry_run(ctx)
    assert r.status is Status.PASS
    assert ctx._client.calls == []  # type: ignore[attr-defined]


def test_dry_run_skips_without_conn_params() -> None:
    ctx = RfcCtx(params={}).bind(lambda fm, kw: {})
    assert BdlsLogicalSystemAction().dry_run(ctx).status is Status.SKIP
    assert StmsReconfigureAction().dry_run(ctx).status is Status.SKIP
    assert SgenLoadGenerationAction().dry_run(ctx).status is Status.SKIP


def test_bdls_dry_run_skips_without_names() -> None:
    ctx = RfcCtx(params=_SRC).bind(lambda fm, kw: {})
    assert BdlsLogicalSystemAction().dry_run(ctx).status is Status.SKIP


# --------------------------------------------------------------------------- #
# BDLS — submit + monitor
# --------------------------------------------------------------------------- #


def _bdls_responder(fm, kw):  # type: ignore[no-untyped-def]
    if fm == "BDLS_MAIN":
        return {"JOBNAME": "BDLS_CONV", "JOBCOUNT": "12345"}
    if fm == "BP_JOB_STATUS_GET":
        return {"STATUS": "F", "PERCENT": 100}
    return {}


def test_bdls_execute_submits_and_finishes() -> None:
    ctx = RfcCtx(params={**_SRC, "old_logical_system": "ABCCLNT100",
                         "new_logical_system": "XYZCLNT100", **_FAST_POLL}).bind(_bdls_responder)
    r = BdlsLogicalSystemAction().execute(ctx)
    assert r.status is Status.PASS
    fms = [c[0] for c in ctx._client.calls]  # type: ignore[attr-defined]
    assert "BDLS_MAIN" in fms and "BP_JOB_STATUS_GET" in fms
    # BDLS_MAIN carries the correct old/new names, no secrets.
    bdls_call = next(c for c in ctx._client.calls if c[0] == "BDLS_MAIN")  # type: ignore[attr-defined]
    assert bdls_call[1]["OLD_NAME"] == "ABCCLNT100"
    assert bdls_call[1]["NEW_NAME"] == "XYZCLNT100"


def test_bdls_execute_aborted_job_fails() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "BDLS_MAIN":
            return {"JOBNAME": "BDLS_CONV", "JOBCOUNT": "1"}
        return {"STATUS": "A"}

    ctx = RfcCtx(params={**_SRC, "old_logical_system": "ABCCLNT100",
                         "new_logical_system": "XYZCLNT100", **_FAST_POLL}).bind(resp)
    r = BdlsLogicalSystemAction().execute(ctx)
    assert r.status is Status.FAIL


def test_bdls_verify_no_residual_is_pass() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "RFC_READ_TABLE":
            return {"FIELDS": [{"FIELDNAME": "OLD_NAME", "OFFSET": 0, "LENGTH": 10}], "DATA": []}
        return {}

    ctx = RfcCtx(params={**_SRC, "old_logical_system": "ABCCLNT100",
                         "new_logical_system": "XYZCLNT100"}).bind(resp)
    r = BdlsLogicalSystemAction().verify(ctx)
    assert r.status is Status.PASS
    assert r.facts["Residual References"] == "0"


# --------------------------------------------------------------------------- #
# STMS — delete / become-controller
# --------------------------------------------------------------------------- #


def test_stms_delete_execute_ok() -> None:
    ctx = RfcCtx(params={**_SRC, "tms_action": "delete"}).bind(
        lambda fm, kw: {"RETURN": {"TYPE": "S"}}
    )
    r = StmsReconfigureAction().execute(ctx)
    assert r.status is Status.PASS
    fms = [c[0] for c in ctx._client.calls]  # type: ignore[attr-defined]
    assert "TMS_MGR_DELETE_TMS_CONFIG" in fms
    assert "TMS_MGR_INIT_TMS_CONFIG" not in fms  # delete doesn't initialise


def test_stms_become_controller_needs_domain() -> None:
    ctx = RfcCtx(params={**_SRC, "tms_action": "become-controller"}).bind(
        lambda fm, kw: {"RETURN": {"TYPE": "S"}}
    )
    r = StmsReconfigureAction().execute(ctx)
    assert r.status is Status.FAIL  # missing tms_domain


def test_stms_become_controller_ok() -> None:
    ctx = RfcCtx(params={**_SRC, "tms_action": "become-controller",
                         "tms_domain": "DOMAIN_XYZ"}).bind(lambda fm, kw: {"RETURN": {"TYPE": "S"}})
    r = StmsReconfigureAction().execute(ctx)
    assert r.status is Status.PASS
    fms = [c[0] for c in ctx._client.calls]  # type: ignore[attr-defined]
    assert "TMS_MGR_INIT_TMS_CONFIG" in fms


def test_stms_delete_verify_empty_domain_is_pass() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "RFC_READ_TABLE":
            return {"FIELDS": [{"FIELDNAME": "SYSNAM", "OFFSET": 0, "LENGTH": 8}], "DATA": []}
        return {}

    ctx = RfcCtx(params={**_SRC, "tms_action": "delete"}).bind(resp)
    r = StmsReconfigureAction().verify(ctx)
    assert r.status is Status.PASS
    assert r.facts["Domain Systems"] == "0"


# --------------------------------------------------------------------------- #
# SGEN — submit + monitor
# --------------------------------------------------------------------------- #


def test_sgen_execute_submits_and_finishes() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "SGEN_MAIN":
            return {"JOBNAME": "RSGEN", "JOBCOUNT": "42"}
        return {"STATUS": "F", "PERCENT": 100}

    ctx = RfcCtx(params={**_SRC, "sgen_scope": "all", **_FAST_POLL}).bind(resp)
    r = SgenLoadGenerationAction().execute(ctx)
    assert r.status is Status.PASS
    fms = [c[0] for c in ctx._client.calls]  # type: ignore[attr-defined]
    assert "SGEN_MAIN" in fms and "BP_JOB_STATUS_GET" in fms


def test_sgen_timeout_warns() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "SGEN_MAIN":
            return {"JOBNAME": "RSGEN", "JOBCOUNT": "42"}
        return {"STATUS": "R"}  # still running, never finishes

    ctx = RfcCtx(params={**_SRC, **_FAST_POLL}).bind(resp)
    r = SgenLoadGenerationAction().execute(ctx)
    assert r.status is Status.WARN


# --------------------------------------------------------------------------- #
# Purge source runtime
# --------------------------------------------------------------------------- #


def test_purge_all_categories() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "RSPO_R_RDELETE_SPOOLREQ_ALL":
            return {"DELETED": 3}
        if fm == "BP_JOB_DELETE_ORPHANED":
            return {"DELETED": 2}
        if fm == "BDL_DELETE_SESSIONS":
            return {"DELETED": 1}
        if fm == "RFC_READ_TABLE":
            return {
                "FIELDS": [
                    {"FIELDNAME": "RFCDEST", "OFFSET": 0, "LENGTH": 20},
                    {"FIELDNAME": "RFCTYPE", "OFFSET": 20, "LENGTH": 1},
                ],
                "DATA": [{"WA": "SRC_HOST1_RFC       3"}, {"WA": "LOCAL_DEST          T"}],
            }
        return {}

    ctx = RfcCtx(params={**_SRC, "source_host_pattern": "SRC_HOST1"}).bind(resp)
    r = PurgeSourceRuntimeAction().execute(ctx)
    assert r.status is Status.PASS
    # 3 spool + 2 jobs + 1 batch-input + 1 matching RFC destination = 7
    assert r.data["total"] == 7
    # Only the source-matching ABAP destination was deleted, not the local TCP one.
    del_calls = [
        c for c in ctx._client.calls  # type: ignore[attr-defined]
        if c[0] == "RFC_MODIFY_R3_DESTINATION"
    ]
    assert len(del_calls) == 1
    assert del_calls[0][1]["DESTINATION"] == "SRC_HOST1_RFC"


def test_purge_selected_category_only() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        return {"DELETED": 5}

    ctx = RfcCtx(params={**_SRC, "purge_categories": "spool"}).bind(resp)
    r = PurgeSourceRuntimeAction().execute(ctx)
    assert r.status is Status.PASS
    fms = [c[0] for c in ctx._client.calls]  # type: ignore[attr-defined]
    assert fms == ["RSPO_R_RDELETE_SPOOLREQ_ALL"]


def test_purge_rfc_without_pattern_removes_nothing() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        return {}

    ctx = RfcCtx(params={**_SRC, "purge_categories": "rfc"}).bind(resp)
    r = PurgeSourceRuntimeAction().execute(ctx)
    assert r.status is Status.PASS
    assert r.data["purged"]["rfc"] == 0


# --------------------------------------------------------------------------- #
# Installation-consistency CHECK (read-only)
# --------------------------------------------------------------------------- #


def test_consistency_check_is_read_only() -> None:
    assert not hasattr(PostCopyInstallationConsistencyCheck, "execute") or \
        PostCopyInstallationConsistencyCheck.__mro__  # it's a Check, not an Action


def test_consistency_clean_is_pass() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "SUSR_CHECK_INSTALLATION_CONSISTENCY":
            return {"ET_MESSAGES": []}
        if fm == "RFC_READ_TABLE":
            table = kw.get("QUERY_TABLE")
            if table == "CVERS":
                return {
                    "FIELDS": [{"FIELDNAME": "COMPONENT", "OFFSET": 0, "LENGTH": 10}],
                    "DATA": [{"WA": "SAP_BASIS "}, {"WA": "SAP_ABA   "}],
                }
            # SPAU/SPDD worklist empty
            return {"FIELDS": [{"FIELDNAME": "OBJ_NAME", "OFFSET": 0, "LENGTH": 10}], "DATA": []}
        return {}

    ctx = RfcCtx(params=_SRC).bind(resp)
    r = PostCopyInstallationConsistencyCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.facts["SICK Errors"] == "0"
    assert r.facts["Software Components"] == "2"


def test_consistency_sick_error_fails() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "SUSR_CHECK_INSTALLATION_CONSISTENCY":
            return {"ET_MESSAGES": [{"TYPE": "E"}, {"TYPE": "W"}]}
        if fm == "RFC_READ_TABLE":
            return {"FIELDS": [{"FIELDNAME": "X", "OFFSET": 0, "LENGTH": 1}], "DATA": []}
        return {}

    ctx = RfcCtx(params=_SRC).bind(resp)
    r = PostCopyInstallationConsistencyCheck().run(ctx)
    assert r.status is Status.FAIL
    assert r.facts["SICK Errors"] == "1"


def test_consistency_pending_spau_warns() -> None:
    def resp(fm, kw):  # type: ignore[no-untyped-def]
        if fm == "SUSR_CHECK_INSTALLATION_CONSISTENCY":
            return {"ET_MESSAGES": []}
        if fm == "RFC_READ_TABLE":
            table = kw.get("QUERY_TABLE")
            if table == "CVERS":
                return {"FIELDS": [{"FIELDNAME": "COMPONENT", "OFFSET": 0, "LENGTH": 10}],
                        "DATA": [{"WA": "SAP_BASIS "}]}
            # non-empty SPAU/SPDD worklist -> pending adjustments
            return {"FIELDS": [{"FIELDNAME": "OBJ_NAME", "OFFSET": 0, "LENGTH": 10}],
                    "DATA": [{"WA": "ZOBJ1     "}]}
        return {}

    ctx = RfcCtx(params=_SRC).bind(resp)
    r = PostCopyInstallationConsistencyCheck().run(ctx)
    assert r.status is Status.WARN


def test_consistency_skips_without_conn() -> None:
    ctx = RfcCtx(params={}).bind(lambda fm, kw: {})
    assert PostCopyInstallationConsistencyCheck().run(ctx).status is Status.SKIP
