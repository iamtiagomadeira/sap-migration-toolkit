"""Connectivity checks for cross-host HANA tenant copy (TIA-71).

  4. source SYSTEMDB reachable + hdbuserstore key usable
  5. target SYSTEMDB reachable + hdbuserstore key usable
  6. cross-host network path from target to source SYSTEMDB SQL port

The tenant copy is driven from the TARGET SYSTEMDB, which opens a connection back
to the SOURCE SYSTEMDB — so the target must be able to reach the source's SQL
port. All checks are read-only.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class _UserstoreKeyCheck(Check):
    """Shared logic: an hdbuserstore key on a given side must be usable."""

    side = c.SOURCE  # overridden by subclasses
    blocking = True

    def run(self, ctx: Context) -> Result:
        key = c.userstore_key(ctx, self.side)
        cr = c.run(ctx, ["hdbuserstore", "LIST", str(key)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"{self.side} hdbuserstore key '{key}' not found — cannot connect",
                detail=cr.stderr or cr.stdout,
            )
        haystack = cr.stdout.upper()
        if "KEY " not in haystack and str(key).upper() not in haystack:
            return Result.fail(
                self.name,
                f"{self.side} hdbuserstore key '{key}' not present in store listing",
                detail=cr.stdout,
            )
        return Result.ok(
            self.name,
            f"{self.side} hdbuserstore key '{key}' present",
            data={"side": self.side, "key": key},
        )


class SourceUserstoreKeyCheck(_UserstoreKeyCheck):
    """The source SYSTEMDB connect key must exist."""

    name = "tenant-copy.hana.source-userstore-key"
    description = "Source SYSTEMDB hdbuserstore key present and usable."
    side = c.SOURCE


class TargetUserstoreKeyCheck(_UserstoreKeyCheck):
    """The target SYSTEMDB connect key must exist."""

    name = "tenant-copy.hana.target-userstore-key"
    description = "Target SYSTEMDB hdbuserstore key present and usable."
    side = c.TARGET


class CrossHostReachabilityCheck(Check):
    """The target must reach the source SYSTEMDB SQL port (33013 for inst 30, etc).

    Tenant copy is initiated on the target and connects to the source, so this
    validates the target -> source network path. The source SQL system port is
    3<nn>13 where <nn> is the source instance number.
    """

    name = "tenant-copy.hana.cross-host-reachability"
    description = "Target can reach the source SYSTEMDB SQL port."
    blocking = True

    def run(self, ctx: Context) -> Result:
        source_host = ctx.get("source_host")
        source_inst = c.instance(ctx, c.SOURCE)
        if not source_host:
            return Result.skip(
                self.name,
                "source_host not provided; cannot probe cross-host reachability",
            )
        if not c.is_valid_instance(source_inst):
            return Result.skip(
                self.name,
                "source instance number not provided/invalid; cannot derive port",
            )
        assert source_inst is not None  # nosec B101 - narrowed by guard above
        port = int(f"3{source_inst}13")
        cr = c.run(ctx, ["nc", "-z", "-w", "5", str(source_host), str(port)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"target cannot reach source SYSTEMDB at {source_host}:{port} — "
                "check firewall / security groups between HEC and customer network",
                data={"source_host": source_host, "port": port},
            )
        return Result.ok(
            self.name,
            f"target can reach source SYSTEMDB at {source_host}:{port}",
            data={"source_host": source_host, "port": port},
        )
