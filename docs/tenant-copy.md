# HANA Tenant Copy — operator guide

A cross-host HANA tenant copy takes a tenant database from a **source** system
(typically the customer environment) and re-creates it on a **target** system
(typically SAP HEC machines), across two *different* HANA installations.

Exodia drives this in three stages, each with its own safety model:

1. **Readiness sweep** (read-only) — one command, one aggregate verdict.
2. **Plan** (dry-run, the default) — see every SQL statement before it runs.
3. **Execute → verify** (opt-in) — run the copy, then prove the data came across.

Everything is password-free: hdbsql authenticates through the HANA secure user
store (`hdbuserstore`), so no secret ever appears on a command line, in a config
file, or in the evidence bundle.

---

## 1. One-time setup

### 1.1 Where Exodia runs

Run it on a host that can reach **both** SYSTEMDBs — usually a jump host. For a
remote host, Exodia uses SSH with host-key verification and key-based auth only.

### 1.2 hdbuserstore keys

Create the secure-store keys once, on the box that runs Exodia. This is the only
place a password is entered — into the store, never into Exodia.

```bash
# SYSTEMDB keys (used by the readiness checks + the copy itself)
hdbuserstore SET SRCSYS  <src_host>:3<nn>13  SYSTEM  <src_systemdb_pwd>
hdbuserstore SET TGTSYS  <tgt_host>:3<mm>13  SYSTEM  <tgt_systemdb_pwd>

# Tenant keys (used by the reinforced post-copy verify — optional but recommended)
hdbuserstore SET SRCTEN  <src_host>:3<nn>15@PRD  <user>  <pwd>
hdbuserstore SET TGTTEN  <tgt_host>:3<mm>15@QAS  <user>  <pwd>
```

`<nn>` / `<mm>` are the two-digit instance numbers. The SYSTEMDB SQL port is
`3<nn>13`; a tenant's indexserver port is typically `3<nn>15`.

### 1.3 Service user authorisations

The SYSTEMDB user needs `DATABASE ADMIN` (create/drop tenants) and `INIFILE
ADMIN`. The tenant user used by verify only needs `SELECT` on `M_TABLES`
(read-only).

### 1.4 Network

The **target** must reach the **source** SYSTEMDB SQL port (`3<nn>13`) for the
replication method. The `cross-host-reachability` check verifies this, but the
firewall must actually be open.

### 1.5 Config file

Copy [`examples/tenant-copy.yaml`](../examples/tenant-copy.yaml), fill in your
values (tenant names, the four key names, source host + instance), and you are
ready. Unknown keys are rejected, so a typo fails loudly at load time.

---

## 2. Stage 1 — readiness sweep (read-only, safe)

```bash
exodia runbook tenant-copy.hana.readiness --config tenant-copy.yaml
```

Runs the eleven prerequisite checks in dependency order and prints one aggregate
verdict plus a sealed evidence bundle:

| Order | Check | What it proves |
|---|---|---|
| 1 | `source-userstore-key` | source SYSTEMDB key authenticates |
| 2 | `target-userstore-key` | target SYSTEMDB key authenticates |
| 3 | `cross-host-reachability` | target can reach the source SQL port |
| 4 | `source-tenant-online` | the source tenant exists and is ONLINE |
| 5 | `target-tenant-absent` | the target name is free (never overwrite) |
| 6 | `version-match` | target HANA revision ≥ source |
| 7 | `ssl-collateral` | cross-host TLS/SSL material is in place |
| 8 | `source-replication-status` | source isn't mid-replication elsewhere |
| 9 | `target-license` | target is licensed |
| 10 | `target-data-space` | room on the target data volume |
| 11 | `target-log-space` | room on the target log volume |

It re-reads the live systems every time — safe to run as often as you like. The
verdict is honest: a run where nothing could be evaluated reads **Inconclusive**,
never a false "ready".

Re-run it until the verdict is green before touching the copy.

---

## 3. Stage 2 — plan the copy (dry-run is the default)

```bash
exodia run tenant-copy.hana.copy-tenant --config tenant-copy.yaml
```

Prints the exact ordered SQL it *would* run (e.g. `CREATE DATABASE QAS AS
REPLICA OF PRD AT 'customer-hana:33013'`, then the finalize step) and executes
nothing. Review it.

---

## 4. Stage 3 — execute and verify

```bash
exodia run tenant-copy.hana.copy-tenant --config tenant-copy.yaml --execute --yes
```

Runs the guarded flow: **pre-checks → execute → verify → (documented rollback on
failure)**.

### Reinforced verify

When `source_tenant_key` and `target_tenant_key` are set, `verify` does more
than confirm the tenant is ONLINE — it connects to both tenants and compares:

- **table count** (`COUNT(*)` over `M_TABLES`, non-`_SYS` schemas) — must match
  **exactly**; any difference is a blocking FAIL ("copy incomplete").
- **total record count** (`SUM(RECORD_COUNT)`) — must match within a tolerance
  (`verify_record_tolerance`, default **1%**). Column-store counts wobble with
  delta/main merges, so a small drift is normal; a larger drift FAILs.

Without those keys, verify falls back to the plain "tenant is online" verdict
and says so — so you always know whether integrity was actually checked.

### Rollback

A completed copy is not auto-reversible. On failure Exodia prints the documented
rollback (drop the partially-created target tenant: `DROP DATABASE <target>`,
see SAP Note 2101244) — it never silently drops anything.

---

## 5. Evidence

Every run — readiness sweep, dry-run, or execute — writes a sealed, tamper-
evident bundle under `evidence/`. Inspect or share it:

```bash
exodia history                     # table of past runs (when / duration / verdict)
exodia report <bundle_dir>         # render a run as shareable HTML + Markdown
exodia evidence verify <bundle_dir>   # re-hash artifacts, detect tampering
```

---

## 6. First test on a sandbox pair

You do **not** need production systems to exercise the whole flow. Provision two
small HANA sandbox systems (or two tenants on two hosts) and:

1. Create the four hdbuserstore keys (§1.2).
2. Run the readiness sweep (§2) and drive it to green.
3. Dry-run the copy (§3) and read the planned SQL.
4. Execute (§4) and confirm the reinforced verify reports matching counts.
5. `exodia report` the bundle and review the timed, sealed audit trail.

That validates hdbsql output parsing, the SQL, timeouts, and the verify
comparison against real HANA — the last step before using it on a real
migration.
