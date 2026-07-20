# SAP Migration Toolkit

> _Codename: Exodia_ — Stateless executor for SAP migration operations — checks & actions for HANA/ASE
> backup-restore, tenant copy, HANA System Replication (HSR), and Java (AS Java) system copy.

SAP Migration Toolkit is a lightweight, plugable command-line tool that automates the repetitive,
error-prone parts of SAP system migrations. It runs on any Linux server, needs no
database of its own, and never phones home. Think of it as `ansible --check` meets
a SAP Basis runbook: it validates prerequisites, then executes migration steps with
dry-run, confirmation, verification, and documented rollback.

## Why

SAP migrations (backup/restore, tenant copy, HSR setup, Java system copy) are
largely manual today — consultants babysit `sapinst` screens for hours and run
prerequisite checks by hand. Exodia turns that into repeatable, monitored, auditable
automation while keeping the human in control for the decisions that matter.

## Principles

- **Stateless** — runs and exits, no memory or embedded knowledge base for planning.
- **Two categories, one safety model:**
  - **Checks** are read-only. Safe to run anywhere, any time.
  - **Actions** change state. They are guarded: pre-checks → dry-run (default) →
    explicit confirmation → execute → verify → documented rollback.
- **Safe by construction** — commands are argument lists, never `shell=True`.
  Secrets are never logged. SSH uses host-key verification.
- **Plugable** — drop a module under `exodia/modules/` and it is auto-discovered.
- **Self-sufficient** — an embedded troubleshooting KB maps known errors to a cause,
  a generic fix, and the relevant **SAP Note number** (we reference notes, never
  reproduce their copyrighted text).
- **Defaults + escape hatch** — sensible opinionated defaults for the 80% standard
  path, plus config/hooks to override anything for the 20% special cases.

## Install

```bash
pip install exodia            # once published to PyPI
# or, from source:
pip install -e ".[tui]"
```

## Usage

```bash
exodia list                              # show all discovered checks & actions
exodia run core.free-space --config my.yaml
exodia run backup-restore.prepare --db-type hana --source PRD --target QAS
exodia run backup-restore.restore-db --db-type hana --execute --yes
exodia doctor                            # self-check
```

Dry-run is the default for actions. Pass `--execute --yes` to actually run.
Exit codes are automation-friendly: `0` = nothing blocking, `1` = a blocking failure.

## Status

Alpha. Core is stable; methodology modules (backup/restore for HANA & ASE, and
Java (AS Java) system copy) are under active development. See the Linear project for the
roadmap.

## Supported scenarios (target)

| Methodology | Databases | Notes |
|---|---|---|
| Backup / Restore | HANA, SAP ASE | via native tools + SWPM system copy |
| Tenant Copy | HANA | TLS/SSL, SYSTEMDB cert handling |
| HANA System Replication | HANA | create / finalize / enable replica |
| Java (AS Java) system copy | HANA | SLD, SECSTORE, RFC, UME post-copy (PI/PO validated first) |

## License

MIT © Tiago Madeira
