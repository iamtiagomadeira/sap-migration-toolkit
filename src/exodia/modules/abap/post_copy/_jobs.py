"""Shared helper for the long-running post-copy ABAP actions (BDLS, SGEN).

BDLS (logical-system conversion) and SGEN (load regeneration) both run as
background work on the ABAP stack and can take a long time. Rather than freeze
the screen, both submit their work as a background job over RFC and then poll
the job's status, streaming a live progress line through the action's optional
monitor (exactly like the tenant-copy replication poll on the HANA side).

Kept tiny and testable: all RFC traffic goes through the readiness ``RfcClient``
protocol, so a test injects a fake client that returns a finished job on the
first poll. The poll loop is bounded by ``*_poll_max`` iterations of
``*_poll_interval`` seconds, and the inter-poll sleep is skipped when the
interval is 0 so tests never block.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..readiness import _rfc

if TYPE_CHECKING:
    from exodia.core.base import Action
    from exodia.core.context import Context

# ABAP background job states (TBTCO.STATUS): R=running, F=finished, A=aborted,
# S=released/scheduled, Y=ready, P=scheduled. We treat F as success, A as
# failure, everything else as still-in-flight.
_JOB_FINISHED = "F"
_JOB_ABORTED = "A"


class JobResult:
    """Outcome of a monitored background job."""

    def __init__(self, status: str, log: list[str], polls: int) -> None:
        self.status = status
        self.log = log
        self.polls = polls

    @property
    def finished_ok(self) -> bool:
        return self.status == _JOB_FINISHED

    @property
    def aborted(self) -> bool:
        return self.status == _JOB_ABORTED


def poll_job(
    action: Action,
    ctx: Context,
    client: _rfc.RfcClient,
    *,
    jobname: str,
    jobcount: str,
    status_fm: str = "BP_JOB_STATUS_GET",
    poll_interval_key: str = "job_poll_interval",
    poll_max_key: str = "job_poll_max",
    default_interval: float = 10.0,
    default_max: int = 360,
) -> JobResult:
    """Poll an ABAP background job until it finishes/aborts, streaming progress.

    Reads the job status over RFC each iteration and emits a progress line to the
    action's monitor (silent no-op when none is attached). Bounded by
    ``poll_max`` iterations; the sleep between polls is skipped when the interval
    is 0 (tests). Returns the final ``JobResult`` — the caller decides PASS/FAIL.
    """
    interval = float(ctx.get(poll_interval_key, default_interval))
    max_iters = int(ctx.get(poll_max_key, default_max))
    log: list[str] = []
    status = ""
    for i in range(1, max_iters + 1):
        res = client.call(status_fm, JOBNAME=jobname, JOBCOUNT=jobcount)
        status = str(res.get("STATUS", "") or "").upper()
        pct = res.get("PERCENT")
        detail = f"job {jobname} status={status or '?'}"
        line = f"poll {i}: {detail}"
        log.append(line)
        action._emit_log(line)
        if isinstance(pct, int | float):
            action._emit_progress(float(pct), detail)
        else:
            action._emit_progress(None, detail)
        if status in (_JOB_FINISHED, _JOB_ABORTED):
            if status == _JOB_FINISHED:
                action._emit_progress(100.0, f"job {jobname} finished")
            return JobResult(status, log, i)
        if interval > 0:
            time.sleep(interval)
    return JobResult(status or "TIMEOUT", log, max_iters)
