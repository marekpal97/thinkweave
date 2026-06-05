"""Gemini-based extraction for media URLs (YouTube) and audio files (podcasts).

Used by ``research-youtube-worker`` and ``research-podcast-worker`` to
extract a transcript + summary from media in a single API call.

- YouTube: Gemini 2.5 Flash accepts YouTube URLs natively as ``file_data``
  parts — no audio download.
- Podcasts: arbitrary MP3 enclosure URLs are downloaded to a tempfile,
  uploaded via the Gemini Files API, then referenced in
  ``generate_content``. Gemini handles the transcription + structuring
  in one call.

Designed to be runnable as a CLI helper for subagent workers, who
shell out via Bash rather than importing Python:

    python -m personal_mem.sources.extractors.gemini_extract youtube <url>
    python -m personal_mem.sources.extractors.gemini_extract podcast <audio_url>

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
``google-genai`` is not installed — extractors return a structured
``missing_sdk`` failure rather than raising at import time.

The binding targets ``google-genai >= 1.0`` (the unified SDK that
replaced the deprecated ``google-generativeai`` package in late 2025).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"
# Flash's hard ceiling. Empirically a 37-min spoken-word podcast asking
# for summary + key_developments + key_moments + mentioned_links +
# topic_tags + speakers truncated mid-summary at 16K — the structured
# brief Gemini composes is much chattier than the "2-5K typical" the
# original YouTube path assumed. Run at the ceiling so 2-3hr panel
# episodes don't silently lose half their content.
MAX_OUTPUT_TOKENS = 65536

# Error classes returned in failure payloads. Workers branch on this
# field to choose a `fetch_failed.reason` for their JSON outcome line.
ERR_MISSING_SDK = "missing_sdk"
ERR_MISSING_API_KEY = "missing_api_key"
ERR_GEMINI_REFUSED = "gemini_refused"
ERR_INVALID_RESPONSE = "invalid_response"
ERR_API_ERROR = "api_error"
ERR_AUDIO_FETCH_FAILED = "audio_fetch_failed"
ERR_AUDIO_TOO_LARGE = "audio_too_large"
ERR_AUDIO_UPLOAD_FAILED = "audio_upload_failed"
ERR_AUDIO_PROCESSING_FAILED = "audio_processing_failed"

# Hard cap on downloaded audio size — Gemini Files API tops out at 2GB
# but realistic podcasts are 10-100MB. Anything over 500MB is almost
# certainly the wrong URL (a video, a live stream archive, a misrouted
# enclosure). Reject early to avoid a long wasted download.
MAX_AUDIO_BYTES = 500 * 1024 * 1024  # 500MB
# Streaming chunk size for the download — large enough to amortise
# per-iteration overhead, small enough that the cap check fires quickly.
DOWNLOAD_CHUNK = 1 << 20  # 1MB
# Files API polling cadence. Gemini processes a 1-hour MP3 in ~5-15s;
# poll at 2s for snappy worker latency.
FILE_PROCESS_POLL_SEC = 2.0
FILE_PROCESS_TIMEOUT_SEC = 300.0  # 5 min — covers ~4-hour podcasts

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


# JSON Schema for the podcast brief — enforced via Gemini's
# ``response_json_schema`` config so the model fills every required
# field. Empirically (verified 2026-05-26 on a 37-min Macro Trading
# Floor episode) Flash without an enforced schema returns only the
# first field of a multi-field prompt — the other fields go to ``null``
# or get omitted entirely. Schema enforcement is the structural fix.
PODCAST_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_developments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["point", "evidence"],
            },
        },
        "key_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["timestamp", "description"],
            },
        },
        "mentioned_links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["url", "context"],
            },
        },
        "topic_tags": {"type": "array", "items": {"type": "string"}},
        "speakers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["name", "role"],
            },
        },
        "duration_sec": {"type": "integer"},
    },
    "required": [
        "summary",
        "key_developments",
        "key_moments",
        "mentioned_links",
        "topic_tags",
        "speakers",
        "duration_sec",
    ],
}


PODCAST_EXTRACT_PROMPT = """\
You are extracting a structured brief from a podcast episode. Listen \
to the audio and reason directly over its content — do NOT return a \
verbatim transcript. Produce summary-grade output that's ready to \
drop into a research note.

