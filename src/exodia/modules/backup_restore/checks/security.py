"""HANA backup security / filesystem checks (TIA-57 #7, #13).

  7. encryption root keys available IF the backup is encrypted
 13. sidadm permissions on the backup paths
"""

from __future__ import annotations

from exodia.core import Check, Context, Result

from . import _common as c


class EncryptionKeysCheck(Check):
    """When the backup is encrypted, the root keys must be available to recover."""

    name = "backup-restore.hana.encryption-keys"
    description = "Encryption root keys available for an encrypted backup."
    blocking = True

    def run(self, ctx: Context) -> Result:
        encrypted = bool(ctx.get("backup_encrypted", False))
        if not encrypted:
            return Result.skip(
                self.name,
                "backup is not encrypted — no root keys required",
                data={"encrypted": False},
            )
        stmt = (
            "SELECT ROOT_KEY_TYPE_NAME, HAS_BACKUP FROM SYS.M_ENCRYPTION_OVERVIEW"
        )
        cr = c.run(ctx, c.hdbsql_argv(ctx, stmt))
        if not cr.ok:
            return Result.fail(
                self.name,
                "encryption root key backup not available — cannot decrypt backup",
                detail=cr.stderr or cr.stdout,
            )
        rows = c.parse_hdbsql_rows(cr.stdout)
        if not rows:
            return Result.fail(
                self.name,
                "no encryption root key information found; root key missing for recovery",
                detail=cr.stdout,
            )
        missing = [r[0] for r in rows if len(r) >= 2 and r[1].strip().upper() in ("FALSE", "0", "")]
        if missing:
            return Result.fail(
                self.name,
                f"encryption root key backup missing for: {missing}",
                data={"missing_keys": missing},
            )
        return Result.ok(
            self.name,
            f"encryption root keys backed up ({len(rows)} type(s))",
            data={"key_types": [r[0] for r in rows]},
        )


class SidadmPermissionsCheck(Check):
    """<sid>adm must own / be able to read the backup paths."""

    name = "backup-restore.hana.sidadm-permissions"
    description = "sidadm has read access to the backup paths."
    blocking = True

    def run(self, ctx: Context) -> Result:
        the_sid = c.sid(ctx)
        backup_path = ctx.get("backup_path", "/hana/shared/backup")
        # -r: readable by the current (sidadm) user. Portable and side-effect free.
        cr = c.run(ctx, ["test", "-r", str(backup_path)])
        if cr.ok:
            return Result.ok(
                self.name,
                f"backup path {backup_path} is readable by sidadm",
                data={"sid": the_sid, "path": backup_path},
            )
        # Distinguish 'missing' from 'permission denied' for a clearer message.
        exists = c.run(ctx, ["test", "-e", str(backup_path)])
        if not exists.ok:
            return Result.fail(
                self.name,
                f"backup path {backup_path} does not exist",
                data={"sid": the_sid, "path": backup_path},
            )
        return Result.fail(
            self.name,
            f"permission denied: {the_sid or 'sidadm'} cannot read backup path {backup_path}",
            data={"sid": the_sid, "path": backup_path},
        )
