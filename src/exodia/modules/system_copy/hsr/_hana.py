"""Shared, side-effect-free helpers for the HSR (HANA System Replication) module.

Everything here builds ``argv: list[str]`` command lines (never a shell string)
so it works on BOTH a local ``Runner`` and a remote ``SSHRunner`` — Exodia's
hard safety rule. The two client families used by HSR:

* ``hdbsql -U <KEY>`` — password-free SQL against a SYSTEMDB (reads M_* views).
  The secure user store key authenticates, so no secret ever reaches argv/logs.
* ``hdbnsutil -sr_*`` — the HANA system-replication control tool run as
  ``<sid>adm``. It NEVER takes a password on the command line; when a secret is
  required (``-sr_register`` may ask for the primary's system user password) it
  is fed over **stdin** via ``runner.run(argv, input_text=...)`` so it appears
  neither in argv nor in any log line.

Grounded in SAP Notes 2407186 (HSR how-to), 1999880 (HSR FAQ), 2456657 (system
replication). Cite by number only — never inline customer data.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from exodia.core.params import ParamKind, ParamSpec

if TYPE_CHECKING:
    from exodia.core.context import Context
    from exodia.core.shell import Runner, SSHRunner

# A HANA version / numeric token like "2.00.067.00.1234567890".
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)*)")
# HANA instance numbers are always two digits (00-99).
_INSTANCE_RE = re.compile(r"^\d{2}$")

# --------------------------------------------------------------------------- #
# Parameter specs — declared by the checks/actions so the interactive menu can
# prompt for exactly the inputs an HSR move needs. Generic placeholders only.
# --------------------------------------------------------------------------- #

PRIMARY_KEY = ParamSpec(
    "primary_userstore_key", "Primary SYSTEMDB hdbuserstore key", default="SYSTEMDB",
    help="hdbsql -U key for the PRIMARY (source) SYSTEMDB.",
)
SECONDARY_KEY = ParamSpec(
    "secondary_userstore_key", "Secondary SYSTEMDB hdbuserstore key", default="SYSTEMDB",
    help="hdbsql -U key for the SECONDARY (target) SYSTEMDB.",
)
SITE_NAME = ParamSpec(
    "site_name", "Local replication site name", default="SITE_A",
    help="Logical site name for this host, e.g. SITE_A (primary) / SITE_B (secondary).",
)
REMOTE_HOST = ParamSpec(
    "remote_host", "Remote (primary) host", default="host1",
    help="Host of the primary the secondary registers against (--remoteHost).",
)
REMOTE_INSTANCE = ParamSpec(
    "remote_instance", "Remote (primary) instance number", default="00",
    help="Two-digit instance of the primary (--remoteInstance).",
)
REPLICATION_MODE = ParamSpec(
    "replication_mode", "Replication mode", default="sync",
    choices=("sync", "syncmem", "async"),
    help="sync = RPO=0 (log written on both before commit); async = lower latency, data-loss window.",
)
OPERATION_MODE = ParamSpec(
    "operation_mode", "Operation mode", default="logreplay",
    choices=("logreplay", "delta_datashipping", "logreplay_readaccess"),
    help="logreplay = modern default; secondary continuously replays logs.",
)
INSTANCE = ParamSpec(
    "instance", "HANA instance number", default="00",
    help="Two digits; replication ports 4<nn>01-07 are derived from it.",
)
SID = ParamSpec(
    "sid", "HANA SID (for systemPKI SSFS paths)", kind=ParamKind.FIELD,
    help="Three-char SID, used to locate the global SSFS PKI files.",
)
# The primary system user password sr_register may prompt for. NEVER placed on
# argv — fed over stdin. Marked secret so the wizard never echoes it.
SR_PASSWORD = ParamSpec(
    "sr_password", "Primary system user password (register, over stdin)",
    secret=True,
    help="Only if -sr_register prompts for it; sent via stdin, never argv/logs.",
)


# --------------------------------------------------------------------------- #
# argv builders — always list[str], never a shell string.
# --------------------------------------------------------------------------- #


def hdbsql_argv(key: str, stmt: str) -> list[str]:
    """Build a password-free hdbsql argv for a SQL statement."""
    return ["hdbsql", "-U", str(key), "-x", "-a", "-j", stmt]


def hdbnsutil_argv(*args: str) -> list[str]:
    """Build an ``hdbnsutil`` argv, e.g. hdbnsutil_argv('-sr_enable', '--name=SITE_A')."""
    return ["hdbnsutil", *[str(a) for a in args]]


def primary_key(ctx: Context) -> str:
    return str(ctx.get("primary_userstore_key") or ctx.get("userstore_key") or "SYSTEMDB")


def secondary_key(ctx: Context) -> str:
    return str(ctx.get("secondary_userstore_key") or ctx.get("userstore_key") or "SYSTEMDB")


def instance(ctx: Context, key: str = "instance") -> str:
    return str(ctx.get(key) or "00").zfill(2)


def is_valid_instance(value: str | None) -> bool:
    return bool(value) and bool(_INSTANCE_RE.match(str(value)))


def run(ctx: Context, argv: list[str], timeout: int = 120, input_text: str | None = None):  # type: ignore[no-untyped-def]
    """Run a command through the context's runner (local or SSH).

    ``input_text`` feeds a secret over stdin; both Runner and SSHRunner accept it.
    """
    runner: Runner | SSHRunner = ctx.runner()
    return runner.run(argv, timeout=timeout, input_text=input_text)


def ssfs_paths(sid: str | None) -> tuple[str, str] | None:
    """Return (DAT, KEY) paths of the global systemPKI SSFS files for a SID.

    These are the files that must be exchanged between primary and secondary
    before ``-sr_register`` will succeed. Returns None when no SID is known.
    """
    if not sid:
        return None
    s = sid.strip().upper()
    base = f"/usr/sap/{s}/SYS/global/security/rsecssfs"
    return (f"{base}/data/SSFS_{s}.DAT", f"{base}/key/SSFS_{s}.KEY")


# --------------------------------------------------------------------------- #
# Output parsing — pure, deterministic, no runner.
# --------------------------------------------------------------------------- #


def parse_hdbsql_rows(stdout: str) -> list[list[str]]:
    """Parse ``hdbsql -x -a -j`` output into a list of column-value rows.

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


