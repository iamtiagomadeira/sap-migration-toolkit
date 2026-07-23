"""ABAP post-copy cross-cutting operations (shared by every system-copy method).

Where ``post_activities`` re-opens the target for business (start servers,
resume the scheduler, unlock users, validate online), this package holds the
*post-copy consistency* steps that make a freshly copied ABAP system correct no
matter HOW it was copied — Backup & Restore, Export & Import, or HANA System
Replication. They are declared once here and reused by all methods rather than
duplicated per method:

* ``abap.post.bdls-logical-system``     — BDLS: convert logical system names
  source -> target so IDoc/ALE/RFC stop pointing at the source (TOP-10 #3).
* ``abap.post.stms-reconfigure``        — reset/reconfigure the Transport
  Management System so the copy cannot transport into the productive
  landscape (TOP-10 #4).
* ``abap.post.sgen-load-generation``    — SGEN: regenerate the ABAP loads to
  avoid startup dumps and latency (TOP-10 #10).
* ``abap.post.installation-consistency``— post-copy consistency CHECK
  (SICK/SM28, SPAU/SPDD pending, component versions).
* ``abap.post.purge-source-runtime``    — purge source-specific runtime data
  that must not live on the copy (orphan spool SP01, orphan jobs SM37,
  source-pointing RFC destinations SM59, batch-input sessions SM35).

All actions are guarded (dry-run -> confirm -> execute -> verify); BDLS and SGEN
are long-running and stream live progress through the optional monitor. The
check is strictly read-only. RFC traffic reuses the readiness ``_rfc`` plumbing.
"""
