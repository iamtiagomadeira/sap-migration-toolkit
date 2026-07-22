# Core Concepts

Exodia has a small, deliberate vocabulary. Five ideas explain the whole tool.

## 1. Check — a read-only validation

A **check** answers one question about a live system and never mutates it:
*"is the source tenant online?"*, *"does the target have enough disk?"*,
*"do the HANA revisions match?"*. It runs, reads, and returns a structured
**result** with a status, a human summary, and labelled **facts**.

```
✅ PASS   ⚠️ WARN   ❌ FAIL   ⏭️ SKIP   💥 ERROR
```

Checks are safe to run anywhere, any time — nothing changes. A check can be
**blocking** (a FAIL stops a guarded action from proceeding) or informational.

## 2. Action — a guarded state change

An **action** changes something (creates a replica, stops app servers, applies
parameters). Every action runs the same safe-execution flow:

```
pre-checks → dry-run (default) → confirm → execute → verify → rollback (documented)
```

- **Dry-run is the default.** It shows the exact command(s) and touches nothing.
- **Execute requires explicit opt-in** (`--execute --yes`).
- Some actions add gates: a **customer-confirmation gate** (stopping the
  customer's app servers won't run until `customer_confirmed=true`), a
  **typed-name confirmation** (type the target tenant to proceed), or a
  **manual attestation** (Exodia performs nothing; you record that you did an
  off-system step, e.g. emailed the customer).

Commands are always argument lists (`list[str]`) — **never** `shell=True`. HANA
auth goes through the secure store (`hdbsql -U <key>`), so no secret ever
reaches a command line or a log.

## 3. Runbook — an ordered sweep with one verdict

A **runbook** bundles a set of read-only checks into one ordered run and rolls
them up into a single aggregate **verdict**. It re-reads the live system every
time (no cached state), so it's idempotent and safe to re-run.

```bash
exodia runbook tenant-copy.hana.readiness-target --config target.yaml
```

Runbooks map to the **four cutover phases**, so the report groups results the
way a migration team reasons about the day:

| Phase | What happens | Downtime? |
|---|---|---|
| **Preparation** | read-only readiness + parity on source & target | no |
| **Ramp-Down** | quiesce the source (drain queues, lock users, stop servers) | starts |
| **Downtime** | the replica is created and synced | yes |
| **Post-Activities** | re-open + validate the target | ends |

## 4. Evidence — a sealed, tamper-evident audit trail

Every run — check, action, or runbook — writes an **evidence bundle**:

```
evidence/<methodology>/<SID>/<UTC-timestamp>/
    manifest.json   chain-of-custody + SHA-256 of every artifact
    run.jsonl       append-only event log
    results.json    the structured results
    report.md       human-readable report
```

- **Tamper-evident:** `exodia evidence verify <dir>` re-hashes every artifact
  and proves nothing was altered after the fact.
- **Shareable:** `exodia report --format html` produces a phase-grouped document
  with a colour-coded verdict banner; `--format csv` opens in Excel.
- **Searchable:** JSONL/JSON, not screenshots.

This replaces the manual "paste screenshots into a handover doc" step with an
audit trail generated as a by-product of doing the work.

## 5. Snapshot & Compare — the air-gapped model

In a real ECS/HEC engagement the source (customer) and target (HEC) sit in
**isolated networks** — one host rarely reaches both. Exodia automates the
consultant's manual "read the source, log on to the target, compare against my
runbook" loop with two commands:

```bash
# in the customer network — capture a signed snapshot of one side:
exodia snapshot tenant-copy.hana.readiness-source --side source --config source.yaml -o source.json

# carry source.json across the air gap, then in the HEC network — diff it live:
exodia compare source.json --against tenant-copy.hana.readiness-target --side target --config target.yaml
```

A **snapshot** is a self-contained JSON file with every check's measured facts
and a **SHA-256 self-hash**. `compare` verifies that hash first (rejecting a file
altered in transit), then produces a check-by-check **source-vs-target diff**
with an aligned / diverge verdict. It carries no secrets — only measured facts.

---

## How they fit together

```
   Checks  ──grouped into──▶  Runbooks  ──run──▶  Verdict + Evidence bundle
     │                                                      │
     └── Actions (guarded) ── each phase ──────────────────┘
                                                            │
   Snapshot (one side) ──carry──▶ Compare (other side) ──▶ diff + Evidence
```

Everything is **auto-discovered**: add a check or action class under
`exodia/modules/` and it appears in `exodia menu`, `exodia list`, and any
runbook that references it — no central registration. See
[Authoring a Module](authoring-a-module.md).
