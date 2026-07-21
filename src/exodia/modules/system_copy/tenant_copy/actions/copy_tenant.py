"""Guarded action: HANA cross-host tenant copy (TIA-71).

``TenantCopyAction`` copies a source tenant (customer environment) into a freshly
provisioned tenant on the target (SAP HEC), across two different HANA systems.

The 6-step safe-execution flow (dry-run -> confirm -> execute -> verify) comes
from the base ``Action.run_guarded`` and is NOT reimplemented here; this class
supplies the four phase methods and delegates command construction to the
planner. Two methods are supported via the ``copy_method`` param:

* ``replication`` (default) — CREATE DATABASE ... AS REPLICA OF ... over system
  replication, then finalize once synced.
* ``backup`` — recover the source backup set into a new target tenant.

Credentials never appear on the command line (hdbsql -U <KEY>). Only secret-free
SQL is passed. A completed copy is not auto-reversible: rollback is documented
(drop the partially-created target tenant), never silently executed.

References (cite by number only): SAP Note 2101244 (MDC admin), 2456657 (system
replication), 1642148 (backup/recovery).
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import DB_TYPE, HOST, USER, ParamSpec

from ..checks import _common as c
from .planner import (
    TenantCopyPlan,
    TenantCopyPlanError,
    build_backup_plan,
    build_replication_plan,
    source_sql_port,
)


class TenantCopyAction(Action):
    """Copy a HANA tenant across hosts (customer -> HEC), guarded."""

    name = "tenant-copy.hana.copy-tenant"
    description = "Copy a HANA tenant across hosts (replication|backup), guarded."
    destructive = True
    requires_checks = [
        "tenant-copy.hana.source-tenant-online",
        "tenant-copy.hana.target-tenant-absent",
        "tenant-copy.hana.version-match",
        "tenant-copy.hana.target-data-space",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [
            HOST,
            USER,
            DB_TYPE.with_default("hana"),
            ParamSpec(
                "copy_method", "Copy method", default="replication",
                choices=("replication", "backup"),
                help="replication = low-downtime cross-host (HEC); backup = recover a shipped backup set.",
            ),
            # tenant identity + keys (shared specs — single source of truth)
            c.SOURCE_TENANT,
            c.TARGET_TENANT,
            c.TARGET_USERSTORE_KEY,
            # replication-method inputs
            c.SOURCE_HOST,
            c.SOURCE_INSTANCE,
            # backup-method inputs
            ParamSpec(
                "catalog_path", "Backup catalog path (backup method)",
                help="Path to the source backup catalog. Required for the backup method.",
            ),
            ParamSpec(
                "data_path", "Backup data path (backup method)",
                help="Path to the source data backup. Required for the backup method.",
            ),
            ParamSpec(
                "log_backup_path", "Backup log path (backup method)",
                help="Path to the source log backups; defaults to the data path.",
            ),
            # Post-copy data-integrity verification (optional but recommended).
            # These connect directly to the TENANTS (not the SYSTEMDBs) so verify
            # can compare object + record counts source vs target after the copy.
            ParamSpec(
                "source_tenant_key", "Source tenant hdbuserstore key (verify)",
                help="hdbsql -U key connecting to the SOURCE tenant itself "
                "(not SYSTEMDB). Enables post-copy row-count comparison.",
            ),
            ParamSpec(
                "target_tenant_key", "Target tenant hdbuserstore key (verify)",
                help="hdbsql -U key connecting to the newly-created TARGET tenant. "
                "Enables post-copy row-count comparison.",
            ),
        ]

    # --- parameter resolution -------------------------------------------------

    @staticmethod
    def _method(ctx: Context) -> str:
        return str(ctx.get("copy_method", "replication")).lower()

    @staticmethod
    def _target_key(ctx: Context) -> str:
        return str(
            ctx.get("target_userstore_key") or ctx.get("userstore_key") or "SYSTEMDB"
        )

    @staticmethod
    def _source_tenant(ctx: Context) -> str | None:
        return ctx.source or ctx.get("source_tenant")

    @staticmethod
    def _target_tenant(ctx: Context) -> str | None:
        return ctx.target or ctx.get("target_tenant")

    def _build_plan(self, ctx: Context) -> TenantCopyPlan:
        method = self._method(ctx)
        src = self._source_tenant(ctx)
        tgt = self._target_tenant(ctx)
        key = self._target_key(ctx)
        if method == "replication":
            source_inst = ctx.get("source_instance")
            port = ctx.get("source_port") or source_sql_port(
                str(source_inst) if source_inst is not None else None
            )
            return build_replication_plan(
                target_key=key,
                source_tenant=src or "",
                target_tenant=tgt or "",
                source_host=ctx.get("source_host"),
                source_port=int(port) if port else None,
            )
        if method == "backup":
            return build_backup_plan(
                target_key=key,
                source_tenant=src or "",
                target_tenant=tgt or "",
                catalog_path=str(ctx.get("catalog_path", "")),
                data_path=str(ctx.get("data_path", "")),
                log_path=str(ctx.get("log_backup_path", "")),
            )
        raise TenantCopyPlanError(
            f"unknown copy_method '{method}' (expected 'replication' or 'backup')"
        )

    # --- Action phase methods -------------------------------------------------

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        try:
            plan = self._build_plan(ctx)
        except TenantCopyPlanError as exc:
            return Result.fail(phase, str(exc), sap_note="2101244")
        lines = [f"  {i}. {pc.display}" for i, pc in enumerate(plan.commands, start=1)]
        endpoint = (
            f"{plan.source_host}:{plan.source_port}"
            if plan.source_host
            else "(backup set)"
        )
        return Result.ok(
            phase,
            f"[{plan.method}] would copy {plan.source_tenant} -> {plan.target_tenant} "
            f"via {endpoint}; {len(plan.commands)} command(s); nothing executed",
            detail="\n".join(lines),
            data={
                "method": plan.method,
                "source_tenant": plan.source_tenant,
                "target_tenant": plan.target_tenant,
                "source_host": plan.source_host,
                "source_port": plan.source_port,
                "commands": [pc.display for pc in plan.commands],
            },
            sap_note="2456657" if plan.method == "replication" else "1642148",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        try:
            plan = self._build_plan(ctx)
        except TenantCopyPlanError as exc:
            return Result.fail(phase, str(exc), sap_note="2101244")

        runner = ctx.runner()
        timeout = int(ctx.get("copy_timeout", 7200))
        ran: list[str] = []
        for pc in plan.commands:
            cr = runner.run(pc.argv, timeout=timeout)
            ran.append(pc.display)
            if not cr.ok:
                return Result.fail(
                    phase,
                    f"tenant copy step failed: {pc.display} (exit {cr.exit_code}) — "
                    "run PAUSED; drop the partial target tenant before retrying",
                    detail=cr.stderr or cr.stdout,
                    data={
                        "method": plan.method,
                        "failed_step": pc.display,
                        "completed_steps": ran[:-1],
                        "exit_code": cr.exit_code,
                    },
                    sap_note="2456657" if plan.method == "replication" else "1642148",
                )
        return Result.ok(
            phase,
            f"tenant copy commands completed for {plan.target_tenant} "
            f"({plan.method}); verify next",
            data={
                "method": plan.method,
                "target_tenant": plan.target_tenant,
                "completed_steps": ran,
            },
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        tgt = self._target_tenant(ctx)
        if not tgt:
            return Result.fail(phase, "no target tenant to verify")
        key = self._target_key(ctx)
        sql = (
            "SELECT DATABASE_NAME, ACTIVE_STATUS FROM SYS_DATABASES.M_DATABASES "
            f"WHERE DATABASE_NAME = '{tgt}'"
        )
        cr = ctx.runner().run(
            ["hdbsql", "-U", key, "-x", "-a", "-j", sql],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        if not cr.ok:
            return Result.fail(
                phase,
                f"target tenant '{tgt}' not reachable / not online after copy",
                detail=cr.stderr or cr.stdout,
                data={"exit_code": cr.exit_code},
                sap_note="2101244",
            )
        if "YES" not in cr.stdout.upper() and "ONLINE" not in cr.stdout.upper():
            return Result.warn(
                phase,
                f"target tenant '{tgt}' created but ACTIVE_STATUS is not YES yet "
                "(replication may still be syncing)",
                detail=cr.stdout,
            )

        # Online is necessary but not sufficient. When tenant-level keys are
        # provided, compare object + record counts source vs target so "online"
        # is upgraded to "online AND the data actually came across".
        integrity = self._verify_data_integrity(ctx, phase, tgt)
        if integrity is not None:
            return integrity
        return Result.ok(
            phase,
            f"target tenant '{tgt}' is online (copy verified; data-integrity "
            "comparison skipped — set source_tenant_key + target_tenant_key to enable)",
            data={"target_tenant": tgt, "stdout": cr.stdout.strip()},
        )

    # --- post-copy data-integrity comparison ---------------------------------

    @staticmethod
    def _tenant_counts(ctx: Context, tenant_key: str, timeout: int) -> tuple[int, int] | None:
        """Return (table_count, total_record_count) for a tenant, or None on error.

        Queries M_TABLES on the tenant itself (not SYSTEMDB): the number of
        tables and the summed RECORD_COUNT give a cheap, deterministic fingerprint
        of the copied data set. Read-only.
        """
        sql = (
            "SELECT COUNT(*), COALESCE(SUM(RECORD_COUNT), 0) FROM M_TABLES "
            "WHERE SCHEMA_NAME NOT LIKE '\\_SYS%' ESCAPE '\\'"
        )
        cr = ctx.runner().run(
            ["hdbsql", "-U", tenant_key, "-x", "-a", "-j", sql], timeout=timeout
        )
        if not cr.ok:
            return None
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows or len(rows[0]) < 2:
            return None
        try:
            return int(rows[0][0]), int(rows[0][1])
        except (ValueError, IndexError):
            return None

    def _verify_data_integrity(
        self, ctx: Context, phase: str, tgt: str
    ) -> Result | None:
        """Compare source vs target tenant counts. None = comparison not attempted.

        Returns a Result (PASS/WARN/FAIL) when both tenant keys are supplied and
        a comparison could be made; returns None when the comparison is skipped
        (no keys) so the caller falls back to the plain online verdict.
        """
        src_key = ctx.get("source_tenant_key")
        tgt_key = ctx.get("target_tenant_key")
        if not src_key or not tgt_key:
            return None
        timeout = int(ctx.get("verify_timeout", 120))
        src = self._tenant_counts(ctx, str(src_key), timeout)
        tgt_counts = self._tenant_counts(ctx, str(tgt_key), timeout)
        if src is None or tgt_counts is None:
            side = "source" if src is None else "target"
            return Result.warn(
                phase,
                f"target tenant '{tgt}' is online, but could not read {side} "
                "tenant counts for the data-integrity comparison",
                data={"source_counts": src, "target_counts": tgt_counts},
            )
        src_tables, src_records = src
        tgt_tables, tgt_records = tgt_counts
        # Record counts on column tables are approximate (delta/main, pending
        # merges), so allow a small tolerance; table count must match exactly.
        tol = float(ctx.get("verify_record_tolerance", 0.01))
        record_delta = abs(src_records - tgt_records)
        record_drift = (record_delta / src_records) if src_records else 0.0
        data = {
            "target_tenant": tgt,
            "source_tables": src_tables,
            "target_tables": tgt_tables,
            "source_records": src_records,
            "target_records": tgt_records,
            "record_drift": round(record_drift, 4),
            "tolerance": tol,
        }
        if tgt_tables != src_tables:
            return Result.fail(
                phase,
                f"data-integrity FAIL: table count differs "
                f"(source={src_tables}, target={tgt_tables}) — copy incomplete",
                data=data,
                sap_note="2101244",
            )
        if record_drift > tol:
            return Result.fail(
                phase,
                f"data-integrity FAIL: record count drift {record_drift:.2%} "
                f"exceeds tolerance {tol:.2%} "
                f"(source={src_records}, target={tgt_records})",
                data=data,
                sap_note="2101244",
            )
        return Result.ok(
            phase,
            f"target tenant '{tgt}' online and data verified: {tgt_tables} tables, "
            f"{tgt_records} records (drift {record_drift:.2%} within {tol:.2%})",
            data=data,
        )

    def rollback(self, ctx: Context) -> Result:
        tgt = self._target_tenant(ctx) or "<target>"
        return Result.skip(
            f"{self.name}.rollback",
            f"no automatic rollback — drop the partially-created target tenant "
            f"manually: DROP DATABASE {tgt} (see SAP Note 2101244), then retry",
            sap_note="2101244",
        )
