"""Tests for the interactive menu wizard logic (Prompter-abstracted, no real I/O).

A scripted FakePrompter replays canned answers so the whole wizard flow is
unit-testable: methodology grouping, parameter collection (fields vs params,
required re-prompt, choices, defaults), and Context assembly.
"""

from __future__ import annotations

from exodia.core.context import Context
from exodia.core.menu import (
    build_context,
    checks_in,
    collect_params,
    discover_operations,
    families,
    methodologies,
    methodologies_in_family,
    params_for_checks,
    runbooks_in,
    spec_for,
    stack_blocks,
)
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.registry import registry


class FakePrompter:
    """Replays scripted answers; records what it was asked."""

    def __init__(
        self,
        choices: list[int] | None = None,
        answers: list[str] | None = None,
        confirms: list[bool] | None = None,
    ) -> None:
        self._choices = list(choices or [])
        self._answers = list(answers or [])
        self._confirms = list(confirms or [])
        self.notes: list[str] = []

    def choose(self, title: str, options: list[str]) -> int:
        return self._choices.pop(0)

    def ask(self, prompt: str, default: str | None, secret: bool) -> str:
        if self._answers:
            return self._answers.pop(0)
        return default or ""

    def confirm(self, prompt: str, default: bool = False) -> bool:
        return self._confirms.pop(0) if self._confirms else default

    def note(self, message: str) -> None:
        self.notes.append(message)


# --------------------------------------------------------------------------- #
# Discovery + grouping
# --------------------------------------------------------------------------- #


def test_discover_operations_includes_tenant_copy() -> None:
    ops = discover_operations(registry)
    names = {o.name for o in ops}
    assert "tenant-copy.hana.copy-tenant" in names
    assert "tenant-copy.hana.source-tenant-online" in names


def test_methodologies_grouping() -> None:
    ops = discover_operations(registry)
    groups = methodologies(ops)
    assert "tenant-copy" in groups
    assert "backup-restore" in groups


def test_checks_sort_before_actions_in_group() -> None:
    ops = discover_operations(registry)
    tc = [o for o in ops if o.methodology == "tenant-copy"]
    kinds = [o.kind for o in tc]
    # all checks come before any action within a methodology
    assert kinds.index("action") > max(
        (i for i, k in enumerate(kinds) if k == "check"), default=-1
    )


# --------------------------------------------------------------------------- #
# Parameter collection
# --------------------------------------------------------------------------- #


def test_collect_params_routes_fields_vs_params() -> None:
    specs = [
        ParamSpec("source", "Source", kind=ParamKind.FIELD, required=True),
        ParamSpec("target", "Target", kind=ParamKind.FIELD, required=True),
        ParamSpec("copy_method", "Method", default="replication"),
        ParamSpec("source_host", "Host"),
    ]
    prompter = FakePrompter(answers=["PRD", "QAS", "replication", "customer-hana"])
    fields, params = collect_params(specs, prompter)
    assert fields == {"source": "PRD", "target": "QAS"}
    assert params == {"copy_method": "replication", "source_host": "customer-hana"}


def test_collect_params_drops_empty_optional() -> None:
    specs = [
        ParamSpec("source_host", "Host"),  # optional, answered empty
        ParamSpec("copy_method", "Method", default="replication"),
    ]
    prompter = FakePrompter(answers=["", "replication"])
    fields, params = collect_params(specs, prompter)
    assert "source_host" not in params
    assert params["copy_method"] == "replication"


def test_collect_params_choices_use_choose() -> None:
    specs = [ParamSpec("copy_method", "Method", choices=("replication", "backup"))]
    prompter = FakePrompter(choices=[1])  # pick "backup"
    _, params = collect_params(specs, prompter)
    assert params["copy_method"] == "backup"


def test_collect_params_required_reprompt() -> None:
    specs = [ParamSpec("source", "Source", kind=ParamKind.FIELD, required=True)]
    # first answer empty -> re-prompt -> "PRD"
    prompter = FakePrompter(answers=["", "PRD"])
    fields, _ = collect_params(specs, prompter)
    assert fields["source"] == "PRD"
    assert any("required" in n for n in prompter.notes)


# --------------------------------------------------------------------------- #
# Context assembly
# --------------------------------------------------------------------------- #


def test_build_context_dry_run_default() -> None:
    ctx = build_context(
        {"source": "PRD", "target": "QAS"},
        {"copy_method": "replication"},
        execute=False,
        assume_yes=False,
    )
    assert isinstance(ctx, Context)
    assert ctx.dry_run is True
    assert ctx.source == "PRD"
    assert ctx.get("copy_method") == "replication"


