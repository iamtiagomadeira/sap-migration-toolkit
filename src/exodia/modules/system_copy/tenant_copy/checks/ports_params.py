"""HANA service port + replication-parameter checks (tenant copy, from the COP).

These map two "SOURCE/TARGET - Preparation" runbook sections onto read-only
Exodia checks:

* ``ports`` — extract the HANA service ports (Name Server / Index Server / XS)
  from ``SYS_DATABASES.M_SERVICES`` on each side, so the engineer knows exactly
  which ports the replication endpoint and the services use (the copy connects
  to the source SYSTEMDB SQL port, e.g. 3<nn>01/31001).
* ``replication-parameters`` — read the system-replication / SSL / persistence
  parameters from ``SYS_DATABASES.M_INIFILE_CONTENTS`` (the exact ``global.ini``
  keys the COP inspects before HSR), so the current SSL and log-shipping config
  is captured as evidence and can be compared source vs target.

Both are read-only (SELECT only). Source-side and target-side variants are
exposed so each runs where it has network access in an air-gapped engagement.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from . import _common as c

# The exact global.ini / indexserver.ini keys the COP inspects before HSR.
_REPLICATION_PARAM_SQL = (
    "SELECT FILE_NAME, SECTION, KEY, VALUE FROM SYS_DATABASES.M_INIFILE_CONTENTS WHERE "
    "(FILE_NAME='global.ini' AND SECTION='communication' AND KEY='listeninterface') OR "
    "(FILE_NAME='global.ini' AND SECTION='communication' AND KEY='ssl') OR "
    "(FILE_NAME='global.ini' AND SECTION='system_replication_communication' AND KEY='enable_ssl') OR "
    "(FILE_NAME='global.ini' AND SECTION='multidb' AND KEY='enforce_ssl_database_replication') OR "
    "(FILE_NAME='global.ini' AND SECTION='system_replication' AND KEY='enable_log_retention') OR "
    "(FILE_NAME='global.ini' AND SECTION='system_replication' AND KEY='replication_port_offset') OR "
    "(FILE_NAME='global.ini' AND SECTION='persistence' AND KEY='log_mode') "
    "ORDER BY FILE_NAME, SECTION, KEY"
)


class _PortsCheck(Check):
    """Shared logic: extract HANA service ports from M_SERVICES for a side."""

    side = c.SOURCE
    phase = Phase.PREPARATION

    def parameters(self) -> list[ParamSpec]:
        key = c.SOURCE_USERSTORE_KEY if self.side == c.SOURCE else c.TARGET_USERSTORE_KEY
        return [key]

    def run(self, ctx: Context) -> Result:
        stmt = (
            "SELECT DATABASE_NAME, SERVICE_NAME, PORT, SQL_PORT "
            "FROM SYS_DATABASES.M_SERVICES ORDER BY DATABASE_NAME, SERVICE_NAME"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, self.side, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not read {self.side} HANA service ports (M_SERVICES)",
                detail=cr.stderr or cr.stdout,
                facts={"Side": self.side.capitalize(), "Readable": "No"},
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        services = [
            {
                "database": r[0] if len(r) > 0 else "",
                "service": r[1] if len(r) > 1 else "",
                "port": r[2] if len(r) > 2 else "",
                "sql_port": r[3] if len(r) > 3 else "",
            }
            for r in rows
            if r and r[0]
        ]
        # Surface the SYSTEMDB SQL port (the replication endpoint the copy uses).
        sql_ports = sorted({s["sql_port"] for s in services if s["sql_port"] and s["sql_port"] != "0"})
        if not services:
            return Result.warn(
                self.name,
                f"M_SERVICES returned no {self.side} services",
                facts={"Side": self.side.capitalize(), "Services": "0"},
            )
        return Result.ok(
            self.name,
            f"{self.side} HANA ports captured: {len(services)} service(s), "
            f"SQL port(s): {', '.join(sql_ports) or 'n/a'}",
            data={"side": self.side, "services": services, "sql_ports": sql_ports},
            facts={
                "Side": self.side.capitalize(),
                "Services": str(len(services)),
                "SQL Ports": ", ".join(sql_ports) or "n/a",
            },
        )


class SourcePortsCheck(_PortsCheck):
    """Extract the source HANA service ports (M_SERVICES)."""

    name = "tenant-copy.hana.source-ports"
    description = "Extract source HANA service ports (M_SERVICES)."
    title = "Source HANA Service Ports (M_SERVICES)"
    side = c.SOURCE


class TargetPortsCheck(_PortsCheck):
    """Extract the target HANA service ports (M_SERVICES)."""

    name = "tenant-copy.hana.target-ports"
    description = "Extract target HANA service ports (M_SERVICES)."
    title = "Target HANA Service Ports (M_SERVICES)"
    side = c.TARGET


class _ReplicationParamsCheck(Check):
    """Shared logic: read the HSR/SSL/persistence parameters for a side."""

    side = c.SOURCE
    phase = Phase.PREPARATION

    def parameters(self) -> list[ParamSpec]:
        key = c.SOURCE_USERSTORE_KEY if self.side == c.SOURCE else c.TARGET_USERSTORE_KEY
        return [key]

    def run(self, ctx: Context) -> Result:
        cr = c.run(ctx, c.hdbsql_argv(ctx, self.side, _REPLICATION_PARAM_SQL))
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not read {self.side} replication parameters (M_INIFILE_CONTENTS)",
                detail=cr.stderr or cr.stdout,
                facts={"Side": self.side.capitalize(), "Readable": "No"},
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        params = {}
        for r in rows:
            if len(r) >= 4:
                params[f"{r[1]}/{r[2]}"] = r[3]
        # Pull the SSL-relevant keys out for a clear headline.
        ssl_repl = params.get("system_replication_communication/enable_ssl", "?")
        ssl_comm = params.get("communication/ssl", "?")
        enforce = params.get("multidb/enforce_ssl_database_replication", "?")
        log_mode = params.get("persistence/log_mode", "?")
        return Result.ok(
            self.name,
            f"{self.side} replication parameters captured "
            f"(SR enable_ssl={ssl_repl}, comm ssl={ssl_comm}, log_mode={log_mode})",
            data={"side": self.side, "parameters": params},
            facts={
                "Side": self.side.capitalize(),
                "SR enable_ssl": str(ssl_repl),
                "communication/ssl": str(ssl_comm),
                "enforce_ssl_db_replication": str(enforce),
                "log_mode": str(log_mode),
            },
        )


class SourceReplicationParamsCheck(_ReplicationParamsCheck):
    """Read source HSR/SSL/persistence parameters (M_INIFILE_CONTENTS)."""

    name = "tenant-copy.hana.source-replication-parameters"
    description = "Read source HSR/SSL/persistence parameters (M_INIFILE_CONTENTS)."
    title = "Source SYSTEMDB Replication & SSL Parameters"
    side = c.SOURCE


class TargetReplicationParamsCheck(_ReplicationParamsCheck):
    """Read target HSR/SSL/persistence parameters (M_INIFILE_CONTENTS)."""

    name = "tenant-copy.hana.target-replication-parameters"
    description = "Read target HSR/SSL/persistence parameters (M_INIFILE_CONTENTS)."
    title = "Target SYSTEMDB Replication & SSL Parameters"
    side = c.TARGET
