"""HANA restore driver — recovery via ``hdbsql`` + ``RECOVER DATABASE``.

Strategy implementation for SAP HANA. It plans and runs a data + log backup
recovery and verifies the tenant is online.

Credentials never appear on the command line: ``hdbsql`` authenticates through
the secure user store (``hdbuserstore``) key given by the ``hdb_userstore_key``
param, i.e. ``hdbsql -U <KEY>``. Only the (secret-free) SQL statement is passed
as an argument, so no password ever lands in argv or logs. Passing SQL as an
argv element (rather than stdin) keeps the driver runner-agnostic: it works
identically over the local ``Runner`` and the remote ``SSHRunner``.

References: SAP Note 1642148 (log backup recovery), 1999930 (space sizing).
"""

from __future__ import annotations

from exodia.core import Context, Result

from .base import DBRestoreDriver, PlannedCommand, register_driver


@register_driver
class HanaRestoreDriver(DBRestoreDriver):
    """Recover a HANA (tenant) database from data + log backups via hdbsql."""

    db_type = "hana"

    # --- parameter resolution -------------------------------------------------

    @staticmethod
    def _userstore_key(ctx: Context) -> str:
        return str(ctx.get("hdb_userstore_key", "SYSTEMDB"))

    @staticmethod
    def _database(ctx: Context) -> str:
        # The tenant to recover; target overrides the explicit param.
        return str(ctx.target or ctx.get("database", ctx.sid or "SYSTEMDB"))

    @staticmethod
    def _data_backup_prefix(ctx: Context) -> str:
        # Where/what to recover FROM (the source backup set prefix).
        return str(ctx.source or ctx.get("data_backup_prefix", ""))

    def _recover_sql(self, ctx: Context) -> str:
        """Build the RECOVER DATABASE statement from ctx.

        Uses the backup catalog + log backups (point-in-time to the most recent
        state by default). ``recover_until`` narrows to a timestamp when given.
        """
        database = self._database(ctx)
        data_prefix = self._data_backup_prefix(ctx)
        catalog_path = str(ctx.get("catalog_path", data_prefix))
        data_path = str(ctx.get("data_path", data_prefix))
        log_path = str(ctx.get("log_backup_path", data_prefix))
        until = ctx.get("recover_until")  # e.g. "2026-07-16 10:00:00"

        timestamp = until if until else "9999-01-01 00:00:00"
        return (
            f"RECOVER DATABASE FOR {database} UNTIL TIMESTAMP '{timestamp}' "
            f"CLEAR LOG "
            f"USING CATALOG PATH ('{catalog_path}') "
            f"USING LOG PATH ('{log_path}') "
            f"USING DATA PATH ('{data_path}') "
            f"CHECK ACCESS USING FILE"
        )

    def _hdbsql(self, ctx: Context, sql: str) -> list[str]:
        # -U <key>: secure user store (no password on the command line).
        # -x quiet, -a no column headers — machine-friendly output.
        return ["hdbsql", "-U", self._userstore_key(ctx), "-x", "-a", sql]

    # --- Strategy interface ---------------------------------------------------

    def plan(self, ctx: Context) -> list[PlannedCommand]:
        database = self._database(ctx)
        return [
            PlannedCommand(
                argv=self._hdbsql(ctx, self._recover_sql(ctx)),
                describe=f"RECOVER DATABASE for tenant {database} (data + log backups)",
            )
        ]

    def restore(self, ctx: Context) -> Result:
        name = "backup-restore.hana.restore"
        if not self._data_backup_prefix(ctx):
            return Result.fail(
                name,
                "no backup source given (set --source or params.data_backup_prefix)",
            )
        runner = ctx.runner()
        cmd = self.plan(ctx)[0]
        cr = runner.run(cmd.argv, timeout=int(ctx.get("recover_timeout", 3600)))
        if not cr.ok:
            return Result.fail(
                name,
                f"RECOVER DATABASE failed (exit {cr.exit_code})",
                detail=cr.stderr or cr.stdout,
                data={"exit_code": cr.exit_code},
            )
        return Result.ok(
            name,
            f"recovery completed for {self._database(ctx)}",
            data={"database": self._database(ctx)},
        )

    def verify(self, ctx: Context) -> Result:
        name = "backup-restore.hana.verify"
        runner = ctx.runner()
        # This query only succeeds when the database is online and accepting
        # connections; M_DATABASES exposes each tenant's ACTIVE_STATUS.
        sql = "SELECT DATABASE_NAME, ACTIVE_STATUS FROM M_DATABASES"
        cr = runner.run(self._hdbsql(ctx, sql), timeout=int(ctx.get("verify_timeout", 120)))
        if not cr.ok:
            return Result.fail(
                name,
                "database is not reachable / not online after recovery",
                detail=cr.stderr or cr.stdout,
                data={"exit_code": cr.exit_code},
            )
        if "YES" not in cr.stdout.upper():
            return Result.warn(
                name,
                "hdbsql reachable but no ACTIVE_STATUS=YES row found",
                detail=cr.stdout,
            )
        return Result.ok(
            name,
            "database online (M_DATABASES ACTIVE_STATUS=YES)",
            data={"stdout": cr.stdout.strip()},
        )
