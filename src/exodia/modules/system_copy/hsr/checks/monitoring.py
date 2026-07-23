"""Downtime/post monitoring checks for HANA System Replication (HSR).

* ``hsr.sync-monitor`` (DOWNTIME, non-blocking) — watches the initial data
  shipping after the secondary is registered: reads M_SERVICE_REPLICATION
  (shipped vs full) and reports the % caught up and whether every service has
  reached ACTIVE. Non-blocking because it is a progress read, not a gate — it
  drives the live progress bar and returns a WARN (not FAIL) while still syncing.
* ``hsr.post-takeover-online`` (DOWNTIME, BLOCKING) — after ``-sr_takeover`` it
  confirms the (former secondary) target is now a PRIMARY and its database is
  online. This is the "did the promotion actually work" gate.

Both read-only. References (cite by number only): SAP Note 2407186 (HSR how-to),
2456657 (system replication).
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from .. import _hana as h


class SyncMonitorCheck(Check):
    """Monitor the initial HSR sync via M_SERVICE_REPLICATION (shipped vs full).

    Reads the per-service replication status and the shipped/full delta sizes on
    the secondary SYSTEMDB, computes the shipped fraction, and reports progress.
    Non-blocking: PASS when everything is ACTIVE, WARN while still shipping (so a
    monitoring sweep doesn't abort a pipeline just because sync is in flight).
    """

    name = "hsr.sync-monitor"
    description = "Monitor the initial HSR sync (M_SERVICE_REPLICATION shipped vs full)."
    title = "HSR Initial Sync Monitor"
    phase = Phase.DOWNTIME
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [h.SECONDARY_KEY]

    def run(self, ctx: Context) -> Result:
        key = h.secondary_key(ctx)
        stmt = (
            "SELECT REPLICATION_STATUS, REPLICATION_MODE, "
            "COALESCE(SHIPPED_DELTA_REPLICA_SIZE,0), COALESCE(FULL_REPLICA_SIZE,0) "
            "FROM M_SERVICE_REPLICATION"
        )
        cr = h.run(ctx, h.hdbsql_argv(key, stmt))
        if not cr.ok:
            return Result.skip(
                self.name,
                "could not read M_SERVICE_REPLICATION on the secondary",
                detail=cr.stderr or cr.stdout,
            )
        statuses, modes, pct = h.parse_replication_progress(cr.stdout)
        if not statuses:
            return Result.warn(
                self.name,
                "no replication services reported yet — sync not started or "
                "the secondary is not registered",
                data={"statuses": statuses},
                sap_note="2407186",
            )
        pct_str = f"{pct:.0f}%" if pct is not None else "n/a"
        facts = {
            "Replication Status": ", ".join(statuses),
            "Replication Mode": ", ".join(modes) or "unknown",
            "Shipped": pct_str,
        }
        if all(s == "ACTIVE" for s in statuses):
            return Result.ok(
                self.name,
                f"initial sync complete — all services ACTIVE ({pct_str} shipped)",
                data={"statuses": statuses, "modes": modes, "percent": pct},
                facts=facts,
            )
        return Result.warn(
            self.name,
            f"sync in progress — status {', '.join(statuses)} ({pct_str} shipped); "
            "wait for ACTIVE before takeover",
            data={"statuses": statuses, "modes": modes, "percent": pct},
            facts=facts,
            sap_note="2407186",
        )


class PostTakeoverOnlineCheck(Check):
    """Confirm the target was promoted to PRIMARY and the DB is online.

    Runs after ``hsr.takeover``. Two signals: ``hdbnsutil -sr_state`` should now
    report ``mode: primary`` on the target, and M_DATABASE should show the system
    online. BLOCKING — if the promotion didn't take, the migration must stop and
    be investigated rather than proceed on a half-promoted system.
    """

    name = "hsr.post-takeover-online"
    description = "Target promoted to PRIMARY and database online after takeover."
    title = "Post-Takeover Online (Target is Primary)"
    phase = Phase.DOWNTIME
    blocking = True

    def parameters(self) -> list[ParamSpec]:
        return [h.SECONDARY_KEY]

    def run(self, ctx: Context) -> Result:
        # 1. Is it now the primary? hdbnsutil -sr_state reports the mode.
        state = h.run(ctx, h.hdbnsutil_argv("-sr_state"))
        text = (state.stdout or "") + (state.stderr or "")
        mode = h.parse_sr_mode(text)

        # 2. Is the database online? Read M_DATABASE on the (now-primary) target.
        key = h.secondary_key(ctx)
        db = h.run(
            ctx,
            h.hdbsql_argv(key, "SELECT DATABASE_NAME, ACTIVE_STATUS FROM M_DATABASE"),
        )
        online = db.ok and ("YES" in db.stdout.upper() or "ONLINE" in db.stdout.upper())
        facts = {
            "Replication Mode": mode or "unknown",
            "Database": "online" if online else "not confirmed",
        }

        if mode == "primary" and online:
            return Result.ok(
                self.name,
                "takeover confirmed — target is now PRIMARY and the database is online",
                data={"mode": mode, "online": online},
                facts=facts,
            )
        if mode is not None and mode != "primary":
            return Result.fail(
                self.name,
                f"target reports mode '{mode}', not 'primary' — the takeover did not "
                "promote this system; do NOT reconnect the application yet",
                data={"mode": mode, "online": online},
                facts=facts,
                sap_note="2407186",
            )
        if not online:
            return Result.fail(
                self.name,
                "target is not confirmed online after takeover — check the DB "
                "started and the license is valid",
                data={"mode": mode, "online": online},
                facts=facts,
                sap_note="2407186",
            )
        return Result.warn(
            self.name,
            "database is online but the primary mode could not be read from "
            "hdbnsutil -sr_state — verify the promotion manually",
            detail=text.strip()[:500],
            data={"mode": mode, "online": online},
            facts=facts,
            sap_note="2407186",
        )
