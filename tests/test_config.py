"""Tests for the formal Pydantic config schema (TIA-55).

Covers: ExodiaConfig validation (good + bad inputs), from_file loading YAML and
TOML, friendly error messages, the escape_hatch, and the Context bridge that keeps
ctx.get(...) working. The 172 pre-existing tests must keep passing (backward compat).
"""

from __future__ import annotations

import pytest

from exodia.core import ConfigError, Context, ExodiaConfig
from exodia.core.config import EscapeHatch


# --------------------------------------------------------------------------- #
# Validation of direct construction
# --------------------------------------------------------------------------- #
def test_defaults_are_safe() -> None:
    cfg = ExodiaConfig()
    assert cfg.dry_run is True  # safe default: nothing executes
    assert cfg.assume_yes is False
    assert cfg.port == 22
    assert cfg.db_type is None
    assert isinstance(cfg.escape_hatch, EscapeHatch)
    assert cfg.escape_hatch.skip_checks == []


def test_good_input_validates() -> None:
    cfg = ExodiaConfig(db_type="hana", sid="PRD", source="PRD", target="QAS")
    assert cfg.db_type == "hana"
    assert cfg.target == "QAS"


def test_ase_is_valid_db_type() -> None:
    assert ExodiaConfig(db_type="ase").db_type == "ase"


def test_bad_db_type_rejected_with_clear_message() -> None:
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_dict({"db_type": "oracle"}, source="test.yaml")
    msg = str(ei.value)
    assert "db_type" in msg
    assert "oracle" in msg  # shows what the user actually passed
    assert "invalid Exodia config in test.yaml" in msg


def test_unknown_field_rejected() -> None:
    # extra="forbid" — typos should not be silently swallowed.
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_dict({"targett": "QAS"}, source="typo.yaml")
    assert "targett" in str(ei.value)


def test_bad_port_type_rejected() -> None:
    with pytest.raises(ConfigError):
        ExodiaConfig.from_dict({"port": "not-a-number"})


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #
def test_escape_hatch_accepts_custom_params() -> None:
    cfg = ExodiaConfig(
        escape_hatch={
            "custom_recover_sql": "RECOVER DATABASE UNTIL TIMESTAMP '2026-07-16 10:00:00'",
            "pre_hooks": ["df -h /hana/data"],
            "post_hooks": ["systemctl status sapinit"],
            "skip_checks": ["hana.free-space"],
            "extra_params": {"userstore_key": "MIGKEY", "headroom_pct": 25},
        }
    )
    assert cfg.escape_hatch.skip_checks == ["hana.free-space"]
    assert cfg.escape_hatch.extra_params["userstore_key"] == "MIGKEY"
    assert cfg.escape_hatch.custom_recover_sql.startswith("RECOVER DATABASE")


def test_escape_hatch_rejects_unknown_key() -> None:
    with pytest.raises(ConfigError):
        ExodiaConfig.from_dict({"escape_hatch": {"bogus_key": 1}})


# --------------------------------------------------------------------------- #
# from_file: YAML + TOML
# --------------------------------------------------------------------------- #
def test_from_file_loads_yaml(tmp_path) -> None:
    p = tmp_path / "exodia.yaml"
    p.write_text(
        "db_type: hana\n"
        "sid: PRD\n"
        "target: QAS\n"
        "escape_hatch:\n"
        "  extra_params:\n"
        "    userstore_key: SYSTEMDB\n"
    )
    cfg = ExodiaConfig.from_file(p)
    assert cfg.db_type == "hana"
    assert cfg.target == "QAS"
    assert cfg.escape_hatch.extra_params["userstore_key"] == "SYSTEMDB"


def test_from_file_loads_toml(tmp_path) -> None:
    p = tmp_path / "exodia.toml"
    p.write_text(
        'db_type = "ase"\n'
        'sid = "SYB"\n'
        "[escape_hatch]\n"
        'skip_checks = ["ase.capacity"]\n'
    )
    cfg = ExodiaConfig.from_file(p)
    assert cfg.db_type == "ase"
    assert cfg.escape_hatch.skip_checks == ["ase.capacity"]


