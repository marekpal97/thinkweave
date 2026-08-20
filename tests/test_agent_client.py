"""Tests for ``core/agent_client.py`` — the completion wrapper.

Mocks ``openai.AsyncOpenAI`` so we don't issue real network calls.
Verifies:

  • provider→base_url dispatch
  • missing-key + unknown-provider raise ProviderError
  • batch_completions honors concurrency cap (semaphore)
  • return_exceptions semantics
  • sync façades wrap asyncio.run cleanly
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from thinkweave.core import agent_client
from thinkweave.core.agent_client import (
    ProviderError,
    batch_completions,
    batch_completions_sync,
    get_completion,
    get_completion_sync,
    supported_providers,
)


# ---------------------------------------------------------------------------
# Fake AsyncOpenAI infrastructure
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, text: str):
        self.message = SimpleNamespace(content=text)


class _FakeUsage:
    """Mimics ``openai.types.CompletionUsage``'s ``model_dump()`` contract."""
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def model_dump(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _FakeResponse:
    def __init__(self, text: str, *, prompt_tokens: int = 10, completion_tokens: int = 20):
        self.choices = [_FakeChoice(text)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, fake: "FakeAsyncOpenAI"):
        self._fake = fake

    async def create(self, *, model: str, messages: list[dict], **kwargs):
        # Capture every kwarg the wrapper forwards. ``response_format`` is
        # the load-bearing extra today; ``max_tokens`` vs
        # ``max_completion_tokens`` (gpt-5 family) is the other; **kwargs
        # keeps the fake future-proof for whatever else the wrapper
        # threads through next.
        call_log: dict[str, Any] = {"model": model, "messages": messages}
        call_log.update(kwargs)
        self._fake.calls.append(call_log)
        # Optional throttle to verify concurrency caps.
        if self._fake.delay:
            await self._fake.tick(self._fake.delay)
        return _FakeResponse(self._fake.canned_text)


class _FakeChat:
    def __init__(self, fake: "FakeAsyncOpenAI"):
        self.completions = _FakeCompletions(fake)


class FakeAsyncOpenAI:
    """Drop-in replacement for ``openai.AsyncOpenAI``.

    Tracks every call site and lets tests assert on the ``base_url`` /
    ``api_key`` passed at construction time. ``delay`` (seconds) gates
    every completion through an asyncio sleep so concurrency caps are
    observable.
    """

    instances: list["FakeAsyncOpenAI"] = []

    def __init__(self, *, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url
        self.calls: list[dict] = []
        self.canned_text = "OK"
        self.delay = 0.0
        self.chat = _FakeChat(self)
        FakeAsyncOpenAI.instances.append(self)

    async def tick(self, secs: float) -> None:
        await asyncio.sleep(secs)


@pytest.fixture(autouse=True)
def reset_instances():
    FakeAsyncOpenAI.instances.clear()
    yield
    FakeAsyncOpenAI.instances.clear()


@pytest.fixture
def patched_sdk(monkeypatch: pytest.MonkeyPatch):
    """Replace ``openai.AsyncOpenAI`` with the fake, and seed a key."""
    import sys
    fake_module = SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    # ``get_provider_key`` reads env; seed a valid key per provider so the
    # wrapper builds a client instead of raising.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anth")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-gem")
    yield monkeypatch


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_supported_providers_contains_three():
    assert set(supported_providers()) == {"openai", "anthropic", "gemini"}


# ---------------------------------------------------------------------------
# get_completion
# ---------------------------------------------------------------------------


def test_get_completion_returns_text_and_usage(patched_sdk):
    text, usage = asyncio.run(
        get_completion(
            "hello",
            provider="openai",
            model="gpt-5-mini",
        )
    )
    assert text == "OK"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


def test_get_completion_dispatches_provider_url(patched_sdk):
    asyncio.run(get_completion("p", provider="openai", model="m"))
    asyncio.run(get_completion("p", provider="anthropic", model="m"))
    asyncio.run(get_completion("p", provider="gemini", model="m"))
    urls = [inst.base_url for inst in FakeAsyncOpenAI.instances]
    # Order matches call order. OpenAI is None (SDK default).
    assert urls == [
        None,
        "https://api.anthropic.com/v1/",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    ]


def test_get_completion_unknown_provider_raises(patched_sdk):
    with pytest.raises(ProviderError, match="unknown provider"):
        asyncio.run(
            get_completion("p", provider="cohere", model="m")
        )


def test_get_completion_missing_key_raises(patched_sdk, monkeypatch, tmp_path):
    # Strip the openai key so the wrapper has nothing to use.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Also isolate .env lookup paths so we don't accidentally pick up
    # the project root's real key. ``tmp_path`` (not
    # ``tempfile.TemporaryDirectory``) because ``monkeypatch.chdir`` is
    # only undone at fixture teardown: on Windows the live process CWD
    # keeps an open handle on the directory, so an eager rmtree inside
    # the test body fails with WinError 32.
    from thinkweave.core import api_keys
    monkeypatch.setattr(api_keys, "_PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THINKWEAVE_VAULT", raising=False)
    with pytest.raises(ProviderError, match="no API key found"):
        asyncio.run(
            get_completion("p", provider="openai", model="m")
        )


def test_get_completion_builds_messages_with_system_prompt(patched_sdk):
    asyncio.run(
        get_completion(
            "user-text",
            provider="openai",
            model="m",
            system="be terse",
        )
    )
    msgs = FakeAsyncOpenAI.instances[0].calls[0]["messages"]
    assert msgs == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "user-text"},
    ]


