"""Compare two sides of a migration — the automated runbook diff.

A consultant's manual runbook is a table: "on the source I saw X, on the target
I see Y, do they match?". :func:`compare_snapshots` builds that table
automatically from two snapshots (or a stored snapshot vs a freshly captured
side), pairing each check by name and diffing both the verdict and the measured
``data`` payload.

The output is a list of :class:`ComparisonRow` — one per check — each carrying
the source value, the target value, and a match verdict. A row is:

* **match**   — both sides present and their comparable data agrees;
* **differ**  — both sides present but the data disagrees (the interesting case);
* **source-only** / **target-only** — a check ran on one side but not the other;
* **error**   — a side reported an ERROR/FAIL status for that check.

Comparison is deliberately data-driven: for each paired check we compare a small
set of "salient" fields pulled from the Result.data (versions, counts, statuses).
This keeps the diff meaningful (a kernel patch level, a queue depth) rather than
noisy (timestamps, hostnames).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .result import Result, Status
from .snapshot import Snapshot

# Fields in Result.data that are meaningful to compare across sides. Anything
# not in here (timestamps, host names, free-text) is ignored so the diff stays
# signal, not noise. Checks can surface any of these in their data payload.
_SALIENT_FIELDS = (
    "version",
    "source",
    "target",
    "kernel",
    "kernel_patch",
    "unicode",
    "db_system",
    "release",
    "active_status",
    "component_count",
    "source_count",
    "target_count",
    "table_count",
    "record_count",
    "queue_count",
    "count",
    "value",
    "status",
)


@dataclass
class ComparisonRow:
    """One check compared across the two sides."""

    name: str
    verdict: str  # match | differ | source-only | target-only | error
    source_value: object = None
    target_value: object = None
    detail: str = ""


@dataclass
class ComparisonReport:
    """The full cross-side diff plus an aggregate verdict."""

    source_label: str
    target_label: str
    operation: str
    rows: list[ComparisonRow] = field(default_factory=list)

    @property
    def differing(self) -> list[ComparisonRow]:
        return [r for r in self.rows if r.verdict == "differ"]

    @property
    def only_one_side(self) -> list[ComparisonRow]:
        return [r for r in self.rows if r.verdict in ("source-only", "target-only")]

    @property
    def errored(self) -> list[ComparisonRow]:
        return [r for r in self.rows if r.verdict == "error"]

    @property
    def matched(self) -> list[ComparisonRow]:
        return [r for r in self.rows if r.verdict == "match"]

    @property
    def aligned(self) -> bool:
        """True when every paired check matched and nothing errored/one-sided."""
        return all(r.verdict == "match" for r in self.rows) and bool(self.rows)

    def verdict_result(self) -> Result:
        """A synthetic Result summarising the whole comparison for evidence."""
        name = "compare.verdict"
        if not self.rows:
            return Result.skip(name, "no checks to compare")
        if self.errored:
            return Result.fail(
                name,
                f"comparison INCONCLUSIVE — {len(self.errored)} check(s) errored: "
                f"{', '.join(r.name for r in self.errored)}",
                data=self._tally(),
            )
        problems = self.differing + self.only_one_side
        if problems:
            return Result.fail(
                name,
                f"sides DIVERGE — {len(self.differing)} differing, "
                f"{len(self.only_one_side)} one-sided of {len(self.rows)} checks",
                data=self._tally(),
            )
        return Result.ok(
            name,
            f"sides ALIGNED — all {len(self.rows)} compared checks match",
            data=self._tally(),
        )

    def _tally(self) -> dict:
        return {
            "matched": len(self.matched),
            "differing": len(self.differing),
            "one_sided": len(self.only_one_side),
            "errored": len(self.errored),
            "total": len(self.rows),
            "source_label": self.source_label,
            "target_label": self.target_label,
        }


def _salient(result: Result) -> dict:
    """Extract the comparable subset of a Result's data payload."""
    return {k: result.data[k] for k in _SALIENT_FIELDS if k in result.data}


def _compare_pair(name: str, src: Result | None, tgt: Result | None) -> ComparisonRow:
    if src is None and tgt is not None:
        return ComparisonRow(name, "target-only", None, tgt.summary, "only ran on target")
    if tgt is None and src is not None:
        return ComparisonRow(name, "source-only", src.summary, None, "only ran on source")
    assert src is not None and tgt is not None  # nosec B101 - both-None excluded by caller
    if src.status is Status.ERROR or tgt.status is Status.ERROR:
        return ComparisonRow(
            name, "error", src.summary, tgt.summary, "a side errored reading this check"
        )
    src_data = _salient(src)
    tgt_data = _salient(tgt)
    if src_data or tgt_data:
        if src_data == tgt_data:
            return ComparisonRow(name, "match", src_data, tgt_data, "data agrees")
        diffs = _describe_diffs(src_data, tgt_data)
        return ComparisonRow(name, "differ", src_data, tgt_data, diffs)
    # No salient data to compare: fall back to comparing the status verdict.
    if src.status is tgt.status:
        return ComparisonRow(
            name, "match", src.status.value, tgt.status.value, "same status, no data to diff"
        )
    return ComparisonRow(
        name, "differ", src.status.value, tgt.status.value, "status differs"
    )


def _describe_diffs(src_data: dict, tgt_data: dict) -> str:
    keys = sorted(set(src_data) | set(tgt_data))
    parts = []
    for k in keys:
        s, t = src_data.get(k), tgt_data.get(k)
        if s != t:
            parts.append(f"{k}: source={s!r} target={t!r}")
    return "; ".join(parts)


def compare_snapshots(source: Snapshot, target: Snapshot) -> ComparisonReport:
    """Diff two snapshots check-by-check, returning a full comparison report.

    Either argument may actually be a freshly-captured side (a Snapshot built
    live) or one read from disk — the comparison is identical.
    """
    report = ComparisonReport(
        source_label=source.label,
        target_label=target.label,
        operation=source.operation or target.operation,
    )
    src_by = source.by_name()
    tgt_by = target.by_name()
    for name in sorted(set(src_by) | set(tgt_by)):
        report.rows.append(_compare_pair(name, src_by.get(name), tgt_by.get(name)))
    return report
