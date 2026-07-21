"""Batch / background job readiness (SAP MIG task ~2019 — SM37).

Reads the batch job header table TBTCO over RFC and reports how many jobs are
active (running) or ready/scheduled to start. Before a takeover the scheduler
should be quiesced (BTCTRNS1 suspends released jobs); a job still running at
cutover can corrupt the copy or leave orphaned work.

Blocking: any *active* (currently running) job FAILs. Ready/released jobs are
surfaced as a WARN count — they should have been suspended, but they are not
mid-write like a running job is.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec

from . import _rfc

# TBTCO.STATUS codes (SAP standard):
#   R = active (running), Y = ready, S = scheduled, P = released/planned,
#   F = finished, A = cancelled
_ACTIVE = "R"
_READY = {"Y", "S", "P"}


class BackgroundJobsCheck(Check):
    """No batch jobs actively running (and report ready/released) pre-takeover."""

    name = "abap.readiness.background-jobs"
    description = "No active background jobs at takeover (SM37 / TBTCO)."
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
            rows = _rfc.read_table(
                client, "TBTCO", fields=["JOBNAME", "JOBCOUNT", "STATUS"]
            )
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not read job table TBTCO: {exc}")

        active = [r["JOBNAME"] for r in rows if r.get("STATUS") == _ACTIVE]
        ready = [r["JOBNAME"] for r in rows if r.get("STATUS") in _READY]
        data = {
            "active_jobs": active,
            "active_count": len(active),
            "ready_count": len(ready),
        }
        if active:
            return Result.fail(
                self.name,
                f"{len(active)} background job(s) still running: {', '.join(active[:5])}"
                + (" …" if len(active) > 5 else ""),
                data=data,
            )
        if ready:
            return Result.warn(
                self.name,
                f"no running jobs, but {len(ready)} ready/released (suspend via BTCTRNS1)",
                data=data,
            )
        return Result.ok(
            self.name, "no active or ready background jobs", data=data
        )
