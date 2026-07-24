"""System copy based on HANA System Replication (HSR).

The HSR method sets up the target as a replication secondary of the source, lets
it catch up, then takes it over as an independent system. Grounded in SAP HSR
requirements:

* **Same major HANA version** — the secondary must run the same or a compatible
  (equal/higher within the allowed window) revision as the primary; SAP does not
  support replicating to a lower revision.
* **Replication ports reachable** — the primary opens ports 4<nn>01-4<nn>07
  (nn = instance) to the secondary; the network path must be open.
* **log_mode = normal** — system replication requires the primary to run in
  ``log_mode=normal`` (not overwrite) so logs can be shipped.
* **Distinct SIDs / hosts** — primary and secondary must be different hosts
  (and normally share the SID for a homogeneous copy).

Every check is read-only.
"""

from __future__ import annotations

import re

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)*)")

# --------------------------------------------------------------------------- #
# Parameter specs
# --------------------------------------------------------------------------- #

PRIMARY_KEY = ParamSpec(
    "primary_userstore_key",
    "Primary SYSTEMDB hdbuserstore key",
    default="SYSTEMDB",
    help="hdbsql -U key for the PRIMARY (source) SYSTEMDB.",
)
SECONDARY_KEY = ParamSpec(
    "secondary_userstore_key",
    "Secondary SYSTEMDB hdbuserstore key",
    default="SYSTEMDB",
    help="hdbsql -U key for the SECONDARY (target) SYSTEMDB.",
)
SECONDARY_HOST = ParamSpec(
    "secondary_host",
    "Secondary host",
    help="Target host that becomes the replication secondary.",
)
INSTANCE = ParamSpec(
    "instance",
    "HANA instance number",
    default="00",
    help="Two digits; replication ports 4<nn>01-07 are derived from it.",
)


def _run(ctx: Context, argv: list[str], timeout: int = 60):  # type: ignore[no-untyped-def]
    return ctx.runner().run(argv, timeout=timeout)


def _hdbsql(key: str, stmt: str) -> list[str]:
    return ["hdbsql", "-U", str(key), "-x", "-a", "-j", stmt]


