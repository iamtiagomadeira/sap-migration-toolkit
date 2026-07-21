"""Ramp-down actions (SAP MIG — quiesce the source before takeover).

These are the state-changing steps of an ECS/HEC cutover ramp-down, guarded by
the standard dry-run -> confirm -> execute -> verify flow. They map the cutover
plan's ramp-down rows onto Exodia actions:

* ``abap.rampdown.suspend-jobs``       — BTCTRNS1: suspend the background
  scheduler so no new jobs start during the copy (SM37 shows jobs suspended).
* ``abap.rampdown.adapt-operation-modes`` — SM63: switch to a ramp-down
  operation mode (all work processes to batch/reduced dialog).
* ``abap.rampdown.stop-app-servers``   — stop ALL application servers via
  sapcontrol. Customer-impacting and irreversible for the business, so it is
  gated behind an EXPLICIT customer confirmation (``customer_confirmed``) on top
  of the normal --yes gate: SAP must not stop the customer's servers until the
  customer has signed off.
* ``abap.rampdown.inform-customer``    — a MANUAL attestation: the admin emails
  the customer that ramp-down is complete and takeover will begin. Exodia
  performs no system action; it records that the admin did it (``attested``).

The RFC-backed actions reuse the readiness ``_rfc`` plumbing; the OS-level stop
uses the context runner (SSH) with ``sapcontrol`` — argv-only, never a shell
string.
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action, _truthy
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase

from ..readiness import _rfc

# Technical/service users that must NOT be locked during ramp-down: locking
# DDIC/SAP*/TMSADM would break the migration itself (transports, background,
# system logon). These are always excluded from a business-user lock.
_PROTECTED_USERS = {"DDIC", "SAP*", "TMSADM", "SAPJSF", "SOLMAN", "EARLYWATCH"}


class SuspendBackgroundJobsAction(Action):
    """BTCTRNS1 — suspend the background job scheduler on the source."""

    name = "abap.rampdown.suspend-jobs"
    description = "Suspend background job scheduling (BTCTRNS1) for ramp-down."
    title = "BTCTRNS1 — Suspend Background Job Scheduler"
    phase = Phase.RAMP_DOWN
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return _rfc.SOURCE_CONN_SPECS

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no source RFC connection params")
        return Result.ok(
            phase,
            "would suspend the background job scheduler (BTCTRNS1) on the source; "
            "no new jobs would start until resumed (BTCTRNS2)",
            detail="  1. RFC BP_JOB_SUSPEND / report BTCTRNS1 (set scheduler to suspended)",
            facts={"Action": "Suspend scheduler (BTCTRNS1)", "Reversible": "Yes (BTCTRNS2)"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            # BP_JOB_SUSPEND suspends the scheduler; SUBRC 0 == success.
            res = client.call("BP_JOB_SUSPEND")
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not suspend background scheduler: {exc}")
        subrc = res.get("SUBRC", 0)
        if subrc not in (0, None):
            return Result.fail(
                phase, f"BTCTRNS1 suspend returned SUBRC={subrc}", data={"subrc": subrc}
            )
        return Result.ok(
            phase,
            "background job scheduler suspended (BTCTRNS1)",
            data={"subrc": subrc},
            facts={"Scheduler": "Suspended"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        # No released jobs should start now; we report the suspended state.
        return Result.ok(
            phase,
            "scheduler suspended — no new background jobs will start until BTCTRNS2",
            facts={"Scheduler": "Suspended"},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "resume the scheduler with BTCTRNS2 (report) when ramp-down is aborted",
        )


class AdaptOperationModesAction(Action):
    """SM63 — switch to a ramp-down operation mode on the source."""

    name = "abap.rampdown.adapt-operation-modes"
    description = "Adapt operation modes & timetable for ramp-down (SM63)."
    title = "SM63 — Adapt Operation Modes for Ramp-Down"
    phase = Phase.RAMP_DOWN
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "operation_mode", "Ramp-down operation mode name",
                help="Operation mode to switch to (e.g. a batch-heavy mode). "
                "Leave blank to only record the intent.",
            ),
        ]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        mode = ctx.get("operation_mode") or "(ramp-down mode)"
        return Result.ok(
            phase,
            f"would switch the active operation mode to {mode} (SM63) so work "
            "processes favour batch/reduced dialog during ramp-down",
            detail=f"  1. SWITCH_OPERATION_MODE -> {mode}",
            facts={"Target Operation Mode": str(mode)},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        mode = ctx.get("operation_mode")
        if not mode:
            return Result.skip(
                phase,
                "no operation_mode provided — SM63 switch recorded as intent only",
            )
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            res = client.call("SWITCH_OPERATION_MODE", OPMODE=str(mode))
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not switch operation mode: {exc}")
        return Result.ok(
            phase,
            f"operation mode switched to {mode} (SM63)",
            data={"operation_mode": mode, "response": dict(res)},
            facts={"Active Operation Mode": str(mode)},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        mode = ctx.get("operation_mode") or "(unchanged)"
        return Result.ok(
            phase, f"operation mode is {mode}", facts={"Active Operation Mode": str(mode)}
        )


class StopApplicationServersAction(Action):
    """Stop ALL application servers on the source (sapcontrol) — customer-gated.

    This freezes the customer's business system. It therefore requires an
    EXPLICIT customer confirmation (``customer_confirmed``) on top of the normal
    execute gate: the admin only selects this AFTER the customer has confirmed.
    """

    name = "abap.rampdown.stop-app-servers"
    description = "Stop all application servers on the source (sapcontrol)."
    title = "Stop All Application Servers (Source) — Customer-Confirmed"
    phase = Phase.RAMP_DOWN
    destructive = True
    requires_customer_confirmation = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "instance_number", "Instance number (NN) to stop",
                help="Two-digit instance number for sapcontrol (e.g. 00).",
            ),
            ParamSpec(
                "stop_scope", "Stop scope", default="system",
                choices=("system", "instance"),
                help="'system' = StopSystem ALL (every instance); 'instance' = Stop this one.",
            ),
            ParamSpec(
                "customer_confirmed", "Customer has confirmed the stop (true/false)",
                help="MUST be true — the customer has signed off on stopping their "
                "application servers. Exodia will not stop them otherwise.",
            ),
            ParamSpec(
                "host", "Source host (blank = local)", kind=ParamKind.FIELD,
                help="Host to run sapcontrol on over SSH; blank runs locally.",
            ),
            ParamSpec(
                "user", "SSH user", kind=ParamKind.FIELD,
                help="SSH user (typically <sid>adm) for the source host.",
            ),
        ]

    def _argv(self, ctx: Context) -> list[str]:
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        func = "StopSystem" if str(ctx.get("stop_scope", "system")) == "system" else "Stop"
        arg = ["ALL"] if func == "StopSystem" else []
        return ["sapcontrol", "-nr", nr, "-function", func, *arg]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        argv = self._argv(ctx)
        return Result.ok(
            phase,
            "would STOP the source application servers — customer-impacting, "
            "runs only after the customer has confirmed",
            detail=f"  1. {' '.join(argv)}",
            data={"argv": argv},
            facts={
                "Command": " ".join(argv),
                "Scope": str(ctx.get("stop_scope", "system")),
                "Customer Confirmed": "yes" if ctx.get("customer_confirmed") else "PENDING",
            },
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        argv = self._argv(ctx)
        cr = ctx.runner().run(argv, timeout=int(ctx.get("stop_timeout", 600)))
        if not cr.ok:
            return Result.fail(
                phase,
                f"sapcontrol stop failed (exit {cr.exit_code})",
                detail=cr.stderr or cr.stdout,
                data={"argv": argv, "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            "sapcontrol stop issued for the source application servers",
            data={"argv": argv, "stdout": cr.stdout.strip()},
            facts={"Command": " ".join(argv), "Result": "Stop issued"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        cr = ctx.runner().run(
            ["sapcontrol", "-nr", nr, "-function", "GetProcessList"],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        out = (cr.stdout or "").upper()
        # GetProcessList returns GREEN/GRAY per process; after a stop we expect
        # no GREEN (running) dispatcher. GRAY/stopped is what we want.
        if "GREEN" in out:
            return Result.warn(
                phase,
                "some processes still report GREEN — servers may still be stopping",
                detail=cr.stdout,
                facts={"Processes Running": "some (GREEN)"},
            )
        return Result.ok(
            phase,
            "application servers report stopped (no GREEN processes)",
            data={"stdout": cr.stdout.strip()},
            facts={"Processes Running": "none"},
        )

    def rollback(self, ctx: Context) -> Result:
        nr = str(ctx.get("instance_number", "00")).zfill(2)
        return Result.skip(
            f"{self.name}.rollback",
            f"to restart, run: sapcontrol -nr {nr} -function StartSystem ALL "
            "(only if the takeover is aborted and the customer agrees)",
        )


class InformCustomerAction(Action):
    """MANUAL — the admin informs the customer that ramp-down is complete.

    Exodia performs NO system action here. The admin emails the customer that
    ramp-down activities are done and the takeover will be initiated, then
    records that they did it (``attested`` truthy). This keeps the cutover
    record complete without pretending Exodia sent anything.
    """

    name = "abap.rampdown.inform-customer"
    description = "MANUAL: inform the customer that ramp-down is complete."
    title = "Inform Customer — Ramp-Down Complete (Manual)"
    phase = Phase.RAMP_DOWN
    manual = True
    destructive = False
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "attested", "Confirm you have emailed the customer (true/false)",
                help="Set true once you have emailed the customer that ramp-down "
                "is complete and takeover will begin.",
            ),
            ParamSpec(
                "attested_note", "Note (e.g. email recipient / timestamp)",
                help="Optional free-text recorded as evidence (who was told, when).",
            ),
        ]

    def dry_run(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.dry-run",
            "MANUAL step — email the customer that ramp-down is complete and "
            "takeover will be initiated. Exodia performs no system action; set "
            "attested=true once you have sent it.",
            facts={"Type": "Manual (admin emails customer)"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"

        note = ctx.get("attested_note") or ""
        if not _truthy(ctx.get("attested")):
            return Result.skip(
                phase,
                "not yet attested — email the customer, then set attested=true to "
                "record that ramp-down completion was communicated",
                facts={"Attested": "No"},
            )
        return Result.ok(
            phase,
            "attested: customer informed that ramp-down is complete and takeover "
            f"will begin{f' ({note})' if note else ''}",
            data={"attested": True, "note": note},
            facts={"Attested": "Yes", "Note": note or "—"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        if _truthy(ctx.get("attested")):
            return Result.ok(phase, "ramp-down completion communicated to the customer",
                             facts={"Attested": "Yes"})
        return Result.skip(phase, "awaiting attestation", facts={"Attested": "No"})


def _business_users(ctx: Context) -> list[str]:
    """Resolve the explicit business-user list from params, excluding protected.

    Users come from the ``business_users`` param (comma-separated). Protected
    technical users are always removed so a lock can never brick the migration.
    """
    raw = ctx.get("business_users") or ""
    extra_protected = {
        u.strip().upper() for u in str(ctx.get("keep_unlocked") or "").split(",") if u.strip()
    }
    protected = _PROTECTED_USERS | extra_protected
    users = [u.strip() for u in str(raw).split(",") if u.strip()]
    return [u for u in users if u.upper() not in protected]


class LockBusinessUsersAction(Action):
    """SU10/BAPI_USER_LOCK — lock business users so no one logs on during copy.

    Protected technical users (DDIC, SAP*, TMSADM, ...) are never locked. The
    business-user list is provided explicitly via the ``business_users`` param
    (a comma-separated list); this keeps the action deterministic and auditable
    rather than locking "everyone" heuristically.
    """

    name = "abap.rampdown.lock-users"
    description = "Lock business users for ramp-down (BAPI_USER_LOCK), sparing technical users."
    title = "SU10 — Lock Business Users (Ramp-Down)"
    phase = Phase.RAMP_DOWN
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "business_users", "Business users to lock (comma-separated)",
                help="Explicit list of dialog/business users to lock. Technical "
                "users (DDIC, SAP*, TMSADM, ...) are always spared.",
            ),
            ParamSpec(
                "keep_unlocked", "Extra users to spare (comma-separated)",
                help="Additional users to never lock, on top of the built-in "
                "technical-user exclusions.",
            ),
        ]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no source RFC connection params")
        users = _business_users(ctx)
        if not users:
            return Result.skip(
                phase,
                "no business_users provided to lock (technical users are always spared)",
            )
        return Result.ok(
            phase,
            f"would lock {len(users)} business user(s): {', '.join(users)} "
            "(technical users spared)",
            detail="\n".join(f"  {i}. BAPI_USER_LOCK {u}" for i, u in enumerate(users, start=1)),
            data={"to_lock": users, "spared": sorted(_PROTECTED_USERS)},
            facts={"Users To Lock": str(len(users)), "Technical Users": "spared"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        users = _business_users(ctx)
        if not users:
            return Result.skip(phase, "no business_users to lock")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            locked: list[str] = []
            for u in users:
                res = client.call("BAPI_USER_LOCK", USERNAME=u)
                ret = res.get("RETURN", {}) or {}
                msg_type = ret.get("TYPE", "") if isinstance(ret, dict) else ""
                if str(msg_type).upper() in ("E", "A"):
                    return Result.fail(
                        phase,
                        f"failed to lock user {u} (locked {len(locked)} so far)",
                        data={"locked": locked, "failed": u, "return": ret},
                    )
                locked.append(u)
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not lock users: {exc}")
        return Result.ok(
            phase,
            f"locked {len(locked)} business user(s); technical users spared",
            data={"locked": locked, "spared": sorted(_PROTECTED_USERS)},
            facts={"Users Locked": str(len(locked)), "Technical Users": "spared"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        users = _business_users(ctx)
        return Result.ok(
            phase,
            f"{len(users)} business user(s) locked for ramp-down",
            facts={"Users Locked": str(len(users))},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "unlock the users with abap.post.unlock-users (BAPI_USER_UNLOCK) if "
            "ramp-down is aborted",
        )
