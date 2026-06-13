"""Unit tests for the youtube-transcript-api extraction module.

The SDK fetch (``_fetch``) is mocked end-to-end; these tests verify
result shaping, error classification, video-id parsing, empty-transcript
detection, and the CLI entry point.
"""

from __future__ import annotations

import json

import pytest

from personal_mem.acquisition.sources.extractors import transcript_extract as te


# ---------------------------------------------------------------------------
# Video-id parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/watch?v=NKwIX3CiRgU", "NKwIX3CiRgU"),
        ("https://youtu.be/NKwIX3CiRgU", "NKwIX3CiRgU"),
        ("https://www.youtube.com/shorts/NKwIX3CiRgU", "NKwIX3CiRgU"),
        ("https://www.youtube.com/embed/NKwIX3CiRgU", "NKwIX3CiRgU"),
        ("https://www.youtube.com/watch?feature=share&v=NKwIX3CiRgU", "NKwIX3CiRgU"),
        ("NKwIX3CiRgU", "NKwIX3CiRgU"),
    ],
)
def test_video_id_from_url_parses_common_forms(url, expected):
    assert te._video_id_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "https://example.com/watch?v=abc", "https://youtu.be/short"],
)
def test_video_id_from_url_rejects_bad_input(url):
    assert te._video_id_from_url(url) == ""


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def _good_segments() -> list[dict]:
    # Build ~600 chars to clear the empty-transcript guard (500 char min).
    base = [
        {"start": 0.0, "duration": 3.5, "text": "Welcome to the talk on agent autonomy."},
        {"start": 3.5, "duration": 4.2, "text": "Today we will cover three patterns of bounded delegation."},
        {"start": 7.7, "duration": 5.1, "text": "First, the principle of least surprise applied to LLM workflows."},
        {"start": 12.8, "duration": 6.0, "text": "Second, how to write evaluation harnesses you actually trust."},
        {"start": 18.8, "duration": 5.4, "text": "Third, a Darwinian skills library where weak skills get pruned automatically."},
        {"start": 24.2, "duration": 4.8, "text": "Let me start with the data that motivated this work last quarter."},
        {"start": 29.0, "duration": 6.3, "text": "We saw a hundred-fold reduction in latency by replacing context blobs with file systems."},
        {"start": 35.3, "duration": 4.7, "text": "The agents collaborate the way researchers do, through shared artifacts."},
    ]
    return base


def test_extract_success_returns_joined_transcript(monkeypatch):
    segments = _good_segments()
    monkeypatch.setattr(
        te,
        "_fetch",
        lambda vid, langs: (segments, "en", [{"code": "en", "kind": "generated"}]),
    )
    result = te.extract_youtube_transcript("https://youtu.be/NKwIX3CiRgU")
    assert result["ok"] is True
    assert result["language"] == "en"
    assert result["model"] == "youtube-transcript-api"
    # transcript is space-joined segment text
    assert "agent autonomy" in result["transcript"]
    assert "Darwinian" in result["transcript"]
    # segments preserved
    assert len(result["segments"]) == len(segments)
    assert result["segments"][0]["start"] == 0.0
    # duration_sec derived from last segment's start+duration
    last = segments[-1]
    assert result["duration_sec"] == int(last["start"] + last["duration"])
    # available_languages flows through
    assert result["available_languages"] == [{"code": "en", "kind": "generated"}]


def test_extract_empty_transcript_under_threshold(monkeypatch):
    """A very short transcript (under MIN_TRANSCRIPT_CHARS) returns empty_transcript."""
    short = [{"start": 0.0, "duration": 1.0, "text": "music"}]
    monkeypatch.setattr(te, "_fetch", lambda vid, langs: (short, "en", []))
    result = te.extract_youtube_transcript("https://youtu.be/NKwIX3CiRgU")
    assert result["ok"] is False
    assert result["error"] == te.ERR_EMPTY_TRANSCRIPT


