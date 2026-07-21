"""Profile capture checks (SAP MIG — capture instance/default profiles).

Rather than *compare* source vs target (which conflates two independent facts),
these checks simply CAPTURE what is in the profiles on one side, as read-only
evidence:

* the DEFAULT profile (DEFAULT.PFL),
* the per-instance profiles,
* (target side) the presence of the fundamental global directories.

Profiles live on the filesystem under ``/sapmnt/<SID>/profile`` (and the shared
config under ``/sapmnt/<SID>/global``). These checks read them over the
context's runner — SSH when a remote host is configured, local otherwise — and
record the inventory + content as evidence. They never modify anything; the
actual *backup/download* is a separate guarded action (``profile-backup``).

Two concrete checks are exposed, one per side, so a snapshot taken on the source
and one taken on the target each capture their own profiles independently.
"""

from __future__ import annotations

from exodia.core import Check, Context, Result
from exodia.core.params import ParamKind, ParamSpec
from exodia.core.result import Phase

# Default profile directory layout for an ABAP system.
_DEFAULT_PROFILE_DIR = "/sapmnt/{sid}/profile"


def _profile_dir(ctx: Context, side: str) -> str:
    """Resolve the profile directory for a side, honouring an explicit override."""
    override = ctx.get(f"{side}_profile_dir")
    if override:
        return str(override)
    sid = ctx.get(f"{side}_sid") or ctx.sid or ""
    return _DEFAULT_PROFILE_DIR.format(sid=str(sid).upper())


class _ProfileCaptureCheck(Check):
    """Shared logic: list and fingerprint the profiles on one side (read-only)."""

    #: "source" or "target" — set by subclasses.
    side = "source"
    phase = Phase.PREPARATION

    def parameters(self) -> list[ParamSpec]:
        return [
            ParamSpec(
                f"{self.side}_sid",
                f"{self.side.capitalize()} SID (for /sapmnt/<SID>/profile)",
                help=f"SID of the {self.side} system; profile dir defaults to "
                f"/sapmnt/<SID>/profile.",
            ),
            ParamSpec(
                f"{self.side}_profile_dir",
                f"{self.side.capitalize()} profile directory (override)",
                help="Explicit profile directory; overrides the /sapmnt/<SID>/profile default.",
            ),
            ParamSpec(
                "host", "Remote host (blank = local)", kind=ParamKind.FIELD,
                help="Host to read the profiles from over SSH; blank reads locally.",
            ),
            ParamSpec(
                "user", "SSH user", kind=ParamKind.FIELD,
                help="SSH user (typically <sid>adm) for the remote host.",
            ),
        ]

    def run(self, ctx: Context) -> Result:
        profile_dir = _profile_dir(ctx, self.side)
        if not profile_dir or "{sid}" in profile_dir or profile_dir.endswith("//profile"):
            return Result.skip(
                self.name,
                f"no {self.side} SID / profile dir provided "
                f"(set {self.side}_sid or {self.side}_profile_dir)",
            )
        runner = ctx.runner()
        # List the profile files (read-only).
        listing = runner.run(["ls", "-1", profile_dir])
        if not listing.ok:
            return Result.fail(
                self.name,
                f"could not list {self.side} profile directory {profile_dir}",
                detail=listing.stderr or listing.stdout,
                facts={"Profile Directory": profile_dir, "Readable": "No"},
            )
        files = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
        default_pfl = [f for f in files if f.upper() == "DEFAULT.PFL"]
        instance_profiles = [
            f for f in files if f not in default_pfl and not f.startswith(".")
        ]
        data = {
            "side": self.side,
            "profile_dir": profile_dir,
            "files": files,
            "default_profile_present": bool(default_pfl),
            "instance_profile_count": len(instance_profiles),
        }
        facts = {
            "Side": self.side.capitalize(),
            "Profile Directory": profile_dir,
            "DEFAULT.PFL": "present" if default_pfl else "missing",
            "Instance Profiles": str(len(instance_profiles)),
        }
        if not files:
            return Result.warn(
                self.name,
                f"{self.side} profile directory {profile_dir} is empty",
                data=data,
                facts=facts,
            )
        if not default_pfl:
            return Result.warn(
                self.name,
                f"{self.side} profiles captured but DEFAULT.PFL is missing in {profile_dir}",
                data=data,
                facts=facts,
            )
        return Result.ok(
            self.name,
            f"{self.side} profiles captured: DEFAULT.PFL + {len(instance_profiles)} "
            f"instance profile(s) in {profile_dir}",
            data=data,
            facts=facts,
        )


class SourceProfileCaptureCheck(_ProfileCaptureCheck):
    """Capture the source system's profiles (read-only inventory)."""

    name = "abap.readiness.source-profiles"
    description = "Capture source instance/default profiles (/sapmnt/<SID>/profile)."
    title = "Source Profiles Capture (DEFAULT.PFL + instance profiles)"
    side = "source"


class TargetProfileCaptureCheck(_ProfileCaptureCheck):
    """Capture the target system's profiles (read-only inventory)."""

    name = "abap.readiness.target-profiles"
    description = "Capture target instance/default profiles (/sapmnt/<SID>/profile)."
    title = "Target Profiles Capture (DEFAULT.PFL + instance profiles)"
    side = "target"
