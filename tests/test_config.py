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

import json
import tomllib
from pathlib import Path

import pytest

from thinkweave.core.config import (
    Config,
    is_vault_initialized,
    load_config,
    normalize_project_name,
    user_config_path,
    write_user_config,
)


def _toml_line(key: str, value) -> str:
    """One ``key = "<value>"`` TOML line with the value properly escaped.

    Fixtures must not interpolate a path straight into a TOML basic string. On
    Windows ``C:\\Users\\...`` makes ``\\U`` look like the start of an 8-hex-digit
    unicode escape, so the fixture writes a file ``tomllib`` refuses; the tier
    under test then silently falls through to the default vault and the
    assertion fails for a reason that has nothing to do with the behaviour
    being tested. Same mechanism as ``core/config.py::write_user_config``.
    """
    return f"{key} = {json.dumps(str(value), ensure_ascii=False)}\n"


# ---------------------------------------------------------------------------
# is_vault_initialized
# ---------------------------------------------------------------------------


def test_is_vault_initialized_false_when_sources_yaml_missing(tmp_path: Path):
    cfg = Config(vault_root=tmp_path)
    assert is_vault_initialized(cfg) is False


def test_is_vault_initialized_does_not_hide_permission_denial(
    tmp_path: Path, monkeypatch
):
    cfg = Config(vault_root=tmp_path / "vault")
    target = cfg.vault_root / "config" / "sources.yaml"
    original_stat = Path.stat

    def denied(path: Path, *args, **kwargs):
        if path == target:
            raise PermissionError("sandbox denied vault access")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(PermissionError, match="sandbox denied"):
        is_vault_initialized(cfg)


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
    # Pin the platform: this asserts the POSIX ``~/.config`` branch, and on a
    # real Windows host %APPDATA% correctly wins instead. The Windows branch
    # has its own test (test_user_config_path_windows_uses_appdata).
    monkeypatch.setattr("thinkweave.core.config._is_windows", lambda: False)
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


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Users\me\vault",
        r"C:\temp\notes",
        "/home/me/vault-\N{ROCKET}",
    ],
    ids=["windows-path", "windows-path-tab-escape", "non-bmp-char"],
)
def test_write_user_config_survives_paths_needing_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
):
    """The value is escaped, not interpolated, so the file we write parses.

    A Windows root dropped raw into a TOML basic string breaks two ways:
    ``C:\\Users`` reads ``\\U`` as an 8-hex-digit unicode escape ("Invalid hex
    value"), and ``C:\\temp`` reads ``\\t`` as a literal tab — silently wrong
    rather than loud. ``/onboard`` persists this file, so a corrupt write means
    nothing can load the vault path back.

    The non-BMP case pins ``ensure_ascii=False``: the json default would emit a
    surrogate pair, which TOML rejects as not a Unicode scalar value.

    Literal backslashes rather than ``tmp_path`` so this holds on POSIX too.
    """
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    write_user_config(Path(raw))

    with open(xdg / "thinkweave" / "config.toml", "rb") as f:
        data = tomllib.load(f)
    assert data == {"vault_root": str(Path(raw))}


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
    for legacy in ("PERSONAL_MEM_VAULT", "PERSONAL_MEM_PROJECT"):
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
    user_path.write_text(_toml_line("vault_root", chosen), encoding="utf-8")

    cfg = load_config()
    assert cfg.vault_root == chosen


