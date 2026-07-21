"""ABAP profile & global-directory backup (SAP MIG — guarded action).

Backs up the SAP profiles (and, on the target, the fundamental global config
directories) to a chosen location before a migration touches them. This is the
automated form of the manual "copy /sapmnt/<SID>/profile somewhere safe" step
every cutover does by hand.

Two backup scopes, selected by ``backup_scope``:

* ``profile`` (default) — the instance/default profiles in
  ``/sapmnt/<SID>/profile``. The typical SOURCE backup.
* ``global`` — the profiles PLUS the fundamental global directories under
  ``/sapmnt/<SID>/global`` (SYS, security, etc.). The typical TARGET backup,
  taken after the source backup as part of the automation.

The copy is done with the context runner (SSH when a remote host is set, local
otherwise) using ``cp -a`` / ``tar`` — never a shell string with interpolated
untrusted data. Guarded flow: dry-run (show exactly what would be copied) →
confirm → execute → verify (the backup exists and is non-empty).
"""

from __future__ import annotations

import shlex

from exodia.core import Context, Result
from exodia.core.base import Action
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase


def _sid(ctx: Context) -> str:
    return str(ctx.get("backup_sid") or ctx.sid or "").upper()


def _profile_dir(ctx: Context) -> str:
    return str(ctx.get("profile_dir") or f"/sapmnt/{_sid(ctx)}/profile")


def _global_dir(ctx: Context) -> str:
    return str(ctx.get("global_dir") or f"/sapmnt/{_sid(ctx)}/global")


def _scope(ctx: Context) -> str:
    return str(ctx.get("backup_scope", "profile")).lower()


def _dest_root(ctx: Context) -> str:
    return str(ctx.get("backup_dir") or f"/tmp/exodia-profile-backup/{_sid(ctx)}")


def _sources(ctx: Context) -> list[str]:
    """The directories to back up for the selected scope."""
    dirs = [_profile_dir(ctx)]
    if _scope(ctx) == "global":
        dirs.append(_global_dir(ctx))
    return dirs


