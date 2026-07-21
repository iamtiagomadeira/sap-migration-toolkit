"""RFC destination inventory (SAP MIG post-processing ~task 4xxx — SM59).

Reads the RFC destination table RFCDES over RFC and inventories the configured
destinations by type. After a system copy, RFC destinations pointing at the
*source* landscape must be repointed (BDLS / SM59 rework); capturing the full
list — especially type '3' (ABAP) and 'T' (external/TCP) destinations that
carry hardcoded hostnames — is the evidence a cutover plan records so nothing
is missed in post-processing.

Read-only. Never FAILs on content (destinations are facts to inventory, not a
pass/fail gate); FAILs only if the table cannot be read.
"""

from __future__ import annotations

from collections import Counter

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

from . import _rfc

# RFCDES.RFCTYPE (SAP standard): 3 = ABAP connection, T = TCP/IP (external),
#   H = HTTP to ABAP, G = HTTP to external, L = logical, I = internal.
_TYPE_LABELS = {
    "3": "ABAP",
    "T": "TCP/external",
    "H": "HTTP-ABAP",
    "G": "HTTP-external",
    "L": "logical",
    "I": "internal",
}


class RfcDestinationsCheck(Check):
    """Inventory RFC destinations (SM59 / RFCDES) for post-copy repointing."""

    name = "abap.readiness.rfc-destinations"
    description = "Inventory RFC destinations by type for post-copy repointing (SM59 / RFCDES)."

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
                client, "RFCDES", fields=["RFCDEST", "RFCTYPE"]
            )
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not read RFC destination table RFCDES: {exc}")

        by_type: Counter[str] = Counter(r.get("RFCTYPE", "?") for r in rows)
        summary = {
            _TYPE_LABELS.get(t, f"type {t}"): n for t, n in sorted(by_type.items())
        }
        # ABAP + external destinations are the ones that typically carry
        # source-landscape hostnames needing rework.
        needs_review = by_type.get("3", 0) + by_type.get("T", 0)
        data = {
            "total": len(rows),
            "by_type": summary,
            "abap_and_external": needs_review,
            "destinations": [r.get("RFCDEST", "") for r in rows],
        }
        if not rows:
            return Result.warn(self.name, "RFCDES returned no destinations", data=data)
        return Result.ok(
            self.name,
            f"{len(rows)} RFC destination(s); {needs_review} ABAP/external need "
            f"post-copy review",
            data=data,
        )
