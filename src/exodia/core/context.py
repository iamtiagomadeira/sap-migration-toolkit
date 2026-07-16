"""Execution context — carries connection + parameters into checks/actions.

Stateless by design: a Context is built per-invocation from CLI args and an
optional config file, passed down, and discarded. No persistence, no memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from .shell import Runner, SSHRunner

if TYPE_CHECKING:
    from .config import ExodiaConfig


class Context(BaseModel):
    """Everything a check or action needs to run. Immutable-ish per run."""

    model_config = {"arbitrary_types_allowed": True}

    # Target selection
    host: str | None = None  # None => run locally
    user: str | None = None
    port: int = 22
    key_filename: str | None = None
    known_hosts: str | None = None

    # SAP / DB parameters
    db_type: str | None = None  # "hana" | "ase" | ...
    sid: str | None = None
    source: str | None = None
    target: str | None = None
    system_type: str | None = None  # "abap" | "java" | "pipo" | ...

    # Behaviour flags
    dry_run: bool = True  # SAFE DEFAULT: nothing executes unless explicitly disabled
    assume_yes: bool = False
    skip_checks: list[str] = Field(default_factory=list)

    # Free-form overrides (the escape hatch — pre/post hooks, custom params).
    params: dict[str, Any] = Field(default_factory=dict)
    pre_hooks: list[str] = Field(default_factory=list)
    post_hooks: list[str] = Field(default_factory=list)

    def runner(self) -> Runner | SSHRunner:
        """Return the right executor: local Runner or remote SSHRunner."""
        if self.host is None:
            return Runner()
        if not self.user:
            raise ValueError("remote host requires --user")
        return SSHRunner(
            host=self.host,
            user=self.user,
            port=self.port,
            key_filename=self.key_filename,
            known_hosts=self.known_hosts,
        )

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    def get(self, key: str, default: Any = None) -> Any:
        """Read a param from the escape-hatch overrides."""
        return self.params.get(key, default)

    @classmethod
    def from_config(cls, config: ExodiaConfig) -> Context:
        """Build a Context from a validated :class:`ExodiaConfig`.

        Typed fields map onto the Context's own attributes; everything else
        (escape-hatch extras, custom SQL, ...) is flattened into ``params`` so
        it stays reachable through the unchanged ``ctx.get(...)`` escape hatch.
        """
        return cls(
            host=config.host,
            user=config.user,
            port=config.port,
            key_filename=config.key_filename,
            known_hosts=config.known_hosts,
            db_type=config.db_type,
            sid=config.sid,
            source=config.source,
            target=config.target,
            system_type=config.system_type,
            dry_run=config.dry_run,
            assume_yes=config.assume_yes,
            skip_checks=list(config.escape_hatch.skip_checks),
            pre_hooks=list(config.escape_hatch.pre_hooks),
            post_hooks=list(config.escape_hatch.post_hooks),
            params=config.as_params(),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> Context:
        """Load a Context from a validated ``exodia.yaml`` / ``exodia.toml`` file.

        Delegates to :class:`ExodiaConfig` so the file is schema-validated and any
        problem surfaces as a friendly ``ConfigError`` instead of a raw traceback.
        """
        from .config import ExodiaConfig

        return cls.from_config(ExodiaConfig.from_file(path))
