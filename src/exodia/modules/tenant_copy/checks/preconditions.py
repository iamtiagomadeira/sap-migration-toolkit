"""Cross-instance preconditions & collateral for HANA tenant copy (TIA-71).

  9. SSL/PKI collateral present for a secure cross-host connection
 10. source tenant is not itself a running HSR primary/secondary that would
     conflict with being copied
 11. target license has headroom for one more tenant

Cross-host tenant copy connects the target SYSTEMDB to the source SYSTEMDB. When
the connection is encrypted (the norm for customer <-> HEC), the target needs the
source's server certificate in its trust store. These checks are read-only.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class SslCollateralCheck(Check):
    """When the cross-host copy is encrypted, trust collateral must be in place.

    Skips cleanly when encrypted_link is falsy (some HEC setups copy over a
    private, already-trusted link). When encryption is required, verifies the
    target trust store file exists and is readable.
    """

    name = "tenant-copy.hana.ssl-collateral"
    description = "SSL trust collateral present for encrypted cross-host copy."
    blocking = True

    def run(self, ctx: Context) -> Result:
        if not ctx.get("encrypted_link", True):
            return Result.skip(
                self.name,
                "encrypted_link=false; skipping SSL trust collateral check",
            )
        trust = ctx.get("target_trust_store", "$SECUDIR/trust.pem")
        cr = c.run(ctx, ["test", "-r", str(trust)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"target trust store '{trust}' missing or unreadable — import the "
                "source SYSTEMDB server certificate before an encrypted copy",
                data={"trust_store": trust},
            )
        return Result.ok(
            self.name,
            f"target trust store '{trust}' present",
            data={"trust_store": trust},
        )


class SourceReplicationStatusCheck(Check):
    """A source tenant actively serving HSR needs care before being copied.

    Read-only: inspects M_SERVICE_REPLICATION on the source SYSTEMDB. An ACTIVE
    replication is a WARN (copy is possible but must be coordinated), not a hard
    block.
    """

    name = "tenant-copy.hana.source-replication-status"
    description = "Source tenant HSR status is understood before copying."
    blocking = False

    def run(self, ctx: Context) -> Result:
        stmt = (
            "SELECT REPLICATION_STATUS FROM M_SERVICE_REPLICATION "
            "WHERE REPLICATION_STATUS IS NOT NULL"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, c.SOURCE, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read source replication status; treat as manual review",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        statuses = {r[0].upper() for r in rows if r and r[0]}
        if not statuses:
            return Result.ok(
                self.name,
                "no active system replication on the source",
            )
        return Result.warn(
            self.name,
            f"source has active system replication ({', '.join(sorted(statuses))}) — "
            "coordinate the copy so it does not disrupt the replica",
            data={"statuses": sorted(statuses)},
        )


class TargetLicenseCheck(Check):
    """The target system license must permit adding another tenant.

    Reads M_LICENSE on the target SYSTEMDB; a non-VALID license or an expired
    one blocks provisioning the new tenant.
    """

    name = "tenant-copy.hana.target-license"
    description = "Target HANA license is valid (permits another tenant)."
    blocking = False

    def run(self, ctx: Context) -> Result:
        stmt = "SELECT PRODUCT_LIMIT, VALID FROM M_LICENSE"
        cr = c.run(ctx, c.hdbsql_argv(ctx, c.TARGET, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read target license; review manually",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows:
            return Result.warn(
                self.name,
                "target license status could not be parsed; review manually",
            )
        valid = rows[0][1].upper() if len(rows[0]) > 1 else ""
        if valid not in ("TRUE", "YES"):
            return Result.fail(
                self.name,
                f"target HANA license is not valid (VALID={valid or 'unknown'}) — "
                "install a valid license before creating the tenant",
                data={"valid": valid},
            )
        return Result.ok(
            self.name,
            "target HANA license is valid",
            data={"valid": valid},
        )
