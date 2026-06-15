"""Shared session-synthesis spec — one prompt, two backends.

A Claude Code session is imported as a verbatim transcript dump
(:func:`thinkweave.onboarding.claude_code_seed._build_session_body`). This
module turns that transcript into the *same* artifact shape a live
``/weave-wrap`` produces: a session summary plus derived insight and
decision notes, with concepts proposed at synthesis time.

The split here is deliberate — this module owns only the **spec** (the
prompt + the parse + the map into :func:`thinkweave.operations.extract.
extract_session`'s input shape + the transcript→companion archival). The
two *backends* live elsewhere and both call into here:

- **batch** — :func:`thinkweave.onboarding.enrich_batch.run_enrichment_batch`
  fans the prompt out through ``agent_client.batch_completions_sync``
  (provider resolved from ``api.yaml``, never hardcoded).
- **inline** — the ``/synthesize-sessions`` skill runs the same spec on the
  running Claude Code model (keyless).

Because both backends converge on ``extract_session`` for the writeback,
an imported-then-synthesised session is byte-for-byte the same shape as a
session that was wrapped live — ontology-gated concepts, commit-evidence
decision flips, predicted-outcome seeding, the lot.
"""

from __future__ import annotations

import json
from pathlib import Path

from thinkweave.core.vault import parse_frontmatter, render_frontmatter

# The synthesis spec. Emits exactly the shape ``extract_session`` consumes:
# a session ``summary``, session-level ``concepts``, and lists of
# ``insights`` / ``decisions``. Concepts are proposed freely here; the
# ontology gate in ``extract_session`` (``split_concepts_by_ontology``)
# routes non-canonical terms to ``proposed_concepts:`` — so the prompt does
# NOT carry the ontology (keeps it cheap across a batch of hundreds).
SYNTHESIS_SYSTEM = """You synthesise a Claude Code session transcript into durable memory.

Return ONLY valid JSON with this exact shape:
{
  "summary": "<2-4 sentence plain-prose summary of what this session was about and what came of it>",
  "concepts": ["<specific term>", ...],
  "insights": [
    {"title": "<short topic phrase, <80 chars>", "body": "<1-3 sentence insight>", "concepts": ["<term>", ...]}
  ],
  "decisions": [
    {"title": "<imperative, <80 chars>", "rationale": "<2-4 sentence rationale>", "outcome": "committed|abandoned|partial", "file_paths": ["<path>", ...], "concepts": ["<term>", ...]}
  ]
}

Rules:
- summary: always present. Plain prose, no markdown headers. This becomes the session note's body.
- decisions: explicit choices the user made or the assistant proposed and the user accepted. Skip exploratory chatter. ``outcome`` reflects what actually happened in the transcript; leave ``file_paths`` empty if none are evident.
- insights: surprises, gotchas, recurring patterns, trade-offs the session surfaced. Empty list if none stand out.
- concepts (every level): 2-6 specific terms (e.g. "fts5", "anthropic-batches"). Prefer terms likely to recur across sessions. Do NOT invent meta-terms like "session" or "conversation".
- If the transcript is too thin to extract anything, still return a one-sentence ``summary`` with empty ``insights``/``decisions``/``concepts`` lists.
- Output JSON only, no prose, no code fences."""


def build_user_prompt(*, project: str, title: str, transcript: str) -> str:
    """Render one session's user-prompt body for the synthesis spec."""
    return (
        f"Project: {project}\nSession title: {title}\n\n"
        f"--- transcript ---\n{transcript}"
    )


