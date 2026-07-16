"""pipo.jvm-kernel-compat — verify JVM (SAP JVM) and kernel version compatibility.

For a Java system copy the target must run a SAP kernel and SAP JVM that are
compatible with (usually equal to or newer than) the source. A mismatch causes
bootstrap failures on the target. This read-only check reads the disp+work
kernel version and the SAP JVM version on both, and compares major/patch
levels supplied via the context.
"""

from __future__ import annotations

import re

from exodia.core import Check, Context, Result

_KERNEL_REL = re.compile(r"kernel release\s+(\d+)", re.IGNORECASE)
_KERNEL_PATCH = re.compile(r"(?:patch number|sup\.?\s*pkg|kernel patch number)\s+(\d+)", re.IGNORECASE)
_JVM_VER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class JvmKernelCompatCheck(Check):
    """SAP kernel + SAP JVM on the target are compatible with the source."""

    name = "pipo.jvm-kernel-compat"
    description = "SAP kernel + SAP JVM versions compatible (target >= source)."
    blocking = True

    def run(self, ctx: Context) -> Result:
        runner = ctx.runner()

        # disp+work -version prints kernel release + patch on the local host.
        dw = runner.run(["disp+work", "-version"])
        if not dw.ok and not dw.stdout:
            return Result.fail(
                self.name,
                "could not read kernel version (disp+work -version failed)",
                detail=dw.stderr,
            )
        krel_m = _KERNEL_REL.search(dw.stdout)
        kpatch_m = _KERNEL_PATCH.search(dw.stdout)
        kernel_rel = int(krel_m.group(1)) if krel_m else 0
        kernel_patch = int(kpatch_m.group(1)) if kpatch_m else 0

        # SAP JVM version.
        jvm_ver = ""
        jv = runner.run(["sapjvm", "-version"])
        if jv.stdout or jv.stderr:
            m = _JVM_VER.search(jv.stdout + "\n" + jv.stderr)
            if m:
                jvm_ver = m.group(0)

        data: dict[str, object] = {
            "kernel_release": kernel_rel,
            "kernel_patch": kernel_patch,
            "jvm_version": jvm_ver,
        }

        # Minimum thresholds expected for the target, provided by the operator.
        min_kernel_rel = int(ctx.get("min_kernel_release", 0))
        min_kernel_patch = int(ctx.get("min_kernel_patch", 0))
        min_jvm = str(ctx.get("min_jvm_version", ""))
        data["min_kernel_release"] = min_kernel_rel
        data["min_kernel_patch"] = min_kernel_patch
        data["min_jvm_version"] = min_jvm

        problems: list[str] = []
        if min_kernel_rel and kernel_rel < min_kernel_rel:
            problems.append(f"kernel release {kernel_rel} < required {min_kernel_rel}")
        elif min_kernel_rel and kernel_rel == min_kernel_rel and kernel_patch < min_kernel_patch:
            problems.append(f"kernel patch {kernel_patch} < required {min_kernel_patch}")
        if min_jvm and jvm_ver and _cmp_version(jvm_ver, min_jvm) < 0:
            problems.append(f"SAP JVM {jvm_ver} < required {min_jvm}")

        if problems:
            return Result.fail(
                self.name,
                "JVM/kernel incompatible: " + "; ".join(problems),
                data=data,
            )
        return Result.ok(
            self.name,
            f"kernel {kernel_rel} patch {kernel_patch}, SAP JVM {jvm_ver or 'n/a'} compatible",
            data=data,
        )


def _cmp_version(a: str, b: str) -> int:
    """Compare dotted numeric versions. Returns -1/0/1."""
    pa = [int(x) for x in re.findall(r"\d+", a)]
    pb = [int(x) for x in re.findall(r"\d+", b)]
    return (pa > pb) - (pa < pb)
