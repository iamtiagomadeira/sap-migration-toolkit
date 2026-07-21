# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **SAP MIG ABAP readiness — Phase 2 (Tier A).** Expanded the read-only ABAP
  check set, all tagged with cutover phase + human-readable title + facts:
  new checks — `source-profiles` / `target-profiles` (capture DEFAULT.PFL +
  instance profiles per side), `system-change-option` (SE06),
  `installation-consistency` (SICK/SM28) and `batch-input-sessions` (SM35) —
  plus the existing checks (SM12, SM13/SMQ, SM37, SM04, SP01, ST22, STMS/SE01,
  SM51, SCC4/T000, SM59, CVERS, RFC_SYSTEM_INFO) now carrying phase/title/facts.
  New runbook `abap.pre-migration-checks` bundles them in cutover order
  (Preparation → Ramp-Down → Post-Activities).
- **Profile backup action** (`abap.profile-backup`) — guarded action that backs
  up the SAP profiles (`/sapmnt/<SID>/profile`) and, in `global` scope, the
  fundamental global directories (`/sapmnt/<SID>/global`) to a chosen location
  over SSH (dry-run → confirm → execute → verify). Typical flow: back up the
  source profiles, then back up the target profiles + global folder.
- **Phase-grouped, human-readable evidence report.** Results now carry a cutover
  ``phase`` (Preparation / Ramp-Down / Downtime / Post-Activities — mirroring the
  ECS/HEC Cutover Plan), an explicit action-oriented ``title`` (e.g. "SM12 —
  Lock Entries Check"), and labelled ``facts`` (e.g. "Source HANA Version:
  2.00.067"). The HTML report groups checks under phase headings with per-phase
  status, shows the human title plus the measured findings as chips, and leads
  with a colour-coded verdict banner. All eleven tenant-copy checks are tagged.
- **CSV export** (`exodia report --format csv`) — one row per check with Phase,
  Check, Title, Status, Duration, Summary, Findings and SAP Note columns; opens
  natively in Excel / Google Sheets, no spreadsheet dependency.
- **Target tenant discovery + selection.** For a HANA tenant copy the wizard
  queries the target SYSTEMDB (M_DATABASES) for the tenants that actually exist
  and: assumes the only one (asking to confirm), or offers a dropdown when there
  are several (showing SID + status), or notes that a new tenant will be created.
  The operator confirms the exact database that will receive the source data,
  and can correct the source/target names if the previewed command is wrong.
- **Portable snapshots + cross-side compare** (`exodia snapshot` /
  `exodia compare`) for air-gapped migrations. Capture one side (source or
  target) into a signed, tamper-evident JSON file carrying every check's
  measured facts plus a chain-of-custody header; carry it across the network
  boundary; then diff it against the other side (live or a second snapshot).
  Produces a check-by-check source-vs-target table with an aligned/diverge
  verdict — the consultant's manual runbook, automated. SHA-256 self-hash is
  verified before any comparison. Exit `0` aligned / `1` diverge.
- **Runbooks** — an ordered, named bundle of read-only checks that produces a
  single aggregate readiness verdict and a sealed evidence bundle. Discovered
  automatically like checks/actions. New commands `exodia runbook <name>` and
  `exodia runbooks`. Ships with `tenant-copy.hana.readiness` (the 11 HANA
  cross-host prerequisites) and `abap.cutover-readiness` (12 read-only SAP MIG
  ramp-down/parity checks). Runbooks re-read the live system every run — no
  cached "done" state — so they are idempotent and safe to re-run.
- **Reinforced tenant-copy verify** — post-copy, `tenant-copy.hana.copy-tenant`
  now optionally compares object + record counts source vs target (via dedicated
  tenant hdbuserstore keys): table count must match exactly and total record
  count must be within a configurable tolerance (default 1%). Upgrades "the
  tenant is online" to "online AND the data came across". Falls back to the
  online-only verdict when tenant keys are not provided.
- HANA tenant-copy operator guide (`docs/tenant-copy.md`) and a ready-to-fill
  config template (`examples/tenant-copy.yaml`) covering the full
  readiness → plan → execute → verify workflow with password-free hdbuserstore
  auth.
- `exodia report [BUNDLE]` renders an evidence bundle as a standalone,
  shareable HTML document plus its Markdown summary; defaults to the most
  recent bundle and writes outside the sealed directory so the tamper-evident
  manifest stays intact.
- Evidence-by-default: every run writes a self-contained, tamper-evident audit
  bundle (`manifest.json` with per-artifact SHA-256, append-only `run.jsonl`,
  `results.json`, `report.md`, harvested `artifacts/`). `exodia evidence verify`
  re-hashes a bundle to prove it was not altered; `exodia evidence attach` adds
  external logs and re-seals.
- System Copy methods **export/import** (SWPM — R3load for ABAP, JLoad for
  Java), **HANA System Replication (HSR)**, and a standalone **Solution
  Manager** post-copy module, each with real read-only pre-checks.
- Interactive `exodia menu`: pick family → method + stack → operation, with a
  stack-compatibility gate that blocks unsupported combinations (e.g. Java +
  backup/restore, which SAP does not support).
- `exodia doctor` self-check and a "Run ALL" option with a clear go/no-go
  verdict.
- CI test-coverage floor of 75% and a `.pre-commit-config.yaml` mirroring the
  CI gates (ruff, ruff-format, mypy).
- Automated PyPI release workflow via Trusted Publishing (OIDC), triggered by
  `v*` tags.

## [0.1.0] - 2026-07-20

### Added

- Initial public release of the SAP Migration Toolkit (codename Exodia): a
  stateless executor for SAP migration operations with auto-discovered
  methodology modules, guarded actions (dry-run → confirm → execute → verify →
  rollback), a YAML-backed error/remediation knowledge base, and pre-checks for
  HANA/ASE backup-restore and HANA tenant copy.

[Unreleased]: https://github.com/iamtiagomadeira/sap-migration-toolkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/iamtiagomadeira/sap-migration-toolkit/releases/tag/v0.1.0
