"""Tests for the ``weave doctor`` stale-embeddings advisory.

Warns when ``embeddings.db`` mtime is older than
``STALE_EMBEDDINGS_DAYS`` AND ``OPENAI_API_KEY`` is in the
environment. Without the key the keep-warm cron can't run regardless,
so the doctor stays quiet to avoid noise.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.vault import VaultManager
from thinkweave.synthesis.concepts import (
    STALE_EMBEDDINGS_DAYS,
    doctor_report,
    find_stale_embeddings_db,
    format_doctor_report,
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


class TestStaleEmbeddingsCheck:
    def test_warns_when_db_old_and_api_key_set(
        self,
        vault: VaultManager,
        config: Config,
        monkeypatch,
    ):
        """A stale embeddings.db (mtime > threshold) with
        ``OPENAI_API_KEY`` set should surface in the doctor report."""
        config.weave_dir.mkdir(parents=True, exist_ok=True)
        db_path = config.embeddings_db
        db_path.write_bytes(b"")
        # Backdate the mtime by 10 days.
        old_ts = time.time() - 10 * 86400
        os.utime(db_path, (old_ts, old_ts))

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        result = find_stale_embeddings_db(config)
        assert result is not None
        assert result["age_days"] > STALE_EMBEDDINGS_DAYS
        assert result["max_age_days"] == STALE_EMBEDDINGS_DAYS

        # End-to-end: format_doctor_report surfaces it.
        report = doctor_report(config)
        assert report.get("stale_embeddings_db") is not None
        text = format_doctor_report(report)
        assert "Stale embeddings DB" in text
        assert "weave index --embed --only-new" in text

    def test_no_warn_when_api_key_missing(
        self,
        vault: VaultManager,
        config: Config,
        monkeypatch,
    ):
        """Without ``OPENAI_API_KEY``, the keep-warm cron can't run
        regardless — don't nag."""
        config.weave_dir.mkdir(parents=True, exist_ok=True)
        db_path = config.embeddings_db
        db_path.write_bytes(b"")
        old_ts = time.time() - 30 * 86400
        os.utime(db_path, (old_ts, old_ts))

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        assert find_stale_embeddings_db(config) is None

    def test_no_warn_when_db_missing(
        self,
        vault: VaultManager,
        config: Config,
        monkeypatch,
    ):
        """No embeddings.db = similarity retrieval not configured —
        not the doctor's job to nag."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert find_stale_embeddings_db(config) is None

    def test_no_warn_when_db_fresh(
        self,
        vault: VaultManager,
        config: Config,
        monkeypatch,
    ):
        """A freshly-touched embeddings.db should pass cleanly."""
        config.weave_dir.mkdir(parents=True, exist_ok=True)
        db_path = config.embeddings_db
        db_path.write_bytes(b"")
        # mtime = now (default for newly-written file).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert find_stale_embeddings_db(config) is None
