"""Guarded action: restore a database from backup, engine-agnostic.

``RestoreDatabaseAction`` is a thin, driver-based orchestrator. It selects the
right :class:`DBRestoreDriver` at runtime via :func:`get_driver` based on
``ctx.db_type`` and delegates the real work — so the *same* guarded flow covers
HANA, ASE, and any future engine.

The 6-step safe-execution flow (dry-run -> confirm -> execute -> verify) is
provided by the base ``Action.run_guarded`` and is NOT reimplemented here. This
class only supplies the four phase methods.
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action

from ..db_drivers import get_driver


class RestoreDatabaseAction(Action):
    """Restore a database (HANA/ASE/...) from data + log backups, guarded."""

    name = "backup-restore.restore-database"
    description = "Restore a database from data + log backups (driver: hana|ase)."
    destructive = True
    requires_checks = [
        "backup-restore.hana.target-data-space",
        "backup-restore.hana.log-mode-normal",
        "backup-restore.hana.catalog-integrity",
    ]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        try:
            driver = get_driver(ctx.db_type)
        except ValueError as exc:
            return Result.fail(phase, str(exc))

        try:
            plan = driver.plan(ctx)
        except Exception as exc:  # noqa: BLE001 - convert planning error to a clean result
            return Result.fail(phase, f"could not build restore plan: {exc}")

        commands = [pc.display for pc in plan]
        detail = "\n".join(f"  {i}. {line}" for i, line in enumerate(commands, start=1))
        return Result.ok(
            phase,
            f"[{driver.db_type}] would run {len(commands)} command(s); nothing executed",
            detail=detail,
            data={"db_type": driver.db_type, "commands": commands},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        try:
            driver = get_driver(ctx.db_type)
        except ValueError as exc:
            return Result.fail(phase, str(exc))
        return driver.restore(ctx)

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        try:
            driver = get_driver(ctx.db_type)
        except ValueError as exc:
            return Result.fail(phase, str(exc))
        return driver.verify(ctx)

    def rollback(self, ctx: Context) -> Result:
        # A completed restore is not auto-reversible: bringing the target back
        # requires restoring the previous backup set (documented runbook step).
        return Result.skip(
            f"{self.name}.rollback",
            "no automatic rollback — restore the prior backup set manually "
            "(see runbook; HANA SAP Note 1642148 / ASE SAP Note 1706801)",
        )
