"""Secret-free builders + parsers for the headless SWPM orchestration.

This module holds the *pure* logic behind the guarded
``backup-restore.swpm.system-copy`` action:

* :func:`validate_inifile` — confirm an existing ``inifile.params`` (the
  Ansible ``sap_swpm`` role convention: Exodia VALIDATES and REUSES it, it does
  NOT generate one from scratch) is present, readable, and carries the minimum
  keys. ``instkey.pkey`` is a SECRET: its *presence* is confirmed, its content
  is NEVER read or logged.
* :func:`build_sapinst_env` / :func:`build_sapinst_argv` — assemble the sapinst
  invocation as an ``argv: list[str]`` plus an env mapping. Hard rule: no
  secrets ever land in argv, and the GUI server is left ON by default (observer
  mode) for a manual handoff.
* :func:`parse_progress` — inspect sapinst stdout/log text to classify the run
  as *waiting for input* (observer-mode handoff), *error* (pause, never kill),
  *done*, or *running*.

Hard rules inherited from the core:
  * Commands are always ``list[str]`` (argv). Never ``shell=True``.
  * Secrets never appear in argv, env values logged verbatim, or Results.

References (cite by number only, never reproduce Note text):
  * SAP Note 2230669 — SWPM, product IDs, SL Protocol / sapinst options.
  * SAP Note 950619 — system copy inifile / unattended parameters.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# The GUI server listens here; the handoff URL for observer mode.
GUI_SERVER_PORT = 4237

# Minimum keys we expect in a system-copy inifile.params. We do not enforce the
# full schema (SWPM owns that) — just enough to fail fast on an empty/wrong file.
MIN_INIFILE_KEYS: tuple[str, ...] = (
    "SAPINST.CD.PACKAGE",
    "NW_System",
)

# Name of the secret key file: its PRESENCE is confirmed, its VALUE is never
# read or logged. This is a filename constant, not a credential.
SECRET_PKEY_FILENAME = "instkey.pkey"  # nosec B105 - filename constant, not a credential value


@dataclass
class SapinstPlan:
    """A secret-free description of the sapinst invocation (for dry-run).

    ``env`` carries the SAPINST_* variables; sensitive *values* are never
    placed here (the inifile path and product id are not secrets). ``argv`` is
    the exact command list that :meth:`execute` would launch.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    describe: str = ""

    @property
    def display(self) -> str:
        """Readable single line for the dry-run plan (secret-free)."""
        env_str = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.env.items())
        cmd = " ".join(shlex.quote(a) for a in self.argv)
        line = f"{env_str} {cmd}".strip()
        if self.describe:
            return f"{line}  # {self.describe}"
        return line


class RunState(str, Enum):
    """Classification of a sapinst run derived from its output/log."""

    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"  # observer-mode handoff needed
    ERROR = "error"  # pause, never kill
    DONE = "done"


@dataclass
class ProgressReport:
    """Outcome of parsing sapinst output/log text."""

    state: RunState
    phase: str = ""
    detail: str = ""


# --- inifile validation -------------------------------------------------------


@dataclass
class InifileInfo:
    """Result of validating an inifile.params (secret-free)."""

    path: str
    keys_found: list[str]
    has_secret_pkey: bool


class InifileError(Exception):
    """Raised when the inifile.params is missing, unreadable, or incomplete."""


def validate_inifile(inifile_path: str | None) -> InifileInfo:
    """Validate an existing ``inifile.params`` without leaking secrets.

    Confirms the file exists, is readable, and carries the minimum keys. The
    presence of ``instkey.pkey`` (either inline or as a sibling file) is noted,
    but its content is NEVER read or returned.

    Raises :class:`InifileError` with a clear message on any problem.
    """
    if not inifile_path:
        raise InifileError(
            "no inifile given — set params.inifile to the path of an existing "
            "inifile.params (Ansible sap_swpm convention; Exodia validates/reuses it)"
        )
    path = Path(inifile_path)
    if not path.exists():
        raise InifileError(f"inifile not found: {inifile_path}")
    if not path.is_file():
        raise InifileError(f"inifile is not a regular file: {inifile_path}")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise InifileError(f"inifile is not readable: {inifile_path} ({exc})") from exc

    keys_found = [key for key in MIN_INIFILE_KEYS if key in text]
    missing = [key for key in MIN_INIFILE_KEYS if key not in text]
    if missing:
        raise InifileError(
            f"inifile {inifile_path} is missing required key(s): {', '.join(missing)}"
        )

    # Secret handling: note presence only. instkey.pkey may be referenced in the
    # params OR live as a sibling file next to inifile.params — check both, but
    # never read its contents.
    sibling_pkey = path.with_name("instkey.pkey")
    has_secret = (SECRET_PKEY_FILENAME in text) or sibling_pkey.exists()

    return InifileInfo(path=str(path), keys_found=keys_found, has_secret_pkey=has_secret)


# --- sapinst command construction ---------------------------------------------


