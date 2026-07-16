"""Tests for the Java PI/PO prerequisite checks (TIA-64).

Uses a FakeRunner (subclass of the real Runner) that returns a pre-fabricated
CommandResult per command — no real SAP system needed. Each check is exercised
for a PASS and a FAIL path, and we assert that the secure-store key phrase / any
secret never leaks into a Result's summary, detail or data.
"""

from __future__ import annotations

from collections.abc import Callable

from exodia.core import Context, Status
from exodia.core.shell import CommandResult, Runner
from exodia.modules.pipo.checks.as_java_up import ASJavaUpCheck
from exodia.modules.pipo.checks.hana_java_schema import HanaJavaSchemaCheck
from exodia.modules.pipo.checks.icm_ports import IcmPortsCheck
from exodia.modules.pipo.checks.jvm_kernel_compat import JvmKernelCompatCheck
from exodia.modules.pipo.checks.rfc_jco_config import RfcJcoConfigCheck
from exodia.modules.pipo.checks.secstore_present import SecStorePresentCheck
from exodia.modules.pipo.checks.sld_reachable import SLDReachableCheck
from exodia.modules.pipo.checks.target_mapping import TargetMappingCheck

Responder = Callable[[list[str]], CommandResult]


class FakeRunner(Runner):
    """A Runner that dispatches each argv to a caller-supplied responder."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder

    def run(
        self, argv: list[str], timeout: int = 300, input_text: str | None = None
    ) -> CommandResult:
        return self._responder(argv)


class FakeContext(Context):
    """Context whose runner() returns an injected FakeRunner."""

    def set_runner(self, runner: Runner) -> None:
        object.__setattr__(self, "_fake_runner", runner)

    def runner(self) -> Runner:  # type: ignore[override]
        return self._fake_runner  # type: ignore[attr-defined,no-any-return]


def _ctx(responder: Responder, **params: object) -> FakeContext:
    ctx = FakeContext(sid="PIX", params=params)
    ctx.set_runner(FakeRunner(responder))
    return ctx


def _cr(argv: list[str], code: int = 0, out: str = "", err: str = "") -> CommandResult:
    return CommandResult(argv, code, out, err)


# --------------------------------------------------------------------------- #
# pipo.as-java-up
# --------------------------------------------------------------------------- #

_GREEN_PROCLIST = """OK
name, description, dispstatus, textstatus, starttime, elapsedtime, pid
jstart, Java Bootstrap, GREEN, Running, 2026 07 16, 10:00:00, 1234
server0, Java Server, GREEN, Running, 2026 07 16, 10:01:00, 1235
"""

_RED_PROCLIST = """OK
name, description, dispstatus, textstatus, starttime, elapsedtime, pid
jstart, Java Bootstrap, GREEN, Running, 2026 07 16, 10:00:00, 1234
server0, Java Server, RED, Stopped, , , 1235
"""


def test_as_java_up_pass() -> None:
    ctx = _ctx(lambda argv: _cr(argv, code=3, out=_GREEN_PROCLIST))
    r = ASJavaUpCheck().run(ctx)
    assert r.status is Status.PASS
    assert "GREEN" in r.summary


def test_as_java_up_fail() -> None:
    ctx = _ctx(lambda argv: _cr(argv, code=4, out=_RED_PROCLIST))
    r = ASJavaUpCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "server" in r.summary.lower()


def test_as_java_up_fail_when_sapcontrol_dead() -> None:
    ctx = _ctx(lambda argv: _cr(argv, code=1, err="connection refused"))
    r = ASJavaUpCheck().run(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# pipo.sld-reachable
# --------------------------------------------------------------------------- #


def test_sld_reachable_pass() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="200"), sld_host="sldhost")
    r = SLDReachableCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["http_code"] == "200"


def test_sld_reachable_fail() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="500"), sld_host="sldhost")
    r = SLDReachableCheck().run(ctx)
    assert r.status is Status.FAIL


def test_sld_reachable_skip_without_host() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="200"))
    r = SLDReachableCheck().run(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# pipo.secstore-present  (secret must never leak)
# --------------------------------------------------------------------------- #

_SECRET = "SuperSecretKeyPhrase123!"


def _secstore_responder(sizes: dict[str, int]) -> Responder:
    def respond(argv: list[str]) -> CommandResult:
        # stat -c %s <path>
        path = argv[-1]
        fname = path.rsplit("/", 1)[-1]
        if fname in sizes:
            return _cr(argv, out=str(sizes[fname]))
        return _cr(argv, code=1, err="stat: No such file or directory")

    return respond


def test_secstore_present_pass() -> None:
    ctx = _ctx(
        _secstore_responder({"SecStore.properties": 2048, "SecStore.key": 64}),
    )
    r = SecStorePresentCheck().run(ctx)
    assert r.status is Status.PASS


def test_secstore_present_fail_when_missing() -> None:
    ctx = _ctx(_secstore_responder({"SecStore.properties": 2048}))  # key missing
    r = SecStorePresentCheck().run(ctx)
    assert r.status is Status.FAIL
    assert "SecStore.key" in r.summary


def test_secstore_key_phrase_never_leaks() -> None:
    # An operator wrongly passes the key phrase as a param. It must never appear
    # in any part of the Result, and the check must note only the KEY NAME.
    ctx = _ctx(
        _secstore_responder({"SecStore.properties": 2048, "SecStore.key": 64}),
        key_phrase=_SECRET,
        secstore_key_phrase=_SECRET,
    )
    r = SecStorePresentCheck().run(ctx)
    blob = r.model_dump_json()
    assert _SECRET not in blob
    # the key NAMES are recorded so the operator knows to remove them
    assert "key_phrase" in r.data.get("secret_params_ignored", [])


# --------------------------------------------------------------------------- #
# pipo.target-mapping
# --------------------------------------------------------------------------- #


def test_target_mapping_pass() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="tgthost\n"), expected_host="tgthost", instance_nr="01")
    r = TargetMappingCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["instance_nr"] == "01"


def test_target_mapping_fail() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="wronghost\n"), expected_host="tgthost")
    r = TargetMappingCheck().run(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# pipo.jvm-kernel-compat
# --------------------------------------------------------------------------- #

_DW_VERSION = """
--------------------
disp+work information
--------------------
kernel release                753
kernel make variant           753_REL
compiled on                   Linux
patch number                  900
"""


def _jvm_kernel_responder(kernel_out: str, jvm_out: str) -> Responder:
    def respond(argv: list[str]) -> CommandResult:
        if argv[0] == "disp+work":
            return _cr(argv, out=kernel_out)
        if argv[0] == "sapjvm":
            return _cr(argv, out=jvm_out)
        return _cr(argv, code=127, err="not found")

    return respond


def test_jvm_kernel_compat_pass() -> None:
    ctx = _ctx(
        _jvm_kernel_responder(_DW_VERSION, "sapjvm version 8.1.079"),
        min_kernel_release=753,
        min_kernel_patch=800,
        min_jvm_version="8.1.070",
    )
    r = JvmKernelCompatCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["kernel_release"] == 753


def test_jvm_kernel_compat_fail() -> None:
    ctx = _ctx(
        _jvm_kernel_responder(_DW_VERSION, "sapjvm version 8.1.010"),
        min_kernel_release=800,  # target kernel 753 < 800 -> fail
    )
    r = JvmKernelCompatCheck().run(ctx)
    assert r.status is Status.FAIL


# --------------------------------------------------------------------------- #
# pipo.hana-java-schema  (password must never leak)
# --------------------------------------------------------------------------- #


def test_hana_java_schema_pass() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="1\n"), hana_password="hanapw123")
    r = HanaJavaSchemaCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["schema"] == "SAPPIXDB"


def test_hana_java_schema_fail_when_absent() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out="0\n"))
    r = HanaJavaSchemaCheck().run(ctx)
    assert r.status is Status.FAIL


def test_hana_password_never_leaks_on_error() -> None:
    pw = "hanapw123!"
    ctx = _ctx(
        lambda argv: _cr(argv, code=1, err=f"connect failed password={pw}"),
        hana_password=pw,
    )
    r = HanaJavaSchemaCheck().run(ctx)
    assert r.status is Status.FAIL
    assert pw not in r.model_dump_json()


# --------------------------------------------------------------------------- #
# pipo.icm-ports
# --------------------------------------------------------------------------- #

_SS_FREE = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      128    0.0.0.0:22         0.0.0.0:*
"""

