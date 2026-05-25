"""Unit tests for the Gemini-based extraction module.

The SDK call (``_call_gemini_for_youtube``) is mocked end-to-end; these
tests verify the result-shaping, JSON parsing, refusal classification,
and graceful degradation when the SDK is unavailable.
"""

from __future__ import annotations

import builtins
import json
import sys
from typing import Any

import pytest

from personal_mem.synthesis import gemini_extract as ge


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def _good_payload() -> dict[str, Any]:
    return {
        "summary": "A test video about testing.",
        "key_developments": [
            {"point": "Tests pass when written first.", "evidence": "Demo at 02:14"},
            {"point": "Mocks reduce coupling.", "evidence": "Quote from slide 4"},
        ],
        "key_moments": [
            {"timestamp": "00:30", "description": "Intro ends"},
            {"timestamp": "02:15", "description": "Main argument"},
        ],
        "mentioned_links": [
            {"url": "https://example.com/talk", "context": "linked in description"},
        ],
        "topic_tags": ["testing", "software-engineering"],
        "duration_sec": 314,
    }


def test_extract_youtube_returns_parsed_payload(monkeypatch):
    monkeypatch.setattr(
        ge,
        "_call_gemini_for_youtube",
        lambda *a, **kw: json.dumps(_good_payload()),
    )
    result = ge.extract_youtube(
        "https://youtu.be/abc", api_key="fake-key"
    )
    assert result["ok"] is True
    assert result["summary"] == "A test video about testing."
    assert len(result["key_developments"]) == 2
    assert result["key_developments"][0]["point"].startswith("Tests pass")
    assert result["key_developments"][0]["evidence"] == "Demo at 02:14"
    assert len(result["key_moments"]) == 2
    assert result["key_moments"][0]["timestamp"] == "00:30"
    assert result["mentioned_links"][0]["url"] == "https://example.com/talk"
    assert "testing" in result["topic_tags"]
    assert result["duration_sec"] == 314
    assert result["model"] == ge.DEFAULT_MODEL


def test_extract_youtube_strips_code_fences(monkeypatch):
    fenced = "```json\n" + json.dumps(_good_payload()) + "\n```"
    monkeypatch.setattr(ge, "_call_gemini_for_youtube", lambda *a, **kw: fenced)
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is True
    assert result["duration_sec"] == 314


def test_extract_youtube_tolerates_missing_optional_fields(monkeypatch):
    """A payload with only ``summary`` set should still succeed —
    list fields default to empty, duration_sec to 0."""
    monkeypatch.setattr(
        ge,
        "_call_gemini_for_youtube",
        lambda *a, **kw: json.dumps({"summary": "just a summary"}),
    )
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is True
    assert result["summary"] == "just a summary"
    assert result["key_developments"] == []
    assert result["key_moments"] == []
    assert result["mentioned_links"] == []
    assert result["topic_tags"] == []
    assert result["duration_sec"] == 0


def test_extract_youtube_skips_links_without_url(monkeypatch):
    """mentioned_links entries must have a non-empty URL or they're dropped."""
    payload = {
        "summary": "x",
        "mentioned_links": [
            {"url": "", "context": "empty"},
            {"url": "https://valid.example", "context": "good"},
            {"context": "missing url field"},
        ],
    }
    monkeypatch.setattr(ge, "_call_gemini_for_youtube", lambda *a, **kw: json.dumps(payload))
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is True
    assert len(result["mentioned_links"]) == 1
    assert result["mentioned_links"][0]["url"] == "https://valid.example"