def test_from_file_missing_raises_friendly() -> None:
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_file("/nonexistent/exodia.yaml")
    assert "not found" in str(ei.value)


def test_from_file_bad_extension_raises(tmp_path) -> None:
    p = tmp_path / "exodia.json"
    p.write_text("{}")
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_file(p)
    assert "unsupported config extension" in str(ei.value)


def test_from_file_malformed_yaml_raises_friendly(tmp_path) -> None:
    p = tmp_path / "exodia.yaml"
    p.write_text("db_type: hana\n  bad: : indent\n")
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_file(p)
    assert "could not parse YAML" in str(ei.value)


def test_from_file_non_mapping_raises(tmp_path) -> None:
    p = tmp_path / "exodia.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError) as ei:
        ExodiaConfig.from_file(p)
    assert "must contain a mapping" in str(ei.value)


def test_example_yaml_is_valid() -> None:
    """The shipped exodia.example.yaml must itself validate."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example = root / "exodia.example.yaml"
    if example.exists():  # keep test robust if repo layout changes
        cfg = ExodiaConfig.from_file(example)
        assert cfg.db_type in ("hana", "ase")


# --------------------------------------------------------------------------- #
# Context bridge — backward compatibility with ctx.get(...)
# --------------------------------------------------------------------------- #
def test_context_from_config_exposes_typed_via_get() -> None:
    cfg = ExodiaConfig(db_type="hana", sid="PRD", source="PRD", target="QAS")
    ctx = Context.from_config(cfg)
    # Typed attrs land on the Context directly...
    assert ctx.db_type == "hana"
    assert ctx.target == "QAS"
    # ...and are also reachable via the escape-hatch ctx.get(...) API.
    assert ctx.get("db_type") == "hana"
    assert ctx.get("sid") == "PRD"


def test_context_from_config_exposes_extra_params_via_get() -> None:
    cfg = ExodiaConfig(
        db_type="hana",
        escape_hatch={"extra_params": {"userstore_key": "MIGKEY", "headroom_pct": 25}},
    )
    ctx = Context.from_config(cfg)
    assert ctx.get("userstore_key") == "MIGKEY"
    assert ctx.get("headroom_pct") == 25
    assert ctx.get("missing", "fallback") == "fallback"


def test_context_from_config_wires_hooks_and_skips() -> None:
    cfg = ExodiaConfig(
        escape_hatch={
            "pre_hooks": ["a"],
            "post_hooks": ["b"],
            "skip_checks": ["hana.free-space"],
        }
    )
    ctx = Context.from_config(cfg)
    assert ctx.pre_hooks == ["a"]
    assert ctx.post_hooks == ["b"]
    assert ctx.skip_checks == ["hana.free-space"]


def test_context_from_config_custom_recover_sql_via_get() -> None:
    sql = "RECOVER DATABASE UNTIL TIMESTAMP '2026-07-16 10:00:00'"
    cfg = ExodiaConfig(escape_hatch={"custom_recover_sql": sql})
    ctx = Context.from_config(cfg)
    assert ctx.get("custom_recover_sql") == sql


def test_context_from_file_end_to_end(tmp_path) -> None:
    p = tmp_path / "exodia.yaml"
    p.write_text("db_type: hana\ntarget: QAS\ndry_run: false\nassume_yes: true\n")
    ctx = Context.from_file(p)
    assert ctx.db_type == "hana"
    assert ctx.dry_run is False
    assert ctx.assume_yes is True
    assert ctx.get("target") == "QAS"


def test_context_from_file_invalid_raises_config_error(tmp_path) -> None:
    p = tmp_path / "exodia.yaml"
    p.write_text("db_type: postgres\n")
    with pytest.raises(ConfigError):
        Context.from_file(p)