def test_get_completion_omits_system_when_none(patched_sdk):
    asyncio.run(get_completion("p", provider="openai", model="m"))
    msgs = FakeAsyncOpenAI.instances[0].calls[0]["messages"]
    assert msgs == [{"role": "user", "content": "p"}]


def test_get_completion_passes_max_tokens(patched_sdk):
    asyncio.run(
        get_completion(
            "p", provider="openai", model="m", max_tokens=4096
        )
    )
    assert FakeAsyncOpenAI.instances[0].calls[0]["max_tokens"] == 4096


def test_get_completion_gpt5_uses_max_completion_tokens(patched_sdk):
    """OpenAI's gpt-5 family rejects ``max_tokens`` at the API level — the
    wrapper must translate to ``max_completion_tokens`` while keeping its
    public kwarg name unchanged."""
    asyncio.run(
        get_completion(
            "p",
            provider="openai",
            model="gpt-5-mini",
            max_tokens=2048,
        )
    )
    call = FakeAsyncOpenAI.instances[0].calls[0]
    assert call["max_completion_tokens"] == 2048
    assert "max_tokens" not in call


def test_get_completion_gpt4_keeps_max_tokens(patched_sdk):
    """Regression guard: the gpt-5 translation must NOT fire for gpt-4
    family models — they still use classic ``max_tokens``."""
    asyncio.run(
        get_completion(
            "p",
            provider="openai",
            model="gpt-4o-mini",
            max_tokens=1024,
        )
    )
    call = FakeAsyncOpenAI.instances[0].calls[0]
    assert call["max_tokens"] == 1024
    assert "max_completion_tokens" not in call


def test_get_completion_anthropic_keeps_max_tokens(patched_sdk):
    """The gpt-5 prefix check is provider-gated: an Anthropic model must
    still receive ``max_tokens`` even if some future model name shares a
    prefix shape with OpenAI's gpt-* family."""
    asyncio.run(
        get_completion(
            "p",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
        )
    )
    call = FakeAsyncOpenAI.instances[0].calls[0]
    assert call["max_tokens"] == 512
    assert "max_completion_tokens" not in call


def test_get_completion_forwards_response_format(patched_sdk):
    """When ``response_format`` is set, the wrapper threads it straight into
    the SDK call so the provider enforces the JSON-only contract at the API
    level — not just via prompt convention."""
    asyncio.run(
        get_completion(
            "p",
            provider="openai",
            model="m",
            response_format={"type": "json_object"},
        )
    )
    call = FakeAsyncOpenAI.instances[0].calls[0]
    assert call["response_format"] == {"type": "json_object"}


def test_get_completion_omits_response_format_when_none(patched_sdk):
    """Default path: ``response_format`` is absent from the SDK call entirely
    (the OpenAI SDK rejects ``response_format=None``, so the wrapper must
    drop the kwarg rather than forward a null)."""
    asyncio.run(
        get_completion("p", provider="openai", model="m")
    )
    call = FakeAsyncOpenAI.instances[0].calls[0]
    assert "response_format" not in call