def parse_replication_progress(stdout: str) -> tuple[list[str], list[str], float | None]:
    """Parse M_SERVICE_REPLICATION rows into (statuses, modes, shipped-percent).

    Each row is expected as
    ``(REPLICATION_STATUS, REPLICATION_MODE, shipped_size, full_size)``. Returns
    the distinct statuses (e.g. ACTIVE/INITIALIZING), the distinct modes (e.g.
    SYNC/SYNCMEM/ASYNC) and the aggregate shipped/full percentage (None when the
    sizes are unavailable so a progress bar stays indeterminate).
    """
    rows = parse_hdbsql_rows(stdout)
    statuses: list[str] = []
    modes: list[str] = []
    shipped_total = 0.0
    full_total = 0.0
    for row in rows:
        if row and row[0]:
            statuses.append(row[0].upper())
        if len(row) >= 2 and row[1]:
            modes.append(row[1].upper())
        if len(row) >= 4:
            try:
                shipped_total += float(row[2])
                full_total += float(row[3])
            except (ValueError, TypeError):
                pass
    pct: float | None = None
    if full_total > 0:
        pct = max(0.0, min(100.0, 100.0 * shipped_total / full_total))
    return _dedupe(statuses), _dedupe(modes), pct


def parse_sr_mode(text: str | None) -> str | None:
    """Extract the replication ``mode`` from ``hdbnsutil -sr_state`` output.

    Looks for a ``mode: <value>`` line (e.g. primary / syncmem / sync / none)
    and returns it lower-cased, or None when absent.
    """
    if not text:
        return None
    m = re.search(r"^\s*mode:\s*(\S+)", text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip().lower() if m else None


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