class ProfileBackupAction(Action):
    """Back up SAP profiles (and optionally the global dir) to a location."""

    name = "abap.profile-backup"
    description = "Back up SAP profiles (+ global dir on target) to a location."
    title = "SAP Profile & Global Directory Backup"
    phase = Phase.PREPARATION
    destructive = True  # writes files (to the backup location) — guarded
    requires_checks: list[str] = []

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                "backup_sid", "SID to back up (for /sapmnt/<SID>/...)",
                help="SID whose profiles are backed up; defaults to the context SID.",
            ),
            ParamSpec(
                "backup_scope", "Backup scope", default="profile",
                choices=("profile", "global"),
                help="'profile' = /sapmnt/<SID>/profile; 'global' = profiles + "
                "/sapmnt/<SID>/global (typical target backup).",
            ),
            ParamSpec(
                "backup_dir", "Backup destination directory",
                help="Where to copy the profiles to; defaults to "
                "/tmp/exodia-profile-backup/<SID>.",
            ),
            ParamSpec(
                "profile_dir", "Profile directory (override)",
                help="Overrides the /sapmnt/<SID>/profile default.",
            ),
            ParamSpec(
                "global_dir", "Global directory (override)",
                help="Overrides the /sapmnt/<SID>/global default (global scope).",
            ),
            ParamSpec(
                "host", "Remote host (blank = local)", kind=ParamKind.FIELD,
                help="Host to back up from over SSH; blank backs up locally.",
            ),
            ParamSpec(
                "user", "SSH user", kind=ParamKind.FIELD,
                help="SSH user (typically <sid>adm) for the remote host.",
            ),
        ]

    def _dest_for(self, ctx: Context, src: str) -> str:
        """Destination path for a given source dir under the backup root."""
        root = _dest_root(ctx)
        leaf = src.rstrip("/").rsplit("/", 1)[-1] or "backup"
        return f"{root}/{leaf}"

    def dry_run(self, ctx: Context) -> Result:
        phase = f"{self.name}.dry-run"
        sid = _sid(ctx)
        if not sid and not ctx.get("profile_dir"):
            return Result.fail(phase, "no SID / profile_dir to back up (set backup_sid)")
        sources = _sources(ctx)
        root = _dest_root(ctx)
        steps = [f"mkdir -p {root}"]
        steps += [f"cp -a {s} {self._dest_for(ctx, s)}" for s in sources]
        return Result.ok(
            phase,
            f"[{_scope(ctx)}] would back up {len(sources)} directory(ies) for {sid or '?'} "
            f"to {root}; nothing copied",
            detail="\n".join(f"  {i}. {s}" for i, s in enumerate(steps, start=1)),
            data={"scope": _scope(ctx), "sources": sources, "dest_root": root},
            facts={
                "SID": sid or "?",
                "Scope": _scope(ctx),
                "Destination": root,
                "Directories": str(len(sources)),
            },
        )

    def execute(self, ctx: Context) -> Result:
        phase = f"{self.name}.execute"
        sid = _sid(ctx)
        sources = _sources(ctx)
        root = _dest_root(ctx)
        runner = ctx.runner()
        timeout = int(ctx.get("backup_timeout", 600))

        mk = runner.run(["mkdir", "-p", root], timeout=timeout)
        if not mk.ok:
            return Result.fail(
                phase,
                f"could not create backup directory {root}",
                detail=mk.stderr or mk.stdout,
            )
        copied: list[str] = []
        for src in sources:
            dest = self._dest_for(ctx, src)
            cr = runner.run(["cp", "-a", src, dest], timeout=timeout)
            if not cr.ok:
                return Result.fail(
                    phase,
                    f"backup failed copying {src} -> {dest} — PAUSED",
                    detail=cr.stderr or cr.stdout,
                    data={"copied": copied, "failed": src},
                    facts={"SID": sid or "?", "Copied So Far": str(len(copied))},
                )
            copied.append(dest)
        return Result.ok(
            phase,
            f"backed up {len(copied)} directory(ies) for {sid or '?'} to {root}",
            data={"scope": _scope(ctx), "copied": copied, "dest_root": root},
            facts={
                "SID": sid or "?",
                "Scope": _scope(ctx),
                "Destination": root,
                "Directories Backed Up": str(len(copied)),
            },
        )

    def verify(self, ctx: Context) -> Result:
        phase = f"{self.name}.verify"
        sources = _sources(ctx)
        runner = ctx.runner()
        missing: list[str] = []
        checked: list[str] = []
        for src in sources:
            dest = self._dest_for(ctx, src)
            # Non-empty check: `ls -A <dest>` must return something.
            cr = runner.run(["ls", "-A", dest], timeout=int(ctx.get("verify_timeout", 120)))
            if not cr.ok or not cr.stdout.strip():
                missing.append(dest)
            else:
                checked.append(dest)
        if missing:
            return Result.fail(
                phase,
                f"backup verification failed — missing/empty: {', '.join(missing)}",
                data={"verified": checked, "missing": missing},
                facts={"Verified": str(len(checked)), "Missing": str(len(missing))},
            )
        return Result.ok(
            phase,
            f"backup verified: {len(checked)} directory(ies) present and non-empty",
            data={"verified": checked},
            facts={"Verified": str(len(checked)), "Missing": "0"},
        )

    def rollback(self, ctx: Context) -> Result:
        # A backup is additive (it only writes to the backup location); removing
        # it is safe but we do not auto-delete an operator's backup. Documented.
        root = _dest_root(ctx)
        return Result.skip(
            f"{self.name}.rollback",
            f"no automatic rollback — a backup is non-destructive; delete {root} "
            "manually if you need to discard it",
        )


# shlex is imported for callers that build display strings safely; referenced to
# keep linters from flagging it while the API stays argv-only.
_ = shlex
