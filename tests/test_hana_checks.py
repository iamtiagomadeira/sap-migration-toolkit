"""Tests for the HANA backup/restore prerequisite checks (TIA-57/58/59).

No real HANA is needed: a FakeRunner returns pre-fabricated CommandResults keyed
by the command being run, and is injected via a Context subclass.
"""

from __future__ import annotations

from collections.abc import Callable

from exodia.core import Context, Result, Status
from exodia.core.knowledge import enrich, lookup
from exodia.core.registry import registry
from exodia.core.shell import CommandResult

# --------------------------------------------------------------------------- #
# Fake runner + context plumbing
# --------------------------------------------------------------------------- #

Responder = Callable[[list[str]], CommandResult]


class FakeRunner:
    """A Runner stand-in whose .run() consults a responder function."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        return self._responder(argv)


class FakeContext(Context):
    """Context whose runner() returns an injected FakeRunner."""

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


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_all_backup_restore_checks_discovered() -> None:
    checks = registry.checks()
    expected = {
        "backup-restore.hana.data-backup-present",
        "backup-restore.hana.log-backups-continuous",
        "backup-restore.hana.catalog-integrity",
        "backup-restore.hana.log-mode-normal",
        "backup-restore.hana.version-compatibility",
        "backup-restore.hana.backint-config",
        "backup-restore.hana.target-data-space",
        "backup-restore.hana.target-log-space",
        "backup-restore.hana.sid-instance-sanity",
        "backup-restore.hana.userstore-key",
        "backup-restore.hana.ports-available",
        "backup-restore.hana.encryption-keys",
        "backup-restore.hana.sidadm-permissions",
        "backup-restore.hana.minichecks",
    }
    assert expected <= set(checks)


# --------------------------------------------------------------------------- #
# Backup availability
# --------------------------------------------------------------------------- #


def test_data_backup_present_pass() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout='"1500000","successful","2026-07-01 10:00:00"\n1 row selected')

    ctx = FakeContext(params={"userstore_key": "SYSTEMDB"}).bind(resp)
    r = _run_check("backup-restore.hana.data-backup-present", ctx)
    assert r.status is Status.PASS
    assert r.data["backup_id"] == "1500000"


def test_data_backup_missing_fail_enriched() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout="0 rows selected")

    ctx = FakeContext(params={}).bind(resp)
    r = _run_check("backup-restore.hana.data-backup-present", ctx)
    assert r.status is Status.FAIL
    # KB enrichment: "no data backup" matches the existing catalog entry.
    assert r.sap_note == "1642148"
    assert r.fix


def test_data_backup_not_successful_fails() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout='"1500000","canceled","2026-07-01 10:00:00"')

    ctx = FakeContext(params={}).bind(resp)
    r = _run_check("backup-restore.hana.data-backup-present", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "1642148"


def test_log_backups_continuous_pass() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout='"342","0"')

    ctx = FakeContext(params={}).bind(resp)
    r = _run_check("backup-restore.hana.log-backups-continuous", ctx)
    assert r.status is Status.PASS
    assert r.data["total"] == 342


def test_log_backups_gap_fails_enriched() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout='"340","2"')

    ctx = FakeContext(params={}).bind(resp)
    r = _run_check("backup-restore.hana.log-backups-continuous", ctx)
    assert r.status is Status.FAIL
    assert r.data["failed"] == 2


def test_log_backups_none_fails() -> None:
    def resp(argv: list[str]) -> CommandResult:
        return _cr(argv, stdout='"0","0"')

    ctx = FakeContext(params={}).bind(resp)
    r = _run_check("backup-restore.hana.log-backups-continuous", ctx)
    assert r.status is Status.FAIL
    # "recovery could not be completed" matches the KB log-backup entry.
    assert r.sap_note == "1642148"


def test_catalog_integrity_pass_and_empty() -> None:
    ok_ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"512"'))
    assert _run_check("backup-restore.hana.catalog-integrity", ok_ctx).status is Status.PASS

    empty_ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"0"'))
    r = _run_check("backup-restore.hana.catalog-integrity", empty_ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "1642148"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_log_mode_normal_pass() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"normal"'))
    r = _run_check("backup-restore.hana.log-mode-normal", ctx)
    assert r.status is Status.PASS


def test_log_mode_overwrite_fail_enriched() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout='"overwrite"'))
    r = _run_check("backup-restore.hana.log-mode-normal", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "1999993"


def test_version_compatibility_pass_and_fail() -> None:
    ok = FakeContext(
        params={"source_version": "2.00.059.09", "target_version": "2.00.067.00"}
    ).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.version-compatibility", ok).status is Status.PASS

    bad = FakeContext(
        params={"source_version": "2.00.067.00", "target_version": "2.00.059.09"}
    ).bind(lambda a: _cr(a))
    r = _run_check("backup-restore.hana.version-compatibility", bad)
    assert r.status is Status.FAIL
    assert r.sap_note == "1642148"


def test_version_compatibility_queries_target_when_absent() -> None:
    ctx = FakeContext(params={"source_version": "2.00.059.09"}).bind(
        lambda a: _cr(a, stdout='"2.00.067.00.1234567890 (fa/hana2sp06)"')
    )
    r = _run_check("backup-restore.hana.version-compatibility", ctx)
    assert r.status is Status.PASS


def test_backint_config_skips_when_file_dest() -> None:
    ctx = FakeContext(params={"backup_destination": "file"}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.backint-config", ctx).status is Status.SKIP


def test_backint_config_fail_when_unconfigured() -> None:
    ctx = FakeContext(params={"backup_destination": "backint"}).bind(
        lambda a: _cr(a, stdout="0 rows selected")
    )
    r = _run_check("backup-restore.hana.backint-config", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "2031547"


def test_backint_config_pass() -> None:
    out = '"data_backup_parameter_file","/usr/sap/HDB/backint.cfg"'
    ctx = FakeContext(params={"backup_destination": "backint"}).bind(lambda a: _cr(a, stdout=out))
    assert _run_check("backup-restore.hana.backint-config", ctx).status is Status.PASS


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #

_DF_OUT = "Avail\n500G"


def test_target_data_space_pass() -> None:
    ctx = FakeContext(params={"source_data_gb": 300}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    r = _run_check("backup-restore.hana.target-data-space", ctx)
    assert r.status is Status.PASS


def test_target_data_space_fail_enriched() -> None:
    ctx = FakeContext(params={"source_data_gb": 900}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    r = _run_check("backup-restore.hana.target-data-space", ctx)
    assert r.status is Status.FAIL
    # "insufficient disk space" matches the KB space entry.
    assert r.sap_note == "1999930"


def test_target_data_space_warns_without_source_size() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    assert _run_check("backup-restore.hana.target-data-space", ctx).status is Status.WARN


def test_target_log_space_pass_and_fail() -> None:
    ok = FakeContext(params={"log_min_gb": 20}).bind(lambda a: _cr(a, stdout=_DF_OUT))
    assert _run_check("backup-restore.hana.target-log-space", ok).status is Status.PASS

    bad = FakeContext(params={"log_min_gb": 20}).bind(lambda a: _cr(a, stdout="Avail\n5G"))
    r = _run_check("backup-restore.hana.target-log-space", bad)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# Connectivity
# --------------------------------------------------------------------------- #


def test_sid_instance_sanity_pass() -> None:
    ctx = FakeContext(sid="HDB", params={"instance": "00"}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.sid-instance-sanity", ctx).status is Status.PASS


def test_sid_instance_sanity_fail() -> None:
    ctx = FakeContext(sid="TOOLONG", params={"instance": "7"}).bind(lambda a: _cr(a))
    r = _run_check("backup-restore.hana.sid-instance-sanity", ctx)
    assert r.status is Status.FAIL


def test_userstore_key_pass() -> None:
    out = "KEY SYSTEMDB\n  ENV : host:30013\n  USER: SYSTEM"
    ctx = FakeContext(params={"userstore_key": "SYSTEMDB"}).bind(lambda a: _cr(a, stdout=out))
    assert _run_check("backup-restore.hana.userstore-key", ctx).status is Status.PASS


def test_userstore_key_missing_fail_enriched() -> None:
    ctx = FakeContext(params={"userstore_key": "MIGKEY"}).bind(
        lambda a: _cr(a, exit_code=1, stderr="key MIGKEY not found")
    )
    r = _run_check("backup-restore.hana.userstore-key", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "2667632"


def test_ports_available_pass_and_warn() -> None:
    ok = FakeContext(params={"instance": "00"}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.ports-available", ok).status is Status.PASS

    warn = FakeContext(params={"instance": "00"}).bind(lambda a: _cr(a, exit_code=1))
    assert _run_check("backup-restore.hana.ports-available", warn).status is Status.WARN


def test_ports_available_skips_without_instance() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.ports-available", ctx).status is Status.SKIP


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


def test_encryption_keys_skip_when_not_encrypted() -> None:
    ctx = FakeContext(params={"backup_encrypted": False}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.encryption-keys", ctx).status is Status.SKIP


def test_encryption_keys_pass() -> None:
    out = '"PERSISTENCE","TRUE"\n"BACKUP","TRUE"'
    ctx = FakeContext(params={"backup_encrypted": True}).bind(lambda a: _cr(a, stdout=out))
    assert _run_check("backup-restore.hana.encryption-keys", ctx).status is Status.PASS


def test_encryption_keys_missing_fail_enriched() -> None:
    out = '"PERSISTENCE","TRUE"\n"BACKUP","FALSE"'
    ctx = FakeContext(params={"backup_encrypted": True}).bind(lambda a: _cr(a, stdout=out))
    r = _run_check("backup-restore.hana.encryption-keys", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "2444090"


def test_sidadm_permissions_pass() -> None:
    ctx = FakeContext(sid="HDB", params={"backup_path": "/hana/backup"}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.sidadm-permissions", ctx).status is Status.PASS


def test_sidadm_permissions_denied_fail() -> None:
    def resp(argv: list[str]) -> CommandResult:
        # `test -r` fails, `test -e` succeeds -> permission denied path.
        if "-e" in argv:
            return _cr(argv, exit_code=0)
        return _cr(argv, exit_code=1)

    ctx = FakeContext(sid="HDB", params={"backup_path": "/hana/backup"}).bind(resp)
    r = _run_check("backup-restore.hana.sidadm-permissions", ctx)
    assert r.status is Status.FAIL
    assert r.sap_note == "1642148"


def test_sidadm_permissions_missing_path_fail() -> None:
    ctx = FakeContext(sid="HDB", params={"backup_path": "/nope"}).bind(
        lambda a: _cr(a, exit_code=1)
    )
    r = _run_check("backup-restore.hana.sidadm-permissions", ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# MiniChecks (TIA-58)
# --------------------------------------------------------------------------- #


def test_minichecks_skip_without_sql() -> None:
    ctx = FakeContext(params={}).bind(lambda a: _cr(a))
    assert _run_check("backup-restore.hana.minichecks", ctx).status is Status.SKIP


def test_minichecks_pass() -> None:
    out = '"C0001","Some check","host","10","OK",""\n"C0002","Another","host","1","1",""'
    ctx = FakeContext(params={"minichecks_stmt": "SELECT * FROM DUMMY"}).bind(
        lambda a: _cr(a, stdout=out)
    )
    assert _run_check("backup-restore.hana.minichecks", ctx).status is Status.PASS


def test_minichecks_critical_warns_enriched() -> None:
    out = '"C0050","Log mode","host","overwrite","normal","X"'
    ctx = FakeContext(params={"minichecks_stmt": "SELECT * FROM DUMMY"}).bind(
        lambda a: _cr(a, stdout=out)
    )
    r = _run_check("backup-restore.hana.minichecks", ctx)
    assert r.status is Status.WARN
    assert r.data["critical_count"] == 1


# --------------------------------------------------------------------------- #
# KB (TIA-59): representative log lines match the right entry
# --------------------------------------------------------------------------- #


def test_kb_new_entries_match() -> None:
    cases = {
        "the backup catalog is empty; check with hdbbackupdiag": "1642148",
        "target revision 2.00.059 is older than source 2.00.067": "1642148",
        "hdbuserstore key 'MIGKEY' not found": "2667632",
        "backint selected but no data_backup_parameter_file configured": "2031547",
        "permission denied: HDB cannot read backup path /hana/backup": "1642148",
        "insufficient disk space for logs/traces on the target": "1999930",
        "3 potentially-critical minicheck(s) found": "1969700",
        "encryption root key backup missing for BACKUP": "2444090",
    }
    for line, note in cases.items():
        entry = lookup(line)
        assert entry is not None, f"no KB match for: {line}"
        assert entry.sap_note == note, f"{line} -> {entry.sap_note}, expected {note}"


def test_kb_enrich_roundtrip_on_new_entry() -> None:
    r = Result.fail("x", "backint selected but no log_backup_parameter_file configured")
    enrich(r)
    assert r.sap_note == "2031547"
    assert r.fix
