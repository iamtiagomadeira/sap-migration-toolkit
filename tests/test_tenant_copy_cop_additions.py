"""Tests for the COP-derived tenant-copy additions (no real HANA):

* HANA service ports (M_SERVICES) + replication parameters (M_INIFILE_CONTENTS)
* HSR parameter config with SSL on/off, and the HANA restart action
* the Dry-Run/Mock isolation actions (users/RFCs/jobs) with backup + reverse
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.registry import registry
from exodia.core.shell import CommandResult, Runner
from exodia.modules.system_copy.tenant_copy.actions.hsr_config import (
    ConfigureHsrParametersAction,
    RestartHanaAction,
)
from exodia.modules.system_copy.tenant_copy.actions.mock_run import (
    MockIsolateRfcsAction,
    MockIsolateUsersAction,
    MockStopJobsAction,
)


class ScriptedRunner(Runner):
    """Replays a canned result per call and records the SQL/argv issued."""

    def __init__(self, exit_code: int = 0, stdout: str = "", by_sql: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._by_sql = by_sql or {}

    def run(self, argv, timeout=300, input_text=None):  # type: ignore[no-untyped-def]
        self.calls.append(argv)
        sql = argv[-1] if argv else ""
        for needle, (code, out) in self._by_sql.items():
            if needle in sql:
                return CommandResult(argv, code, out, "")
        return CommandResult(argv, self._exit_code, self._stdout, "")


def _ctx(runner: Runner, **params: object) -> Context:
    class _C(Context):
        def runner(self):  # type: ignore[override]
            return runner

    return _C(params=params)  # type: ignore[arg-type]


def _rfc_ctx(responder, **params):  # type: ignore[no-untyped-def]
    class _FakeClient:
        def __init__(self, r):
            self._r = r

        def call(self, fm, **kw):
            return self._r(fm, kw)

        def close(self):
            pass

    class _C(Context):
        def rfc_client(self, side):  # type: ignore[override]
            return _FakeClient(responder)

    return _C(params=params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Ports + replication parameters checks (hdbsql via runner)
# --------------------------------------------------------------------------- #


def _run_check(name, ctx):  # type: ignore[no-untyped-def]
    cls = registry.get_check(name)
    assert cls is not None, name
    return cls().execute(ctx)


def test_source_ports_extracted() -> None:
    # M_SERVICES rows: DATABASE, SERVICE, PORT, SQL_PORT
    out = '"HT4","indexserver","31003","31015"\n"SYSTEMDB","nameserver","31001","31013"'
    ctx = _ctx(ScriptedRunner(stdout=out), source_userstore_key="SRC")
    r = _run_check("tenant-copy.hana.source-ports", ctx)
    assert r.status is Status.PASS
    assert r.data["services"]
    assert "31015" in r.facts["SQL Ports"] or "31013" in r.facts["SQL Ports"]


def test_target_ports_read_failure_fails() -> None:
    ctx = _ctx(ScriptedRunner(exit_code=1, stdout=""), target_userstore_key="TGT")
    r = _run_check("tenant-copy.hana.target-ports", ctx)
    assert r.status is Status.FAIL


def test_source_replication_parameters_captured() -> None:
    out = (
        '"global.ini","system_replication_communication","enable_ssl","off"\n'
        '"global.ini","communication","ssl","off"\n'
        '"global.ini","persistence","log_mode","normal"'
    )
    ctx = _ctx(ScriptedRunner(stdout=out), source_userstore_key="SRC")
    r = _run_check("tenant-copy.hana.source-replication-parameters", ctx)
    assert r.status is Status.PASS
    assert r.facts["SR enable_ssl"] == "off"
    assert r.facts["log_mode"] == "normal"


# --------------------------------------------------------------------------- #
# HSR parameter config — SSL on/off
# --------------------------------------------------------------------------- #


def test_hsr_config_ssl_off_statements() -> None:
    ctx = _ctx(ScriptedRunner(stdout="ok"), target_userstore_key="TGT", ssl_mode="off")
    r = ConfigureHsrParametersAction().dry_run(ctx)
    assert r.status is Status.PASS
    joined = "\n".join(r.data["statements"])
    assert "'system_replication_communication','enable_ssl') = 'off'" in joined
    assert "'communication','listeninterface') = '.internal'" in joined
    assert r.facts["SSL Mode"] == "OFF"
    assert r.facts["Restart Required"] == "Yes"


def test_hsr_config_ssl_on_statements() -> None:
    ctx = _ctx(ScriptedRunner(stdout="ok"), target_userstore_key="TGT", ssl_mode="on")
    r = ConfigureHsrParametersAction().dry_run(ctx)
    joined = "\n".join(r.data["statements"])
    assert "'system_replication_communication','enable_ssl') = 'on'" in joined
    assert "'communication','ssl') = 'on'" in joined
    assert "'communication','listeninterface') = '.global'" in joined
    assert r.facts["SSL Mode"] == "ON"


def test_hsr_config_execute_applies_each() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, target_userstore_key="TGT", ssl_mode="off")
    r = ConfigureHsrParametersAction().execute(ctx)
    assert r.status is Status.PASS
    # every statement issued via hdbsql
    assert all(call[0] == "hdbsql" for call in runner.calls)
    assert int(r.facts["Parameters Applied"]) >= 10


def test_restart_hana_stop_then_start() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner)
    r = RestartHanaAction().execute(ctx)
    assert r.status is Status.PASS
    assert runner.calls[0] == ["HDB", "stop"]
    assert runner.calls[1] == ["HDB", "start"]


def test_restart_hana_stop_failure() -> None:
    runner = ScriptedRunner(exit_code=1, stdout="")
    ctx = _ctx(runner)
    r = RestartHanaAction().execute(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Mock-run isolation (backup + reverse)
# --------------------------------------------------------------------------- #


def test_mock_isolate_users_backs_up_then_locks() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, tenant_key="TEN", abap_schema="SAPABAP1")
    r = MockIsolateUsersAction().execute(ctx)
    assert r.status is Status.PASS
    sqls = [c[-1] for c in runner.calls]
    assert any("CREATE TABLE" in s and "BKP_USR02" in s for s in sqls)
    assert any("UPDATE USR02 SET UFLAG = '65'" in s and "DDIC" in s for s in sqls)


def test_mock_isolate_users_spares_extra() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, tenant_key="TEN", keep_unlocked="JSMITH,MARY")
    r = MockIsolateUsersAction().dry_run(ctx)
    assert "JSMITH" in r.facts["Spared"] and "DDIC" in r.facts["Spared"]


def test_mock_isolate_rfcs_neutralises_prefixes() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, tenant_key="TEN")
    r = MockIsolateRfcsAction().execute(ctx)
    assert r.status is Status.PASS
    sqls = " ".join(c[-1] for c in runner.calls)
    assert "'G=', 'G=#'" in sqls and "'H=', 'H=#'" in sqls
    assert "SAPGUI_QUEUE" in sqls  # spared for X=


def test_mock_stop_jobs_sets_status_z() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, tenant_key="TEN")
    r = MockStopJobsAction().execute(ctx)
    assert r.status is Status.PASS
    sqls = " ".join(c[-1] for c in runner.calls)
    assert "UPDATE TBTCO SET STATUS = 'Z'" in sqls
    assert "RDDIMPDP%" in sqls  # spared


def test_mock_rollback_restores_from_backup() -> None:
    runner = ScriptedRunner(stdout="ok")
    ctx = _ctx(runner, tenant_key="TEN", abap_schema="SAPABAP1")
    r = MockIsolateUsersAction().rollback(ctx)
    assert r.status is Status.PASS
    sqls = " ".join(c[-1] for c in runner.calls)
    assert "TRUNCATE TABLE" in sqls and "USR02" in sqls
    assert "INSERT INTO" in sqls and "BKP_USR02" in sqls


def test_mock_actions_skip_without_tenant_key() -> None:
    ctx = _ctx(ScriptedRunner(stdout="ok"), abap_schema="SAPABAP1")
    r = MockIsolateUsersAction().execute(ctx)
    assert r.status is Status.SKIP


def test_mock_actions_are_downtime_phase_and_discovered() -> None:
    from exodia.core.result import Phase

    for name, cls in [
        ("tenant-copy.hana.mock-isolate-users", MockIsolateUsersAction),
        ("tenant-copy.hana.mock-isolate-rfcs", MockIsolateRfcsAction),
        ("tenant-copy.hana.mock-stop-jobs", MockStopJobsAction),
    ]:
        assert registry.get_action(name) is not None
        assert cls.phase is Phase.DOWNTIME