def test_build_context_execute_sets_flags() -> None:
    ctx = build_context({}, {}, execute=True, assume_yes=True)
    assert ctx.dry_run is False
    assert ctx.assume_yes is True


# --------------------------------------------------------------------------- #
# spec_for: operations expose their declared parameters
# --------------------------------------------------------------------------- #


def test_spec_for_action_returns_declared_params() -> None:
    ops = discover_operations(registry)
    copy = next(o for o in ops if o.name == "tenant-copy.hana.copy-tenant")
    specs = spec_for(copy, registry)
    keys = {s.key for s in specs}
    assert {"source", "target", "copy_method", "source_host"} <= keys


def test_spec_for_undeclared_check_is_empty_but_safe() -> None:
    ops = discover_operations(registry)
    # any check without an override returns [] and must not raise
    for o in ops:
        if o.kind == "check":
            assert isinstance(spec_for(o, registry), list)


# --------------------------------------------------------------------------- #
# Run-all-pre-checks flow
# --------------------------------------------------------------------------- #


def test_checks_in_returns_only_checks_of_methodology() -> None:
    ops = discover_operations(registry)
    tc = checks_in(ops, "tenant-copy")
    assert len(tc) >= 1
    assert all(o.kind == "check" and o.methodology == "tenant-copy" for o in tc)
    # actions must be excluded
    assert all("copy-tenant" not in o.name for o in tc)


def test_params_for_checks_unions_and_dedupes() -> None:
    ops = discover_operations(registry)
    tc_checks = checks_in(ops, "tenant-copy")
    specs = params_for_checks(tc_checks, registry)
    keys = [s.key for s in specs]
    # deduped: no key appears twice even though many checks share host/user
    assert len(keys) == len(set(keys))


def test_params_for_checks_empty_when_no_checks() -> None:
    assert params_for_checks([], registry) == []


# --------------------------------------------------------------------------- #
# Runbooks surfaced per methodology in the menu
# --------------------------------------------------------------------------- #


def test_runbooks_in_returns_methodology_sweeps() -> None:
    rbs = runbooks_in(registry, "tenant-copy")
    names = {name for name, _desc, _steps in rbs}
    # the side-scoped readiness sweeps must be offered for tenant-copy
    assert "tenant-copy.hana.readiness-source" in names
    assert "tenant-copy.hana.readiness-target" in names
    # each entry carries a positive step count
    assert all(steps > 0 for _n, _d, steps in rbs)


def test_runbooks_in_excludes_other_methodologies() -> None:
    rbs = runbooks_in(registry, "tenant-copy")
    names = {name for name, _desc, _steps in rbs}
    # abap runbooks must not leak into tenant-copy
    assert not any(n.startswith("abap.") for n in names)


def test_runbooks_in_empty_for_methodology_without_runbooks() -> None:
    assert runbooks_in(registry, "pipo") == []


# --------------------------------------------------------------------------- #
# Umbrella families (System Copy groups its methods) + stack gating
# --------------------------------------------------------------------------- #


def test_families_groups_system_copy_methods() -> None:
    ops = discover_operations(registry)
    fams = families(ops)
    # backup-restore and tenant-copy collapse into the "system-copy" family
    assert "system-copy" in fams
    assert "backup-restore" not in fams
    assert "tenant-copy" not in fams


def test_families_keeps_unmapped_methodology_standalone() -> None:
    ops = discover_operations(registry)
    fams = families(ops)
    # pipo is not part of any umbrella -> stands on its own
    assert "pipo" in fams


def test_methodologies_in_family_returns_present_members_in_order() -> None:
    ops = discover_operations(registry)
    members = methodologies_in_family(ops, "system-copy")
    # only methods actually discovered appear; order follows FAMILIES definition
    assert "backup-restore" in members
    assert "tenant-copy" in members
    assert members.index("backup-restore") < members.index("tenant-copy")


def test_methodologies_in_family_standalone() -> None:
    ops = discover_operations(registry)
    assert methodologies_in_family(ops, "pipo") == ["pipo"]


def test_stack_blocks_java_backup_restore() -> None:
    # SAP-mandated: Java cannot be copied via database backup/restore
    reason = stack_blocks("java", "backup-restore")
    assert reason is not None
    assert "JLoad" in reason or "export/import" in reason


def test_stack_blocks_allows_supported_combos() -> None:
    # ABAP + backup/restore is fine; Java + tenant-copy has no block rule
    assert stack_blocks("abap", "backup-restore") is None
    assert stack_blocks("java", "tenant-copy") is None
    assert stack_blocks("dual", "export-import") is None
