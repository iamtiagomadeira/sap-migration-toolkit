# Security Policy

Exodia is a **stateless executor for SAP migration operations**. It runs against
**production SAP systems**, performs **destructive operations** (database restore,
tenant/system copy, HSR handover), and handles **sensitive credentials**
(hdbuserstore keys, SECSTORE key phrases, database passwords). The risk surface is
high by design, so security is a first-class concern of the project.

This document describes the threat model, the guarantees the codebase makes, and
how to report a vulnerability.

## Supported versions

Exodia is pre-1.0 (`0.1.x`, Development Status: Alpha). Security fixes are applied
to `main` and released in the next tagged version. There is no long-term-support
branch yet.

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report privately via one of:

- GitHub **Security Advisories**: open a draft advisory at
  <https://github.com/iamtiagomadeira/sap-migration-toolkit/security/advisories/new>
  (preferred — keeps the report private and coordinated).
- Email the maintainer: **tiago.filipe.madeira@gmail.com** with subject
  `EXODIA SECURITY`.

Please include: affected version/commit, a description of the issue, reproduction
steps or a PoC, and the impact you foresee. We aim to acknowledge within
**72 hours** and to agree on a disclosure timeline with you. Coordinated
disclosure is appreciated — give us a reasonable window to ship a fix before any
public disclosure.

## Threat model

Exodia is operated by SAP Basis administrators against systems they already have
privileged access to. The primary threats we defend against are:

1. **Shell / command injection** — a migration tool that builds command strings
   is one crafted hostname/path away from arbitrary code execution on a
   production host.
2. **Credential leakage** — passwords, hdbuserstore keys and SECSTORE key phrases
   leaking into argv, logs, `Result` objects, JSON output, or tracebacks (which
   are frequently captured by CI/automation and ticket systems).
3. **Unintended destructive execution** — a restore/recovery firing without an
   explicit, reviewed confirmation, or against the wrong target.
4. **Man-in-the-middle over SSH** — silently trusting an unknown remote host key
   and executing privileged commands against an impostor.

Out of scope: securing the SAP systems themselves, network segmentation, OS
hardening of the hosts Exodia runs on, and the confidentiality of the
hdbuserstore / SECSTORE material at rest (that is SAP's responsibility).

## Design guarantees

These are enforced in code and locked in by tests (`tests/test_security.py`):

### 1. No `shell=True`, ever — argv-only execution
All command execution goes through `core/shell.py`. Commands are always
`argv: list[str]` handed directly to the process; `shell=True` is never used.
`Runner.run` rejects anything that is not a `list[str]` with a `TypeError`. This
eliminates shell injection as a **class** of bug — the single biggest flaw in the
internal predecessor tool. For remote execution, `SSHRunner` escapes every argv
element with `shlex.quote` before handing the line to paramiko's `exec_command`,
so no argument can break out of its token.

### 2. Secrets never reach argv or logs
- **HANA** authenticates through the secure user store: `hdbsql -U <KEY>`. No
  password is ever placed on the command line.
- **ASE** avoids `-P <cleartext>`; it relies on the ASE user store / SSO. Only
  the (secret-free) SQL statement is passed as an argv element.
- **SECSTORE** checks confirm the key files exist and are readable — they never
  read, print, or log the key phrase or key-file contents.
- The structured logger (`core/logging.py`) installs a `RedactingFilter` on
  every handler that scrubs `password=`, `passphrase=`, `key_phrase=`, `token=`,
  `api_key=`, and command-line `-p/-P/--password <value>` forms before any
  record is emitted.
- A defensive `redact()` helper scrubs captured command output before it lands
  in a `Result` that could be serialised to JSON in CI.
- If an operator accidentally passes a secret as a `--config` param, the SECSTORE
  check records only the **key name** that must be removed, never the value.

### 3. Guarded destructive flow — dry-run by default
Every state-changing `Action` runs through `Action.run_guarded`:

```
dry-run (always, shows exact commands)  ->  confirmation gate (--yes)  ->  execute  ->  verify
```

- `ctx.dry_run` defaults to **`True`**. Nothing executes unless the operator
  passes `--execute`.
- Even with `--execute`, the action stops at a confirmation gate unless `--yes`
  is also given.
- Required pre-checks (`requires_checks`) must pass before an action executes;
  a blocking pre-check failure aborts the run.
- A failed `execute` is **never** followed by `verify`.
- `rollback` is documented-only by default: Exodia does **not** auto-reverse a
  completed restore (it points at the runbook / SAP Note instead).

### 4. Secure SSH by default
`SSHRunner` uses paramiko's `RejectPolicy()` — an **unknown host key is refused**,
never silently trusted (no `StrictHostKeyChecking=no` equivalent). Host keys are
loaded from the system `known_hosts` or a caller-supplied file. Authentication is
**key-based only** (agent / `look_for_keys` / explicit `key_filename`); no
password is ever passed to paramiko. Connection establishment is bounded by
`connect_timeout` (TCP, banner, and auth timeouts), and every command carries an
execution timeout, so a hung or unreachable host cannot block a run indefinitely.

### 5. Stateless by design
A `Context` is built per invocation from CLI args + an optional config file,
passed down, and discarded. Exodia keeps no persistent state and no credential
store of its own — reducing the window in which any secret exists in memory.

## Secrets hygiene for operators

- Never pass passwords or key phrases on the command line or in `--config`.
- Use the hdbuserstore (`hdbsql -U <KEY>`) for HANA and the ASE user store / SSO
  for ASE.
- Keep `known_hosts` accurate; do not disable host-key verification.
- `.gitignore` already excludes `.exodia/`, `*.key`, `*.pem`, `id_rsa*`,
  `SecStore.*`, `.env`, `inifile.params`, and similar — never force-add these.

## Automated security checks

CI runs two security tools on every push and pull request (non-blocking for now,
to be promoted to blocking once a baseline is established):

- **[bandit](https://bandit.readthedocs.io/)** — static analysis of the Python
  source (`bandit -r src/`). Triaged false-positives carry an inline
  `# nosec <ID>` with a justification.
- **[pip-audit](https://pypi.org/project/pip-audit/)** — known-vulnerability scan
  of the dependency tree.

Run them locally with:

```bash
pip install bandit pip-audit
bandit -r src/
pip-audit
```
