"""pipo.rfc-jco-config — inventory RFC/JCo destinations for post-copy review.

PI/PO relies on JCo RFC destinations (to the ABAP integration engine, to the
SLD, to back-end systems). After a Java system copy these destinations point at
the SOURCE landscape and MUST be reviewed/re-pointed on the target. This is a
read-only inventory check: it lists the configured JCo/RFC destination names so
the operator has a concrete checklist for the post-copy (TIA-65) step. It does
NOT change anything and never prints destination credentials.

Destinations are read from the AS Java JCo RFC provider config that SWPM/NWA
exports, or from a config file path supplied via params.jco_config_path.
"""

from __future__ import annotations

import re

from exodia.core import Check, Context, Result

from ._common import redact

# Matches a destination/name entry in typical JCoRFC / destination XML/props.
_DEST_NAME = re.compile(
    r'(?:name|destinationName|jco\.destination|DestinationName)\s*[=:]\s*["\']?([A-Za-z0-9_./-]+)',
    re.IGNORECASE,
)


class RfcJcoConfigCheck(Check):
    """Inventory of RFC/JCo destinations that will need post-copy review."""

    name = "pipo.rfc-jco-config"
    description = "List RFC/JCo destinations for post-copy re-pointing review (read-only)."
    blocking = False

    def run(self, ctx: Context) -> Result:
        path = ctx.get("jco_config_path")
        if not path:
            return Result.skip(
                self.name,
                "no jco_config_path configured — set it to inventory RFC/JCo destinations",
            )
        runner = ctx.runner()
        # cat the config file (read-only). Never parse for secrets.
        cr = runner.run(["cat", str(path)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"could not read JCo/RFC config at {path}",
                detail=redact(cr.stderr),
                data={"path": str(path)},
            )
        names = sorted({m.group(1) for m in _DEST_NAME.finditer(cr.stdout)})
        data = {"path": str(path), "destinations": names, "count": len(names)}
        if not names:
            return Result.warn(
                self.name,
                f"no RFC/JCo destinations found in {path} — verify the config path",
                data=data,
            )
        return Result.ok(
            self.name,
            f"{len(names)} RFC/JCo destination(s) to review post-copy: {', '.join(names[:10])}"
            + (" …" if len(names) > 10 else ""),
            data=data,
        )