This is spoken-word audio with no visual component. Identify speakers \
by voice when possible (host vs. guest), and quote them verbatim in \
the evidence half of key_developments.

Return ONLY a JSON object with these exact fields:

- "summary": 3-5 dense paragraphs covering the episode's argument, \
evidence, and conclusions. Evidence-rich. State substance directly; \
do not use hedging language like "the podcast discusses" or "the \
host talks about". Reads like a smart colleague summarising what \
they just listened to.

- "key_developments": list of 4-8 objects, each \
{"point": "<one-sentence assertion the episode makes>", "evidence": \
"<the specific quote, datum, or example used to support it — quote \
verbatim where possible, attributing to host/guest by role>"}. \
Capture distinct claims, not paraphrases of one claim.

- "key_moments": list of 5-10 objects, each \
{"timestamp": "MM:SS", "description": "<what happens at this \
timestamp>"}, marking the most informative points. Use HH:MM:SS for \
episodes over an hour. Anchor to the actual audio time.

- "mentioned_links": list of objects \
{"url": "<URL>", "context": "<one-line: why the speaker brought \
this up>"}, capturing every URL or named publication the speakers \
cite. Audio-only — speakers usually read URLs out loud or say \
"check out our website at ...". Empty list if none.

- "topic_tags": list of 5-10 short kebab-case tags describing the \
episode's subject matter (e.g. "federal-reserve", "fx-positioning", \
"yield-curve"). These are hints for downstream concept extraction — \
favour specificity over breadth.

- "speakers": list of objects \
{"name": "<host or guest name as spoken>", "role": "host|guest|panelist"}. \
Capture speakers identified by name in the episode (intro, outro, or \
mid-episode references). Empty list if not identifiable.

- "duration_sec": episode length in seconds, as an integer.

