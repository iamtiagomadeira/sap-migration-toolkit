"""Exodia CLI — the router. `exodia list`, `exodia run <name>`, `exodia doctor`."""

from __future__ import annotations

from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .core import report
from .core.base import Check
from .core.context import ConfigError, Context
from .core.evidence import (
    EvidenceBundle,
    find_active_bundle,
    find_latest_bundle,
    list_bundles,
    render_html,
    replay_events,
    verify_bundle,
)
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
from .core.monitor import get_monitor
from .core.registry import registry
from .core.runner import run_action, run_checks, run_runbook

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
        try:
            ctx = Context.from_file(config)
        except ConfigError as exc:
            console.print(f"[red]config error:[/] {exc}")
            raise typer.Exit(2) from exc
        # CLI flags override file values when provided.
        overrides: dict[str, Any] = {"dry_run": dry_run, "assume_yes": yes}
        if host:
            overrides["host"] = host
        if user:
            overrides["user"] = user
        if db_type:
            overrides["db_type"] = db_type
        if source:
            overrides["source"] = source
        if target:
            overrides["target"] = target
        try:
            return ctx.model_copy(update=overrides)
        except ValidationError as exc:
            console.print(f"[red]invalid option:[/] {exc.errors()[0]['msg']}")
            raise typer.Exit(2) from exc
    try:
        return Context.model_validate(
            {
                "host": host,
                "user": user,
                "db_type": db_type,
                "source": source,
                "target": target,
                "dry_run": dry_run,
                "assume_yes": yes,
            }
        )
    except ValidationError as exc:
        console.print(f"[red]invalid option:[/] {exc.errors()[0]['msg']}")
        raise typer.Exit(2) from exc


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
    monitor: bool = typer.Option(
        False,
        "--monitor",
        help="Show a live dashboard for long-running actions (progress, log tail, results).",
    ),
    no_emoji: bool = typer.Option(
        False,
        "--no-emoji",
        help="Use ASCII status tags instead of emoji (CI / non-UTF-8 terminals). "
        "NO_COLOR is also honoured automatically.",
    ),
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
        title = f"Action: {name}" + (" (dry-run)" if ctx.dry_run else "")
        if monitor and not as_json:
            # Live dashboard: stream each phase result into the monitor as it lands.
            mon = get_monitor(title, enabled=True, no_emoji=no_emoji)
            with mon:
                mon.phase("pre-checks")
                results = run_action(action, prechecks, ctx)
                for r in results:
                    mon.result(r)
                mon.phase("done")
        else:
            results = run_action(action, prechecks, ctx)

    if as_json:
        console.print_json(report.render_json(results))
    else:
        report.render_table(results, title, console, no_emoji=no_emoji)

    raise typer.Exit(report.exit_code(results))


@app.command("runbooks")
def list_runbooks() -> None:
    """List all discovered runbooks (ordered check sweeps with an aggregate verdict)."""
    runbooks = registry.runbooks()
    rt = Table(title="Runbooks (read-only sweeps)", expand=True)
    rt.add_column("Name", style="green")
    rt.add_column("Steps")
    rt.add_column("Description")
    for name, rb_cls in sorted(runbooks.items()):
        rt.add_row(name, str(len(rb_cls.steps)), rb_cls.description)
    console.print(rt)
    if not runbooks:
        console.print("[yellow]No runbooks discovered yet.[/]")


