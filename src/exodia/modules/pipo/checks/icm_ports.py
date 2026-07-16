"""pipo.icm-ports — verify ICM HTTP/HTTPS ports are free/available on the target.

The AS Java ICM (Internet Communication Manager) listens on the instance ports
5<nn>00 (HTTP) and 5<nn>01 (HTTPS), where <nn> is the instance number. Before a
system copy lands on the target these ports must be available (not already bound
by a stale process or a conflicting service). This read-only check inspects the
listening sockets on the target and reports whether the ICM ports are free.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import instance_nr


class IcmPortsCheck(Check):
    """ICM HTTP/HTTPS ports (5<nn>00 / 5<nn>01) are available on the target."""

    name = "pipo.icm-ports"
    description = "ICM HTTP/HTTPS ports (5NN00/5NN01) available on the target host."
    blocking = True

    def run(self, ctx: Context) -> Result:
        nr = instance_nr(ctx)
        http_port = int(f"5{nr}00")
        https_port = int(f"5{nr}01")
        expected_owned = bool(ctx.get("icm_expected_running", False))

        runner = ctx.runner()
        # `ss -ltn` lists listening TCP sockets numerically, no name resolution.
        cr = runner.run(["ss", "-ltn"])
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not enumerate listening ports (ss -ltn failed)",
                detail=cr.stderr,
            )
        listening = _listening_ports(cr.stdout)
        data = {
            "instance_nr": nr,
            "http_port": http_port,
            "https_port": https_port,
            "http_in_use": http_port in listening,
            "https_in_use": https_port in listening,
        }

        in_use = [p for p in (http_port, https_port) if p in listening]

        # Two valid outcomes depending on intent:
        #  - Preparing the target (default): ports must be FREE.
        #  - Verifying the ICM is up post-copy: ports SHOULD be in use.
        if expected_owned:
            if len(in_use) == 2:
                return Result.ok(
                    self.name,
                    f"ICM ports {http_port}/{https_port} are listening (ICM up)",
                    data=data,
                )
            return Result.fail(
                self.name,
                f"ICM ports not fully listening: expected {http_port} and {https_port} up",
                data=data,
            )

        if in_use:
            return Result.fail(
                self.name,
                f"ICM port(s) already in use on target: {', '.join(map(str, in_use))}",
                data=data,
            )
        return Result.ok(
            self.name,
            f"ICM ports {http_port}/{https_port} free on target",
            data=data,
        )


def _listening_ports(stdout: str) -> set[int]:
    """Extract the set of local listening ports from `ss -ltn` output."""
    ports: set[int] = set()
    for line in stdout.splitlines()[1:]:  # skip header
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]  # e.g. 0.0.0.0:50000 or [::]:50000
        _, _, port = local.rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return ports
