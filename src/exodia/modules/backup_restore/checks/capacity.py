"""HANA target capacity checks (TIA-57 #5, #12).

  5. free space on target >= source data size (reuses core.free-space df logic)
 12. space for logs/traces on the target
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class TargetDataSpaceCheck(Check):
    """Target data volume free space must be >= source data size + headroom."""

    name = "backup-restore.hana.target-data-space"
    description = "Target free space >= source data size (df-based)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        path = ctx.get("data_path", "/hana/data")
        source_gb = ctx.get("source_data_gb")
        headroom = float(ctx.get("headroom_pct", 20))
        cr = c.run(ctx, ["df", "-BG", "--output=avail", str(path)])
        avail = c.avail_gb(cr)
        if avail is None:
            return Result.fail(
                self.name,
                f"could not read free space for {path}",
                detail=cr.stderr or cr.stdout,
            )
        if source_gb is None:
            return Result.warn(
                self.name,
                f"{avail:.0f}G free at {path}; source_data_gb not provided to compare",
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
            f"insufficient disk space: {avail:.0f}G free at {path}, need {needed:.0f}G",
            data={"avail_gb": avail, "needed_gb": needed, "path": path},
        )


class TargetLogSpaceCheck(Check):
    """Target log/trace volume must have enough free space."""

    name = "backup-restore.hana.target-log-space"
    description = "Target log/trace volume free space >= threshold."
    blocking = True

    def run(self, ctx: Context) -> Result:
        path = ctx.get("log_path", "/hana/log")
        min_gb = float(ctx.get("log_min_gb", 20))
        cr = c.run(ctx, ["df", "-BG", "--output=avail", str(path)])
        avail = c.avail_gb(cr)
        if avail is None:
            return Result.fail(
                self.name,
                f"could not read free space for {path}",
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
            f"insufficient disk space for logs/traces: {avail:.0f}G at {path}, need {min_gb:.0f}G",
            data={"avail_gb": avail, "min_gb": min_gb, "path": path},
        )
