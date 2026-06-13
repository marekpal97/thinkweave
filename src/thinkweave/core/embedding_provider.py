"""Embedding provider abstraction — decoupled from the completion wrapper.

The completion wrapper (:mod:`thinkweave.core.agent_client`) covers
*text-in / text-out* LLM calls. Embeddings are a different layer with
different providers (OpenAI, sentence-transformers, Voyage/Cohere via
LiteLLM) — bundling them under one client costs more than it saves.

Configured via ``vault/config/api.yaml::embeddings.{provider, model}``;
:func:`build_provider` consults the config and returns the right backend.
Switching backends requires a re-embed (dimensionalities differ) —
``weave index --embed --reset`` clears the cache.

Provider matrix (initial set):

  • ``openai`` (default) — ``text-embedding-3-small`` (1536-dim). Existing
    vaults keep working without a re-embed at upgrade.
  • ``sentence_transformer`` — ``all-MiniLM-L6-v2`` (384-dim, ~80 MB,
    local). Opt-in via ``pip install thinkweave[embeddings-local]``.
  • ``litellm`` — pass-through to any LiteLLM-supported embedding
    provider (Voyage, Cohere, etc.). Opt-in via
    ``pip install thinkweave[embeddings-litellm]``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from thinkweave.core.api_keys import get_provider_key


# Known embedding dimensionalities per (provider, model). Used as the
# default ``dim`` when a backend can't introspect at construction time.
# Missing entries default to 0; callers needing dim must check.
_KNOWN_DIMS: dict[tuple[str, str], int] = {
    ("openai", "text-embedding-3-small"): 1536,
    ("openai", "text-embedding-3-large"): 3072,
    ("openai", "text-embedding-ada-002"): 1536,
    ("sentence_transformer", "all-MiniLM-L6-v2"): 384,
    ("sentence_transformer", "all-mpnet-base-v2"): 768,
}


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol every embedding backend implements.

    The ``embed`` call returns one vector per input text.
    """

    @property
    def dim(self) -> int: ...

    @property
    def model(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """OpenAI-native embeddings via ``httpx`` POST to the embeddings
    endpoint. Preserves the existing-vault contract: switching to this
    provider on an existing 1536-dim cache is a no-op."""

    _API_URL = "https://api.openai.com/v1/embeddings"

    def __init__(self, model: str = "text-embedding-3-small"):
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return _KNOWN_DIMS.get(("openai", self._model), 0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "OpenAI embeddings require httpx: "
                "pip install thinkweave[embeddings]"
            ) from exc

        api_key = get_provider_key("openai")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY not found (checked env, vault .env, cwd .env, "
                "project .env)."
            )

        response = httpx.post(
            self._API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self._model, "input": texts},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return [item["embedding"] for item in data["data"]]


class SentenceTransformerProvider:
    """Local embeddings via ``sentence-transformers``. No network call,
    no API key.

    The model is loaded lazily on first ``embed`` so importing this
    module stays cheap. Subsequent calls reuse the loaded model.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        self._model_name = model
        self._cached: Any = None  # SentenceTransformer instance, loaded lazily

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        if self._cached is not None:
            return int(self._cached.get_sentence_embedding_dimension())
        return _KNOWN_DIMS.get(("sentence_transformer", self._model_name), 0)

    def _ensure_loaded(self) -> None:
        if self._cached is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence_transformer provider requires the extra: "
                "pip install thinkweave[embeddings-local]"
            ) from exc
        self._cached = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        vectors = self._cached.encode(texts, convert_to_numpy=True)
        return [list(map(float, v)) for v in vectors]


class LiteLLMEmbeddingProvider:
    """Pass-through to LiteLLM. Lets the user point at Voyage, Cohere,
    or any other LiteLLM-supported embedding model — without dragging
    the LiteLLM dep into the default install.

    LiteLLM accepts model strings like ``"voyage/voyage-2"`` and
    ``"cohere/embed-english-v3.0"``. Authentication is provider-specific
    (whatever env vars the LiteLLM matrix documents).
    """

    def __init__(self, model: str):
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        # LiteLLM doesn't always expose dim ahead of a call; surface 0
        # and let the caller introspect after the first embed.
        return 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            from litellm import embedding as litellm_embedding
        except ImportError as exc:
            raise ImportError(
                "litellm provider requires the extra: "
                "pip install thinkweave[embeddings-litellm]"
            ) from exc

        response = litellm_embedding(model=self._model, input=texts)
        # LiteLLM normalizes to OpenAI's response shape.
        data = response["data"] if isinstance(response, dict) else response.data
        return [
            item["embedding"] if isinstance(item, dict) else item.embedding
            for item in data
        ]


def build_provider(provider: str, model: str) -> EmbeddingProvider:
    """Factory: return the EmbeddingProvider for ``(provider, model)``.

    Unknown provider raises :class:`ValueError`. Model defaults are
    provider-specific and live in :data:`_KNOWN_DIMS`.
    """
    p = (provider or "").lower()
    if p == "openai":
        return OpenAIEmbeddingProvider(model=model or "text-embedding-3-small")
    if p in {"sentence_transformer", "sentence-transformer", "sbert"}:
        return SentenceTransformerProvider(model=model or "all-MiniLM-L6-v2")
    if p == "litellm":
        if not model:
            raise ValueError(
                "litellm embedding provider requires an explicit model "
                "(e.g. 'voyage/voyage-2')"
            )
        return LiteLLMEmbeddingProvider(model=model)
    raise ValueError(
        f"unknown embedding provider '{provider}'; "
        f"supported: openai, sentence_transformer, litellm"
    )


def build_from_vault(vault_root: Any) -> EmbeddingProvider:
    """Convenience: build a provider from the ``vault/config/api.yaml``
    of a given vault root.
    """
    from pathlib import Path
    from thinkweave.core.api_config import embeddings_config, load_api_config

    cfg = load_api_config(Path(vault_root) if vault_root else None)
    emb = embeddings_config(cfg)
    return build_provider(emb["provider"], emb["model"])
