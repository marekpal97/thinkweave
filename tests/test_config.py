"""Tests for the user-scope config tier + helper predicates.

Covers the Wave-1A vault-wiring seam:

- ``is_vault_initialized(cfg)`` — single canonical "is the vault wired?"
  predicate. True iff ``vault/config/sources.yaml`` exists.
- ``user_config_path()`` — XDG-respectful path resolution.
- ``write_user_config(vault_root)`` — atomic TOML write at the XDG path.
- ``load_config()`` precedence: env var > user-config > vault-internal > defaults.

The user-scope tier exists so ``/onboard`` can persist the vault path
without forcing the user to touch shell rc. It only ever provides
``vault_root``; vault-internal fields (embeddings/edges/dream) stay
owned by the vault-internal ``config.toml``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from thinkweave.core.config import (
    Config,
    is_vault_initialized,
    load_config,
    user_config_path,
    write_user_config,
)


# ---------------------------------------------------------------------------
# is_vault_initialized
# ---------------------------------------------------------------------------


def test_is_vault_initialized_false_when_sources_yaml_missing(tmp_path: Path):
    cfg = Config(vault_root=tmp_path)
    assert is_vault_initialized(cfg) is False


def test_is_vault_initialized_true_when_sources_yaml_present(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "sources.yaml").write_text("- slug: paper\n", encoding="utf-8")
    cfg = Config(vault_root=tmp_path)
    assert is_vault_initialized(cfg) is True


def test_is_vault_initialized_ignores_legacy_mem_path(tmp_path: Path):
    """Phase-3.1 moved sources.yaml to vault/config/; legacy path doesn't count."""
    legacy_dir = tmp_path / ".weave"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "sources.yaml").write_text("- slug: paper\n", encoding="utf-8")
    cfg = Config(vault_root=tmp_path)
    assert is_vault_initialized(cfg) is False


# ---------------------------------------------------------------------------
# user_config_path
# ---------------------------------------------------------------------------


def test_user_config_path_honors_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert user_config_path() == xdg / "thinkweave" / "config.toml"


def test_user_config_path_falls_back_to_home_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert user_config_path() == fake_home / ".config" / "thinkweave" / "config.toml"


def test_user_config_path_windows_uses_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("thinkweave.core.config._is_windows", lambda: True)
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    assert user_config_path() == appdata / "thinkweave" / "config.toml"


