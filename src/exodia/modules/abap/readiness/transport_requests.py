"""Transport / STMS readiness (SAP MIG tasks ~1054, 1056 — STMS / SPAM).

Reads the transport request header table E070 over RFC and reports how many
transports are still modifiable (not yet released) in the source. A cutover
plan verifies the transport landscape is clean — no half-finished changes —
before the copy. Also surfaces the count so the engineer can confirm STMS is
quiet.

Read-only. WARNs when modifiable (open) transports exist; they are not
necessarily a hard blocker (some are long-lived), so the engineer decides,
but they are surfaced with counts.
"""

from __future__ import annotations

from collections import Counter

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

from . import _rfc

# E070.TRSTATUS (SAP standard): D/L = modifiable, O = release started,
#   R = released, N/A = released (repair). E070.TRFUNCTION: K = workbench,
#   W = customizing, T = transport of copies, etc.
_MODIFIABLE = {"D", "L"}


class TransportRequestsCheck(Check):
    """Report modifiable (unreleased) transport requests on the source (STMS / E070)."""

    name = "abap.readiness.transport-requests"
    description = "Modifiable/unreleased transport requests (STMS / E070)."

    def parameters(self) -> list[ParamSpec]:
        return _rfc.SOURCE_CONN_SPECS

    def run(self, ctx: Context) -> Result:
        side = _rfc.SOURCE
        if not _rfc.has_connection_params(ctx, side):
            return Result.skip(
                self.name, "no source RFC connection params (set source_ashost + credentials)"
            )
        try:
            client = _rfc.get_client(ctx, side)
            rows = _rfc.read_table(
                client, "E070", fields=["TRKORR", "TRFUNCTION", "TRSTATUS"]
            )
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not read transport table E070: {exc}")

        modifiable = [r for r in rows if r.get("TRSTATUS") in _MODIFIABLE]
        by_function: Counter[str] = Counter(r.get("TRFUNCTION", "?") for r in modifiable)
        data = {
            "total_requests": len(rows),
            "modifiable_count": len(modifiable),
            "modifiable_by_function": dict(sorted(by_function.items())),
            "modifiable": [r.get("TRKORR", "") for r in modifiable[:50]],
        }
        if modifiable:
            return Result.warn(
                self.name,
                f"{len(modifiable)} modifiable (unreleased) transport request(s) in source",
                data=data,
            )
        return Result.ok(
            self.name,
            f"no modifiable transport requests ({len(rows)} total, all released)",
            data=data,
        )