def test_extract_bad_url_returns_api_error():
    result = te.extract_youtube_transcript("not a youtube url at all")
    assert result["ok"] is False
    assert result["error"] == te.ERR_API_ERROR
    assert "could not parse video_id" in result["reason"]


# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------


def _make_exc(name: str, msg: str = "boom") -> Exception:
    """Build an exception whose ``type(...).__name__`` matches the SDK class
    we want to simulate, without importing the SDK."""
    exc_cls = type(name, (Exception,), {})
    return exc_cls(msg)


@pytest.mark.parametrize(
    "exc_name, expected_error",
    [
        ("TranscriptsDisabled", te.ERR_TRANSCRIPTS_DISABLED),
        ("NoTranscriptFound", te.ERR_NO_TRANSCRIPTS),
        ("NotTranslatable", te.ERR_NO_TRANSCRIPTS),
        ("VideoUnavailable", te.ERR_VIDEO_UNAVAILABLE),
        ("CouldNotRetrieveTranscript", te.ERR_API_ERROR),
        ("RuntimeError", te.ERR_API_ERROR),
    ],
)
def test_sdk_exceptions_classified(monkeypatch, exc_name, expected_error):
    def boom(vid, langs):
        te._raise_classified(_make_exc(exc_name))

    monkeypatch.setattr(te, "_fetch", boom)
    result = te.extract_youtube_transcript("https://youtu.be/NKwIX3CiRgU")
    assert result["ok"] is False
    assert result["error"] == expected_error


def test_missing_sdk_returns_structured_failure(monkeypatch):
    def raise_import(vid, langs):
        raise ImportError("No module named youtube_transcript_api")

    monkeypatch.setattr(te, "_fetch", raise_import)
    result = te.extract_youtube_transcript("https://youtu.be/NKwIX3CiRgU")
    assert result["ok"] is False
    assert result["error"] == te.ERR_MISSING_SDK
    assert "personal-mem[youtube]" in result["reason"]


def test_unexpected_exception_classified_as_api_error(monkeypatch):
    """A non-``_Failure``, non-ImportError exception from ``_fetch`` ends up
    as ``transcript_api_failed`` with the type name in the reason."""

    def boom(vid, langs):
        raise ValueError("malformed thing")

    monkeypatch.setattr(te, "_fetch", boom)
    result = te.extract_youtube_transcript("https://youtu.be/NKwIX3CiRgU")
    assert result["ok"] is False
    assert result["error"] == te.ERR_API_ERROR
    assert "ValueError" in result["reason"]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_youtube_success_prints_json_and_returns_zero(monkeypatch, capsys):
    segments = _good_segments()
    monkeypatch.setattr(
        te,
        "_fetch",
        lambda vid, langs: (segments, "en", [{"code": "en", "kind": "generated"}]),
    )
    exit_code = te.main(["youtube", "https://youtu.be/NKwIX3CiRgU"])
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["language"] == "en"
    assert "transcript" in payload


def test_main_youtube_failure_returns_one(monkeypatch, capsys):
    def boom(vid, langs):
        te._raise_classified(_make_exc("TranscriptsDisabled", "owner disabled captions"))

    monkeypatch.setattr(te, "_fetch", boom)
    exit_code = te.main(["youtube", "https://youtu.be/NKwIX3CiRgU"])
    assert exit_code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == te.ERR_TRANSCRIPTS_DISABLED


def test_main_youtube_accepts_repeated_lang_flag(monkeypatch, capsys):
    captured_langs: list[tuple[str, ...]] = []

    def spy(vid, langs):
        captured_langs.append(tuple(langs))
        return (_good_segments(), langs[0], [])

    monkeypatch.setattr(te, "_fetch", spy)
    te.main(["youtube", "https://youtu.be/NKwIX3CiRgU", "--lang", "de", "--lang", "en"])
    assert captured_langs == [("de", "en")]
