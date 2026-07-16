"""HANA configuration prerequisite checks (TIA-57 #4, #6, #8).

4. log_mode = normal in global.ini [persistence]
6. source vs target HANA version compatibility (target >= source)
8. backint configuration present when backint is the backup destination
"""

from __future__ import annotations

import re

from exodia.core import Check, Context, Result

from . import _common as c


def _parse_revision(version: str) -> tuple[int, ...]:
    """Turn a HANA version string like '2.00.059.09' into a comparable tuple."""
    nums = re.findall(r"\d+", version)
    return tuple(int(n) for n in nums) if nums else (0,)


class LogModeNormalCheck(Check):
    """log_mode must be 'normal' for log backups / point-in-time recovery."""

    name = "backup-restore.hana.log-mode-normal"
    description = "log_mode=normal in global.ini [persistence]."
    blocking = True

    def run(self, ctx: Context) -> Result:
        stmt = (
            "SELECT VALUE FROM SYS.M_INIFILE_CONTENTS WHERE FILE_NAME = 'global.ini' "
            "AND SECTION = 'persistence' AND KEY = 'log_mode'"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not read log_mode from global.ini [persistence]",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        value = rows[0][0].strip().lower() if rows and rows[0] and rows[0][0] else ""
        if value == "normal":
            return Result.ok(self.name, "log_mode=normal", data={"log_mode": value})
        return Result.fail(
            self.name,
            f"log_mode is '{value or 'unset'}', expected normal (log mode overwrite blocks PITR)",
            data={"log_mode": value},
        )


class VersionCompatibilityCheck(Check):
    """Target HANA revision must be >= source revision."""

    name = "backup-restore.hana.version-compatibility"
    description = "Target HANA revision >= source revision."
    blocking = True

    def run(self, ctx: Context) -> Result:
        source_v = ctx.get("source_version")
        target_v = ctx.get("target_version")
        if not target_v:
            stmt = "SELECT VERSION FROM SYS.M_DATABASE"
            cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
            if cr.ok:
                rows = c.parse_hdbsql_rows(cr.stdout)
                if rows and rows[0] and rows[0][0]:
                    target_v = rows[0][0].strip()
        if not source_v or not target_v:
            return Result.skip(
                self.name,
                "source/target versions not provided; pass source_version & target_version params",
            )
        src = _parse_revision(str(source_v))
        tgt = _parse_revision(str(target_v))
        if tgt >= src:
            return Result.ok(
                self.name,
                f"target {target_v} >= source {source_v}",
                data={"source": source_v, "target": target_v},
            )
        return Result.fail(
            self.name,
            f"target revision {target_v} is older than source {source_v} — recovery unsupported",
            data={"source": source_v, "target": target_v},
        )


class BackintConfigCheck(Check):
    """When backint is the destination, its config parameters must be set."""

    name = "backup-restore.hana.backint-config"
    description = "Backint configuration present when backint is used."
    blocking = False

    def run(self, ctx: Context) -> Result:
        destination = str(ctx.get("backup_destination", "file")).lower()
        if destination != "backint":
            return Result.skip(
                self.name,
                "backup destination is not backint — nothing to verify",
                data={"destination": destination},
            )
        stmt = (
            "SELECT KEY, VALUE FROM SYS.M_INIFILE_CONTENTS WHERE FILE_NAME = 'global.ini' "
            "AND SECTION = 'backup' AND KEY IN "
            "('data_backup_parameter_file', 'log_backup_parameter_file', 'catalog_backup_parameter_file')"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not read backint configuration from global.ini [backup]",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        configured = {r[0]: r[1] for r in rows if len(r) >= 2 and r[1]}
        if not configured:
            return Result.fail(
                self.name,
                "backint selected but no *_parameter_file configured in global.ini [backup]",
                data={"destination": destination},
            )
        return Result.ok(
            self.name,
            f"backint configured ({len(configured)} parameter file(s))",
            data={"destination": destination, "configured": list(configured)},
        )
