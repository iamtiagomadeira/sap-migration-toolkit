"""Shared helpers for the HANA cross-host tenant-copy prerequisite checks.

These wrap the plumbing every tenant-copy check needs: building an hdbsql argv
against either the SOURCE or the TARGET SYSTEMDB, parsing hdbsql output, and the
small amount of SID/instance/version parsing shared across checks.

Cross-host model
----------------
A tenant copy here spans two *different* HANA systems:

* SOURCE = the customer environment (read from, never mutated).
* TARGET = SAP HEC machines that will host the copied tenant and later be handed
  to the customer.

Because a Context carries a single connection, each side is addressed through
explicit params rather than ctx.host. A check that needs the source reads
``source_userstore_key`` / ``source_*`` params; a target check reads
``target_userstore_key`` / ``target_*``. This keeps every check read-only and
trivially unit-testable with a fake Runner.

Kept dependency-free and side-effect-free.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from exodia.core.params import ParamKind, ParamSpec

if TYPE_CHECKING:
    from exodia.core.context import Context
    from exodia.core.shell import CommandResult, Runner, SSHRunner

# HANA instance numbers are always two digits (00-99).
_INSTANCE_RE = re.compile(r"^\d{2}$")
# HANA SIDs are three alphanumeric chars, first is a letter, upper-case by convention.
_SID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2}$")
# HANA tenant (database) names: up to 8 chars, letter first, alphanumerics/underscore.
_TENANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,7}$")
# SAP DB schema / object identifiers interpolated into DDL (never bindable as a
# parameter): letter first, then alphanumerics/underscore. Rejects quotes,
# whitespace and semicolons so an identifier can never break out of a statement.
_SCHEMA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# A HANA version string like "2.00.067.00.1234567890".
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)*)")

# Which side of the copy a check addresses.
SOURCE = "source"
TARGET = "target"


def side_key(side: str, name: str, default: object = None) -> str:
    """Build the param name for a given side, e.g. ('source','userstore_key')."""
    return f"{side}_{name}"


def userstore_key(ctx: Context, side: str) -> str:
    """Return the hdbuserstore key for the SYSTEMDB of the given side.

    Falls back to a plain ``userstore_key`` and finally to ``SYSTEMDB`` so a
    single-key setup still works.
    """
    return (
        ctx.get(side_key(side, "userstore_key"))
        or ctx.get("userstore_key")
        or "SYSTEMDB"
    )


def sid(ctx: Context, side: str) -> str | None:
    """Return the SID for the given side (source_sid / target_sid, or ctx.sid)."""
    return ctx.get(side_key(side, "sid")) or (ctx.sid if side == SOURCE else None)


def instance(ctx: Context, side: str) -> str | None:
    """Return the two-digit instance number for the given side."""
    inst = ctx.get(side_key(side, "instance"))
    if inst is None:
        return None
    return str(inst).zfill(2)


def source_tenant(ctx: Context) -> str | None:
    """Tenant DB name to copy FROM (ctx.source or source_tenant param)."""
    return ctx.source or ctx.get("source_tenant")


def target_tenant(ctx: Context) -> str | None:
    """Tenant DB name to create ON THE TARGET (ctx.target or target_tenant param)."""
    return ctx.target or ctx.get("target_tenant")


def hdbsql_argv(ctx: Context, side: str, stmt: str) -> list[str]:
    """Build an hdbsql argv for a SQL statement against a side's SYSTEMDB.

    Uses the side's hdbuserstore key (password-free). Always a list[str] — never
    a shell string (Exodia hard rule).
    """
    key = userstore_key(ctx, side)
    return ["hdbsql", "-U", str(key), "-x", "-a", "-j", stmt]


def run(ctx: Context, argv: list[str], timeout: int = 300) -> CommandResult:
    """Run a command through the context's runner (local or SSH)."""
    runner: Runner | SSHRunner = ctx.runner()
    return runner.run(argv, timeout=timeout)


def parse_hdbsql_rows(stdout: str) -> list[list[str]]:
    """Parse hdbsql -x -a -j output into a list of column-value rows.

    Comma-separated, double-quoted fields per row; blank lines and the trailing
    'N rows selected' line are dropped.
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


def is_valid_tenant(value: str | None) -> bool:
    """Tenant names must be valid and never SYSTEMDB (that is not copyable)."""
    if not value or value.upper() == "SYSTEMDB":
        return False
    return bool(_TENANT_RE.match(value))


def is_valid_schema(value: str | None) -> bool:
    """Validate a SAP DB schema / object identifier before it goes into DDL.

    Schema and table names cannot be passed as bound parameters (they name the
    object, not a value), so they are interpolated into the statement. This
    guard guarantees the identifier is a plain SQL identifier — letter first,
    then alphanumerics/underscore — so it can never carry a quote, whitespace or
    semicolon that would let it break out of the statement.
    """
    return bool(value) and bool(_SCHEMA_RE.match(value or ""))


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Extract a comparable version tuple from a HANA version string."""
    if not text:
        return None
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


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


# --------------------------------------------------------------------------- #
# Parameter specs — declared by checks so the interactive menu can prompt for
# exactly the cross-host inputs a tenant copy needs. Grouped so each check
# advertises only the subset it reads; the "run all pre-checks" flow unions them.
# --------------------------------------------------------------------------- #

# Tenant identity (used by nearly every check).
SOURCE_TENANT = ParamSpec(
    "source", "Source tenant name (customer)", required=True, kind=ParamKind.FIELD,
    help="The tenant DB to copy FROM, e.g. PRD. Never SYSTEMDB.",
)
TARGET_TENANT = ParamSpec(
    "target", "Target tenant name (new)", required=True, kind=ParamKind.FIELD,
    help="The new tenant DB to create on the target, e.g. QAS.",
)

# hdbuserstore keys (password-free connections to each SYSTEMDB).
SOURCE_USERSTORE_KEY = ParamSpec(
    "source_userstore_key", "Source SYSTEMDB hdbuserstore key", default="SYSTEMDB",
    help="hdbsql -U key for the SOURCE (customer) SYSTEMDB.",
)
TARGET_USERSTORE_KEY = ParamSpec(
    "target_userstore_key", "Target SYSTEMDB hdbuserstore key", default="SYSTEMDB",
    help="hdbsql -U key for the TARGET (HEC) SYSTEMDB.",
)

# Cross-host connectivity (target must reach the source SYSTEMDB SQL port).
# NOTE: source_host is read via ctx.get("source_host") — it is a free-form param,
# NOT a first-class Context field (that's ctx.host, the machine we run ON).
SOURCE_HOST = ParamSpec(
    "source_host", "Source SYSTEMDB host",
    help="Customer HANA host the target connects to for the copy.",
)
SOURCE_INSTANCE = ParamSpec(
    "source_instance", "Source instance number", default="00",
    help="Two digits; the source SQL port 3<nn>13 is derived from it.",
)

# Capacity sizing inputs (optional — checks fall back to safe defaults).
SOURCE_TENANT_GB = ParamSpec(
    "source_tenant_gb", "Source tenant size (GB)",
    help="Approx size of the source tenant; used to size target free space.",
)
TARGET_DATA_PATH = ParamSpec(
    "target_data_path", "Target data volume path", default="/hana/data",
)
TARGET_LOG_PATH = ParamSpec(
    "target_log_path", "Target log volume path", default="/hana/log",
)

#: Common set shared by most checks (identity + both userstore keys).
COMMON_SPECS: list[ParamSpec] = [
    SOURCE_TENANT,
    TARGET_TENANT,
    SOURCE_USERSTORE_KEY,
    TARGET_USERSTORE_KEY,
]
