"""Exodia CLI — the router. `exodia list`, `exodia run <name>`, `exodia doctor`."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .core import report
from .core.context import Context
from .core.evidence import EvidenceBundle, verify_bundle
from .core.logging import configure
from .core.menu import (
    STACKS,
    Operation,
    build_context,
    checks_in,
    collect_params,
    discover_operations,
    families,
    methodologies_in_family,
    params_for_checks,
    pretty,
    spec_for,
    stack_blocks,
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

    # Step 1: umbrella family (e.g. "System Copy")
    fams = families(ops)
    fam_labels = [pretty(f) for f in fams]
    f_idx = prompter.choose("Select a migration family", fam_labels)
    family = fams[f_idx]

    # Step 2: methodology within the family (skip prompt if only one)
    methods = methodologies_in_family(ops, family)
    if len(methods) == 1:
        methodology = methods[0]
    else:
        m_labels = [pretty(m) for m in methods]
        m_idx = prompter.choose(f"[{pretty(family)}] Select a method", m_labels)
        methodology = methods[m_idx]

    # Step 2b: stack gate — the SAP application-server stack constrains methods.
    # If the chosen method is unsupported for the picked stack, block with the
    # SAP-grounded reason instead of letting the operator run an invalid copy.
    stack_labels = ["ABAP", "Java (AS Java)", "Dual-stack (ABAP+Java)", "Solution Manager"]
    s_idx = prompter.choose(f"[{pretty(methodology)}] Which stack?", stack_labels)
    stack = STACKS[s_idx]
    block = stack_blocks(stack, methodology)
    if block:
        console.print(
            Panel(
                f"[red]Not supported by SAP[/]\n\n{block}",
                title="⛔ Stack / method incompatible",
                border_style="red",
            )
        )
        raise typer.Exit(2)

    # Step 3: operation within the methodology (checks first — they're safe)
    group_ops = [o for o in ops if o.methodology == methodology]
    group_checks = checks_in(ops, methodology)
    run_all_label = (
        f"✅ Run ALL {len(group_checks)} pre-checks in this methodology"
    )
    op_labels = [run_all_label] + [
        f"{'🔍' if o.kind == 'check' else '⚙️ '} {o.name}  —  {o.description}"
        for o in group_ops
    ]
    sel = prompter.choose(f"[{pretty(methodology)}] Select an operation", op_labels)

    # "Run all pre-checks" is offered as index 0 when the methodology has checks.
    if sel == 0 and group_checks:
        specs = params_for_checks(group_checks, registry)
        console.print(
            f"\n[bold]Configure:[/] all {len(group_checks)} pre-checks "
            f"for {methodology} (answer the combined fields once)"
        )
        fields, params = collect_params(specs, prompter)
        # Thread the chosen stack through so checks/actions can adapt.
        params.setdefault("stack", stack)
        ctx = build_context(fields, params, execute=False, assume_yes=False)
        check_objs = [
            cc() for c in group_checks if (cc := registry.get_check(c.name)) is not None
        ]
        bundle = EvidenceBundle(methodology, ctx, operation="run-all-pre-checks").open()
        results = run_checks(check_objs, ctx, evidence=bundle)
        bundle.close(results)
        report.render_table(results, f"Pre-checks: {methodology}", console)
        console.print(
            Panel(report.verdict_line(results), title="Verdict", border_style="cyan")
        )
        console.print(f"[dim]📁 evidence: {bundle.dir}[/]")
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

    # Step 5: run — evidence captured automatically for the audit trail.
    bundle = EvidenceBundle(methodology, ctx, operation=op.name).open()
    if op.kind == "check":
        check_cls = registry.get_check(op.name)
        assert check_cls is not None  # nosec B101 - from discovery
        results = run_checks([check_cls()], ctx, evidence=bundle)
        title = f"Check: {op.name}"
    else:
        action_cls = registry.get_action(op.name)
        assert action_cls is not None  # nosec B101 - from discovery
        action = action_cls()
        prechecks = [
            pc() for c in action.requires_checks if (pc := registry.get_check(c)) is not None
        ]
        results = run_action(action, prechecks, ctx, evidence=bundle)
        title = f"Action: {op.name}" + (" (dry-run)" if ctx.dry_run else "")

    bundle.close(results)
    report.render_table(results, title, console)
    console.print(f"[dim]📁 evidence: {bundle.dir}[/]")
    raise typer.Exit(report.exit_code(results))


evidence_app = typer.Typer(name="evidence", help="Manage migration evidence bundles.")
app.add_typer(evidence_app)


@evidence_app.command("verify")
def evidence_verify(bundle_dir: str) -> None:
    """Re-hash a bundle's artifacts and report any tampering."""
    problems = verify_bundle(bundle_dir)
    if not problems:
        console.print(f"[green]✅ evidence intact[/] — {bundle_dir}")
        raise typer.Exit(0)
    console.print(f"[red]⛔ evidence problems in {bundle_dir}:[/]")
    for p in problems:
        console.print(f"  • {p}")
    raise typer.Exit(1)


@evidence_app.command("attach")
def evidence_attach(
    bundle_dir: str,
    file: str,
    caption: str = typer.Option("", "--caption", "-c", help="Describe the attachment."),
) -> None:
    """Attach an external file (harvested log, screenshot) to an existing bundle.

    Re-seals the manifest so the new artifact is hashed and tamper-evident.
    """
    from pathlib import Path

    d = Path(bundle_dir)
    if not (d / "manifest.json").is_file():
        console.print(f"[red]not an evidence bundle:[/] {bundle_dir}")
        raise typer.Exit(1)
    import json
    import shutil

    from .core.evidence import _sha256  # noqa: PLC2701 - internal helper reuse

    src = Path(file)
    if not src.is_file():
        console.print(f"[red]not a file:[/] {file}")
        raise typer.Exit(1)
    dest = d / "artifacts" / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    # Re-seal manifest: add/refresh the artifact list with fresh hashes.
    manifest = json.loads((d / "manifest.json").read_text())
    artifacts = []
    for f in sorted(d.rglob("*")):
        if f.is_file() and f.name != "manifest.json":
            artifacts.append(
                {"path": str(f.relative_to(d)), "sha256": _sha256(f), "bytes": f.stat().st_size}
            )
    manifest["artifacts"] = artifacts
    manifest.setdefault("attachments", []).append({"file": src.name, "caption": caption})
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    console.print(f"[green]✅ attached[/] {src.name} → {dest.parent}")


if __name__ == "__main__":
    app()
