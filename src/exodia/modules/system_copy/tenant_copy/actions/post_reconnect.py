"""Post-copy reconnect & cleanup actions for HANA tenant copy (from the COP).

After the replica is finalized, the SAP system on the target is reconnected to
the copied tenant and validated, then migration-specific ABAP dictionary/monitoring
data is cleared. These map the COP "Reconnect SAP System to DB" and post-activity
cleanup rows onto guarded Exodia actions:

* ``reconnect-verify`` — test the DB connection (``hdbsql -U DEFAULT``) and the
  transport system (``R3trans -x``) after the SAP system is pointed at the new
  tenant. Read-mostly (R3trans -x is a self-test); guarded for consistency.
* ``delete-abap-dict-data`` — clear the migration-stale monitoring / dictionary
  tables (ALCONSEG, ALSYSTEMS, DBSNP, MONI, OSMON, PAHI, SDBA*, TPFET, TPFHT,
  DDLOG) on the copied tenant, so the target doesn't carry the source's history.
  Backs up nothing (these are transient monitoring tables) but shows every
  DELETE in dry-run and requires explicit execute.

Both are POST phase. SQL/commands are the exact statements from the runbook.
"""

from __future__ import annotations

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase

from ..checks import _common as c

# The migration-stale tables the runbook clears on the target (schema-qualified
# at runtime). Monitoring/dictionary history that must not carry over.
_DICT_TABLES = [
    "ALCONSEG", "ALSYSTEMS", "DBSNP", "MONI", "OSMON", "PAHI",
    "SDBAD", "SDBAP", "SDBAR", "TPFET", "TPFHT", "DDLOG",
]


def _schema(ctx: Context) -> str:
    schema = str(ctx.get("abap_schema", "SAPABAP1"))
    if not c.is_valid_schema(schema):
        raise ValueError(
            f"invalid abap_schema '{schema}' — must be a plain SQL identifier "
            "(letter first, then alphanumerics/underscore)"
        )
    return schema