def test_load_config_env_overrides_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Tier 1 (env) wins over tier 2 (user-config)."""
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    user_path.parent.mkdir(parents=True)
    user_path.write_text(
        _toml_line("vault_root", tmp_path / "user-vault"), encoding="utf-8"
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
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")

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
        _toml_line("vault_root", internal_target), encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", preferred), encoding="utf-8")

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
    ("health_stale_factor", 1.5, "health", "stale_factor"),
    ("health_backlog_days", 7, "health", "backlog_days"),
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
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")

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


# ---------------------------------------------------------------------------
# weave_dir override — relocate derived state off the vault path
# ---------------------------------------------------------------------------


def test_weave_dir_defaults_to_vault_root_dot_weave():
    cfg = Config(vault_root=Path("/tmp/vault"))
    assert cfg.weave_dir == Path("/tmp/vault/.weave")


def test_weave_dir_toml_override_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """[weave_dir] set to an absolute path relocates index/embeddings/buffer."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    fast_disk = tmp_path / "fast-disk" / "weave-state"
    (vault / ".weave" / "config.toml").write_text(
        _toml_line("weave_dir", fast_disk), encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")

    cfg = load_config()
    assert cfg.weave_dir == fast_disk
    assert cfg.index_db == fast_disk / "index.db"
    assert cfg.embeddings_db == fast_disk / "embeddings.db"


def test_index_db_env_vars_are_inert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """THINKWEAVE_DB / PERSONAL_MEM_DB are gone (#34) — must not touch config.

    The single override story is weave_dir (config.toml / THINKWEAVE_WEAVE_DIR);
    index_db is always weave_dir / "index.db". The removed env vars must
    neither move index_db nor leave any override attribute behind.
    """
    monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
    _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    monkeypatch.setenv("THINKWEAVE_DB", str(tmp_path / "elsewhere" / "other.db"))
    monkeypatch.setenv(
        "PERSONAL_MEM_DB", str(tmp_path / "elsewhere" / "legacy.db")
    )

    cfg = load_config()
    assert cfg.index_db == vault / ".weave" / "index.db"
    assert not hasattr(cfg, "_index_db_override")


def test_weave_dir_toml_override_relative_path_anchors_at_vault_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A relative weave_dir resolves against vault_root, not the process cwd."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    (vault / ".weave" / "config.toml").write_text(
        'weave_dir = "../weave-state"\n', encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")

    cfg = load_config()
    assert cfg.weave_dir == vault / "../weave-state"


def test_weave_dir_toml_override_expands_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """``~`` expansion goes through ``Path.expanduser()`` (reads $HOME),
    matching the repo's existing expanduser call sites (e.g.
    ``surfaces/cli/util.py``) — not ``Path.home()``."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    # expanduser() reads the environment, and which variable depends on the
    # platform: ntpath consults USERPROFILE (then HOMEDRIVE/HOMEPATH) and
    # ignores HOME entirely, while posixpath uses HOME. Set both so the test
    # pins the real home on either OS.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    (vault / ".weave" / "config.toml").write_text(
        'weave_dir = "~/weave-state"\n', encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")

    cfg = load_config()
    assert cfg.weave_dir == fake_home / "weave-state"


def test_weave_dir_env_override_wins_over_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """THINKWEAVE_WEAVE_DIR (env) beats the vault-internal config.toml key."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / ".weave").mkdir(parents=True)
    (vault / ".weave" / "config.toml").write_text(
        _toml_line("weave_dir", tmp_path / "toml-weave-state"), encoding="utf-8"
    )
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")
    env_weave_dir = tmp_path / "env-weave-state"
    monkeypatch.setenv("THINKWEAVE_WEAVE_DIR", str(env_weave_dir))

    cfg = load_config()
    assert cfg.weave_dir == env_weave_dir


def test_weave_dir_env_override_without_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The env override applies even with no vault-internal config.toml at all."""
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    user_path = _isolate_user_config(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_toml_line("vault_root", vault), encoding="utf-8")
    env_weave_dir = tmp_path / "env-weave-state"
    monkeypatch.setenv("THINKWEAVE_WEAVE_DIR", str(env_weave_dir))

    cfg = load_config()
    assert cfg.weave_dir == env_weave_dir


# ---------------------------------------------------------------------------
# normalize_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("trade-ideas", "trade_ideas"),
        ("Trade Ideas", "trade_ideas"),
        ("mp-unit2_optimizer", "unit2_optimizer"),
        ("mp_unit2_optimizer", "unit2_optimizer"),
        ("qdi-unit1_optimizer", "unit1_optimizer"),
        ("qdi_unit1_optimizer", "unit1_optimizer"),
        ("unit1_optimizer", "unit1_optimizer"),
        ("", ""),
        ("mp", "mp"),
        ("mp-", "mp_"),
    ],
)
def test_normalize_project_name(raw: str, expected: str):
    """mp-/mp_/qdi-/qdi_ worktree prefixes collapse onto the bare shared name."""
    assert normalize_project_name(raw) == expected
