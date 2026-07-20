"""Tests for the evidence bundle: layout, manifest, tamper detection, attach."""

from __future__ import annotations

import json
from pathlib import Path

from exodia.core.context import Context
from exodia.core.evidence import EvidenceBundle, verify_bundle
from exodia.core.result import Result


def _ctx() -> Context:
    return Context(source="PRD", target="QAS", sid="H10", db_type="hana")


def test_bundle_layout_and_manifest(tmp_path: Path) -> None:
    b = EvidenceBundle("tenant-copy", _ctx(), root=tmp_path, operation="tenant-copy.hana.copy-tenant")
    b.open()
    b.add_results([Result.ok("c1", "fine"), Result.fail("c2", "nope")])
    out = b.close()

    assert (out / "manifest.json").is_file()
    assert (out / "run.jsonl").is_file()
    assert (out / "results.json").is_file()
    assert (out / "report.md").is_file()

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["schema"] == "exodia.evidence/v1"
    assert manifest["methodology"] == "tenant-copy"
    assert manifest["context"]["sid"] == "H10"
    assert manifest["context"]["source"] == "PRD"
    assert manifest["results_count"] == 2
    # every artifact carries a sha256
    assert all("sha256" in a and len(a["sha256"]) == 64 for a in manifest["artifacts"])


def test_path_includes_sid_and_methodology(tmp_path: Path) -> None:
    b = EvidenceBundle("backup-restore", _ctx(), root=tmp_path).open()
    b.close()
    # evidence/backup-restore/H10/<ts>/
    assert b.dir.parent.parent.name == "backup-restore"
    assert b.dir.parent.name == "H10"


def test_run_jsonl_is_append_only_events(tmp_path: Path) -> None:
    b = EvidenceBundle("tenant-copy", _ctx(), root=tmp_path).open()
    b.add_results([Result.ok("c1", "ok")])
    b.close()
    lines = (b.dir / "run.jsonl").read_text().strip().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds[0] == "run.start"
    assert "result" in kinds
    assert kinds[-1] == "run.end"


def test_verify_bundle_intact(tmp_path: Path) -> None:
    b = EvidenceBundle("tenant-copy", _ctx(), root=tmp_path).open()
    b.add_results([Result.ok("c1", "ok")])
    out = b.close()
    assert verify_bundle(out) == []


def test_verify_bundle_detects_tampering(tmp_path: Path) -> None:
    b = EvidenceBundle("tenant-copy", _ctx(), root=tmp_path).open()
    b.add_results([Result.ok("c1", "ok")])
    out = b.close()
    # tamper with results.json after sealing
    (out / "results.json").write_text("[]")
    problems = verify_bundle(out)
    assert any("hash mismatch" in p for p in problems)


def test_verify_bundle_detects_untracked_file(tmp_path: Path) -> None:
    b = EvidenceBundle("tenant-copy", _ctx(), root=tmp_path).open()
    b.add_results([Result.ok("c1", "ok")])
    out = b.close()
    (out / "artifacts" / "snuck-in.txt").write_text("added later")
    problems = verify_bundle(out)
    assert any("untracked" in p for p in problems)


def test_attach_copies_and_hashes(tmp_path: Path) -> None:
    src = tmp_path / "sapinst.log"
    src.write_text("INFO: system copy finished\n")
    b = EvidenceBundle("backup-restore", _ctx(), root=tmp_path).open()
    dest = b.attach(src, caption="SWPM install log")
    out = b.close()
    assert dest.is_file()
    assert dest.parent.name == "artifacts"
    # attachment shows up in the manifest with a hash, and bundle verifies
    manifest = json.loads((out / "manifest.json").read_text())
    paths = [a["path"] for a in manifest["artifacts"]]
    assert any("sapinst.log" in p for p in paths)
    assert verify_bundle(out) == []


def test_context_manager_finalises(tmp_path: Path) -> None:
    with EvidenceBundle("tenant-copy", _ctx(), root=tmp_path) as b:
        b.add_results([Result.ok("c1", "ok")])
    assert (b.dir / "manifest.json").is_file()


def test_runner_records_into_evidence(tmp_path: Path) -> None:
    from exodia.core.base import Check
    from exodia.core.runner import run_checks

    class _OkCheck(Check):
        name = "demo.ok"
        description = "always ok"

        def run(self, ctx: Context) -> Result:
            return Result.ok(self.name, "fine")

    b = EvidenceBundle("demo", _ctx(), root=tmp_path).open()
    results = run_checks([_OkCheck()], _ctx(), evidence=b)
    b.close(results)
    manifest = json.loads((b.dir / "manifest.json").read_text())
    assert manifest["results_count"] == 1