# ---------------------------------------------------------------------------
# batch_completions
# ---------------------------------------------------------------------------


def test_batch_completions_returns_per_prompt(patched_sdk):
    results = asyncio.run(
        batch_completions(
            ["a", "b", "c"],
            provider="openai",
            model="m",
        )
    )
    assert len(results) == 3
    for text, usage in results:
        assert text == "OK"
        assert usage["prompt_tokens"] == 10


def test_batch_completions_empty_returns_empty(patched_sdk):
    results = asyncio.run(
        batch_completions([], provider="openai", model="m")
    )
    assert results == []


def test_batch_completions_honors_concurrency_cap(patched_sdk):
    """Run 6 prompts with concurrency=2 against a fake that gates each
    completion behind a tracked semaphore — verify never more than 2 in
    flight at once."""
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _tracked_tick(self, secs):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(secs)
        async with lock:
            in_flight -= 1

    # Monkey-patch the fake to call our tracker per completion.
    FakeAsyncOpenAI.tick = _tracked_tick  # type: ignore[assignment]

    # Seed the fake's delay so each completion takes a measurable slice.
    async def _runner():
        # Construct the clients first so we can set the delay.
        # Easier: pre-set the delay via fixture's first instance after
        # the wrapper builds it. We rely on the wrapper building one
        # client per call (current behaviour) — so seed via monkey-patch.
        original_init = FakeAsyncOpenAI.__init__

        def patched_init(self, *a, **kw):
            original_init(self, *a, **kw)
            self.delay = 0.05

        FakeAsyncOpenAI.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await batch_completions(
                ["p"] * 6,
                provider="openai",
                model="m",
                concurrency=2,
            )
        finally:
            FakeAsyncOpenAI.__init__ = original_init  # type: ignore[assignment]

    asyncio.run(_runner())
    assert peak <= 2, f"concurrency cap breached: peak={peak}"
    assert peak >= 1


def test_batch_completions_return_exceptions_partial(patched_sdk, monkeypatch):
    """When one prompt fails and return_exceptions=True, surviving slots
    still hold their (text, usage) tuples."""
    call_count = {"n": 0}

    original_create = _FakeCompletions.create

    async def flaky_create(self, *, model, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated rate limit")
        return await original_create(
            self, model=model, messages=messages, **kwargs
        )

    monkeypatch.setattr(_FakeCompletions, "create", flaky_create)
    results = asyncio.run(
        batch_completions(
            ["a", "b", "c"],
            provider="openai",
            model="m",
            return_exceptions=True,
            concurrency=1,  # serialize so call order is deterministic
        )
    )
    assert isinstance(results[1], RuntimeError)
    assert isinstance(results[0], tuple)
    assert isinstance(results[2], tuple)


def test_batch_completions_fail_fast_raises(patched_sdk, monkeypatch):
    async def broken_create(self, *, model, messages, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_FakeCompletions, "create", broken_create)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            batch_completions(
                ["a", "b"],
                provider="openai",
                model="m",
            )
        )


# ---------------------------------------------------------------------------
# Bounded admission (#176 / #177)
#
# The concurrency knob must bound how many per-prompt *tasks exist*, not
# just how many provider calls are in flight. These tests patch
# ``get_completion`` (the seam below ``batch_completions``) so no SDK, no
# network, and no clock is involved — the gate is an ``asyncio.Event``.
# ---------------------------------------------------------------------------


def _gated_completion(gate: asyncio.Event, log: list[str], live: dict[str, int]):
    """Build a ``get_completion`` stand-in that parks on ``gate`` and
    records entry order + live-call bookkeeping."""

    async def _stub(prompt: str, **kwargs) -> tuple[str, dict[str, int]]:
        log.append(prompt)
        live["now"] += 1
        live["peak_calls"] = max(live["peak_calls"], live["now"])
        live["peak_tasks"] = max(live["peak_tasks"], len(asyncio.all_tasks()))
        try:
            await gate.wait()
        finally:
            live["now"] -= 1
        return f"reply:{prompt}", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    return _stub


