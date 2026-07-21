"""Base classes for the two operation categories: Check and Action.

Check  = read-only validation. Safe to run anywhere, any time.
Action = state-changing execution. Guarded: requires pre-checks, dry-run first,
         explicit confirmation, verify after, documented rollback.

This distinction is the safety backbone of Exodia.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from .context import Context
from .knowledge import enrich
from .logging import get_logger
from .params import ParamSpec
from .result import Phase, Result

log = get_logger()


class Check(ABC):
    """A read-only validation. Never mutates the target."""

    #: unique dotted name, e.g. "hana.free-space"
    name: str = ""
    #: human description
    description: str = ""
    #: if True, a FAIL aborts the surrounding prepare pipeline immediately
    blocking: bool = False
    #: which cutover macro-phase this check belongs to (drives report grouping)
    phase: Phase = Phase.UNCLASSIFIED
    #: explicit, action-oriented report title, e.g. "SM12 — Lock Entries Check".
    #: Falls back to the dotted name when empty.
    title: str = ""

    @abstractmethod
    def run(self, ctx: Context) -> Result:
        """Perform the validation and return a structured Result."""
        ...

    def parameters(self) -> list[ParamSpec]:
        """Inputs this check needs. Override to drive the interactive menu.

        Default: no declared inputs. The wizard still offers the common
        connection fields and the free-form escape hatch, so undeclared
        operations keep working.
        """
        return []

    def execute(self, ctx: Context) -> Result:
        """Wrapper: runs the check, catches exceptions, enriches from KB.

        Also stamps the check's declared ``phase`` / ``title`` onto the Result
        (unless ``run`` already set them), so every check is grouped and labelled
        for the human report without each ``run`` having to repeat that metadata.
        """
        started = datetime.now(UTC)
        try:
            result = self.run(ctx)
        except Exception as exc:  # noqa: BLE001 - convert to structured ERROR
            log.exception("check %s raised", self.name)
            result = Result.error(self.name, f"unexpected error: {exc}")
        if result.phase is Phase.UNCLASSIFIED and self.phase is not Phase.UNCLASSIFIED:
            result.phase = self.phase
        if not result.title and self.title:
            result.title = self.title
        result.stamp_timing(started, datetime.now(UTC))
        if result.status.is_blocking:
            enrich(result, ctx)
        return result


class Action(ABC):
    """A state-changing operation. Guarded by the safe-execution flow."""

    name: str = ""
    description: str = ""
    #: marks that this modifies systems (always True for real actions)
    destructive: bool = True
    #: names of checks that MUST pass before this action runs
    requires_checks: list[str] = []
    #: which cutover macro-phase this action belongs to (drives report grouping)
    phase: Phase = Phase.UNCLASSIFIED
    #: explicit, action-oriented report title; falls back to the dotted name.
    title: str = ""

    @abstractmethod
    def dry_run(self, ctx: Context) -> Result:
        """Describe exactly what execute() would do, without doing it."""
        ...

    def parameters(self) -> list[ParamSpec]:
        """Inputs this action needs. Override to drive the interactive menu.

        Default: no declared inputs. The wizard still offers the common
        connection fields and the free-form escape hatch.
        """
        return []

    @abstractmethod
    def execute(self, ctx: Context) -> Result:
        """Perform the action. Only called after dry-run + confirmation."""
        ...

    @abstractmethod
    def verify(self, ctx: Context) -> Result:
        """Confirm the action achieved its goal (e.g. replica ACTIVE)."""
        ...

    def rollback(self, ctx: Context) -> Result:
        """Best-effort reversal. Default: documented-only (no auto-rollback)."""
        return Result.skip(
            f"{self.name}.rollback",
            "no automatic rollback — see runbook / SAP Note for manual steps",
        )

    def run_guarded(self, ctx: Context) -> list[Result]:
        """The full safe-execution flow. Returns one Result per phase."""
        phase_results: list[Result] = []

        # Phase 2: dry-run always happens and is shown.
        dr = self._tag(self._safe(self.dry_run, ctx, f"{self.name}.dry-run"))
        phase_results.append(dr)
        if ctx.dry_run:
            return phase_results  # stop here in dry-run mode (the default)

        # Phase 3: confirmation gate (unless --yes).
        if not ctx.assume_yes:
            phase_results.append(
                self._tag(
                    Result.skip(f"{self.name}.execute", "awaiting confirmation (--yes not set)")
                )
            )
            return phase_results

        # Phase 4: execute.
        ex = self._tag(self._safe(self.execute, ctx, f"{self.name}.execute"))
        phase_results.append(ex)
        if ex.status.is_blocking:
            enrich(ex, ctx)
            return phase_results  # do NOT verify a failed execute

        # Phase 5: verify.
        phase_results.append(self._tag(self._safe(self.verify, ctx, f"{self.name}.verify")))
        return phase_results

    def _tag(self, result: Result) -> Result:
        """Stamp this action's phase/title onto a phase result (if unset)."""
        if result.phase is Phase.UNCLASSIFIED and self.phase is not Phase.UNCLASSIFIED:
            result.phase = self.phase
        if not result.title and self.title:
            result.title = self.title
        return result

    @staticmethod
    def _safe(fn, ctx: Context, name: str) -> Result:  # type: ignore[no-untyped-def]
        started = datetime.now(UTC)
        try:
            result: Result = fn(ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s raised", name)
            result = Result.error(name, f"unexpected error: {exc}")
        return result.stamp_timing(started, datetime.now(UTC))
