"""Runner — orchestrates ordered check pipelines and guarded actions."""

from __future__ import annotations

from .base import Action, Check
from .context import Context
from .logging import get_logger
from .result import Result

log = get_logger()


def run_checks(checks: list[Check], ctx: Context) -> list[Result]:
    """Run an ordered list of checks. A blocking FAIL stops the pipeline early."""
    results: list[Result] = []
    for check in checks:
        if check.name in ctx.skip_checks:
            results.append(Result.skip(check.name, "skipped via config/--skip"))
            continue
        result = check.execute(ctx)
        results.append(result)
        if check.blocking and result.status.is_blocking:
            log.warning("blocking check %s failed — stopping pipeline early", check.name)
            break
    return results


def run_action(action: Action, prechecks: list[Check], ctx: Context) -> list[Result]:
    """Run pre-checks, then the guarded action flow. Aborts if a precheck blocks."""
    results = run_checks(prechecks, ctx)
    if any(r.status.is_blocking for r in results):
        results.append(Result.skip(f"{action.name}.execute", "aborted — pre-checks did not pass"))
        return results
    results.extend(action.run_guarded(ctx))
    return results
