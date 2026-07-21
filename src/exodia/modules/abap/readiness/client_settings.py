"""Client configuration readiness (SAP MIG task ~1061 — SCC4 / table T000).

Reads the client table T000 over RFC and reports each client's role and change
options. A cutover plan checks SCC4 to confirm productive clients are locked
down (no cross-client / client-dependent changes, no automatic recording of
changes) before and after the copy. Read-only.

WARNs when a productive client (CCCATEGORY = 'P') is left open for changes —
exactly the misconfiguration SCC4 review is meant to catch — but never mutates.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

from . import _rfc

# T000.CCCORACTIV / CCNOCLIIND semantics (SAP standard):
#   CCCATEGORY: 'P' productive, 'T' test, 'C' customizing, 'D' demo, 'E' training
#   CCCORACTIV: '1'/'2'/'3' -> changes/no changes for client-dependent objects
#   CCNOCLIIND: cross-client object change option
_PRODUCTIVE = "P"


class ClientSettingsCheck(Check):
    """Inventory clients (T000) and flag productive clients open for changes."""

    name = "abap.readiness.client-settings"
    description = "Client roles and change options (SCC4 / T000)."

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
                client,
                "T000",
                fields=["MANDT", "MTEXT", "CCCATEGORY", "CCCORACTIV", "CCNOCLIIND"],
            )
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not read client table T000: {exc}")

        clients = [
            {
                "client": r.get("MANDT", ""),
                "name": r.get("MTEXT", ""),
                "category": r.get("CCCATEGORY", ""),
                "client_dependent_changes": r.get("CCCORACTIV", ""),
                "cross_client_changes": r.get("CCNOCLIIND", ""),
            }
            for r in rows
        ]
        # A productive client open for client-dependent changes (CCCORACTIV in
        # {"1","2"} allows changes) is the risky state.
        open_prod = [
            c
            for c in clients
            if c["category"] == _PRODUCTIVE and c["client_dependent_changes"] in ("1", "2")
        ]
        data = {"clients": clients, "productive_open_for_changes": [c["client"] for c in open_prod]}
        if not clients:
            return Result.warn(self.name, "T000 returned no clients — unexpected", data=data)
        if open_prod:
            return Result.warn(
                self.name,
                f"{len(open_prod)} productive client(s) open for changes: "
                f"{', '.join(c['client'] for c in open_prod)}",
                data=data,
            )
        return Result.ok(
            self.name,
            f"{len(clients)} client(s); no productive client open for changes",
            data=data,
        )
