"""Centralized provider API key loading.

Replaces the ad-hoc ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` /
``GOOGLE_API_KEY`` reads scattered across :mod:`enrich`,
:mod:`importers.chatgpt`, :mod:`operations.news_triage`,
:mod:`operations.hubs_batch`, :mod:`onboarding.enrich_batch`, and
:mod:`surfaces.cli._hubs_link`. One canonical lookup path so the user
configures the key once and every entry point sees it.

Lookup order (first non-empty wins):

  1. ``os.environ`` (already-exported key)
  2. ``.env`` at ``$THINKWEAVE_VAULT/.env`` (vault-scoped secrets)
  3. ``.env`` at ``Path.cwd()/.env`` (project-local development)
  4. ``.env`` at the thinkweave project root (back-compat with
     pre-consolidation behaviour)

The ``.env`` loader is best-effort — malformed lines are skipped, parse
errors swallowed. It NEVER overrides a key that's already in
``os.environ``.

Provider → env var name mapping:

  openai     → OPENAI_API_KEY
  anthropic  → ANTHROPIC_API_KEY
  gemini     → GEMINI_API_KEY, falling back to GOOGLE_API_KEY (legacy)
"""

from __future__ import annotations

import os
from pathlib import Path


# Provider → ordered list of env var names. First non-empty value wins.
# Gemini accepts both the new (``GEMINI_API_KEY``) and legacy
# (``GOOGLE_API_KEY``) names — preferring the new name nudges users
# toward the canonical export without breaking existing vaults.
_PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


# Thinkweave project root — the back-compat fallback for developers
# running CLI commands from a checkout where the .env lives at the
# repo root rather than the vault. Module-level so tests can patch it.
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent


def get_provider_key(provider: str) -> str | None:
    """Return the API key for ``provider``, or ``None`` if unset.

    Reads ``os.environ`` first, then loads ``.env`` from the well-known
    locations (vault → cwd → thinkweave project root) only if the key
    isn't already exported. The loader populates ``os.environ`` for
    subsequent callers in the same process.

    Unknown provider names return ``None`` (the wrapper should raise a
    typed error upstream rather than letting an unknown key sneak by).
    """
    var_names = _PROVIDER_ENV_VARS.get(provider.lower())
    if not var_names:
        return None

    # Pass 1: live env.
    for var in var_names:
        val = os.environ.get(var)
        if val:
            return val

    # Pass 2: .env files. Populates os.environ as a side effect so
    # downstream code (SDKs that read env directly) sees the key.
    _load_env_files()
    for var in var_names:
        val = os.environ.get(var)
        if val:
            return val

    return None


def _load_env_files() -> None:
    """Best-effort `.env` loader — vault → cwd → project root.

    Silent on missing files and parse errors. Does NOT overwrite keys
    already in ``os.environ``. Idempotent: re-running is a no-op once
    the env is populated.
    """
    candidates: list[Path] = []

    # PERSONAL_MEM_VAULT: pre-rename migration fallback (→ thinkweave 2026-06-13).
    vault = os.environ.get("THINKWEAVE_VAULT") or os.environ.get("PERSONAL_MEM_VAULT")
    if vault:
        candidates.append(Path(vault) / ".env")

    candidates.append(Path.cwd() / ".env")

    # Back-compat with ``enrich.load_openai_api_key()`` — the thinkweave
    # project root. Module-level constant so tests can monkeypatch it.
    candidates.append(_PROJECT_ROOT / ".env")

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if not resolved.is_file():
                continue
            for raw in resolved.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue
