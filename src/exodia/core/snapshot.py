"""Portable snapshots — capture one side of a migration, carry it to the other.

The problem this solves
-----------------------
An ECS/HEC migration spans two *isolated* networks. The consultant cannot sit on
one jump host that reaches both HANA/ABAP systems — they log on to the customer
(source) system, read what they need, then separately log on to the target and
compare by hand against a runbook. That manual "read here, remember, compare
there" loop is exactly what Exodia automates with snapshots:

    # on / with access to the SOURCE:
    exodia snapshot tenant-copy.hana.readiness --config src.yaml -o source.json

    # carry source.json across the air-gap, then on the TARGET:
    exodia compare source.json --against tenant-copy.hana.readiness --config tgt.yaml

A snapshot is a self-contained, tamper-evident JSON file: the structured Result
of every check that ran on one side (including each check's ``data`` payload —
versions, counts, queue depths, ...), plus a chain-of-custody header (who, where,
when, tool version) and a SHA-256 self-hash so the other side can prove the file
was not altered in transit.

Snapshots are read-only artifacts: capturing one never mutates a system, and a
snapshot carries no secrets (only the facts the checks measured).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import platform
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .result import Result

if TYPE_CHECKING:
    from .context import Context

SNAPSHOT_SCHEMA = "exodia.snapshot/v1"


def _tool_version() -> str:
    try:
        from importlib.metadata import version

        return version("exodia")
    except Exception:  # noqa: BLE001 - packaging metadata may be absent in dev
        return "0.0.0-dev"


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in some containers
        return "unknown"


@dataclass
class Snapshot:
    """One side's captured facts, portable across an air-gap.

    ``side`` labels which end of the migration this is ("source" / "target").
    ``label`` is a free-form human tag (e.g. the SID or hostname) used in the
    comparison report. ``results`` are the structured Results captured on this
    side; ``meta`` is the chain-of-custody header.
    """

    side: str
    label: str
    operation: str
    results: list[Result] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def capture(
        cls,
        side: str,
        operation: str,
        results: list[Result],
        ctx: Context | None = None,
        label: str | None = None,
    ) -> Snapshot:
        """Build a snapshot from the Results a check pipeline / runbook produced."""
        lbl = label or _derive_label(ctx, side)
        meta = {
            "operator": _operator(),
            "hostname": platform.node(),
            "tool_version": _tool_version(),
            "captured_at": datetime.now(UTC).isoformat(),
            "source": getattr(ctx, "source", None) if ctx else None,
            "target": getattr(ctx, "target", None) if ctx else None,
            "sid": getattr(ctx, "sid", None) if ctx else None,
            "db_type": getattr(ctx, "db_type", None) if ctx else None,
            "host": getattr(ctx, "host", None) if ctx else None,
        }
        return cls(side=side, label=lbl, operation=operation, results=list(results), meta=meta)

    # -- serialisation ------------------------------------------------------ #

    def to_dict(self) -> dict:
        """Serialise to a plain dict with a stable self-hash of the payload.

        The hash covers everything except the hash field itself, so any edit to
        the captured facts, side, operation or header is detectable by the other
        side via :func:`verify_snapshot`.
        """
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "side": self.side,
            "label": self.label,
            "operation": self.operation,
            "meta": self.meta,
            "results": [r.model_dump(mode="json") for r in self.results],
        }
        payload["sha256"] = _hash_payload(payload)
        return payload

    def write(self, path: Path | str) -> Path:
        """Write the snapshot as pretty JSON and return the path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return p

    @classmethod
    def read(cls, path: Path | str) -> Snapshot:
        """Load a snapshot from disk (does NOT verify — call verify_snapshot)."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Snapshot:
        results = [Result.model_validate(r) for r in data.get("results", [])]
        return cls(
            side=data.get("side", "?"),
            label=data.get("label", "?"),
            operation=data.get("operation", ""),
            results=results,
            meta=data.get("meta", {}),
        )

    # -- convenience -------------------------------------------------------- #

    def by_name(self) -> dict[str, Result]:
        """Index the captured results by check name for pairing across sides."""
        return {r.name: r for r in self.results}


def _derive_label(ctx: Context | None, side: str) -> str:
    if ctx is None:
        return side
    if side == "source":
        return str(getattr(ctx, "source", None) or getattr(ctx, "sid", None) or "source")
    return str(getattr(ctx, "target", None) or getattr(ctx, "sid", None) or "target")


def _hash_payload(payload: dict) -> str:
    """Deterministic SHA-256 over the payload minus its own hash field."""
    clone = {k: v for k, v in payload.items() if k != "sha256"}
    encoded = json.dumps(clone, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_snapshot(path: Path | str) -> list[str]:
    """Re-hash a snapshot file and report problems (empty list => intact).

    The receiving side runs this before trusting a snapshot carried across the
    air-gap, exactly like ``exodia evidence verify`` does for a local bundle.
    """
    p = Path(path)
    if not p.is_file():
        return [f"snapshot file not found: {p}"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    if data.get("schema") != SNAPSHOT_SCHEMA:
        return [f"unexpected schema: {data.get('schema')!r} (expected {SNAPSHOT_SCHEMA})"]
    recorded = data.get("sha256")
    if not recorded:
        return ["snapshot has no sha256 self-hash"]
    actual = _hash_payload(data)
    if actual != recorded:
        return ["hash mismatch — snapshot was altered after capture"]
    return []
