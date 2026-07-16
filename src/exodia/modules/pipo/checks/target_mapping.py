"""pipo.target-mapping — verify the target carries the expected SID/host mapping.

A Java system copy lands on a target whose hostname, instance number and
virtual host must match what the copy was prepared for. A mismatch here is the
classic cause of a profile/SLD/ICM misconfiguration after restore. This
read-only check compares the live target hostname + instance number against the
expected values supplied in the context.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import instance_nr, sid


class TargetMappingCheck(Check):
    """Target hostname / instance number match the expected mapping."""

    name = "pipo.target-mapping"
    description = "Target host + instance number match the expected system-copy mapping."
    blocking = True

    def run(self, ctx: Context) -> Result:
        expected_host = ctx.get("expected_host") or ctx.target
        if not expected_host:
            return Result.skip(
                self.name,
                "no expected_host/target configured — cannot validate the target mapping",
            )
        runner = ctx.runner()
        cr = runner.run(["hostname", "-s"])
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not read target hostname",
                detail=cr.stderr,
            )
        actual_host = cr.stdout.strip().lower()
        expected_short = str(expected_host).split(".")[0].lower()

        expected_nr = instance_nr(ctx)
        data: dict[str, object] = {
            "expected_host": expected_short,
            "actual_host": actual_host,
            "instance_nr": expected_nr,
            "sid": sid(ctx),
        }

        # Optional virtual host verification (SAP often uses a virtual hostname).
        virtual_host = ctx.get("virtual_host")
        if virtual_host:
            data["virtual_host"] = str(virtual_host).lower()

        if actual_host != expected_short and (
            not virtual_host or str(virtual_host).split(".")[0].lower() != actual_host
        ):
            return Result.fail(
                self.name,
                f"target host mismatch: expected '{expected_short}' but system reports '{actual_host}'",
                data=data,
            )
        return Result.ok(
            self.name,
            f"target mapping OK: host '{actual_host}', instance {expected_nr}, SID {sid(ctx)}",
            data=data,
        )
