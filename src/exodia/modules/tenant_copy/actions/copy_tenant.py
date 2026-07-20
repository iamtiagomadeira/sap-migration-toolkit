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
        return Result.ok(
            phase,
            f"target tenant '{tgt}' is online (copy verified)",
            data={"target_tenant": tgt, "stdout": cr.stdout.strip()},
        )

    def rollback(self, ctx: Context) -> Result:
        tgt = self._target_tenant(ctx) or "<target>"
        return Result.skip(
            f"{self.name}.rollback",
            f"no automatic rollback — drop the partially-created target tenant "
            f"manually: DROP DATABASE {tgt} (see SAP Note 2101244), then retry",
            sap_note="2101244",
        )
