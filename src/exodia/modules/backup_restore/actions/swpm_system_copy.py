"""Guarded action: headless SWPM (system copy) orchestration.

``SwpmSystemCopyAction`` orchestrates SAP Software Provisioning Manager
(``sapinst``) in headless (unattended) mode. It does NOT reimplement SWPM — it
drives it, following three logical sub-phases (documented in ``dry_run``):

1. **prepare_inifile** — validate/parametrise an EXISTING ``inifile.params``
   (Ansible ``sap_swpm`` convention). ``instkey.pkey`` is a SECRET: presence is
   confirmed, content is never read or logged.
2. **run_sapinst (headless)** — launch sapinst with the correct SAPINST_* env
   as an argv list. OBSERVER MODE is the default: the GUI server is left ON so
   an operator can attach at ``https://<host>:4237/sapinst/docs/index.html`` for
   a manual handoff. ``SAPINST_SKIP_ERRORSTEP`` is NEVER set.
3. **monitor_sapinst** — parse output/logs to detect the current phase/state.
   *waiting for input* → :meth:`Result.warn` with the GUI URL (observer-mode
   handoff). An error → :meth:`Result.fail` (PAUSE) — sapinst is never killed.

The 6-step safe-execution flow (dry-run → confirm → execute → verify) is
provided by the base ``Action.run_guarded`` and is NOT reimplemented here.

References (cite by number only): SAP Note 2230669 (SWPM / product IDs / SL
Protocol), SAP Note 950619 (system copy inifile).
"""

from __future__ import annotations

from pathlib import Path

from exodia.core import Context, Result
from exodia.core.base import Action

from .swpm.planner import (
    InifileError,
    ProgressReport,
    RunState,
    build_plan,
    gui_url,
    parse_progress,
    validate_inifile,
)


