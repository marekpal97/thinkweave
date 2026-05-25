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
``google-genai`` is not installed — ``extract_youtube`` returns a
structured ``missing_sdk`` failure rather than raising at import time.

The binding targets ``google-genai >= 1.0`` (the unified SDK that
replaced the deprecated ``google-generativeai`` package in late 2025).

PR 2 (podcasts) will add ``extract_audio(file_path)`` to this same
module — same Gemini binding, same wrapper shape, different input type.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"
# Flash supports 65536 output tokens; we ask for a structured summary
# (no verbatim transcript) so typical responses are 2-5K tokens. The
# generous cap protects against unusually-long structured outputs
# (e.g. 3hr panel discussions with 40+ key moments) without truncation.
MAX_OUTPUT_TOKENS = 16384

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
You are extracting a structured brief from a YouTube video. Watch the \
video and reason directly over its content — do NOT return a verbatim \
transcript. Produce summary-grade output that's ready to drop into a \
research note.

Return ONLY a JSON object with these exact fields:

- "summary": 3-5 dense paragraphs covering the video's argument, \
evidence, and conclusions. Evidence-rich. State substance directly; \
do not use hedging language like "the video discusses" or "the \
speaker talks about". Reads like a smart colleague summarising what \
they just watched.

- "key_developments": list of 4-8 objects, each \
{"point": "<one-sentence assertion the video makes>", "evidence": \
"<the specific quote, data, citation, or demonstration the video \
uses to support it — quote verbatim where possible>"}. Capture \
distinct claims, not paraphrases of one claim.

- "key_moments": list of 5-10 objects, each \
{"timestamp": "MM:SS", "description": "<what happens at this \
timestamp>"}, marking the most informative points in the video. \
Use HH:MM:SS for videos over an hour.

- "mentioned_links": list of objects \
{"url": "<URL>", "context": "<one-line: why the speaker brought \
this up>"}, capturing every URL the speaker cites verbally or shows \
on screen. Empty list if none.

- "topic_tags": list of 5-10 short kebab-case tags describing the \
video's subject matter (e.g. "transformer-architecture", \
"federal-reserve", "post-training-rl"). These are hints for \
downstream concept extraction — favour specificity over breadth.

- "duration_sec": video length in seconds, as an integer.

Output only the JSON object — no preamble, no commentary."""


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

        {"ok": True, "summary": str,
         "key_developments": list[{point, evidence}],
         "key_moments": list[{timestamp, description}],
         "mentioned_links": list[{url, context}],
         "topic_tags": list[str], "duration_sec": int,
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
    if not api_key and not os.environ.get("GOOGLE_API_KEY"):
        _maybe_load_env_file()
    key = api_key or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return {
            "ok": False,
            "error": ERR_MISSING_API_KEY,
            "reason": "GOOGLE_API_KEY env var not set (checked env + .env in PERSONAL_MEM_VAULT and CWD)",
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
        "summary": str(parsed.get("summary", "") or ""),
        "key_developments": _coerce_key_developments(parsed.get("key_developments")),
        "key_moments": _coerce_key_moments(parsed.get("key_moments")),
        "mentioned_links": _coerce_mentioned_links(parsed.get("mentioned_links")),
        "topic_tags": _coerce_str_list(parsed.get("topic_tags")),
        "duration_sec": _coerce_int(parsed.get("duration_sec")),
        "model": model,
    }


def _call_gemini_for_youtube(api_key: str, model: str, url: str) -> str:
    """Invoke the SDK and return raw response text.

    Isolated in its own function so tests can monkeypatch this entire
    call (skipping both the lazy SDK import and the network round-trip)
    without touching the structured-result logic in ``extract_youtube``.

    The shape here targets ``google-genai >= 1.0`` — YouTube URLs are
    passed as a ``file_data`` part with the URL as ``file_uri``. Gemini
    2.5 Flash handles transcript extraction natively from the URL.

    Three production-critical settings:
    - ``response_mime_type='application/json'`` — Gemini guarantees
      well-formed JSON output; no need to strip code fences.
    - ``max_output_tokens=65536`` — Flash's actual max; the SDK default
      (8K) truncates real-world transcripts mid-string.
    - ``temperature=0.2`` — keep transcript faithful to source.

    Raises ``ImportError`` when ``google-genai`` is not installed;
    ``extract_youtube`` catches this and maps to ``missing_sdk``.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=types.Content(
            parts=[
                types.Part(file_data=types.FileData(file_uri=url)),
                types.Part(text=YOUTUBE_EXTRACT_PROMPT),
            ]
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.2,
        ),
    )
    return getattr(response, "text", "") or ""


def _maybe_load_env_file() -> None:
    """Best-effort .env loader — populates os.environ from .env files in
    well-known locations if the key isn't already exported. Called only
    when GOOGLE_API_KEY is missing from the live environment, so the
    common case (CI, properly-exported shell) pays nothing.

    Looks for ``.env`` at, in order:
      1. ``$PERSONAL_MEM_VAULT/.env`` — vault-scoped secrets
      2. ``Path.cwd() / '.env'`` — project-local development

    Silently ignores parse errors and missing files. Does not overwrite
    keys already set in the environment.
    """
    candidates: list[Path] = []
    vault = os.environ.get("PERSONAL_MEM_VAULT")
    if vault:
        candidates.append(Path(vault) / ".env")
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


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


def _coerce_key_developments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "point": str(item.get("point", "") or ""),
                "evidence": str(item.get("evidence", "") or ""),
            }
        )
    return out


def _coerce_mentioned_links(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "") or "").strip()
        if not url:
            continue
        out.append({"url": url, "context": str(item.get("context", "") or "")})
    return out


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


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
