"""Guarded Java PI/PO post-copy actions (TIA-65).

After a HANA backup/restore of a Java PI/PO (NetWeaver AS Java) system, the
target will NOT start until the Java post-copy steps are executed:

1. ``pipo.rebuild-secstore``  — re-key the secure store with the correct key phrase.
2. ``pipo.register-sld``      — re-point the SLD data supplier to the target SLD.
3. ``pipo.fix-rfc-jco``       — re-point JCo/RFC destinations to the target hosts.
4. ``pipo.reconfigure-ume``   — re-point the UME datasource to the target schema.

An orchestrator ``pipo.postcopy-all`` runs the four in the mandatory order,
stopping at the first failure (fail-safe).

Every action is *guarded*: the 6-step safe-execution flow (dry-run -> confirm ->
execute -> verify) comes from :class:`exodia.core.base.Action`.``run_guarded`` and
is NOT reimplemented here — each class only supplies the four phase methods.

SECURITY INVARIANT (non-negotiable):
    The secure-store key phrase and any DB/SLD password are SECRETS. They are
    handed to the AS Java tool exclusively via ``stdin`` (``input_text``) or a
    caller-protected file. They are NEVER placed on the command line (argv),
    never returned in a Result (summary/detail/data), and never logged. The
    ``verify`` step confirms the secure store *opens* without ever revealing it.

SAP Notes (referenced by NUMBER only — never reproduced): 2230669 (SWPM system
copy Java), 1642148 (secure store), 718383 (UME), 1043195 (JCo RFC).
"""

from __future__ import annotations

from pathlib import Path

from exodia.core import Context, Result
from exodia.core.base import Action

from ..checks._common import (
    instance_dir,
    java_schema,
    redact,
    sapcontrol_argv,
    sid,
)


# --------------------------------------------------------------------------- #
# Shared secret handling
# --------------------------------------------------------------------------- #
def _read_secret(ctx: Context, file_param: str, value_param: str) -> str | None:
    """Resolve a secret from a protected file first, then a raw param.

    Precedence: ``<file_param>`` (path to a caller-protected file whose *content*
    is the secret) wins over ``<value_param>`` (the secret passed inline as a
    param, discouraged but supported for non-interactive runs).

    The returned string is destined for ``input_text`` (stdin) ONLY. It must
    never reach argv, a Result, or a log line. Returns ``None`` when neither is
    configured, so the caller can fail cleanly without inventing a secret.
    """
    path = ctx.get(file_param)
    if path:
        try:
            text = Path(str(path)).read_text(encoding="utf-8")
        except OSError:
            return None
        # Strip a single trailing newline the operator's editor may have added,
        # but preserve the phrase exactly otherwise.
        return text[:-1] if text.endswith("\n") else text
    value = ctx.get(value_param)
    return str(value) if value else None


def _secret_source(ctx: Context, file_param: str, value_param: str) -> str:
    """Human-readable, NON-secret description of where the secret came from."""
    if ctx.get(file_param):
        return f"protected file at {ctx.get(file_param)} (via stdin)"
    if ctx.get(value_param):
        return "inline param (via stdin; not logged)"
    return "NOT CONFIGURED"


class _PostCopyAction(Action):
    """Common base: marks destructive and provides a documented-only rollback."""

    destructive = True

    #: one-line reference used by the default rollback message
    rollback_hint: str = "see runbook for the manual reversal steps"

    def rollback(self, ctx: Context) -> Result:
        # Java post-copy steps are not auto-reversible: reverting means restoring
        # the previously-saved configuration artefact (documented runbook step).
        return Result.skip(
            f"{self.name}.rollback",
            f"no automatic rollback — {self.rollback_hint}",
        )


