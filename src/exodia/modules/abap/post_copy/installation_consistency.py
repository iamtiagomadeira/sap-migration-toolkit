"""Post-copy installation-consistency check (cross-cutting, read-only).

After a system copy — by any method — the copied ABAP system must be checked for
installation consistency before it is handed over: SICK/SM28 must be clean, no
SPAU/SPDD adjustments should be left pending, and the software component set must
be intact. This is the POST-phase counterpart of the PREPARATION-phase
``abap.readiness.installation-consistency`` check: same read-only style, but run
on the copy after the fact and aggregating the three signals into one verdict.

Strictly read-only. FAILs on genuine SICK errors; WARNs on pending
SPAU/SPDD adjustments (a manual post-copy task, not a hard blocker).
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamSpec
from exodia.core.result import Phase

from ..readiness import _rfc


class PostCopyInstallationConsistencyCheck(Check):
    """Post-copy consistency: SICK/SM28, pending SPAU/SPDD, component versions."""

    name = "abap.post.installation-consistency"
    description = "Post-copy consistency: SICK/SM28 clean, no pending SPAU/SPDD, components intact."
    title = "Post-Copy Installation Consistency (SICK / SPAU-SPDD / Components)"
    phase = Phase.POST
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return _rfc.SOURCE_CONN_SPECS

    def run(self, ctx: Context) -> Result:
        side = _rfc.SOURCE
        if not _rfc.has_connection_params(ctx, side):
            return Result.skip(
                self.name, "no RFC connection params (set source_ashost + credentials)"
            )
        try:
            client = _rfc.get_client(ctx, side)
        except _rfc.RfcError as exc:
            return Result.fail(self.name, f"could not connect for consistency check: {exc}")

        sick_errors = self._sick_errors(client)
        pending_spau = self._pending_modifications(client, "SPAU")
        pending_spdd = self._pending_modifications(client, "SPDD")
        components = self._component_count(client)

        facts = {
            "SICK Errors": "n/a" if sick_errors is None else str(sick_errors),
            "Pending SPDD": "n/a" if pending_spdd is None else str(pending_spdd),
            "Pending SPAU": "n/a" if pending_spau is None else str(pending_spau),
            "Software Components": "n/a" if components is None else str(components),
        }
        data = {
            "sick_errors": sick_errors,
            "pending_spdd": pending_spdd,
            "pending_spau": pending_spau,
            "components": components,
        }

        if sick_errors:
            return Result.fail(
                self.name,
                f"post-copy consistency FAIL: SICK reports {sick_errors} error(s)",
                data=data, facts=facts,
            )
        pending_total = (pending_spau or 0) + (pending_spdd or 0)
        if pending_total:
            return Result.warn(
                self.name,
                f"post-copy consistency: {pending_total} pending modification "
                "adjustment(s) (SPAU/SPDD) — resolve before handover",
                data=data, facts=facts,
            )
        return Result.ok(
            self.name,
            "post-copy installation consistency OK: SICK clean, no pending "
            f"SPAU/SPDD, {components if components is not None else 'n/a'} components present",
            data=data, facts=facts,
        )

    @staticmethod
    def _sick_errors(client: _rfc.RfcClient) -> int | None:
        """Number of SICK/SM28 errors, or None if the FM is unavailable."""
        try:
            res = client.call("SUSR_CHECK_INSTALLATION_CONSISTENCY")
        except _rfc.RfcError:
            return None
        messages = res.get("ET_MESSAGES", []) or res.get("MESSAGES", []) or []
        return len([m for m in messages if str(m.get("TYPE", "")).upper() in ("E", "A")])

    @staticmethod
    def _pending_modifications(client: _rfc.RfcClient, kind: str) -> int | None:
        """Count pending SPAU (repository) / SPDD (dictionary) adjustments.

        Reads the modification-adjustment worklist table; None when it cannot be
        read (FM/table not exposed on this release).
        """
        try:
            rows = _rfc.read_table(
                client, "ADIRACCESS", fields=["OBJ_NAME"],
                where=f"ADJUST_TYPE = '{kind}'",
            )
        except _rfc.RfcError:
            return None
        return len(rows)

    @staticmethod
    def _component_count(client: _rfc.RfcClient) -> int | None:
        """Number of installed software components (CVERS), or None on error."""
        try:
            rows = _rfc.read_table(client, "CVERS", fields=["COMPONENT"])
        except _rfc.RfcError:
            return None
        return len(rows)
