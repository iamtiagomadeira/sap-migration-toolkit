"""Tests for the guarded TenantCopyAction and its planner (TIA-71).

Focus: planner command construction, the guarded flow (dry-run default => nothing
executes), both copy methods (replication|backup), execute/verify behaviour, and
documented-only rollback.
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.runner import run_action
from exodia.core.shell import CommandResult, Runner
from exodia.modules.system_copy.tenant_copy.actions.copy_tenant import TenantCopyAction
from exodia.modules.system_copy.tenant_copy.actions.planner import (
    TenantCopyPlanError,
    build_backup_plan,
    build_replication_plan,
    source_sql_port,
)


class FakeRunner(Runner):
    """Records argv calls and replays a canned result."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        return CommandResult(argv, self._exit_code, self._stdout, self._stderr)


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #


def test_source_sql_port_derivation() -> None:
    assert source_sql_port("30") == 33013
    assert source_sql_port("00") == 30013
    assert source_sql_port(None) is None
    assert source_sql_port("bad") is None


def test_build_replication_plan_ok() -> None:
    plan = build_replication_plan(
        target_key="TGT",
        source_tenant="PRD",
        target_tenant="QAS",
        source_host="customer-hana",
        source_port=33013,
    )
    assert plan.method == "replication"
    assert len(plan.commands) == 2
    assert "AS REPLICA OF PRD AT 'customer-hana:33013'" in plan.commands[0].describe
    # SQL is passed as an argv element, never a shell string
    assert plan.commands[0].argv[0] == "hdbsql"
    assert "-U" in plan.commands[0].argv and "TGT" in plan.commands[0].argv


def test_build_replication_plan_requires_host() -> None:
    try:
        build_replication_plan(
            target_key="TGT",
            source_tenant="PRD",
            target_tenant="QAS",
            source_host=None,
            source_port=33013,
        )
    except TenantCopyPlanError as exc:
        assert "source_host" in str(exc)
    else:
        raise AssertionError("expected TenantCopyPlanError")


def test_build_plan_rejects_systemdb() -> None:
    try:
        build_replication_plan(
            target_key="TGT",
            source_tenant="SYSTEMDB",
            target_tenant="QAS",
            source_host="h",
            source_port=33013,
        )
    except TenantCopyPlanError as exc:
        assert "SYSTEMDB" in str(exc)
    else:
        raise AssertionError("expected TenantCopyPlanError")


def test_build_backup_plan_ok() -> None:
    plan = build_backup_plan(
        target_key="TGT",
        source_tenant="PRD",
        target_tenant="QAS",
        catalog_path="/backup/catalog",
        data_path="/backup/data",
        log_path="/backup/log",
    )
    assert plan.method == "backup"
    assert any("RECOVER DATABASE FOR QAS" in c.describe for c in plan.commands)


# --------------------------------------------------------------------------- #
# Discovery + metadata
# --------------------------------------------------------------------------- #


def test_action_discovered() -> None:
    assert "tenant-copy.hana.copy-tenant" in registry.actions()


def test_action_metadata() -> None:
    action = TenantCopyAction()
    assert action.destructive is True
    assert action.requires_checks
    # pre-checks must resolve to real registered checks
    assert all(registry.get_check(c) is not None for c in action.requires_checks)


# --------------------------------------------------------------------------- #
# Dry-run: no side effects
# --------------------------------------------------------------------------- #


def test_dry_run_replication_describes_without_running() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        db_type="hana",
        source="PRD",
        target="QAS",
        params={
            "source_host": "customer-hana",
            "source_instance": "30",
            "target_userstore_key": "TGT",
        },
    )
    result = TenantCopyAction().dry_run(ctx)
    assert result.status is Status.PASS
    assert runner.calls == []  # NOTHING executed
    assert result.data["method"] == "replication"
    assert result.data["source_port"] == 33013
    assert any("AS REPLICA OF PRD" in c for c in result.data["commands"])


def test_dry_run_backup_method() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        db_type="hana",
        source="PRD",
        target="QAS",
        params={
            "copy_method": "backup",
            "catalog_path": "/backup/catalog",
            "data_path": "/backup/data",
        },
    )
    result = TenantCopyAction().dry_run(ctx)
    assert result.status is Status.PASS
    assert result.data["method"] == "backup"
    assert runner.calls == []