def build_sapinst_env(
    inifile_path: str,
    product_id: str,
    *,
    start_guiserver: bool = True,
) -> dict[str, str]:
    """Build the SAPINST_* environment for a headless run.

    Defaults to OBSERVER MODE: ``SAPINST_START_GUISERVER`` is left unset (the
    GUI server stays ON) so an operator can attach for a manual handoff. Only
    when ``start_guiserver=False`` do we explicitly disable it.

    Never sets ``SAPINST_SKIP_ERRORSTEP`` (dangerous — errors must pause, not be
    skipped). No secrets are placed here.
    """
    env = {
        "SAPINST_INPUT_PARAMETERS_URL": inifile_path,
        "SAPINST_EXECUTE_PRODUCT_ID": product_id,
        "SAPINST_SKIP_DIALOGS": "true",
    }
    if not start_guiserver:
        # Only disable explicitly on request; default keeps observer-mode GUI up.
        env["SAPINST_START_GUISERVER"] = "false"
    return env


def build_sapinst_argv(sapinst_path: str, *, extra_args: list[str] | None = None) -> list[str]:
    """Build the sapinst argv (list[str], never a shell string)."""
    argv = [sapinst_path]
    if extra_args:
        argv.extend(str(a) for a in extra_args)
    return argv


def gui_url(host: str | None) -> str:
    """The observer-mode GUI handoff URL for the sapinst GUI server."""
    hostname = host or "localhost"
    return f"https://{hostname}:{GUI_SERVER_PORT}/sapinst/docs/index.html"


def build_plan(
    *,
    sapinst_path: str,
    inifile_path: str,
    product_id: str,
    start_guiserver: bool = True,
    extra_args: list[str] | None = None,
) -> SapinstPlan:
    """Assemble the full (secret-free) sapinst plan for dry-run/execute."""
    env = build_sapinst_env(inifile_path, product_id, start_guiserver=start_guiserver)
    argv = build_sapinst_argv(sapinst_path, extra_args=extra_args)
    mode = "observer-mode GUI ON" if start_guiserver else "GUI server OFF"
    return SapinstPlan(
        argv=argv,
        env=env,
        describe=f"headless SWPM system copy for {product_id} ({mode})",
    )


# --- output / log parsing -----------------------------------------------------

# Ordered so the most decisive states win. Each entry: (regex, RunState).
_WAIT_PATTERNS = (
    re.compile(r"waiting for input", re.IGNORECASE),
    re.compile(r"waiting for the user", re.IGNORECASE),
    re.compile(r"please (?:go to|open).*sapinst", re.IGNORECASE),
    re.compile(r"open (?:your )?(?:web )?browser", re.IGNORECASE),
    re.compile(r"enter the following (?:url|address)", re.IGNORECASE),
)
_ERROR_PATTERNS = (
    re.compile(r"\bERROR\b", re.IGNORECASE),
    re.compile(r"\bFATAL\b", re.IGNORECASE),
    re.compile(r"execution of .* aborted", re.IGNORECASE),
    re.compile(r"phase .* failed", re.IGNORECASE),
    re.compile(r"an error occurred", re.IGNORECASE),
)
_DONE_PATTERNS = (
    re.compile(r"execution of .* has completed", re.IGNORECASE),
    re.compile(r"has been completed successfully", re.IGNORECASE),
    re.compile(r"sapinst .* finished", re.IGNORECASE),
    re.compile(r"\bINFO\b.*completed successfully", re.IGNORECASE),
)
# Best-effort current-phase extraction (informational only).
_PHASE_PATTERN = re.compile(
    r"(?:executing|entering|processing) (?:phase|step|task)[:\s]+(?P<phase>[^\n]{1,120})",
    re.IGNORECASE,
)


def _find_phase(text: str) -> str:
    matches = _PHASE_PATTERN.findall(text)
    return matches[-1].strip() if matches else ""


def parse_progress(text: str) -> ProgressReport:
    """Classify sapinst output/log text into a :class:`ProgressReport`.

    Precedence: *waiting for input* (observer-mode handoff) and *error* (pause)
    take priority over *done*/*running* so we never mistake a stalled run for a
    completed one. Errors PAUSE (surfaced as :attr:`RunState.ERROR`) — this
    module never kills sapinst.
    """
    phase = _find_phase(text)

    if any(p.search(text) for p in _WAIT_PATTERNS):
        return ProgressReport(state=RunState.WAITING_FOR_INPUT, phase=phase)

    if any(p.search(text) for p in _ERROR_PATTERNS):
        # Surface the first matching error line as detail (secret-free: sapinst
        # error lines do not contain the pkey/password).
        detail = _first_matching_line(text, _ERROR_PATTERNS)
        return ProgressReport(state=RunState.ERROR, phase=phase, detail=detail)

    if any(p.search(text) for p in _DONE_PATTERNS):
        return ProgressReport(state=RunState.DONE, phase=phase)

    return ProgressReport(state=RunState.RUNNING, phase=phase)


def _first_matching_line(text: str, patterns: tuple[re.Pattern[str], ...]) -> str:
    for line in text.splitlines():
        if any(p.search(line) for p in patterns):
            return line.strip()
    return ""
