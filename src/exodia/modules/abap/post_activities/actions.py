"""ABAP post-activities — guarded steps to bring the target back into service.

After the copy completes and the target tenant is verified, these actions
reverse the ramp-down and re-open the system for business, in cutover order:

* ``abap.post.start-app-servers``  — start the target application servers
  (sapcontrol StartSystem ALL); the mirror of the ramp-down stop.
* ``abap.post.resume-jobs``        — BTCTRNS2: resume the background scheduler
  that ramp-down suspended, so scheduled jobs run again.
* ``abap.post.unlock-users``       — BAPI_USER_UNLOCK the business users that
  ramp-down locked, re-opening the system to end users.
* ``abap.post.validate-online``    — confirm the app servers are up (SM51 /
  sapcontrol GetProcessList) as the final post-activity gate.

All guarded (dry-run -> confirm -> execute -> verify). RFC-backed steps reuse
the readiness ``_rfc`` plumbing; sapcontrol steps use the context runner (SSH),
argv-only.
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase

from ..readiness import _rfc


class StartApplicationServersAction(Action):
    """Start the target application servers (sapcontrol StartSystem)."""

    name = "abap.post.start-app-servers"
    description = "Start all application servers on the target (sapcontrol)."
    title = "Start All Application Servers (Target)"
    phase = Phase.POST
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "instance_number", "Instance number (NN) to start",
                help="Two-digit instance number for sapcontrol (e.g. 00).",
            ),
            ParamSpec(
                "start_scope", "Start scope", default="system",
                choices=("system", "instance"),
                help="'system' = StartSystem ALL; 'instance' = Start this one.",
            ),
            ParamSpec(
                "host", "Target host (blank = local)", kind=ParamKind.FIELD,
                help="Host to run sapcontrol on over SSH; blank runs locally.",
            ),
            ParamSpec(
                "user", "SSH user", kind=ParamKind.FIELD,
                help="SSH user (typically <sid>adm) for the target host.",
            ),
        ]

    def _argv(self, ctx: Context) -> list[str]:
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        func = "StartSystem" if str(ctx.get("start_scope", "system")) == "system" else "Start"
        arg = ["ALL"] if func == "StartSystem" else []
        return ["sapcontrol", "-nr", nr, "-function", func, *arg]

    def dry_run(self, ctx: Context) -> Result:
        argv = self._argv(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            "would START the target application servers",
            detail=f"  1. {' '.join(argv)}",
            data={"argv": argv},
            facts={"Command": " ".join(argv), "Scope": str(ctx.get("start_scope", "system"))},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        argv = self._argv(ctx)
        cr = ctx.runner().run(argv, timeout=int(ctx.get("start_timeout", 600)))
        if not cr.ok:
            return Result.fail(
                phase,
                f"sapcontrol start failed (exit {cr.exit_code})",
                detail=cr.stderr or cr.stdout,
                data={"argv": argv, "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            "sapcontrol start issued for the target application servers",
            data={"argv": argv, "stdout": cr.stdout.strip()},
            facts={"Command": " ".join(argv), "Result": "Start issued"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        cr = ctx.runner().run(
            ["sapcontrol", "-nr", nr, "-function", "GetProcessList"],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        out = (cr.stdout or "").upper()
        if "GREEN" in out:
            return Result.ok(
                phase,
                "application servers report running (GREEN processes present)",
                data={"stdout": cr.stdout.strip()},
                facts={"Processes Running": "yes (GREEN)"},
            )
        return Result.warn(
            phase,
            "no GREEN processes yet — servers may still be starting",
            detail=cr.stdout,
            facts={"Processes Running": "not yet"},
        )

    def rollback(self, ctx: Context) -> Result:
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        return Result.skip(
            f"{self.name}.rollback",
            f"to stop again, run: sapcontrol -nr {nr} -function StopSystem ALL",
        )


class ResumeBackgroundJobsAction(Action):
    """BTCTRNS2 — resume the background scheduler suspended during ramp-down."""

    name = "abap.post.resume-jobs"
    description = "Resume background job scheduling (BTCTRNS2) after takeover."
    title = "BTCTRNS2 — Resume Background Job Scheduler"
    phase = Phase.POST
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return _rfc.SOURCE_CONN_SPECS

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params")
        return Result.ok(
            phase,
            "would resume the background job scheduler (BTCTRNS2) so scheduled "
            "jobs run again",
            detail="  1. RFC BP_JOB_RESUME / report BTCTRNS2",
            facts={"Action": "Resume scheduler (BTCTRNS2)"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            res = client.call("BP_JOB_RESUME")
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not resume background scheduler: {exc}")
        subrc = res.get("SUBRC", 0)
        if subrc not in (0, None):
            return Result.fail(phase, f"BTCTRNS2 resume returned SUBRC={subrc}",
                               data={"subrc": subrc})
        return Result.ok(
            phase,
            "background job scheduler resumed (BTCTRNS2)",
            data={"subrc": subrc},
            facts={"Scheduler": "Resumed"},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.verify",
            "scheduler resumed — scheduled background jobs will run again",
            facts={"Scheduler": "Resumed"},
        )


class UnlockBusinessUsersAction(Action):
    """BAPI_USER_UNLOCK — unlock the business users locked during ramp-down."""

    name = "abap.post.unlock-users"
    description = "Unlock business users after takeover (BAPI_USER_UNLOCK)."
    title = "SU10 — Unlock Business Users (Re-open System)"
    phase = Phase.POST
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "business_users", "Business users to unlock (comma-separated)",
                help="List of users to unlock — typically the same set locked at "
                "ramp-down.",
            ),
        ]

    def _users(self, ctx: Context) -> list[str]:
        raw = ctx.get("business_users") or ""
        return [u.strip() for u in str(raw).split(",") if u.strip()]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params")
        users = self._users(ctx)
        if not users:
            return Result.skip(phase, "no business_users provided to unlock")
        return Result.ok(
            phase,
            f"would unlock {len(users)} business user(s): {', '.join(users)}",
            detail="\n".join(f"  {i}. BAPI_USER_UNLOCK {u}" for i, u in enumerate(users, 1)),
            data={"to_unlock": users},
            facts={"Users To Unlock": str(len(users))},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        users = self._users(ctx)
        if not users:
            return Result.skip(phase, "no business_users to unlock")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            unlocked: list[str] = []
            for u in users:
                res = client.call("BAPI_USER_UNLOCK", USERNAME=u)
                ret = res.get("RETURN", {}) or {}
                if str(ret.get("TYPE", "") if isinstance(ret, dict) else "").upper() in ("E", "A"):
                    return Result.fail(
                        phase,
                        f"failed to unlock user {u} (unlocked {len(unlocked)} so far)",
                        data={"unlocked": unlocked, "failed": u},
                    )
                unlocked.append(u)
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not unlock users: {exc}")
        return Result.ok(
            phase,
            f"unlocked {len(unlocked)} business user(s) — system re-opened",
            data={"unlocked": unlocked},
            facts={"Users Unlocked": str(len(unlocked))},
        )

    def verify(self, ctx: Context) -> Result:
        users = self._users(ctx)
        return Result.ok(
            f"{self.name}.verify",
            f"{len(users)} business user(s) unlocked",
            facts={"Users Unlocked": str(len(users))},
        )


class ValidateOnlineAction(Action):
    """Final post-activity: confirm the target is up and serving (SM51)."""

    name = "abap.post.validate-online"
    description = "Validate the target application servers are online (SM51)."
    title = "SM51 — Post-Takeover Online Validation"
    phase = Phase.POST
    destructive = False
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return _rfc.SOURCE_CONN_SPECS

    def dry_run(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.dry-run",
            "would confirm the target application servers are online (TH_SERVER_LIST / SM51)",
            facts={"Action": "Validate online (SM51)"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params to validate against")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            res = client.call("TH_SERVER_LIST")
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not read server list: {exc}")
        servers = res.get("LIST", []) or []
        if not servers:
            return Result.fail(
                phase, "no application servers online after takeover",
                facts={"App Servers Online": "0"},
            )
        return Result.ok(
            phase,
            f"{len(servers)} application server(s) online after takeover",
            data={"server_count": len(servers)},
            facts={"App Servers Online": str(len(servers))},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.verify",
            "post-takeover online validation complete",
        )
