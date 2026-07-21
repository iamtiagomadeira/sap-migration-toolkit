"""Tenant-copy readiness runbook — "is this pair ready for a tenant copy?".

Bundles the eleven read-only HANA cross-host prerequisite checks into one
ordered sweep with a single aggregate verdict and a sealed evidence bundle.
It answers, in one command, the question a consultant otherwise assembles by
hand across two SYSTEMDBs: can I safely start copying the source tenant onto
the target host right now?

Ordering mirrors how the copy itself depends on things being true:

1. **Connectivity** first — both hdbuserstore keys must authenticate, and the
   target must actually reach the source SYSTEMDB SQL port. Nothing else can be
   trusted until we can talk to both systems (source-userstore-key,
   target-userstore-key, cross-host-reachability).
2. **Topology** — the source tenant must be online to copy from, the target
   tenant name must be free (we never silently overwrite), and the two HANA
   versions must be compatible (source-tenant-online, target-tenant-absent,
   version-match).
3. **Pre-conditions** — SSL collateral in place for a cross-host connection,
   the source not mid-replication to somewhere else, and the target licensed
   (ssl-collateral, source-replication-status, target-license).
4. **Capacity** — the target data and log volumes must have room for the copy
   (target-data-space, target-log-space).

``stop_on_blocking`` is False on purpose: the operator wants the whole picture
in one pass (every blocker and caveat at once), not a run that halts at the
first failure and hides the rest. Read-only throughout — safe to re-run as
often as you like; it always reflects the current state of both systems.
"""

from __future__ import annotations

from exodia.core.runbook import Runbook


class TenantCopyReadinessRunbook(Runbook):
    """Full read-only HANA tenant-copy prerequisite sweep, with one verdict."""

    name = "tenant-copy.hana.readiness"
    description = (
        "Read-only HANA cross-host tenant-copy prerequisite sweep: connectivity, "
        "topology, pre-conditions and capacity, with one aggregate verdict."
    )
    stop_on_blocking = False
    steps = [
        # 1. connectivity — can we talk to both SYSTEMDBs at all?
        "tenant-copy.hana.source-userstore-key",
        "tenant-copy.hana.target-userstore-key",
        "tenant-copy.hana.cross-host-reachability",
        # 2. topology — right source, free target, compatible versions
        "tenant-copy.hana.source-tenant-online",
        "tenant-copy.hana.target-tenant-absent",
        "tenant-copy.hana.version-match",
        # 3. pre-conditions — SSL, replication state, licensing
        "tenant-copy.hana.ssl-collateral",
        "tenant-copy.hana.source-replication-status",
        "tenant-copy.hana.target-license",
        # 4. capacity — room for the copy on the target volumes
        "tenant-copy.hana.target-data-space",
        "tenant-copy.hana.target-log-space",
    ]