# ---------------------------------------------------------------------------
# Missing-prerequisite degradation (no SDK / no API key)
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_structured_failure(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Disable the .env file loader so a developer's real .env on PATH
    # doesn't accidentally populate the env mid-test.
    monkeypatch.setattr(ge, "_maybe_load_env_file", lambda: None)
    result = ge.extract_youtube("https://youtu.be/abc")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_MISSING_API_KEY


def test_missing_sdk_returns_structured_failure(monkeypatch):
    """Simulate ``google-genai`` not installed by forcing an ImportError
    on the lazy import inside ``extract_youtube``."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        # Block the new SDK (`google.genai`) but not the older
        # `google.generativeai` namespace siblings that may be on PATH.
        if name == "google.genai" or name.startswith("google.genai."):
            raise ImportError("No module named google.genai")
        # `from google import genai` resolves via the `google` package
        # and then imports the `genai` submodule — block that too.
        if name == "google" and "genai" in (kwargs.get("fromlist") or args[2] if len(args) >= 3 else ()):
            # Let the import of `google` succeed but the submodule lookup fail.
            pass
        return real_import(name, *args, **kwargs)

    # Drop any cached SDK module so the lazy import re-runs.
    for mod_name in [m for m in sys.modules if m.startswith("google.genai")]:
        monkeypatch.delitem(sys.modules, mod_name, raising=False)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_MISSING_SDK
    assert "personal-mem[gemini]" in result["reason"]


# ---------------------------------------------------------------------------
# SDK error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_msg",
    [
        "Video is private",
        "This video has been removed",
        "Content is age-restricted",
        "Region blocked: not accessible in your geo",
        "permission denied for the requested resource",
    ],
)
def test_sdk_exceptions_classified_as_refusal(monkeypatch, exc_msg):
    def boom(*a, **kw):
        raise RuntimeError(exc_msg)

    monkeypatch.setattr(ge, "_call_gemini_for_youtube", boom)
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_GEMINI_REFUSED
    assert exc_msg in result["reason"]


@pytest.mark.parametrize(
    "exc_msg",
    [
        "Rate limit exceeded",
        "Internal server error",
        "Connection reset",
        "Quota exhausted for the day",
    ],
)
def test_sdk_exceptions_classified_as_api_error(monkeypatch, exc_msg):
    def boom(*a, **kw):
        raise RuntimeError(exc_msg)

    monkeypatch.setattr(ge, "_call_gemini_for_youtube", boom)
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_API_ERROR
    assert exc_msg in result["reason"]


# ---------------------------------------------------------------------------
# Invalid response handling
# ---------------------------------------------------------------------------


def test_non_json_response_returns_invalid_response(monkeypatch):
    monkeypatch.setattr(
        ge,
        "_call_gemini_for_youtube",
        lambda *a, **kw: "I cannot extract this video as JSON.",
    )
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_INVALID_RESPONSE
    assert "raw" in result
    assert result["raw"].startswith("I cannot")


def test_empty_response_returns_invalid_response(monkeypatch):
    monkeypatch.setattr(ge, "_call_gemini_for_youtube", lambda *a, **kw: "")
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_INVALID_RESPONSE


def test_json_array_response_returns_invalid_response(monkeypatch):
    """A JSON array (not an object) is invalid — the prompt asks for an object."""
    monkeypatch.setattr(
        ge,
        "_call_gemini_for_youtube",
        lambda *a, **kw: json.dumps(["transcript", "summary"]),
    )
    result = ge.extract_youtube("https://youtu.be/abc", api_key="k")
    assert result["ok"] is False
    assert result["error"] == ge.ERR_INVALID_RESPONSE


# ---------------------------------------------------------------------------
# Refusal-marker heuristic
# ---------------------------------------------------------------------------


def test_looks_like_refusal_matches_known_phrases():
    assert ge._looks_like_refusal("Video is private")
    assert ge._looks_like_refusal("BLOCKED")
    assert ge._looks_like_refusal("permission denied")


def test_looks_like_refusal_misses_unrelated_errors():
    assert not ge._looks_like_refusal("Rate limit hit")
    assert not ge._looks_like_refusal("Internal server error")
    assert not ge._looks_like_refusal("")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_youtube_success_prints_json_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(
        ge,
        "_call_gemini_for_youtube",
        lambda *a, **kw: json.dumps(_good_payload()),
    )
    exit_code = ge.main(["youtube", "https://youtu.be/abc", "--model", "gemini-2.5-flash"])
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["duration_sec"] == 314


def test_main_youtube_failure_returns_one(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(ge, "_maybe_load_env_file", lambda: None)
    exit_code = ge.main(["youtube", "https://youtu.be/abc"])
    assert exit_code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == ge.ERR_MISSING_API_KEY
