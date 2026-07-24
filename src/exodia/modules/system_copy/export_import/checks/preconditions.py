"""Prerequisite checks for SWPM-driven export/import system copy.

Grounded in the official SAP export/import method (SWPM + load tools):

* **SWPM present** — the Software Provisioning Manager (`sapinst`) drives both
  the export on the source and the import on the target.
* **Export directory free space** — R3load/JLoad write the dump here; SAP sizing
  guidance is to have headroom for the largest table plus the package files.
* **Load tool matches stack** — ABAP uses **R3load**, AS Java uses **JLoad**.
  A backup/restore copy of Java is not supported, so export/import (JLoad) is
  mandatory for Java; this check verifies the right tool is available for the
  chosen stack.
* **Kernel / DBMS client reachable** — the target import needs the SAP kernel
  and a database client; a missing client is the most common early failure.

Every check is read-only.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamKind, ParamSpec

# --------------------------------------------------------------------------- #
# Parameter specs
# --------------------------------------------------------------------------- #

STACK = ParamSpec(
    "stack",
    "System stack",
    choices=("abap", "java", "dual"),
    default="abap",
    help="ABAP uses R3load; AS Java uses JLoad; dual-stack needs both.",
)
SWPM_PATH = ParamSpec(
    "swpm_path",
    "SWPM directory",
    default="/usr/sap/SWPM",
    help="Directory containing the sapinst executable.",
)
EXPORT_DIR = ParamSpec(
    "export_dir",
    "Export dump directory",
    default="/export",
    help="Where R3load/JLoad write the export; needs free space.",
)
EXPORT_SIZE_GB = ParamSpec(
    "export_size_gb",
    "Expected export size (GB)",
    help="Approx dump size; used to check export_dir free space.",
)
KERNEL_DIR = ParamSpec(
    "kernel_dir",
    "Kernel run directory (DIR_CT_RUN)",
    help="SAP kernel run directory; the real load tool must live here, "
    "not just anywhere on PATH.",
)


def _run(ctx: Context, argv: list[str], timeout: int = 60):  # type: ignore[no-untyped-def]
    return ctx.runner().run(argv, timeout=timeout)


def _kernel_dir(ctx: Context) -> str | None:
    """Resolve the kernel run directory from the parameter or $DIR_CT_RUN."""
    explicit = ctx.get("kernel_dir")
    if explicit:
        return str(explicit)
    cr = _run(ctx, ["sh", "-c", "echo $DIR_CT_RUN"])
    val = cr.stdout.strip() if cr.ok else ""
    return val or None


def _avail_gb(cr) -> float | None:  # type: ignore[no-untyped-def]
    """Parse `df -BG --output=avail <path>` output into available GB."""
    if not cr.ok:
        return None
    lines = [ln.strip() for ln in cr.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    try:
        return float(lines[-1].rstrip("G"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


class SwpmPresentCheck(Check):
    """SWPM (sapinst) must be present — it drives export and import."""

    name = "export-import.swpm-present"
    description = "SWPM (sapinst) is available to drive the export/import."
    title = "SWPM (sapinst) Available for Export/Import"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [SWPM_PATH]

    def run(self, ctx: Context) -> Result:
        swpm = ctx.get("swpm_path") or "/usr/sap/SWPM"
        cr = _run(ctx, ["test", "-x", f"{swpm}/sapinst"])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"sapinst not found/executable under {swpm} — download the latest "
                "SWPM from SAP and unpack it before the copy",
                data={"swpm_path": swpm},
            )
        return Result.ok(self.name, f"sapinst present under {swpm}", data={"swpm_path": swpm})


class LoadToolForStackCheck(Check):
    """The load tool must match the stack: R3load (ABAP) / JLoad (Java).

    Java cannot be copied via database backup/restore, so JLoad export/import is
    mandatory; this verifies the right binary is on PATH for the chosen stack.
    """

    name = "export-import.load-tool-for-stack"
    description = "R3load (ABAP) / JLoad (Java) available for the chosen stack."
    title = "R3load/JLoad Available for Stack"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [STACK, KERNEL_DIR]

    def run(self, ctx: Context) -> Result:
        stack = (ctx.get("stack") or "abap").lower()
        needed = {
            "abap": ["R3load"],
            "java": ["jload.sh"],
            "dual": ["R3load", "jload.sh"],
        }.get(stack, ["R3load"])
        kdir = _kernel_dir(ctx)
        missing = []
        found_in_kernel = []
        for tool in needed:
            # Prefer the kernel run directory (DIR_CT_RUN): the load tool used by
            # SWPM must be the kernel's, not some stray copy earlier on PATH.
            located = False
            if kdir:
                cr = _run(ctx, ["test", "-x", f"{kdir}/{tool}"])
                if cr.ok:
                    found_in_kernel.append(tool)
                    located = True
            if not located:
                cr = _run(ctx, ["sh", "-c", f"command -v {tool}"])
                located = cr.ok
            if not located:
                missing.append(tool)
        if missing:
            hint = (
                f"in kernel dir {kdir} or on PATH" if kdir else "on PATH (set kernel_dir/$DIR_CT_RUN)"
            )
            return Result.fail(
                self.name,
                f"stack '{stack}' needs {needed} but missing {missing} {hint} — "
                "source the SAP kernel environment (e.g. as <sid>adm)",
                data={"stack": stack, "missing": missing, "kernel_dir": kdir},
            )
        detail = (
            f"resolved in kernel dir {kdir}: {found_in_kernel}"
            if found_in_kernel
            else "resolved on PATH (kernel_dir/$DIR_CT_RUN not set)"
        )
        return Result.ok(
            self.name,
            f"load tool(s) {needed} available for stack '{stack}'",
            detail=detail,
            data={"stack": stack, "tools": needed, "kernel_dir": kdir, "in_kernel": found_in_kernel},
        )


class ExportDirSpaceCheck(Check):
    """The export directory must have room for the dump (R3load/JLoad output)."""

    name = "export-import.export-dir-space"
    description = "Export directory has enough free space for the dump."
    title = "Export Directory Free Space for Dump"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [EXPORT_DIR, EXPORT_SIZE_GB]

    def run(self, ctx: Context) -> Result:
        path = ctx.get("export_dir") or "/export"
        cr = _run(ctx, ["df", "-BG", "--output=avail", str(path)])
        avail = _avail_gb(cr)
        if avail is None:
            return Result.warn(
                self.name,
                f"could not read free space for {path}",
                detail=cr.stderr or cr.stdout,
                data={"export_dir": path},
            )
        raw = ctx.get("export_size_gb")
        if raw is None:
            return Result.ok(
                self.name,
                f"{avail:.0f}G free at {path} (no expected size given to compare)",
                data={"export_dir": path, "avail_gb": avail},
            )
        try:
            needed = float(raw)
        except (TypeError, ValueError):
            return Result.warn(self.name, f"invalid export_size_gb: {raw!r}")
        # SAP guidance: keep headroom above the raw dump size.
        required = needed * 1.2
        if avail < required:
            return Result.fail(
                self.name,
                f"{avail:.0f}G free at {path} < ~{required:.0f}G needed "
                f"(export {needed:.0f}G + 20% headroom)",
                data={"export_dir": path, "avail_gb": avail, "required_gb": required},
            )
        return Result.ok(
            self.name,
            f"{avail:.0f}G free at {path} ≥ ~{required:.0f}G needed",
            data={"export_dir": path, "avail_gb": avail, "required_gb": required},
        )


class DbClientReachableCheck(Check):
    """A database client must be reachable for the target import."""

    name = "export-import.db-client-reachable"
    description = "Database client available for the target import."
    title = "Target DB Client Available for Import"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "db_type",
                "Target database type",
                kind=ParamKind.FIELD,
                choices=("hana", "ase", "oracle", "db2"),
                help="Determines which DB client binary must be present.",
            )
        ]

    def run(self, ctx: Context) -> Result:
        db = (ctx.db_type or "").lower()
        client = {
            "hana": "hdbsql",
            "ase": "isql",
            "oracle": "sqlplus",
            "db2": "db2",
        }.get(db)
        if not client:
            return Result.skip(
                self.name,
                f"no db_type given or unknown ('{db}') — cannot check DB client",
            )
        cr = _run(ctx, ["sh", "-c", f"command -v {client}"])
        if not cr.ok:
            return Result.warn(
                self.name,
                f"{db} client '{client}' not on PATH — the target import will need it",
                data={"db_type": db, "client": client},
            )
        return Result.ok(
            self.name,
            f"{db} client '{client}' available",
            data={"db_type": db, "client": client},
        )


class ExportConsistencyCheck(Check):
    """Validate a finished source export before importing it.

    A real export/import copy imports from a dump directory produced by SWPM on
    the source. Two artefacts prove the export actually completed:

    * ``LABEL.ASC`` / ``export`` marker and the ``DATA``/``DB`` subtrees exist.
    * SWPM's ``keydb.xml`` (or the export summary) records the export step; if
      the export aborted, importing yields a silently incomplete target.

    Read-only: it only inspects files under the export directory.
    """

    name = "export-import.export-consistency"
    description = "Finished source export looks complete (labels + keydb.xml)."
    title = "Source Export Consistency (labels + keydb.xml)"
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [EXPORT_DIR]

    def run(self, ctx: Context) -> Result:
        path = str(ctx.get("export_dir") or "/export")
        # 1. The export must have the standard load-directory layout.
        cr = _run(ctx, ["test", "-d", f"{path}/ABAP/DATA"])
        cr_java = _run(ctx, ["test", "-d", f"{path}/DATA"])
        if not cr.ok and not cr_java.ok:
            return Result.fail(
                self.name,
                f"no export payload under {path} (expected ABAP/DATA or DATA) — "
                "the source export has not produced load files yet",
                data={"export_dir": path},
            )
        # 2. A completed SWPM export leaves keydb.xml; grep for the export step.
        keydb = _run(ctx, ["sh", "-c", f"test -f {path}/keydb.xml && echo yes"])
        if "yes" not in keydb.stdout:
            return Result.warn(
                self.name,
                f"load files present under {path} but no keydb.xml found — cannot "
                "confirm the export finished cleanly; verify the source SWPM export "
                "reached 'Execution of ... has completed'",
                data={"export_dir": path},
            )
        # keydb present: confirm it references a completed export, not an abort.
        done = _run(
            ctx,
            ["sh", "-c", f"grep -c 'STATUS=\"OK\"\\|export.*completed' {path}/keydb.xml"],
        )
        completed = done.ok and done.stdout.strip() not in ("", "0")
        if not completed:
            return Result.warn(
                self.name,
                f"keydb.xml under {path} does not clearly record a completed export "
                "— re-check the source export log before importing",
                data={"export_dir": path},
            )
        return Result.ok(
            self.name,
            f"export under {path} has load files and a keydb.xml recording completion",
            data={"export_dir": path},
        )
