# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Tenant copy — COP-derived checks & actions (from the real dry-run runbook).**
  - Checks: `source-ports` / `target-ports` (HANA service ports from
    `M_SERVICES`, incl. the SQL/replication port) and `source-replication-
    parameters` / `target-replication-parameters` (the exact HSR/SSL/persistence
    `global.ini` keys from `M_INIFILE_CONTENTS`). Wired into the side-scoped
    readiness runbooks.
  - `configure-hsr-parameters`: applies the SAP best-practice system-replication
    tuning + SSL parameter set via `ALTER SYSTEM ALTER CONFIGURATION ... WITH
    RECONFIGURE`, with an operator-selected **SSL on/off** mode (the two exact
    parameter sets from the runbook) and a restart-required flag.
  - `restart-hana`: guarded `HDB stop` + `HDB start` for when the parameter
    changes need a DB restart.
  - **Dry-Run / Mock-Run isolation** actions (`mock-isolate-users`,
    `mock-isolate-rfcs`, `mock-stop-jobs`): the COP "Isolate System (Mock-Run
    Only)" section — lock USR02 users (sparing DDIC + keep-list), neutralise
    RFCDES destinations, stop TBTCO jobs — each backing up the table first and
    with a real restore-from-backup rollback. Downtime phase, dry-run gated.
- **`exodia cutover-plan` + config templates — the day-of starter kit.** A new
  read-only command prints the whole SAP MIG cutover playbook (the four phases,
  in order, with every exact command and the safety gates ⛔/✋ flagged) as a
  terminal reference card. Ships with fill-in-the-blanks config templates
  `examples/abap-ramp-down.yaml` and `examples/abap-post-activities.yaml`
  (alongside the existing `tenant-copy.yaml`), so an admin can go from clone to
  running the cutover without guessing any parameter.
- **Full cutover lifecycle — ramp-down + post-activities actions.** Completed the
  four-phase ABAP cutover as guarded actions:
  - Ramp-Down: `suspend-jobs` (BTCTRNS1), `adapt-operation-modes` (SM63),
    `lock-users` (SU10/BAPI_USER_LOCK, always sparing DDIC/SAP*/TMSADM/...),
    `stop-app-servers` (sapcontrol, customer-confirmation gated), and the manual
    `inform-customer` attestation.
  - Post-Activities: `start-app-servers` (sapcontrol StartSystem), `resume-jobs`
    (BTCTRNS2), `unlock-users` (BAPI_USER_UNLOCK), `validate-online` (SM51).
  New `docs/cutover.md` walks the four phases (Preparation → Ramp-Down →
  Downtime → Post-Activities) end to end with the exact commands and gates.
- **Ramp-down actions (SAP MIG).** Guarded state-changing steps to quiesce the
  source before takeover: `abap.rampdown.suspend-jobs` (BTCTRNS1 — suspend the
  background scheduler), `abap.rampdown.adapt-operation-modes` (SM63),
  `abap.rampdown.stop-app-servers` (stop ALL application servers via sapcontrol),
  and `abap.rampdown.inform-customer` (manual attestation). Two new safety
  primitives on the Action base support them:
  - **customer-confirmation gate** (`requires_customer_confirmation`): stopping
    the customer's application servers will not run until the customer has
    explicitly signed off (`customer_confirmed=true`) — on top of the usual
    execute gate. Without it the step SKIPs and sapcontrol is never invoked.
  - **manual attestation** (`manual` + `attested`): the "inform customer that
    ramp-down is complete" step performs no system action; the admin emails the
    customer and records that they did it, so the cutover evidence stays complete.
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