def parse_synthesis(raw: str) -> dict | None:
    """Parse a model response into a synthesis dict, or None on failure.

    Tolerates the markdown code-fence wrapping some providers add even when
    asked for raw JSON. Returns None (rather than raising) so a single bad
    response degrades to a skip instead of killing the batch.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        # ```json\n…\n``` → strip the fence lines.
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def to_extract_inputs(parsed: dict) -> dict:
    """Map a parsed synthesis dict into ``extract_session`` keyword inputs.

    Returns ``{summary, concepts, insights, decisions}`` where ``insights``
    and ``decisions`` are already in the list-of-dict shape
    ``extract_session`` expects. Defensive against missing/mistyped keys —
    a malformed sub-entry is dropped, not propagated.
    """
    summary = parsed.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    session_concepts = [c for c in (parsed.get("concepts") or []) if isinstance(c, str) and c.strip()]

    insights: list[dict] = []
    for ins in parsed.get("insights") or []:
        if not isinstance(ins, dict):
            continue
        title = (ins.get("title") or "").strip()
        body = (ins.get("body") or "").strip()
        if not title and not body:
            continue
        insights.append(
            {
                "title": title or body[:80],
                "body": body or title,
                "concepts": [c for c in (ins.get("concepts") or []) if isinstance(c, str)],
            }
        )

    decisions: list[dict] = []
    for dec in parsed.get("decisions") or []:
        if not isinstance(dec, dict):
            continue
        title = (dec.get("title") or "").strip()
        if not title:
            continue
        decisions.append(
            {
                "title": title,
                "rationale": (dec.get("rationale") or "").strip(),
                "outcome": dec.get("outcome") or "committed",
                "file_paths": [f for f in (dec.get("file_paths") or []) if isinstance(f, str)],
                "concepts": [c for c in (dec.get("concepts") or []) if isinstance(c, str)],
            }
        )

    return {
        "summary": summary,
        "concepts": session_concepts,
        "insights": insights,
        "decisions": decisions,
    }


# Body sections written by the importer's verbatim dump. Archival keys on
# the ``## Transcript`` marker; ``## Source`` is the provenance preamble.
_TRANSCRIPT_MARKERS = ("## Transcript", "## Source")
TRANSCRIPT_COMPANION = "transcript.md"


def has_archivable_transcript(session_path: Path) -> bool:
    """True if this session note still carries the verbatim transcript dump.

    Used to keep archival idempotent — a session synthesised once (transcript
    already a companion) won't be re-archived on a re-run.
    """
    try:
        _, body = parse_frontmatter(session_path.read_text(encoding="utf-8"))
    except OSError:
        return False
    return "## Transcript" in body


def archive_transcript(session_path: Path) -> bool:
    """Move the verbatim transcript out of the session body into a companion.

    Generation-is-synthesis: after this runs the session note's body is empty
    (``extract_session`` then writes the ``## Summary``), and the raw
    transcript lives at ``<session-dir>/transcript.md`` for provenance. Also
    retires the legacy ``enrichment_status`` / ``enriched_at`` frontmatter —
    the canonical "synthesised" marker is ``processed: true`` (stamped by
    ``extract_session``), identical to a live-wrapped session.

    Idempotent: a no-op (returns False) when there's no ``## Transcript`` left
    to move. Returns True when it archived.
    """
    text = session_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if "## Transcript" not in body:
        return False

    companion = session_path.parent / TRANSCRIPT_COMPANION
    header = (
        f"# Transcript — {fm.get('title', session_path.parent.name)}\n\n"
        f"Raw verbatim transcript of imported session `{fm.get('claude_session_uuid', '')}`. "
        f"The synthesised summary, insights, and decisions live in "
        f"[[{fm.get('id', 'session')}|session.md]] and its derived notes.\n\n"
    )
    companion.write_text(header + body.strip() + "\n", encoding="utf-8")

    # Retire the deferred-enrichment frontmatter; `processed` is the marker now.
    fm.pop("enrichment_status", None)
    fm.pop("enriched_at", None)
    fm["transcript_file"] = TRANSCRIPT_COMPANION

    # Body becomes empty — extract_session appends `## Summary` next.
    session_path.write_text(render_frontmatter(fm) + "\n", encoding="utf-8")
    return True
