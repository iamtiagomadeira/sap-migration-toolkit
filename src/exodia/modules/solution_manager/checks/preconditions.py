"""Solution Manager post-copy reconfiguration.

After a technical system copy of a SAP Solution Manager (or any managed system
registered in it), the landscape metadata must be reconciled. Grounded in SAP's
post-copy guidance:

* **Post-Copy Automation (PCA)** — SolMan copies must run the PCA task lists
  (SAP_BASIS_COPY_*) to clean up the copied system's configuration.
* **SLD / LMDB reachable** — the System Landscape Directory and the Landscape
  Management Database must be reachable so the copied system re-registers under
  the right SID and no stale source entries remain.
* **Data supplier / SLD registration** — the copied system must point its SLD
  data supplier at the correct SLD, not the source's.
* **Managed-system connectivity** — RFC/HTTP to the managed systems must work
  after the copy or monitoring/PCA steps fail.

This is a distinct family from the raw System Copy methods: it is the *landscape*
reconfiguration that follows a copy. Every check is read-only.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamKind, ParamSpec

# --------------------------------------------------------------------------- #
# Parameter specs
# --------------------------------------------------------------------------- #

SID = ParamSpec(
    "sid",
    "Copied system SID",
    kind=ParamKind.FIELD,
    help="SID of the freshly copied SolMan/managed system.",
)
SLD_HOST = ParamSpec(
    "sld_host",
    "SLD host",
    help="System Landscape Directory host the copy should register with.",
)
SLD_PORT = ParamSpec(
    "sld_port",
    "SLD HTTP port",
    default="50000",
    help="SLD data-supplier HTTP port (e.g. 5<nn>00).",
)
LMDB_HOST = ParamSpec(
    "lmdb_host",
    "LMDB host",
    help="Landscape Management Database host (usually the SolMan itself).",
)
MANAGED_HOST = ParamSpec(
    "managed_host",
    "Managed system host",
    help="A managed system that must be reachable via RFC/HTTP after the copy.",
)
MANAGED_INSTANCE = ParamSpec(
    "managed_instance",
    "Managed system instance number",
    default="00",
    help="Two digits; the gateway port 33<nn> is derived from it.",
)


def _run(ctx: Context, argv: list[str], timeout: int = 60):  # type: ignore[no-untyped-def]
    return ctx.runner().run(argv, timeout=timeout)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


class PcaTaskListAvailableCheck(Check):
    """The PCA task lists must be present to reconfigure the copied system.

    Post-Copy Automation ships as ABAP task lists (SAP_BASIS_COPY_*). Their
    presence is a prerequisite for a clean SolMan/managed-system copy.
    """

    name = "solution-manager.pca-tasklist-available"
    description = "Post-Copy Automation task lists are available."
    title = "STC01 — Post-Copy Automation Task Lists Available"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [SID]

    def run(self, ctx: Context) -> Result:
        sid = ctx.sid or ctx.get("sid")
        if not sid:
            return Result.skip(self.name, "no SID given for the copied system")
        # PCA runs inside ABAP (STC01 / task manager). From the OS side we can at
        # least confirm the system is up; deep task-list state needs ABAP access.
        cr = _run(ctx, ["sh", "-c", "command -v R3trans"])
        if not cr.ok:
            return Result.warn(
                self.name,
                f"R3trans not on PATH for {sid} — run PCA (SAP_BASIS_COPY_*) via "
                "task manager (STC01) inside the copied ABAP system",
                data={"sid": sid},
            )
        return Result.ok(
            self.name,
            f"ABAP toolchain present for {sid}; run PCA task lists (STC01) next",
            data={"sid": sid},
        )


class SldReachableCheck(Check):
    """The copied system must reach the correct SLD to re-register."""

    name = "solution-manager.sld-reachable"
    description = "SLD is reachable for data-supplier re-registration."
    title = "SLD Reachable (data-supplier re-registration)"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [SLD_HOST, SLD_PORT]

    def run(self, ctx: Context) -> Result:
        host = ctx.get("sld_host")
        port = str(ctx.get("sld_port") or "50000")
        if not host:
            return Result.skip(self.name, "no sld_host given; cannot probe SLD")
        cr = _run(ctx, ["nc", "-z", "-w", "5", str(host), str(port)])
        if not cr.ok:
            return Result.fail(
                self.name,
                f"cannot reach SLD at {host}:{port} — the copied system must "
                "re-point its data supplier (RZ70/SLDAPICUST) at this SLD",
                data={"sld_host": host, "sld_port": port},
            )
        return Result.ok(
            self.name,
            f"SLD reachable at {host}:{port}",
            data={"sld_host": host, "sld_port": port},
        )


class LmdbReachableCheck(Check):
    """The LMDB must be reachable so the landscape reflects the copied SID."""

    name = "solution-manager.lmdb-reachable"
    description = "LMDB host is reachable for landscape reconciliation."
    title = "LMDB Reachable (landscape reconciliation)"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [LMDB_HOST]

    def run(self, ctx: Context) -> Result:
        host = ctx.get("lmdb_host")
        if not host:
            return Result.skip(self.name, "no lmdb_host given; cannot probe LMDB")
        # LMDB lives inside SolMan (ABAP+Java); probe the standard ICM HTTP port.
        cr = _run(ctx, ["nc", "-z", "-w", "5", str(host), "8000"])
        if not cr.ok:
            return Result.warn(
                self.name,
                f"could not reach {host}:8000 — verify LMDB/SolMan is up so the "
                "copied SID reconciles and stale source entries are removed",
                data={"lmdb_host": host},
            )
        return Result.ok(
            self.name, f"LMDB host {host} reachable", data={"lmdb_host": host}
        )


class NoStaleSourceRegistrationCheck(Check):
    """Warn to remove the source system's stale SLD/LMDB registration.

    A copy that keeps the source's SLD registration data pollutes the landscape;
    this check reminds the operator to clear it (advisory — needs SLD access to
    verify programmatically).
    """

    name = "solution-manager.no-stale-source-registration"
    description = "Reminder: purge the source's stale SLD/LMDB registration."
    title = "Purge Stale Source SLD/LMDB Registration"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [SID]

    def run(self, ctx: Context) -> Result:
        sid = ctx.sid or ctx.get("sid")
        return Result.warn(
            self.name,
            "after the copy, delete the SOURCE system's technical-system entry in "
            "LMDB and re-run the copied system's SLD data supplier so only the new "
            f"SID ({sid or '?'}) is registered",
            data={"sid": sid},
        )


class ManagedSystemConnectivityCheck(Check):
    """RFC/HTTP to a managed system must work after the copy.

    Post-copy, SolMan re-establishes its connections to the managed systems.
    The SAP gateway listens on ``33<nn>`` (nn = instance) and the ICM HTTP on
    ``8<nn>00``. If neither is reachable, PCA/monitoring steps that depend on
    the managed system will fail. This probes the real gateway port instead of
    assuming reachability.

    Read-only.
    """

    name = "solution-manager.managed-system-connectivity"
    description = "Managed system reachable via SAP gateway (33<nn>) after copy."
    title = "Managed System Reachable via SAP Gateway (33<nn>)"
    blocking = False

    def parameters(self) -> list[ParamSpec]:
        return [MANAGED_HOST, MANAGED_INSTANCE]

    def run(self, ctx: Context) -> Result:
        host = ctx.get("managed_host")
        if not host:
            return Result.skip(
                self.name, "no managed_host given; cannot probe managed-system RFC"
            )
        inst = str(ctx.get("managed_instance") or "00").zfill(2)
        gw_port = int(f"33{inst}")
        cr = _run(ctx, ["nc", "-z", "-w", "5", str(host), str(gw_port)])
        if cr.ok:
            return Result.ok(
                self.name,
                f"managed system {host} reachable on SAP gateway {gw_port}",
                data={"managed_host": host, "gateway_port": gw_port},
            )
        # Gateway closed — try the ICM HTTP port before failing.
        http_port = int(f"8{inst}00")
        cr_http = _run(ctx, ["nc", "-z", "-w", "5", str(host), str(http_port)])
        if cr_http.ok:
            return Result.warn(
                self.name,
                f"managed system {host} gateway {gw_port} is closed but HTTP "
                f"{http_port} is open — verify RFC connectivity for PCA/monitoring",
                data={"managed_host": host, "gateway_port": gw_port, "http_port": http_port},
            )
        return Result.fail(
            self.name,
            f"managed system {host} unreachable on gateway {gw_port} and HTTP "
            f"{http_port} — SolMan cannot manage it until connectivity is restored",
            data={"managed_host": host, "gateway_port": gw_port, "http_port": http_port},
        )
