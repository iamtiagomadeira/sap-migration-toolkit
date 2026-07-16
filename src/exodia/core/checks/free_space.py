"""Example free-space check — proves the core plumbing end-to-end.

This is a real, working check used by the smoke test. Methodology modules will
follow this exact pattern.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result


class FreeSpaceCheck(Check):
    """Verify a filesystem path has at least N GB free on the target."""

    name = "core.free-space"
    description = "Filesystem free space >= threshold (df-based)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        path = ctx.get("path", "/")
        min_gb = float(ctx.get("min_gb", 10))
        runner = ctx.runner()
        cr = runner.run(["df", "-BG", "--output=avail", path])
        if not cr.ok:
            return Result.fail(self.name, f"could not read free space for {path}", detail=cr.stderr)
        lines = [ln.strip() for ln in cr.stdout.splitlines() if ln.strip()]
        if len(lines) < 2:
            return Result.fail(self.name, f"unexpected df output for {path}", detail=cr.stdout)
        avail_gb = float(lines[-1].rstrip("G"))
        if avail_gb >= min_gb:
            return Result.ok(
                self.name,
                f"{avail_gb:.0f}G available at {path} (>= {min_gb:.0f}G)",
                data={"avail_gb": avail_gb, "min_gb": min_gb, "path": path},
            )
        return Result.fail(
            self.name,
            f"only {avail_gb:.0f}G available at {path}, need {min_gb:.0f}G",
            data={"avail_gb": avail_gb, "min_gb": min_gb, "path": path},
        )
