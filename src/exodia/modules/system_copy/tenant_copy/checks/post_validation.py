"""Post-copy validation checks for HANA tenant copy (from the COP post-activities).

Read-only checks run on the target after the reconnect, mapping ECS post-
processing rows onto Exodia checks:

* ``secure-communication`` — the profile parameter ``system/secure_communication``
  must be ON (an ECS-mandatory post check). Read via M_INIFILE_CONTENTS.
* ``data-consistency`` — compare the top tables by record count on the source
  and the copied target (the runbook's ``SELECT TOP 30 ... FROM M_TABLES ORDER
  BY RECORD_COUNT``), so the engineer confirms the biggest tables carried over
  with matching row counts.

Read-only (SELECT only). Both POST phase.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from . import _common as c


class SecureCommunicationCheck(Check):
    """system/secure_communication must be ON (ECS-mandatory post check)."""

    name = "tenant-copy.hana.secure-communication"
    description = "Profile parameter system/secure_communication is ON (ECS mandatory)."
    title = "system/secure_communication = ON (ECS Mandatory)"
    phase = Phase.POST
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [c.TARGET_USERSTORE_KEY]

    def run(self, ctx: Context) -> Result:
        sql = (
            "SELECT VALUE FROM SYS_DATABASES.M_INIFILE_CONTENTS "
            "WHERE SECTION='system' AND KEY='secure_communication'"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, c.TARGET, sql))
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not read system/secure_communication on the target",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        value = rows[0][0].upper() if rows and rows[0] else ""
        if value == "ON":
            return Result.ok(
                self.name, "system/secure_communication is ON",
                data={"value": value}, facts={"secure_communication": "ON"},
            )
        return Result.fail(
            self.name,
            f"system/secure_communication is {value or 'unset'} — ECS requires ON "
            "(set it and restart the service)",
            data={"value": value}, facts={"secure_communication": value or "unset"},
        )


class DataConsistencyCheck(Check):
    """Compare top tables by record count, source vs copied target (M_TABLES)."""

    name = "tenant-copy.hana.data-consistency"
    description = "Top tables by record count match between source and target (M_TABLES)."
    title = "Post-Copy Data Consistency (Top Tables by Record Count)"
    phase = Phase.POST
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "source_tenant_key", "Source tenant hdbuserstore key",
                help="hdbsql -U key connecting to the SOURCE tenant as the ABAP schema.",
            ),
            ParamSpec(
                "target_tenant_key", "Target tenant hdbuserstore key",
                help="hdbsql -U key connecting to the copied TARGET tenant.",
            ),
            ParamSpec(
                "abap_schema", "ABAP schema owner", default="SAPABAP1",
                help="Schema whose tables are compared (e.g. SAPABAP1).",
            ),
            ParamSpec(
                "top_n", "How many top tables to compare", default="30",
                help="Compare the N largest tables by record count.",
            ),
        ]

    def _top_tables(self, ctx: Context, key: str) -> dict[str, int] | None:
        schema = str(ctx.get("abap_schema", "SAPABAP1"))
        if not c.is_valid_schema(schema):
            return None
        top_n = int(ctx.get("top_n", 30))
        sql = (
            f"SELECT TOP {top_n} TABLE_NAME, RECORD_COUNT FROM M_TABLES "  # nosec B608 - top_n coerced to int; schema validated by is_valid_schema (no quote/space/semicolon possible)
            f"WHERE SCHEMA_NAME = '{schema}' ORDER BY RECORD_COUNT DESC"
        )
        cr = ctx.runner().run(
            ["hdbsql", "-U", key, "-x", "-a", "-j", sql],
            timeout=int(ctx.get("verify_timeout", 120)),
        )
        if not cr.ok:
            return None
        out: dict[str, int] = {}
        for row in c.parse_hdbsql_rows(cr.stdout):
            if len(row) >= 2:
                try:
                    out[row[0]] = int(row[1])
                except ValueError:
                    continue
        return out

    def run(self, ctx: Context) -> Result:
        src_key = ctx.get("source_tenant_key")
        tgt_key = ctx.get("target_tenant_key")
        if not src_key or not tgt_key:
            return Result.skip(
                self.name,
                "need source_tenant_key + target_tenant_key to compare table counts",
            )
        src = self._top_tables(ctx, str(src_key))
        tgt = self._top_tables(ctx, str(tgt_key))
        if src is None or tgt is None:
            side = "source" if src is None else "target"
            return Result.warn(self.name, f"could not read {side} top tables to compare")
        tol = float(ctx.get("record_tolerance", 0.01))
        differing = []
        for tbl, src_count in src.items():
            tgt_count = tgt.get(tbl)
            if tgt_count is None:
                differing.append(f"{tbl} (missing on target)")
                continue
            drift = abs(src_count - tgt_count) / src_count if src_count else 0.0
            if drift > tol:
                differing.append(f"{tbl} (src={src_count}, tgt={tgt_count})")
        data = {"source_top": src, "target_top": tgt, "differing": differing}
        if differing:
            return Result.fail(
                self.name,
                f"{len(differing)} of {len(src)} top table(s) differ beyond tolerance: "
                f"{', '.join(differing[:5])}{'…' if len(differing) > 5 else ''}",
                data=data,
                facts={"Tables Compared": str(len(src)), "Differing": str(len(differing))},
                sap_note="2101244",
            )
        return Result.ok(
            self.name,
            f"all {len(src)} top tables match within tolerance between source and target",
            data=data,
            facts={"Tables Compared": str(len(src)), "Differing": "0"},
        )
