"""pipo.secstore-present — verify the secure store (SECSTORE) material exists.

AS Java stores DB credentials and other secrets in the secure store. The two
artefacts that must travel with (or be re-created on) the target are the
SecStore data file and its key file:

    <instance>/SDM/... or /sec/  ->  SecStore.properties  +  SecStore.key

Without a valid key phrase / key file the Java stack cannot decrypt its DB
credentials on the target and will not start. This check confirms the files are
PRESENT and readable. It NEVER reads, prints, or logs the key phrase or the
key file contents — only their existence and size.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import instance_dir, is_secret_key, redact, sid

# Default relative locations of the secure store artefacts under the instance.
# Real installs vary; an operator can override with params.secstore_dir.
_SECSTORE_FILES = ("SecStore.properties", "SecStore.key")


class SecStorePresentCheck(Check):
    """Secure store data + key files are present (never reveals the key phrase)."""

    name = "pipo.secstore-present"
    description = "SECSTORE files present and readable (key phrase never logged)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        # Guard: if someone passed the key phrase as a param, refuse to proceed
        # in a way that could echo it, and remind them it must not be here.
        leaked = [k for k in ctx.params if is_secret_key(k)]

        secstore_dir = ctx.get("secstore_dir") or f"{instance_dir(ctx)}/SDM/program/config"
        runner = ctx.runner()

        present: dict[str, int] = {}
        missing: list[str] = []
        for fname in _SECSTORE_FILES:
            path = f"{secstore_dir}/{fname}"
            # `stat -c %s` gives size in bytes; exit non-zero if the file is absent.
            cr = runner.run(["stat", "-c", "%s", path])
            if cr.ok:
                try:
                    present[fname] = int(cr.stdout.strip())
                except ValueError:
                    present[fname] = -1
            else:
                missing.append(fname)

        data: dict[str, object] = {
            "secstore_dir": secstore_dir,
            "present": present,
            "missing": missing,
        }
        if leaked:
            # We record only the KEY NAMES that must be removed, never values.
            data["secret_params_ignored"] = leaked

        if missing:
            return Result.fail(
                self.name,
                f"SECSTORE incomplete for {sid(ctx)}: missing {', '.join(missing)}",
                detail=redact(f"searched under {secstore_dir}"),
                data=data,
            )
        # A zero-byte key file is as bad as a missing one.
        empty = [f for f, size in present.items() if size <= 0]
        if empty:
            return Result.fail(
                self.name,
                f"SECSTORE file(s) empty: {', '.join(empty)}",
                data=data,
            )
        return Result.ok(
            self.name,
            f"SECSTORE present for {sid(ctx)} ({len(present)} file(s), key phrase not read)",
            data=data,
        )
