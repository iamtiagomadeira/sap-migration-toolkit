"""pipo.hana-java-schema — verify the AS Java HANA schema exists and is accessible.

AS Java persists to a HANA schema named SAP<SID>DB. After a backup/restore copy
the schema must exist in the target tenant and be reachable with the SAP<SID>DB
technical user. This read-only check queries HANA's SCHEMAS system view via
hdbsql and confirms the schema is present.

No credentials are echoed. The password (if provided) is passed to hdbsql via a
param and scrubbed from any captured output.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from ._common import java_schema, redact


class HanaJavaSchemaCheck(Check):
    """HANA schema SAP<SID>DB for the Java stack exists and is queryable."""

    name = "pipo.hana-java-schema"
    description = "HANA Java schema SAP<SID>DB present and accessible via hdbsql."
    blocking = True

    def run(self, ctx: Context) -> Result:
        schema = java_schema(ctx)
        hana_host = ctx.get("hana_host", "localhost")
        hana_port = str(ctx.get("hana_sql_port", "30015"))
        hana_user = ctx.get("hana_user", "SYSTEM")
        hana_password = ctx.get("hana_password", "")

        runner = ctx.runner()
        # Query the SCHEMAS system view for the Java schema. -j = no headers,
        # -x = quiet, -a = no column formatting -> just the value.
        sql = (
            "SELECT COUNT(*) FROM SYS.SCHEMAS "
            f"WHERE SCHEMA_NAME = '{schema}'"
        )
        argv = [
            "hdbsql",
            "-n",
            f"{hana_host}:{hana_port}",
            "-u",
            str(hana_user),
            "-x",
            "-a",
            "-j",
        ]
        if hana_password:
            argv += ["-p", str(hana_password)]
        argv += [sql]

        cr = runner.run(argv)
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not query HANA for schema {schema}",
                detail=redact(cr.stderr or cr.stdout),
                data={"schema": schema, "host": hana_host, "port": hana_port},
            )
        count = _first_int(cr.stdout)
        data = {"schema": schema, "host": hana_host, "port": hana_port, "count": count}
        if count and count > 0:
            return Result.ok(
                self.name,
                f"HANA Java schema {schema} present and accessible",
                data=data,
            )
        return Result.fail(
            self.name,
            f"HANA Java schema {schema} not found in target tenant",
            data=data,
        )


def _first_int(stdout: str) -> int:
    for token in stdout.split():
        clean = token.strip().strip('"')
        if clean.isdigit():
            return int(clean)
    return 0
