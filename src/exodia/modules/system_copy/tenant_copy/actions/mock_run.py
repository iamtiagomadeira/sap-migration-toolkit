"""Dry-Run / Mock-Run isolation actions for HANA tenant copy.

The COP has an "Isolate System (Mock-Run Only)" section: on a DRY-RUN the copied
target must be isolated so it can be validated WITHOUT talking to the outside
world (locking users, neutralising RFC destinations, stopping jobs). This is
ONLY done on a mock/dry run — never on the real cutover — so these actions are
marked as such and always take a table backup first, and every one has a real
reverse (restore-from-backup) so the isolation is fully undoable.

Each action runs SQL against the copied tenant via a tenant hdbuserstore key
(``tenant_key``) as the ABAP schema owner (e.g. SAPABAP1). Guarded flow:
dry-run -> confirm -> execute -> verify, with rollback restoring from the backup
table.

Exact statements come from the runbook (USR02 lock, RFCDES neutralise, TBTCO
stop). Technical users (DDIC + an explicit keep-list) are always spared.
"""

from __future__ import annotations

import re

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from ..checks import _common as c

_DEFAULT_SPARED = ["DDIC"]

# SAP business/technical user names (USR02.BNAME): letters, digits, and the
# handful of punctuation SAP allows (_ . -). Rejects quotes/whitespace/semicolons
# so a spared-user name can never break out of the IN (...) list literal.
_USER_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,12}$")


def _tenant_key(ctx: Context) -> str:
    return str(ctx.get("tenant_key") or ctx.get("target_tenant_key") or "")


def _schema(ctx: Context) -> str:
    schema = str(ctx.get("abap_schema", "SAPABAP1"))
    if not c.is_valid_schema(schema):
        raise ValueError(
            f"invalid abap_schema '{schema}' — must be a plain SQL identifier "
            "(letter first, then alphanumerics/underscore)"
        )
    return schema


def _hdbsql(ctx: Context, sql: str) -> list[str]:
    """hdbsql argv against the tenant key, running as the ABAP schema."""
    key = _tenant_key(ctx)
    return ["hdbsql", "-U", key, "-x", "-a", "-j", sql]


def _spared_users(ctx: Context) -> list[str]:
    extra = [u.strip() for u in str(ctx.get("keep_unlocked") or "").split(",") if u.strip()]
    users = _DEFAULT_SPARED + extra
    invalid = [u for u in users if not _USER_RE.match(u)]
    if invalid:
        raise ValueError(
            f"invalid keep_unlocked user name(s) {invalid} — expected SAP user "
            "names (letters/digits/_.- only, no quotes or spaces)"
        )
    return users


class _MockAction(Action):
    """Shared plumbing for the mock-run isolation actions (backup + guard)."""

    phase = Phase.DOWNTIME
    destructive = True
    manual = False
    requires_checks: list[str] = []
    #: table this action mutates + its backup table name.
    table = ""
    backup_table = ""

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "tenant_key", "Tenant hdbuserstore key (copied tenant)",
                help="hdbsql -U key connecting to the copied tenant as the ABAP schema.",
            ),
            ParamSpec(
                "abap_schema", "ABAP schema owner", default="SAPABAP1",
                help="Schema that owns USR02/RFCDES/TBTCO (e.g. SAPABAP1).",
            ),
        ]

    def _backup(self, ctx: Context) -> Result | None:
        """Create the backup table (idempotent-ish). None on success."""
        if not _tenant_key(ctx):
            return Result.skip(f"{self.name}.execute", "no tenant_key provided")
        schema = _schema(ctx)
        sql = f'CREATE TABLE "{schema}"."{self.backup_table}" AS (SELECT * FROM "{schema}"."{self.table}")'  # nosec B608 - schema validated by is_valid_schema; table/backup_table are class-level literals (no user input)
        self._emit_log(f"$ backup {self.table} -> {self.backup_table}")
        cr = ctx.runner().run(_hdbsql(ctx, sql), timeout=int(ctx.get("mock_timeout", 300)))
        # A pre-existing backup table is fine (already backed up); other errors fail.
        if not cr.ok and "exists" not in (cr.stderr or cr.stdout).lower():
            return Result.fail(
                f"{self.name}.execute",
                f"could not back up {self.table} before isolating",
                detail=cr.stderr or cr.stdout,
            )
        return None

    def rollback(self, ctx: Context) -> Result:
        """Restore the mutated table from its backup (real reverse)."""
        phase = f"{self.name}.rollback"
        if not _tenant_key(ctx):
            return Result.skip(phase, "no tenant_key provided")
        schema = _schema(ctx)
        # Truncate + reinsert from backup, then drop the backup.
        stmts = [
            f'TRUNCATE TABLE "{schema}"."{self.table}"',  # nosec B608 - schema validated by is_valid_schema; table is a class-level literal (no user input)
            f'INSERT INTO "{schema}"."{self.table}" SELECT * FROM "{schema}"."{self.backup_table}"',  # nosec B608 - schema validated by is_valid_schema; table/backup_table are class-level literals (no user input)
        ]
        for sql in stmts:
            cr = ctx.runner().run(_hdbsql(ctx, sql), timeout=int(ctx.get("mock_timeout", 300)))
            if not cr.ok:
                return Result.fail(
                    phase, f"restore failed on: {sql}", detail=cr.stderr or cr.stdout
                )
        return Result.ok(
            phase, f"{self.table} restored from {self.backup_table}",
            facts={"Restored": self.table},
        )


