"""Tests for ``core/embedding_provider.py``.

Mocks httpx so we don't hit OpenAI; sentence-transformer and litellm
backends are exercised via dep-presence skipif gates plus
factory-shape assertions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from thinkweave.core.embedding_provider import (
    LiteLLMEmbeddingProvider,
    OpenAIEmbeddingProvider,
    SentenceTransformerProvider,
    build_from_vault,
    build_provider,
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_build_provider_openai_default():
    p = build_provider("openai", "")
    assert isinstance(p, OpenAIEmbeddingProvider)
    assert p.model == "text-embedding-3-small"
    assert p.dim == 1536


def test_build_provider_openai_large():
    p = build_provider("openai", "text-embedding-3-large")
    assert p.model == "text-embedding-3-large"
    assert p.dim == 3072


def test_build_provider_sentence_transformer_aliases():
    a = build_provider("sentence_transformer", "")
    b = build_provider("sentence-transformer", "")
    c = build_provider("sbert", "")
    for p in (a, b, c):
        assert isinstance(p, SentenceTransformerProvider)
        assert p.model == "all-MiniLM-L6-v2"
        assert p.dim == 384


def test_build_provider_litellm_requires_model():
    with pytest.raises(ValueError, match="requires an explicit model"):
        build_provider("litellm", "")


def test_build_provider_litellm_with_model():
    p = build_provider("litellm", "voyage/voyage-2")
    assert isinstance(p, LiteLLMEmbeddingProvider)
    assert p.model == "voyage/voyage-2"


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown embedding provider"):
        build_provider("cohere-direct", "embed-english-v3.0")


# ---------------------------------------------------------------------------
# build_from_vault — reads api.yaml
# ---------------------------------------------------------------------------


def test_build_from_vault_default_when_no_config(tmp_path: Path):
    p = build_from_vault(tmp_path)
    assert isinstance(p, OpenAIEmbeddingProvider)
    assert p.model == "text-embedding-3-small"


def test_build_from_vault_overlay(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "api.yaml").write_text(
        "embeddings:\n"
        "  provider: sentence_transformer\n"
        "  model: all-MiniLM-L6-v2\n",
        encoding="utf-8",
    )
    p = build_from_vault(tmp_path)
    assert isinstance(p, SentenceTransformerProvider)


def test_build_from_vault_none_root():
    p = build_from_vault(None)
    assert isinstance(p, OpenAIEmbeddingProvider)


# ---------------------------------------------------------------------------
# OpenAIEmbeddingProvider — mock httpx
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def fake_httpx(monkeypatch: pytest.MonkeyPatch):
    """Replace ``httpx.post`` with a recorder; seed an OpenAI key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json})
        # Mirror OpenAI's embedding response shape.
        return _FakeResponse({
            "data": [{"embedding": [0.1, 0.2, 0.3]} for _ in json["input"]],
            "usage": {"prompt_tokens": 5 * len(json["input"])},
        })

    fake_httpx_mod = SimpleNamespace(post=fake_post)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx_mod)
    yield calls


def test_openai_provider_returns_vectors(fake_httpx):
    p = OpenAIEmbeddingProvider(model="text-embedding-3-small")
    vecs = p.embed(["a", "b", "c"])
    assert vecs == [[0.1, 0.2, 0.3]] * 3


def test_openai_provider_empty_texts_is_noop(fake_httpx):
    p = OpenAIEmbeddingProvider()
    assert p.embed([]) == []
    assert fake_httpx == []


def test_openai_provider_missing_key_raises(monkeypatch, fake_httpx):
    # Strip key + isolate .env lookups.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    from thinkweave.core import api_keys
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(api_keys, "_PROJECT_ROOT", Path(td))
        monkeypatch.chdir(td)
        p = OpenAIEmbeddingProvider()
        with pytest.raises(ValueError, match="OPENAI_API_KEY not found"):
            p.embed(["x"])


def test_openai_provider_sends_model_and_input(fake_httpx):
    p = OpenAIEmbeddingProvider(model="text-embedding-3-large")
    p.embed(["hello"])
    assert fake_httpx[0]["json"] == {
        "model": "text-embedding-3-large",
        "input": ["hello"],
    }
    assert fake_httpx[0]["headers"]["Authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# SentenceTransformerProvider — gated on dep
# ---------------------------------------------------------------------------


def test_sentence_transformer_dim_known_without_load():
    p = SentenceTransformerProvider("all-MiniLM-L6-v2")
    assert p.dim == 384  # from _KNOWN_DIMS, no model load needed


def test_sentence_transformer_lazy_load_error_when_missing(monkeypatch):
    """Without the dep, calling ``embed`` raises a typed ImportError —
    the protocol assertion stays useful even if the user hasn't
    installed the extra."""
    # Force the import to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = SentenceTransformerProvider()
    with pytest.raises(ImportError, match="embeddings-local"):
        p.embed(["x"])


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_all_providers_conform_to_protocol():
    from thinkweave.core.embedding_provider import EmbeddingProvider
    for p in (
        OpenAIEmbeddingProvider(),
        SentenceTransformerProvider(),
        LiteLLMEmbeddingProvider("voyage/voyage-2"),
    ):
        assert isinstance(p, EmbeddingProvider)