def test_dry_run_unknown_method_fails_cleanly() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, source="PRD", target="QAS", params={"copy_method": "magic"})
    result = TenantCopyAction().dry_run(ctx)
    assert result.status is Status.FAIL
    assert "unknown copy_method" in result.summary
    assert runner.calls == []


def test_dry_run_missing_host_fails_cleanly() -> None:
    runner = FakeRunner()
    ctx = _ctx(runner, source="PRD", target="QAS", params={"source_instance": "30"})
    result = TenantCopyAction().dry_run(ctx)
    assert result.status is Status.FAIL
    assert "source_host" in result.summary
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# Guarded flow
# --------------------------------------------------------------------------- #


def test_guarded_flow_dry_run_default_does_not_execute() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        source="PRD",
        target="QAS",
        params={"source_host": "h", "source_instance": "30"},
    )
    assert ctx.dry_run is True
    results = TenantCopyAction().run_guarded(ctx)
    assert len(results) == 1
    assert results[0].name.endswith(".dry-run")
    assert runner.calls == []


def test_guarded_flow_execute_without_yes_awaits_confirmation() -> None:
    runner = FakeRunner()
    ctx = _ctx(
        runner,
        source="PRD",
        target="QAS",
        dry_run=False,
        params={"source_host": "h", "source_instance": "30"},
    )
    results = TenantCopyAction().run_guarded(ctx)
    assert results[-1].status is Status.SKIP
    assert "confirmation" in results[-1].summary
    assert runner.calls == []


def test_execute_runs_commands_then_verify_pass() -> None:
    runner = FakeRunner(exit_code=0, stdout='"QAS","YES"')
    ctx = _ctx(
        runner,
        source="PRD",
        target="QAS",
        dry_run=False,
        assume_yes=True,
        params={"source_host": "customer-hana", "source_instance": "30", "target_userstore_key": "TGT"},
    )
    results = TenantCopyAction().run_guarded(ctx)
    phases = [r.name.rsplit(".", 1)[-1] for r in results]
    assert phases == ["dry-run", "execute", "verify"]
    assert all(r.status is Status.PASS for r in results)
    # 2 replication commands on execute + 1 verify query = 3 runner calls
    assert len(runner.calls) == 3


def test_execute_step_failure_pauses() -> None:
    runner = FakeRunner(exit_code=1, stderr="database QAS already exists")
    ctx = _ctx(
        runner,
        source="PRD",
        target="QAS",
        dry_run=False,
        assume_yes=True,
        params={"source_host": "h", "source_instance": "30"},
    )
    result = TenantCopyAction().execute(ctx)
    assert result.status is Status.FAIL
    assert "PAUSED" in result.summary
    # stopped after the first failing command (no second command attempted)
    assert len(runner.calls) == 1


def test_verify_warns_when_not_online_yet() -> None:
    runner = FakeRunner(exit_code=0, stdout='"QAS","NO"')
    ctx = _ctx(runner, target="QAS", params={"target_userstore_key": "TGT"})
    result = TenantCopyAction().verify(ctx)
    assert result.status is Status.WARN


def test_rollback_documented_only() -> None:
    ctx = _ctx(FakeRunner(), target="QAS")
    result = TenantCopyAction().rollback(ctx)
    assert result.status is Status.SKIP
    assert "DROP DATABASE QAS" in result.summary


# --------------------------------------------------------------------------- #
# Reinforced verify: post-copy data-integrity comparison (source vs target)
# --------------------------------------------------------------------------- #


class ScriptedRunner(Runner):
    """Runner that replays a canned result based on a substring of the SQL argv.

    verify() issues up to three queries: the M_DATABASES online check, then a
    source M_TABLES count and a target M_TABLES count. This picks the response
    by matching against the userstore key (-U <KEY>) and/or the SQL text.
    """

    def __init__(self, by_key: dict[str, tuple[int, str]], default: tuple[int, str] = (0, "")):
        self.calls: list[list[str]] = []
        self._by_key = by_key
        self._default = default

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        key = argv[argv.index("-U") + 1] if "-U" in argv else ""
        sql = argv[-1]
        # M_DATABASES online check uses the SYSTEMDB/target key; M_TABLES counts
        # use the dedicated tenant keys. Disambiguate by SQL when keys collide.
        for needle, (code, out) in self._by_key.items():
            if needle in key or needle in sql:
                return CommandResult(argv, code, out, "")
        code, out = self._default
        return CommandResult(argv, code, out, "")


