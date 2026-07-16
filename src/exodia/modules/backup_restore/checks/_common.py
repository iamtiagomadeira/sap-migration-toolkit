"""Shared helpers for the HANA backup/restore prerequisite checks.

These helpers wrap the small amount of parsing/plumbing every HANA check needs:
building an hdbsql argv, deriving SID/instance, and reading free space via df.
Kept dependency-free and side-effect-free so they are trivial to unit-test with
a fake Runner.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from exodia.core.context import Context
    from exodia.core.shell import CommandResult, Runner, SSHRunner


# HANA instance numbers are always two digits (00-99).
_INSTANCE_RE = re.compile(r"^\d{2}$")
# HANA SIDs are three alphanumeric chars, first is a letter, upper-case by convention.
_SID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2}$")


def sid(ctx: Context) -> str | None:
    """Return the SID from the context (ctx.sid or the escape-hatch param)."""
    return ctx.sid or ctx.get("sid")


def instance(ctx: Context) -> str | None:
    """Return the two-digit instance number from the escape-hatch param."""
    inst = ctx.get("instance")
    if inst is None:
        return None
    return str(inst).zfill(2)


def hdbsql_argv(
    ctx: Context,
    stmt: str,
    *,
    userstore_key: str | None = None,
) -> list[str]:
    """Build an hdbsql argv for a SQL statement.

    Uses an hdbuserstore key when provided (the recommended, password-free path).
    Always a list[str] — never a shell string (Exodia hard rule).
    """
    key = userstore_key or ctx.get("userstore_key", "SYSTEMDB")
    argv = ["hdbsql", "-U", str(key), "-x", "-a", "-j"]
    argv.append(stmt)
    return argv


def run(ctx: Context, argv: list[str], timeout: int = 300) -> CommandResult:
    """Run a command through the context's runner (local or SSH)."""
    runner: Runner | SSHRunner = ctx.runner()
    return runner.run(argv, timeout=timeout)


def parse_hdbsql_rows(stdout: str) -> list[list[str]]:
    """Parse hdbsql -x -a output into a list of column-value rows.

    With -a (no headers), -x (expanded/quoted) and -j (no formatting) hdbsql
    emits comma-separated, double-quoted fields per row. We strip the quotes and
    return the fields. Blank lines and the trailing 'rows selected' line are
    dropped.
    """
    rows: list[list[str]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^\d+ rows? (selected|affected)", line, re.IGNORECASE):
            continue
        fields = [f.strip().strip('"') for f in line.split(",")]
        rows.append(fields)
    return rows


def is_valid_sid(value: str | None) -> bool:
    return bool(value) and bool(_SID_RE.match(value or ""))


def is_valid_instance(value: str | None) -> bool:
    return bool(value) and bool(_INSTANCE_RE.match(value or ""))


def hana_ports(instance_no: str) -> dict[str, int]:
    """Return the standard SQL/HTTP-ish HANA ports for an instance number.

    3<nn>13 = indexserver SQL port (SYSTEMDB / single-container),
    3<nn>15 = first tenant SQL port.
    """
    nn = instance_no.zfill(2)
    return {
        "sql_systemdb": int(f"3{nn}13"),
        "sql_tenant": int(f"3{nn}15"),
    }


def avail_gb(runner_result: CommandResult) -> float | None:
    """Extract available GB from `df -BG --output=avail <path>` output."""
    if not runner_result.ok:
        return None
    lines = [ln.strip() for ln in runner_result.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    try:
        return float(lines[-1].rstrip("G"))
    except ValueError:
        return None
