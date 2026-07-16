"""Pluggable DB restore driver interface (Strategy pattern).

Each supported database engine (HANA, ASE, ...) provides a driver that knows how
to *plan*, *restore* and *verify* a database recovery. The guarded
``RestoreDatabaseAction`` selects the right driver at runtime via
:func:`get_driver` and delegates to it — the action itself stays engine-agnostic.

Hard rules inherited from the core:
  * Commands are always ``list[str]`` (argv). Never ``shell=True``.
  * Secrets (passwords) never appear in argv, and therefore never in logs.
    Drivers authenticate via the HANA secure user store key or feed SQL via
    stdin, keeping credentials out of the visible command line.
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass

from exodia.core import Context, Result


@dataclass
class PlannedCommand:
    """A single command the driver would run, in a form safe to display.

    ``argv`` never contains secrets. When a SQL batch is fed to the client via
    stdin it lives in ``input_text`` (also secret-free) and ``describe`` carries
    a human-readable one-line summary for the dry-run report.
    """

    argv: list[str]
    input_text: str | None = None
    describe: str = ""

    @property
    def display(self) -> str:
        """Readable single line for the dry-run plan (secret-free)."""
        rendered = " ".join(shlex.quote(a) for a in self.argv)
        if self.describe:
            return f"{rendered}  # {self.describe}"
        return rendered


class DBRestoreDriver(ABC):
    """Strategy interface for engine-specific database restore + verification."""

    #: engine key, e.g. "hana" | "ase"
    db_type: str = ""

    @abstractmethod
    def plan(self, ctx: Context) -> list[PlannedCommand]:
        """Return the ordered commands ``restore()`` would run — no side effects.

        Used by the action's ``dry_run`` to describe exactly what would happen
        without touching the target.
        """
        ...

    @abstractmethod
    def restore(self, ctx: Context) -> Result:
        """Execute the recovery. Only called after dry-run + confirmation."""
        ...

    @abstractmethod
    def verify(self, ctx: Context) -> Result:
        """Confirm the database is online / recovered."""
        ...


# --- registry / factory -------------------------------------------------------

_DRIVERS: dict[str, type[DBRestoreDriver]] = {}


def register_driver(cls: type[DBRestoreDriver]) -> type[DBRestoreDriver]:
    """Class decorator: register a driver under its ``db_type`` key."""
    key = (cls.db_type or "").strip().lower()
    if not key:
        raise ValueError(f"{cls.__name__} must define a non-empty db_type")
    _DRIVERS[key] = cls
    return cls


def get_driver(db_type: str | None) -> DBRestoreDriver:
    """Return an instance of the driver for ``db_type``.

    Raises a clear ValueError for an unknown or missing engine, listing the
    supported ones.
    """
    key = (db_type or "").strip().lower()
    if not key:
        raise ValueError("db_type is required (e.g. 'hana' or 'ase'); pass --db-type on the CLI")
    cls = _DRIVERS.get(key)
    if cls is None:
        supported = ", ".join(sorted(_DRIVERS)) or "(none registered)"
        raise ValueError(f"unknown db_type {db_type!r}; supported: {supported}")
    return cls()


def supported_db_types() -> list[str]:
    """Sorted list of registered engine keys."""
    return sorted(_DRIVERS)


# Register the built-in drivers. Imported here so a single import of this module
# (or of the package) wires up the factory. Placed at the bottom to avoid a
# circular import (the driver modules import from this one).
def _register_builtin_drivers() -> None:
    from . import ase, hana  # noqa: F401  (import triggers @register_driver)


_register_builtin_drivers()