Output only the JSON object — no preamble, no commentary."""


def extract_audio(
    audio_url: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Extract transcript + summary from a podcast audio URL via Gemini Flash.

    Downloads the audio enclosure to a tempfile, uploads via the Gemini
    Files API, and prompts the model for a structured brief.

    Returns a structured dict — never raises. Workers should call this
    and branch on ``result["ok"]``.

    On success::

        {"ok": True, "summary": str,
         "key_developments": list[{point, evidence}],
         "key_moments": list[{timestamp, description}],
         "mentioned_links": list[{url, context}],
         "topic_tags": list[str], "speakers": list[{name, role}],
         "duration_sec": int, "model": str}

    On failure::

        {"ok": False, "error": <class>, "reason": <details>}

    Error classes (constants above):

    - ``missing_api_key`` / ``missing_sdk`` — same as ``extract_youtube``.
    - ``audio_fetch_failed`` — HTTP error or network failure downloading
      the enclosure URL. Reason carries the HTTP status / exception text.
    - ``audio_too_large`` — file exceeds ``MAX_AUDIO_BYTES`` (500MB).
    - ``audio_upload_failed`` — Files API upload raised.
    - ``audio_processing_failed`` — File reached FAILED state or
      poll timed out at ``FILE_PROCESS_TIMEOUT_SEC``.
    - ``gemini_refused`` / ``api_error`` / ``invalid_response`` — same
      semantics as ``extract_youtube`` but for the generate call.
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

    import tempfile

    audio_path: Path | None = None
    try:
        try:
            audio_path = _download_audio(audio_url)
        except _DownloadTooLarge as exc:
            return {"ok": False, "error": ERR_AUDIO_TOO_LARGE, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 — network can raise anything
            return {"ok": False, "error": ERR_AUDIO_FETCH_FAILED, "reason": str(exc)}

        try:
            text = _call_gemini_for_audio(key, model, audio_path)
        except ImportError:
            return {
                "ok": False,
                "error": ERR_MISSING_SDK,
                "reason": (
                    "google-genai not installed; "
                    "run `pip install personal-mem[gemini]`"
                ),
            }
        except _AudioUploadFailed as exc:
            return {"ok": False, "error": ERR_AUDIO_UPLOAD_FAILED, "reason": str(exc)}
        except _AudioProcessingFailed as exc:
            return {"ok": False, "error": ERR_AUDIO_PROCESSING_FAILED, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 — SDK can raise anything
            msg = str(exc)
            if _looks_like_refusal(msg):
                return {"ok": False, "error": ERR_GEMINI_REFUSED, "reason": msg}
            return {"ok": False, "error": ERR_API_ERROR, "reason": msg}
    finally:
        if audio_path is not None:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass

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
        "speakers": _coerce_speakers(parsed.get("speakers")),
        "duration_sec": _coerce_int(parsed.get("duration_sec")),
        "model": model,
    }


class _DownloadTooLarge(Exception):
    """Raised when the audio download exceeds MAX_AUDIO_BYTES."""


class _AudioUploadFailed(Exception):
    """Raised when client.files.upload fails for non-SDK-missing reasons."""


class _AudioProcessingFailed(Exception):
    """Raised when the uploaded File reaches FAILED state or polling times out."""


def _download_audio(url: str) -> Path:
    """Stream ``url`` to a NamedTemporaryFile and return its path.

    Streams via urllib (stdlib only — no httpx dep in the audio path) so
    that the cap check fires before the whole body is buffered. Raises
    ``_DownloadTooLarge`` when the running byte count exceeds the cap.

    Caller is responsible for ``Path.unlink`` after the upload completes.
    """
    import tempfile
    from urllib.request import Request, urlopen

    # Set a UA — many podcast hosts (Spreaker, Megaphone, etc.) 403
    # requests without one or with the default urllib UA.
    req = Request(url, headers={"User-Agent": "personal-mem/0.1 podcast-fetch"})
    with urlopen(req, timeout=60) as resp:
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_AUDIO_BYTES:
            raise _DownloadTooLarge(
                f"Content-Length {content_length} exceeds MAX_AUDIO_BYTES "
                f"({MAX_AUDIO_BYTES})"
            )
        # Suffix matters — Gemini Files API uses extension as a MIME hint.
        suffix = _audio_suffix_from_url_or_headers(url, resp.headers)
        fh = tempfile.NamedTemporaryFile(prefix="podcast_", suffix=suffix, delete=False)
        path = Path(fh.name)
        total = 0
        try:
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise _DownloadTooLarge(
                        f"Streamed bytes exceeded MAX_AUDIO_BYTES ({MAX_AUDIO_BYTES})"
                    )
                fh.write(chunk)
        finally:
            fh.close()
    return path


def _audio_suffix_from_url_or_headers(url: str, headers: Any) -> str:
    """Guess a sensible suffix for the tempfile based on URL or Content-Type.

    Gemini's Files API infers MIME from extension; getting this wrong
    causes a confusing rejection. Default to ``.mp3`` because ~95% of
    podcast enclosures are MP3.
    """
    lowered = url.lower().split("?", 1)[0]
    for ext in (".mp3", ".m4a", ".mp4", ".aac", ".wav", ".opus", ".ogg"):
        if lowered.endswith(ext):
            return ext
    ctype = (headers.get("Content-Type") or "").lower() if headers else ""
    if "mp4" in ctype or "m4a" in ctype:
        return ".m4a"
    if "wav" in ctype:
        return ".wav"
    if "ogg" in ctype or "opus" in ctype:
        return ".ogg"
    return ".mp3"


def _call_gemini_for_audio(api_key: str, model: str, audio_path: Path) -> str:
    """Upload ``audio_path`` to the Files API and invoke the SDK.

    Isolated in its own function so tests can monkeypatch this entire
    call (skipping both the lazy SDK import and the network round-trip)
    without touching the structured-result logic in ``extract_audio``.

    Targets ``google-genai >= 1.0``: uses ``client.files.upload`` to push
    the audio, polls for ``state.name == "ACTIVE"``, then passes the
    File object as a content part alongside the prompt.

    Raises ``ImportError`` when ``google-genai`` is not installed
    (caller maps to ``missing_sdk``), ``_AudioUploadFailed`` when upload
    raises a non-missing-SDK exception, ``_AudioProcessingFailed`` when
    the file reaches FAILED state or polling times out.
    """
    import time

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    try:
        uploaded = client.files.upload(file=str(audio_path))
    except Exception as exc:  # noqa: BLE001 — SDK can raise anything
        raise _AudioUploadFailed(str(exc)) from exc

    deadline = time.monotonic() + FILE_PROCESS_TIMEOUT_SEC
    while True:
        state = getattr(getattr(uploaded, "state", None), "name", None) or str(
            getattr(uploaded, "state", "")
        )
        if state == "ACTIVE":
            break
        if state == "FAILED":
            raise _AudioProcessingFailed(
                f"Gemini Files API reported FAILED for {audio_path.name}"
            )
        if time.monotonic() > deadline:
            raise _AudioProcessingFailed(
                f"Gemini Files API processing timed out after "
                f"{FILE_PROCESS_TIMEOUT_SEC}s (last state: {state})"
            )
        time.sleep(FILE_PROCESS_POLL_SEC)
        uploaded = client.files.get(name=uploaded.name)

    response = client.models.generate_content(
        model=model,
        contents=types.Content(
            parts=[
                types.Part(
                    file_data=types.FileData(
                        file_uri=uploaded.uri,
                        mime_type=getattr(uploaded, "mime_type", None) or "audio/mpeg",
                    )
                ),
                types.Part(text=PODCAST_EXTRACT_PROMPT),
            ]
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=PODCAST_BRIEF_SCHEMA,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.2,
        ),
    )
    _record_gemini_spend(response, model, "gemini_extract")
    return getattr(response, "text", "") or ""


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
    _record_gemini_spend(response, model, "gemini_extract")
    return getattr(response, "text", "") or ""


def _record_gemini_spend(response, model: str, op: str) -> None:
    """Best-effort Layer-B spend capture from a Gemini response's
    ``usage_metadata`` (``prompt_token_count`` / ``candidates_token_count``)."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return
    from personal_mem.core.spend import record_spend

    # Thinking tokens (``thoughts_token_count``) bill as output on 2.5 models.
    output = (getattr(meta, "candidates_token_count", 0) or 0) + (
        getattr(meta, "thoughts_token_count", 0) or 0
    )
    record_spend(
        "gemini",
        model,
        op,
        getattr(meta, "prompt_token_count", 0) or 0,
        output,
        mode="mcp",
    )


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
    """True if an SDK exception message suggests a refusal.

    HTTP 5xx and 429 are *never* refusals — they're transport / capacity
    errors the orchestrator should retry. Without this guard, Google's
    503 message ("503 UNAVAILABLE. ...") false-positives the
    ``unavailable`` refusal marker, which was originally added for
    YouTube-side "video unavailable" responses.
    """
    lower = (msg or "").lower().lstrip()
    if any(
        lower.startswith(f"{code} ") or lower.startswith(f"{code}:")
        for code in ("429", "500", "502", "503", "504")
    ):
        return False
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


def _coerce_speakers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        out.append({"name": name, "role": str(item.get("role", "") or "").strip()})
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

    pod = sub.add_parser("podcast", help="Extract from a podcast audio URL (MP3 enclosure)")
    pod.add_argument("audio_url", help="Direct URL to the audio file (MP3, M4A, etc.)")
    pod.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")

    args = parser.parse_args(argv)

    if args.cmd == "youtube":
        result = extract_youtube(args.url, model=args.model)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.cmd == "podcast":
        result = extract_audio(args.audio_url, model=args.model)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