@app.command("runbook")
def run_runbook_op(
    name: str = typer.Argument(..., help="Runbook name, e.g. 'abap.cutover-readiness'."),
    host: str | None = typer.Option(None, "--host", help="Remote host (omit for local)."),
    user: str | None = typer.Option(None, "--user", help="SSH user for remote host."),
    db_type: str | None = typer.Option(None, "--db-type", help="hana | ase | ..."),
    source: str | None = typer.Option(None, "--source"),
    target: str | None = typer.Option(None, "--target"),
    yes: bool = typer.Option(False, "--yes", help="Assume yes (unattended)."),
    config: str | None = typer.Option(
        None, "--config", help="YAML config with saved connection params."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    no_emoji: bool = typer.Option(
        False,
        "--no-emoji",
        help="Use ASCII status tags instead of emoji (CI / non-UTF-8 terminals).",
    ),
) -> None:
    """Run a runbook: an ordered, read-only sweep with one aggregate verdict.

    A runbook re-reads the live system on every run (no cached state), streams
    each step into a sealed evidence bundle, and finishes with a readiness
    verdict. Safe to re-run as often as you like — it always reflects the
    current state of the system.
    """
    rb_cls = registry.get_runbook(name)
    if rb_cls is None:
        console.print(f"[red]Unknown runbook:[/] {name}. Try `exodia runbooks`.")
        raise typer.Exit(2)

    # Runbooks are read-only, so a runbook run is never a state-changing action;
    # dry_run is irrelevant here and left at its default.
    ctx = _build_context(host, user, db_type, source, target, dry_run=True, yes=yes, config=config)
    runbook = rb_cls()

    bundle = EvidenceBundle(name, ctx, operation="runbook").open()
    results = run_runbook(runbook, ctx, evidence=bundle)
    bundle.close(results)

    title = f"Runbook: {name}"
    if as_json:
        console.print_json(report.render_json(results))
    else:
        report.render_table(results, title, console, no_emoji=no_emoji)
        console.print(f"[dim]📁 evidence: {bundle.dir}[/]")

    raise typer.Exit(report.exit_code(results))


def _resolve_check_objs(name: str) -> list[Check]:
    """Resolve a name to an ordered list of check instances.

    Accepts a runbook name (expands to its steps) or a single check name, so
    ``snapshot`` and ``compare`` work with either. Unknown names raise Exit(2).
    """
    rb_cls = registry.get_runbook(name)
    if rb_cls is not None:
        objs: list[Check] = []
        for step in rb_cls().steps:
            cc = registry.get_check(step)
            if cc is not None:
                objs.append(cc())
        return objs
    check_cls = registry.get_check(name)
    if check_cls is not None:
        return [check_cls()]
    console.print(f"[red]Unknown check or runbook:[/] {name}. Try `exodia runbooks` / `exodia list`.")
    raise typer.Exit(2)


@app.command("snapshot")
def snapshot_op(
    name: str = typer.Argument(..., help="Runbook or check name to capture, e.g. 'tenant-copy.hana.readiness'."),
    output: str = typer.Option(..., "--output", "-o", help="Path to write the snapshot JSON."),
    side: str = typer.Option("source", "--side", help="Which side this is: source | target."),
    label: str | None = typer.Option(None, "--label", help="Human label (defaults to SID/host)."),
    host: str | None = typer.Option(None, "--host"),
    user: str | None = typer.Option(None, "--user"),
    db_type: str | None = typer.Option(None, "--db-type"),
    source: str | None = typer.Option(None, "--source"),
    target: str | None = typer.Option(None, "--target"),
    config: str | None = typer.Option(None, "--config", help="YAML config with connection params."),
    no_emoji: bool = typer.Option(False, "--no-emoji"),
) -> None:
    """Capture one side of a migration into a portable, tamper-evident snapshot.

    Run this WITH ACCESS TO ONE SIDE (e.g. logged on to the customer source).
    It runs the read-only checks, prints the table, and writes a signed JSON
    file you carry to the other side for `exodia compare`. Never mutates anything.
    """
    from .core.snapshot import Snapshot

    ctx = _build_context(host, user, db_type, source, target, dry_run=True, yes=False, config=config)
    check_objs = _resolve_check_objs(name)

    bundle = EvidenceBundle(name, ctx, operation=f"snapshot:{side}").open()
    results = run_checks(check_objs, ctx, evidence=bundle)  # type: ignore[arg-type]
    bundle.close(results)

    snap = Snapshot.capture(side=side, operation=name, results=results, ctx=ctx, label=label)
    path = snap.write(output)

    report.render_table(results, f"Snapshot [{side}]: {name}", console, no_emoji=no_emoji)
    console.print(f"[dim]📁 evidence: {bundle.dir}[/]")
    console.print(f"[green]📸 snapshot written:[/] {path}  ([dim]carry this to the other side[/])")
    raise typer.Exit(report.exit_code(results))


@app.command("compare")
def compare_op(
    snapshot_file: str = typer.Argument(..., help="Path to a snapshot JSON captured on the other side."),
    against: str | None = typer.Option(
        None, "--against", help="Runbook/check to capture live on THIS side and compare against."
    ),
    with_file: str | None = typer.Option(
        None, "--with", help="A second snapshot file to compare against (offline diff)."
    ),
    side: str = typer.Option("target", "--side", help="Which side THIS is when capturing live."),
    host: str | None = typer.Option(None, "--host"),
    user: str | None = typer.Option(None, "--user"),
    db_type: str | None = typer.Option(None, "--db-type"),
    source: str | None = typer.Option(None, "--source"),
    target: str | None = typer.Option(None, "--target"),
    config: str | None = typer.Option(None, "--config"),
    as_json: bool = typer.Option(False, "--json"),
    no_emoji: bool = typer.Option(False, "--no-emoji"),
) -> None:
    """Compare a carried-over snapshot against this side — the automated runbook diff.

    Two modes:
      * live:    `exodia compare source.json --against <runbook> --config tgt.yaml`
                 captures this side now and diffs it against the file.
      * offline: `exodia compare source.json --with target.json`
                 diffs two already-captured snapshots.

    Verifies the incoming snapshot's hash first (tamper-evident), then prints a
    check-by-check source-vs-target table with an aligned / diverge verdict.
    """
    from .core.compare import compare_snapshots
    from .core.snapshot import Snapshot, verify_snapshot

    problems = verify_snapshot(snapshot_file)
    if problems:
        console.print(f"[red]⛔ snapshot verification failed:[/] {snapshot_file}")
        for p in problems:
            console.print(f"  • {p}")
        raise typer.Exit(1)
    incoming = Snapshot.read(snapshot_file)

    if with_file:
        other_problems = verify_snapshot(with_file)
        if other_problems:
            console.print(f"[red]⛔ second snapshot verification failed:[/] {with_file}")
            for p in other_problems:
                console.print(f"  • {p}")
            raise typer.Exit(1)
        this_side = Snapshot.read(with_file)
    elif against:
        ctx = _build_context(host, user, db_type, source, target, dry_run=True, yes=False, config=config)
        check_objs = _resolve_check_objs(against)
        bundle = EvidenceBundle(against, ctx, operation=f"compare-capture:{side}").open()
        results = run_checks(check_objs, ctx, evidence=bundle)  # type: ignore[arg-type]
        bundle.close(results)
        this_side = Snapshot.capture(side=side, operation=against, results=results, ctx=ctx)
    else:
        console.print("[red]provide either --against <runbook> (live) or --with <file> (offline).[/]")
        raise typer.Exit(2)

    # Orient the diff so 'source' is always the source-side snapshot.
    if incoming.side == "source":
        rpt = compare_snapshots(incoming, this_side)
    else:
        rpt = compare_snapshots(this_side, incoming)

    if as_json:
        import json as _json

        console.print_json(
            _json.dumps(
                {
                    "operation": rpt.operation,
                    "source_label": rpt.source_label,
                    "target_label": rpt.target_label,
                    "aligned": rpt.aligned,
                    "rows": [vars(r) for r in rpt.rows],
                },
                default=str,
            )
        )
    else:
        _render_comparison(rpt, no_emoji=no_emoji)

    raise typer.Exit(0 if rpt.aligned else 1)


def _render_comparison(rpt: object, *, no_emoji: bool = False) -> None:
    """Render a ComparisonReport as a source-vs-target table with a verdict."""
    from .core.compare import ComparisonReport

    assert isinstance(rpt, ComparisonReport)  # nosec B101 - internal call contract
    icon = {
        "match": "[OK]" if no_emoji else "✅",
        "differ": "[DIFF]" if no_emoji else "❌",
        "source-only": "[SRC]" if no_emoji else "⬅️ ",
        "target-only": "[TGT]" if no_emoji else "➡️ ",
        "error": "[ERR]" if no_emoji else "💥",
    }
    table = Table(title=f"Compare: {rpt.operation}  ({rpt.source_label} → {rpt.target_label})", expand=True)
    table.add_column("", width=6 if no_emoji else 3)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Verdict")
    table.add_column("Source")
    table.add_column("Target")
    table.add_column("Detail")
    for r in rpt.rows:
        table.add_row(
            icon.get(r.verdict, "?"),
            r.name,
            r.verdict.upper(),
            _short(r.source_value),
            _short(r.target_value),
            r.detail,
        )
    console.print(table)
    v = rpt.verdict_result()
    if rpt.aligned:
        console.print(f"[bold green]✅ SIDES ALIGNED — {v.summary}[/]")
    else:
        console.print(f"[bold red]⛔ SIDES DIVERGE — {v.summary}[/]")


def _short(value: object) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= 48 else s[:45] + "…"


@app.command("doctor")
def doctor() -> None:
    """Self-check: verify Exodia's own setup and discovery."""
    checks = registry.checks()
    actions = registry.actions()
    runbooks = registry.runbooks()
    console.print(f"[green]exodia {__version__}[/]")
    console.print(f"  discovered checks : {len(checks)}")
    console.print(f"  discovered actions: {len(actions)}")
    console.print(f"  discovered runbooks: {len(runbooks)}")
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


@app.command(name="report")
def report_cmd(
    bundle_dir: str = typer.Argument(
        "", help="Evidence bundle to render. Omit to use the most recent one."
    ),
    fmt: str = typer.Option(
        "both", "--format", "-f", help="Output format: html, md, or both."
    ),
    out: str = typer.Option("", "--out", "-o", help="Output path/prefix (no extension)."),
) -> None:
    """Render a run's evidence bundle as a shareable HTML + Markdown summary.

    Reads the sealed bundle (manifest + results) and writes standalone files —
    handy for handover docs and build-in-public screenshots. The Markdown is the
    bundle's own report.md; the HTML is generated inline (no external assets).
    """
    from pathlib import Path

    d = Path(bundle_dir) if bundle_dir else find_latest_bundle()
    if d is None:
        console.print(
            "[red]no evidence bundle found[/] — run a check/action first, "
            "or pass a bundle directory explicitly."
        )
        raise typer.Exit(1)
    if not (d / "manifest.json").is_file():
        console.print(f"[red]not an evidence bundle:[/] {d}")
        raise typer.Exit(1)

    prefix = Path(out) if out else Path.cwd() / f"exodia-report-{d.name}"
    fmt = fmt.lower()
    written: list[Path] = []
    if fmt in ("md", "both"):
        md_src = d / "report.md"
        md_dest = prefix.with_suffix(".md")
        if md_dest.resolve() != md_src.resolve():
            md_dest.write_text(md_src.read_text(), encoding="utf-8")
        written.append(md_dest)
    if fmt in ("html", "both"):
        html_dest = prefix.with_suffix(".html")
        html_dest.write_text(render_html(d), encoding="utf-8")
        written.append(html_dest)
    if not written:
        console.print(f"[red]unknown format:[/] {fmt} (use html, md, or both)")
        raise typer.Exit(1)
    console.print(f"[green]✅ report written[/] from {d}:")
    for p in written:
        console.print(f"  • {p}")


@app.command(name="history")
def history_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show."),
    root: str = typer.Option("evidence", "--root", help="Evidence root directory."),
) -> None:
    """List past migration runs with their exact start, end and duration.

    Reads every evidence bundle under the evidence root and prints a table
    (newest first) so you can answer 'when did that migration start/end and how
    long did it take' retroactively — the audit clock, persisted.
    """
    rows = list_bundles(root)
    if not rows:
        console.print(
            f"[yellow]no evidence bundles under[/] {root!r} — run a check/action first."
        )
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold", title="Migration history")
    table.add_column("started (UTC)")
    table.add_column("methodology")
    table.add_column("operation")
    table.add_column("SID")
    table.add_column("duration", justify="right")
    table.add_column("results", justify="right")
    for r in rows[: max(1, limit)]:
        started = (r.get("started") or "—").replace("T", " ").replace("+00:00", "")
        table.add_row(
            started,
            str(r.get("methodology") or "?"),
            str(r.get("operation") or ""),
            str(r.get("sid") or "—"),
            str(r.get("duration_str") or "—"),
            str(r.get("results_count") or 0),
        )
    console.print(table)
    if len(rows) > limit:
        console.print(f"[dim]… {len(rows) - limit} older run(s) not shown (use --limit).[/]")