def test_user_config_path_xdg_wins_over_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # An explicitly-set XDG var beats the Windows %APPDATA% branch.
    monkeypatch.setattr("thinkweave.core.config._is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert user_config_path() == xdg / "thinkweave" / "config.toml"


def test_user_cache_dir_windows_uses_localappdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from thinkweave.core.config import user_cache_dir

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("thinkweave.core.config._is_windows", lambda: True)
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert user_cache_dir() == local / "thinkweave"


def test_user_cache_dir_posix_falls_back_to_home_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from thinkweave.core.config import user_cache_dir

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("thinkweave.core.config._is_windows", lambda: False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert user_cache_dir() == fake_home / ".cache" / "thinkweave"


# ---------------------------------------------------------------------------
# write_user_config
# ---------------------------------------------------------------------------


def test_write_user_config_creates_parent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    xdg = tmp_path / "xdg"  # does not exist yet
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    vault = tmp_path / "my-vault"

    write_user_config(vault)

    target = xdg / "thinkweave" / "config.toml"
    assert target.exists()


def test_write_user_config_writes_valid_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    vault = tmp_path / "my-vault"

    write_user_config(vault)

    target = xdg / "thinkweave" / "config.toml"
    with open(target, "rb") as f:
        data = tomllib.load(f)
    assert data == {"vault_root": str(vault)}


def test_write_user_config_overwrites_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    write_user_config(tmp_path / "v1")
    write_user_config(tmp_path / "v2")

    target = xdg / "thinkweave" / "config.toml"
    with open(target, "rb") as f:
        data = tomllib.load(f)
    assert data == {"vault_root": str(tmp_path / "v2")}


# ---------------------------------------------------------------------------
# load_config precedence
# ---------------------------------------------------------------------------


def _isolate_user_config(
    monkeypatch: pytest.MonkeyPatch, base: Path
) -> Path:
    """Point user_config_path() at a clean tmp_path-scoped XDG dir.

    Both the env override and the Path.home() fallback are pinned so
    the test never reads the developer's real ~/.config.
    """
    xdg = base / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    # Belt-and-braces in case any helper calls Path.home() directly.
    monkeypatch.setattr(Path, "home", lambda: base / "fake-home")
    # The pre-rename PERSONAL_MEM_* names are still honoured as migration
    # fallbacks by load_config(); a developer shell that exports
    # PERSONAL_MEM_VAULT would otherwise leak into "nothing set" cases.
    for legacy in ("PERSONAL_MEM_VAULT", "PERSONAL_MEM_PROJECT", "PERSONAL_MEM_DB"):
        monkeypatch.delenv(legacy, raising=False)
    return xdg / "thinkweave" / "config.toml"


def test_load_config_uses_user_config_when_no_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 2 picks up vault_root when the env var is absent."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    chosen = tmp_path / "user-chosen-vault"
    user_path.parent.mkdir(parents=True)
    user_path.write_text(f'vault_root = "{chosen}"\n', encoding="utf-8")

    cfg = load_config()
    assert cfg.vault_root == chosen


def test_load_config_env_overrides_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 1 (env) wins over tier 2 (user-config)."""
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    user_path.parent.mkdir(parents=True)
    user_path.write_text(
        f'vault_root = "{tmp_path / "user-vault"}"\n', encoding="utf-8"
    )
    env_vault = tmp_path / "env-vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(env_vault))

    cfg = load_config()
    assert cfg.vault_root == env_vault


def test_load_config_user_config_does_not_clobber_vault_internal_embedding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 2 sets vault_root only — vault-internal embedding fields survive."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)

    # User-config points vault_root at a vault that has its own internal
    # config.toml with a non-default embedding model. That internal field
    # must still apply — user-config only owns vault_root.
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    (vault / ".weave" / "config.toml").write_text(
        '[embeddings]\nmodel = "custom-embed-model"\n', encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(f'vault_root = "{vault}"\n', encoding="utf-8")

    cfg = load_config()
    assert cfg.vault_root == vault
    assert cfg.embedding_model == "custom-embed-model"


def test_load_config_falls_back_to_default_when_nothing_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 4: built-in default applies when no env, no user-config, no internal.

    The default ``_DEFAULT_VAULT`` is captured at module load (``~/vault``
    at the real home), so we just confirm load_config matches a fresh
    ``Config()``'s default — the precedence chain bottomed out cleanly.
    """
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    _isolate_user_config(monkeypatch, tmp_path)  # XDG points to empty dir

    cfg = load_config()
    assert cfg.vault_root == Config().vault_root


def test_load_config_user_config_overrides_vault_internal_vault_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 2 wins over tier 3 for vault_root specifically."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)

    # User-config picks 'preferred-vault'. That vault's internal toml
    # tries to redirect to 'internal-vault' — must lose to tier 2.
    preferred = tmp_path / "preferred-vault"
    internal_target = tmp_path / "internal-vault"
    (preferred / ".weave").mkdir(parents=True)
    (preferred / ".weave" / "config.toml").write_text(
        f'vault_root = "{internal_target}"\n', encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(f'vault_root = "{preferred}"\n', encoding="utf-8")

    cfg = load_config()
    assert cfg.vault_root == preferred


def test_load_config_ignores_malformed_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A broken user-config TOML must not brick `weave`; fall through silently."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    user_path.parent.mkdir(parents=True)
    user_path.write_text("not = valid = toml = at = all\n", encoding="utf-8")

    cfg = load_config()
    # Falls through to the built-in default — same as a fresh Config().
    assert cfg.vault_root == Config().vault_root


# ---------------------------------------------------------------------------
# Policy knobs (2026-06 bucket-3 audit) — defaults + toml override path
# ---------------------------------------------------------------------------

# (field, old hardcoded literal, toml block, toml key) — the table is the
# contract: each default must equal the literal it replaced, and each key
# must round-trip through ``load_config``.
_POLICY_KNOBS = [
    ("dream_promotion_threshold", 5, "dream", "promotion_threshold"),
    ("dream_promotion_cap", 20, "dream", "promotion_cap"),
    ("dream_probe_window_days", 14, "dream", "probe_window_days"),
    ("dream_rejudge_cap", 20, "dream", "rejudge_cap"),
    ("dream_knowledge_delta_hours", 24, "dream", "knowledge_delta_hours"),
    ("dream_essence_max_catalysts", 10, "dream", "essence_max_catalysts"),
    (
        "dream_essence_placeholder_max_catalysts",
        25,
        "dream",
        "essence_placeholder_max_catalysts",
    ),
    ("extract_insights_cap", 3, "extract", "insights_cap"),
    ("enrich_fanout_threshold", 12, "enrich", "fanout_threshold"),
    ("enrich_batch_size", 6, "enrich", "batch_size"),
    ("enrich_parallelism", 3, "enrich", "parallelism"),
    ("theme_min_cluster_size", 3, "themes", "min_cluster_size"),
    ("theme_recent_days", 30, "themes", "recent_days"),
    ("theme_min_shared_concepts", 2, "themes", "min_shared_concepts"),
    ("theme_name_family_jaccard", 0.5, "themes", "name_family_jaccard"),
    ("theme_generic_concept_ratio", 0.5, "themes", "generic_concept_ratio"),
    ("landing_open_probes_cap", 20, "landing", "open_probes_cap"),
    ("landing_probes_display_cap", 10, "landing", "probes_display_cap"),
    ("retrieval_rrf_k", 60, "retrieval", "rrf_k"),
]


def test_policy_knob_defaults_match_old_literals():
    """Each new Config field defaults to the literal it replaced."""
    cfg = Config()
    for field_name, old_literal, _block, _key in _POLICY_KNOBS:
        assert getattr(cfg, field_name) == old_literal, field_name


def test_load_config_parses_policy_knob_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Every policy knob is overridable from vault-internal config.toml."""
    _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))

    # Distinct non-default value per knob: int knobs get literal+1,
    # float knobs get literal+0.25.
    overrides = {
        field_name: (
            old + 0.25 if isinstance(old, float) else old + 1
        )
        for field_name, old, _b, _k in _POLICY_KNOBS
    }
    blocks: dict[str, list[str]] = {}
    for field_name, _old, block, key in _POLICY_KNOBS:
        blocks.setdefault(block, []).append(
            f"{key} = {overrides[field_name]}"
        )
    toml_text = "\n".join(
        f"[{block}]\n" + "\n".join(lines) + "\n"
        for block, lines in blocks.items()
    )
    (vault / ".weave" / "config.toml").write_text(toml_text, encoding="utf-8")

    cfg = load_config()
    for field_name, _old, _block, _key in _POLICY_KNOBS:
        assert getattr(cfg, field_name) == overrides[field_name], field_name


def test_rrf_k_override_coexists_with_prompt_time_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """[retrieval] rrf_k and [retrieval.prompt_time] parse side by side."""
    _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    (vault / ".weave" / "config.toml").write_text(
        "[retrieval]\nrrf_k = 30\n\n"
        "[retrieval.prompt_time]\nenabled = false\n",
        encoding="utf-8",
    )

    cfg = load_config()
    assert cfg.retrieval_rrf_k == 30
    assert cfg.retrieval_prompt_time.enabled is False


def test_config_toml_canonical_location_is_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """config.toml at vault/config/ (the canonical home as of 2026-06-13) is read."""
    _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    (vault / "config" / "config.toml").write_text(
        "[retrieval]\nrrf_k = 42\n", encoding="utf-8"
    )

    cfg = load_config()
    assert cfg.config_path == vault / "config" / "config.toml"
    assert cfg.retrieval_rrf_k == 42


def test_config_toml_canonical_wins_over_legacy_mem_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When both locations exist, vault/config/config.toml wins; the legacy
    vault/.weave/config.toml is only a fallback for un-migrated vaults."""
    _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / "config").mkdir(parents=True)
    (vault / ".weave").mkdir(parents=True)
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    (vault / ".weave" / "config.toml").write_text(
        "[retrieval]\nrrf_k = 11\n", encoding="utf-8"
    )
    (vault / "config" / "config.toml").write_text(
        "[retrieval]\nrrf_k = 99\n", encoding="utf-8"
    )

    cfg = load_config()
    assert cfg.config_path == vault / "config" / "config.toml"
    assert cfg.retrieval_rrf_k == 99


def test_load_config_parses_coarsen_and_resolve_knobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """[dream] coarsen knobs + [themes] resolve_after_days override defaults."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    (vault / ".weave" / "config.toml").write_text(
        "[dream]\n"
        "coarsen_threshold = 0.9\n"
        "coarsen_cap = 7\n"
        "coarsen_max_size = 4\n"
        "coarsen_apply = false\n"
        "[themes]\n"
        "resolve_after_days = 30\n",
        encoding="utf-8",
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(f'vault_root = "{vault}"\n', encoding="utf-8")

    cfg = load_config()
    assert cfg.dream_coarsen_threshold == 0.9
    assert cfg.dream_coarsen_cap == 7
    assert cfg.dream_coarsen_max_size == 4
    assert cfg.dream_coarsen_apply is False
    assert cfg.theme_resolve_after_days == 30


def test_coarsen_knob_defaults():
    """Absent [dream]/[themes] blocks → shipped defaults unchanged."""
    cfg = Config(vault_root=Path("/tmp"))
    assert cfg.dream_coarsen_threshold == 0.85
    assert cfg.dream_coarsen_cap == 3
    assert cfg.dream_coarsen_max_size == 6
    assert cfg.dream_coarsen_apply is True
    assert cfg.theme_resolve_after_days == 60