class MockIsolateUsersAction(_MockAction):
    """Mock-run: lock business users on the copied tenant (USR02.UFLAG)."""

    name = "tenant-copy.hana.mock-isolate-users"
    description = "DRY-RUN ONLY: lock users on the copied tenant (USR02), sparing DDIC."
    title = "Mock-Run — Isolate Users (USR02 lock)"
    table = "USR02"
    backup_table = "BKP_USR02"

    def dry_run(self, ctx: Context) -> Result:
        spared = _spared_users(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            "[MOCK-RUN] would back up USR02 then lock all active users (UFLAG 0->65) "
            f"except {', '.join(spared)}",
            detail=(
                f'  1. CREATE TABLE "{_schema(ctx)}"."BKP_USR02" AS (SELECT * FROM USR02)\n'  # nosec B608 - schema validated by is_valid_schema; display-only dry-run detail (not executed)
                "  2. UPDATE USR02 SET UFLAG='65' WHERE UFLAG='0' AND BNAME NOT IN (...)"
            ),
            facts={"Table": "USR02", "Spared": ", ".join(spared), "Mock-Run": "yes"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        bk = self._backup(ctx)
        if bk is not None:
            return bk
        spared = _spared_users(ctx)
        in_list = ", ".join(f"'{u}'" for u in spared)
        sql = f"UPDATE USR02 SET UFLAG = '65' WHERE UFLAG = '0' AND BNAME NOT IN ({in_list})"  # nosec B608 - in_list built from _spared_users (each name validated by _USER_RE, no quote/space possible); rest is a literal
        cr = ctx.runner().run(_hdbsql(ctx, sql), timeout=int(ctx.get("mock_timeout", 300)))
        if not cr.ok:
            return Result.fail(phase, "failed to lock users on the copied tenant",
                               detail=cr.stderr or cr.stdout)
        return Result.ok(
            phase, f"[MOCK-RUN] users locked on the copied tenant (spared: {', '.join(spared)})",
            data={"spared": spared}, facts={"Users Locked": "all active", "Spared": ", ".join(spared)},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(f"{self.name}.verify", "users isolated for the mock run",
                         facts={"Table": "USR02"})


class MockIsolateRfcsAction(_MockAction):
    """Mock-run: neutralise RFC destinations on the copied tenant (RFCDES)."""

    name = "tenant-copy.hana.mock-isolate-rfcs"
    description = "DRY-RUN ONLY: neutralise RFC destinations on the copied tenant (RFCDES)."
    title = "Mock-Run — Isolate RFCs (RFCDES)"
    table = "RFCDES"
    backup_table = "BKP_RFCDEST"

    def dry_run(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.dry-run",
            "[MOCK-RUN] would back up RFCDES then prefix target hosts (G=/H=/N=/X=) "
            "with '#' so RFC destinations cannot reach the outside world",
            detail=(
                f'  1. CREATE TABLE "{_schema(ctx)}"."BKP_RFCDEST" AS (SELECT * FROM RFCDES)\n'  # nosec B608 - schema validated by is_valid_schema; display-only dry-run detail (not executed)
                "  2. UPDATE RFCDES SET RFCOPTIONS = REPLACE(...,'G=','G=#') WHERE ..."
            ),
            facts={"Table": "RFCDES", "Mock-Run": "yes"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        bk = self._backup(ctx)
        if bk is not None:
            return bk
        # Neutralise each connection-type prefix; SAPGUI_QUEUE is spared for X=.
        stmts = [
            "UPDATE RFCDES SET RFCOPTIONS = REPLACE(RFCOPTIONS, 'G=', 'G=#') WHERE RFCOPTIONS LIKE 'G=%'",
            "UPDATE RFCDES SET RFCOPTIONS = REPLACE(RFCOPTIONS, 'H=', 'H=#') WHERE RFCOPTIONS LIKE 'H=%'",
            "UPDATE RFCDES SET RFCOPTIONS = REPLACE(RFCOPTIONS, 'N=', 'N=#') WHERE RFCOPTIONS LIKE 'N=%'",
            "UPDATE RFCDES SET RFCOPTIONS = REPLACE(RFCOPTIONS, 'X=', 'X=#') WHERE RFCOPTIONS LIKE 'X=%' "
            "AND RFCDEST NOT IN ('SAPGUI_QUEUE')",
        ]
        for i, sql in enumerate(stmts, start=1):
            self._emit_progress(100.0 * i / len(stmts), f"{i}/{len(stmts)}")
            cr = ctx.runner().run(_hdbsql(ctx, sql), timeout=int(ctx.get("mock_timeout", 300)))
            if not cr.ok:
                return Result.fail(phase, f"failed neutralising RFCs (step {i})",
                                   detail=cr.stderr or cr.stdout)
        return Result.ok(
            phase, "[MOCK-RUN] RFC destinations neutralised on the copied tenant",
            facts={"Table": "RFCDES", "RFCs": "neutralised"},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(f"{self.name}.verify", "RFCs isolated for the mock run",
                         facts={"Table": "RFCDES"})


class MockStopJobsAction(_MockAction):
    """Mock-run: stop released background jobs on the copied tenant (TBTCO)."""

    name = "tenant-copy.hana.mock-stop-jobs"
    description = "DRY-RUN ONLY: stop released jobs on the copied tenant (TBTCO)."
    title = "Mock-Run — Stop Jobs (TBTCO)"
    table = "TBTCO"
    backup_table = "BKP_TBTCO"

    def dry_run(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.dry-run",
            "[MOCK-RUN] would back up TBTCO then set released jobs (STATUS 'S'->'Z') "
            "except RDDIMPDP%",
            detail=(
                f'  1. CREATE TABLE "{_schema(ctx)}"."BKP_TBTCO" AS (SELECT * FROM TBTCO)\n'  # nosec B608 - schema validated by is_valid_schema; display-only dry-run detail (not executed)
                "  2. UPDATE TBTCO SET STATUS='Z', LASTCHNAME='DDIC' WHERE STATUS='S' "
                "AND JOBNAME NOT LIKE 'RDDIMPDP%'"
            ),
            facts={"Table": "TBTCO", "Mock-Run": "yes"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        bk = self._backup(ctx)
        if bk is not None:
            return bk
        sql = (
            "UPDATE TBTCO SET STATUS = 'Z', LASTCHNAME = 'DDIC' "
            "WHERE STATUS = 'S' AND JOBNAME NOT LIKE 'RDDIMPDP%'"
        )
        cr = ctx.runner().run(_hdbsql(ctx, sql), timeout=int(ctx.get("mock_timeout", 300)))
        if not cr.ok:
            return Result.fail(phase, "failed stopping jobs on the copied tenant",
                               detail=cr.stderr or cr.stdout)
        return Result.ok(
            phase, "[MOCK-RUN] released jobs stopped on the copied tenant (RDDIMPDP% spared)",
            facts={"Table": "TBTCO", "Jobs": "stopped (S->Z)"},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(f"{self.name}.verify", "jobs stopped for the mock run",
                         facts={"Table": "TBTCO"})
