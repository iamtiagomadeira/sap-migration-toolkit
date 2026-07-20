"""Evidence bundle — audit trail for migration operations.

Migration consultants must document what they did for audit. Instead of manual
screenshots (which are not searchable, diffable, or tamper-evident), Exodia
captures evidence as an automatic by-product of execution: every run writes a
self-contained bundle with a tamper-evident manifest.

Bundle layout::

    evidence/<methodology>/<SID>/<UTC-timestamp>/
        manifest.json   chain-of-custody metadata + SHA-256 of every artifact
        run.jsonl       append-only event log (one JSON object per line)
        results.json    the structured Results (same objects rendered to table)
        report.md       human-readable report (table + verdict) for the auditor
        artifacts/      harvested external logs (sapinst.log, keydb.xml, ...)

Design goals:
- **Zero extra effort**: the runner writes evidence automatically.
- **Tamper-evident**: manifest records the SHA-256 of each artifact; an auditor
  re-hashes to prove nothing was altered after the fact.
- **Searchable**: JSONL / JSON, not images.
- **Chain of custody**: operator, host, source->target, tool version, ticket,
  timestamps.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:
    from .context import Context
    from .result import Result

log = get_logger()

_DEFAULT_ROOT = Path("evidence")


def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("exodia")
    except Exception:  # noqa: BLE001 - packaging metadata may be absent in dev
        return "0.0.0-dev"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(value: str | None, fallback: str) -> str:
    """Filesystem-safe path segment."""
    if not value:
        return fallback
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in value)
    return safe[:64] or fallback


class EvidenceBundle:
    """A single evidence directory for one operation run.

    Create one per invocation, ``open()`` it (writes the initial manifest),
    log events / add results / attach files as the run proceeds, then
    ``close()`` to finalise the manifest with artifact hashes and a report.
    """

    def __init__(
        self,
        methodology: str,
        ctx: Context | None = None,
        *,
        root: Path | str = _DEFAULT_ROOT,
        operation: str = "",
        now: datetime | None = None,
    ) -> None:
        self.methodology = methodology
        self.operation = operation
        self._ctx = ctx
        ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        sid = _slug(getattr(ctx, "sid", None), "NOSID")
        self.dir = Path(root) / _slug(methodology, "unknown") / sid / ts
        self.artifacts_dir = self.dir / "artifacts"
        self._events: list[dict] = []
        self._results: list[Result] = []
        self._started = now or datetime.now(UTC)

    # -- lifecycle ---------------------------------------------------------- #

    def open(self) -> EvidenceBundle:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.log_event("run.start", methodology=self.methodology, operation=self.operation)
        return self

    def close(self, results: list[Result] | None = None) -> Path:
        """Finalise: write results.json, report.md, and the sealed manifest."""
        if results is not None:
            self._results = results
        self.log_event("run.end", results=len(self._results))
        self._write_results()
        self._write_report()
        self._write_manifest()
        log.info("evidence bundle written: %s", self.dir)
        return self.dir

    def __enter__(self) -> EvidenceBundle:
        return self.open()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- capture ------------------------------------------------------------ #

    def log_event(self, kind: str, **fields: object) -> None:
        """Append one structured event to the in-memory log (flushed on close)."""
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            **fields,
        }
        self._events.append(event)
        # Flush incrementally so a crash still leaves a partial trail.
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")

    def add_results(self, results: list[Result]) -> None:
        self._results.extend(results)
        for r in results:
            self.log_event(
                "result",
                name=r.name,
                status=r.status.value,
                summary=r.summary,
            )

    def attach(self, source: Path | str, *, caption: str = "") -> Path:
        """Copy an external file (log, screenshot) into the bundle's artifacts.

        Registered in the manifest with its own SHA-256 + caption. Use this for
        harvested SWPM logs (sapinst.log, keydb.xml) or the rare unavoidable GUI
        screenshot.
        """
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(f"cannot attach — not a file: {src}")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = self.artifacts_dir / src.name
        shutil.copy2(src, dest)
        self.log_event("attach", file=src.name, caption=caption, source=str(src))
        return dest

    # -- serialisation ------------------------------------------------------ #

    def _write_results(self) -> None:
        payload = [r.model_dump(mode="json") for r in self._results]
        (self.dir / "results.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    def _write_report(self) -> None:
        lines = [
            f"# Migration evidence — {self.methodology}",
            "",
            f"- **Operation:** {self.operation or '(pre-checks)'}",
            f"- **When (UTC):** {self._started.isoformat()}",
            f"- **Operator:** {self._operator()}",
        ]
        if self._ctx is not None:
            src = getattr(self._ctx, "source", None)
            tgt = getattr(self._ctx, "target", None)
            sid = getattr(self._ctx, "sid", None)
            host = getattr(self._ctx, "host", None) or "local"
            if src or tgt:
                lines.append(f"- **Copy:** {src or '?'} → {tgt or '?'}")
            if sid:
                lines.append(f"- **SID:** {sid}")
            lines.append(f"- **Host:** {host}")
        lines += ["", "## Results", "", "| Check / phase | Status | Summary |", "|---|---|---|"]
        for r in self._results:
            summary = r.summary.replace("|", "\\|")
            lines.append(f"| {r.name} | {r.status.value.upper()} | {summary} |")
        lines.append("")
        (self.dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_manifest(self) -> None:
        artifacts = []
        for f in sorted(self.dir.rglob("*")):
            if f.is_file() and f.name != "manifest.json":
                artifacts.append(
                    {
                        "path": str(f.relative_to(self.dir)),
                        "sha256": _sha256(f),
                        "bytes": f.stat().st_size,
                    }
                )
        manifest = {
            "schema": "exodia.evidence/v1",
            "methodology": self.methodology,
            "operation": self.operation,
            "tool_version": _tool_version(),
            "operator": self._operator(),
            "hostname": platform.node(),
            "started": self._started.isoformat(),
            "sealed": datetime.now(UTC).isoformat(),
            "context": self._context_summary(),
            "results_count": len(self._results),
            "artifacts": artifacts,
        }
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )

    def _context_summary(self) -> dict:
        if self._ctx is None:
            return {}
        c = self._ctx
        return {
            "source": getattr(c, "source", None),
            "target": getattr(c, "target", None),
            "sid": getattr(c, "sid", None),
            "db_type": getattr(c, "db_type", None),
            "system_type": getattr(c, "system_type", None),
            "host": getattr(c, "host", None),
            "dry_run": getattr(c, "dry_run", None),
            "ticket": c.get("ticket") if hasattr(c, "get") else None,
        }

    @staticmethod
    def _operator() -> str:
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001 - no passwd entry in some containers
            return "unknown"


def verify_bundle(bundle_dir: Path | str) -> list[str]:
    """Re-hash every artifact and report any that don't match the manifest.

    Returns a list of human-readable problems (empty list => bundle intact).
    An auditor runs this to prove the evidence was not tampered with.
    """
    d = Path(bundle_dir)
    manifest_path = d / "manifest.json"
    if not manifest_path.is_file():
        return [f"no manifest.json in {d}"]
    manifest = json.loads(manifest_path.read_text())
    problems: list[str] = []
    recorded = {a["path"]: a["sha256"] for a in manifest.get("artifacts", [])}
    for rel, expected in recorded.items():
        f = d / rel
        if not f.is_file():
            problems.append(f"missing artifact: {rel}")
            continue
        actual = _sha256(f)
        if actual != expected:
            problems.append(f"hash mismatch: {rel}")
    # Detect files present but not in the manifest (added after sealing).
    for f in sorted(d.rglob("*")):
        if f.is_file() and f.name != "manifest.json":
            rel = str(f.relative_to(d))
            if rel not in recorded:
                problems.append(f"untracked file (added after sealing?): {rel}")
    return problems
