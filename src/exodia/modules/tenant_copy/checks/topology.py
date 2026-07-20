"""Tenant topology checks for cross-host HANA tenant copy (TIA-71).

  1. source tenant exists and is ONLINE on the source SYSTEMDB (M_DATABASES)
  2. target tenant is ABSENT on the target SYSTEMDB (no accidental overwrite)
  3. source and target HANA versions are compatible (target >= source)

All read-only. Source is the customer environment; target is the SAP HEC system.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class SourceTenantOnlineCheck(Check):
    """The source tenant must exist and be ONLINE to be copied."""

    name = "tenant-copy.hana.source-tenant-online"
    description = "Source tenant exists and is ONLINE (M_DATABASES)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        tenant = c.source_tenant(ctx)
        if not c.is_valid_tenant(tenant):
            return Result.fail(
                self.name,
                f"invalid or missing source tenant name '{tenant}' "
                "(set --source or source_tenant; SYSTEMDB is not copyable)",
            )
        stmt = (
            "SELECT DATABASE_NAME, ACTIVE_STATUS FROM SYS_DATABASES.M_DATABASES "
            f"WHERE DATABASE_NAME = '{tenant}'"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, c.SOURCE, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not query source SYSTEMDB for tenant '{tenant}'",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows:
            return Result.fail(
                self.name,
                f"source tenant '{tenant}' not found on the source system",
                data={"tenant": tenant},
            )
        status = rows[0][1] if len(rows[0]) > 1 else "UNKNOWN"
        if status.upper() != "YES" and status.upper() != "ONLINE":
            return Result.fail(
                self.name,
                f"source tenant '{tenant}' is not online (ACTIVE_STATUS={status})",
                data={"tenant": tenant, "active_status": status},
            )
        return Result.ok(
            self.name,
            f"source tenant '{tenant}' is online",
            data={"tenant": tenant, "active_status": status},
        )


class TargetTenantAbsentCheck(Check):
    """The target tenant must NOT already exist — copying would clash/overwrite."""

    name = "tenant-copy.hana.target-tenant-absent"
    description = "Target tenant does not already exist on the target SYSTEMDB."
    blocking = True

    def run(self, ctx: Context) -> Result:
        tenant = c.target_tenant(ctx)
        if not c.is_valid_tenant(tenant):
            return Result.fail(
                self.name,
                f"invalid or missing target tenant name '{tenant}' "
                "(set --target or target_tenant)",
            )
        stmt = (
            "SELECT DATABASE_NAME FROM SYS_DATABASES.M_DATABASES "
            f"WHERE DATABASE_NAME = '{tenant}'"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, c.TARGET, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not query target SYSTEMDB for tenant '{tenant}'",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if rows:
            return Result.fail(
                self.name,
                f"target tenant '{tenant}' already exists — drop it first or pick "
                "another name (tenant copy will not overwrite)",
                data={"tenant": tenant},
            )
        return Result.ok(
            self.name,
            f"target tenant '{tenant}' is absent (safe to create)",
            data={"tenant": tenant},
        )


class VersionMatchCheck(Check):
    """Target HANA revision must be >= source revision for a tenant copy.

    Reads explicit source_version/target_version params when provided; otherwise
    queries M_DATABASE.VERSION on each SYSTEMDB.
    """

    name = "tenant-copy.hana.version-match"
    description = "Target HANA revision >= source revision."
    blocking = True

    def _version(self, ctx: Context, side: str) -> tuple[int, ...] | None:
        explicit = ctx.get(c.side_key(side, "version"))
        if explicit:
            return c.parse_version(str(explicit))
        cr = c.run(
            ctx,
            c.hdbsql_argv(ctx, side, "SELECT VERSION FROM M_DATABASE"),
        )
        if not cr.ok:
            return None
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows:
            return None
        return c.parse_version(rows[0][0])

    def run(self, ctx: Context) -> Result:
        src = self._version(ctx, c.SOURCE)
        tgt = self._version(ctx, c.TARGET)
        if src is None or tgt is None:
            missing = "source" if src is None else "target"
            return Result.warn(
                self.name,
                f"could not determine {missing} HANA version to compare",
                data={"source": src, "target": tgt},
            )
        if tgt >= src:
            return Result.ok(
                self.name,
                f"target revision {_fmt(tgt)} >= source {_fmt(src)}",
                data={"source": _fmt(src), "target": _fmt(tgt)},
            )
        return Result.fail(
            self.name,
            f"target revision {_fmt(tgt)} is older than source {_fmt(src)} — "
            "a tenant copy cannot downgrade the revision",
            data={"source": _fmt(src), "target": _fmt(tgt)},
        )


def _fmt(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)
