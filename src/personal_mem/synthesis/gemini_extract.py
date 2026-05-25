"""Gemini-based extraction for media URLs (PR 1) and audio files (PR 2).

Used by ``research-youtube-worker`` to extract a transcript + summary
from a YouTube video URL in a single API call. Gemini 2.5 Flash accepts
YouTube URLs natively as ``file_data`` parts, so there is no audio
download or separate transcription pipeline — one call gives back
structured JSON.

Designed to be runnable as a CLI helper for subagent workers, who
shell out via Bash rather than importing Python:

    python -m personal_mem.synthesis.gemini_extract youtube <url>

The command prints a single JSON line on stdout. On success the line
contains ``"ok": true`` plus the transcript / summary / key moments;
on failure ``"ok": false`` plus an ``error`` class and ``reason`` text.
Exit code is ``0`` on success, ``1`` on a structured failure, ``2`` on
a usage error from argparse — workers should branch on the parsed
``ok`` field rather than on the exit code.

Requires ``GOOGLE_API_KEY`` and the ``[gemini]`` optional dep group::

    pip install personal-mem[gemini]
    export GOOGLE_API_KEY=...

The SDK import is lazy so the rest of personal_mem stays usable when
``google-generativeai`` is not installed — ``extract_youtube`` returns
a structured ``missing_sdk`` failure rather than raising at import time.

PR 2 (podcasts) will add ``extract_audio(file_path)`` to this same
module — same Gemini binding, same wrapper shape, different input type.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"

# Error classes returned in failure payloads. Workers branch on this
# field to choose a `fetch_failed.reason` for their JSON outcome line.
ERR_MISSING_SDK = "missing_sdk"
ERR_MISSING_API_KEY = "missing_api_key"
ERR_GEMINI_REFUSED = "gemini_refused"
ERR_INVALID_RESPONSE = "invalid_response"
ERR_API_ERROR = "api_error"

# Substrings in SDK exception messages that suggest a refusal rather
# than a transient API error. Kept conservative — false-positive
# `gemini_refused` is preferable to false-positive `api_error` because
# the worker handles them differently (refusal = archive failed,
# api_error = leave in queue for retry).
_REFUSAL_MARKERS = (
    "private",
    "age-restricted",
    "age restricted",
    "unavailable",
    "permission denied",
    "not accessible",
    "region",
    "geo",
    "blocked",
    "removed",
    "deleted",
)

YOUTUBE_EXTRACT_PROMPT = """\
Extract structured information from this YouTube video.

Return ONLY a JSON object with these exact fields:

- "transcript": the full spoken transcript, dialog-only, with light \
cleanup (filler words removed, sentence boundaries restored) but no \
edits that change meaning. If captions are available use them; \
otherwise transcribe from audio.
- "summary": a 3-5 paragraph summary of the video's argument, \
evidence, and conclusions. Dense and evidence-rich. Do not use \
hedging language like "the video discusses" or "the speaker \
talks about" — state the substance directly.
- "key_moments": list of 5-10 objects, each with \
{"timestamp": "MM:SS", "description": "..."}, marking the most \
informative points in the video.
- "duration_sec": video length in seconds, as an integer.

Output only the JSON object — no markdown fences, no preamble."""


def extract_youtube(
    url: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Extract transcript + summary from a YouTube URL via Gemini Flash.

    Returns a structured dict — never raises. Workers should call this
    and branch on ``result["ok"]``.

    On success::

        {"ok": True, "transcript": str, "summary": str,
         "key_moments": list[dict], "duration_sec": int,
         "model": str}

    On failure::

        {"ok": False, "error": <class>, "reason": <details>}

    Error classes (constants above):

    - ``missing_api_key`` — ``GOOGLE_API_KEY`` not set and ``api_key``
      not passed.
    - ``missing_sdk`` — ``google-generativeai`` not installed.
    - ``gemini_refused`` — SDK raised with a message matching a known
      refusal marker (private / age-gated / region-blocked / removed).
      The worker should archive this queue item as ``failed``.
    - ``api_error`` — any other SDK exception. Worker leaves the
      queue item for the next drain.
    - ``invalid_response`` — Gemini returned non-JSON or malformed
      JSON. Includes ``raw`` field with first 500 chars for debug.
    """
    key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return {
            "ok": False,
            "error": ERR_MISSING_API_KEY,
            "reason": "GOOGLE_API_KEY env var not set",
        }

    try:
        text = _call_gemini_for_youtube(key, model, url)
    except ImportError as exc:
        return {
            "ok": False,
            "error": ERR_MISSING_SDK,
            "reason": (
                "google-generativeai not installed; "
                "run `pip install personal-mem[gemini]`"
            ),
        }
    except Exception as exc:  # noqa: BLE001 — SDK can raise anything
        msg = str(exc)
        if _looks_like_refusal(msg):
            return {"ok": False, "error": ERR_GEMINI_REFUSED, "reason": msg}
        return {"ok": False, "error": ERR_API_ERROR, "reason": msg}

    parsed = _parse_json(text)
    if parsed is None:
        return {
            "ok": False,
            "error": ERR_INVALID_RESPONSE,
            "reason": "Gemini did not return parseable JSON",
            "raw": (text or "")[:500],
        }

    return {
        "ok": True,
        "transcript": str(parsed.get("transcript", "") or ""),
        "summary": str(parsed.get("summary", "") or ""),
        "key_moments": _coerce_key_moments(parsed.get("key_moments")),
        "duration_sec": _coerce_int(parsed.get("duration_sec")),
        "model": model,
    }


def _call_gemini_for_youtube(api_key: str, model: str, url: str) -> str:
    """Invoke the SDK and return raw response text.

    Isolated in its own function so tests can monkeypatch this entire
    call (skipping both the lazy SDK import and the network round-trip)
    without touching the structured-result logic in ``extract_youtube``.

    The shape here targets ``google-generativeai >= 0.8`` — YouTube URLs
    are passed as a ``file_data`` part with the URL as ``file_uri``.
    Gemini 2.5 Flash handles transcript extraction natively from the URL.

    Raises ``ImportError`` when ``google-generativeai`` is not installed;
    ``extract_youtube`` catches this and maps to ``missing_sdk``.
    """
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    client = genai.GenerativeModel(model)
    response = client.generate_content(
        [
            {"file_data": {"file_uri": url, "mime_type": "video/*"}},
            YOUTUBE_EXTRACT_PROMPT,
        ]
    )
    return getattr(response, "text", "") or ""


def _looks_like_refusal(msg: str) -> bool:
    """True if an SDK exception message suggests a refusal."""
    lower = (msg or "").lower()
    return any(marker in lower for marker in _REFUSAL_MARKERS)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON parser — strips ```json fences if present."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _coerce_key_moments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "timestamp": str(item.get("timestamp", "") or ""),
                "description": str(item.get("description", "") or ""),
            }
        )
    return out


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gemini_extract",
        description="Gemini-based extraction for media URLs and audio files",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    yt = sub.add_parser("youtube", help="Extract from a YouTube URL")
    yt.add_argument("url", help="YouTube video URL")
    yt.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")

    args = parser.parse_args(argv)

    if args.cmd == "youtube":
        result = extract_youtube(args.url, model=args.model)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
