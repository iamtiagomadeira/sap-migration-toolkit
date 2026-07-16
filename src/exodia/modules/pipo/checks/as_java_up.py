"""pipo.as-java-up — verify the AS Java stack is running and all processes green.

Uses ``sapcontrol -function GetProcessList``. For a NetWeaver AS Java central
instance the relevant processes are jstart / the bootstrap and the server0
worker node; all must report GREEN. This is a prerequisite for a consistent
online-safe reading of the source, and for confirming the target came up after
a system copy.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import redact, sapcontrol_argv

# Process names that make up a running AS Java instance. sapcontrol reports one
# line per OS process with a "dispstatus" colour (GREEN/YELLOW/GRAY/RED).
_JAVA_PROCS = ("jstart", "jcontrol", "jlaunch", "server", "bootstrap", "sdm")


class ASJavaUpCheck(Check):
    """AS Java processes are running and reporting GREEN."""

    name = "pipo.as-java-up"
    description = "AS Java instance is up (sapcontrol GetProcessList all GREEN)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        runner = ctx.runner()
        cr = runner.run(sapcontrol_argv(ctx, "GetProcessList"))
        # sapcontrol returns exit code 3 when all green, 4 when not all green,
        # and non-zero for genuine errors. Treat 3/4 as "reachable".
        if cr.exit_code not in (0, 3, 4):
            return Result.fail(
                self.name,
                "sapcontrol GetProcessList did not respond — is sapstartsrv running?",
                detail=redact(cr.stderr or cr.stdout),
            )

        procs = _parse_process_list(cr.stdout)
        if not procs:
            return Result.fail(
                self.name,
                "no AS Java processes reported by sapcontrol",
                detail=redact(cr.stdout),
            )

        not_green = {name: status for name, status in procs.items() if status != "GREEN"}
        if not_green:
            return Result.fail(
                self.name,
                "AS Java not fully up: " + ", ".join(f"{n}={s}" for n, s in not_green.items()),
                data={"processes": procs},
            )
        return Result.ok(
            self.name,
            f"AS Java up — {len(procs)} process(es) GREEN",
            data={"processes": procs},
        )


def _parse_process_list(stdout: str) -> dict[str, str]:
    """Parse GetProcessList CSV-ish output into {process_name: dispstatus}.

    Example line:
        jstart, Running, Green, ... , GREEN
    We look for a recognised java process name and the GREEN/YELLOW/... token.
    """
    procs: dict[str, str] = {}
    for line in stdout.splitlines():
        low = line.lower()
        matched = next((p for p in _JAVA_PROCS if p in low), None)
        if matched is None:
            continue
        status = "UNKNOWN"
        for token in ("GREEN", "YELLOW", "GRAY", "GREY", "RED"):
            if token in line.upper():
                status = "GRAY" if token == "GREY" else token
                break
        # server0/server1 collapse to a stable key so ordering is deterministic.
        key = matched
        procs[key if key not in procs else f"{key}:{len(procs)}"] = status
    return procs