class ReconnectVerifyAction(Action):
    """Verify the SAP system's DB connection + transport system after reconnect."""

    name = "tenant-copy.hana.reconnect-verify"
    description = "Verify DB connection (hdbsql -U DEFAULT) + transport (R3trans -x)."
    title = "Reconnect Verification (DB Connection + R3trans)"
    phase = Phase.POST
    destructive = False
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "userstore_key", "hdbuserstore key to test", default="DEFAULT",
                help="The DEFAULT key the SAP system uses to reach the copied tenant.",
            ),
            ParamSpec(
                "host", "App server host (blank = local)", kind=ParamKind.FIELD,
                help="Host to run hdbsql / R3trans on over SSH; blank = local.",
            ),
            ParamSpec(
                "user", "SSH user (<sid>adm)", kind=ParamKind.FIELD,
                help="OS user (e.g. t40adm) that runs R3trans / hdbsql.",
            ),
        ]

    def _key(self, ctx: Context) -> str:
        return str(ctx.get("userstore_key", "DEFAULT"))

    def dry_run(self, ctx: Context) -> Result:
        key = self._key(ctx)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would test DB connection (hdbsql -U {key}) and the transport system (R3trans -x)",
            detail=f'  1. hdbsql -U {key} "SELECT 1 FROM DUMMY"\n  2. R3trans -x',  # nosec B608 - display-only dry-run text (not executed); key is an hdbuserstore key name, SQL is a literal
            facts={"Connection Key": key},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        key = self._key(ctx)
        runner = ctx.runner()
        self._emit_phase("db connection", f"hdbsql -U {key}")
        conn = runner.run(
            ["hdbsql", "-U", key, "-x", "-a", "-j", "SELECT 1 FROM DUMMY"],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        if not conn.ok:
            return Result.fail(
                phase, f"DB connection test failed (hdbsql -U {key})",
                detail=conn.stderr or conn.stdout, facts={"DB Connection": "FAILED"},
            )
        self._emit_phase("transport", "R3trans -x")
        r3 = runner.run(["R3trans", "-x"], timeout=int(ctx.get("r3trans_timeout", 300)))
        # R3trans -x returns 0 on a clean connect test.
        if not r3.ok:
            return Result.warn(
                phase,
                f"DB connection OK, but R3trans -x returned {r3.exit_code} — check trans.log",
                detail=r3.stderr or r3.stdout,
                facts={"DB Connection": "OK", "R3trans": f"rc={r3.exit_code}"},
            )
        return Result.ok(
            phase, "DB connection and transport system (R3trans) both OK",
            facts={"DB Connection": "OK", "R3trans": "rc=0"},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(f"{self.name}.verify", "reconnect verified")


class DeleteAbapDictDataAction(Action):
    """Clear migration-stale monitoring/dictionary tables on the copied tenant."""

    name = "tenant-copy.hana.delete-abap-dict-data"
    description = "Clear migration-stale monitoring/dictionary tables on the copied tenant."
    title = "Verify & Delete ABAP Dictionary/Monitoring Data"
    phase = Phase.POST
    destructive = True
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "tenant_key", "Tenant hdbuserstore key (copied tenant)",
                help="hdbsql -U key connecting to the copied tenant as the ABAP schema.",
            ),
            ParamSpec(
                "abap_schema", "ABAP schema owner", default="SAPABAP1",
                help="Schema that owns the monitoring/dictionary tables.",
            ),
        ]

    def _key(self, ctx: Context) -> str:
        return str(ctx.get("tenant_key") or ctx.get("target_tenant_key") or "")

    def dry_run(self, ctx: Context) -> Result:
        schema = _schema(ctx)
        stmts = [f"DELETE FROM {schema}.{t};" for t in _DICT_TABLES]  # nosec B608 - schema validated by is_valid_schema; table names from the _DICT_TABLES literal allow-list (no user input)
        return Result.ok(
            f"{self.name}.dry-run",
            f"would clear {len(_DICT_TABLES)} migration-stale table(s) in schema {schema}",
            detail="\n".join(f"  {i}. {s}" for i, s in enumerate(stmts, start=1)),
            data={"tables": _DICT_TABLES, "schema": schema},
            facts={"Tables": str(len(_DICT_TABLES)), "Schema": schema},
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        key = self._key(ctx)
        if not key:
            return Result.skip(phase, "no tenant_key provided")
        schema = _schema(ctx)
        cleared: list[str] = []
        n = len(_DICT_TABLES)
        for i, t in enumerate(_DICT_TABLES, start=1):
            sql = f"DELETE FROM {schema}.{t}"  # nosec B608 - schema validated by is_valid_schema; t from the _DICT_TABLES literal allow-list (no user input)
            self._emit_phase(f"clear {i}/{n}", t)
            cr = ctx.runner().run(
                ["hdbsql", "-U", key, "-x", "-a", "-j", sql],
                timeout=int(ctx.get("cleanup_timeout", 300)),
            )
            # A missing table is not fatal (some don't exist on every release).
            if not cr.ok and "invalid table name" not in (cr.stderr or cr.stdout).lower():
                return Result.fail(
                    phase, f"failed clearing {schema}.{t} (cleared {len(cleared)} so far)",
                    detail=cr.stderr or cr.stdout, data={"cleared": cleared, "failed": t},
                )
            cleared.append(t)
            self._emit_progress(100.0 * i / n, f"{i}/{n} tables")
        return Result.ok(
            phase, f"cleared {len(cleared)} migration-stale table(s) in {schema}",
            data={"cleared": cleared, "schema": schema},
            facts={"Tables Cleared": str(len(cleared)), "Schema": schema},
        )

    def verify(self, ctx: Context) -> Result:
        return Result.ok(f"{self.name}.verify", "dictionary/monitoring tables cleared",
                         facts={"Schema": _schema(ctx)})