def _parse_version(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    m = _VERSION_RE.search(text)
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


class VersionCompatibilityCheck(Check):
    """Primary and secondary must run compatible HANA revisions.

    SAP requires the secondary revision >= primary revision (never lower).
    """

    name = "hsr.version-compatibility"
    description = "Secondary HANA revision is compatible with the primary."
    title = "HSR — Secondary Revision Compatibility"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [PRIMARY_KEY, SECONDARY_KEY]

    def _version(self, ctx: Context, key: str) -> tuple[int, ...] | None:
        cr = _run(ctx, _hdbsql(key, "SELECT VERSION FROM M_DATABASE"))
        return _parse_version(cr.stdout) if cr.ok else None

    def run(self, ctx: Context) -> Result:
        pkey = ctx.get("primary_userstore_key") or "SYSTEMDB"
        skey = ctx.get("secondary_userstore_key") or "SYSTEMDB"
        pv = self._version(ctx, pkey)
        sv = self._version(ctx, skey)
        if pv is None or sv is None:
            return Result.skip(
                self.name,
                "could not read version from one/both systems (keys reachable?)",
                data={"primary": pv, "secondary": sv},
            )
        if sv < pv:
            return Result.fail(
                self.name,
                f"secondary {sv} is LOWER than primary {pv} — HSR does not support "
                "replicating to a lower revision; upgrade the secondary first",
                data={"primary": list(pv), "secondary": list(sv)},
            )
        return Result.ok(
            self.name,
            f"secondary {sv} is compatible with primary {pv}",
            data={"primary": list(pv), "secondary": list(sv)},
        )


class LogModeNormalCheck(Check):
    """The primary must run in log_mode=normal for replication to ship logs."""

    name = "hsr.log-mode-normal"
    description = "Primary runs in log_mode=normal (required for HSR)."
    title = "HSR — Primary log_mode=normal"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [PRIMARY_KEY]

    def run(self, ctx: Context) -> Result:
        key = ctx.get("primary_userstore_key") or "SYSTEMDB"
        stmt = (
            "SELECT VALUE FROM M_INIFILE_CONTENTS WHERE FILE_NAME='global.ini' "
            "AND KEY='log_mode'"
        )
        cr = _run(ctx, _hdbsql(key, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read log_mode from primary global.ini",
                detail=cr.stderr or cr.stdout,
            )
        value = cr.stdout.strip().strip('"').lower()
        if "normal" not in value:
            return Result.fail(
                self.name,
                f"primary log_mode is '{value or 'unknown'}' — set log_mode=normal "
                "and take a full data backup before enabling replication",
                data={"log_mode": value},
            )
        return Result.ok(self.name, "primary log_mode=normal", data={"log_mode": value})


class ReplicationPortsReachableCheck(Check):
    """The secondary must reach the primary's system-replication ports."""

    name = "hsr.replication-ports-reachable"
    description = "Secondary can reach the primary replication ports."
    title = "HSR — Replication Ports Reachable (4<nn>01-07)"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [SECONDARY_HOST, INSTANCE]

    def run(self, ctx: Context) -> Result:
        host = ctx.get("secondary_host") or ctx.host
        inst = str(ctx.get("instance") or "00").zfill(2)
        if not host:
            return Result.skip(
                self.name, "no secondary_host/host given; cannot probe ports"
            )
        # HSR uses 4<nn>01..4<nn>07; probe the first as a representative.
        port = int(f"4{inst}01")
        cr = _run(ctx, ["nc", "-z", "-w", "5", str(host), str(port)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"cannot reach {host}:{port} — open replication ports 4{inst}01-07 "
                "between primary and secondary",
                data={"host": host, "port": port},
            )
        return Result.ok(
            self.name,
            f"replication port {host}:{port} reachable",
            data={"host": host, "port": port},
        )


class DistinctHostsCheck(Check):
    """Primary and secondary must be different hosts."""

    name = "hsr.distinct-hosts"
    description = "Primary and secondary are different hosts."
    title = "HSR — Distinct Primary/Secondary Hosts"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [SECONDARY_HOST]

    def run(self, ctx: Context) -> Result:
        secondary = ctx.get("secondary_host")
        primary = ctx.host
        if not secondary:
            return Result.skip(self.name, "secondary_host not provided")
        if primary and secondary and primary.strip().lower() == secondary.strip().lower():
            return Result.fail(
                self.name,
                f"primary and secondary are the same host ({primary}) — HSR requires "
                "two distinct hosts",
                data={"primary": primary, "secondary": secondary},
            )
        return Result.ok(
            self.name,
            f"primary ({primary or '?'}) and secondary ({secondary}) are distinct",
            data={"primary": primary, "secondary": secondary},
        )


class ReplicationStatusCheck(Check):
    """Read the real replication state via systemReplicationStatus.py.

    ``hdbnsutil -sr_state`` and ``systemReplicationStatus.py`` report the live
    replication status. Before a takeover the secondary must be fully caught up
    (overall status ``ACTIVE``); ``SYNCING``/``ERROR`` means a takeover would
    lose data. This is the difference between "a port is open" and "replication
    is actually healthy".

    Read-only: it only queries status.
    """

    name = "hsr.replication-status"
    description = "System replication overall status is ACTIVE (caught up)."
    title = "HSR — Replication Status ACTIVE (hdbnsutil -sr_state)"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [INSTANCE]

    def run(self, ctx: Context) -> Result:
        inst = str(ctx.get("instance") or "00").zfill(2)
        # The canonical tool returns exit code 15 when ACTIVE, and prints an
        # "overall system replication status: <STATE>" line.
        script = f"/usr/sap/*/HDB{inst}/exe/python_support/systemReplicationStatus.py"
        cr = _run(ctx, ["sh", "-c", f"python3 {script} 2>&1 || true"])
        text = (cr.stdout or "") + (cr.stderr or "")
        if not text.strip():
            # Fall back to hdbnsutil which every HANA install ships.
            cr = _run(ctx, ["sh", "-c", "hdbnsutil -sr_state 2>&1 || true"])
            text = (cr.stdout or "") + (cr.stderr or "")
        if not text.strip():
            return Result.skip(
                self.name,
                "could not read replication status (run on the primary as <sid>adm; "
                "systemReplicationStatus.py / hdbnsutil not reachable)",
            )
        low = text.lower()
        m = re.search(r"overall system replication status:\s*(\w+)", low)
        state = m.group(1).upper() if m else None
        if state == "ACTIVE" or "mode: primary" in low and "active" in low:
            return Result.ok(
                self.name,
                f"replication overall status is {state or 'ACTIVE'} (secondary caught up)",
                data={"status": state or "ACTIVE"},
            )
        if state in {"SYNCING", "INITIALIZING", "SYNCING_FULL", "UNKNOWN", "ERROR"}:
            return Result.fail(
                self.name,
                f"replication status is {state}, not ACTIVE — do NOT take over until "
                "the secondary is fully caught up or you will lose data",
                data={"status": state},
            )
        return Result.warn(
            self.name,
            "could not parse a clear replication status; inspect "
            "systemReplicationStatus.py output manually before takeover",
            detail=text.strip()[:500],
            data={"status": state},
        )


class DataBackupExistsCheck(Check):
    """A full data backup must exist before replication can be enabled.

    HANA refuses to register a secondary unless the primary has at least one
    full data backup (the log position the secondary starts from). This queries
    the backup catalog rather than assuming.

    Read-only.
    """

    name = "hsr.data-backup-exists"
    description = "Primary has a full data backup (required to enable HSR)."
    title = "HSR — Primary Full Data Backup Exists"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [PRIMARY_KEY]

    def run(self, ctx: Context) -> Result:
        key = ctx.get("primary_userstore_key") or "SYSTEMDB"
        stmt = (
            "SELECT COUNT(*) FROM M_BACKUP_CATALOG "
            "WHERE ENTRY_TYPE_NAME='complete data backup' AND STATE_NAME='successful'"
        )
        cr = _run(ctx, _hdbsql(key, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read the backup catalog on the primary",
                detail=cr.stderr or cr.stdout,
            )
        digits = re.search(r"\d+", cr.stdout or "")
        count = int(digits.group(0)) if digits else 0
        if count == 0:
            return Result.fail(
                self.name,
                "primary has no successful full data backup — take one "
                "(BACKUP DATA USING FILE) before enabling system replication",
                data={"backup_count": count},
            )
        return Result.ok(
            self.name,
            f"primary has {count} successful full data backup(s)",
            data={"backup_count": count},
        )
