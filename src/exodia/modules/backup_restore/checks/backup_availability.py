"""HANA backup availability & integrity checks (TIA-57 #1, #2, #3).

Covers the three backup-side prerequisites that must hold before a recovery:
  1. a readable data backup exists at the configured path/backint,
  2. the log backups form a continuous sequence (no gaps),
  3. the backup catalog is intact.

All logic reads from hdbsql views and/or the backup path via the injected
runner, so it is fully unit-testable with a fake runner.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class DataBackupPresentCheck(Check):
    """A complete data backup exists and is readable at the target."""

    name = "backup-restore.hana.data-backup-present"
    description = "A successful HANA data backup exists and is readable."
    blocking = True

    def run(self, ctx: Context) -> Result:
        stmt = (
            "SELECT TOP 1 BACKUP_ID, STATE_NAME, UTC_END_TIME FROM "
            "SYS.M_BACKUP_CATALOG WHERE ENTRY_TYPE_NAME = 'complete data backup' "
            "ORDER BY UTC_END_TIME DESC"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not query backup catalog for a data backup",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows or not rows[0] or not rows[0][0]:
            return Result.fail(
                self.name,
                "no data backup found in the backup catalog",
                detail=cr.stdout,
            )
        backup_id, state = rows[0][0], (rows[0][1] if len(rows[0]) > 1 else "")
        if state.strip().lower() != "successful":
            return Result.fail(
                self.name,
                f"latest data backup {backup_id} is not successful (state={state})",
                data={"backup_id": backup_id, "state": state},
            )
        return Result.ok(
            self.name,
            f"data backup {backup_id} present and successful",
            data={"backup_id": backup_id, "state": state},
        )


class LogBackupsContinuousCheck(Check):
    """Log backups form a continuous chain (no gaps in the sequence)."""

    name = "backup-restore.hana.log-backups-continuous"
    description = "Log backups are present and continuous (no gaps)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        stmt = (
            "SELECT COUNT(*), SUM(CASE WHEN STATE_NAME = 'successful' THEN 0 ELSE 1 END) "
            "FROM SYS.M_BACKUP_CATALOG WHERE ENTRY_TYPE_NAME = 'log backup'"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "cannot find log backup information in the catalog",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows or not rows[0] or not rows[0][0]:
            return Result.fail(
                self.name,
                "log backup is missing from the recovery sequence",
                detail=cr.stdout,
            )
        total = int(rows[0][0])
        failed = int(rows[0][1]) if len(rows[0]) > 1 and rows[0][1] else 0
        if total == 0:
            return Result.fail(
                self.name,
                "no log backups found — recovery could not be completed to a point in time",
                data={"total": 0},
            )
        if failed > 0:
            return Result.fail(
                self.name,
                f"{failed} log backup(s) missing or unsuccessful in the sequence",
                data={"total": total, "failed": failed},
            )
        return Result.ok(
            self.name,
            f"{total} log backups present and continuous",
            data={"total": total, "failed": 0},
        )


class BackupCatalogIntegrityCheck(Check):
    """The backup catalog itself is present and readable."""

    name = "backup-restore.hana.catalog-integrity"
    description = "Backup catalog is intact (hdbbackupdiag / backup.log)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        stmt = "SELECT COUNT(*) FROM SYS.M_BACKUP_CATALOG"
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "backup catalog not found or unreadable",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows or not rows[0] or not rows[0][0]:
            return Result.fail(
                self.name,
                "backup catalog appears empty — catalog integrity cannot be confirmed",
                detail=cr.stdout,
            )
        count = int(rows[0][0])
        if count == 0:
            return Result.fail(
                self.name,
                "backup catalog is empty; check with hdbbackupdiag / backup.log",
                data={"entries": 0},
            )
        return Result.ok(
            self.name,
            f"backup catalog intact ({count} entries)",
            data={"entries": count},
        )
