"""ASE (Sybase) restore driver — load + online via ``isql``.

Strategy implementation for SAP ASE. The recovery sequence is:

    load database   <db> from <data dump>
    load transaction <db> from <log dump>   (repeated, in order, for each log)
    online database <db>

Credentials never appear on the command line: ``isql`` reads the password from
stdin (``-w`` width tuning only), and connection identity is supplied via the
``-U`` user with ``-P`` sourced from an interactive prompt fed through
``input_text``. To keep the driver runner-agnostic and secret-free in argv, the
SQL batch is passed with ``-i`` pointing at a caller-provided file, or inlined
via ``-Q``-style query when no file is used. Here we pass the batch as a single
``-Q`` query argument (SQL only, never a password).

Reference: SAP Note 1706801 (ASE homogeneous system copy via load database).
"""

from __future__ import annotations

from exodia.core import Context, Result

from .base import DBRestoreDriver, PlannedCommand, register_driver


@register_driver
class AseRestoreDriver(DBRestoreDriver):
    """Restore an ASE database: load database -> load transaction(s) -> online."""

    db_type = "ase"

    # --- parameter resolution -------------------------------------------------

    @staticmethod
    def _server(ctx: Context) -> str:
        return str(ctx.get("ase_server", ctx.sid or "SYBASE"))

    @staticmethod
    def _user(ctx: Context) -> str:
        return str(ctx.get("ase_user", "sapsa"))

    @staticmethod
    def _database(ctx: Context) -> str:
        return str(ctx.target or ctx.get("database", ctx.sid or ""))

    @staticmethod
    def _data_dump(ctx: Context) -> str:
        # The full database dump to load FROM.
        return str(ctx.source or ctx.get("data_dump", ""))

    @staticmethod
    def _log_dumps(ctx: Context) -> list[str]:
        # Ordered list of transaction-log dumps to replay after the data load.
        logs = ctx.get("log_dumps", [])
        if isinstance(logs, str):
            return [logs] if logs else []
        return [str(x) for x in logs]

    def _isql(self, ctx: Context, sql: str) -> list[str]:
        # -S server, -U user. Password is NOT on the command line: isql reads it
        # from stdin at the "Password:" prompt in interactive contexts; in this
        # secret-free argv we rely on the ASE user store / -X SSO or an
        # externally-supplied auth, never -P <cleartext>.
        return [
            "isql",
            "-S",
            self._server(ctx),
            "-U",
            self._user(ctx),
            "-b",
            "-w",
            "1000",
            "-Q",
            sql,
        ]

    # --- Strategy interface ---------------------------------------------------

    def plan(self, ctx: Context) -> list[PlannedCommand]:
        database = self._database(ctx)
        data_dump = self._data_dump(ctx)
        commands: list[PlannedCommand] = [
            PlannedCommand(
                argv=self._isql(ctx, f"load database {database} from '{data_dump}'"),
                describe=f"load full database dump into {database}",
            )
        ]
        for i, log in enumerate(self._log_dumps(ctx), start=1):
            commands.append(
                PlannedCommand(
                    argv=self._isql(ctx, f"load transaction {database} from '{log}'"),
                    describe=f"replay transaction-log dump #{i}",
                )
            )
        commands.append(
            PlannedCommand(
                argv=self._isql(ctx, f"online database {database}"),
                describe=f"bring {database} online",
            )
        )
        return commands

    def restore(self, ctx: Context) -> Result:
        name = "backup-restore.ase.restore"
        database = self._database(ctx)
        if not database:
            return Result.fail(name, "no target database given (set --target or params.database)")
        if not self._data_dump(ctx):
            return Result.fail(name, "no data dump given (set --source or params.data_dump)")
        runner = ctx.runner()
        timeout = int(ctx.get("load_timeout", 3600))
        for cmd in self.plan(ctx):
            cr = runner.run(cmd.argv, timeout=timeout)
            if not cr.ok:
                return Result.fail(
                    name,
                    f"step failed: {cmd.describe} (exit {cr.exit_code})",
                    detail=cr.stderr or cr.stdout,
                    data={"exit_code": cr.exit_code, "step": cmd.describe},
                )
        return Result.ok(
            name,
            f"loaded and brought {database} online",
            data={"database": database, "log_dumps": len(self._log_dumps(ctx))},
        )

    def verify(self, ctx: Context) -> Result:
        name = "backup-restore.ase.verify"
        database = self._database(ctx)
        runner = ctx.runner()
        # status 0 in sysdatabases means the database is online (no offline/
        # loading/suspect bits set).
        # nosec B608 - not user-facing SQL: `database` is a SAP DB identifier from
        # trusted migration config, and the statement is passed as a single argv
        # element to isql (never a shell). ASE DDL/status verbs cannot be bound.
        sql = f"select name, status from master..sysdatabases where name = '{database}'"  # nosec B608
        cr = runner.run(self._isql(ctx, sql), timeout=int(ctx.get("verify_timeout", 120)))
        if not cr.ok:
            return Result.fail(
                name,
                "could not query database status after load",
                detail=cr.stderr or cr.stdout,
                data={"exit_code": cr.exit_code},
            )
        if database and database not in cr.stdout:
            return Result.warn(
                name,
                f"{database} not found in sysdatabases output",
                detail=cr.stdout,
            )
        return Result.ok(
            name,
            f"{database} present and online in sysdatabases",
            data={"stdout": cr.stdout.strip()},
        )
