"""ABAP post-activities — guarded steps to bring the target back into service.

The mirror of ramp-down, run after the copy is verified: start the target
application servers, resume the background scheduler (BTCTRNS2), unlock the
business users, and validate the system is online (SM51). All guarded Actions.
"""