def test_batch_completions_bounds_task_admission(monkeypatch):
    """AC1 + AC5: with N=64 prompts and concurrency=4, at most ~4 per-prompt
    tasks may EXIST at any moment (not merely 4 active calls).

    The expected ceiling is hand-derived, not recomputed from the code:
    4 worker tasks + the ``batch_completions`` task + this test's driver
    task = 6. The pre-fix ``asyncio.gather``-over-N implementation parks
    64 tasks on the semaphore, so it lands at 66.
    """
    n_prompts, cap = 64, 4
    gate = asyncio.Event()
    log: list[str] = []
    live = {"now": 0, "peak_calls": 0, "peak_tasks": 0}
    monkeypatch.setattr(
        agent_client, "get_completion", _gated_completion(gate, log, live)
    )

    async def _runner():
        batch = asyncio.create_task(
            batch_completions(
                [f"p{i}" for i in range(n_prompts)],
                provider="openai",
                model="m",
                concurrency=cap,
            )
        )
        # Let every task the implementation wants to create get scheduled.
        for _ in range(20):
            await asyncio.sleep(0)
        admitted = len(log)
        gate.set()
        return admitted, await batch

    admitted, results = asyncio.run(_runner())

    assert admitted == cap, f"admitted {admitted} prompts with concurrency={cap}"
    assert live["peak_tasks"] <= cap + 2, (
        f"per-prompt task admission unbounded: {live['peak_tasks']} tasks alive "
        f"with concurrency={cap} over {n_prompts} prompts"
    )
    assert live["peak_calls"] <= cap
    assert len(results) == n_prompts


def test_batch_completions_preserves_positional_order(monkeypatch):
    """AC2: results line up 1:1 with the input prompts even when the
    completions finish out of order (each prompt's stub sleeps for a
    duration that inverts the input ordering)."""
    prompts = [f"p{i}" for i in range(8)]

    async def _reordering_stub(prompt: str, **kwargs):
        # p0 sleeps longest, p7 shortest → completion order reverses input.
        await asyncio.sleep((8 - int(prompt[1:])) * 0.005)
        return f"reply:{prompt}", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(agent_client, "get_completion", _reordering_stub)
    results = asyncio.run(
        batch_completions(prompts, provider="openai", model="m", concurrency=8)
    )
    assert [text for text, _ in results] == [f"reply:{p}" for p in prompts]


def test_batch_completions_fail_fast_does_not_start_remaining(monkeypatch):
    """AC3: the first failure propagates unwrapped AND the un-admitted
    prompts are never started — with 32 prompts, concurrency 2 and the
    first prompt raising, at most a couple of stubs may ever run."""
    seen: list[str] = []

    async def _stub(prompt: str, **kwargs):
        seen.append(prompt)
        await asyncio.sleep(0)
        if prompt == "p0":
            raise RuntimeError("boom")
        return "OK", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(agent_client, "get_completion", _stub)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            batch_completions(
                [f"p{i}" for i in range(32)],
                provider="openai",
                model="m",
                concurrency=2,
            )
        )
    assert len(seen) <= 4, f"kept working after a fail-fast failure: {seen}"


def test_batch_completions_return_exceptions_keeps_slots(monkeypatch):
    """AC4: with ``return_exceptions=True`` every slot is either a
    ``(text, usage)`` tuple or the exception instance, in position — here
    the odd-indexed prompts fail."""
    prompts = [f"p{i}" for i in range(10)]

    async def _stub(prompt: str, **kwargs):
        await asyncio.sleep(0)
        if int(prompt[1:]) % 2:
            raise ValueError(f"bad {prompt}")
        return f"reply:{prompt}", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(agent_client, "get_completion", _stub)
    results = asyncio.run(
        batch_completions(
            prompts,
            provider="openai",
            model="m",
            concurrency=3,
            return_exceptions=True,
        )
    )
    for i, slot in enumerate(results):
        if i % 2:
            assert isinstance(slot, ValueError) and str(slot) == f"bad p{i}"
        else:
            assert slot[0] == f"reply:p{i}"


# ---------------------------------------------------------------------------
# Sync façades
# ---------------------------------------------------------------------------


def test_get_completion_sync(patched_sdk):
    text, usage = get_completion_sync(
        "p", provider="openai", model="m"
    )
    assert text == "OK"
    assert usage["total_tokens"] == 30


def test_batch_completions_sync(patched_sdk):
    results = batch_completions_sync(
        ["a", "b"], provider="openai", model="m"
    )
    assert len(results) == 2
