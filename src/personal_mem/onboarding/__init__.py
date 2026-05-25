"""Vault-scope onboarding operations.

These are one-shot vault-seeding ops invoked from ``/onboard`` (or
directly via ``mem import``). They write across multiple projects under
one vault — distinct from per-source-type imports which live under
``personal_mem/importers/`` and operate on a single feed.
"""

from __future__ import annotations
