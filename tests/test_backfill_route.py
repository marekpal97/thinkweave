"""Tests for ``operations/_backfill_route.choose_route``.

Verifies the decision matrix:

  • explicit --via wins (with batch→inline downgrade when no key)
  • size threshold + key presence determines batch vs inline default
  • the fallback is always inline (no key, or N ≤ threshold)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_mem.operations._backfill_route import choose_route


@pytest.fixture
def with_openai_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Seed an OpenAI key and isolate .env lookup."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
    monkeypatch.chdir(tmp_path)
    from personal_mem.core import api_keys
    monkeypatch.setattr(api_keys, "_PROJECT_ROOT", tmp_path)
    yield monkeypatch


@pytest.fixture
def without_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Strip all provider keys + isolate .env lookup."""
    for var in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "PERSONAL_MEM_VAULT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    from personal_mem.core import api_keys
    monkeypatch.setattr(api_keys, "_PROJECT_ROOT", tmp_path)
    yield monkeypatch


# ---- Rule 1: explicit --via wins -------------------------------------------


def test_explicit_inline_wins(with_openai_key):
    d = choose_route(via="inline", n_items=10_000)
    assert d.route == "inline"
    assert "user requested" in d.reason


def test_explicit_batch_wins_when_key_present(with_openai_key):
    d = choose_route(via="batch", n_items=5)
    assert d.route == "batch"


def test_explicit_batch_downgrades_when_no_key(without_keys):
    d = choose_route(via="batch", n_items=10_000)
    assert d.route == "inline"
    assert "no OPENAI_API_KEY" in d.reason


def test_explicit_batch_downgrades_with_anthropic_provider(without_keys):
    d = choose_route(via="batch", n_items=10_000, provider="anthropic")
    assert d.route == "inline"
    assert "no ANTHROPIC_API_KEY" in d.reason


def test_via_norm_case_insensitive_and_strips(with_openai_key):
    d = choose_route(via="  BATCH ", n_items=5)
    assert d.route == "batch"


# ---- Rule 2: size threshold + key ------------------------------------------


def test_above_threshold_with_key_picks_batch(with_openai_key):
    d = choose_route(via=None, n_items=500, default_threshold_n=200)
    assert d.route == "batch"
    assert "threshold 200" in d.reason


def test_above_threshold_without_key_picks_inline(without_keys):
    d = choose_route(via=None, n_items=500, default_threshold_n=200)
    assert d.route == "inline"
    assert "no OPENAI_API_KEY" in d.reason


def test_at_threshold_picks_inline(with_openai_key):
    # 200 == 200 is NOT strictly greater, so inline.
    d = choose_route(via=None, n_items=200, default_threshold_n=200)
    assert d.route == "inline"


# ---- Rule 3: fallback ------------------------------------------------------


def test_below_threshold_picks_inline_even_with_key(with_openai_key):
    d = choose_route(via=None, n_items=10, default_threshold_n=200)
    assert d.route == "inline"


def test_below_threshold_picks_inline_without_key(without_keys):
    d = choose_route(via=None, n_items=10, default_threshold_n=200)
    assert d.route == "inline"


# ---- Threshold override ----------------------------------------------------


def test_custom_threshold_respected(with_openai_key):
    # 50 items, threshold 25 → batch
    d = choose_route(via=None, n_items=50, default_threshold_n=25)
    assert d.route == "batch"
    # 50 items, threshold 100 → inline
    d = choose_route(via=None, n_items=50, default_threshold_n=100)
    assert d.route == "inline"


# ---- Provider plumbing -----------------------------------------------------


def test_provider_arg_determines_which_key_is_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # ANTHROPIC key is set; OPENAI is not.
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "PERSONAL_MEM_VAULT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anth")
    monkeypatch.chdir(tmp_path)
    from personal_mem.core import api_keys
    monkeypatch.setattr(api_keys, "_PROJECT_ROOT", tmp_path)

    # provider=anthropic → batch (key present)
    d_anth = choose_route(via=None, n_items=500, provider="anthropic")
    assert d_anth.route == "batch"
    # provider=openai → inline (no openai key)
    d_oai = choose_route(via=None, n_items=500, provider="openai")
    assert d_oai.route == "inline"
