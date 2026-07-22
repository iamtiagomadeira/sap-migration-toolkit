# Getting Started

Exodia is a Python 3.11+ CLI. It runs on any Linux box that can reach the SAP
systems you're migrating (directly, or one side at a time over an air gap).

## Install

```bash
git clone https://github.com/iamtiagomadeira/sap-migration-toolkit.git
cd sap-migration-toolkit
python3 -m venv .venv && .venv/bin/pip install -e .
```

Verify the install and see what was discovered:

```bash
exodia doctor
#   exodia 0.1.0
#   discovered checks : 91
#   discovered actions: 25
#   discovered runbooks: 7
#   ✅ core healthy
```

## Your first run — a read-only sweep

Everything read-only is safe to run any time; it changes nothing. Start with the
interactive wizard — it discovers your hdbuserstore keys and asks only the
fields the operation needs:

```bash
exodia menu
#   → System Copy → Tenant Copy → ABAP
#   → pick "readiness-source (7 checks)"  (or readiness-target on the HEC side)
```

Or run it directly:

```bash
exodia runbook tenant-copy.hana.readiness-source --config source.yaml
```

You'll get a **phase-grouped table**, a single **verdict** (READY / NOT READY /
inconclusive), and a sealed **evidence bundle** on disk. Exit code is `0` when
nothing blocks, `1` when there's a blocker — so it drops straight into CI.

## The config model

You never *have* to hand-write YAML — the wizard prompts interactively. But for
repeatable runs, a small config file is convenient. **No passwords live in it**:
HANA authenticates through the secure user store, so the file only names the
*keys*.

```yaml
# source.yaml
db_type: hana
source: PRD                       # the tenant to copy FROM
params:
  source_userstore_key: SRCSYS    # created once with: hdbuserstore SET ...
```

```yaml
# target.yaml
db_type: hana
source: PRD
target: QAS                       # the tenant to create on the target
params:
  target_userstore_key: TGTSYS
  source_host: customer-hana.example.com   # host the target reaches for the copy
  source_instance: "00"
```

Unknown keys are **rejected** (a typo fails loudly at load time). Ready-to-fill
templates live in [`examples/`](https://github.com/iamtiagomadeira/sap-migration-toolkit/tree/main/examples).

### One-time prerequisite: hdbuserstore keys

HANA authentication uses the secure user store — the password is entered once,
into the store, and never appears in a config file, a command line, or a log:

```bash
hdbuserstore SET SRCSYS  <src_host>:3<nn>13  SYSTEM  <pwd>   # source SYSTEMDB
hdbuserstore SET TGTSYS  <tgt_host>:3<mm>13  SYSTEM  <pwd>   # target SYSTEMDB
```

On a real migration these keys usually already exist — the wizard discovers them
and offers a dropdown, so you rarely type a key name.

## Running an action (state-changing)

Actions are guarded. **Dry-run is the default** — it shows the exact command and
touches nothing:

```bash
exodia run tenant-copy.hana.copy-tenant --config target.yaml
#   [DRY-RUN] would run: CREATE DATABASE QAS AS REPLICA OF PRD AT '<src>:3<nn>13'
```

To run for real you must opt in explicitly with `--execute --yes`. For a copy
with a target tenant, the wizard also asks you to **type the target name** to
confirm — no accidental migration of the wrong tenant:

```bash
exodia run tenant-copy.hana.copy-tenant --config target.yaml --execute --yes --monitor
#   → pre-checks → execute (live progress bar + log tail) → verify → rollback on failure
```

## The day-of playbook

Print the whole cutover as a reference card — the four phases in order, with the
exact command for each step and the safety gates flagged:

```bash
exodia cutover-plan
```

## Reporting

Every run seals an evidence bundle. Produce a shareable report any time:

```bash
exodia report --format html    # phase-grouped, colour-coded verdict banner
exodia report --format csv     # opens in Excel
exodia history                 # when / duration / verdict for every past run
exodia evidence verify <dir>   # re-hash a bundle to prove it wasn't altered
```

## Next steps

- **[Core Concepts](concepts.md)** — the mental model behind checks, actions,
  runbooks, evidence and snapshot/compare.
- **[HANA Tenant Copy](tenant-copy.md)** — the full operator guide.
