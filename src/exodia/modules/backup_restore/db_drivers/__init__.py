"""Pluggable DB restore drivers (Strategy pattern): hana, ase, ...

Public API:
    get_driver(db_type) -> DBRestoreDriver   # factory
    DBRestoreDriver, PlannedCommand          # interface types
    supported_db_types()                     # registered engine keys
"""

from __future__ import annotations

from .base import (
    DBRestoreDriver,
    PlannedCommand,
    get_driver,
    register_driver,
    supported_db_types,
)

__all__ = [
    "DBRestoreDriver",
    "PlannedCommand",
    "get_driver",
    "register_driver",
    "supported_db_types",
]
