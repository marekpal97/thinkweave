"""Multi-provider completion wrapper — the single LLM client for
personal_mem's internal ops (backfill paths, news triage fallback,
anywhere we need text-in / text-out with usage tracking).

Built on the ``openai`` Python SDK's :class:`AsyncOpenAI` with per-provider
``base_url`` overrides. Per the Round 2 plan ([[feedback_unified_wrapper_no_batches_apis]]):

  • ONE wrapper (no LiteLLM extension, no Agents SDK Runner on top).
  • ``base_url`` swaps between OpenAI-native, Anthropic's OpenAI-compat
    endpoint, and Gemini's OpenAI-compat endpoint.
  • Batched work = ``asyncio.gather`` over N completions with a
    semaphore-capped concurrency budget. Provider-native Batches APIs
    are NOT used.
  • Every call records spend via :func:`personal_mem.core.spend.record_spend`
    exactly once.

The Gemini Files API (audio modality for podcast extraction) stays a
direct ``google.genai`` carve-out in :mod:`sources.extractors.gemini_extract`
— no chat-completion shape covers it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from personal_mem.core.api_keys import get_provider_key
from personal_mem.core.spend import record_spend


# Per-provider ``base_url`` override for ``AsyncOpenAI``. None → SDK default
# (the OpenAI-native endpoint). Each non-None URL points at the provider's
# OpenAI-compatible chat-completions path; the SDK calls
# ``<base_url>/chat/completions`` under the hood.
_PROVIDER_URLS: dict[str, str | None] = {
    "openai": None,
    "anthropic": "https://api.anthropic.com/v1/",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


class ProviderError(RuntimeError):
    """Raised for provider-level problems the wrapper can't resolve
    (unknown provider, missing API key). Network / API errors from the
    SDK are NOT wrapped — they propagate as-is so callers can branch on
    typed exceptions (rate-limit, auth, etc.).
    """


def supported_providers() -> tuple[str, ...]:
    """Tuple of provider slugs the wrapper accepts."""
    return tuple(_PROVIDER_URLS)


def _build_messages(prompt: str, system: str | None) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _usage_to_dict(usage: Any) -> dict[str, int]:
    """Normalize the SDK's typed ``CompletionUsage`` (or a dict) to a
    plain ``{prompt_tokens, completion_tokens, total_tokens}`` dict.
    """
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        d = usage
    else:
        # SDK objects expose ``model_dump()`` (pydantic) or ``__dict__``.
        dump = getattr(usage, "model_dump", None)
        d = dump() if callable(dump) else dict(getattr(usage, "__dict__", {}) or {})
    return {
        "prompt_tokens": int(d.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(d.get("completion_tokens", 0) or 0),
        "total_tokens": int(d.get("total_tokens", 0) or 0),
    }


def _resolve_client(provider: str) -> Any:
    """Build an ``AsyncOpenAI`` client pointed at the provider's endpoint.

    Raises :class:`ProviderError` when the provider is unknown or the
    key is missing. The ``openai`` SDK import is deferred so importing
    this module doesn't pay the dep at startup time.
    """
    if provider not in _PROVIDER_URLS:
        raise ProviderError(
            f"unknown provider '{provider}'; "
            f"supported: {', '.join(supported_providers())}"
        )

    key = get_provider_key(provider)
    if not key:
        raise ProviderError(
            f"no API key found for provider '{provider}' "
            f"(env, vault .env, cwd .env, project .env all empty)"
        )

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover — openai is a hard dep
        raise ProviderError(
            "openai SDK not installed; this should be in pyproject deps"
        ) from exc

    base_url = _PROVIDER_URLS[provider]
    if base_url is None:
        return AsyncOpenAI(api_key=key)
    return AsyncOpenAI(api_key=key, base_url=base_url)


async def get_completion(
    prompt: str,
    *,
    provider: str,
    model: str,
    op: str,
    max_tokens: int = 8000,
    system: str | None = None,
    mode: str = "mcp",
    session_id: str | None = None,
    response_format: dict | None = None,
) -> tuple[str, dict[str, int]]:
    """Issue one chat completion and return ``(text, usage)``.

    Records spend once per call via :func:`record_spend` — ``op`` is the
    bookkeeping label (e.g. ``"enrich"``, ``"hubs_run"``, ``"hubs_link"``).
    ``mode`` mirrors ``record_spend``'s ``mode`` arg: ``"mcp"`` (default,
    in-server), ``"cli"`` (CLI process), ``"cron"`` (scheduled).

    ``response_format`` — when set (e.g. ``{"type": "json_object"}``) the
    SDK enforces the constraint at the provider level. When ``None`` the
    kwarg is omitted from the SDK call (the OpenAI SDK expects the param
    absent rather than null).
    """
    client = _resolve_client(provider)
    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": _build_messages(prompt, system),
    }
    # OpenAI's gpt-5 family (gpt-5, gpt-5-mini, gpt-5-nano) rejects
    # ``max_tokens`` and requires ``max_completion_tokens`` instead. Every
    # other provider/model — including Anthropic and Gemini via their
    # OpenAI-compat endpoints, and OpenAI's gpt-4*/gpt-3.5* — still uses
    # the classic ``max_tokens`` kwarg. Keep the wrapper's public param
    # name unchanged; translate only at the SDK boundary.
    if provider == "openai" and model.startswith("gpt-5"):
        create_kwargs["max_completion_tokens"] = max_tokens
    else:
        create_kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        create_kwargs["response_format"] = response_format
    response = await client.chat.completions.create(**create_kwargs)
    usage = _usage_to_dict(getattr(response, "usage", None))
    record_spend(
        provider,
        model,
        op,
        usage["prompt_tokens"],
        usage["completion_tokens"],
        mode=mode,
        session_id=session_id,
    )
    text = response.choices[0].message.content or ""
    return text, usage


async def batch_completions(
    prompts: list[str],
    *,
    provider: str,
    model: str,
    op: str,
    max_tokens: int = 8000,
    system: str | None = None,
    concurrency: int = 20,
    mode: str = "mcp",
    session_id: str | None = None,
    return_exceptions: bool = False,
    response_format: dict | None = None,
) -> list[tuple[str, dict[str, int]]]:
    """Issue N chat completions in parallel with a concurrency cap.

    Replaces the Anthropic Batches / OpenAI Batches code paths the
    backfill orchestrators (``hubs_batch.py``, ``enrich_batch.py``) used
    to maintain. Trades ~50% provider-discount for one code path.

    When ``return_exceptions=False`` (default) the first failure raises
    and cancels the rest. Pass ``return_exceptions=True`` for partial-
    success semantics — each list slot is either a ``(text, usage)``
    tuple or an exception instance.

    ``response_format`` is forwarded to every per-prompt
    :func:`get_completion` call.
    """
    if not prompts:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(p: str) -> tuple[str, dict[str, int]]:
        async with sem:
            return await get_completion(
                p,
                provider=provider,
                model=model,
                op=op,
                max_tokens=max_tokens,
                system=system,
                mode=mode,
                session_id=session_id,
                response_format=response_format,
            )

    results = await asyncio.gather(
        *(_one(p) for p in prompts),
        return_exceptions=return_exceptions,
    )
    return list(results)


# ---------------------------------------------------------------------------
# Sync façades — for callers that aren't async yet (CLI dispatchers,
# the hubs_batch / enrich_batch orchestrators). Wrap ``asyncio.run`` so
# the migration stays shallow.
# ---------------------------------------------------------------------------


def get_completion_sync(
    prompt: str,
    *,
    provider: str,
    model: str,
    op: str,
    max_tokens: int = 8000,
    system: str | None = None,
    mode: str = "mcp",
    session_id: str | None = None,
    response_format: dict | None = None,
) -> tuple[str, dict[str, int]]:
    return asyncio.run(
        get_completion(
            prompt,
            provider=provider,
            model=model,
            op=op,
            max_tokens=max_tokens,
            system=system,
            mode=mode,
            session_id=session_id,
            response_format=response_format,
        )
    )


def batch_completions_sync(
    prompts: list[str],
    *,
    provider: str,
    model: str,
    op: str,
    max_tokens: int = 8000,
    system: str | None = None,
    concurrency: int = 20,
    mode: str = "mcp",
    session_id: str | None = None,
    return_exceptions: bool = False,
    response_format: dict | None = None,
) -> list[tuple[str, dict[str, int]]]:
    return asyncio.run(
        batch_completions(
            prompts,
            provider=provider,
            model=model,
            op=op,
            max_tokens=max_tokens,
            system=system,
            concurrency=concurrency,
            mode=mode,
            session_id=session_id,
            return_exceptions=return_exceptions,
            response_format=response_format,
        )
    )
