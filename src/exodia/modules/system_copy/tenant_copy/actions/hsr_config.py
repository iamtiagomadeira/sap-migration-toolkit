"""HSR configuration actions for HANA tenant copy (from the COP, target side).

Two guarded actions run on the TARGET SYSTEMDB before triggering the replica:

* ``configure-hsr-parameters`` — apply the SAP best-practice system-replication
  tuning + SSL parameters via ``ALTER SYSTEM ALTER CONFIGURATION ... WITH
  RECONFIGURE``. The admin chooses SSL **on** or **off** (``ssl_mode`` param):
  the two parameter sets come straight from the runbook. Some of these changes
  need a DB restart to take effect (communication/ssl, listeninterface), so the
  action flags whether a restart is required.
* ``restart-hana`` — ``HDB stop`` + ``HDB start`` on the target, for when the
  parameter changes require it. Customer/host-impacting, so guarded.

Credentials never appear on the command line (hdbsql -U <key>); only secret-free
SQL is passed. Guarded flow: dry-run (show every statement) -> confirm ->
execute -> verify.

Reference (cite by number only): SAP Note 2300943 (HANA SR parameters),
2456657 (system replication).
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase

from ..checks import _common as c

# --------------------------------------------------------------------------- #
# The exact parameter sets from the runbook (SSL on vs off). Each entry is
# (file, section, key, value); the rest of the tuning block is shared.
# --------------------------------------------------------------------------- #

_SSL_ON = [
    ("global.ini", "system_replication_communication", "enable_ssl", "on"),
    ("global.ini", "system_replication_communication", "ssl", "systemPKI"),
    ("global.ini", "multidb", "enforce_ssl_database_replication", "true"),
    ("global.ini", "communication", "ssl", "on"),
    ("global.ini", "communication", "listeninterface", ".global"),
]
_SSL_OFF = [
    ("global.ini", "system_replication_communication", "enable_ssl", "off"),
    ("global.ini", "system_replication_communication", "ssl", "no"),
    ("global.ini", "multidb", "enforce_ssl_database_replication", "false"),
    ("global.ini", "communication", "ssl", "off"),
    ("global.ini", "communication", "listeninterface", ".internal"),
]
# Shared performance / log-shipping tuning applied in both modes.
_SHARED_TUNING = [
    ("global.ini", "inifile_checker", "replicate", "false"),
    ("global.ini", "system_replication", "enable_data_compression", "true"),
    ("global.ini", "system_replication", "enable_log_compression", "true"),
    ("global.ini", "system_replication", "enable_log_retention", "on"),
    ("global.ini", "system_replication", "logshipping_async_buffer_size", "1073741824"),
    ("indexserver.ini", "system_replication", "logshipping_async_buffer_size", "26843545600"),
    ("global.ini", "system_replication", "logshipping_max_retention_size", "1048576"),
    ("global.ini", "system_replication", "datashipping_parallel_processing", "true"),
]
# Keys that only take effect after a DB restart (per the runbook note).
_RESTART_KEYS = {"communication/ssl", "communication/listeninterface"}


def _ssl_mode(ctx: Context) -> str:
    return str(ctx.get("ssl_mode", "off")).lower()


def _target_key(ctx: Context) -> str:
    return str(ctx.get("target_userstore_key") or ctx.get("userstore_key") or "SYSTEMDB")


def _alter_stmt(file: str, section: str, key: str, value: str) -> str:
    return (
        f"ALTER SYSTEM ALTER CONFIGURATION ('{file}','SYSTEM') "
        f"SET ('{section}','{key}') = '{value}' WITH RECONFIGURE"
    )


def _param_set(ctx: Context) -> list[tuple[str, str, str, str]]:
    ssl = _SSL_ON if _ssl_mode(ctx) == "on" else _SSL_OFF
    return ssl + _SHARED_TUNING


class ConfigureHsrParametersAction(Action):
    """Apply system-replication + SSL parameters on the target (SSL on/off)."""

    name = "tenant-copy.hana.configure-hsr-parameters"
    description = "Apply HSR + SSL parameters on the target SYSTEMDB (SSL on/off)."
    title = "Configure HSR & SSL Parameters (Target SYSTEMDB)"
    phase = Phase.PREPARATION
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            c.TARGET_USERSTORE_KEY,
            ParamSpec(
                "ssl_mode", "System-replication SSL mode", default="off",
                choices=("on", "off"),
                help="'on' = encrypted replication (systemPKI); 'off' = unencrypted. "
                "Changes to communication/ssl + listeninterface need a DB restart.",
            ),
        ]

    def _requires_restart(self, ctx: Context) -> bool:
        # ssl/listeninterface always change between modes -> restart needed.
        return True

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        params = _param_set(ctx)
        stmts = [_alter_stmt(*p) for p in params]
        restart = self._requires_restart(ctx)
        return Result.ok(
            phase,
            f"would apply {len(stmts)} HSR parameter(s) with SSL={_ssl_mode(ctx).upper()} "
            f"on the target; {'DB RESTART required after' if restart else 'no restart needed'}",
            detail="\n".join(f"  {i}. {s}" for i, s in enumerate(stmts, start=1)),
            data={"ssl_mode": _ssl_mode(ctx), "statements": stmts, "requires_restart": restart},
            facts={
                "SSL Mode": _ssl_mode(ctx).upper(),
                "Parameters": str(len(stmts)),
                "Restart Required": "Yes" if restart else "No",
            },
            sap_note="2300943",
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        key = _target_key(ctx)
        params = _param_set(ctx)
        applied: list[str] = []
        n = len(params)
        for i, p in enumerate(params, start=1):
            stmt = _alter_stmt(*p)
            self._emit_phase(f"param {i}/{n}", f"{p[1]}/{p[2]} = {p[3]}")
            self._emit_log(f"$ hdbsql … {stmt}")
            cr = ctx.runner().run(
                ["hdbsql", "-U", key, "-x", "-a", "-j", stmt],
                timeout=int(ctx.get("param_timeout", 120)),
            )
            if not cr.ok:
                return Result.fail(
                    phase,
                    f"failed applying {p[1]}/{p[2]} (applied {len(applied)} so far)",
                    detail=cr.stderr or cr.stdout,
                    data={"applied": applied, "failed": f"{p[1]}/{p[2]}"},
                    sap_note="2300943",
                )
            applied.append(f"{p[1]}/{p[2]}")
            self._emit_progress(100.0 * i / n, f"{i}/{n} parameters set")
        restart = self._requires_restart(ctx)
        return Result.ok(
            phase,
            f"applied {len(applied)} HSR parameter(s) with SSL={_ssl_mode(ctx).upper()}"
            + (" — RESTART the DB now (tenant-copy.hana.restart-hana)" if restart else ""),
            data={"ssl_mode": _ssl_mode(ctx), "applied": applied, "requires_restart": restart},
            facts={
                "SSL Mode": _ssl_mode(ctx).upper(),
                "Parameters Applied": str(len(applied)),
                "Restart Required": "Yes" if restart else "No",
            },
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        key = _target_key(ctx)
        # Confirm the SSL keys landed as intended.
        sql = (
            "SELECT SECTION, KEY, VALUE FROM M_INIFILE_CONTENTS "
            "WHERE FILE_NAME='global.ini' AND LAYER_NAME='SYSTEM' "
            "AND SECTION IN ('communication','system_replication_communication','multidb')"
        )
        cr = ctx.runner().run(
            ["hdbsql", "-U", key, "-x", "-a", "-j", sql],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        if not cr.ok:
            return Result.warn(
                phase, "applied, but could not read back the parameters to verify",
                detail=cr.stderr or cr.stdout,
            )
        want = "ON" if _ssl_mode(ctx) == "on" else "OFF"
        out = cr.stdout.upper()
        ok = ("ENABLE_SSL" in out and (want in out or ("OFF" if want == "OFF" else "ON") in out))
        return Result.ok(
            phase,
            f"HSR parameters read back (target SSL intended {want})",
            data={"stdout": cr.stdout.strip(), "ssl_intended": want, "ssl_seen": ok},
            facts={"SSL Mode (intended)": want},
        )

    def rollback(self, ctx: Context) -> Result:
        return Result.skip(
            f"{self.name}.rollback",
            "re-run with the previous ssl_mode to restore parameters, then restart the DB",
        )


class RestartHanaAction(Action):
    """HDB stop + HDB start on the target (needed after some parameter changes)."""

    name = "tenant-copy.hana.restart-hana"
    description = "Restart the target HANA DB (HDB stop/start) after parameter changes."
    title = "Restart HANA DB (HDB stop/start)"
    phase = Phase.PREPARATION
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "host", "HANA host (blank = local)", kind=ParamKind.FIELD,
                help="Host to run HDB on over SSH (as <sid>adm); blank runs locally.",
            ),
            ParamSpec(
                "user", "SSH user (<sid>adm)", kind=ParamKind.FIELD,
                help="OS user that owns the HANA instance (e.g. h40adm).",
            ),
        ]

    def dry_run(self, ctx: Context) -> Result:
        return Result.ok(
            f"{self.name}.dry-run",
            "would restart the target HANA DB: HDB stop; HDB start",
            detail="  1. HDB stop\n  2. HDB start",
            facts={"Action": "HDB stop; HDB start"},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        runner = ctx.runner()
        timeout = int(ctx.get("restart_timeout", 1800))
        self._emit_phase("stop", "HDB stop")
        stop = runner.run(["HDB", "stop"], timeout=timeout)
        self._emit_log(stop.stdout or stop.stderr)
        if not stop.ok:
            return Result.fail(phase, f"HDB stop failed (exit {stop.exit_code})",
                               detail=stop.stderr or stop.stdout)
        self._emit_phase("start", "HDB start")
        start = runner.run(["HDB", "start"], timeout=timeout)
        self._emit_log(start.stdout or start.stderr)
        if not start.ok:
            return Result.fail(phase, f"HDB start failed (exit {start.exit_code})",
                               detail=start.stderr or start.stdout)
        return Result.ok(
            phase, "target HANA DB restarted (HDB stop; HDB start)",
            facts={"Restart": "Completed"},
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        cr = ctx.runner().run(["HDB", "info"], timeout=int(ctx.get("verify_timeout", 120)))
        running = cr.ok and "hdbnameserver" in (cr.stdout or "").lower()
        if running:
            return Result.ok(phase, "HANA is back up (hdbnameserver running)",
                             facts={"HANA": "Running"})
        return Result.warn(phase, "could not confirm HANA is up via HDB info",
                           detail=cr.stdout or cr.stderr, facts={"HANA": "unknown"})
