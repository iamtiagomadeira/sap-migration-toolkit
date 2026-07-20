"""Exodia CLI — the router. `exodia list`, `exodia run <name>`, `exodia doctor`."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .core import report
from .core.context import Context
from .core.logging import configure
from .core.menu import (
    Operation,
    build_context,
    checks_in,
    collect_params,
    discover_operations,
    methodologies,
    params_for_checks,
    spec_for,
)
from .core.registry import registry
from .core.runner import run_action, run_checks

app = typer.Typer(
    name="exodia",
    help="Stateless executor for SAP migration operations (HANA/ASE, Java AS).",
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
        ctx = Context.from_file(config)
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


class _TyperPrompter:
    """Real Prompter: numbered menus + prompts via Rich/Typer."""

    def choose(self, title: str, options: list[str]) -> int:
        console.print(f"\n[bold cyan]{title}[/]")
        for i, opt in enumerate(options, start=1):
            console.print(f"  [bold]{i}[/]. {opt}")
        while True:
            raw = typer.prompt("  Choose", default="1")
            try:
                idx = int(raw) - 1
            except ValueError:
                idx = -1
            if 0 <= idx < len(options):
                return idx
            console.print("[red]  invalid choice — enter a number from the list[/]")

    def ask(self, prompt: str, default: str | None, secret: bool) -> str:
        answer: str = typer.prompt(
            f"  {prompt}",
            default=default if default is not None else "",
            hide_input=secret,
            show_default=not secret,
        )
        return answer

    def confirm(self, prompt: str, default: bool = False) -> bool:
        return typer.confirm(prompt, default=default)

    def note(self, message: str) -> None:
        console.print(f"[dim]{message}[/]")


@app.command("menu")
def menu() -> None:
    """Interactive wizard — pick a methodology and operation, no long commands."""
    prompter = _TyperPrompter()
    ops = discover_operations(registry)
    if not ops:
        console.print("[yellow]No operations discovered yet.[/]")
        raise typer.Exit(0)

    # Step 1: methodology
    groups = methodologies(ops)
    labels = [g.replace("-", " ").replace("_", " ").title() for g in groups]
    g_idx = prompter.choose("Select a methodology", labels)
    methodology = groups[g_idx]

    # Step 2: operation within the methodology (checks first — they're safe)
    group_ops = [o for o in ops if o.methodology == methodology]
    group_checks = checks_in(ops, methodology)
    run_all_label = (
        f"✅ Run ALL {len(group_checks)} pre-checks in this methodology"
    )
    op_labels = [run_all_label] + [
        f"{'🔍' if o.kind == 'check' else '⚙️ '} {o.name}  —  {o.description}"
        for o in group_ops
    ]
    sel = prompter.choose(f"[{labels[g_idx]}] Select an operation", op_labels)

    # "Run all pre-checks" is offered as index 0 when the methodology has checks.
    if sel == 0 and group_checks:
        specs = params_for_checks(group_checks, registry)
        console.print(
            f"\n[bold]Configure:[/] all {len(group_checks)} pre-checks "
            f"for {methodology} (answer the combined fields once)"
        )
        fields, params = collect_params(specs, prompter)
        ctx = build_context(fields, params, execute=False, assume_yes=False)
        check_objs = [
            cc() for c in group_checks if (cc := registry.get_check(c.name)) is not None
        ]
        results = run_checks(check_objs, ctx)
        report.render_table(results, f"Pre-checks: {methodology}", console)
        console.print(
            Panel(report.verdict_line(results), title="Verdict", border_style="cyan")
        )
        raise typer.Exit(report.exit_code(results))

    op: Operation = group_ops[sel - 1]

    # Step 3: collect declared parameters
    specs = spec_for(op, registry)
    console.print(f"\n[bold]Configure:[/] {op.name}")
    fields, params = collect_params(specs, prompter)

    # Step 4: execution mode (actions only; checks are always read-only)
    execute = False
    assume_yes = False
    if op.kind == "action":
        console.print(
            "\n[yellow]This is a state-changing action.[/] "
            "Dry-run shows the plan without touching anything."
        )
        execute = prompter.confirm("Execute for real (not just dry-run)?", default=False)
        if execute:
            assume_yes = prompter.confirm(
                "Confirm: run the guarded execution now?", default=False
            )
            if not assume_yes:
                console.print("[dim]Staying in dry-run — nothing will be executed.[/]")
                execute = False

    ctx = build_context(fields, params, execute=execute, assume_yes=assume_yes)

    # Step 5: run
    if op.kind == "check":
        check_cls = registry.get_check(op.name)
        assert check_cls is not None  # nosec B101 - from discovery
        results = run_checks([check_cls()], ctx)
        title = f"Check: {op.name}"
    else:
        action_cls = registry.get_action(op.name)
        assert action_cls is not None  # nosec B101 - from discovery
        action = action_cls()
        prechecks = [
            pc() for c in action.requires_checks if (pc := registry.get_check(c)) is not None
        ]
        results = run_action(action, prechecks, ctx)
        title = f"Action: {op.name}" + (" (dry-run)" if ctx.dry_run else "")

    report.render_table(results, title, console)
    raise typer.Exit(report.exit_code(results))


if __name__ == "__main__":
    app()
