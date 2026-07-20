"""Target capacity checks for cross-host HANA tenant copy (TIA-71).

  7. target data volume free space >= source tenant size + headroom
  8. target log volume has enough free space

The target is the SAP HEC machine that will host the copied tenant. Source size
can be passed explicitly (source_tenant_gb) or, if omitted, the check warns so
the operator sizes it before executing.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class TargetDataSpaceCheck(Check):
    """Target data volume must fit the source tenant plus headroom."""

    name = "tenant-copy.hana.target-data-space"
    description = "Target data free space >= source tenant size + headroom."
    blocking = True

    def run(self, ctx: Context) -> Result:
        path = ctx.get("target_data_path", "/hana/data")
        source_gb = ctx.get("source_tenant_gb")
        headroom = float(ctx.get("headroom_pct", 20))
        cr = c.run(ctx, ["df", "-BG", "--output=avail", str(path)])
        avail = c.avail_gb(cr)
        if avail is None:
            return Result.fail(
                self.name,
                f"could not read free space for {path} on the target",
                detail=cr.stderr or cr.stdout,
            )
        if source_gb is None:
            return Result.warn(
                self.name,
                f"{avail:.0f}G free at {path}; source_tenant_gb not provided to compare",
                data={"avail_gb": avail, "path": path},
            )
        needed = float(source_gb) * (1 + headroom / 100)
        if avail >= needed:
            return Result.ok(
                self.name,
                f"{avail:.0f}G free at {path} (>= {needed:.0f}G needed)",
                data={"avail_gb": avail, "needed_gb": needed, "path": path},
            )
        return Result.fail(
            self.name,
            f"insufficient disk space on target: {avail:.0f}G free at {path}, "
            f"need {needed:.0f}G",
            data={"avail_gb": avail, "needed_gb": needed, "path": path},
        )


class TargetLogSpaceCheck(Check):
    """Target log/trace volume must have enough free space."""

    name = "tenant-copy.hana.target-log-space"
    description = "Target log/trace volume free space >= threshold."
    blocking = True

    def run(self, ctx: Context) -> Result:
        path = ctx.get("target_log_path", "/hana/log")
        min_gb = float(ctx.get("log_min_gb", 20))
        cr = c.run(ctx, ["df", "-BG", "--output=avail", str(path)])
        avail = c.avail_gb(cr)
        if avail is None:
            return Result.fail(
                self.name,
                f"could not read free space for {path} on the target",
                detail=cr.stderr or cr.stdout,
            )
        if avail >= min_gb:
            return Result.ok(
                self.name,
                f"{avail:.0f}G free at {path} (>= {min_gb:.0f}G)",
                data={"avail_gb": avail, "min_gb": min_gb, "path": path},
            )
        return Result.fail(
            self.name,
            f"insufficient disk space for logs/traces on target: {avail:.0f}G at "
            f"{path}, need {min_gb:.0f}G",
            data={"avail_gb": avail, "min_gb": min_gb, "path": path},
        )
