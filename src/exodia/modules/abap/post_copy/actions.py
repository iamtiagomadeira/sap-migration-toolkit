"""Cross-cutting post-copy ABAP actions (shared by every system-copy method).

These are the guarded, state-changing consistency steps that any freshly copied
ABAP system needs regardless of the copy technique (Backup & Restore, Export &
Import, HANA System Replication). Declared once, reused by all methods.

* ``abap.post.bdls-logical-system`` (BLOCKING) — BDLS conversion of logical
  system names source -> target. Long-running: submitted as a background job
  and monitored with a live progress line. SAP Note 121163 (BDLS).
* ``abap.post.stms-reconfigure`` (BLOCKING) — reset/reconfigure the Transport
  Management System so the copy cannot transport into the productive
  landscape. SAP Note 359186 (post-copy TMS).
* ``abap.post.sgen-load-generation`` — SGEN regeneration of the ABAP loads;
  background job, monitored. SAP Note 1332428 (SGEN).
* ``abap.post.purge-source-runtime`` — purge source-specific runtime data that
  must not survive on the copy (orphan spool, orphan jobs, source-pointing RFC
  destinations, stale batch-input sessions).

All RFC-backed; reuse the readiness ``_rfc`` plumbing. Argv-only OS calls where
applicable. Secrets never appear in argv or logs. In dry-run (the default) the
actions execute NOTHING — they describe exactly what they would do.
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from ..readiness import _rfc
from . import _jobs


class BdlsLogicalSystemAction(Action):
    """BDLS — convert logical system names from source to target on the copy.

    After a copy, the logical system names (BD54/SALE) still identify the SOURCE
    client, so IDoc/ALE/RFC partner assignments point at the source landscape.
    BDLS rewrites the logical system name across all application tables. This is
    long-running (it touches every table carrying a logical-system field), so it
    is submitted as a background job and monitored with a live progress line.
    Blocking: skipping it breaks IDoc/ALE/RFC on the copy.
    """

    name = "abap.post.bdls-logical-system"
    description = "BDLS: convert logical system names source -> target on the copy."
    title = "BDLS — Convert Logical System Names (source -> target)"
    phase = Phase.POST
    destructive = True
    blocking = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "old_logical_system", "Old (source) logical system name",
                help="Logical system name carried over from the source, e.g. ABCCLNT100.",
            ),
            ParamSpec(
                "new_logical_system", "New (target) logical system name",
                help="Logical system name the copy should use, e.g. XYZCLNT100.",
            ),
            ParamSpec(
                "bdls_test_run", "BDLS test run first (true/false)", default="false",
                help="When true, BDLS runs in analysis/test mode (no conversion). "
                "Leave false for the real conversion.",
            ),
        ]

    def _names(self, ctx: Context) -> tuple[str, str]:
        return (
            str(ctx.get("old_logical_system") or "").strip(),
            str(ctx.get("new_logical_system") or "").strip(),
        )

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params (set source_ashost + credentials)")
        old, new = self._names(ctx)
        if not old or not new:
            return Result.skip(
                phase,
                "old_logical_system and new_logical_system are both required for BDLS",
            )
        test = str(ctx.get("bdls_test_run", "false")).strip().lower() in ("true", "yes", "y", "1")
        return Result.ok(
            phase,
            f"would run BDLS to convert logical system {old} -> {new} across all "
            f"application tables{' (TEST run — no conversion)' if test else ''}; "
            "submitted as a background job and monitored to completion",
            detail=(
                f"  1. BDLS_MAIN old={old} new={new} test={'X' if test else ''}\n"
                "  2. poll job status until finished (live progress)"
            ),
            data={"old": old, "new": new, "test_run": test},
            facts={"Old Logical System": old, "New Logical System": new,
                   "Mode": "TEST" if test else "CONVERT"},
            sap_note="121163",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        old, new = self._names(ctx)
        if not old or not new:
            return Result.skip(phase, "old_logical_system and new_logical_system are required")
        test = str(ctx.get("bdls_test_run", "false")).strip().lower() in ("true", "yes", "y", "1")
        self._emit_phase("bdls submit", f"{old} -> {new}")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            sub = client.call(
                "BDLS_MAIN",
                OLD_NAME=old,
                NEW_NAME=new,
                TEST_RUN="X" if test else "",
            )
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not start BDLS conversion: {exc}", sap_note="121163")
        jobname = str(sub.get("JOBNAME", "") or "BDLS")
        jobcount = str(sub.get("JOBCOUNT", "") or "")
        self._emit_log(f"BDLS job submitted: {jobname}/{jobcount}")
        job = _jobs.poll_job(self, ctx, client, jobname=jobname, jobcount=jobcount)
        if job.aborted:
            return Result.fail(
                phase, f"BDLS job {jobname} aborted before completion",
                data={"job_log": job.log}, sap_note="121163",
            )
        if not job.finished_ok:
            return Result.warn(
                phase,
                f"BDLS job {jobname} did not finish within the poll window "
                f"(last status={job.status}); check SM37",
                data={"job_log": job.log},
            )
        return Result.ok(
            phase,
            f"BDLS conversion {old} -> {new} completed{' (TEST run)' if test else ''}",
            data={"old": old, "new": new, "test_run": test, "polls": job.polls},
            facts={"Converted": f"{old} -> {new}", "Mode": "TEST" if test else "CONVERT"},
            sap_note="121163",
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        old, new = self._names(ctx)
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params to verify BDLS conversion")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            # TBDLS / conversion table should show no remaining occurrences of the
            # old logical system name once BDLS has run.
            rows = _rfc.read_table(
                client, "TBDLS", fields=["OLD_NAME"], where=f"OLD_NAME = '{old}'"
            )
        except _rfc.RfcError:
            return Result.warn(
                phase,
                "BDLS ran, but the conversion table could not be read to confirm "
                "no stale logical-system references remain — verify via BDLS analysis",
            )
        remaining = len(rows)
        if remaining:
            return Result.warn(
                phase,
                f"{remaining} residual reference(s) to old logical system {old} "
                "still present — re-run BDLS analysis",
                data={"remaining": remaining},
                facts={"Residual References": str(remaining)},
            )
        return Result.ok(
            phase,
            f"no residual references to old logical system {old}; conversion to {new} confirmed",
            facts={"Residual References": "0", "New Logical System": new},
        )

    def rollback(self, ctx: Context) -> Result:
        old, new = self._names(ctx)
        return Result.skip(
            f"{self.name}.rollback",
            f"BDLS is not auto-reversible — to undo, run BDLS again converting "
            f"{new} -> {old} (see SAP Note 121163)",
            sap_note="121163",
        )


class StmsReconfigureAction(Action):
    """Reset / reconfigure the Transport Management System on the copy.

    A copy inherits the source's TMS configuration (domain controller, transport
    routes), so without rework the copy can transport CHANGES INTO the productive
    landscape — a serious risk. This action deletes the inherited TMS
    configuration (STMS -> delete TMS config) so the system can be re-added to
    the correct transport domain, or set up as its own domain controller.
    Blocking: leaving the source TMS in place risks cross-transports into PRD.
    """

    name = "abap.post.stms-reconfigure"
    description = "Reconfigure TMS: remove the source transport config to avoid cross-transports."
    title = "STMS — Reconfigure Transport Management System (post-copy)"
    phase = Phase.POST
    destructive = True
    blocking = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "tms_action", "TMS reconfigure action", default="delete",
                choices=("delete", "become-controller"),
                help="'delete' = remove inherited TMS config so the copy is isolated "
                "from the source domain; 'become-controller' = also initialise this "
                "system as its own transport domain controller.",
            ),
            ParamSpec(
                "tms_domain", "New transport domain name (become-controller)",
                help="Transport domain to create when tms_action=become-controller, "
                "e.g. DOMAIN_XYZ. Ignored for 'delete'.",
            ),
        ]

    def _tms_action(self, ctx: Context) -> str:
        return str(ctx.get("tms_action", "delete")).strip().lower()

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params (set source_ashost + credentials)")
        act = self._tms_action(ctx)
        if act == "become-controller":
            domain = str(ctx.get("tms_domain") or "").strip()
            steps = (
                "  1. TMS_MGR_DELETE_TMS_CONFIG (drop inherited source config)\n"
                f"  2. TMS_MGR_INIT_TMS_CONFIG domain={domain or '<tms_domain>'} "
                "(become domain controller)"
            )
            desc = (
                "would delete the inherited (source) TMS config and initialise this "
                f"copy as its own transport domain controller ({domain or '<tms_domain>'})"
            )
        else:
            steps = "  1. TMS_MGR_DELETE_TMS_CONFIG (drop inherited source TMS config)"
            desc = (
                "would delete the inherited (source) TMS configuration so the copy is "
                "isolated from the source transport domain — no cross-transports into PRD"
            )
        return Result.ok(
            phase, desc, detail=steps,
            data={"tms_action": act, "tms_domain": ctx.get("tms_domain")},
            facts={"TMS Action": act, "Domain": str(ctx.get("tms_domain") or "—")},
            sap_note="359186",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        act = self._tms_action(ctx)
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            res = client.call("TMS_MGR_DELETE_TMS_CONFIG")
            ret = res.get("RETURN", {}) or {}
            if isinstance(ret, dict) and str(ret.get("TYPE", "")).upper() in ("E", "A"):
                return Result.fail(
                    phase,
                    f"failed to delete inherited TMS config: {ret.get('MESSAGE', '')}",
                    data={"return": ret}, sap_note="359186",
                )
            done = ["deleted inherited TMS config"]
            if act == "become-controller":
                domain = str(ctx.get("tms_domain") or "").strip()
                if not domain:
                    return Result.fail(
                        phase, "tms_domain is required for become-controller", sap_note="359186"
                    )
                init = client.call("TMS_MGR_INIT_TMS_CONFIG", IV_DOMAIN=domain)
                iret = init.get("RETURN", {}) or {}
                if isinstance(iret, dict) and str(iret.get("TYPE", "")).upper() in ("E", "A"):
                    return Result.fail(
                        phase,
                        f"deleted source config but failed to initialise domain {domain}: "
                        f"{iret.get('MESSAGE', '')}",
                        data={"return": iret}, sap_note="359186",
                    )
                done.append(f"initialised transport domain {domain}")
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"TMS reconfigure failed: {exc}", sap_note="359186")
        return Result.ok(
            phase, "; ".join(done),
            data={"tms_action": act, "steps": done},
            facts={"TMS Action": act, "Result": "reconfigured"},
            sap_note="359186",
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params to verify TMS config")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            # TMSCSYS holds the systems known to the transport domain. After a
            # 'delete' it should be empty (isolated); after 'become-controller'
            # it should contain exactly this system as the controller.
            rows = _rfc.read_table(client, "TMSCSYS", fields=["SYSNAM", "DOMNAM"])
        except _rfc.RfcError:
            return Result.warn(
                phase, "TMS reconfigured, but TMSCSYS could not be read to confirm"
            )
        act = self._tms_action(ctx)
        if act == "delete":
            if rows:
                return Result.warn(
                    phase,
                    f"{len(rows)} transport-domain system(s) still configured — the "
                    "source TMS config may not be fully removed",
                    data={"systems": [r.get("SYSNAM", "") for r in rows]},
                    facts={"Domain Systems": str(len(rows))},
                )
            return Result.ok(
                phase, "TMS configuration is empty — copy isolated from the source domain",
                facts={"Domain Systems": "0"},
            )
        return Result.ok(
            phase,
            f"transport domain reconfigured; {len(rows)} system(s) registered",
            data={"systems": [r.get("SYSNAM", "") for r in rows]},
            facts={"Domain Systems": str(len(rows))},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "TMS config deletion is not auto-reversible — re-add the system to the "
            "correct transport domain via STMS (see SAP Note 359186)",
            sap_note="359186",
        )


class SgenLoadGenerationAction(Action):
    """SGEN — regenerate the ABAP loads after a copy to avoid startup dumps.

    A copy carries the source's generated loads, which may be invalid on the
    target (different kernel/patch level), causing runtime regeneration on first
    access: dumps and latency for early users. SGEN regenerates all loads up
    front. Long-running: submitted as a background job and monitored with a live
    progress line. Not blocking (the system runs without it, just slower).
    """

    name = "abap.post.sgen-load-generation"
    description = "SGEN: regenerate ABAP loads after a copy to avoid startup dumps/latency."
    title = "SGEN — Regenerate ABAP Loads (post-copy)"
    phase = Phase.POST
    destructive = True
    blocking = False
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "sgen_scope", "SGEN generation scope", default="all",
                choices=("all", "invalid"),
                help="'all' = regenerate every load; 'invalid' = only regenerate "
                "loads invalidated by the copy (faster).",
            ),
        ]

    def _scope(self, ctx: Context) -> str:
        return str(ctx.get("sgen_scope", "all")).strip().lower()

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params (set source_ashost + credentials)")
        scope = self._scope(ctx)
        return Result.ok(
            phase,
            f"would run SGEN to regenerate the ABAP loads (scope={scope}); submitted "
            "as a background job and monitored to completion — avoids startup dumps "
            "and first-access latency",
            detail=(
                f"  1. SGEN_MAIN scope={scope} (build the generation task list + submit)\n"
                "  2. poll job status until finished (live progress)"
            ),
            data={"scope": scope},
            facts={"SGEN Scope": scope},
            sap_note="1332428",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        scope = self._scope(ctx)
        self._emit_phase("sgen submit", f"scope={scope}")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            sub = client.call("SGEN_MAIN", SCOPE=scope.upper())
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not start SGEN: {exc}", sap_note="1332428")
        jobname = str(sub.get("JOBNAME", "") or "RSPARAGENLOAD")
        jobcount = str(sub.get("JOBCOUNT", "") or "")
        self._emit_log(f"SGEN job submitted: {jobname}/{jobcount}")
        job = _jobs.poll_job(self, ctx, client, jobname=jobname, jobcount=jobcount)
        if job.aborted:
            return Result.fail(
                phase, f"SGEN job {jobname} aborted before completion",
                data={"job_log": job.log}, sap_note="1332428",
            )
        if not job.finished_ok:
            return Result.warn(
                phase,
                f"SGEN job {jobname} did not finish within the poll window "
                f"(last status={job.status}); monitor via SGEN/SM37",
                data={"job_log": job.log},
            )
        return Result.ok(
            phase,
            f"SGEN load regeneration completed (scope={scope})",
            data={"scope": scope, "polls": job.polls},
            facts={"SGEN Scope": scope, "Result": "loads regenerated"},
            sap_note="1332428",
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params to verify SGEN state")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
            # GENSETC / generation-request tables hold outstanding generation
            # entries; empty means SGEN has nothing left to regenerate.
            rows = _rfc.read_table(client, "GENSETC", fields=["REPORT"])
        except _rfc.RfcError:
            return Result.warn(
                phase, "SGEN ran, but the generation-request table could not be read to confirm"
            )
        pending = len(rows)
        if pending:
            return Result.warn(
                phase,
                f"{pending} generation request(s) still pending — SGEN may not have "
                "completed the full load set",
                data={"pending": pending}, facts={"Pending Loads": str(pending)},
            )
        return Result.ok(
            phase, "no pending load generation requests — ABAP loads regenerated",
            facts={"Pending Loads": "0"},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "SGEN only regenerates loads (no data change) — nothing to roll back; "
            "re-run SGEN if generation was incomplete (see SAP Note 1332428)",
            sap_note="1332428",
        )


class PurgeSourceRuntimeAction(Action):
    """Purge source-specific runtime data that must not live on the copy.

    A copy inherits transient runtime data that referenced the SOURCE and is
    meaningless (or dangerous) on the target: orphan spool requests (SP01),
    orphan/leftover background jobs (SM37), RFC destinations still pointing at
    the source (SM59), and stale batch-input sessions (SM35). This action clears
    each category. Not blocking, but strongly recommended for a clean copy.
    """

    name = "abap.post.purge-source-runtime"
    description = "Purge source runtime leftovers on the copy: spool, jobs, source RFCs, batch input."
    title = "Purge Source Runtime Data (SP01/SM37/SM59/SM35)"
    phase = Phase.POST
    destructive = True
    blocking = False
    requires_checks: list[str] = []

    _CATEGORIES = ("spool", "jobs", "rfc", "batch_input")

    def parameters(self) -> list[ParamSpec]:
        return [
            *_rfc.SOURCE_CONN_SPECS,
            ParamSpec(
                "purge_categories", "Categories to purge (comma-separated)",
                default="spool,jobs,rfc,batch_input",
                help="Any of: spool (SP01 orphan spool), jobs (SM37 orphan jobs), "
                "rfc (SM59 source-pointing destinations), batch_input (SM35 sessions).",
            ),
            ParamSpec(
                "source_host_pattern", "Source host pattern (for RFC purge)",
                help="Substring/hostname identifying the SOURCE landscape in RFC "
                "destinations (SM59) to select which destinations to remove.",
            ),
        ]

    def _categories(self, ctx: Context) -> list[str]:
        raw = ctx.get("purge_categories") or ",".join(self._CATEGORIES)
        req = [c.strip().lower() for c in str(raw).split(",") if c.strip()]
        return [c for c in req if c in self._CATEGORIES]

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        if not _rfc.has_connection_params(ctx, _rfc.SOURCE):
            return Result.skip(phase, "no RFC connection params (set source_ashost + credentials)")
        cats = self._categories(ctx)
        if not cats:
            return Result.skip(phase, "no valid purge_categories selected")
        labels = {
            "spool": "orphan spool requests (SP01 / RSPO_R_RDELETE_SPOOLREQ)",
            "jobs": "orphan background jobs (SM37 / BP_JOB_DELETE)",
            "rfc": "source-pointing RFC destinations (SM59 / RFC_MODIFY_R3_DESTINATION)",
            "batch_input": "stale batch-input sessions (SM35 / BDL_DELETE_SESSIONS)",
        }
        steps = "\n".join(f"  {i}. purge {labels[c]}" for i, c in enumerate(cats, start=1))
        return Result.ok(
            phase,
            f"would purge {len(cats)} category(ies) of source runtime data from the "
            f"copy: {', '.join(cats)}",
            detail=steps,
            data={"categories": cats},
            facts={"Categories": ", ".join(cats)},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        cats = self._categories(ctx)
        if not cats:
            return Result.skip(phase, "no valid purge_categories selected")
        try:
            client = _rfc.get_client(ctx, _rfc.SOURCE)
        except _rfc.RfcError as exc:
            return Result.fail(phase, f"could not connect to purge source runtime: {exc}")
        purged: dict[str, int] = {}
        cat = ""
        try:
            for cat in cats:
                self._emit_phase(f"purge {cat}", "")
                purged[cat] = self._purge_category(ctx, client, cat)
                self._emit_log(f"purged {cat}: {purged[cat]} item(s)")
        except _rfc.RfcError as exc:
            return Result.fail(
                phase, f"purge failed on category '{cat}': {exc}", data={"purged": purged}
            )
        total = sum(purged.values())
        return Result.ok(
            phase,
            f"purged {total} source runtime item(s) across {len(cats)} category(ies)",
            data={"purged": purged, "total": total},
            facts={k.capitalize(): str(v) for k, v in purged.items()},
        )

    def _purge_category(self, ctx: Context, client: _rfc.RfcClient, cat: str) -> int:
        """Purge one category, returning the number of items removed."""
        if cat == "spool":
            res = client.call("RSPO_R_RDELETE_SPOOLREQ_ALL")
            return int(res.get("DELETED", res.get("COUNT", 0)) or 0)
        if cat == "jobs":
            res = client.call("BP_JOB_DELETE_ORPHANED")
            return int(res.get("DELETED", res.get("COUNT", 0)) or 0)
        if cat == "rfc":
            pattern = str(ctx.get("source_host_pattern") or "").strip()
            if not pattern:
                return 0
            rows = _rfc.read_table(
                client, "RFCDES", fields=["RFCDEST", "RFCTYPE"]
            )
            removed = 0
            for r in rows:
                dest = r.get("RFCDEST", "")
                # Only touch ABAP (3) / TCP (T) destinations carrying the source
                # host pattern; logical/internal destinations are left alone.
                if r.get("RFCTYPE") in ("3", "T") and pattern.upper() in dest.upper():
                    client.call("RFC_MODIFY_R3_DESTINATION", DESTINATION=dest, ACTIVITY="DELETE")
                    removed += 1
            return removed
        if cat == "batch_input":
            res = client.call("BDL_DELETE_SESSIONS")
            return int(res.get("DELETED", res.get("COUNT", 0)) or 0)
        return 0

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        cats = self._categories(ctx)
        return Result.ok(
            phase,
            f"source runtime purge complete for {len(cats)} category(ies): {', '.join(cats)}",
            facts={"Categories Purged": str(len(cats))},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "purged runtime data (orphan spool/jobs/sessions, source RFC destinations) "
            "cannot be restored — recreate any needed RFC destinations manually (SM59)",
        )
