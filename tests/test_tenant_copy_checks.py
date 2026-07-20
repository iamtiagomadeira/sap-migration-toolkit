"""Tests for the HANA cross-host tenant-copy prerequisite checks (TIA-71).

No real HANA is needed: a FakeRunner returns pre-fabricated CommandResults, and
is injected via a Context subclass. The responder can branch on the argv so a
single context can answer both a `df` probe and an `hdbsql` query.
"""

from __future__ import annotations

from collections.abc import Callable

from exodia.core import Context, Result, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult

# --------------------------------------------------------------------------- #
# Fake runner + context plumbing
# --------------------------------------------------------------------------- #

Responder = Callable[[list[str]], CommandResult]


class FakeRunner:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        return self._responder(argv)


class FakeContext(Context):
    def bind(self, responder: Responder) -> FakeContext:
        object.__setattr__(self, "_responder", responder)
        return self

    def runner(self):  # type: ignore[override]
        return FakeRunner(self._responder)  # type: ignore[attr-defined]


def _cr(argv: list[str], stdout: str = "", exit_code: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(argv=argv, exit_code=exit_code, stdout=stdout, stderr=stderr)


def _run_check(name: str, ctx: Context) -> Result:
    check_cls = registry.get_check(name)
    assert check_cls is not None, f"check {name} not discovered"
    return check_cls().execute(ctx)


def _is_sql(argv: list[str], needle: str) -> bool:
    """True if this is an hdbsql invocation whose statement contains needle."""
    return bool(argv) and argv[0] == "hdbsql" and any(needle in a for a in argv)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_all_tenant_copy_checks_discovered() -> None:
    checks = registry.checks()
    expected = {
        "tenant-copy.hana.source-tenant-online",
        "tenant-copy.hana.target-tenant-absent",
        "tenant-copy.hana.version-match",
        "tenant-copy.hana.source-userstore-key",
        "tenant-copy.hana.target-userstore-key",
        "tenant-copy.hana.cross-host-reachability",
        "tenant-copy.hana.target-data-space",
        "tenant-copy.hana.target-log-space",
        "tenant-copy.hana.ssl-collateral",
        "tenant-copy.hana.source-replication-status",
        "tenant-copy.hana.target-license",
    }
    assert expected <= set(checks)


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #


def test_source_tenant_online_pass() -> None:
    ctx = FakeContext(source="PRD").bind(lambda a: _cr(a, stdout='"PRD","YES"'))
    r = _run_check("tenant-copy.hana.source-tenant-online", ctx)
    assert r.status is Status.PASS
    assert r.data["tenant"] == "PRD"


def test_source_tenant_not_found_fail() -> None:
    ctx = FakeContext(source="PRD").bind(lambda a: _cr(a, stdout="0 rows selected"))
    r = _run_check("tenant-copy.hana.source-tenant-online", ctx)
    assert r.status is Status.FAIL


def test_source_tenant_offline_fail() -> None:
    ctx = FakeContext(source="PRD").bind(lambda a: _cr(a, stdout='"PRD","NO"'))
    r = _run_check("tenant-copy.hana.source-tenant-online", ctx)
    assert r.status is Status.FAIL
    assert r.data["active_status"] == "NO"


def test_source_tenant_rejects_systemdb() -> None:
    ctx = FakeContext(source="SYSTEMDB").bind(lambda a: _cr(a))
    r = _run_check("tenant-copy.hana.source-tenant-online", ctx)
    assert r.status is Status.FAIL


def test_target_tenant_absent_pass() -> None:
    ctx = FakeContext(target="QAS").bind(lambda a: _cr(a, stdout="0 rows selected"))
    r = _run_check("tenant-copy.hana.target-tenant-absent", ctx)
    assert r.status is Status.PASS


def test_target_tenant_present_fail() -> None:
    ctx = FakeContext(target="QAS").bind(lambda a: _cr(a, stdout='"QAS"'))
    r = _run_check("tenant-copy.hana.target-tenant-absent", ctx)
    assert r.status is Status.FAIL


def test_version_match_pass_explicit() -> None:
    ctx = FakeContext(
        params={"source_version": "2.00.059.09", "target_version": "2.00.067.00"}
    ).bind(lambda a: _cr(a))
    assert _run_check("tenant-copy.hana.version-match", ctx).status is Status.PASS


def test_version_match_downgrade_fail() -> None:
    ctx = FakeContext(
        params={"source_version": "2.00.067.00", "target_version": "2.00.059.09"}
    ).bind(lambda a: _cr(a))
    r = _run_check("tenant-copy.hana.version-match", ctx)
    assert r.status is Status.FAIL


def test_version_match_queries_when_absent() -> None:
    # source explicit, target queried from M_DATABASE
    ctx = FakeContext(params={"source_version": "2.00.059.09"}).bind(
        lambda a: _cr(a, stdout='"2.00.067.00.1234567890 (fa/hana2sp06)"')
    )
    assert _run_check("tenant-copy.hana.version-match", ctx).status is Status.PASS


def test_version_match_warns_when_unknown() -> None:
    ctx = FakeContext(params={"source_version": "2.00.059.09"}).bind(
        lambda a: _cr(a, exit_code=1)
    )
    assert _run_check("tenant-copy.hana.version-match", ctx).status is Status.WARN


# --------------------------------------------------------------------------- #
# Connectivity
# --------------------------------------------------------------------------- #


def test_source_userstore_key_pass() -> None:
    out = "KEY SOURCESYS\n  ENV : chost:33013\n  USER: SYSTEM"
    ctx = FakeContext(params={"source_userstore_key": "SOURCESYS"}).bind(
        lambda a: _cr(a, stdout=out)
    )
    assert _run_check("tenant-copy.hana.source-userstore-key", ctx).status is Status.PASS


def test_target_userstore_key_missing_fail() -> None:
    ctx = FakeContext(params={"target_userstore_key": "TGTKEY"}).bind(
        lambda a: _cr(a, exit_code=1, stderr="key TGTKEY not found")
    )
    r = _run_check("tenant-copy.hana.target-userstore-key", ctx)
    assert r.status is Status.FAIL


def test_cross_host_reachability_pass() -> None:
    ctx = FakeContext(
        params={"source_host": "customer-hana", "source_instance": "30"}
    ).bind(lambda a: _cr(a))  # nc -z succeeds
    r = _run_check("tenant-copy.hana.cross-host-reachability", ctx)
    assert r.status is Status.PASS
    assert r.data["port"] == 33013


def test_cross_host_reachability_fail() -> None:
    ctx = FakeContext(
        params={"source_host": "customer-hana", "source_instance": "30"}
    ).bind(lambda a: _cr(a, exit_code=1))
    r = _run_check("tenant-copy.hana.cross-host-reachability", ctx)
    assert r.status is Status.FAIL


def test_cross_host_reachability_skips_without_host() -> None:
    ctx = FakeContext(params={"source_instance": "30"}).bind(lambda a: _cr(a))
    assert (
        _run_check("tenant-copy.hana.cross-host-reachability", ctx).status is Status.SKIP
    )


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #

_DF_OUT = "Avail\n500G"


def test_target_data_space_pass() -> None:
    ctx = FakeContext(params={"source_tenant_gb": 300}).bind(
        lambda a: _cr(a, stdout=_DF_OUT)
    )
    assert _run_check("tenant-copy.hana.target-data-space", ctx).status is Status.PASS


def test_target_data_space_fail() -> None:
    ctx = FakeContext(params={"source_tenant_gb": 900}).bind(
        lambda a: _cr(a, stdout=_DF_OUT)
    )
    assert _run_check("tenant-copy.hana.target-data-space", ctx).status is Status.FAIL


def test_target_data_space_warns_without_size() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    assert _run_check("tenant-copy.hana.target-data-space", ctx).status is Status.WARN


def test_target_log_space_pass_and_fail() -> None:
    ok = FakeContext(params={"log_min_gb": 20}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    assert _run_check("tenant-copy.hana.target-log-space", ok).status is Status.PASS
    bad = FakeContext(params={"log_min_gb": 20}).bind(lambda a: _cr(a, stdout="Avail\n5G"))
    assert _run_check("tenant-copy.hana.target-log-space", bad).status is Status.FAIL


# --------------------------------------------------------------------------- #
# Preconditions / collateral
# --------------------------------------------------------------------------- #


def test_ssl_collateral_pass() -> None:
    ctx = FakeContext(params={"encrypted_link": True}).bind(lambda a: _cr(a))
    assert _run_check("tenant-copy.hana.ssl-collateral", ctx).status is Status.PASS


def test_ssl_collateral_missing_fail() -> None:
    ctx = FakeContext(params={"encrypted_link": True}).bind(lambda a: _cr(a, exit_code=1))
    assert _run_check("tenant-copy.hana.ssl-collateral", ctx).status is Status.FAIL


def test_ssl_collateral_skips_when_unencrypted() -> None:
    ctx = FakeContext(params={"encrypted_link": False}).bind(lambda a: _cr(a))
    assert _run_check("tenant-copy.hana.ssl-collateral", ctx).status is Status.SKIP


def test_source_replication_none_pass() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout="0 rows selected"))
    assert (
        _run_check("tenant-copy.hana.source-replication-status", ctx).status is Status.PASS
    )


def test_source_replication_active_warns() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"ACTIVE"'))
    r = _run_check("tenant-copy.hana.source-replication-status", ctx)
    assert r.status is Status.WARN
    assert "ACTIVE" in r.data["statuses"]


def test_target_license_valid_pass() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"unlimited","TRUE"'))
    assert _run_check("tenant-copy.hana.target-license", ctx).status is Status.PASS


def test_target_license_invalid_fail() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"unlimited","FALSE"'))
    r = _run_check("tenant-copy.hana.target-license", ctx)
    assert r.status is Status.FAIL
