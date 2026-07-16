"""Shared helpers for Java PI/PO (NetWeaver AS Java) prerequisite checks.

These utilities centralise the conventions used across the pipo.* checks:

* deriving the SAP instance number / SID from the context,
* building ``sapcontrol`` argument lists (never shell strings),
* locating the SAP instance directory tree,
* redacting anything that looks like a secret before it reaches a Result.

Everything here is read-only. No command mutates the target.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from exodia.core import Context

# Parameters that must NEVER be echoed back into a Result.summary/detail/data.
# The secure store key phrase is the obvious one, but we keep a small denylist
# so an operator cannot accidentally leak a secret through a --config override.
_SECRET_KEYS = {
    "key_phrase",
    "keyphrase",
    "secstore_key_phrase",
    "password",
    "passwd",
    "pwd",
    "secret",
    "db_password",
    "master_password",
}

# Matches a value that looks like a secret being passed on a command line, e.g.
# "-key_phrase Abc123" or "password=Abc123". Used to scrub captured output.
_SECRET_INLINE = re.compile(
    r"(?i)\b(key[_-]?phrase|password|passwd|pwd|secret)\b\s*[=: ]\s*\S+"
)


def instance_nr(ctx: Context) -> str:
    """Return the two-digit AS Java instance number (e.g. "00").

    Order of precedence: explicit param ``instance_nr`` -> ``nr`` -> default 00.
    Always normalised to two digits because sapcontrol requires ``-nr NN``.
    """
    raw = str(ctx.get("instance_nr", ctx.get("nr", "00")))
    digits = "".join(c for c in raw if c.isdigit())
    return digits.zfill(2)[-2:] if digits else "00"


def sid(ctx: Context) -> str:
    """Return the SID in upper case, or empty string if unknown."""
    value = ctx.sid or str(ctx.get("sid", ""))
    return value.upper()


def sapcontrol_argv(ctx: Context, function: str, *extra: str) -> list[str]:
    """Build a ``sapcontrol -nr NN -function <fn> [extra...]`` argument list.

    Returns a list[str] — never a shell string — so it can be handed straight
    to ``ctx.runner().run(...)`` without any injection risk.
    """
    argv = ["sapcontrol", "-nr", instance_nr(ctx), "-function", function]
    argv.extend(extra)
    return argv


def instance_dir(ctx: Context, kind: str = "J") -> str:
    """Best-effort path to the instance directory, e.g. /usr/sap/SID/J00.

    ``kind`` is the instance prefix letter: "J" for a Java central instance,
    "SCS" handled separately by callers. Falls back to a param override.
    """
    override = ctx.get("instance_dir")
    if override:
        return str(override)
    system_id = sid(ctx) or "SID"
    return f"/usr/sap/{system_id}/{kind}{instance_nr(ctx)}"


def sys_profile_dir(ctx: Context) -> str:
    """Path to the SYS/profile directory for the SID."""
    override = ctx.get("profile_dir")
    if override:
        return str(override)
    return f"/usr/sap/{sid(ctx) or 'SID'}/SYS/profile"


def java_schema(ctx: Context) -> str:
    """HANA schema that holds the AS Java persistence: SAP<SID>DB.

    Allows an explicit ``java_schema`` override for non-standard installs.
    """
    override = ctx.get("java_schema")
    if override:
        return str(override).upper()
    return f"SAP{sid(ctx) or 'SID'}DB"


def redact(text: str) -> str:
    """Scrub anything that looks like an inline secret from a captured string.

    Defensive: even if a secret leaks into command output or an error stream,
    it must not survive into a structured Result that could be logged or
    serialised to JSON in CI.
    """
    if not text:
        return text
    return _SECRET_INLINE.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)


def is_secret_key(key: str) -> bool:
    """Return True if a param key holds a secret that must not be surfaced."""
    return key.strip().lower() in _SECRET_KEYS
