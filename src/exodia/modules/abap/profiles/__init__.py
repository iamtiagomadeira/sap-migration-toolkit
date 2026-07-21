"""ABAP profile management — guarded actions to back up SAP profiles.

The instance/default profiles (``/sapmnt/<SID>/profile``) and, on the target,
the fundamental global directories (``/sapmnt/<SID>/global``) are backed up to a
chosen location before a migration touches them. State-changing (writes to the
backup location), so exposed as a guarded ``Action`` (dry-run → confirm →
execute → verify), not a read-only check.
"""
