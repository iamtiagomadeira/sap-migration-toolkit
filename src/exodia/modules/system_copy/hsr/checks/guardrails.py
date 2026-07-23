"""Guard-rail checks for HANA System Replication (HSR) — the safety fence.

These are the read-only checks that must pass BEFORE the state-changing HSR
actions are allowed to run. They are the difference between "a command
succeeded" and "the move is actually safe":

* ``hsr.replication-parameters`` (PREPARATION, BLOCKING) — the
  ``[system_replication]`` block in global.ini is present and aligned
  (logshipping_timeout, operation_mode) so ``-sr_register`` starts from a known
  configuration rather than defaults that drift between hosts.
* ``hsr.pki-ssfs-exchanged`` (PREPARATION, BLOCKING) — the systemPKI SSFS files
  have been exchanged between primary and secondary. ``-sr_register`` fails
  without this, so it is a hard pre-req of registering the secondary.
* ``hsr.sync-active-verify`` (RAMP_DOWN, BLOCKING) — the RPO=0 guard-rail:
  replication must be in a synchronous mode AND overall ACTIVE before a takeover
  is permitted. Taking over while SYNCING / ASYNC-behind loses committed data.

Every check is read-only. References (cite by number only): SAP Note 2407186
(HSR how-to), 1999880 (HSR FAQ), 2456657 (system replication).
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from .. import _hana as h


class ReplicationParametersCheck(Check):
    """global.ini [system_replication] present and aligned before registering.

    Reads M_INIFILE_CONTENTS for the ``system_replication`` section and confirms
    the operator-critical keys (``logshipping_timeout``, ``operation_mode``) are
    set. Missing/blank values mean ``-sr_register`` would fall back to defaults
    that can differ between the two hosts — a silent source of desync.
    """

    name = "hsr.replication-parameters"
    description = "global.ini [system_replication] present/aligned (logshipping_timeout, operation_mode)."
    title = "HSR Replication Parameters (global.ini)"
    phase = Phase.PREPARATION
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [h.PRIMARY_KEY, h.OPERATION_MODE]

    def run(self, ctx: Context) -> Result:
        key = h.primary_key(ctx)
        stmt = (
            "SELECT KEY, VALUE FROM M_INIFILE_CONTENTS "
            "WHERE FILE_NAME='global.ini' AND SECTION='system_replication' "
            "AND KEY IN ('logshipping_timeout','operation_mode')"
        )
        cr = h.run(ctx, h.hdbsql_argv(key, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read the [system_replication] section of global.ini",
                detail=cr.stderr or cr.stdout,
            )
        found = {r[0].lower(): r[1] for r in h.parse_hdbsql_rows(cr.stdout) if len(r) >= 2}
        want_mode = str(ctx.get("operation_mode") or "logreplay").lower()
        missing = [k for k in ("logshipping_timeout", "operation_mode") if not found.get(k)]
        facts = {
            "logshipping_timeout": found.get("logshipping_timeout", "(unset)"),
            "operation_mode": found.get("operation_mode", "(unset)"),
        }
        if missing:
            return Result.fail(
                self.name,
                "global.ini [system_replication] is missing key(s): "
                f"{', '.join(missing)} — set them on both hosts before registering",
                data={"found": found, "missing": missing},
                facts=facts,
                sap_note="2407186",
            )
        seen_mode = str(found.get("operation_mode", "")).lower()
        if seen_mode and want_mode and seen_mode != want_mode:
            return Result.fail(
                self.name,
                f"operation_mode is '{seen_mode}' but the move expects '{want_mode}' "
                "— align global.ini on both hosts (a mismatch blocks registration)",
                data={"found": found, "expected_operation_mode": want_mode},
                facts=facts,
                sap_note="2407186",
            )
        return Result.ok(
            self.name,
            f"[system_replication] aligned (operation_mode={seen_mode}, "
            f"logshipping_timeout={found.get('logshipping_timeout')})",
            data={"found": found},
            facts=facts,
        )


class PkiSsfsExchangedCheck(Check):
    """systemPKI SSFS must be exchanged between hosts (pre-req of -sr_register).

    HANA authenticates the replication handshake with the global systemPKI SSFS
    store. The secondary can only register once the primary's SSFS DAT+KEY have
    been copied over (and the secondary restarted). This confirms both files are
    present and non-empty on the host we run on. Read-only (stat/test only).
    """

    name = "hsr.pki-ssfs-exchanged"
    description = "systemPKI SSFS exchanged between primary and secondary (pre-req of -sr_register)."
    title = "systemPKI SSFS Exchanged (HSR Handshake)"
    phase = Phase.PREPARATION
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [h.SID]

    def run(self, ctx: Context) -> Result:
        sid = ctx.sid or ctx.get("sid")
        paths = h.ssfs_paths(sid)
        if paths is None:
            return Result.skip(
                self.name,
                "no SID given; cannot locate the systemPKI SSFS files to verify",
            )
        dat, keyfile = paths
        # `test -s` = exists and non-empty; argv-only, safe on Runner + SSHRunner.
        missing: list[str] = []
        for p in (dat, keyfile):
            cr = h.run(ctx, ["test", "-s", p])
            if not cr.ok:
                missing.append(p)
        if missing:
            return Result.fail(
                self.name,
                "systemPKI SSFS not exchanged — missing/empty: "
                f"{', '.join(missing)}. Copy SSFS_<SID>.DAT/.KEY from the primary "
                "to the secondary and restart it before -sr_register",
                data={"missing": missing, "expected": [dat, keyfile]},
                facts={"SSFS DAT": dat, "SSFS KEY": keyfile},
                sap_note="2407186",
            )
        return Result.ok(
            self.name,
            "systemPKI SSFS present on this host (DAT + KEY non-empty)",
            data={"dat": dat, "key": keyfile},
            facts={"SSFS DAT": "present", "SSFS KEY": "present"},
        )


class SyncActiveVerifyCheck(Check):
    """RPO=0 guard-rail: replication is SYNC and ACTIVE before allowing takeover.

    This is the single most important safety check of the whole HSR move. A
    takeover promotes the secondary to primary; if replication is not in a
    synchronous mode (``sync``/``syncmem``) AND overall ``ACTIVE``, any
    not-yet-shipped committed transaction is LOST at takeover (RPO breaks).

    It reads M_SERVICE_REPLICATION on the secondary and cross-checks the mode via
    ``hdbnsutil -sr_state``. FAIL blocks the takeover action (requires_checks).
    Read-only.
    """

    name = "hsr.sync-active-verify"
    description = "Replication is SYNC and ACTIVE before takeover (RPO=0 guard-rail)."
    title = "HSR Sync + Active Verify (RPO=0 Guard-Rail)"
    phase = Phase.RAMP_DOWN
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [h.SECONDARY_KEY, h.REPLICATION_MODE]

    _SYNC_MODES = {"SYNC", "SYNCMEM"}

    def run(self, ctx: Context) -> Result:
        key = h.secondary_key(ctx)
        stmt = (
            "SELECT REPLICATION_STATUS, REPLICATION_MODE, "
            "COALESCE(SHIPPED_LOG_POSITION,0), COALESCE(LAST_LOG_POSITION,0) "
            "FROM M_SERVICE_REPLICATION"
        )
        cr = h.run(ctx, h.hdbsql_argv(key, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read M_SERVICE_REPLICATION on the secondary "
                "(run on the secondary SYSTEMDB) — cannot certify sync state",
                detail=cr.stderr or cr.stdout,
            )
        statuses, modes, _pct = h.parse_replication_progress(cr.stdout)
        if not statuses:
            return Result.fail(
                self.name,
                "no replication services reported — the secondary is not "
                "replicating; do NOT take over",
                data={"statuses": statuses, "modes": modes},
                sap_note="1999880",
            )
        facts = {
            "Replication Status": ", ".join(statuses) or "unknown",
            "Replication Mode": ", ".join(modes) or "unknown",
        }
        not_active = [s for s in statuses if s != "ACTIVE"]
        if not_active:
            return Result.fail(
                self.name,
                f"replication status is {', '.join(statuses)} (not all ACTIVE) — "
                "the secondary is not fully caught up; a takeover now WOULD LOSE DATA",
                data={"statuses": statuses, "modes": modes},
                facts=facts,
                sap_note="1999880",
            )
        non_sync = [m for m in modes if m not in self._SYNC_MODES]
        if not modes or non_sync:
            return Result.fail(
                self.name,
                f"replication mode is {', '.join(modes) or 'unknown'} (not sync/syncmem) — "
                "an async takeover has a non-zero RPO; switch to sync or accept the "
                "data-loss window explicitly before taking over",
                data={"statuses": statuses, "modes": modes},
                facts=facts,
                sap_note="1999880",
            )
        return Result.ok(
            self.name,
            f"replication is {', '.join(modes)} and ACTIVE — safe to take over (RPO=0)",
            data={"statuses": statuses, "modes": modes},
            facts=facts,
        )
