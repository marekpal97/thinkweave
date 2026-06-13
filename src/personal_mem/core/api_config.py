"""LLM + embedding provider plumbing — ``vault/config/api.yaml``.

The single user-facing surface for which provider+model personal_mem's
backfill ops (`mem enrich`, `mem hubs run`, `mem import chatgpt`,
`mem import claude-code --enrich`, `mem hubs link`) and embedding paths
talk to. Mirrors the posture of :mod:`personal_mem.acquisition.sources.priorities`:
missing file → built-in defaults, malformed YAML → defaults + warn,
canonical path only (``vault/config/api.yaml``).

Schema::

    completion:
      provider: openai            # openai | anthropic | gemini
      model: gpt-5-mini
      max_tokens: 8000
      batch_concurrency: 20       # async fan-out semaphore cap

    embeddings:
      provider: openai            # openai | sentence_transformer | litellm
      model: text-embedding-3-small

    overrides:
      hubs_run:        {model: gpt-5}
      hubs_link:       {}
      enrich:          {}
      chatgpt_import:  {model: gpt-5-mini}
      claude_code_enrich:
        provider: anthropic
        model: claude-haiku-4-5-20251001

``resolve_for_op(cfg, op_name)`` merges the global ``completion`` block
with the op's override. Override fields win; anything omitted falls
through. Listed initial ops:

    hubs_run, hubs_link, enrich, chatgpt_import, claude_code_enrich
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from personal_mem.acquisition.sources.config import _parse_simple_yaml


_API_FILENAME = "api.yaml"


# Built-in defaults — used when the user file is missing, empty, or
# malformed. Kept conservative: OpenAI defaults preserve existing-vault
# embed compatibility (no re-embed at upgrade).
DEFAULT_CONFIG: dict[str, Any] = {
    "completion": {
        "provider": "openai",
        "model": "gpt-5-mini",
        "max_tokens": 8000,
        "batch_concurrency": 20,
    },
    "embeddings": {
        "provider": "openai",
        "model": "text-embedding-3-small",
    },
    "overrides": {},
}


def api_config_path(vault_root: Path | None) -> Path | None:
    """Canonical location of api.yaml, or None if no vault is configured."""
    if vault_root is None:
        return None
    return Path(vault_root) / "config" / _API_FILENAME


def load_api_config(vault_root: Path | None) -> dict[str, Any]:
    """Return the merged api.yaml dict: defaults overlaid with user file.

    Missing file → defaults. Malformed YAML → defaults (silent, same
    posture as :func:`personal_mem.acquisition.sources.config.load_user_config`).
    """
    merged: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    if vault_root is None:
        return merged
    path = api_config_path(vault_root)
    if path is None or not path.exists():
        return merged
    try:
        user_doc = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    except ValueError:
        return merged
    if isinstance(user_doc, dict):
        _deep_merge(merged, user_doc)
    return merged


def resolve_for_op(cfg: dict[str, Any], op_name: str) -> dict[str, Any]:
    """Return the effective ``{provider, model, max_tokens, batch_concurrency}``
    for a named op.

    Merges ``completion.*`` with ``overrides.<op>.*`` — override fields
    win, anything omitted falls through. Returns a fresh dict; callers
    can mutate without leaking back into ``cfg``.
    """
    completion = cfg.get("completion") or {}
    if not isinstance(completion, dict):
        completion = {}

    overrides_root = cfg.get("overrides") or {}
    if not isinstance(overrides_root, dict):
        overrides_root = {}
    op_overrides = overrides_root.get(op_name) or {}
    if not isinstance(op_overrides, dict):
        op_overrides = {}

    effective: dict[str, Any] = {
        "provider": completion.get("provider") or DEFAULT_CONFIG["completion"]["provider"],
        "model": completion.get("model") or DEFAULT_CONFIG["completion"]["model"],
        "max_tokens": completion.get("max_tokens") or DEFAULT_CONFIG["completion"]["max_tokens"],
        "batch_concurrency": completion.get("batch_concurrency")
            or DEFAULT_CONFIG["completion"]["batch_concurrency"],
    }
    for k, v in op_overrides.items():
        if v is not None:
            effective[k] = v
    return effective


def embeddings_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return the effective ``embeddings.{provider, model}`` block."""
    block = cfg.get("embeddings") or {}
    if not isinstance(block, dict):
        block = {}
    return {
        "provider": block.get("provider") or DEFAULT_CONFIG["embeddings"]["provider"],
        "model": block.get("model") or DEFAULT_CONFIG["embeddings"]["model"],
    }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """In-place recursive dict merge — mirrors
    :func:`personal_mem.acquisition.sources.config._deep_merge`."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
