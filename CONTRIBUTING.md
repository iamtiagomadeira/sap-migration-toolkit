# Contributing to SAP Migration Toolkit

Thanks for your interest in improving the toolkit. Contributions of all sizes are
welcome — bug reports, new methodology modules, additional checks, and SAP Note
mappings are especially valuable.

## Getting started

```bash
git clone https://github.com/iamtiagomadeira/sap-migration-toolkit.git
cd sap-migration-toolkit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tui,dev]"
exodia doctor          # sanity check
```

## Development workflow

1. **Open an issue first** for anything non-trivial, so we can agree on the approach.
2. Create a branch: `feat/<short-name>` or `fix/<short-name>`.
3. Keep changes focused — one logical change per pull request.
4. Run the checks locally before pushing:
   ```bash
   pytest                 # test suite
   ruff check .           # lint
   ```
5. Open a pull request against `main` with a clear description of the what and why.

## Commit style

Use [Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:` — e.g.
`feat(backup-restore): add HANA log_mode pre-check`.

## Adding a methodology module

The toolkit is plugable: a module dropped under `src/exodia/modules/` is
auto-discovered. Each module exposes:

- **Checks** — read-only, safe to run anywhere. No side effects, ever.
- **Actions** — state-changing, and always guarded: pre-checks → dry-run (default)
  → explicit confirmation → execute → verify → documented rollback.

Guidelines:

- Commands must be argument lists, never `shell=True`.
- Never log secrets. Use host-key verification for SSH.
- When you reference an SAP Note, cite the **note number** only — never reproduce
  its copyrighted text.
- Add tests under `tests/` for every new check and action.

## Reporting security issues

Please do **not** open a public issue for security vulnerabilities. Report them
privately following our [security policy](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](LICENSE).
