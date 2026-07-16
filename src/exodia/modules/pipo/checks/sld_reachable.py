"""pipo.sld-reachable — verify the System Landscape Directory is reachable.

The SLD (System Landscape Directory) is central to PI/PO: the Integration
Directory, the SLD data supplier, and technical system registration all depend
on it. After a Java system copy the SLD hostname/port usually changes and the
data supplier must be re-pointed. This read-only check confirms the configured
SLD endpoint answers, so the operator knows what to re-register post-copy.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import redact


class SLDReachableCheck(Check):
    """Configured SLD HTTP(S) endpoint is reachable."""

    name = "pipo.sld-reachable"
    description = "SLD endpoint reachable (HTTP status) for data-supplier registration."
    blocking = False

    def run(self, ctx: Context) -> Result:
        host = ctx.get("sld_host")
        if not host:
            return Result.skip(
                self.name,
                "no sld_host configured — set params.sld_host to validate the SLD endpoint",
            )
        port = str(ctx.get("sld_port", "50000"))
        scheme = "https" if ctx.get("sld_https") else "http"
        url = f"{scheme}://{host}:{port}/sld"

        runner = ctx.runner()
        # curl with -sS -o /dev/null and %{http_code}; head-only, no body pulled.
        cr = runner.run(
            [
                "curl",
                "-sS",
                "-m",
                "10",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                url,
            ]
        )
        if not cr.ok:
            return Result.fail(
                self.name,
                f"SLD endpoint {url} not reachable",
                detail=redact(cr.stderr),
                data={"url": url},
            )
        code = cr.stdout.strip()
        # SLD typically answers 200 (open) or 401/403 (auth required) — all mean
        # the service is up. A 000/5xx means it is not answering properly.
        if code in ("200", "301", "302", "401", "403"):
            return Result.ok(
                self.name,
                f"SLD reachable at {url} (HTTP {code})",
                data={"url": url, "http_code": code},
            )
        return Result.fail(
            self.name,
            f"SLD at {url} returned unexpected HTTP {code or '000'}",
            data={"url": url, "http_code": code},
        )
