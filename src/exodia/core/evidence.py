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
    from .monitor import Monitor
    from .result import Result

from .result import format_duration

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
                started_at=r.started_at.isoformat() if r.started_at else None,
                ended_at=r.ended_at.isoformat() if r.ended_at else None,
                duration_seconds=r.duration_seconds,
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
        op_started, op_ended, op_duration = self._operation_timing()
        lines = [
            f"# Migration evidence — {self.methodology}",
            "",
            f"- **Operation:** {self.operation or '(pre-checks)'}",
            f"- **Started (UTC):** {op_started.isoformat() if op_started else self._started.isoformat()}",
        ]
        if op_ended is not None:
            lines.append(f"- **Ended (UTC):** {op_ended.isoformat()}")
        if op_duration is not None:
            lines.append(f"- **Duration:** {format_duration(op_duration)}")
        lines.append(f"- **Operator:** {self._operator()}")
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
        lines += [
            "",
            "## Results",
            "",
            "| Check / phase | Status | Duration | Summary |",
            "|---|---|---|---|",
        ]
        for r in self._results:
            summary = r.summary.replace("|", "\\|")
            lines.append(
                f"| {r.name} | {r.status.value.upper()} | {r.duration_str} | {summary} |"
            )
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
        op_started, op_ended, op_duration = self._operation_timing()
        manifest = {
            "schema": "exodia.evidence/v1",
            "methodology": self.methodology,
            "operation": self.operation,
            "tool_version": _tool_version(),
            "operator": self._operator(),
            "hostname": platform.node(),
            "started": self._started.isoformat(),
            "sealed": datetime.now(UTC).isoformat(),
            # Exact span of the actual work (first phase start -> last phase end),
            # distinct from ``sealed`` (when the bundle was written to disk).
            "operation_started": op_started.isoformat() if op_started else None,
            "operation_ended": op_ended.isoformat() if op_ended else None,
            "duration_seconds": op_duration,
            "duration_str": format_duration(op_duration),
            "context": self._context_summary(),
            "results_count": len(self._results),
            "artifacts": artifacts,
        }
        (self.dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )

    def _operation_timing(
        self,
    ) -> tuple[datetime | None, datetime | None, float | None]:
        """Real start/end/duration of the work, derived from timed results.

        Start = earliest ``started_at``; end = latest ``ended_at``. Duration is
        the wall-clock span between them (not the sum of phases), which is what
        an auditor means by "how long did the migration take".
        """
        starts = [r.started_at for r in self._results if r.started_at is not None]
        ends = [r.ended_at for r in self._results if r.ended_at is not None]
        if not starts or not ends:
            return None, None, None
        first, last = min(starts), max(ends)
        return first, last, max(0.0, (last - first).total_seconds())

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


def find_latest_bundle(root: Path | str = _DEFAULT_ROOT) -> Path | None:
    """Return the most recent evidence bundle under ``root``, or None.

    A bundle is any directory that contains a ``manifest.json``. "Most recent"
    is decided by the manifest's ``sealed`` timestamp, falling back to the
    directory mtime when the manifest can't be read.
    """
    r = Path(root)
    if not r.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for manifest in r.rglob("manifest.json"):
        d = manifest.parent
        try:
            sealed = json.loads(manifest.read_text()).get("sealed", "")
        except (OSError, json.JSONDecodeError):
            sealed = ""
        key = sealed or datetime.fromtimestamp(d.stat().st_mtime, UTC).isoformat()
        candidates.append((key, d))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def list_bundles(root: Path | str = _DEFAULT_ROOT) -> list[dict]:
    """Return a summary of every evidence bundle under ``root``, newest first.

    Each entry carries the fields needed to answer "when did each migration
    start, end and how long did it take": ``dir``, ``methodology``,
    ``operation``, ``sid``, ``operator``, ``started``, ``ended``,
    ``duration_seconds``, ``duration_str`` and ``results_count``.
    """
    r = Path(root)
    if not r.is_dir():
        return []
    rows: list[dict] = []
    for manifest_path in r.rglob("manifest.json"):
        d = manifest_path.parent
        try:
            m = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ctx = m.get("context", {}) or {}
        started = m.get("operation_started") or m.get("started")
        rows.append(
            {
                "dir": str(d),
                "methodology": m.get("methodology", "?"),
                "operation": m.get("operation") or "(pre-checks)",
                "sid": ctx.get("sid"),
                "operator": m.get("operator"),
                "started": started,
                "ended": m.get("operation_ended"),
                "duration_seconds": m.get("duration_seconds"),
                "duration_str": m.get("duration_str") or format_duration(m.get("duration_seconds")),
                "results_count": m.get("results_count", 0),
                "sealed": m.get("sealed", ""),
            }
        )
    rows.sort(key=lambda x: x.get("started") or x.get("sealed") or "", reverse=True)
    return rows


def read_events(bundle_dir: Path | str) -> list[dict]:
    """Read the append-only ``run.jsonl`` event trail of a bundle, in order.

    Tolerant of a partially-written trailing line (a crash mid-flush): malformed
    lines are skipped so a live/interrupted operation can still be reattached.
    """
    path = Path(bundle_dir) / "run.jsonl"
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # partial trailing write from an in-flight run
    return events


