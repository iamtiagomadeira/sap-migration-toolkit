"""Side-scoped tenant-copy readiness runbooks (air-gapped model).

The full ``tenant-copy.hana.readiness`` runbook assumes one host can reach both
SYSTEMDBs. In a real ECS/HEC engagement the source (customer) and target (HEC)
sit in isolated networks, so the consultant runs readiness on ONE side at a
time. These two runbooks scope the checks to a single side so each runs cleanly
where it has access:

* ``tenant-copy.hana.readiness-source`` — the checks that only need the SOURCE
  SYSTEMDB (run in / with access to the customer environment).
* ``tenant-copy.hana.readiness-target`` — the checks that need the TARGET
  SYSTEMDB, including the target→source reachability probe and the revision
  compare (the copy is driven from the target, so these live target-side).

Typical air-gapped flow:

    # in the customer (source) network:
    exodia snapshot tenant-copy.hana.readiness-source --side source \\
        --config source.yaml -o source.json
    # carry source.json across, then in the HEC (target) network:
    exodia compare source.json --against tenant-copy.hana.readiness-target \\
        --side target --config target.yaml
"""

from __future__ import annotations

from exodia.core.runbook import Runbook


class SourceReadinessRunbook(Runbook):
    """Source-side tenant-copy readiness (run with access to the customer system)."""

    name = "tenant-copy.hana.readiness-source"
    description = (
        "Source-side HANA tenant-copy readiness: source SYSTEMDB key, source "
        "tenant online, and source replication status. Run in the customer network."
    )
    stop_on_blocking = False
    steps = [
        "tenant-copy.hana.source-userstore-key",
        "tenant-copy.hana.source-tenant-online",
        "tenant-copy.hana.source-replication-status",
    ]


class TargetReadinessRunbook(Runbook):
    """Target-side tenant-copy readiness (run with access to the HEC system)."""

    name = "tenant-copy.hana.readiness-target"
    description = (
        "Target-side HANA tenant-copy readiness: target SYSTEMDB key, target "
        "tenant free, target→source reachability, revision compatibility, SSL "
        "collateral, license and capacity. Run in the HEC network."
    )
    stop_on_blocking = False
    steps = [
        "tenant-copy.hana.target-userstore-key",
        "tenant-copy.hana.target-tenant-absent",
        "tenant-copy.hana.cross-host-reachability",
        "tenant-copy.hana.version-match",
        "tenant-copy.hana.ssl-collateral",
        "tenant-copy.hana.target-license",
        "tenant-copy.hana.target-data-space",
        "tenant-copy.hana.target-log-space",
    ]
