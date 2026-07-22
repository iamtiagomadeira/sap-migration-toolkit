---
hide:
  - navigation
  - toc
---

# SAP Migration Toolkit

<p style="font-size:1.15rem; color:var(--md-default-fg-color--light); max-width:46rem;">
<em>Codename: Exodia.</em> A stateless, plugable CLI that automates the
repetitive, error-prone parts of <strong>SAP system migrations</strong> — HANA
tenant copy, backup/restore, HSR, and the ABAP cutover — with dry-run,
confirmation, verification, documented rollback, and a sealed audit trail for
every run.
</p>

[Get started :material-arrow-right:](getting-started.md){ .md-button .md-button--primary }
[Core concepts](concepts.md){ .md-button }
[View on GitHub](https://github.com/iamtiagomadeira/sap-migration-toolkit){ .md-button }

---

## Think `ansible --check` meets a SAP Basis runbook

An SAP system copy today is largely manual: a consultant babysits `sapinst`,
runs prerequisite checks by hand across a dozen transactions and two SYSTEMDBs,
and pastes screenshots into a handover. Exodia turns that runbook into
**repeatable, monitored, auditable automation** — while keeping the human in
control for the decisions that matter.

<div class="grid cards" markdown>

-   :material-shield-check:{ .lg .middle } **Safe by construction**

    ---

    Checks are read-only. Actions are guarded: pre-checks → dry-run → confirm →
    execute → verify → rollback. Commands are argument lists, never `shell=True`.
    Secrets never touch a command line.

-   :material-clipboard-check-outline:{ .lg .middle } **Evidence by default**

    ---

    Every run seals a tamper-evident bundle (SHA-256 manifest, event log) and a
    phase-grouped HTML/CSV report — the audit trail, generated automatically.

-   :material-swap-horizontal:{ .lg .middle } **Air-gapped ready**

    ---

    Source and target in isolated networks? Capture one side into a signed
    snapshot, carry it across, and diff it against the other — the manual
    "read here, compare there" loop, automated.

-   :material-puzzle-outline:{ .lg .middle } **Plugable**

    ---

    Drop a module in and it is auto-discovered in the menu, `list`, and
    runbooks. No central wiring. 91 checks, 25 actions, 7 runbooks today.

</div>

---

## Where to go next

<div class="grid cards" markdown>

-   **:material-rocket-launch: [Getting Started](getting-started.md)**

    Install, run your first read-only sweep, and understand the config model.

-   **:material-book-open-variant: [Core Concepts](concepts.md)**

    Checks, actions, runbooks, evidence, and the snapshot/compare model.

-   **:material-database-arrow-right: [HANA Tenant Copy](tenant-copy.md)**

    The operator guide: readiness → plan → execute → verify, air-gapped.

-   **:material-sitemap: [Tenant Copy — Full Coverage](tenant-copy-coverage.md)**

    Every check and action, phase by phase, with the flow diagram.

-   **:material-timeline-clock: [SAP MIG Cutover](cutover.md)**

    The four-phase ABAP cutover playbook, end to end.

-   **:material-code-braces: [Authoring a Module](authoring-a-module.md)**

    Add your own check or action — it's a small class.

</div>

---

!!! note "Independent open-source project"
    Exodia references SAP Note *numbers* for remediation and never reproduces
    their copyrighted text. SAP, HANA, and related marks are trademarks of SAP
    SE. This is an independent, unofficial project — not affiliated with or
    endorsed by SAP.
