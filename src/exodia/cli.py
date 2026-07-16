"""Exodia CLI — the router. `exodia list`, `exodia run <name>`, `exodia doctor`."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .core import report
from .core.config import ConfigError
from .core.context import Context
from .core.logging import configure
from .core.registry import registry
from .core.runner import run_action, run_checks

app = typer.Typer(
    name="exodia",
    help="Stateless executor for SAP migration operations (HANA/ASE, PI/PO Java).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"exodia {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
    _version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="Show version and exit."
    ),
) -> None:
    configure(verbose=verbose)


@app.command("list")
def list_ops() -> None:
    """List all discovered checks and actions."""
    checks = registry.checks()
    actions = registry.actions()

    ct = Table(title="Checks (read-only)", expand=True)
    ct.add_column("Name", style="cyan")
    ct.add_column("Blocking")
    ct.add_column("Description")
    for name, check_cls in sorted(checks.items()):
        ct.add_row(name, "yes" if check_cls.blocking else "no", check_cls.description)
    console.print(ct)

    at = Table(title="Actions (state-changing)", expand=True)
    at.add_column("Name", style="magenta")
    at.add_column("Requires checks")
    at.add_column("Description")
    for name, action_cls in sorted(actions.items()):
        at.add_row(name, ", ".join(action_cls.requires_checks) or "-", action_cls.description)
    console.print(at)

    if not checks and not actions:
        console.print("[yellow]No operations discovered yet. Modules land under exodia.modules.[/]")


def _build_context(
    host: str | None,
    user: str | None,
    db_type: str | None,
    source: str | None,
    target: str | None,
    dry_run: bool,
    yes: bool,
    config: str | None,
) -> Context:
    if config:
        try:
            ctx = Context.from_file(config)
        except ConfigError as exc:
            console.print(f"[red]Config error:[/]\n{exc}")
            raise typer.Exit(2) from exc
        # CLI flags override file values when provided.
        if host:
            ctx.host = host
        if user:
            ctx.user = user
        if db_type:
            ctx.db_type = db_type
        if source:
            ctx.source = source
        if target:
            ctx.target = target
        ctx.dry_run = dry_run
        ctx.assume_yes = yes
        return ctx
    return Context(
        host=host,
        user=user,
        db_type=db_type,
        source=source,
        target=target,
        dry_run=dry_run,
        assume_yes=yes,
    )


@app.command("run")
def run_op(
    name: str = typer.Argument(..., help="Check or action name, e.g. 'hana.free-space'."),
    host: str | None = typer.Option(None, "--host", help="Remote host (omit for local)."),
    user: str | None = typer.Option(None, "--user", help="SSH user for remote host."),
    db_type: str | None = typer.Option(None, "--db-type", help="hana | ase | ..."),
    source: str | None = typer.Option(None, "--source"),
    target: str | None = typer.Option(None, "--target"),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Dry-run (default) or execute."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation for actions."),
    config: str | None = typer.Option(None, "--config", help="YAML config (escape hatch)."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Run a check or a guarded action by name."""
    ctx = _build_context(host, user, db_type, source, target, dry_run, yes, config)

    check_cls = registry.get_check(name)
    action_cls = registry.get_action(name)

    if check_cls is None and action_cls is None:
        console.print(f"[red]Unknown operation:[/] {name}. Try `exodia list`.")
        raise typer.Exit(2)

    if check_cls is not None:
        results = run_checks([check_cls()], ctx)
        title = f"Check: {name}"
    else:
        assert action_cls is not None  # nosec B101 - type-narrowing invariant (checked above), not a security gate
        action = action_cls()
        prechecks = []
        for c in action.requires_checks:
            pc_cls = registry.get_check(c)
            if pc_cls is not None:
                prechecks.append(pc_cls())
        results = run_action(action, prechecks, ctx)
        title = f"Action: {name}" + (" (dry-run)" if ctx.dry_run else "")

    if as_json:
        console.print_json(report.render_json(results))
    else:
        report.render_table(results, title, console)

    raise typer.Exit(report.exit_code(results))


@app.command("doctor")
def doctor() -> None:
    """Self-check: verify Exodia's own setup and discovery."""
    checks = registry.checks()
    actions = registry.actions()
    console.print(f"[green]exodia {__version__}[/]")
    console.print(f"  discovered checks : {len(checks)}")
    console.print(f"  discovered actions: {len(actions)}")
    from .core.knowledge import _load_kb

    console.print(f"  KB error entries  : {len(_load_kb())}")
    console.print("[green]✅ core healthy[/]")


if __name__ == "__main__":
    app()
