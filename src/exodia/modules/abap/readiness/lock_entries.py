"""Enqueue lock readiness (SAP MIG task ~2019 — SM12).

Reads the current enqueue lock entries via the standard ENQUEUE_READ function
module. Before a takeover there should be no user lock entries held: a stale
lock at cutover points to an in-flight transaction that has not committed, and
copying underneath it risks an inconsistent target.

Blocking: any lock entry FAILs. (SAP's own internal locks are filtered by the
read scope; ENQUEUE_READ with an empty key returns the held application locks.)
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

from . import _rfc


class LockEntriesCheck(Check):
    """No enqueue lock entries held on the source pre-takeover (SM12 / ENQUEUE_READ)."""

    name = "abap.readiness.lock-entries"
    description = "No enqueue lock entries held at takeover (SM12 / ENQUEUE_READ)."
    blocking = True

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
            res = client.call("ENQUEUE_READ", GCLIENT="", GNAME="", GARG="", GUNAME="")
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not read enqueue locks: {exc}")

        entries = res.get("ENQ", []) or []
        holders = sorted({e.get("GUNAME", "") for e in entries if e.get("GUNAME")})
        data = {
            "lock_count": len(entries),
            "holders": holders,
            "subrc": res.get("SUBRC", 0),
        }
        if entries:
            return Result.fail(
                self.name,
                f"{len(entries)} enqueue lock(s) held by: {', '.join(holders) or 'unknown'}",
                data=data,
            )
        return Result.ok(self.name, "no enqueue lock entries held", data=data)
