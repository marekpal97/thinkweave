"""YouTube-transcript-api-based extraction for YouTube URLs.

Used by ``research-youtube-worker`` as the primary extractor — pulls the
auto-generated (or human-authored) captions YouTube already hosts, plus
segment timings. Unlike ``gemini_extract``, this module returns raw
transcript text; the worker derives the structured brief sections
(key_developments, key_moments, mentioned_links, topic_tags) itself by
reasoning over the transcript.

Designed to be runnable as a CLI helper for subagent workers, mirroring
``gemini_extract``'s shape so the worker can branch on a single ``ok``
field regardless of which backend is in play::

    python -m personal_mem.sources.extractors.transcript_extract youtube <url>

The command prints a single JSON line on stdout. On success the line
contains ``"ok": true`` plus ``transcript``, ``segments``,
``duration_sec``, ``language``; on failure ``"ok": false`` plus an
``error`` class and ``reason`` text.

Requires the ``[youtube]`` optional dep::

    pip install personal-mem[youtube]

No API key, no auth, no network egress beyond the YouTube captions
endpoint — works headless under cron.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

ERR_MISSING_SDK = "missing_sdk"
ERR_TRANSCRIPTS_DISABLED = "transcripts_disabled"
ERR_NO_TRANSCRIPTS = "no_transcripts"
ERR_VIDEO_UNAVAILABLE = "video_unavailable"
ERR_EMPTY_TRANSCRIPT = "empty_transcript"
ERR_API_ERROR = "transcript_api_failed"

MIN_TRANSCRIPT_CHARS = 500

DEFAULT_LANGUAGES: tuple[str, ...] = ("en", "en-US", "en-GB")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_transcript(
    url: str,
    *,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
) -> dict[str, Any]:
    """Fetch transcript + segment timings for a YouTube URL.

    Returns a structured dict — never raises. Workers should branch on
    ``result["ok"]``.

    On success::

        {"ok": True,
         "transcript": "<joined plaintext>",
         "segments": [{"start": float, "duration": float, "text": str}, ...],
         "duration_sec": int,
         "language": str,
         "available_languages": list[{"code": str, "kind": "generated"|"manual"}],
         "model": "youtube-transcript-api"}

    On failure::

        {"ok": False, "error": <class>, "reason": <details>}

    Error classes:

    - ``missing_sdk`` — ``youtube-transcript-api`` not installed.
    - ``transcripts_disabled`` — channel owner disabled captions on this video.
    - ``no_transcripts`` — no transcript for any of the requested languages.
    - ``video_unavailable`` — private / removed / region-blocked.
    - ``empty_transcript`` — transcript fetched but body is below the
      minimum length threshold (mostly music or non-verbal content).
    - ``transcript_api_failed`` — any other SDK exception.
    """
    video_id = _video_id_from_url(url)
    if not video_id:
        return {
            "ok": False,
            "error": ERR_API_ERROR,
            "reason": f"could not parse video_id from url: {url!r}",
        }

    try:
        snippets, language, available = _fetch(video_id, languages)
    except ImportError:
        return {
            "ok": False,
            "error": ERR_MISSING_SDK,
            "reason": (
                "youtube-transcript-api not installed; "
                "run `pip install personal-mem[youtube]`"
            ),
        }
    except _Failure as fail:
        return {"ok": False, "error": fail.error_class, "reason": fail.reason}
    except Exception as exc:  # noqa: BLE001 — SDK can raise anything
        return {
            "ok": False,
            "error": ERR_API_ERROR,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    transcript_text = " ".join(s["text"] for s in snippets).strip()
    if len(transcript_text) < MIN_TRANSCRIPT_CHARS:
        return {
            "ok": False,
            "error": ERR_EMPTY_TRANSCRIPT,
            "reason": (
                f"transcript only {len(transcript_text)} chars (< {MIN_TRANSCRIPT_CHARS}); "
                "video likely non-verbal (music, silent demo, etc.)"
            ),
        }

    duration_sec = 0
    if snippets:
        last = snippets[-1]
        duration_sec = int(last["start"] + last["duration"])

    return {
        "ok": True,
        "transcript": transcript_text,
        "segments": snippets,
        "duration_sec": duration_sec,
        "language": language,
        "available_languages": available,
        "model": "youtube-transcript-api",
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _Failure(Exception):
    """Carries a structured-error classification out of ``_fetch``."""

    def __init__(self, error_class: str, reason: str) -> None:
        super().__init__(reason)
        self.error_class = error_class
        self.reason = reason


def _fetch(
    video_id: str, languages: tuple[str, ...]
) -> tuple[list[dict[str, Any]], str, list[dict[str, str]]]:
    """Lazy SDK import + fetch. Maps SDK exceptions to ``_Failure``.

    Isolated so tests can monkeypatch it wholesale, the way
    ``gemini_extract._call_gemini_for_youtube`` is isolated.
    """
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import-not-found]

    api = YouTubeTranscriptApi()

    available: list[dict[str, str]] = []
    try:
        tlist = api.list(video_id)
        for t in tlist:
            available.append(
                {
                    "code": t.language_code,
                    "kind": "generated" if t.is_generated else "manual",
                }
            )
    except Exception as exc:  # noqa: BLE001
        _raise_classified(exc)

    try:
        fetched = api.fetch(video_id, languages=list(languages))
    except Exception as exc:  # noqa: BLE001
        _raise_classified(exc)

    language = getattr(fetched, "language_code", "") or (languages[0] if languages else "")
    snippets = [
        {
            "start": float(getattr(s, "start", 0.0)),
            "duration": float(getattr(s, "duration", 0.0)),
            "text": str(getattr(s, "text", "")),
        }
        for s in fetched
    ]
    return snippets, language, available


def _raise_classified(exc: Exception) -> None:
    """Map an SDK exception to a ``_Failure`` with a stable error class.

    The library exposes specific exception classes at package root
    (``TranscriptsDisabled``, ``NoTranscriptFound``, ``VideoUnavailable``,
    ``CouldNotRetrieveTranscript``) — we classify by classname rather
    than ``isinstance`` to avoid importing them at module top, keeping
    the SDK import lazy.
    """
    name = type(exc).__name__
    msg = str(exc) or name
    if name == "TranscriptsDisabled":
        raise _Failure(ERR_TRANSCRIPTS_DISABLED, msg)
    if name in ("NoTranscriptFound", "NotTranslatable"):
        raise _Failure(ERR_NO_TRANSCRIPTS, msg)
    if name == "VideoUnavailable":
        raise _Failure(ERR_VIDEO_UNAVAILABLE, msg)
    raise _Failure(ERR_API_ERROR, f"{name}: {msg}")


def _video_id_from_url(url: str) -> str:
    """Extract the 11-char video_id from any common YouTube URL form.

    Supports:
    - ``https://www.youtube.com/watch?v=VIDEOID``
    - ``https://youtu.be/VIDEOID``
    - ``https://www.youtube.com/shorts/VIDEOID``
    - Bare 11-char IDs passed through directly.
    """
    if not url:
        return ""
    if _VIDEO_ID_RE.match(url):
        return url
    patterns = (
        r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)"
        r"([A-Za-z0-9_-]{11})"
    )
    m = re.search(patterns, url)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcript_extract",
        description="Caption-based transcript extraction for YouTube URLs",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    yt = sub.add_parser("youtube", help="Extract transcript from a YouTube URL")
    yt.add_argument("url", help="YouTube video URL or 11-char video_id")
    yt.add_argument(
        "--lang",
        action="append",
        default=None,
        help="Preferred language code (repeatable). Default: en, en-US, en-GB.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "youtube":
        languages = tuple(args.lang) if args.lang else DEFAULT_LANGUAGES
        result = extract_youtube_transcript(args.url, languages=languages)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
