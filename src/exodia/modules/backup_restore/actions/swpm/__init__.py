"""Headless orchestration of SAP Software Provisioning Manager (SWPM).

Exodia does not reimplement SWPM — it *orchestrates* ``sapinst`` in headless
(unattended) mode with an observer-mode GUI handoff by default. See
:mod:`.action` for the guarded action and :mod:`.planner` for the secret-free
command/parameter builders and log parsing.
"""