class SwpmSystemCopyAction(Action):
    """Orchestrate a headless SWPM system copy (observer-mode GUI handoff)."""

    name = "backup-restore.swpm.system-copy"
    description = "Orchestrate SWPM sapinst headless (observer-mode GUI handoff)."
    destructive = True
    requires_checks: list[str] = []

    # --- parameter resolution -------------------------------------------------

    @staticmethod
    def _inifile(ctx: Context) -> str | None:
        val = ctx.get("inifile")
        return str(val) if val else None

    @staticmethod
    def _product_id(ctx: Context) -> str:
        return str(ctx.get("product_id", ""))

    @staticmethod
    def _sapinst_path(ctx: Context) -> str:
        return str(ctx.get("sapinst_path", "/usr/sap/SWPM/sapinst"))

    @staticmethod
    def _start_guiserver(ctx: Context) -> bool:
        # Default True: observer mode keeps the GUI server up for a handoff.
        return bool(ctx.get("start_guiserver", True))

    @staticmethod
    def _log_path(ctx: Context) -> str | None:
        val = ctx.get("sapinst_log")
        return str(val) if val else None

    def _build_plan(self, ctx: Context) -> object:
        return build_plan(
            sapinst_path=self._sapinst_path(ctx),
            inifile_path=self._inifile(ctx) or "",
            product_id=self._product_id(ctx),
            start_guiserver=self._start_guiserver(ctx),
        )

    # --- phase 1: prepare_inifile (used by dry_run) ---------------------------

    def _prepare(self, ctx: Context, phase: str) -> Result:
        """Validate the inifile and the required params. Secret-free."""
        product_id = self._product_id(ctx)
        if not product_id:
            return Result.fail(
                phase,
                "no product_id given (set params.product_id; see SAP Note 2230669)",
            )
        try:
            info = validate_inifile(self._inifile(ctx))
        except InifileError as exc:
            return Result.fail(phase, str(exc), sap_note="950619")
        return Result.ok(
            phase,
            f"inifile validated ({len(info.keys_found)} key(s) present)",
            data={
                "inifile": info.path,
                "keys_found": info.keys_found,
                # Confirm the secret's presence WITHOUT ever reading its value.
                "instkey_pkey_present": info.has_secret_pkey,
                "product_id": product_id,
            },
        )

    # --- Action phase methods -------------------------------------------------

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        # Sub-phase 1: validate the inifile (no side effects).
        prep = self._prepare(ctx, phase)
        if prep.status.is_blocking:
            return prep

        # Sub-phase 2: describe the sapinst command + env we WOULD run.
        plan = self._build_plan(ctx)
        guiserver_on = self._start_guiserver(ctx)
        detail_lines = [
            "sub-phase 1 prepare_inifile: validate existing inifile.params (done above)",
            f"sub-phase 2 run_sapinst (headless): {plan.display}",  # type: ignore[attr-defined]
            "  strategy: launched detached (nohup/setsid) so the run survives the session",
            (
                "sub-phase 3 monitor_sapinst: parse sapinst_dev.log / stdout for phase + state; "
                "'waiting for input' => WARN with GUI URL (observer-mode handoff); "
                "error => FAIL (pause, never kill)"
            ),
        ]
        if guiserver_on:
            detail_lines.append(f"  observer-mode GUI handoff URL: {gui_url(ctx.host)}")

        return Result.ok(
            phase,
            f"would run headless SWPM for {self._product_id(ctx)}; nothing executed",
            detail="\n".join(detail_lines),
            data={
                "argv": plan.argv,  # type: ignore[attr-defined]
                "env": plan.env,  # type: ignore[attr-defined]
                "observer_mode": guiserver_on,
                "gui_url": gui_url(ctx.host) if guiserver_on else None,
                "inifile": prep.data.get("inifile"),
                "instkey_pkey_present": prep.data.get("instkey_pkey_present"),
            },
            sap_note="2230669",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        # Re-validate before doing anything state-changing.
        prep = self._prepare(ctx, phase)
        if prep.status.is_blocking:
            return prep

        plan = self._build_plan(ctx)
        runner = ctx.runner()

        # Launch strategy: sapinst is long-running. We run it detached via setsid
        # + nohup so it survives the controlling session; the SAPINST_* env is
        # exported inline via `env` (argv list — never shell=True). The GUI
        # server is left ON by default for the observer-mode handoff.
        env_argv = [f"{k}={v}" for k, v in plan.env.items()]  # type: ignore[attr-defined]
        launch_argv = [
            "setsid",
            "nohup",
            "env",
            *env_argv,
            *plan.argv,  # type: ignore[attr-defined]
        ]

        cr = runner.run(launch_argv, timeout=int(ctx.get("launch_timeout", 300)))
        # Errors PAUSE (FAIL) — we never kill sapinst. A non-zero launch is a
        # startup failure (bad path/permissions), surfaced for the operator.
        if not cr.ok:
            return Result.fail(
                phase,
                f"sapinst failed to launch (exit {cr.exit_code}) — run paused, not killed",
                detail=cr.stderr or cr.stdout,
                data={"exit_code": cr.exit_code, "argv": launch_argv},
                sap_note="2230669",
            )

        # Inspect whatever output the launch produced to classify early state.
        report = parse_progress(cr.stdout + "\n" + cr.stderr)
        return self._report_to_result(ctx, phase, report, launched=True, launch_argv=launch_argv)

    def verify(self, ctx: Context) -> Result:
        """Sub-phase 3: monitor/verify sapinst end state from its log."""
        phase = f"{self.name}.verify"
        log_text = self._read_log(ctx)
        if log_text is None:
            # No log to parse yet: this is an observer-mode handoff, not a
            # failure — sapinst may be waiting on the GUI.
            return Result.warn(
                phase,
                "no sapinst log available yet — check the GUI for a manual handoff",
                data={"gui_url": gui_url(ctx.host)},
            )
        report = parse_progress(log_text)
        return self._report_to_result(ctx, phase, report, launched=False)

    def rollback(self, ctx: Context) -> Result:
        # A system copy is NOT auto-reversible: reverting requires re-provisioning
        # or restoring the previous system state per the migration runbook.
        return Result.skip(
            f"{self.name}.rollback",
            "no automatic rollback for a system copy — follow the migration "
            "runbook to re-provision/restore (see SAP Note 2230669)",
            sap_note="2230669",
        )

    # --- helpers --------------------------------------------------------------

    def _read_log(self, ctx: Context) -> str | None:
        log_path = self._log_path(ctx)
        if not log_path:
            return None
        path = Path(log_path)
        if not path.is_file():
            return None
        try:
            return path.read_text(errors="replace")
        except OSError:
            return None

    def _report_to_result(
        self,
        ctx: Context,
        phase: str,
        report: ProgressReport,
        *,
        launched: bool,
        launch_argv: list[str] | None = None,
    ) -> Result:
        """Map a parsed :class:`ProgressReport` onto a Result (secret-free)."""
        data: dict[str, object] = {"phase": report.phase, "state": report.state.value}
        if launch_argv is not None:
            data["argv"] = launch_argv

        if report.state is RunState.WAITING_FOR_INPUT:
            data["gui_url"] = gui_url(ctx.host)
            return Result.warn(
                phase,
                f"sapinst is waiting for input — manual handoff via GUI "
                f"(observer mode){f'; phase: {report.phase}' if report.phase else ''}",
                detail=f"Open the SWPM GUI: {gui_url(ctx.host)}",
                data=data,
            )

        if report.state is RunState.ERROR:
            return Result.fail(
                phase,
                f"sapinst reported an error — run PAUSED (not killed)"
                f"{f'; phase: {report.phase}' if report.phase else ''}",
                detail=report.detail,
                data=data,
                sap_note="2230669",
            )

        if report.state is RunState.DONE:
            return Result.ok(
                phase,
                "sapinst completed successfully",
                data=data,
            )

        # RUNNING
        if launched:
            data["gui_url"] = gui_url(ctx.host)
            return Result.warn(
                phase,
                "sapinst launched and running headless — monitor via log or GUI",
                detail=f"Observer-mode GUI: {gui_url(ctx.host)}",
                data=data,
            )
        return Result.warn(
            phase,
            "sapinst still running (no completion marker in log yet)",
            data=data,
        )
