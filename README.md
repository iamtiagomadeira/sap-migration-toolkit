# SAP Migration Toolkit

<p>
  <a href="https://github.com/iamtiagomadeira/sap-migration-toolkit/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/iamtiagomadeira/sap-migration-toolkit/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="https://github.com/iamtiagomadeira/sap-migration-toolkit/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/iamtiagomadeira/sap-migration-toolkit/actions/workflows/codeql.yml/badge.svg" /></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue" />
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green" /></a>
  <a href="CONTRIBUTING.md"><img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen" /></a>
</p>

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

## Example: a guarded HANA restore

A typical system-copy cutover, showing the safety model end to end:

```bash
# 1. Read-only pre-checks — safe to run any time, changes nothing
exodia run backup-restore.prepare --db-type hana --source PRD --target QAS
#   ✓ target disk space sufficient   ✓ backup catalog reachable
#   ✓ target SID stopped             ✗ log_mode = normal (expected: overwrite)
#   → exit 1: one blocking issue, nothing was changed

# 2. Fix the flagged item, then preview the real action (dry-run is default)
exodia run backup-restore.restore-db --db-type hana --source PRD --target QAS
#   [DRY-RUN] would run: HDBSettings.sh recoverSys.py --command="RECOVER DATABASE ..."
#   [DRY-RUN] would verify: SYSTEMDB + tenant reach state 'OK'

# 3. Execute for real — explicit opt-in required
exodia run backup-restore.restore-db --db-type hana --source PRD --target QAS --execute --yes
#   → pre-checks → execute → verify → on failure, documented rollback steps
```

Every action follows the same path: **pre-checks → dry-run → confirm → execute →
verify → rollback**. You never touch a destructive step without seeing it first.

## Status

Alpha. The core execution engine is stable. Methodology modules — backup/restore
for SAP HANA & SAP ASE, tenant copy, HANA System Replication, and Java (AS Java)
system copy — are under active development. See the
[Supported scenarios](#supported-scenarios-target) table below for what's covered
and what's planned.

## Supported scenarios (target)

| Methodology | Databases | Notes |
|---|---|---|
| Backup / Restore | HANA, SAP ASE | via native tools + SWPM system copy |
| Tenant Copy | HANA | TLS/SSL, SYSTEMDB cert handling |
| HANA System Replication | HANA | create / finalize / enable replica |
| Java (AS Java) system copy | HANA | SLD, SECSTORE, RFC, UME post-copy (PI/PO validated first) |

## Contributing

Contributions are welcome — new methodology modules, checks, and SAP Note mappings
especially. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started, and please report
security issues privately per our [security policy](SECURITY.md).

## License

MIT © Tiago Madeira

## Star History

<a href="https://www.star-history.com/#iamtiagomadeira/sap-migration-toolkit&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=iamtiagomadeira/sap-migration-toolkit&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=iamtiagomadeira/sap-migration-toolkit&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=iamtiagomadeira/sap-migration-toolkit&type=Date" />
 </picture>
</a>
