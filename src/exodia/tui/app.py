"""ExodiaTUI — the Textual application: a two-axis migration cockpit.

Run it with ``exodia tui`` (or ``python -m exodia.tui``). The cockpit is built
around the two axes an SAP system copy actually has:

  * METHOD axis  — *which* migration procedure. SAP splits system duplication
                   into "System Copy" (classic SWPM: Backup & Restore, Export &
                   Import) and "System Transition" (HANA DB-level: Tenant Copy,
                   HSR). That is the left tree's top level.
  * PHASE axis   — *when* in the cutover. Every method walks the same four
                   macro-phases (Preparation -> Ramp-Down -> Downtime ->
                   Post-Activities, per the ECS/HEC cutover plan). Once a method
                   + context is chosen, the tree lays the operations out by
                   phase.

Choosing a method prompts for the context that disambiguates it — Source DB ->
Target DB (same DB = homogeneous, different = heterogeneous/migration) and the
application-server Stack (ABAP vs AS Java/PI-PO, which decides the post-copy
activities). Read-only operations (checks, runbooks) run for real in a worker
thread and stream into the log + results table; state-changing actions are shown
but deferred to the guarded CLI flow.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Label,
    RichLog,
    Select,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from .. import __version__
from ..core.context import Context
from ..core.evidence import EvidenceBundle
from ..core.menu import (
    DATABASES,
    STACKS,
    copy_kind,
    db_blocks,
    discover_operations,
    group_by_phase,
    methodologies_in_family,
    migration_families,
    operations_for_context,
    pretty,
    runbooks_in,
)
from ..core.registry import registry
from ..core.result import Phase, Result, Status, format_duration
from ..core.runner import run_checks, run_runbook

# ASCII wordmark — pure typography of the tool's own name (no third-party art).
_WORDMARK = r""" ███████ ██   ██  ██████  ██████  ██  █████
 ██       ██ ██  ██    ██ ██   ██ ██ ██   ██
 █████     ███   ██    ██ ██   ██ ██ ███████
 ██       ██ ██  ██    ██ ██   ██ ██ ██   ██
 ███████ ██   ██  ██████  ██████  ██ ██   ██"""

_STATUS_ICON = {
    Status.PASS: "✅",
    Status.WARN: "⚠️",
    Status.FAIL: "❌",
    Status.SKIP: "⏭️",
    Status.ERROR: "💥",
}
_STATUS_CLASS = {
    Status.PASS: "pass",
    Status.WARN: "warn",
    Status.FAIL: "fail",
    Status.SKIP: "skip",
    Status.ERROR: "err",
}

# Compact per-phase labels for the phase-progress panel (kept short so the bars
# line up). Falls back to the LIFECYCLE_PHASES label with its leading number
# stripped for any phase not listed here.
_PHASE_SHORT = {
    "preparation": "Preparation",
    "ramp_down": "Ramp-Down",
    "downtime": "Downtime",
    "post": "Post",
    "unclassified": "Other",
}

# Human labels for the application-server stacks offered in the context modal.
_STACK_LABELS = {
    "abap": "ABAP",
    "java": "Java (PI/PO)",
    "dual": "Dual-stack (ABAP+Java)",
    "solman": "Solution Manager",
}


class ContextModal(ModalScreen[dict | None]):
    """Prompt Source DB → Target DB → Stack for a chosen migration method.

    Dismisses with a dict ``{"source_db", "target_db", "stack"}`` on confirm, or
    ``None`` on cancel. Enforces the SAP method×DB rules (e.g. Tenant Copy / HSR
    are HANA-only) live, disabling Confirm with an inline reason when invalid.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, methodology: str, method_label: str) -> None:
        super().__init__()
        self._methodology = methodology
        self._method_label = method_label

    def compose(self) -> ComposeResult:
        with Container(id="modal-box"):
            yield Static(
                f"[b]Configure:[/] [cyan]{self._method_label}[/]\n"
                f"[dim]Source DB → Target DB → Stack. "
                f"Different DBs ⇒ heterogeneous (migration).[/]",
                id="modal-title",
                markup=True,
            )
            yield Label("Source database")
            yield Select(
                [(db, db) for db in DATABASES],
                id="source-db",
                value=DATABASES[0],
                allow_blank=False,
            )
            yield Label("Target database")
            yield Select(
                [(db, db) for db in DATABASES],
                id="target-db",
                value=DATABASES[0],
                allow_blank=False,
            )
            yield Label("Application-server stack")
            yield Select(
                [(_STACK_LABELS[s], s) for s in STACKS],
                id="stack",
                value="abap",
                allow_blank=False,
            )
            yield Static("", id="modal-msg", markup=True)
            with Horizontal(id="modal-buttons"):
                yield Button("Confirm", variant="primary", id="confirm")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        self._revalidate()

    def on_select_changed(self, event: Select.Changed) -> None:
        self._revalidate()

    def _revalidate(self) -> str | None:
        """Update the inline message + Confirm state; return blocking reason."""
        src = str(self.query_one("#source-db", Select).value)
        tgt = str(self.query_one("#target-db", Select).value)
        msg = self.query_one("#modal-msg", Static)
        confirm = self.query_one("#confirm", Button)

        reason = db_blocks(self._methodology, src) or db_blocks(self._methodology, tgt)
        if reason:
            msg.update(f"[b red]✗ {reason}[/]")
            confirm.disabled = True
            return reason

        kind = copy_kind(src, tgt)
        badge = "homogeneous" if kind == "homogeneous" else "heterogeneous (migration)"
        msg.update(f"[green]✓ {src} → {tgt}  ·  [b]{badge}[/][/]")
        confirm.disabled = False
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            if self._revalidate() is not None:
                return
            self.dismiss(
                {
                    "source_db": str(self.query_one("#source-db", Select).value),
                    "target_db": str(self.query_one("#target-db", Select).value),
                    "stack": str(self.query_one("#stack", Select).value),
                }
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExodiaTUI(App[None]):
    """The two-axis (method × phase) migration cockpit."""

    CSS_PATH = "exodia.tcss"
    TITLE = "EXODIA"
    SUB_TITLE = "SAP migration cockpit"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "run_selected", "Run / Configure"),
        Binding("r", "run_selected", "Run", show=False),
        Binding("escape", "clear_context", "Reset method", show=False),
        # vim-style + Tab panel focus movement
        Binding("tab", "focus_next", "Next panel", show=False),
        Binding("shift+tab", "focus_previous", "Prev panel", show=False),
        Binding("l", "focus_main", "→ main", show=False),
        Binding("h", "focus_sidebar", "← tree", show=False),
        Binding("f", "toggle_maximize", "Zoom panel"),
        Binding("c", "clear_log", "Clear log"),
        Binding("d", "toggle_dark", "Dark/Light"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._ops = discover_operations(registry)
        self._results: list[Result] = []
        self._started_at = datetime.now(UTC)
        # active context: None until a method is configured via the modal.
        self._active_ctx: dict | None = None
        # Phase-progress state (distinct names to avoid clashing with internal
        # Textual App attributes — a past bug had self._context shadow
        # App._context and hang compose).
        # ordered phase keys currently shown (subset of LIFECYCLE_PHASES keys)
        self._phase_order: list[str] = []
        # phase_key -> total expected ops in the active context
        self._phase_totals: dict[str, int] = {}
        # phase_key -> set of Result.name seen (dedup so re-runs don't overcount)
        self._phase_done: dict[str, set[str]] = {}
        # Result.name -> phase_key map for the active context
        self._name_to_phase: dict[str, str] = {}
        self._counts = {
            "checks": len(registry.checks()),
            "actions": len(registry.actions()),
            "runbooks": len(registry.runbooks()),
        }

    # -- layout ------------------------------------------------------------- #
    def compose(self) -> ComposeResult:
        c = self._counts
        header_text = (
            f"{_WORDMARK}\n\n"
            f"[b white]The stateless SAP migration toolkit[/]  "
            f"[dim]— read-only checks, guarded actions, sealed evidence.[/]\n"
            f"[cyan]Creator:[/] [b]Tiago Madeira[/]   [dim]·[/]   "
            f"[cyan]v[/]{__version__}   [dim]·[/]   "
            f"[green]{c['checks']} checks[/] · "
            f"[magenta]{c['actions']} actions[/] · "
            f"[green]{c['runbooks']} runbooks[/]"
        )
        yield Static(header_text, id="header", markup=True)

        with Horizontal(id="body"):
            with Container(id="sidebar"):
                yield Static("Migration methods", id="sidebar-title")
                yield self._build_tree()
            with Vertical(id="main"):
                with Container(id="detail"):
                    yield Static("Detail", classes="panel-title")
                    yield Static(
                        "[dim]Pick a migration method on the left "
                        "(System Copy or System Transition) and press Enter "
                        "to configure Source/Target DB + stack.[/]",
                        id="detail-body",
                        markup=True,
                    )
                with Container(id="phasepanel"):
                    yield Static("Phase progress", classes="panel-title")
                    yield Static(
                        self._phase_board_text(),
                        id="phase-board",
                        markup=True,
                    )
                with Container(id="logpanel"):
                    yield Static("Live log", classes="panel-title")
                    yield RichLog(id="log", highlight=True, markup=True, wrap=True)
                with Container(id="resultspanel"):
                    yield Static("Results", classes="panel-title")
                    yield DataTable(id="results", zebra_stripes=True, cursor_type="row")

        yield Static(self._board_text(), id="footer-board", markup=True)
        yield Footer()

    def _build_tree(self) -> Tree[dict]:
        """Build the METHOD-axis tree: family → method (leaves are selectable)."""
        tree: Tree[dict] = Tree("migration methods", id="optree")
        tree.root.expand()
        tree.show_root = False
        for fam in migration_families(self._ops):
            fam_node = tree.root.add(f"📦 {pretty(fam)}", data={"kind": "family"})
            fam_node.expand()
            for method in methodologies_in_family(self._ops, fam):
                fam_node.add_leaf(
                    f"📂 {pretty(method)}",
                    data={"kind": "method", "methodology": method},
                )
        return tree

    def _rebuild_tree_for_context(self) -> None:
        """Rebuild the tree to show the chosen method's PHASE-grouped plan.

        Called after the context modal confirms. Replaces the method picker with
        a phased plan (Preparation → Ramp-Down → Downtime → Post-Activities) of
        the operations that apply to this (method, stack) context.
        """
        assert self._active_ctx is not None
        ctx = self._active_ctx
        method = ctx["methodology"]
        stack = ctx["stack"]

        tree = self.query_one(Tree)
        tree.clear()
        tree.show_root = True
        kind = copy_kind(ctx["source_db"], ctx["target_db"])
        tree.root.set_label(
            f"🧭 {pretty(method)}  ·  {ctx['source_db']}→{ctx['target_db']} "
            f"({kind})  ·  {_STACK_LABELS.get(stack, stack)}"
        )
        tree.root.expand()
        tree.root.data = {"kind": "context-root"}

        ctx_ops = operations_for_context(self._ops, methodology=method, stack=stack)
        for _key, label, group in group_by_phase(ctx_ops):
            phase_node = tree.root.add(f"📁 {label}", data={"kind": "phase"})
            phase_node.expand()
            # runbooks that belong to this method surface under Preparation-style
            # sweeps; attach any runbook whose methodology matches, once, on the
            # phase that carries its checks. Simpler: list method runbooks under
            # the first phase group they were seen. Here we list per-op only.
            for op in group:
                icon = {"check": "🔍", "action": "⚙️ ", "runbook": "📋"}.get(op.kind, "•")
                data = {
                    "kind": op.kind,
                    "name": op.name,
                    "desc": op.description,
                    "label": op.label,
                }
                # Human label leads; the raw machine name trails in dim so an
                # admin reads "SM04/AL08 — Logged-On Users Check" not
                # "abap.readiness.active-users", but can still copy the id.
                phase_node.add_leaf(
                    f"{icon} {op.label}  [dim]· {op.name}[/]", data=data
                )

        # Method-level runbooks (one-click sweeps) as a dedicated top group.
        rbs = runbooks_in(registry, method)
        if rbs:
            rb_node = tree.root.add("📋 Runbooks (one-click sweeps)", data={"kind": "phase"})
            rb_node.expand()
            for name, desc, steps in rbs:
                rb_node.add_leaf(
                    f"📋 {name}  [dim]({steps} checks)[/]",
                    data={"kind": "runbook", "name": name, "desc": desc},
                )
        tree.focus()

    # -- phase progress ----------------------------------------------------- #
    def _rebuild_phase_progress(self) -> None:
        """Recompute per-phase totals + name→phase map from the active context.

        Called after the context modal confirms. Only phases that actually exist
        in this (method, stack) context are shown (via ``group_by_phase``), so a
        context with just Preparation+Downtime+Post shows three bars, not four.
        Any previously accumulated progress is reset — a new plan starts empty.
        """
        self._phase_order = []
        self._phase_totals = {}
        self._phase_done = {}
        self._name_to_phase = {}
        if self._active_ctx is None:
            self._refresh_phase_board()
            return
        ctx = self._active_ctx
        ctx_ops = operations_for_context(
            self._ops, methodology=ctx["methodology"], stack=ctx["stack"]
        )
        for key, _label, group in group_by_phase(ctx_ops):
            self._phase_order.append(key)
            self._phase_totals[key] = len(group)
            self._phase_done[key] = set()
            for op in group:
                self._name_to_phase[op.name] = key
        self._refresh_phase_board()

    def _reset_phase_progress(self) -> None:
        """Clear all phase-progress state (back to the no-context view)."""
        self._phase_order = []
        self._phase_totals = {}
        self._phase_done = {}
        self._name_to_phase = {}
        self._refresh_phase_board()

    def _record_phase_result(self, result: Result) -> None:
        """Attribute a Result to its lifecycle phase for the progress bars.

        The Result carries an explicit ``.phase`` enum, but we prefer the
        context's own name→phase map (built from the active plan) so a result
        counts against the exact bar shown. Falls back to the Result's declared
        phase when the name is not in the active context. Dedups by name so
        re-running the same op does not overshoot the total.
        """
        if not self._phase_order:
            return
        phase_key = self._name_to_phase.get(result.name)
        if phase_key is None:
            phase_key = getattr(result.phase, "value", None)
        if phase_key not in self._phase_done:
            return
        self._phase_done[phase_key].add(result.name)
        self._refresh_phase_board()

    def _phase_agg_icon(self, phase_key: str) -> str:
        """Aggregate status glyph for a phase from the results seen so far.

        ❌ any FAIL/ERROR · ⚠️ any WARN · ✅ all done & clean · ⏳ in progress ·
        empty string when nothing has run for the phase yet.
        """
        done = self._phase_done.get(phase_key, set())
        if not done:
            return ""
        statuses = [
            r.status
            for r in self._results
            if self._name_to_phase.get(r.name, getattr(r.phase, "value", None))
            == phase_key
        ]
        if any(s in (Status.FAIL, Status.ERROR) for s in statuses):
            return _STATUS_ICON[Status.FAIL]
        if any(s is Status.WARN for s in statuses):
            return _STATUS_ICON[Status.WARN]
        total = self._phase_totals.get(phase_key, 0)
        if len(done) >= total and total > 0:
            return _STATUS_ICON[Status.PASS]
        return "⏳"

    _PHASE_KEY_TO_ENUM = {
        "preparation": Phase.PREPARATION,
        "ramp_down": Phase.RAMP_DOWN,
        "downtime": Phase.DOWNTIME,
        "post": Phase.POST,
    }

    def _phase_gate_badge(self, phase_key: str) -> str:
        """Gate verdict badge for a phase, from the real gate engine.

        Runs :func:`evaluate_gate` over the results seen for this phase so the
        board shows the same GO / NO-GO / GO-WITH-OVERRIDE decision the CLI and
        exception report use — not a hand-rolled heuristic. Returns a short
        coloured token (e.g. ``[fail]NO-GO[/]``) or empty string when nothing
        graded yet. Advisory failures never flip a gate to NO-GO here, exactly
        as the COP model dictates.
        """
        from ..core.gate import GateDecision, evaluate_gate

        phase = self._PHASE_KEY_TO_ENUM.get(phase_key)
        if phase is None:
            return ""
        phase_results = [
            r
            for r in self._results
            if self._name_to_phase.get(r.name, getattr(r.phase, "value", None))
            == phase_key
        ]
        if not phase_results:
            return ""
        # The TUI runs interactively without a config file, so it uses the
        # intrinsic (default) gate policy. Per-engagement reclassification lives
        # in the CLI path where a --config is supplied.
        verdict = evaluate_gate(phase, phase_results)
        token = {
            GateDecision.GO: "[pass]GO[/]",
            GateDecision.NO_GO: "[fail]NO-GO[/]",
            GateDecision.GO_WITH_OVERRIDE: "[warn]GO*[/]",
            GateDecision.PENDING: "",
        }
        return token.get(verdict.decision, "")

    def _phase_board_text(self) -> str:
        """Render the compact per-phase progress board (markup string).

        One line per phase in cutover order:
            ``Preparation   ▓▓▓▓▓░░░░░  5/10  ⏳``
        The filled portion is coloured by the aggregate phase state (success /
        warning / error / in-progress). When no context is active, prompts the
        operator to configure a method.
        """
        if not self._phase_order:
            return (
                "[dim]Configure a migration method (pick one on the left and "
                "press Enter) to see phase-by-phase progress here.[/]"
            )
        bar_w = 10
        # widest short label so the bars align in a column
        label_w = max(
            (len(_PHASE_SHORT.get(k, k)) for k in self._phase_order), default=0
        )
        lines: list[str] = []
        for key in self._phase_order:
            total = self._phase_totals.get(key, 0)
            done = len(self._phase_done.get(key, set()))
            filled = round(bar_w * done / total) if total else 0
            # show at least one filled cell once any op has run, and never
            # overfill; a fully-done phase always shows a full bar.
            if done and total:
                filled = max(1, min(bar_w, filled))
                if done >= total:
                    filled = bar_w
            else:
                filled = 0
            icon = self._phase_agg_icon(key)
            if icon == _STATUS_ICON[Status.FAIL]:
                klass = "fail"
            elif icon == _STATUS_ICON[Status.WARN]:
                klass = "warn"
            elif icon == _STATUS_ICON[Status.PASS]:
                klass = "pass"
            else:
                klass = "accent"
            bar = f"[{klass}]{'▓' * filled}[/][dim]{'░' * (bar_w - filled)}[/]"
            label = _PHASE_SHORT.get(key, key).ljust(label_w)
            gate_badge = self._phase_gate_badge(key)
            gate_suffix = f"  {gate_badge}" if gate_badge else ""
            lines.append(
                f"[b]{label}[/]  {bar}  [b]{done}/{total}[/]  {icon}{gate_suffix}".rstrip()
            )
        return "\n".join(lines)

    def _refresh_phase_board(self) -> None:
        """Push the current phase board into its Static (no-op before mount)."""
        with contextlib.suppress(Exception):  # widget not mounted yet
            self.query_one("#phase-board", Static).update(self._phase_board_text())

    # -- setup after mount -------------------------------------------------- #
    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_columns("", "operation", "status", "duration", "summary")
        self.query_one(Tree).focus()

    # -- tree selection ----------------------------------------------------- #
    def on_tree_node_selected(self, event: Tree.NodeSelected[dict]) -> None:
        self._show_detail(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[dict]) -> None:
        self._show_detail(event.node)

    def _show_detail(self, node: TreeNode[dict]) -> None:
        data = node.data or {}
        kind = data.get("kind")
        body = self.query_one("#detail-body", Static)
        if kind == "method":
            body.update(
                f"[b cyan]{pretty(data['methodology'])}[/]  "
                f"[dim](migration method)[/]\n\n"
                f"[green]▶ Enter to configure[/] — pick Source DB → Target DB → "
                f"stack, then the plan lays out by cutover phase.\n"
                f"[dim]Different source/target DB ⇒ heterogeneous (migration).[/]"
            )
        elif kind == "check":
            check_cls = registry.get_check(data["name"])
            blocking = getattr(check_cls, "blocking", False) if check_cls else False
            label = data.get("label") or data["name"]
            body.update(
                f"[b cyan]🔍 {label}[/]  [dim](read-only check)[/]\n"
                f"[dim]· {data['name']}[/]\n\n"
                f"{data.get('desc') or '—'}\n\n"
                f"blocking: [b]{'yes' if blocking else 'no'}[/]\n"
                f"[green]▶ Enter to run — safe, reads the live system.[/]"
            )
        elif kind == "runbook":
            rb_cls = registry.get_runbook(data["name"])
            steps = len(getattr(rb_cls, "steps", []) or [])
            label = data.get("label") or data["name"]
            body.update(
                f"[b green]📋 {label}[/]  [dim](runbook — {steps} checks)[/]\n"
                f"[dim]· {data['name']}[/]\n\n"
                f"{data.get('desc') or '—'}\n\n"
                f"[green]▶ Enter to run the whole sweep — re-reads the live "
                f"system, writes a sealed evidence bundle.[/]"
            )
        elif kind == "action":
            act_cls = registry.get_action(data["name"])
            reqs = ", ".join(getattr(act_cls, "requires_checks", []) or []) or "—"
            label = data.get("label") or data["name"]
            body.update(
                f"[b magenta]⚙️ {label}[/]  [dim](state-changing action)[/]\n"
                f"[dim]· {data['name']}[/]\n\n"
                f"{data.get('desc') or '—'}\n\n"
                f"requires checks: {reqs}\n"
                f"[yellow]⚠ Actions are guarded and NOT run from the TUI. "
                f"Use:[/] [b]exodia run {data['name']} --execute[/]"
            )
        elif kind == "phase":
            body.update(
                "[b]Cutover phase[/]\n\n"
                "[dim]Operations grouped by lifecycle phase. Pick a check or "
                "runbook leaf and press Enter to run it (read-only).[/]"
            )
        elif kind == "context-root":
            body.update(
                "[b]Migration plan[/]  [dim](press Esc to pick another method)[/]\n\n"
                "[dim]The plan below is laid out by cutover phase for the chosen "
                "method, DB direction and stack.[/]"
            )
        else:
            body.update("[dim]Pick a migration method, then press Enter.[/]")

    # -- running ------------------------------------------------------------ #
    def action_run_selected(self) -> None:
        tree = self.query_one(Tree)
        node = tree.cursor_node
        data = (node.data or {}) if node else {}
        kind = data.get("kind")
        if kind == "method":
            self._configure_method(data["methodology"])
        elif kind == "check":
            self._run_worker(kind="check", name=data["name"])
        elif kind == "runbook":
            self._run_worker(kind="runbook", name=data["name"])
        elif kind == "action":
            self.notify(
                f"'{data['name']}' is a state-changing action — run it via "
                f"`exodia run {data['name']} --execute` for the guarded flow.",
                severity="warning",
                title="Guarded action",
            )
        else:
            self.notify("Pick a migration method, check or runbook first.",
                        severity="information")

    def _configure_method(self, methodology: str) -> None:
        """Open the context modal for a method; rebuild the plan on confirm."""
        def _on_close(result: dict | None) -> None:
            if not result:
                return
            self._active_ctx = {"methodology": methodology, **result}
            self.query_one("#log", RichLog).write(
                f"[b]▶ configured[/] {pretty(methodology)} — "
                f"{result['source_db']}→{result['target_db']} "
                f"({copy_kind(result['source_db'], result['target_db'])}), "
                f"stack={result['stack']}"
            )
            self._rebuild_tree_for_context()
            self._rebuild_phase_progress()

        self.push_screen(ContextModal(methodology, pretty(methodology)), _on_close)

    def action_clear_context(self) -> None:
        """Return to the method picker (Esc)."""
        if self._active_ctx is None:
            return
        self._active_ctx = None
        self._reset_phase_progress()
        tree = self.query_one(Tree)
        tree.clear()
        tree.show_root = False
        # repopulate the existing tree widget in place
        tree.root.set_label("migration methods")
        for fam in migration_families(self._ops):
            fam_node = tree.root.add(f"📦 {pretty(fam)}", data={"kind": "family"})
            fam_node.expand()
            for method in methodologies_in_family(self._ops, fam):
                fam_node.add_leaf(
                    f"📂 {pretty(method)}",
                    data={"kind": "method", "methodology": method},
                )
        tree.root.expand()
        tree.focus()
        self.query_one("#detail-body", Static).update(
            "[dim]Pick a migration method on the left and press Enter.[/]"
        )

    @work(thread=True, exclusive=True)
    def _run_worker(self, *, kind: str, name: str) -> None:
        """Run a check or runbook in a worker thread, streaming into the UI.

        Read-only only: builds a minimal local Context (dry-run), runs it, and
        pushes phase/log/result events back onto the UI thread via the
        ``mon_*`` hooks (mirroring what an action's monitor would do).
        """
        self.call_from_thread(self.mon_start, f"{kind}: {name}")
        self.call_from_thread(self.mon_phase, "running", name)
        try:
            ctx = Context(dry_run=True, assume_yes=False)  # type: ignore[call-arg]
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.mon_log, f"[red]could not build context: {exc}[/]")
            self.call_from_thread(self.mon_stop)
            return

        try:
            if kind == "check":
                check_cls = registry.get_check(name)
                if check_cls is None:
                    self.call_from_thread(self.mon_log, f"[red]unknown check: {name}[/]")
                    self.call_from_thread(self.mon_stop)
                    return
                self.call_from_thread(self.mon_log, f"[cyan]running check →[/] {name} …")
                results = run_checks([check_cls()], ctx)
            else:
                rb_cls = registry.get_runbook(name)
                if rb_cls is None:
                    self.call_from_thread(self.mon_log, f"[red]unknown runbook: {name}[/]")
                    self.call_from_thread(self.mon_stop)
                    return
                bundle = EvidenceBundle(name, ctx, operation="runbook").open()
                self.call_from_thread(
                    self.mon_log, f"[cyan]running runbook →[/] {name} ({len(rb_cls().steps)} steps) …"
                )
                results = run_runbook(rb_cls(), ctx, evidence=bundle)
                bundle.close(results)
                self.call_from_thread(self.mon_log, f"[dim]📁 evidence: {bundle.dir}[/]")
        except Exception as exc:  # noqa: BLE001 - surface, never crash the UI
            self.call_from_thread(self.mon_log, f"[red]run failed: {exc}[/]")
            self.call_from_thread(self.mon_stop)
            return

        for r in results:
            self.call_from_thread(self.mon_result, r)
        self.call_from_thread(self.mon_phase, "done", name)
        self.call_from_thread(self.mon_stop)

    # -- Monitor hooks (called on the UI thread) ---------------------------- #
    def mon_start(self, title: str) -> None:
        self.query_one("#log", RichLog).write(f"[b]▶ {title}[/]  [dim]started[/]")

    def mon_stop(self) -> None:
        self.query_one("#footer-board", Static).update(self._board_text())

    def mon_phase(self, name: str, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        self.query_one("#log", RichLog).write(f"[cyan]▶ phase:[/] {name}{suffix}")

    def mon_progress(self, percent: float | None, detail: str = "") -> None:
        if percent is not None:
            self.query_one("#log", RichLog).write(f"[cyan]  {percent:5.1f}%[/] {detail}")

    def mon_log(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def mon_result(self, result: Result) -> None:
        self._results.append(result)
        self._record_phase_result(result)
        icon = _STATUS_ICON.get(result.status, "")
        klass = _STATUS_CLASS.get(result.status, "")
        table = self.query_one("#results", DataTable)
        table.add_row(
            icon,
            result.display_title,
            f"[{klass}]{result.status.value.upper()}[/]",
            result.duration_str,
            (result.summary or "")[:80],
        )
        self.query_one("#footer-board", Static).update(self._board_text())

    def mon_handoff(self, message: str, url: str | None = None) -> None:
        log = self.query_one("#log", RichLog)
        log.write(f"[b yellow]⏸ HANDOFF[/] {message}")
        if url:
            log.write(f"[underline cyan]{url}[/]")

    # -- readiness board ---------------------------------------------------- #
    def _board_text(self) -> str:
        counts = dict.fromkeys(Status, 0)
        for r in self._results:
            counts[r.status] += 1
        order = [Status.PASS, Status.WARN, Status.FAIL, Status.SKIP, Status.ERROR]
        segs = []
        for s in order:
            n = counts[s]
            if n:
                segs.append(f"[{_STATUS_CLASS[s]}]{_STATUS_ICON[s]} {n} {s.value}[/]")
        body = "  ·  ".join(segs) if segs else "[dim]no results yet — pick an op and press Enter[/]"
        elapsed = format_duration((datetime.now(UTC) - self._started_at).total_seconds())
        total = len(self._results)
        return f" [b]Readiness[/]   {body}    [dim]│ {total} result(s) · session {elapsed}[/]"

    # -- actions ------------------------------------------------------------ #
    def action_focus_main(self) -> None:
        self.query_one("#results", DataTable).focus()

    def action_focus_sidebar(self) -> None:
        self.query_one(Tree).focus()

    def action_toggle_maximize(self) -> None:
        """Zoom the focused panel to full screen (and back)."""
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif self.focused is not None:
            self.screen.maximize(self.focused)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_toggle_dark(self) -> None:
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"


def run_tui() -> None:
    """Entry point used by ``exodia tui``."""
    ExodiaTUI().run()
