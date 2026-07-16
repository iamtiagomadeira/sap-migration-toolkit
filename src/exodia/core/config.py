"""Formal, typed configuration schema for Exodia (TIA-55).

Historically every parameter reached a check/action ad-hoc via ``ctx.get(...)``.
That is flexible but undocumented and unvalidated. :class:`ExodiaConfig` gives the
common parameters a typed, validated home while keeping a free-form ``escape_hatch``
for the 20% of runs that need something bespoke.

Design:

* ``ExodiaConfig`` — a Pydantic v2 model with typed fields for the parameters that
  show up across the HANA / ASE / PI-PO modules, plus an :class:`EscapeHatch` block
  for custom SQL, pre/post hooks, skipped checks, and arbitrary extra params.
* ``ExodiaConfig.from_file(path)`` — load & validate a ``exodia.yaml`` / ``exodia.toml``
  file, turning Pydantic's noisy ``ValidationError`` into a friendly, actionable message.
* Bridge to :class:`~exodia.core.context.Context` (see ``Context.from_config``) so the
  existing ``ctx.get(...)`` escape-hatch keeps working unchanged — full backward compat.

IP rule: nothing here embeds client data or SAP Note text.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DbType = Literal["hana", "ase"]


class ConfigError(ValueError):
    """Raised when a config file is missing, unparseable, or fails validation.

    Carries a human-friendly, multi-line message suitable for printing straight
    to the user — no need to show them a raw Pydantic traceback.
    """


class EscapeHatch(BaseModel):
    """The deliberate 20% override block.

    Everything here is optional and free-form: it is how a run steps outside the
    opinionated defaults without us having to model every conceivable parameter.
    """

    model_config = ConfigDict(extra="forbid")

    #: raw SQL to run instead of the generated recovery statement
    custom_recover_sql: str | None = None
    #: shell-safe argv-style commands (or documented step ids) to run before the operation
    pre_hooks: list[str] = Field(default_factory=list)
    #: same, but after the operation
    post_hooks: list[str] = Field(default_factory=list)
    #: dotted names of checks to skip (e.g. ["hana.free-space"])
    skip_checks: list[str] = Field(default_factory=list)
    #: anything else — reachable from checks/actions via ctx.get("<key>")
    extra_params: dict[str, Any] = Field(default_factory=dict)


class ExodiaConfig(BaseModel):
    """Formal, validated configuration for an Exodia invocation.

    Typed fields cover the common parameters; :attr:`escape_hatch` covers the rest.
    Build a :class:`~exodia.core.context.Context` from this via ``Context.from_config``.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Target selection -------------------------------------------------- #
    host: str | None = None  # None => run locally
    user: str | None = None
    port: int = 22
    key_filename: str | None = None
    known_hosts: str | None = None

    # --- SAP / DB parameters ---------------------------------------------- #
    db_type: DbType | None = None
    sid: str | None = None
    source: str | None = None
    target: str | None = None
    system_type: str | None = None  # "abap" | "java" | "pipo" | ...
    instance_number: str | None = None  # e.g. "00"
    inifile: str | None = None  # SWPM inifile.params path
    product_id: str | None = None  # SWPM product id

    # --- Behaviour flags --------------------------------------------------- #
    dry_run: bool = True  # SAFE DEFAULT: nothing executes unless explicitly disabled
    assume_yes: bool = False

    # --- The escape hatch -------------------------------------------------- #
    escape_hatch: EscapeHatch = Field(default_factory=EscapeHatch)

    # ---------------------------------------------------------------------- #
    # Loading
    # ---------------------------------------------------------------------- #
    @classmethod
    def from_file(cls, path: str | Path) -> ExodiaConfig:
        """Load and validate a config file (``.yaml`` / ``.yml`` / ``.toml``).

        Raises :class:`ConfigError` with a friendly message on any problem —
        missing file, malformed syntax, or schema validation failure.
        """
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")

        suffix = p.suffix.lower()
        try:
            if suffix in (".yaml", ".yml"):
                data = yaml.safe_load(p.read_text()) or {}
            elif suffix == ".toml":
                data = tomllib.loads(p.read_text())
            else:
                raise ConfigError(
                    f"unsupported config extension '{suffix}' for {p}; "
                    "use .yaml, .yml, or .toml"
                )
        except yaml.YAMLError as exc:
            raise ConfigError(f"could not parse YAML config {p}:\n  {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"could not parse TOML config {p}:\n  {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"config file {p} must contain a mapping at the top level, "
                f"got {type(data).__name__}"
            )

        return cls.from_dict(data, source=str(p))

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "<dict>") -> ExodiaConfig:
        """Validate an already-parsed mapping, raising a friendly :class:`ConfigError`."""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(exc, source)) from exc

    # ---------------------------------------------------------------------- #
    # Bridge to the flat param dict consumed by ctx.get(...)
    # ---------------------------------------------------------------------- #
    def as_params(self) -> dict[str, Any]:
        """Flatten typed + escape-hatch values into the dict ``ctx.get()`` reads.

        Escape-hatch ``extra_params`` are folded in last so a user can always
        override a typed default from the free-form block if they must.
        """
        params: dict[str, Any] = {}
        for key in (
            "db_type",
            "sid",
            "source",
            "target",
            "system_type",
            "instance_number",
            "inifile",
            "product_id",
        ):
            value = getattr(self, key)
            if value is not None:
                params[key] = value

        if self.escape_hatch.custom_recover_sql is not None:
            params["custom_recover_sql"] = self.escape_hatch.custom_recover_sql

        params.update(self.escape_hatch.extra_params)
        return params


def _format_validation_error(exc: ValidationError, source: str) -> str:
    """Turn a Pydantic ValidationError into a friendly, actionable message."""
    lines = [f"invalid Exodia config in {source}:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        msg = err["msg"]
        got = err.get("input")
        detail = f"  - {loc}: {msg}"
        if got is not None and err["type"] not in ("missing",):
            detail += f" (got: {got!r})"
        lines.append(detail)
    lines.append("  See exodia.example.yaml for the full list of valid fields.")
    return "\n".join(lines)
