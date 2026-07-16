"""HANA MiniChecks wrapper (TIA-58 bonus).

Runs the HANA_Configuration_MiniChecks SQL (see SAP Note 1969700) via hdbsql
and parses the rows flagged potentially-critical (C = 'X') into a WARN/FAIL.

We do NOT ship the SAP-authored SQL text (SAP Note attachment, copyrighted). The
statement to execute is provided by the operator via the `minichecks_sql` param
(path to the SQL) or `minichecks_stmt` (inline). Without it, the check SKIPs.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class MiniChecksCheck(Check):
    """Wrap SAP's HANA_Configuration_MiniChecks and surface critical findings."""

    name = "backup-restore.hana.minichecks"
    description = "Run HANA_Configuration_MiniChecks and flag critical rows (SAP Note 1969700)."
    blocking = False

    def run(self, ctx: Context) -> Result:
        stmt = ctx.get("minichecks_stmt")
        sql_path = ctx.get("minichecks_sql")
        if not stmt and sql_path:
            cat = c.run(ctx, ["cat", str(sql_path)])
            if cat.ok and cat.stdout.strip():
                stmt = cat.stdout
        if not stmt:
            return Result.skip(
                self.name,
                "MiniChecks SQL not provided; pass minichecks_sql (path) or minichecks_stmt "
                "(SAP Note 1969700 attachment — not shipped for IP reasons)",
            )
        cr = c.run(ctx, c.hdbsql_argv(ctx, str(stmt)))
        if not cr.ok:
            return Result.fail(
                self.name,
                "could not run HANA_Configuration_MiniChecks",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        critical = self._critical_rows(rows)
        if critical:
            preview = "; ".join(self._describe(r) for r in critical[:5])
            return Result.warn(
                self.name,
                f"{len(critical)} potentially-critical minicheck(s): {preview}",
                data={"critical_count": len(critical), "critical": critical},
            )
        return Result.ok(
            self.name,
            f"no potentially-critical minichecks ({len(rows)} rows evaluated)",
            data={"critical_count": 0, "rows": len(rows)},
        )

    @staticmethod
    def _critical_rows(rows: list[list[str]]) -> list[list[str]]:
        """Rows whose last column (the C flag) is 'X' are potentially critical."""
        out: list[list[str]] = []
        for r in rows:
            if r and r[-1].strip().upper() == "X":
                out.append(r)
        return out

    @staticmethod
    def _describe(row: list[str]) -> str:
        # MiniChecks layout: CHID, DESCRIPTION, HOST, VALUE, EXPECTED, C
        chid = row[0] if len(row) > 0 else "?"
        desc = row[1] if len(row) > 1 else ""
        return f"{chid} {desc}".strip()
