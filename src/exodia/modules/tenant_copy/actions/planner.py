"""Planner for HANA cross-host tenant copy — builds the SQL/argv, no side effects.

Kept separate from the action so the command construction is trivially unit-
testable without a runner. Two cross-host methods are supported, selected by the
``copy_method`` param:

* ``replication`` (default) — the modern, low-downtime path. On the TARGET
  SYSTEMDB, create the tenant as a replica of the source over system replication,
  let it sync, then hand control over. Uses::

      CREATE DATABASE <target> AS REPLICA OF <source> AT '<src_host>:<src_port>'

  followed by a monitored sync and a finalize step. This is the method used when
  copying a customer tenant onto SAP HEC machines.

* ``backup`` — the classic path: recover the source tenant's backup set into a
  freshly created target tenant (``RECOVER DATABASE ... USING ...``). Chosen when
  no live network path to the source exists and only a backup set is shipped.

Credentials never appear on the command line: hdbsql authenticates through the
secure user store (``hdbsql -U <KEY>``). Only the secret-free SQL is passed.

References (cite by number only): SAP Note 2101244 (HANA administration / MDC),
2456657 (system replication), 1642148 (backup/recovery).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class TenantCopyPlanError(ValueError):
    """Raised when the copy cannot be planned from the given parameters."""


@dataclass(frozen=True)
class PlannedCommand:
    """A single command the action would run, with a human description."""

    argv: list[str]
    describe: str

    @property
    def display(self) -> str:
        return self.describe


@dataclass(frozen=True)
class TenantCopyPlan:
    """The ordered commands + metadata for a tenant copy."""

    method: str
    source_tenant: str
    target_tenant: str
    commands: list[PlannedCommand] = field(default_factory=list)
    source_host: str | None = None
    source_port: int | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_TENANT_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


def _validate_tenant(name: str | None, role: str) -> str:
    if not name:
        raise TenantCopyPlanError(f"{role} tenant name is required")
    if name.upper() == "SYSTEMDB":
        raise TenantCopyPlanError(f"{role} tenant cannot be SYSTEMDB")
    if not (name[0].isalpha() and set(name) <= _TENANT_SAFE and len(name) <= 8):
        raise TenantCopyPlanError(
            f"invalid {role} tenant name '{name}' "
            "(<=8 chars, letter first, alphanumeric/underscore)"
        )
    return name


def hdbsql_argv(userstore_key: str, sql: str) -> list[str]:
    """Build an hdbsql argv against a SYSTEMDB via a user store key (no password)."""
    return ["hdbsql", "-U", str(userstore_key), "-x", "-a", "-j", sql]


def source_sql_port(source_instance: str | None) -> int | None:
    """The source SYSTEMDB SQL port (3<nn>13) used as the replication endpoint."""
    if not source_instance:
        return None
    nn = str(source_instance).zfill(2)
    if not (len(nn) == 2 and nn.isdigit()):
        return None
    return int(f"3{nn}13")


# --------------------------------------------------------------------------- #
# Plan builders
# --------------------------------------------------------------------------- #


def build_replication_plan(
    *,
    target_key: str,
    source_tenant: str,
    target_tenant: str,
    source_host: str | None,
    source_port: int | None,
) -> TenantCopyPlan:
    """Build the replication-based tenant copy plan (run on the TARGET SYSTEMDB)."""
    src = _validate_tenant(source_tenant, "source")
    tgt = _validate_tenant(target_tenant, "target")
    if not source_host:
        raise TenantCopyPlanError(
            "replication method requires source_host (the customer SYSTEMDB host)"
        )
    if not source_port:
        raise TenantCopyPlanError(
            "replication method requires a source SQL port "
            "(set source_instance so it can be derived, or source_port)"
        )
    endpoint = f"{source_host}:{source_port}"
    create = (
        f"CREATE DATABASE {tgt} AS REPLICA OF {src} AT '{endpoint}'"
    )
    finalize = f"ALTER SYSTEM STOP DATABASE REPLICATION FOR {tgt}"
    return TenantCopyPlan(
        method="replication",
        source_tenant=src,
        target_tenant=tgt,
        source_host=source_host,
        source_port=source_port,
        commands=[
            PlannedCommand(
                argv=hdbsql_argv(target_key, create),
                describe=(
                    f"CREATE DATABASE {tgt} AS REPLICA OF {src} AT '{endpoint}' "
                    "(start cross-host system replication)"
                ),
            ),
            PlannedCommand(
                argv=hdbsql_argv(target_key, finalize),
                describe=(
                    f"finalize: stop replication for {tgt} once synced "
                    "(promotes the replica to an independent tenant)"
                ),
            ),
        ],
    )


def build_backup_plan(
    *,
    target_key: str,
    source_tenant: str,
    target_tenant: str,
    catalog_path: str,
    data_path: str,
    log_path: str,
) -> TenantCopyPlan:
    """Build the backup/recovery-based tenant copy plan (run on the TARGET SYSTEMDB)."""
    src = _validate_tenant(source_tenant, "source")
    tgt = _validate_tenant(target_tenant, "target")
    if not (catalog_path and data_path):
        raise TenantCopyPlanError(
            "backup method requires catalog_path and data_path (the shipped backup set)"
        )
    create = f"CREATE DATABASE {tgt} SYSTEM USER PASSWORD_UNSET"
    recover = (
        f"RECOVER DATABASE FOR {tgt} UNTIL TIMESTAMP '9999-01-01 00:00:00' "
        f"CLEAR LOG USING CATALOG PATH ('{catalog_path}') "
        f"USING LOG PATH ('{log_path or data_path}') "
        f"USING DATA PATH ('{data_path}') CHECK ACCESS USING FILE"
    )
    return TenantCopyPlan(
        method="backup",
        source_tenant=src,
        target_tenant=tgt,
        commands=[
            PlannedCommand(
                argv=hdbsql_argv(target_key, create),
                describe=f"CREATE DATABASE {tgt} (empty target tenant)",
            ),
            PlannedCommand(
                argv=hdbsql_argv(target_key, recover),
                describe=(
                    f"RECOVER DATABASE FOR {tgt} from the source backup set "
                    f"(catalog: {catalog_path})"
                ),
            ),
        ],
    )
