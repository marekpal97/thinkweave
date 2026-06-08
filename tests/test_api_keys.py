"""Tests for ``core/api_keys.py`` — consolidated provider key loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from personal_mem.core import api_keys
from personal_mem.core.api_keys import get_provider_key


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Strip provider-key env vars + isolate .env lookup paths.

    Tests can have a checkout-local ``.env`` polluting the project-root
    fallback — point ``_PROJECT_ROOT`` at an empty tmp dir so only the
    test-supplied paths are consulted.
    """
    for var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "PERSONAL_MEM_VAULT",
    ):
        monkeypatch.delenv(var, raising=False)
    # Repoint the project-root fallback at an empty dir so the checkout's
    # real .env can't leak in.
    isolated_root = tmp_path_factory.mktemp("isolated_root")
    monkeypatch.setattr(api_keys, "_PROJECT_ROOT", isolated_root)
    yield monkeypatch


# ---- env-first --------------------------------------------------------------


def test_get_provider_key_reads_env(clean_env: pytest.MonkeyPatch):
    clean_env.setenv("OPENAI_API_KEY", "sk-from-env")
    assert get_provider_key("openai") == "sk-from-env"


def test_get_provider_key_provider_is_case_insensitive(clean_env: pytest.MonkeyPatch):
    clean_env.setenv("OPENAI_API_KEY", "sk-from-env")
    assert get_provider_key("OpenAI") == "sk-from-env"
    assert get_provider_key("OPENAI") == "sk-from-env"


def test_get_provider_key_unknown_provider_returns_none(clean_env: pytest.MonkeyPatch):
    assert get_provider_key("xai") is None


# ---- gemini legacy fallback -------------------------------------------------


def test_gemini_prefers_new_var_over_legacy(clean_env: pytest.MonkeyPatch):
    clean_env.setenv("GEMINI_API_KEY", "new-key")
    clean_env.setenv("GOOGLE_API_KEY", "legacy-key")
    assert get_provider_key("gemini") == "new-key"


def test_gemini_falls_back_to_legacy(clean_env: pytest.MonkeyPatch):
    clean_env.setenv("GOOGLE_API_KEY", "legacy-key")
    assert get_provider_key("gemini") == "legacy-key"


# ---- .env loader -------------------------------------------------------------


def test_loads_from_vault_env_file(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-vault\n", encoding="utf-8")
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    assert get_provider_key("openai") == "sk-from-vault"
    # Side effect: populated os.environ for downstream SDK consumers.
    assert os.environ.get("OPENAI_API_KEY") == "sk-from-vault"


def test_loads_from_cwd_env_file_when_vault_unset(
    clean_env: pytest.MonkeyPatch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_provider_key("anthropic") == "sk-from-cwd"


def test_env_file_does_not_override_already_set(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    clean_env.setenv("OPENAI_API_KEY", "sk-already-set")
    assert get_provider_key("openai") == "sk-already-set"


def test_missing_env_file_returns_none(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))  # no .env in it
    clean_env.chdir(tmp_path)  # also isolate cwd fallback
    assert get_provider_key("openai") is None


def test_malformed_env_file_is_silent(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / ".env").write_text(
        "this is not a valid line\n"
        "# a comment\n"
        "OPENAI_API_KEY=sk-still-works\n"
        "no_equals_sign\n",
        encoding="utf-8",
    )
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    assert get_provider_key("openai") == "sk-still-works"


def test_env_file_strips_quotes(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".env").write_text(
        'OPENAI_API_KEY="sk-quoted"\n'
        "ANTHROPIC_API_KEY='sk-single'\n",
        encoding="utf-8",
    )
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    assert get_provider_key("openai") == "sk-quoted"
    assert get_provider_key("anthropic") == "sk-single"


# ---- idempotency ------------------------------------------------------------


def test_load_env_files_is_idempotent(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-once\n", encoding="utf-8")
    clean_env.setenv("PERSONAL_MEM_VAULT", str(tmp_path))
    api_keys._load_env_files()
    # Manually overwrite to verify the second call doesn't undo it.
    os.environ["OPENAI_API_KEY"] = "sk-changed-by-caller"
    api_keys._load_env_files()
    assert os.environ["OPENAI_API_KEY"] == "sk-changed-by-caller"
