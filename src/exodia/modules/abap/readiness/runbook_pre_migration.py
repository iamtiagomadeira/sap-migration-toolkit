"""Full SAP MIG pre-migration checks runbook (Phase 2 — Tier A).

Bundles the complete set of read-only ABAP readiness checks into one ordered
sweep that walks the cutover phases in order: preparation/parity first, then the
source ramp-down drain, then the post-processing signals. It is the ABAP
counterpart of ``tenant-copy.hana.readiness`` and the single command a
consultant runs on the source (and, for the parity checks, against the target)
to answer "is this ABAP system ready to hand over?".

Every step is read-only and re-observes the live system, so the runbook is
idempotent and safe to re-run. Results carry their cutover phase, so the
evidence report groups them under Preparation / Ramp-Down / Post-Activities
headings automatically.

``stop_on_blocking`` is False: on cutover day the operator wants the whole
picture in one pass — every blocker and every warning at once.
"""

from __future__ import annotations

from exodia.core.runbook import Runbook


class PreMigrationChecksRunbook(Runbook):
    """Complete read-only SAP MIG ABAP readiness sweep, grouped by cutover phase."""

    name = "abap.pre-migration-checks"
    description = (
        "Full read-only SAP MIG ABAP readiness sweep: system parity, source "
        "ramp-down drain and post-processing signals, with one aggregate verdict."
    )
    stop_on_blocking = False
    steps = [
        # --- Preparation / parity (identity, versions, config) ---------------
        "abap.readiness.system-info",
        "abap.readiness.component-versions",
        "abap.readiness.app-servers",
        "abap.readiness.client-settings",
        "abap.readiness.rfc-destinations",
        "abap.readiness.source-profiles",
        "abap.readiness.target-profiles",
        "abap.readiness.system-change-option",
        "abap.readiness.installation-consistency",
        # --- Ramp-down (quiesce the source) ----------------------------------
        "abap.readiness.active-users",
        "abap.readiness.lock-entries",
        "abap.readiness.update-queues-drained",
        "abap.readiness.background-jobs",
        "abap.readiness.batch-input-sessions",
        "abap.readiness.transport-requests",
        "abap.readiness.spool-requests",
        # --- Post-processing signals -----------------------------------------
        "abap.readiness.short-dumps",
    ]
