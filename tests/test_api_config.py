"""Tests for ``core/api_config.py`` — api.yaml loader + resolver.

Covers missing-file, malformed-file, valid-file paths plus the typed
accessors (``resolve_for_op``, ``embeddings_config``). Mirrors the
shape of ``tests/test_priorities.py``.
"""

from __future__ import annotations

from pathlib import Path

from personal_mem.core.api_config import (
    DEFAULT_CONFIG,
    api_config_path,
    embeddings_config,
    load_api_config,
    resolve_for_op,
)


# ---- path + missing-file -----------------------------------------------------


def test_api_config_path_returns_canonical(tmp_path: Path):
    assert api_config_path(tmp_path) == tmp_path / "config" / "api.yaml"


def test_api_config_path_none_vault_returns_none():
    assert api_config_path(None) is None


def test_load_api_config_none_vault_returns_defaults():
    cfg = load_api_config(None)
    assert cfg["completion"]["provider"] == "openai"
    assert cfg["completion"]["model"] == "gpt-5-mini"
    assert cfg["embeddings"]["provider"] == "openai"


def test_load_api_config_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_api_config(tmp_path)
    assert cfg["completion"]["provider"] == "openai"
    assert cfg["completion"]["batch_concurrency"] == 20
    assert cfg["embeddings"]["model"] == "text-embedding-3-small"


def test_load_api_config_malformed_yaml_returns_defaults(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        # block-style list — the personal_mem reader rejects this
        # mid-document with a ValueError, which the loader swallows.
        "completion:\n  - bad\n  - shape\n",
        encoding="utf-8",
    )
    cfg = load_api_config(tmp_path)
    # Falls back to defaults silently.
    assert cfg["completion"]["provider"] == "openai"


# ---- valid-file overlay ------------------------------------------------------


def test_load_api_config_overlays_completion(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "completion:\n"
        "  provider: anthropic\n"
        "  model: claude-haiku-4-5-20251001\n"
        "  max_tokens: 4000\n",
        encoding="utf-8",
    )
    cfg = load_api_config(tmp_path)
    assert cfg["completion"]["provider"] == "anthropic"
    assert cfg["completion"]["model"] == "claude-haiku-4-5-20251001"
    assert cfg["completion"]["max_tokens"] == 4000
    # Field user didn't set survives from defaults.
    assert cfg["completion"]["batch_concurrency"] == 20


def test_load_api_config_overlays_embeddings(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "embeddings:\n"
        "  provider: sentence_transformer\n"
        "  model: all-MiniLM-L6-v2\n",
        encoding="utf-8",
    )
    cfg = load_api_config(tmp_path)
    assert cfg["embeddings"]["provider"] == "sentence_transformer"
    assert cfg["embeddings"]["model"] == "all-MiniLM-L6-v2"
    # Completion block still defaults.
    assert cfg["completion"]["provider"] == "openai"


def test_load_api_config_parses_overrides(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "overrides:\n"
        "  hubs_run:\n"
        "    model: gpt-5\n"
        "  claude_code_enrich:\n"
        "    provider: anthropic\n"
        "    model: claude-haiku-4-5-20251001\n",
        encoding="utf-8",
    )
    cfg = load_api_config(tmp_path)
    assert cfg["overrides"]["hubs_run"]["model"] == "gpt-5"
    assert cfg["overrides"]["claude_code_enrich"]["provider"] == "anthropic"


# ---- resolve_for_op ----------------------------------------------------------


def test_resolve_for_op_falls_through_when_no_override():
    cfg = {"completion": {"provider": "openai", "model": "gpt-5-mini", "max_tokens": 8000}}
    eff = resolve_for_op(cfg, "enrich")
    assert eff["provider"] == "openai"
    assert eff["model"] == "gpt-5-mini"
    assert eff["max_tokens"] == 8000
    # batch_concurrency comes from defaults when omitted entirely.
    assert eff["batch_concurrency"] == 20


def test_resolve_for_op_applies_override(tmp_path: Path):
    cfg = {
        "completion": {"provider": "openai", "model": "gpt-5-mini", "max_tokens": 8000},
        "overrides": {"hubs_run": {"model": "gpt-5"}},
    }
    eff = resolve_for_op(cfg, "hubs_run")
    assert eff["provider"] == "openai"          # falls through
    assert eff["model"] == "gpt-5"              # overridden
    assert eff["max_tokens"] == 8000            # falls through


def test_resolve_for_op_full_provider_swap():
    cfg = {
        "completion": {"provider": "openai", "model": "gpt-5-mini"},
        "overrides": {
            "claude_code_enrich": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
            },
        },
    }
    eff = resolve_for_op(cfg, "claude_code_enrich")
    assert eff["provider"] == "anthropic"
    assert eff["model"] == "claude-haiku-4-5-20251001"


def test_resolve_for_op_unknown_op_returns_completion_defaults():
    cfg = {"completion": {"provider": "openai", "model": "gpt-5-mini"}}
    eff = resolve_for_op(cfg, "no_such_op")
    assert eff["provider"] == "openai"
    assert eff["model"] == "gpt-5-mini"


def test_resolve_for_op_handles_empty_cfg():
    eff = resolve_for_op({}, "enrich")
    assert eff["provider"] == DEFAULT_CONFIG["completion"]["provider"]
    assert eff["model"] == DEFAULT_CONFIG["completion"]["model"]
    assert eff["max_tokens"] == DEFAULT_CONFIG["completion"]["max_tokens"]


def test_resolve_for_op_handles_malformed_override():
    cfg = {
        "completion": {"provider": "openai", "model": "gpt-5-mini"},
        "overrides": {"hubs_run": "not-a-dict"},   # malformed
    }
    eff = resolve_for_op(cfg, "hubs_run")
    # Silently ignored — falls through to completion.*.
    assert eff["provider"] == "openai"
    assert eff["model"] == "gpt-5-mini"


# ---- embeddings_config -------------------------------------------------------


def test_embeddings_config_returns_block():
    cfg = {"embeddings": {"provider": "openai", "model": "text-embedding-3-small"}}
    assert embeddings_config(cfg) == {
        "provider": "openai",
        "model": "text-embedding-3-small",
    }


def test_embeddings_config_falls_back_to_defaults():
    assert embeddings_config({}) == {
        "provider": DEFAULT_CONFIG["embeddings"]["provider"],
        "model": DEFAULT_CONFIG["embeddings"]["model"],
    }


def test_embeddings_config_partial_overlay():
    # User set provider only — model falls through to default.
    cfg = {"embeddings": {"provider": "sentence_transformer"}}
    eff = embeddings_config(cfg)
    assert eff["provider"] == "sentence_transformer"
    assert eff["model"] == DEFAULT_CONFIG["embeddings"]["model"]
