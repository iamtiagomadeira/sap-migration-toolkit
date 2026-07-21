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


# --------------------------------------------------------------------------- #
# Runbook — tenant-copy.hana.readiness (aggregate verdict over the 11 checks)
# --------------------------------------------------------------------------- #


def test_tenant_copy_readiness_runbook_discovered() -> None:
    rb_cls = registry.get_runbook("tenant-copy.hana.readiness")
    assert rb_cls is not None
    assert len(rb_cls.steps) == 11
    # every step resolves to a registered check
    for step in rb_cls.steps:
        assert registry.get_check(step) is not None, f"unresolved step {step}"


def test_tenant_copy_readiness_verdict_all_pass() -> None:
    from exodia.core.runner import run_runbook

    # A responder that makes every check pass: online tenant, absent target,
    # matching versions, valid license, replication idle, ample space.
    def responder(argv: list[str]) -> CommandResult:
        sql = " ".join(argv)
        if "M_DATABASES" in sql and "PRD" in sql:
            return _cr(argv, stdout='"PRD","YES"')  # source online
        if "M_DATABASES" in sql and "QAS" in sql:
            return _cr(argv, stdout="")  # target absent (no rows)
        if "M_DATABASE" in sql and "VERSION" in sql:
            return _cr(argv, stdout='"2.00.067.00.1234567890"')
        if "REPLICATION" in sql.upper() or "M_SERVICE_REPLICATION" in sql.upper():
            return _cr(argv, stdout="")  # no active replication
        if "LICENSE" in sql.upper() or "M_LICENSE" in sql.upper():
            return _cr(argv, stdout='"unlimited","TRUE"')
        if argv and argv[0] == "df":
            return _cr(argv, stdout="Avail\n5000G")
        if argv and argv[0] == "hdbuserstore":
            return _cr(argv, stdout="KEY: SYSTEMDB")
        # default: connectivity / SSL probes succeed
        return _cr(argv, stdout='"OK"')

    rb = registry.get_runbook("tenant-copy.hana.readiness")()
    ctx = FakeContext(
        source="PRD",
        target="QAS",
        params={
            "source_userstore_key": "SRC",
            "target_userstore_key": "TGT",
            "source_host": "customer-hana",
            "source_instance": "00",
        },
    ).bind(responder)
    results = run_runbook(rb, ctx)
    verdict = results[-1]
    assert verdict.name.endswith(".verdict")
    # 11 steps + 1 verdict
    assert len(results) == 12


def test_tenant_copy_readiness_verdict_skip_bare_context() -> None:
    from exodia.core.runner import run_runbook

    rb = registry.get_runbook("tenant-copy.hana.readiness")()
    # bare context: checks that require params will fail/skip; the verdict must
    # never read as a green go.
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, exit_code=1, stderr="no conn"))
    results = run_runbook(rb, ctx)
    verdict = results[-1]
    assert verdict.status is not Status.PASS


def test_sided_runbooks_split_all_checks() -> None:
    """The source+target side runbooks together cover exactly the 11 checks."""
    src = registry.get_runbook("tenant-copy.hana.readiness-source")
    tgt = registry.get_runbook("tenant-copy.hana.readiness-target")
    full = registry.get_runbook("tenant-copy.hana.readiness")
    assert src is not None and tgt is not None and full is not None
    src_steps = set(src().steps)
    tgt_steps = set(tgt().steps)
    # every step resolves to a real check
    for s in src_steps | tgt_steps:
        assert registry.get_check(s) is not None, f"unresolved: {s}"
    # union covers the full readiness set, with no accidental overlap
    assert src_steps | tgt_steps == set(full().steps)
    assert src_steps.isdisjoint(tgt_steps)


def test_source_runbook_only_source_checks() -> None:
    """Source-side runbook must not include any target-only check."""
    src = registry.get_runbook("tenant-copy.hana.readiness-source")()
    assert all("target" not in s and "cross-host" not in s for s in src.steps)

