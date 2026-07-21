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

# Umbrella families group related methodologies under the official SAP term.
# SAP calls the top-level procedure "System Copy"; backup/restore, export/import,
# HSR and tenant copy are *methods* within it (validated against SAP guides,
# 2026-07-20). Maps a family key -> ordered methodology names (first dotted name
# segment). A methodology not listed here is its own standalone family so any
# future module keeps appearing with zero menu wiring (same philosophy as the
# auto-discovery registry).
FAMILIES: dict[str, list[str]] = {
    "system-copy": ["backup-restore", "export-import", "hsr", "tenant-copy"],
}


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


def discover_operations(registry: Registry) -> list[Operation]:
    """Flatten the registry into menu operations, sorted by name."""
    ops: list[Operation] = []
    for name, check_cls in registry.checks().items():
        ops.append(Operation(name, "check", check_cls.description, _methodology(name)))
    for name, action_cls in registry.actions().items():
        ops.append(Operation(name, "action", action_cls.description, _methodology(name)))
    return sorted(ops, key=lambda o: (o.methodology, o.kind != "check", o.name))


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