def find_active_bundle(root: Path | str = _DEFAULT_ROOT) -> Path | None:
    """Return the newest bundle that has a ``run.jsonl`` but is not yet sealed.

    A sealed bundle has ``sealed`` set in its manifest; an operation still in
    flight has events but no seal. Used by ``exodia run --reattach`` to find the
    operation to reconnect to after a dropped SSH session.
    """
    r = Path(root)
    if not r.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    for jsonl in r.rglob("run.jsonl"):
        d = jsonl.parent
        manifest = d / "manifest.json"
        sealed = ""
        if manifest.is_file():
            try:
                m = json.loads(manifest.read_text())
                sealed = m.get("sealed", "")
            except (OSError, json.JSONDecodeError):
                pass
        if sealed:
            continue  # already finished
        # Order by the newest event timestamp in the trail.
        events = read_events(d)
        last_ts = events[-1]["ts"] if events and "ts" in events[-1] else ""
        candidates.append((last_ts or "", d))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def replay_events(bundle_dir: Path | str, monitor: Monitor) -> int:
    """Push a bundle's persisted events into a monitor to rebuild its state.

    Reconstructs phase, log tail and per-result rows (with their persisted
    timing) from ``run.jsonl`` so ``--reattach`` shows the same dashboard the
    original operator saw. Returns the number of events replayed.
    """
    from .result import Result, Status

    events = read_events(bundle_dir)
    for ev in events:
        kind = ev.get("kind")
        if kind == "phase":
            monitor.phase(str(ev.get("name", "")), str(ev.get("detail", "")))
        elif kind == "log":
            monitor.log_line(str(ev.get("line", "")))
        elif kind == "result":
            try:
                status = Status(ev.get("status", "pass"))
            except ValueError:
                status = Status.PASS
            r = Result(
                name=str(ev.get("name", "?")),
                status=status,
                summary=str(ev.get("summary", "")),
            )
            r.duration_seconds = ev.get("duration_seconds")
            monitor.result(r)
    return len(events)


_HTML_STATUS_COLOR = {
    "pass": "#1a7f37",
    "warn": "#9a6700",
    "fail": "#cf222e",
    "skip": "#57606a",
    "error": "#a40e26",
}


def render_html(bundle_dir: Path | str) -> str:
    """Render a sealed bundle as a standalone, shareable HTML document.

    Reads ``manifest.json`` + ``results.json`` (no external assets, inline CSS)
    so the file can be attached to a handover email or opened offline.
    """
    d = Path(bundle_dir)
    manifest = json.loads((d / "manifest.json").read_text())
    results_path = d / "results.json"
    results = json.loads(results_path.read_text()) if results_path.is_file() else []

    def esc(text: object) -> str:
        s = "" if text is None else str(text)
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    ctx = manifest.get("context", {}) or {}
    rows = []
    for r in results:
        status = str(r.get("status", "")).lower()
        color = _HTML_STATUS_COLOR.get(status, "#57606a")
        dur = format_duration(r.get("duration_seconds"))
        rows.append(
            f'<tr><td class="name">{esc(r.get("name"))}</td>'
            f'<td><span class="badge" style="background:{color}">'
            f"{esc(status.upper())}</span></td>"
            f'<td class="dur">{esc(dur)}</td>'
            f'<td>{esc(r.get("summary"))}</td></tr>'
        )
    rows_html = "\n".join(rows) or '<tr><td colspan="4">No results recorded.</td></tr>'

    meta = [
        ("Methodology", manifest.get("methodology")),
        ("Operation", manifest.get("operation") or "(pre-checks)"),
        ("Operator", manifest.get("operator")),
        ("Hostname", manifest.get("hostname")),
        ("Started", manifest.get("operation_started") or manifest.get("started")),
        ("Ended", manifest.get("operation_ended")),
        ("Duration", manifest.get("duration_str")),
        ("Sealed", manifest.get("sealed")),
        ("Tool version", manifest.get("tool_version")),
        ("SID", ctx.get("sid")),
        ("Copy", f"{ctx.get('source') or '?'} → {ctx.get('target') or '?'}"),
        ("Ticket", ctx.get("ticket")),
    ]
    meta_html = "\n".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in meta if v not in (None, "")
    )
    n_artifacts = len(manifest.get("artifacts", []))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exodia evidence — {esc(manifest.get("methodology"))}</title>
<style>
  body {{ font: 15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; color:#1f2328;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; border-bottom: 2px solid #d0d7de; padding-bottom:.4rem; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap:.2rem 1rem; }}
  dt {{ font-weight: 600; color:#57606a; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th,td {{ text-align: left; padding:.5rem .6rem; border-bottom: 1px solid #d0d7de; }}
  th {{ background:#f6f8fa; }}
  td.name {{ font-family: ui-monospace,SFMono-Regular,monospace; font-size:.9em; }}
  td.dur {{ font-variant-numeric: tabular-nums; color:#57606a; white-space:nowrap; }}
  .badge {{ color:#fff; padding:.1rem .5rem; border-radius: 2rem; font-size:.8em;
            font-weight:600; }}
  footer {{ margin-top: 2rem; color:#57606a; font-size:.85em; }}
</style></head><body>
<h1>Migration evidence — {esc(manifest.get("methodology"))}</h1>
<dl>{meta_html}</dl>
<table><thead><tr><th>Check / phase</th><th>Status</th><th>Duration</th><th>Summary</th></tr></thead>
<tbody>
{rows_html}
</tbody></table>
<footer>Tamper-evident bundle · {esc(n_artifacts)} artifact(s) hashed (SHA-256) ·
schema {esc(manifest.get("schema", "exodia.evidence/v1"))}</footer>
</body></html>
"""