# --------------------------------------------------------------------------- #
# 1. Secure store (SECSTORE)
# --------------------------------------------------------------------------- #
class RebuildSecStoreAction(_PostCopyAction):
    """Rebuild/re-key the AS Java secure store on the target with the key phrase.

    The key phrase is fed to the secure-store tool over stdin — NEVER argv.
    """

    name = "pipo.rebuild-secstore"
    description = "Re-key AS Java secure store on target (key phrase via stdin, never logged)."
    requires_checks = ["pipo.secstore-present", "pipo.as-java-up"]
    rollback_hint = (
        "restore the previous SecStore.properties + SecStore.key pair from the "
        "pre-change backup, then re-run (SAP Note 1642148)"
    )

    def _tool(self, ctx: Context) -> str:
        return str(
            ctx.get("secstore_tool") or f"{instance_dir(ctx)}/j2ee/configtool/secure-store.sh"
        )

    def _store_dir(self, ctx: Context) -> str:
        return str(ctx.get("secstore_dir") or f"{instance_dir(ctx)}/SDM/program/config")

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        tool = self._tool(ctx)
        store = self._store_dir(ctx)
        # NOTE: the key phrase is intentionally absent from this argv preview.
        argv_preview = [tool, "-mode", "rekey", "-store", store]
        source = _secret_source(ctx, "key_phrase_file", "key_phrase")
        return Result.ok(
            phase,
            f"[{sid(ctx)}] would re-key secure store at {store}; key phrase from {source}",
            detail=(
                "  1. " + " ".join(argv_preview) + "  (key phrase piped via stdin)\n"
                "  2. verify: open the store read-only to confirm the phrase decrypts it"
            ),
            data={"tool": tool, "store": store, "command": argv_preview},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        phrase = _read_secret(ctx, "key_phrase_file", "key_phrase")
        if not phrase:
            return Result.fail(
                phase,
                "no key phrase available — set params.key_phrase_file (protected file) "
                "or params.key_phrase; it is never taken from the command line",
            )
        tool = self._tool(ctx)
        store = self._store_dir(ctx)
        # Key phrase over stdin (input_text) — argv carries NO secret.
        cr = ctx.runner().run(
            [tool, "-mode", "rekey", "-store", store],
            input_text=phrase,
        )
        if not cr.ok:
            return Result.fail(
                phase,
                "secure store re-key failed — key phrase may be wrong or the store file is inconsistent",
                detail=redact(cr.stderr or cr.stdout),
                data={"store": store, "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            f"secure store re-keyed for {sid(ctx)} (key phrase not logged)",
            data={"store": store},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        phrase = _read_secret(ctx, "key_phrase_file", "key_phrase")
        if not phrase:
            return Result.warn(
                phase,
                "cannot verify secure store opens — no key phrase available for the read-only check",
            )
        tool = self._tool(ctx)
        store = self._store_dir(ctx)
        # 'check' mode opens the store read-only; phrase again via stdin only.
        cr = ctx.runner().run(
            [tool, "-mode", "check", "-store", store],
            input_text=phrase,
        )
        if not cr.ok:
            return Result.fail(
                phase,
                "secure store did not open — the key phrase does not match the store on the target",
                detail=redact(cr.stderr or cr.stdout),
                data={"store": store},
            )
        return Result.ok(
            phase,
            f"secure store opens for {sid(ctx)} — key phrase matches (phrase not revealed)",
            data={"store": store},
        )


# --------------------------------------------------------------------------- #
# 2. SLD data supplier (register-sld)
# --------------------------------------------------------------------------- #
class RegisterSldAction(_PostCopyAction):
    """Re-point/re-register the SLD data supplier at the target SLD host/port."""

    name = "pipo.register-sld"
    description = "Re-point SLD data supplier to the target SLD host/port (sldreg)."
    requires_checks = ["pipo.sld-reachable", "pipo.as-java-up"]
    rollback_hint = (
        "restore the previous SLD data-supplier configuration (host/port) from "
        "the saved config and re-run sldreg (SAP Note 2230669)"
    )

    def _tool(self, ctx: Context) -> str:
        return str(ctx.get("sldreg_tool") or "sldreg")

    def _endpoint(self, ctx: Context) -> tuple[str | None, str]:
        host = ctx.get("sld_host")
        port = str(ctx.get("sld_port", "50000"))
        return (str(host) if host else None, port)

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        host, port = self._endpoint(ctx)
        if not host:
            return Result.fail(
                phase,
                "no sld_host configured — set params.sld_host (and sld_port) to the target SLD",
            )
        tool = self._tool(ctx)
        user = str(ctx.get("sld_user", "SLDDSUSER"))
        argv_preview = [tool, "-configure", "-hostname", host, "-port", port, "-user", user]
        return Result.ok(
            phase,
            f"[{sid(ctx)}] would re-point SLD data supplier to {host}:{port} (user {user})",
            detail="  1. " + " ".join(argv_preview) + "  (password, if any, piped via stdin)",
            data={"tool": tool, "sld_host": host, "sld_port": port, "command": argv_preview},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        host, port = self._endpoint(ctx)
        if not host:
            return Result.fail(
                phase, "no sld_host configured — cannot register the SLD data supplier"
            )
        tool = self._tool(ctx)
        user = str(ctx.get("sld_user", "SLDDSUSER"))
        # SLD password (if provided) goes over stdin only — never on argv.
        password = _read_secret(ctx, "sld_password_file", "sld_password")
        cr = ctx.runner().run(
            [tool, "-configure", "-hostname", host, "-port", port, "-user", user],
            input_text=password,
        )
        if not cr.ok:
            return Result.fail(
                phase,
                f"SLD data supplier registration failed against {host}:{port}",
                detail=redact(cr.stderr or cr.stdout),
                data={"sld_host": host, "sld_port": port, "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            f"SLD data supplier re-pointed to {host}:{port} for {sid(ctx)}",
            data={"sld_host": host, "sld_port": port},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        host, port = self._endpoint(ctx)
        tool = self._tool(ctx)
        cr = ctx.runner().run([tool, "-status"])
        if not cr.ok:
            return Result.fail(
                phase,
                "could not read SLD data-supplier status after re-pointing",
                detail=redact(cr.stderr or cr.stdout),
                data={"sld_host": host, "sld_port": port},
            )
        # Confirm the configured target host appears in the reported status.
        if host and host not in cr.stdout:
            return Result.fail(
                phase,
                f"SLD status does not reflect the target host {host} — registration not applied",
                detail=redact(cr.stdout),
                data={"sld_host": host, "sld_port": port},
            )
        return Result.ok(
            phase,
            f"SLD data supplier now points at {host}:{port}",
            data={"sld_host": host, "sld_port": port},
        )


# --------------------------------------------------------------------------- #
# 3. RFC / JCo destinations (fix-rfc-jco)
# --------------------------------------------------------------------------- #
class FixRfcJcoAction(_PostCopyAction):
    """Re-point JCo/RFC destinations from the source hosts to the target hosts."""

    name = "pipo.fix-rfc-jco"
    description = "Re-point JCo/RFC destinations to the target ABAP/back-end hosts."
    requires_checks = ["pipo.rfc-jco-config", "pipo.as-java-up"]
    rollback_hint = (
        "restore the pre-change JCo destination configuration export and re-apply "
        "it, then re-test each destination (SAP Note 1043195)"
    )

    def _tool(self, ctx: Context) -> str:
        return str(ctx.get("jco_admin_tool") or "jcoadmin")

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        target_host = ctx.get("target_ashost")
        if not target_host:
            return Result.fail(
                phase,
                "no target_ashost configured — set params.target_ashost to the target ABAP app server",
            )
        tool = self._tool(ctx)
        config = str(ctx.get("jco_config_path", "<jco-config>"))
        argv_preview = [tool, "-repoint", "-target-host", str(target_host), "-config", config]
        return Result.ok(
            phase,
            f"[{sid(ctx)}] would re-point JCo/RFC destinations to target host {target_host}",
            detail="  1. " + " ".join(argv_preview),
            data={"tool": tool, "target_ashost": str(target_host), "command": argv_preview},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        target_host = ctx.get("target_ashost")
        if not target_host:
            return Result.fail(
                phase, "no target_ashost configured — cannot re-point JCo/RFC destinations"
            )
        tool = self._tool(ctx)
        config = str(ctx.get("jco_config_path", ""))
        argv = [tool, "-repoint", "-target-host", str(target_host)]
        if config:
            argv += ["-config", config]
        cr = ctx.runner().run(argv)
        if not cr.ok:
            return Result.fail(
                phase,
                f"JCo/RFC destination re-pointing failed for target host {target_host}",
                detail=redact(cr.stderr or cr.stdout),
                data={"target_ashost": str(target_host), "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            f"JCo/RFC destinations re-pointed to {target_host} for {sid(ctx)}",
            data={"target_ashost": str(target_host)},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        target_host = str(ctx.get("target_ashost", ""))
        source_host = str(ctx.get("source_ashost", ""))
        tool = self._tool(ctx)
        cr = ctx.runner().run([tool, "-list"])
        if not cr.ok:
            return Result.fail(
                phase,
                "could not list JCo/RFC destinations after re-pointing",
                detail=redact(cr.stderr or cr.stdout),
            )
        # A lingering reference to the source host means the re-point missed one.
        if source_host and source_host in cr.stdout:
            return Result.fail(
                phase,
                f"JCo/RFC destination still references the source host {source_host}",
                detail=redact(cr.stdout),
                data={"target_ashost": target_host, "source_ashost": source_host},
            )
        if target_host and target_host not in cr.stdout:
            return Result.warn(
                phase,
                f"no JCo/RFC destination references the target host {target_host} yet — review manually",
                data={"target_ashost": target_host},
            )
        return Result.ok(
            phase,
            f"JCo/RFC destinations reference target host {target_host}",
            data={"target_ashost": target_host},
        )


# --------------------------------------------------------------------------- #
# 4. UME datasource (reconfigure-ume)
# --------------------------------------------------------------------------- #
class ReconfigureUmeAction(_PostCopyAction):
    """Re-point the UME (User Management Engine) datasource to the target schema."""

    name = "pipo.reconfigure-ume"
    description = "Re-point the UME datasource/connection to the target HANA schema."
    requires_checks = ["pipo.as-java-up", "pipo.hana-java-schema"]
    rollback_hint = (
        "restore the previous ume.persistence.data_source_configuration value "
        "with the offline config tool and restart AS Java (SAP Note 718383)"
    )

    _UME_PROP = "ume.persistence.data_source_configuration"

    def _tool(self, ctx: Context) -> str:
        return str(ctx.get("configtool") or f"{instance_dir(ctx)}/j2ee/configtool/consoleconfig.sh")

    def _datasource(self, ctx: Context) -> str:
        return str(ctx.get("ume_datasource", "dataSourceConfiguration_database_only.xml"))

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        tool = self._tool(ctx)
        schema = java_schema(ctx)
        datasource = self._datasource(ctx)
        argv_preview = [tool, "-set", self._UME_PROP, "-value", datasource, "-schema", schema]
        return Result.ok(
            phase,
            f"[{sid(ctx)}] would set UME datasource to '{datasource}' on schema {schema}",
            detail="  1. " + " ".join(argv_preview) + "  (DB password, if any, piped via stdin)",
            data={
                "tool": tool,
                "schema": schema,
                "datasource": datasource,
                "command": argv_preview,
            },
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        tool = self._tool(ctx)
        schema = java_schema(ctx)
        datasource = self._datasource(ctx)
        # DB password (if the datasource needs one) goes over stdin only.
        password = _read_secret(ctx, "db_password_file", "db_password")
        cr = ctx.runner().run(
            [tool, "-set", self._UME_PROP, "-value", datasource, "-schema", schema],
            input_text=password,
        )
        if not cr.ok:
            return Result.fail(
                phase,
                "UME datasource reconfiguration failed — check the target schema and datasource",
                detail=redact(cr.stderr or cr.stdout),
                data={"schema": schema, "datasource": datasource, "exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            f"UME datasource re-pointed to schema {schema} for {sid(ctx)}",
            data={"schema": schema, "datasource": datasource},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        tool = self._tool(ctx)
        schema = java_schema(ctx)
        cr = ctx.runner().run([tool, "-get", self._UME_PROP])
        if not cr.ok:
            return Result.fail(
                phase,
                "could not read back the UME datasource configuration",
                detail=redact(cr.stderr or cr.stdout),
                data={"schema": schema},
            )
        datasource = self._datasource(ctx)
        if datasource not in cr.stdout:
            return Result.fail(
                phase,
                f"UME datasource does not reflect '{datasource}' after reconfiguration",
                detail=redact(cr.stdout),
                data={"schema": schema, "datasource": datasource},
            )
        return Result.ok(
            phase,
            f"UME datasource confirmed as '{datasource}' on schema {schema}",
            data={"schema": schema, "datasource": datasource},
        )


# --------------------------------------------------------------------------- #
# Orchestrator: run all four in the mandatory order (fail-safe)
# --------------------------------------------------------------------------- #
class PostCopyAllAction(_PostCopyAction):
    """Run the four Java post-copy actions in order, stopping on the first failure.

    Order is mandatory: SECSTORE -> SLD -> RFC/JCo -> UME. The secure store must
    open before anything that reads DB credentials from it; the SLD/JCo/UME steps
    follow. Each sub-action runs its own ``execute`` then ``verify``; a blocking
    result aborts the sequence before the next step (fail-safe).
    """

    name = "pipo.postcopy-all"
    description = "Run all Java post-copy steps in order (SECSTORE -> SLD -> RFC/JCo -> UME)."
    rollback_hint = "roll back each completed sub-action individually per its runbook"

    #: the ordered sub-actions
    _STEPS: tuple[type[_PostCopyAction], ...] = (
        RebuildSecStoreAction,
        RegisterSldAction,
        FixRfcJcoAction,
        ReconfigureUmeAction,
    )

    # Union of every sub-action's prerequisites (de-duplicated, order preserved).
    requires_checks = list(
        dict.fromkeys(
            check
            for step in (
                RebuildSecStoreAction,
                RegisterSldAction,
                FixRfcJcoAction,
                ReconfigureUmeAction,
            )
            for check in step.requires_checks
        )
    )

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        lines: list[str] = []
        for i, step_cls in enumerate(self._STEPS, start=1):
            step = step_cls()
            dr = step.dry_run(ctx)
            lines.append(f"  {i}. {step.name}: {dr.summary}")
        return Result.ok(
            phase,
            f"[{sid(ctx)}] would run {len(self._STEPS)} post-copy step(s) in order; nothing executed",
            detail="\n".join(lines),
            data={"steps": [s.name for s in self._STEPS]},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        completed: list[str] = []
        for step_cls in self._STEPS:
            step = step_cls()
            ex = step.execute(ctx)
            if ex.status.is_blocking:
                return Result.fail(
                    phase,
                    f"post-copy aborted at {step.name}: {ex.summary}",
                    detail=redact(ex.detail),
                    data={"completed": completed, "failed_at": step.name},
                )
            # Verify each step before moving to the next (fail-safe).
            vr = step.verify(ctx)
            if vr.status.is_blocking:
                return Result.fail(
                    phase,
                    f"post-copy verify failed at {step.name}: {vr.summary}",
                    detail=redact(vr.detail),
                    data={"completed": completed, "failed_at": step.name},
                )
            completed.append(step.name)
        return Result.ok(
            phase,
            f"all {len(completed)} post-copy step(s) executed and verified for {sid(ctx)}",
            data={"completed": completed},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        # Final gate: the AS Java stack must be up after the full post-copy.
        cr = ctx.runner().run(sapcontrol_argv(ctx, "GetProcessList"))
        if cr.exit_code not in (0, 3, 4):
            return Result.fail(
                phase,
                "AS Java did not respond after post-copy — check sapstartsrv and the work traces",
                detail=redact(cr.stderr or cr.stdout),
            )
        up = cr.exit_code in (0, 3) or "GREEN" in cr.stdout.upper()
        if not up:
            return Result.warn(
                phase,
                "AS Java responded but not all processes are GREEN yet — review GetProcessList",
                data={"exit_code": cr.exit_code},
            )
        return Result.ok(
            phase,
            f"post-copy complete — AS Java responding for {sid(ctx)}",
            data={"exit_code": cr.exit_code},
        )