@app.command(name="reattach")
def reattach_cmd(
    bundle_dir: str = typer.Argument(
        "",
        help="Bundle to reattach to. Omit to auto-detect the newest unsealed run.",
    ),
    root: str = typer.Option("evidence", "--root", help="Evidence root directory."),
    no_emoji: bool = typer.Option(
        False, "--no-emoji", help="ASCII status tags (CI / non-UTF-8 terminals)."
    ),
) -> None:
    """Reconnect the live dashboard to an operation already in flight.

    A restore/recovery can run for hours; if the SSH session drops you lose the
    dashboard but not the operation. ``reattach`` rebuilds the dashboard from the
    bundle's persisted event trail (run.jsonl) — phase, log tail and per-result
    timing — so you can keep watching. With no argument it finds the newest
    bundle that has events but is not yet sealed.
    """
    from pathlib import Path

    target = Path(bundle_dir) if bundle_dir else find_active_bundle(root)
    if target is None:
        console.print(
            f"[yellow]no in-flight operation found under[/] {root!r} "
            "(nothing unsealed with events to reattach to)."
        )
        raise typer.Exit(0)
    if not (target / "run.jsonl").is_file():
        console.print(f"[red]no event trail (run.jsonl) in[/] {target}")
        raise typer.Exit(1)

    mon = get_monitor(f"Reattached: {target.name}", enabled=True, no_emoji=no_emoji)
    with mon:
        n = replay_events(target, mon)
    console.print(f"[dim]replayed {n} event(s) from {target}[/]")


if __name__ == "__main__":
    app()
