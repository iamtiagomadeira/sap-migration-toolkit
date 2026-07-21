# SAP MIG cutover — the four phases end to end

Exodia maps an ECS/HEC ABAP cutover onto the four macro-phases of the Cutover
Plan. Every check and action is tagged with its phase, so the evidence report
groups them exactly the way a migration team reasons about the day.

| Phase | What happens | Downtime? |
|---|---|---|
| **Preparation** | read-only readiness + parity on source & target, profile capture/backup | no |
| **Ramp-Down (Source)** | quiesce the source: suspend jobs, lock users, stop app servers | starts |
| **Downtime / Execution** | the tenant copy / replica is created and synced | yes |
| **Post-Activities (Target)** | re-open: start servers, resume jobs, unlock users, validate | ends |

Every action is **guarded**: dry-run (shows the exact command) → confirm →
execute → verify → documented rollback. Nothing state-changing runs without an
explicit opt-in.

---

## Phase 1 — Preparation (read-only + backups)

```bash
# Full readiness sweep (source parity + config), grouped verdict + evidence
exodia runbook abap.pre-migration-checks --config source.yaml

# Capture / back up the profiles before anything changes them
exodia run abap.profile-backup --config source.yaml --execute --yes         # source: profile scope
exodia run abap.profile-backup --config target.yaml --execute --yes         # target: global scope
#   (set backup_scope=global on the target to include /sapmnt/<SID>/global)
```

Readiness checks include SM51, CVERS, SCC4/T000, SM59, SE06, SICK, and the
source/target profile capture. Drive the verdict to green before proceeding.

## Phase 2 — Ramp-Down (quiesce the source)

Run in this order. The customer-impacting stop is gated behind an explicit
customer confirmation.

```bash
# 1. Suspend the background scheduler (no new jobs)
exodia run abap.rampdown.suspend-jobs --config source.yaml --execute --yes

# 2. (optional) Adapt operation modes for ramp-down
exodia run abap.rampdown.adapt-operation-modes --config source.yaml --execute --yes

# 3. Lock business users (technical users DDIC/SAP*/TMSADM are always spared)
exodia run abap.rampdown.lock-users --config source.yaml --execute --yes
#   set business_users="JSMITH,MARY,..." in the config

# 4. Stop ALL application servers — ONLY after the customer confirms
exodia run abap.rampdown.stop-app-servers --config source.yaml --execute --yes
#   REQUIRES customer_confirmed=true in the config; without it the step SKIPs
#   and sapcontrol is never invoked. The admin selects this only after the
#   customer has signed off.

# 5. Inform the customer that ramp-down is complete — MANUAL
exodia run abap.rampdown.inform-customer --config source.yaml --execute --yes
#   Exodia sends nothing: the admin emails the customer, then sets attested=true
#   so the cutover record shows ramp-down completion was communicated.
```

**Customer-confirmation gate (`stop-app-servers`)** — the config must carry:
```yaml
params:
  instance_number: "00"
  stop_scope: system          # StopSystem ALL
  customer_confirmed: true     # ← only true AFTER the customer signs off
```

## Phase 3 — Downtime / Execution (the copy)

```bash
# Confirm readiness, then create the replica (customer-gated, typed-name confirm)
exodia runbook tenant-copy.hana.readiness --config tenant-copy.yaml
exodia run tenant-copy.hana.copy-tenant --config tenant-copy.yaml            # dry-run: shows CREATE DATABASE ... AS REPLICA OF ...
exodia run tenant-copy.hana.copy-tenant --config tenant-copy.yaml --execute --yes
```

## Phase 4 — Post-Activities (re-open the target)

The mirror of ramp-down, once the copy is verified:

```bash
# 1. Start the target application servers
exodia run abap.post.start-app-servers --config target.yaml --execute --yes

# 2. Resume the background scheduler (BTCTRNS2)
exodia run abap.post.resume-jobs --config target.yaml --execute --yes

# 3. Unlock the business users (re-open to end users)
exodia run abap.post.unlock-users --config target.yaml --execute --yes
#   business_users="JSMITH,MARY,..." — typically the same set locked at ramp-down

# 4. Validate the system is online (SM51)
exodia run abap.post.validate-online --config target.yaml --execute --yes
```

---

## Evidence & reporting

Every phase writes a sealed evidence bundle. Produce the phased report for the
migration team / customer / SES at any point:

```bash
exodia report --format html    # phase-grouped HTML with a verdict banner
exodia report --format csv     # same data, opens in Excel
exodia history                 # every run: when / duration / verdict
```

The HTML groups results under the four phase headings, shows each step's
action-oriented title (e.g. "SM12 — Enqueue Lock Entries Check") and its
measured findings, and leads with a colour-coded readiness banner.