def test_verify_data_integrity_pass() -> None:
    # online YES, source and target counts match exactly
    runner = ScriptedRunner(
        by_key={
            "M_DATABASES": (0, '"QAS","YES"'),
            "SRCTEN": (0, '"1500","2000000"'),
            "TGTTEN": (0, '"1500","2000000"'),
        }
    )
    ctx = _ctx(
        runner,
        target="QAS",
        params={
            "target_userstore_key": "TGT",
            "source_tenant_key": "SRCTEN",
            "target_tenant_key": "TGTTEN",
        },
    )
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.PASS
    assert r.data["source_tables"] == 1500
    assert r.data["target_records"] == 2000000
    assert "data verified" in r.summary


def test_verify_data_integrity_within_tolerance_pass() -> None:
    # 0.5% record drift, default tolerance 1% => PASS
    runner = ScriptedRunner(
        by_key={
            "M_DATABASES": (0, '"QAS","YES"'),
            "SRCTEN": (0, '"1500","1000000"'),
            "TGTTEN": (0, '"1500","995000"'),
        }
    )
    ctx = _ctx(
        runner,
        target="QAS",
        params={
            "target_userstore_key": "TGT",
            "source_tenant_key": "SRCTEN",
            "target_tenant_key": "TGTTEN",
        },
    )
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.PASS


def test_verify_data_integrity_table_mismatch_fails() -> None:
    runner = ScriptedRunner(
        by_key={
            "M_DATABASES": (0, '"QAS","YES"'),
            "SRCTEN": (0, '"1500","2000000"'),
            "TGTTEN": (0, '"1490","2000000"'),
        }
    )
    ctx = _ctx(
        runner,
        target="QAS",
        params={
            "target_userstore_key": "TGT",
            "source_tenant_key": "SRCTEN",
            "target_tenant_key": "TGTTEN",
        },
    )
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.FAIL
    assert "table count differs" in r.summary


def test_verify_data_integrity_record_drift_fails() -> None:
    # 10% record drift, default tolerance 1% => FAIL
    runner = ScriptedRunner(
        by_key={
            "M_DATABASES": (0, '"QAS","YES"'),
            "SRCTEN": (0, '"1500","1000000"'),
            "TGTTEN": (0, '"1500","900000"'),
        }
    )
    ctx = _ctx(
        runner,
        target="QAS",
        params={
            "target_userstore_key": "TGT",
            "source_tenant_key": "SRCTEN",
            "target_tenant_key": "TGTTEN",
        },
    )
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.FAIL
    assert "drift" in r.summary


def test_verify_skips_integrity_without_tenant_keys() -> None:
    # No tenant keys => falls back to plain online verdict (backwards compatible)
    runner = FakeRunner(exit_code=0, stdout='"QAS","YES"')
    ctx = _ctx(runner, target="QAS", params={"target_userstore_key": "TGT"})
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.PASS
    assert "comparison skipped" in r.summary
    # only the online check ran — no tenant-count queries
    assert len(runner.calls) == 1


def test_verify_integrity_warns_when_counts_unreadable() -> None:
    # online YES but source count query errors => WARN, not a false PASS
    runner = ScriptedRunner(
        by_key={
            "M_DATABASES": (0, '"QAS","YES"'),
            "SRCTEN": (1, ""),
            "TGTTEN": (0, '"1500","2000000"'),
        }
    )
    ctx = _ctx(
        runner,
        target="QAS",
        params={
            "target_userstore_key": "TGT",
            "source_tenant_key": "SRCTEN",
            "target_tenant_key": "TGTTEN",
        },
    )
    r = TenantCopyAction().verify(ctx)
    assert r.status is Status.WARN


# --------------------------------------------------------------------------- #
# End-to-end via run_action: blocking precheck aborts before execute
# --------------------------------------------------------------------------- #


def test_full_run_action_prechecks_abort_before_execute() -> None:
    runner = FakeRunner()  # bare runner => prechecks fail
    ctx = _ctx(
        runner,
        source="PRD",
        target="QAS",
        params={"source_host": "h", "source_instance": "30"},
    )
    action = TenantCopyAction()
    prechecks = [
        cls() for c in action.requires_checks if (cls := registry.get_check(c)) is not None
    ]
    assert prechecks, "requires_checks should resolve to registered checks"
    results = run_action(action, prechecks, ctx)
    assert results[-1].status is Status.SKIP
    assert "pre-checks" in results[-1].summary.lower()