_SS_HTTP_TAKEN = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0      128    0.0.0.0:22         0.0.0.0:*
LISTEN 0      128    0.0.0.0:50000      0.0.0.0:*
"""


def test_icm_ports_pass_when_free() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out=_SS_FREE), instance_nr="00")
    r = IcmPortsCheck().run(ctx)
    assert r.status is Status.PASS
    assert r.data["http_port"] == 50000


def test_icm_ports_fail_when_taken() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out=_SS_HTTP_TAKEN), instance_nr="00")
    r = IcmPortsCheck().run(ctx)
    assert r.status is Status.FAIL


def test_icm_ports_pass_when_expected_running() -> None:
    ss = """State Recv-Q Send-Q Local Address:Port Peer
LISTEN 0 128 0.0.0.0:50000 0.0.0.0:*
LISTEN 0 128 0.0.0.0:50001 0.0.0.0:*
"""
    ctx = _ctx(lambda argv: _cr(argv, out=ss), instance_nr="00", icm_expected_running=True)
    r = IcmPortsCheck().run(ctx)
    assert r.status is Status.PASS


# --------------------------------------------------------------------------- #
# pipo.rfc-jco-config
# --------------------------------------------------------------------------- #

_JCO_CONFIG = """
jco.destination.name=SAP_ABAP_BACKEND
name = SLD_DataSupplier
DestinationName: PI_CENTRAL
"""


def test_rfc_jco_config_pass() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out=_JCO_CONFIG), jco_config_path="/tmp/jco.props")
    r = RfcJcoConfigCheck().run(ctx)
    assert r.status is Status.PASS
    assert "SAP_ABAP_BACKEND" in r.data["destinations"]
    assert r.data["count"] == 3


def test_rfc_jco_config_fail_when_unreadable() -> None:
    ctx = _ctx(
        lambda argv: _cr(argv, code=1, err="cat: No such file"),
        jco_config_path="/tmp/missing.props",
    )
    r = RfcJcoConfigCheck().run(ctx)
    assert r.status is Status.FAIL


def test_rfc_jco_config_skip_without_path() -> None:
    ctx = _ctx(lambda argv: _cr(argv, out=""))
    r = RfcJcoConfigCheck().run(ctx)
    assert r.status is Status.SKIP


# --------------------------------------------------------------------------- #
# registry / discovery
# --------------------------------------------------------------------------- #


def test_all_pipo_checks_discovered() -> None:
    from exodia.core.registry import registry

    names = set(registry.checks())
    expected = {
        "pipo.as-java-up",
        "pipo.sld-reachable",
        "pipo.secstore-present",
        "pipo.target-mapping",
        "pipo.jvm-kernel-compat",
        "pipo.hana-java-schema",
        "pipo.icm-ports",
        "pipo.rfc-jco-config",
    }
    assert expected <= names


def test_pipo_kb_entries_loaded() -> None:
    from exodia.core.knowledge import lookup

    entry = lookup("secure store key phrase is invalid on target")
    assert entry is not None
    assert entry.sap_note
