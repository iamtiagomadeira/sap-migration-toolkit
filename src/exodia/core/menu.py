"""Interactive menu — the operator-friendly front door to Exodia.

Goal: an admin should never have to hand-craft a long command line or a YAML
file. ``exodia menu`` walks them through it:

    1. pick a methodology (grouped from discovered operations)
    2. pick an operation within it (checks first — they're safe — then actions)
    3. answer only the fields that operation declares (with defaults + choices)
    4. review, confirm, run — actions keep the guarded dry-run -> confirm flow

The prompting is abstracted behind a small ``Prompter`` protocol so the wizard
logic is unit-testable with a scripted fake, with the real Typer/Rich prompts
used at runtime.

Operation grouping is derived from the dotted name: the methodology is the first
segment (``tenant-copy`` from ``tenant-copy.hana.copy-tenant``). This means any
future module is picked up automatically with zero menu wiring — same philosophy
as the auto-discovery registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .base import Action, Check
from .context import Context
from .params import ParamKind, ParamSpec, dedupe
from .registry import Registry

_FIELD_KEYS = {"host", "user", "db_type", "source", "target", "sid", "system_type"}

# --- Migration taxonomy (validated against official SAP SL Toolset / SWPM
# System Copy guides + the RISE "System Transition Workbench", 2026-07-23).
#
# SAP splits system duplication into two top-level buckets, and we mirror that
# faithfully rather than lumping everything under one label:
#
#   * "System Copy"       — the classic SWPM methods. Backup/restore is the
#                           database-specific method; export/import (R3load /
#                           JLoad via SWPM) is the database-independent method.
#   * "System Copy" — the umbrella SAP uses for duplicating a system. Its
#                     methods are: backup & restore and export & import (classic
#                     SWPM), plus the HANA database-level procedures tenant copy
#                     and HANA System Replication (HSR). The HANA-level ones are
#                     only available on HANA (see HANA_ONLY_METHODS); the modal
#                     blocks non-HANA source/target for them.
#
# Everything else discovered in the registry (abap.*, pipo.*, solution-manager.*)
# is NOT a migration method — it is cross-cutting lifecycle work (ramp-down,
# post-activities, product-specific post-copy). Those are surfaced by the PHASE
# axis (see LIFECYCLE_PHASES) and by stack-specific post-activities, never as a
# top-level "migration type". PI/PO in particular is just the AS Java branch of
# post-copy activities, not a migration type of its own.
#
# Maps a family key -> ordered methodology names (first dotted name segment).
# A methodology not listed here becomes its own standalone family, so any future
# module still appears with zero menu wiring (same philosophy as auto-discovery).
FAMILIES: dict[str, list[str]] = {
    "system-copy": ["backup-restore", "export-import", "tenant-copy", "hsr"],
}

# The migration *methods* an operator picks first (flattened, ordered). These
# are the leaves of the method axis — the concrete procedures a copy runs on.
MIGRATION_METHODS: dict[str, str] = {
    "backup-restore": "Backup & Restore",
    "export-import": "Export & Import",
    "tenant-copy": "Tenant Copy",
    "hsr": "HSR",
}

# Cross-cutting methodologies that are NOT migration methods. They contribute
# lifecycle/post-copy operations along the PHASE axis regardless of method.
CROSS_CUTTING: tuple[str, ...] = ("abap", "pipo", "solution-manager")

# --- DB axis. Source DB -> Target DB is prompted after the method. When the
# two differ it is a *heterogeneous* system copy (a migration in SAP terms);
# when they match it is *homogeneous*. Order mirrors SWPM's own DB menu.
DATABASES: tuple[str, ...] = ("HANA", "ASE", "Oracle", "MSSQL", "MaxDB")


def copy_kind(source_db: str, target_db: str) -> str:
    """Classify a copy per SAP naming conventions from the source/target DB.

    Same DB platform  -> "homogeneous"; different -> "heterogeneous" (migration).
    """
    return (
        "homogeneous"
        if source_db.strip().lower() == target_db.strip().lower()
        else "heterogeneous"
    )


# --- Method x DB compatibility (validated against SAP guides, 2026-07-23).
# Tenant Copy and HSR are HANA-only database-level procedures — they have no
# meaning on ASE/Oracle/MSSQL/MaxDB. Backup/restore and export/import support
# every supported platform. A method absent here supports all DATABASES.
_METHOD_DB_ALLOW: dict[str, tuple[str, ...]] = {
    "tenant-copy": ("HANA",),
    "hsr": ("HANA",),
}


def db_blocks(methodology: str, db: str) -> str | None:
    """Return a blocking reason if (method, db) is unsupported, else None."""
    allowed = _METHOD_DB_ALLOW.get(methodology)
    if allowed is not None and db.strip().upper() not in allowed:
        pretty_allowed = " / ".join(allowed)
        return (
            f"{MIGRATION_METHODS.get(methodology, methodology)} is a HANA "
            f"database-level procedure — it is only available for {pretty_allowed}."
        )
    return None


class Prompter(Protocol):
    """Minimal I/O surface the wizard needs; real impl wraps Typer/Rich."""

    def choose(self, title: str, options: list[str]) -> int:
        """Show numbered options; return the chosen 0-based index."""
        ...

    def ask(self, prompt: str, default: str | None, secret: bool) -> str:
        """Ask for a free-text value; return the answer (possibly empty)."""
        ...

    def confirm(self, prompt: str, default: bool = False) -> bool:
        """Yes/no confirmation."""
        ...

    def note(self, message: str) -> None:
        """Show an informational line."""
        ...


@dataclass(frozen=True)
class Operation:
    """A discovered operation, normalised for the menu."""

    name: str
    kind: str  # "check" | "action"
    description: str
    methodology: str
    phase: str = "unclassified"  # lifecycle phase key (Phase enum value)
    #: human-readable label for the operation (SAP-transaction-first when known).
    #: Falls back to a title-cased leaf of the dotted name when the class did not
    #: declare a ``title`` — so the UI never has to show a raw machine name.
    title: str = ""

    @property
    def label(self) -> str:
        """The best human label: the declared title, else a prettified name."""
        return self.title or op_label(self.name)


def op_label(name: str) -> str:
    """Turn a dotted machine name into a readable label as a last resort.

    ``hsr.log-mode-normal`` -> ``Log Mode Normal``. Used only when a check/action
    class did not declare an explicit ``title``.
    """
    leaf = name.rsplit(".", 1)[-1]
    return leaf.replace("-", " ").replace("_", " ").title()


def discover_operations(registry: Registry) -> list[Operation]:
    """Flatten the registry into menu operations, sorted by name."""
    ops: list[Operation] = []
    for name, check_cls in registry.checks().items():
        ops.append(
            Operation(
                name, "check", check_cls.description, _methodology(name),
                _phase_of(check_cls, name, "check"),
                getattr(check_cls, "title", "") or "",
            )
        )
    for name, action_cls in registry.actions().items():
        ops.append(
            Operation(
                name, "action", action_cls.description, _methodology(name),
                _phase_of(action_cls, name, "action"),
                getattr(action_cls, "title", "") or "",
            )
        )
    return sorted(ops, key=lambda o: (o.methodology, o.kind != "check", o.name))


# Fallback lifecycle phase for operations whose class does not declare one.
# Keyed by (methodology, kind). An explicit ``phase = Phase.X`` on the class
# always wins; this only fills the gap so the PHASE axis has no noisy "Other"
# bucket. Rationale per family:
#   * readiness checks (backup-restore / export-import / hsr) are PREPARATION —
#     they read source & target before any downtime.
#   * their state-changing actions (restore, SWPM system copy) run in DOWNTIME —
#     that is when the copy is actually created.
#   * PI/PO (AS Java) is post-copy work on the target ⇒ POST (checks + actions).
#   * Solution Manager preconditions are validated up front ⇒ PREPARATION.
_PHASE_FALLBACK: dict[tuple[str, str], str] = {
    ("backup-restore", "check"): "preparation",
    ("backup-restore", "action"): "downtime",
    ("export-import", "check"): "preparation",
    ("export-import", "action"): "downtime",
    ("hsr", "check"): "preparation",
    ("hsr", "action"): "downtime",
    ("pipo", "check"): "post",
    ("pipo", "action"): "post",
    ("solution-manager", "check"): "preparation",
    ("solution-manager", "action"): "preparation",
    ("core", "check"): "preparation",
    ("core", "action"): "preparation",
}


def _phase_of(cls: object, name: str = "", kind: str = "") -> str:
    """Resolve a lifecycle Phase for an operation.

    An explicit ``phase = Phase.X`` attribute on the class always wins. When a
    class declares none, fall back to a (methodology, kind) heuristic so the
    PHASE axis stays fully populated instead of dumping ops into "Other".
    """
    phase = getattr(cls, "phase", None)
    value = getattr(phase, "value", None)
    if value and value != "unclassified":
        return str(value)
    return _PHASE_FALLBACK.get((_methodology(name), kind), "unclassified")


def _methodology(name: str) -> str:
    """First dotted segment is the methodology group (e.g. 'tenant-copy')."""
    return name.split(".", 1)[0] if "." in name else name


def methodologies(ops: list[Operation]) -> list[str]:
    """Distinct methodology groups, in stable order."""
    out: list[str] = []
    for op in ops:
        if op.methodology not in out:
            out.append(op.methodology)
    return out


def families(ops: list[Operation]) -> list[str]:
    """Distinct umbrella families, in stable order.

    A methodology that belongs to a mapped family (see FAMILIES) collapses into
    that family; any unmapped methodology becomes its own family. This gives the
    menu a 3-level shape (family -> method -> operation) without hard-coding the
    method list — new modules slot in via FAMILIES or stand alone automatically.
    """
    present = methodologies(ops)
    out: list[str] = []
    for fam, members in FAMILIES.items():
        if any(m in present for m in members):
            out.append(fam)
    for m in present:
        if _family_of(m) == m and m not in out:
            out.append(m)
    return out


def _family_of(methodology: str) -> str:
    """Return the family a methodology belongs to (itself if unmapped)."""
    for fam, members in FAMILIES.items():
        if methodology in members:
            return fam
    return methodology


def migration_families(ops: list[Operation]) -> list[str]:
    """Only the true migration-method families, for the METHOD axis of the TUI.

    This is ``families()`` minus the cross-cutting methodologies (abap, pipo,
    solution-manager) and internal infra (core). Those are surfaced via the
    PHASE axis, never as a top-level migration type. Keeps the operator's first
    choice honest: "which migration method?" — System Copy or System Transition.
    """
    hidden = set(CROSS_CUTTING) | {"core"}
    return [f for f in families(ops) if f in FAMILIES and f not in hidden]


def methodologies_in_family(ops: list[Operation], family: str) -> list[str]:
    """Ordered methodologies that belong to a family and are actually present."""
    present = methodologies(ops)
    if family in FAMILIES:
        return [m for m in FAMILIES[family] if m in present]
    # Standalone family: the methodology is the family itself.
    return [family] if family in present else []


# --- Stack compatibility (validated against SAP System Copy guides, 2026-07-20)
# The SAP application-server stack constrains which system-copy methods are
# supported. Hard rule from the guides: an AS Java copy cannot be produced from a
# database backup/restore (Java config lives partly on the filesystem + secure
# store), so Java MUST use SWPM export/import (JLoad). ABAP stores everything in
# the DB, so both methods work.
STACKS = ("abap", "java", "dual", "solman")

# Methods NOT supported for a given stack -> human-readable reason.
_STACK_METHOD_BLOCKS: dict[str, dict[str, str]] = {
    "java": {
        "backup-restore": (
            "SAP does not support a Java system copy from a database "
            "backup/restore — AS Java configuration lives partly on the "
            "filesystem and secure store. Use SWPM export/import (JLoad)."
        ),
    },
}


def stack_blocks(stack: str, methodology: str) -> str | None:
    """Return a blocking reason if (stack, methodology) is unsupported, else None.

    Pure lookup so the wizard and any future automation share one source of
    truth for the SAP-mandated stack constraints.
    """
    return _STACK_METHOD_BLOCKS.get(stack.lower().strip(), {}).get(methodology)


def pretty(methodology: str) -> str:
    """Human label for a family / methodology key.

    Honours explicit overrides (SAP-correct casing like "HSR", "PI/PO") and
    otherwise title-cases the dotted key. Keeps the tree/menu labels faithful
    to how SAP names things rather than a naive .title().
    """
    overrides = {
        "system-copy": "System Copy",
        "hsr": "HSR",
        "pipo": "PI/PO (AS Java)",
        "solution-manager": "Solution Manager",
        "abap": "ABAP",
        "backup-restore": "Backup & Restore",
        "export-import": "Export & Import",
        "tenant-copy": "Tenant Copy",
    }
    if methodology in overrides:
        return overrides[methodology]
    return methodology.replace("-", " ").replace("_", " ").title()


def collect_params(
    specs: list[ParamSpec], prompter: Prompter
) -> tuple[dict[str, str], dict[str, str]]:
    """Prompt for each spec. Returns (field_values, param_values).

    Empty answers to optional fields are dropped so defaults resolve naturally.
    Required-but-empty answers are re-prompted once, then kept as-is (the
    operation's own validation will surface a clean error downstream).
    """
    fields: dict[str, str] = {}
    params: dict[str, str] = {}
    for spec in dedupe(specs):
        value = _ask_one(spec, prompter)
        if value == "" and not spec.required:
            continue
        if spec.kind is ParamKind.FIELD or spec.key in _FIELD_KEYS:
            fields[spec.key] = value
        else:
            params[spec.key] = value
    return fields, params


def _ask_one(spec: ParamSpec, prompter: Prompter) -> str:
    if spec.help:
        prompter.note(f"  ⓘ {spec.help}")
    if spec.choices:
        idx = prompter.choose(spec.prompt, list(spec.choices))
        return spec.choices[idx]
    prompt = spec.prompt + (" *" if spec.required else "")
    answer = prompter.ask(prompt, spec.default, spec.secret).strip()
    if answer == "" and spec.required:
        prompter.note("  ⚠️  this field is required")
        answer = prompter.ask(prompt, spec.default, spec.secret).strip()
    return answer


def build_context(
    fields: dict[str, str],
    params: dict[str, str],
    *,
    execute: bool,
    assume_yes: bool,
) -> Context:
    """Assemble a Context from collected field + param values."""
    kwargs: dict[str, object] = {k: v for k, v in fields.items() if v != ""}
    kwargs["params"] = params
    kwargs["dry_run"] = not execute
    kwargs["assume_yes"] = assume_yes
    return Context(**kwargs)  # type: ignore[arg-type]


def spec_for(op: Operation, registry: Registry) -> list[ParamSpec]:
    """Return the declared parameters for an operation instance."""
    cls: type[Check] | type[Action] | None = (
        registry.get_check(op.name)
        if op.kind == "check"
        else registry.get_action(op.name)
    )
    if cls is None:
        return []
    return list(cls().parameters())


def checks_in(ops: list[Operation], methodology: str) -> list[Operation]:
    """All check operations belonging to a methodology, in menu order."""
    return [o for o in ops if o.methodology == methodology and o.kind == "check"]


def runbooks_in(registry: Registry, methodology: str) -> list[tuple[str, str, int]]:
    """Runbooks whose name belongs to a methodology, as (name, description, steps).

    A runbook belongs to the methodology when its dotted name starts with the
    methodology segment (e.g. ``tenant-copy.hana.readiness-source`` ->
    ``tenant-copy``). Sorted by name for a stable menu. This lets the wizard
    offer one-click "run this whole readiness sweep" entries per side/phase.
    """
    out: list[tuple[str, str, int]] = []
    for name, rb_cls in registry.runbooks().items():
        if _methodology(name) == methodology:
            out.append((name, rb_cls.description, len(rb_cls.steps)))
    return sorted(out, key=lambda t: t[0])


def params_for_checks(
    check_ops: list[Operation], registry: Registry
) -> list[ParamSpec]:
    """Union of the parameters declared by several checks (deduped by key).

    Powers the "run all pre-checks" flow: the operator answers the combined
    field set once, and every check in the methodology receives the same
    Context. Checks that declare no parameters contribute nothing.
    """
    merged: list[ParamSpec] = []
    for op in check_ops:
        merged.extend(spec_for(op, registry))
    return dedupe(merged)


# --------------------------------------------------------------------------- #
# Lifecycle PHASE axis
#
# The second axis of the cockpit. Independent of the migration method, every
# copy walks the same four macro-phases (mirroring the official ECS/HEC cutover
# plan, encoded in core.result.Phase): Preparation -> Ramp-Down -> Downtime ->
# Post-Activities. Operations are bucketed into these phases by their declared
# ``phase`` attribute; cross-cutting ABAP/PI/PO/SolMan work lands in the right
# phase automatically instead of being a top-level "migration type".
# --------------------------------------------------------------------------- #

# Ordered lifecycle phases: (phase key, human label). Mirrors core.result.Phase
# order. "unclassified" is intentionally last and only shown when non-empty.
LIFECYCLE_PHASES: list[tuple[str, str]] = [
    ("preparation", "1 · Preparation"),
    ("ramp_down", "2 · Ramp-Down (Source)"),
    ("downtime", "3 · Downtime / Execution"),
    ("post", "4 · Post-Activities (Target)"),
    ("unclassified", "Other"),
]

_PHASE_LABEL = dict(LIFECYCLE_PHASES)
_PHASE_ORDER = {key: i for i, (key, _) in enumerate(LIFECYCLE_PHASES)}


def phase_label(phase_key: str) -> str:
    """Human label for a lifecycle phase key (falls back to a title-cased key)."""
    return _PHASE_LABEL.get(phase_key, phase_key.replace("_", " ").title())


def stack_post_methodology(stack: str) -> str:
    """Which cross-cutting family supplies POST-phase work for a given stack.

    ABAP copies finish with the ABAP post-activities (SM63/BTCTRNS/SU10/SM51);
    an AS Java (PI/PO) copy finishes with the PI/PO post-copy activities
    (secstore, SLD, RFC/JCo, UME). This is how the Stack selection decides which
    post-activities the phase view shows — PI/PO is the Java branch of POST, not
    a migration type of its own.
    """
    return "pipo" if stack.strip().lower() == "java" else "abap"


def operations_for_context(
    ops: list[Operation],
    *,
    methodology: str,
    stack: str = "abap",
) -> list[Operation]:
    """Resolve the operations that make up a copy for a (method, stack) context.

    Combines the chosen migration METHOD's own operations with the cross-cutting
    lifecycle work that applies:

      * the method's operations (e.g. all ``tenant-copy.*``),
      * ABAP ramp-down + readiness (source quiesce / preparation), always,
      * the stack-specific POST-activities (ABAP vs PI/PO), and
      * Solution Manager preconditions when present (LMDB/SLD re-register).

    The result is everything the phase view needs to lay a copy out along the
    four lifecycle phases for that context.
    """
    post_family = stack_post_methodology(stack)
    keep = {methodology, "abap", "solution-manager", post_family}
    # PI/PO only belongs when the stack is Java; ABAP post always available but
    # its POST ops are only meaningful for ABAP/dual stacks.
    picked = [o for o in ops if o.methodology in keep]
    # Drop the non-selected post family's POST-phase ops to avoid mixing
    # ABAP and PI/PO post-activities in the same plan.
    other_post = "abap" if post_family == "pipo" else "pipo"
    return [
        o
        for o in picked
        if not (o.methodology == other_post and o.phase == "post")
    ]


def group_by_phase(ops: list[Operation]) -> list[tuple[str, str, list[Operation]]]:
    """Bucket operations into lifecycle phases, ordered, non-empty phases only.

    Returns a list of (phase_key, phase_label, operations) in cutover order.
    Within a phase, checks come before actions, then by name — the same order
    the report and menu use.
    """
    buckets: dict[str, list[Operation]] = {}
    for op in ops:
        buckets.setdefault(op.phase, []).append(op)
    out: list[tuple[str, str, list[Operation]]] = []
    for key, label in LIFECYCLE_PHASES:
        group = buckets.get(key)
        if not group:
            continue
        group.sort(key=lambda o: (o.kind != "check", o.name))
        out.append((key, label, group))
    return out

