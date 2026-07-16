"""Safe command execution — local and remote (SSH).

Hard rule: NEVER use shell=True. Commands are always a list of args, passed
directly to the process. This eliminates shell-injection as a class of bug —
the single biggest flaw found in the internal predecessor tool.
"""

from __future__ import annotations

import shlex
import subprocess  # nosec B404 - argv-only execution; shell=True is never used (see run())
from dataclasses import dataclass

import paramiko


@dataclass
class CommandResult:
    """Raw outcome of running a command (exit code + streams)."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def display(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)


class Runner:
    """Runs commands locally. Never uses shell=True."""

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise TypeError("argv must be a list[str] — no shell strings allowed")
        try:
            proc = subprocess.run(  # nosec B603 # noqa: S603 - argv list[str], never shell=True; input is a validated arg list
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
                check=False,
            )
            return CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return CommandResult(argv, 124, partial, f"timeout after {timeout}s")
        except FileNotFoundError as exc:
            return CommandResult(argv, 127, "", str(exc))


class SSHRunner:
    """Runs commands on a remote host over SSH with host-key verification.

    Host-key verification is ON by default (RejectPolicy). A migration tool must
    not blindly trust unknown hosts. Callers can supply a known_hosts path.
    """

    #: TCP/auth/banner timeout for establishing the connection (seconds).
    connect_timeout: int = 30

    def __init__(
        self,
        host: str,
        user: str,
        port: int = 22,
        key_filename: str | None = None,
        known_hosts: str | None = None,
        connect_timeout: int = 30,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.key_filename = key_filename
        self._known_hosts = known_hosts
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        if self._known_hosts:
            client.load_host_keys(self._known_hosts)
        else:
            client.load_system_host_keys()
        # RejectPolicy: refuse unknown hosts rather than silently trusting them.
        # Password auth is intentionally NOT enabled: only key-based auth (agent,
        # look_for_keys, or explicit key_filename) is used, so no secret is ever
        # passed to paramiko or held in memory as a cleartext password.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_filename,
            allow_agent=True,
            look_for_keys=True,
            # Bound every phase of the handshake so a hung/unreachable host can
            # never block a migration run indefinitely.
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
        )
        self._client = client

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        if self._client is None:
            self.connect()
        assert self._client is not None  # nosec B101 - post-connect invariant, not a security gate
        # Build a properly-quoted remote command line from the arg list. Every
        # element is escaped with shlex.quote, so no argv value can break out of
        # its token — this neutralises the paramiko exec_command injection risk.
        cmd = " ".join(shlex.quote(a) for a in argv)
        stdin, stdout, stderr = self._client.exec_command(  # nosec B601 - argv shlex-quoted, no shell metachar injection
            cmd, timeout=timeout
        )
        # Feed a secret (e.g. a secure-store key phrase) over stdin so it never
        # appears on the remote command line — mirrors the local Runner contract.
        if input_text is not None:
            stdin.write(input_text)
            stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        return CommandResult(argv, exit_code, stdout.read().decode(), stderr.read().decode())

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> SSHRunner:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
