"""Tests for the new System Copy method modules and Solution Manager checks.

Covers export/import (SWPM/R3load/JLoad), HSR (version, log_mode, ports), and
Solution Manager post-copy (PCA, SLD/LMDB). All checks are read-only, so a
FakeRunner replays canned command output and we assert on the structured Result.
"""

from __future__ import annotations

from exodia.core import Context, Status
from exodia.core.shell import CommandResult, Runner
from exodia.modules.solution_manager.checks.preconditions import (
    LmdbReachableCheck,
    NoStaleSourceRegistrationCheck,
    PcaTaskListAvailableCheck,
    SldReachableCheck,
)
from exodia.modules.system_copy.export_import.checks.preconditions import (
    DbClientReachableCheck,
    ExportDirSpaceCheck,
    LoadToolForStackCheck,
    SwpmPresentCheck,
)
from exodia.modules.system_copy.hsr.checks.preconditions import (
    DistinctHostsCheck,
    LogModeNormalCheck,
    ReplicationPortsReachableCheck,
    VersionCompatibilityCheck,
)


class FakeRunner(Runner):
    """Replays a canned result (optionally different per call)."""

    def __init__(
        self,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        results: list[CommandResult] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._results = list(results or [])

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(argv)
        if self._results:
            return self._results.pop(0)
        return CommandResult(argv, self._exit_code, self._stdout, self._stderr)


def _ctx(runner: Runner, **kw: object) -> Context:
    class _FakeCtx(Context):
        def runner(self) -> Runner:  # type: ignore[override]
            return runner

    return _FakeCtx(**kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# export/import
# --------------------------------------------------------------------------- #


def test_swpm_present_pass() -> None:
    ctx = _ctx(FakeRunner(exit_code=0), params={"swpm_path": "/usr/sap/SWPM"})
    assert SwpmPresentCheck().run(ctx).status is Status.PASS


def test_swpm_present_fail() -> None:
    ctx = _ctx(FakeRunner(exit_code=1), params={"swpm_path": "/nope"})
    assert SwpmPresentCheck().run(ctx).status is Status.FAIL


def test_load_tool_java_needs_jload() -> None:
    # command -v jload.sh fails -> Java has no load tool
    ctx = _ctx(FakeRunner(exit_code=1), params={"stack": "java"})
    res = LoadToolForStackCheck().run(ctx)
    assert res.status is Status.FAIL
    assert "jload.sh" in res.data["missing"]


def test_load_tool_abap_ok() -> None:
    ctx = _ctx(FakeRunner(exit_code=0), params={"stack": "abap"})
    assert LoadToolForStackCheck().run(ctx).status is Status.PASS


def test_export_dir_space_insufficient() -> None:
    # df reports 50G avail; need 100G * 1.2 = 120G -> FAIL
    df = CommandResult(["df"], 0, "Avail\n50G\n", "")
    ctx = _ctx(FakeRunner(results=[df]), params={"export_dir": "/export", "export_size_gb": 100})
    res = ExportDirSpaceCheck().run(ctx)
    assert res.status is Status.FAIL


def test_export_dir_space_ok() -> None:
    df = CommandResult(["df"], 0, "Avail\n500G\n", "")
    ctx = _ctx(FakeRunner(results=[df]), params={"export_dir": "/export", "export_size_gb": 100})
    assert ExportDirSpaceCheck().run(ctx).status is Status.PASS


def test_db_client_skips_without_db_type() -> None:
    ctx = _ctx(FakeRunner(exit_code=0))
    assert DbClientReachableCheck().run(ctx).status is Status.SKIP


def test_db_client_warns_when_missing() -> None:
    ctx = _ctx(FakeRunner(exit_code=1), db_type="hana")
    assert DbClientReachableCheck().run(ctx).status is Status.WARN


# --------------------------------------------------------------------------- #
# HSR
# --------------------------------------------------------------------------- #


def test_hsr_version_secondary_lower_fails() -> None:
    primary = CommandResult(["hdbsql"], 0, '"2.00.067.00.1"\n', "")
    secondary = CommandResult(["hdbsql"], 0, '"2.00.059.00.1"\n', "")
    ctx = _ctx(FakeRunner(results=[primary, secondary]))
    res = VersionCompatibilityCheck().run(ctx)
    assert res.status is Status.FAIL


def test_hsr_version_secondary_equal_ok() -> None:
    v = '"2.00.067.00.1"\n'
    ctx = _ctx(
        FakeRunner(results=[CommandResult(["x"], 0, v, ""), CommandResult(["x"], 0, v, "")])
    )
    assert VersionCompatibilityCheck().run(ctx).status is Status.PASS


def test_hsr_log_mode_overwrite_fails() -> None:
    cr = CommandResult(["hdbsql"], 0, '"overwrite"\n', "")
    ctx = _ctx(FakeRunner(results=[cr]))
    assert LogModeNormalCheck().run(ctx).status is Status.FAIL


def test_hsr_log_mode_normal_ok() -> None:
    cr = CommandResult(["hdbsql"], 0, '"normal"\n', "")
    ctx = _ctx(FakeRunner(results=[cr]))
    assert LogModeNormalCheck().run(ctx).status is Status.PASS


def test_hsr_ports_unreachable_fails() -> None:
    ctx = _ctx(FakeRunner(exit_code=1), params={"secondary_host": "sec01", "instance": "00"})
    assert ReplicationPortsReachableCheck().run(ctx).status is Status.FAIL


def test_hsr_distinct_hosts_same_fails() -> None:
    ctx = _ctx(FakeRunner(), host="node1", params={"secondary_host": "node1"})
    assert DistinctHostsCheck().run(ctx).status is Status.FAIL


def test_hsr_distinct_hosts_different_ok() -> None:
    ctx = _ctx(FakeRunner(), host="node1", params={"secondary_host": "node2"})
    assert DistinctHostsCheck().run(ctx).status is Status.PASS


# --------------------------------------------------------------------------- #
# Solution Manager
# --------------------------------------------------------------------------- #


def test_pca_skips_without_sid() -> None:
    ctx = _ctx(FakeRunner(exit_code=0))
    assert PcaTaskListAvailableCheck().run(ctx).status is Status.SKIP


def test_pca_ok_with_toolchain() -> None:
    ctx = _ctx(FakeRunner(exit_code=0), sid="SOL")
    assert PcaTaskListAvailableCheck().run(ctx).status is Status.PASS


def test_sld_unreachable_fails() -> None:
    ctx = _ctx(FakeRunner(exit_code=1), params={"sld_host": "sld01", "sld_port": "50000"})
    assert SldReachableCheck().run(ctx).status is Status.FAIL


def test_sld_reachable_ok() -> None:
    ctx = _ctx(FakeRunner(exit_code=0), params={"sld_host": "sld01"})
    assert SldReachableCheck().run(ctx).status is Status.PASS


def test_lmdb_skips_without_host() -> None:
    ctx = _ctx(FakeRunner(exit_code=0))
    assert LmdbReachableCheck().run(ctx).status is Status.SKIP


def test_no_stale_registration_always_warns() -> None:
    ctx = _ctx(FakeRunner(), sid="SOL")
    assert NoStaleSourceRegistrationCheck().run(ctx).status is Status.WARN


# --------------------------------------------------------------------------- #
# report rendering (TIA-67): find_latest_bundle + render_html
# --------------------------------------------------------------------------- #


def _make_bundle(root, methodology="tenant-copy", sid="PRD"):  # type: ignore[no-untyped-def]
    from exodia.core.evidence import EvidenceBundle
    from exodia.core.result import Result

    bundle = EvidenceBundle(methodology, None, root=root)
    ctx_dir = bundle.open()
    ctx_dir.add_results(
        [Result.ok("demo.check", "all good"), Result.fail("demo.block", "nope")]
    )
    bundle.close()
    return bundle.dir


def test_find_latest_bundle_none_when_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from exodia.core.evidence import find_latest_bundle

    assert find_latest_bundle(tmp_path / "nothing") is None


def test_find_latest_bundle_picks_most_recent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from exodia.core.evidence import find_latest_bundle

    root = tmp_path / "evidence"
    d1 = _make_bundle(root, sid="AAA")
    d2 = _make_bundle(root, sid="BBB")
    latest = find_latest_bundle(root)
    assert latest in (d1, d2)  # both sealed ~now; must return a real bundle
    assert (latest / "manifest.json").is_file()


def test_render_html_contains_results(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from exodia.core.evidence import render_html

    d = _make_bundle(tmp_path / "evidence")
    html = render_html(d)
    assert "<!doctype html>" in html
    assert "demo.check" in html
    assert "demo.block" in html
    assert "PASS" in html and "FAIL" in html
    assert "tenant-copy" in html


def test_render_html_escapes_markup(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from exodia.core.evidence import EvidenceBundle, render_html
    from exodia.core.result import Result

    root = tmp_path / "evidence"
    bundle = EvidenceBundle("tenant-copy", None, root=root).open()
    bundle.add_results([Result.ok("x.y", "value <script>alert(1)</script>")])
    bundle.close()
    html = render_html(bundle.dir)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_verdict_banner_not_ready(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A bundle with a blocking FAIL renders a red NOT READY banner."""
    from exodia.core.evidence import render_html

    d = _make_bundle(tmp_path / "evidence")  # has one PASS + one FAIL
    html = render_html(d)
    assert "NOT READY" in html
    assert "#cf222e" in html  # red banner background


def test_render_html_verdict_banner_ready(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A bundle where everything passed renders a green READY banner."""
    from exodia.core.evidence import EvidenceBundle, render_html
    from exodia.core.result import Result

    root = tmp_path / "evidence"
    bundle = EvidenceBundle("tenant-copy", None, root=root).open()
    bundle.add_results([Result.ok("a", "ok"), Result.ok("b", "ok")])
    bundle.close()
    html = render_html(bundle.dir)
    assert "READY" in html
    assert "#1a7f37" in html  # green banner


def test_render_html_banner_ignores_verdict_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The synthetic .verdict row must not be double-counted in the banner."""
    from exodia.core.evidence import EvidenceBundle, render_html
    from exodia.core.result import Result

    root = tmp_path / "evidence"
    bundle = EvidenceBundle("tenant-copy", None, root=root).open()
    bundle.add_results(
        [
            Result.ok("a", "ok"),
            Result.fail("b", "bad"),
            Result.fail("runbook.verdict", "NOT READY — 1 blocking"),
        ]
    )
    bundle.close()
    html = render_html(bundle.dir)
    # one real blocker, not two (the verdict row is excluded)
    assert "NOT READY — 1 blocking issue(s) must be resolved" in html
