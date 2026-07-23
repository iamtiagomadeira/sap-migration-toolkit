"""Guarded downtime actions that drive the HSR relationship via ``hdbnsutil``.

These are the three state-changing steps that actually *move* the system with
HANA System Replication — the capability the HSR module was missing:

* ``hsr.enable-primary`` (DOWNTIME, BLOCKING) — enable system replication on the
  SOURCE (``hdbnsutil -sr_enable --name=<site>``). Precondition: the system is a
  valid, running primary (not already a secondary).
* ``hsr.register-secondary`` (DOWNTIME, BLOCKING) — register the TARGET as the
  secondary (``hdbnsutil -sr_register --remoteHost=<h> --remoteInstance=<nn>
  --replicationMode=<m> --operationMode=<o> --name=<site>``). Requires the
  systemPKI SSFS to be exchanged and the replication parameters aligned first.
* ``hsr.takeover`` (DOWNTIME, BLOCKING) — promote the target to primary
  (``hdbnsutil -sr_takeover``). Guarded by ``hsr.sync-active-verify`` so it can
  never run while replication is behind (RPO=0 protection).

Safety contract (Exodia hard rules):

* Commands are ``argv: list[str]`` — never ``shell=True``.
* ``dry_run`` describes the EXACT command that WOULD run and executes NOTHING
  (asserted in tests: the runner records zero calls).
* No secret is ever placed on argv. If ``-sr_register`` prompts for the primary
  system-user password, it is fed over **stdin** (``input_text``), never argv,
  never a log line.

References (cite by number only): SAP Note 2407186 (HSR how-to), 1999880 (HSR
FAQ), 2456657 (system replication).
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import DB_TYPE, HOST, USER, ParamSpec
from exodia.core.result import Phase

from .. import _hana as h


class EnablePrimaryAction(Action):
    """Enable system replication on the source primary (hdbnsutil -sr_enable)."""

    name = "hsr.enable-primary"
    description = "Enable HSR on the source primary (hdbnsutil -sr_enable --name=<site>)."
    title = "Enable HSR Primary (hdbnsutil -sr_enable)"
    phase = Phase.DOWNTIME
    destructive = True
    requires_checks = [
        "hsr.log-mode-normal",
        "hsr.data-backup-exists",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [
            HOST,
            USER,
            DB_TYPE.with_default("hana"),
            h.SITE_NAME,
            h.PRIMARY_KEY,
        ]

    @staticmethod
    def _site(ctx: Context) -> str:
        return str(ctx.get("site_name") or "SITE_A")

    def _enable_argv(self, ctx: Context) -> list[str]:
        return h.hdbnsutil_argv("-sr_enable", f"--name={self._site(ctx)}")

    def dry_run(self, ctx: Context) -> Result:
        argv = self._enable_argv(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would enable HSR on the primary as site '{self._site(ctx)}'; nothing executed",
            detail=f"  1. {' '.join(argv)}",
            data={"command": argv, "site": self._site(ctx)},
            facts={"Site": self._site(ctx), "Command": "hdbnsutil -sr_enable"},
            sap_note="2407186",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        # Precondition: must be a valid primary, i.e. NOT already a secondary.
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        mode = h.parse_sr_mode((state.stdout or "") + (state.stderr or ""))
        if mode in {"sync", "syncmem", "async", "secondary"}:
            return Result.fail(
                phase,
                f"this system is already a replication secondary (mode '{mode}') — "
                "-sr_enable must run on the PRIMARY; aborting",
                data={"mode": mode},
                sap_note="2407186",
            )
        argv = self._enable_argv(ctx)
        self._emit_phase("enable", " ".join(argv))
        self._emit_log(f"$ {' '.join(argv)}")
        cr = h.run(ctx, argv, timeout=int(ctx.get("sr_timeout", 300)))
        if cr.stdout:
            self._emit_log(cr.stdout)
        if not cr.ok:
            return Result.fail(
                phase,
                f"hdbnsutil -sr_enable failed (exit {cr.exit_code})",
                detail=cr.stderr or cr.stdout,
                data={"command": argv, "exit_code": cr.exit_code},
                sap_note="2407186",
            )
        return Result.ok(
            phase,
            f"HSR enabled on the primary as site '{self._site(ctx)}'; verify next",
            data={"command": argv, "site": self._site(ctx)},
            facts={"Site": self._site(ctx), "Primary": "enabled"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        text = (state.stdout or "") + (state.stderr or "")
        mode = h.parse_sr_mode(text)
        if mode == "primary":
            return Result.ok(
                phase, "system reports mode 'primary' — replication is enabled",
                data={"mode": mode}, facts={"Mode": "primary"},
            )
        return Result.warn(
            phase,
            f"could not confirm primary mode from -sr_state (saw '{mode or 'unknown'}')",
            detail=text.strip()[:500], data={"mode": mode},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "run 'hdbnsutil -sr_disable' on the primary to undo enablement "
            "(see SAP Note 2407186)",
            sap_note="2407186",
        )


class RegisterSecondaryAction(Action):
    """Register the target as the replication secondary (hdbnsutil -sr_register)."""

    name = "hsr.register-secondary"
    description = (
        "Register the target as HSR secondary "
        "(hdbnsutil -sr_register --remoteHost/--remoteInstance/--replicationMode/--operationMode/--name)."
    )
    title = "Register HSR Secondary (hdbnsutil -sr_register)"
    phase = Phase.DOWNTIME
    destructive = True
    requires_checks = [
        "hsr.pki-ssfs-exchanged",
        "hsr.replication-parameters",
        "hsr.version-compatibility",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [
            HOST,
            USER,
            DB_TYPE.with_default("hana"),
            h.SITE_NAME,
            h.REMOTE_HOST,
            h.REMOTE_INSTANCE,
            h.REPLICATION_MODE,
            h.OPERATION_MODE,
            h.SR_PASSWORD,
        ]

    @staticmethod
    def _site(ctx: Context) -> str:
        return str(ctx.get("site_name") or "SITE_B")

    def _register_argv(self, ctx: Context) -> list[str]:
        remote_host = str(ctx.get("remote_host") or "host1")
        remote_inst = h.instance(ctx, "remote_instance")
        mode = str(ctx.get("replication_mode") or "sync")
        op = str(ctx.get("operation_mode") or "logreplay")
        # Password is NEVER an argv element — it goes over stdin when prompted.
        return h.hdbnsutil_argv(
            "-sr_register",
            f"--remoteHost={remote_host}",
            f"--remoteInstance={remote_inst}",
            f"--replicationMode={mode}",
            f"--operationMode={op}",
            f"--name={self._site(ctx)}",
        )

    def dry_run(self, ctx: Context) -> Result:
        argv = self._register_argv(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would register this system as secondary site '{self._site(ctx)}' "
            f"against {ctx.get('remote_host') or 'host1'}; nothing executed",
            detail=f"  1. {' '.join(argv)}",
            data={
                "command": argv,
                "site": self._site(ctx),
                "remote_host": ctx.get("remote_host") or "host1",
                "replication_mode": ctx.get("replication_mode") or "sync",
                "operation_mode": ctx.get("operation_mode") or "logreplay",
                # A password, if given, is fed over stdin — never shown here.
                "password_via_stdin": bool(ctx.get("sr_password")),
            },
            facts={
                "Site": self._site(ctx),
                "Remote Host": str(ctx.get("remote_host") or "host1"),
                "Mode": str(ctx.get("replication_mode") or "sync"),
                "Operation": str(ctx.get("operation_mode") or "logreplay"),
            },
            sap_note="2407186",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        argv = self._register_argv(ctx)
        # Secret (if any) goes over stdin so it never touches argv or the log.
        pwd = ctx.get("sr_password")
        input_text = f"{pwd}\n" if pwd else None
        self._emit_phase("register", " ".join(argv))
        # Log the argv only — NEVER the stdin secret.
        self._emit_log(f"$ {' '.join(argv)}")
        cr = h.run(ctx, argv, timeout=int(ctx.get("sr_timeout", 900)), input_text=input_text)
        if cr.stdout:
            self._emit_log(cr.stdout)
        if not cr.ok:
            return Result.fail(
                phase,
                f"hdbnsutil -sr_register failed (exit {cr.exit_code}) — check the "
                "systemPKI SSFS exchange and that the primary is enabled",
                detail=cr.stderr or cr.stdout,
                data={"command": argv, "exit_code": cr.exit_code},
                sap_note="2407186",
            )
        return Result.ok(
            phase,
            f"registered as secondary site '{self._site(ctx)}'; initial sync begins "
            "(monitor with hsr.sync-monitor)",
            data={"command": argv, "site": self._site(ctx)},
            facts={"Site": self._site(ctx), "Secondary": "registered"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        text = (state.stdout or "") + (state.stderr or "")
        mode = h.parse_sr_mode(text)
        if mode in {"sync", "syncmem", "async", "secondary"}:
            return Result.ok(
                phase, f"system reports secondary mode '{mode}' — registration took",
                data={"mode": mode}, facts={"Mode": mode},
            )
        return Result.warn(
            phase,
            f"could not confirm secondary mode from -sr_state (saw '{mode or 'unknown'}')",
            detail=text.strip()[:500], data={"mode": mode},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "run 'hdbnsutil -sr_unregister --name=<site>' on the secondary to "
            "undo registration (see SAP Note 2407186)",
            sap_note="2407186",
        )


class TakeoverAction(Action):
    """Promote the target to primary (hdbnsutil -sr_takeover).

    Guarded by ``hsr.sync-active-verify`` — the RPO=0 guard-rail — so a takeover
    can never run while replication is behind. This is the point of no return of
    the move: after it, the (former) secondary is an independent primary.
    """

    name = "hsr.takeover"
    description = "Promote the target to primary (hdbnsutil -sr_takeover)."
    title = "HSR Takeover (Promote Target to Primary)"
    phase = Phase.DOWNTIME
    destructive = True
    requires_checks = [
        "hsr.sync-active-verify",
    ]

    def parameters(self) -> list[ParamSpec]:
        return [HOST, USER, DB_TYPE.with_default("hana"), h.SECONDARY_KEY]

    def _takeover_argv(self) -> list[str]:
        return h.hdbnsutil_argv("-sr_takeover")

    def dry_run(self, ctx: Context) -> Result:
        argv = self._takeover_argv()
        return Result.ok(
            f"{self.name}.dry-run",
            "would take over: promote this secondary to an independent primary; "
            "nothing executed",
            detail=f"  1. {' '.join(argv)}",
            data={"command": argv},
            facts={"Command": "hdbnsutil -sr_takeover"},
            sap_note="2407186",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        argv = self._takeover_argv()
        self._emit_phase("takeover", " ".join(argv))
        self._emit_log(f"$ {' '.join(argv)}")
        cr = h.run(ctx, argv, timeout=int(ctx.get("takeover_timeout", 900)))
        if cr.stdout:
            self._emit_log(cr.stdout)
        if not cr.ok:
            return Result.fail(
                phase,
                f"hdbnsutil -sr_takeover failed (exit {cr.exit_code}) — the system "
                "may be in an inconsistent state; investigate before retrying",
                detail=cr.stderr or cr.stdout,
                data={"command": argv, "exit_code": cr.exit_code},
                sap_note="2407186",
            )
        return Result.ok(
            phase,
            "takeover issued — target promoted to primary; confirm with "
            "hsr.post-takeover-online",
            data={"command": argv},
            facts={"Takeover": "issued"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        text = (state.stdout or "") + (state.stderr or "")
        mode = h.parse_sr_mode(text)
        if mode == "primary":
            return Result.ok(
                phase, "target reports mode 'primary' — takeover succeeded",
                data={"mode": mode}, facts={"Mode": "primary"},
            )
        return Result.warn(
            phase,
            f"takeover issued but primary mode not confirmed (saw '{mode or 'unknown'}') "
            "— run hsr.post-takeover-online to certify",
            detail=text.strip()[:500], data={"mode": mode},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "a completed takeover is not auto-reversible — to fall back, re-establish "
            "replication in the opposite direction (register the old primary as the new "
            "secondary); see SAP Note 2407186",
            sap_note="2407186",
        )
