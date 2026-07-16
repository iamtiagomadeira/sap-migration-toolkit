"""HANA connectivity & identity checks (TIA-57 #9, #10, #11).

 9. SID and instance number sanity
10. hdbuserstore keys present (the user can actually connect)
11. HANA SQL ports reachable (3<nn>13 / 3<nn>15)
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class SidInstanceSanityCheck(Check):
    """SID and instance number must be syntactically valid."""

    name = "backup-restore.hana.sid-instance-sanity"
    description = "SID and instance number are well-formed."
    blocking = True

    def run(self, ctx: Context) -> Result:
        the_sid = c.sid(ctx)
        the_instance = c.instance(ctx)
        problems: list[str] = []
        if not c.is_valid_sid(the_sid):
            problems.append(f"invalid SID '{the_sid}' (expected 3 alphanumerics, letter first)")
        if not c.is_valid_instance(the_instance):
            problems.append(f"invalid instance number '{the_instance}' (expected two digits 00-99)")
        if problems:
            return Result.fail(
                self.name,
                "; ".join(problems),
                data={"sid": the_sid, "instance": the_instance},
            )
        return Result.ok(
            self.name,
            f"SID={the_sid} instance={the_instance} valid",
            data={"sid": the_sid, "instance": the_instance},
        )


class UserstoreKeyCheck(Check):
    """The hdbuserstore key must exist so the migration user can connect."""

    name = "backup-restore.hana.userstore-key"
    description = "hdbuserstore key present and usable."
    blocking = True

    def run(self, ctx: Context) -> Result:
        key = ctx.get("userstore_key", "SYSTEMDB")
        cr = c.run(ctx, ["hdbuserstore", "LIST", str(key)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"hdbuserstore key '{key}' not found — the user cannot connect",
                detail=cr.stderr or cr.stdout,
            )
        if "KEY " not in cr.stdout.upper() and str(key).upper() not in cr.stdout.upper():
            return Result.fail(
                self.name,
                f"hdbuserstore key '{key}' not found in store listing",
                detail=cr.stdout,
            )
        return Result.ok(
            self.name,
            f"hdbuserstore key '{key}' present",
            data={"key": key},
        )


class HanaPortsCheck(Check):
    """The standard HANA SQL ports for the instance should be reachable."""

    name = "backup-restore.hana.ports-available"
    description = "HANA SQL ports (3<nn>13 / 3<nn>15) reachable."
    blocking = False

    def run(self, ctx: Context) -> Result:
        the_instance = c.instance(ctx)
        if not c.is_valid_instance(the_instance):
            return Result.skip(
                self.name,
                "instance number not provided/invalid; cannot derive ports",
            )
        assert the_instance is not None  # nosec B101 - narrowed by the guard above, not a security gate
        host = ctx.get("db_host", "localhost")
        ports = c.hana_ports(the_instance)
        unreachable: list[int] = []
        for port in ports.values():
            # Portable TCP probe: `nc -z` (no shell tricks, argv is always a list).
            cr = c.run(ctx, ["nc", "-z", "-w", "3", str(host), str(port)])
            if not cr.ok:
                unreachable.append(port)
        if unreachable:
            return Result.warn(
                self.name,
                f"HANA port(s) not reachable on {host}: {unreachable}",
                data={"host": host, "ports": ports, "unreachable": unreachable},
            )
        return Result.ok(
            self.name,
            f"HANA ports reachable on {host}: {sorted(ports.values())}",
            data={"host": host, "ports": ports},
        )
