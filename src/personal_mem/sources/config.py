"""User-overridable source-config layer.

``vault/.mem/sources.yaml`` lets a user adjust the per-source-type behaviour
of personal_mem (queue paths, importer/skill bindings, dedup keys, drain
strategy, intake folders, …) without forking the codebase. The framework
ships an in-code ``DEFAULT_CONFIG`` that mirrors the registry; the user
file overlays it key-by-key.

This module deliberately uses a tiny stdlib-only YAML reader that handles
exactly the constrained shape documented in
``vault_templates/.mem/sources.yaml`` — top-level scalars, top-level
mappings, nested mappings, scalar values, and inline ``[a, b, c]`` lists.
It is **not** a general YAML parser; if the loader can't parse a line it
raises ``ValueError`` so the user finds out at ``mem doctor``/``mem init``
time rather than getting a silent fallback.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

# In-code defaults — the source of truth when no override is present.
DEFAULT_CONFIG: dict[str, Any] = {
    "sources": {
        "paper": {
            "queue": "vault/.mem/queues/papers.jsonl",
            "research_skill": "research-paper",
            "drain_strategy": "anthropic_batch",
            "intake_folder": "~/papers_inbox",
            "summarize_format": "technical_brief",
            "dedup_keys": ["arxiv_id", "doi", "title"],
            "url_patterns": ["arxiv.org", "openreview.net"],
        },
        "repo": {
            "queue": "vault/.mem/queues/repos.jsonl",
            "research_skill": "research-repo",
            "drain_strategy": "inline",
            "dedup_keys": ["github_url", "slug"],
            "url_patterns": ["github.com", "gitlab.com"],
        },
        "article": {
            "queue": "vault/.mem/queues/articles.jsonl",
            "research_skill": "research-article",
            "drain_strategy": "inline",
            "dedup_keys": ["url", "title"],
        },
        "substack": {
            "drain_strategy": "inline",
            "intake_folder": "~/substack_inbox",
            "dedup_keys": ["url", "slug"],
        },
        "news": {
            "queue": "vault/.mem/queues/news.jsonl",
            "feed_config": "vault/.mem/news_feeds.yaml",
            # v2 admission: Haiku title-triage against the active-themes
            # catalog rendered in vault/THEMES.md (## Catalog (active)).
            # The legacy focus_manifest field is intentionally absent —
            # FOCUS.md is a deprecated stub.
            "triage_model": "claude-haiku-4-5",
            "themes_catalog": "vault/THEMES.md",
            "dedup_keys": ["url", "entry_id"],
            "drain_strategy": "subagent",
            "drain_parallelism": 4,
            "drain_batch_max": 20,
            "subagent_type": "research-news-worker",
            "subagent_model": "sonnet",
            # post_batch_hooks stays empty for the standard config: theme
            # naming happens in /dream (signal-based, agent-composed) not
            # in drain (which can only mint mechanical concept-pair slugs
            # via the legacy theme_scan hook).
            "post_batch_hooks": [],
            # /research router dispatches news URLs to /news (the one-off
            # ingest skill). The cron-driven drain uses the subagent path
            # (subagent_type above) — research_skill is only consulted by
            # Path A (sequential Skill dispatch) which news doesn't take.
            "research_skill": "news",
            "url_patterns": [
                "reuters.com",
                "ft.com",
                "bloomberg.com",
                "wsj.com",
                "bankier.pl",
            ],
            "allowed_failure_prefixes": [
                "HTTP ",
                "paywall",
                "Cloudflare",
                "empty body",
                "timeout",
                "mem_create:",
            ],
        },
        "newsletter-events": {
            # Event-grain email newsletters. Theme-candidate floater fires
            # on create via VaultManager. Mail connector is swappable;
            # gmail is the only one implemented today. See commands/newsletter.md.
            "queue": "vault/.mem/queues/newsletter-events.jsonl",
            "drain_strategy": "subagent",
            "subagent_type": "research-newsletter-worker",
            "subagent_model": "sonnet",
            "drain_parallelism": 4,
            "drain_batch_max": 20,
            "mail_connector": "gmail",
            # `senders` is the canonical allowlist. Each entry is an email
            # address (alice@example.com) or a bare domain (example.com);
            # the skill composes `from:(...)` for the connector's query.
            # Empty allowlist + empty mail_query → deliberate halt in the
            # skill (guards against an accidental whole-inbox fan-out).
            "senders": [],
            # Optional extra filter (e.g. "is:unread", "has:attachment").
            # The skill ANDs this with the `from:` clause built from senders.
            "mail_query": "",
            "processed_label": "mem-processed",
            "lookback_days": 30,
            "dedup_keys": ["message_id", "url"],
            # post_batch_hooks empty — /newsletter applies processed_label
            # itself after /drain returns; no /drain hook needed.
            "post_batch_hooks": [],
        },
        "newsletter-concepts": {
            # Concept-grain sibling — technical / methodology / philosophy
            # newsletters. No theme floating; concepts populate hubs.
            "queue": "vault/.mem/queues/newsletter-concepts.jsonl",
            "drain_strategy": "subagent",
            "subagent_type": "research-newsletter-worker",
            "subagent_model": "sonnet",
            "drain_parallelism": 4,
            "drain_batch_max": 20,
            "mail_connector": "gmail",
            "senders": [],
            "mail_query": "",
            "processed_label": "mem-processed",
            "lookback_days": 90,
            "dedup_keys": ["message_id", "url"],
            "post_batch_hooks": [],
        },
        "youtube-events": {
            # Event-grain YouTube channels (tech-news, market recaps).
            # `channels` is the canonical subscription allowlist; the skill
            # polls each channel's RSS feed at
            # https://www.youtube.com/feeds/videos.xml?channel_id=<id>.
            # Empty list + no URL paste = nothing to do.
            # Gemini Flash handles transcript + summary from the video URL
            # directly; no audio download or transcription needed.
            "queue": "vault/.mem/queues/youtube-events.jsonl",
            "drain_strategy": "subagent",
            "subagent_type": "research-youtube-worker",
            "subagent_model": "sonnet",
            "drain_parallelism": 4,
            "drain_batch_max": 20,
            "channels": [],
            "lookback_days": 7,
            "dedup_keys": ["video_id", "url"],
            "url_patterns": ["youtube.com/watch", "youtu.be/", "youtube.com/shorts"],
            "research_skill": "youtube",
            "post_batch_hooks": [],
            # No `triage_model` — admission is the channel allowlist.
            # /drain Path B treats every queue item as keep_unfiled.
            "allowed_failure_prefixes": [
                "transcripts_disabled",
                "no_transcripts",
                "video_unavailable",
                "empty_transcript",
                "transcript_api_failed",
                "mem_create:",
            ],
        },
        "youtube-concepts": {
            # Concept-grain sibling — tutorials, lectures, explainers.
            # Longer lookback than events since durable content is worth
            # backfilling further.
            "queue": "vault/.mem/queues/youtube-concepts.jsonl",
            "drain_strategy": "subagent",
            "subagent_type": "research-youtube-worker",
            "subagent_model": "sonnet",
            "drain_parallelism": 4,
            "drain_batch_max": 20,
            "channels": [],
            "lookback_days": 30,
            "dedup_keys": ["video_id", "url"],
            "url_patterns": ["youtube.com/watch", "youtu.be/", "youtube.com/shorts"],
            "research_skill": "youtube",
            "post_batch_hooks": [],
            "allowed_failure_prefixes": [
                "transcripts_disabled",
                "no_transcripts",
                "video_unavailable",
                "empty_transcript",
                "transcript_api_failed",
                "mem_create:",
            ],
        },
        "podcast-events": {
            # Event-grain podcasts (markets, macro, interview shows).
            # Each show is one outlet entry in podcast_events_feeds.yaml;
            # the rss_poll strategy pulls the <enclosure> audio URL per
            # episode, and the worker hands the MP3 to Gemini Flash via
            # the Files API. Theme candidate floater fires on create.
            # See commands/podcast.md and
            # .claude/agents/research-podcast-worker.md.
            "queue": "vault/.mem/queues/podcast-events.jsonl",
            "feed_config": "vault/.mem/podcast_events_feeds.yaml",
            "drain_strategy": "subagent",
            "subagent_type": "research-podcast-worker",
            "subagent_model": "sonnet",
            # Parallelism 2 (not 4 like news/newsletter): each worker
            # downloads a 20-100MB MP3 and uploads to Gemini before
            # generation — bandwidth-bound, higher parallelism rarely
            # improves wall-clock. Override per-vault in sources.yaml.
            "drain_parallelism": 2,
            "drain_batch_max": 10,
            "lookback_days": 7,
            "dedup_keys": ["entry_id", "audio_url", "url"],
            "url_patterns": [
                "feeds.megaphone.fm",
                "feeds.libsyn.com",
                "feeds.transistor.fm",
                "feeds.simplecast.com",
                "anchor.fm",
                "omny.fm",
                "rss.art19.com",
                "spreaker.com",
            ],
            "research_skill": "podcast",
            "post_batch_hooks": [],
            # No `triage_model` — admission is the per-show subscription
            # decision (an outlet in podcast_events_feeds.yaml), not
            # per-episode triage. /drain Path B fans out directly.
            "allowed_failure_prefixes": [
                "audio_fetch_failed",
                "audio_too_large",
                "audio_upload_failed",
                "audio_processing_failed",
                "gemini_refused",
                "api_error",
                "invalid_response",
                "mem_create:",
            ],
        },
        "podcast-concepts": {
            # Concept-grain sibling — deep-dives, lecture-style shows,
            # technical explainer pods. Same Gemini audio extraction;
            # no theme floating. Longer lookback than events because
            # durable content is worth backfilling further.
            "queue": "vault/.mem/queues/podcast-concepts.jsonl",
            "feed_config": "vault/.mem/podcast_concepts_feeds.yaml",
            "drain_strategy": "subagent",
            "subagent_type": "research-podcast-worker",
            "subagent_model": "sonnet",
            "drain_parallelism": 2,
            "drain_batch_max": 10,
            "lookback_days": 30,
            "dedup_keys": ["entry_id", "audio_url", "url"],
            "url_patterns": [
                "feeds.megaphone.fm",
                "feeds.libsyn.com",
                "feeds.transistor.fm",
                "feeds.simplecast.com",
                "anchor.fm",
                "omny.fm",
                "rss.art19.com",
                "spreaker.com",
            ],
            "research_skill": "podcast",
            "post_batch_hooks": [],
            "allowed_failure_prefixes": [
                "audio_fetch_failed",
                "audio_too_large",
                "audio_upload_failed",
                "audio_processing_failed",
                "gemini_refused",
                "api_error",
                "invalid_response",
                "mem_create:",
            ],
        },
        "conversation": {
            "drain_strategy": "inline",
            "importer": "chatgpt",
            "dedup_keys": ["conversation_id", "title"],
        },
        "claude-history": {
            "drain_strategy": "inline",
            "importer": "claude_mem",
            "dedup_keys": ["session_uuid"],
        },
    },
    "projects": {
        "default": {
            "discover_strategies": ["concept_coverage"],
        },
    },
    "landing_files": {
        "state": "STATE.md",
        "backlog": "BACKLOG.md",
        "decisions": "DECISIONS.md",
        "themes": "THEMES.md",
        "research_focus": "RESEARCH_FOCUS.md",
    },
    "auto_todo_extraction": True,
}


def load_user_config(vault_root: Path | None) -> dict[str, Any]:
    """Return the merged source config: defaults overlaid with user file.

    Reads ``<vault_root>/.mem/sources.yaml`` if present and merges it on
    top of ``DEFAULT_CONFIG``. Missing or empty file → defaults.
    """
    merged: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    if vault_root is None:
        return merged
    user_path = Path(vault_root) / ".mem" / "sources.yaml"
    if not user_path.exists():
        return merged
    try:
        user_doc = _parse_simple_yaml(user_path.read_text(encoding="utf-8"))
    except ValueError:
        # Malformed user file: fall back silently to defaults. (We could
        # raise here, but the loader is read by tools that should be
        # robust to a half-edited config; surfacing the error lives in
        # `mem doctor`.)
        return merged
    if user_doc:
        _deep_merge(merged, user_doc)
    return merged


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Merge ``overlay`` into ``base`` in place. Dicts merge recursively;
    everything else (lists, scalars) is overwritten wholesale."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# Tiny YAML reader
#
# The parser handles exactly:
#   - Comments (#) and blank lines.
#   - ``key: value``                     scalar at any indent level
#   - ``key:`` followed by indented map  nested mapping
#   - ``key: [a, b, "c"]``               inline list of scalars
#   - Booleans: ``true``/``false`` (case-insensitive)
#   - Integers and floats
#   - Quoted strings (single or double)
#   - Unquoted strings (everything else)
#
# Indent depth is whatever the user used; the parser tracks the column of
# each line and groups children by indent > parent. Block-style ``- item``
# list syntax is NOT supported — the configured shape doesn't need it.
# ---------------------------------------------------------------------------


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    rows: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped_right = raw.rstrip()
        if not stripped_right.strip() or stripped_right.lstrip().startswith("#"):
            continue
        comment_idx = _find_inline_comment(stripped_right)
        if comment_idx >= 0:
            stripped_right = stripped_right[:comment_idx].rstrip()
            if not stripped_right:
                continue
        indent = len(stripped_right) - len(stripped_right.lstrip(" "))
        rows.append((lineno, indent, stripped_right.strip()))

    root: dict[str, Any] = {}
    _parse_block(rows, 0, -1, root)
    return root


def _find_inline_comment(line: str) -> int:
    """Index of an inline ``#`` comment, or -1 if none.

    Treats ``#`` as a comment only when preceded by whitespace, so values
    that happen to contain ``#`` aren't truncated.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in (" ", "\t"):
                return i
    return -1


def _parse_block(
    rows: list[tuple[int, int, str]],
    cursor: int,
    parent_indent: int,
    target: dict[str, Any],
) -> int:
    """Consume rows whose indent > ``parent_indent`` into ``target``. The
    first qualifying row's indent fixes the indent for the entire block.
    Returns the cursor position one past the last consumed row."""
    block_indent: int | None = None
    while cursor < len(rows):
        lineno, indent, content = rows[cursor]
        if indent <= parent_indent:
            return cursor
        if block_indent is None:
            block_indent = indent
        if indent > block_indent:
            raise ValueError(
                f"Unexpected indent at line {lineno}: {content!r} "
                f"(block_indent={block_indent}, indent={indent})"
            )
        if indent < block_indent:
            return cursor
        if ":" not in content:
            raise ValueError(f"Expected 'key: value' at line {lineno}: {content!r}")
        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        cursor += 1
        if rest:
            target[key] = _parse_scalar_or_list(rest, lineno)
        else:
            child: dict[str, Any] = {}
            cursor = _parse_block(rows, cursor, block_indent, child)
            target[key] = child
    return cursor


def _parse_scalar_or_list(value: str, lineno: int) -> Any:
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        return [_parse_scalar(item.strip(), lineno) for item in _split_inline_list(body)]
    return _parse_scalar(value, lineno)


def _split_inline_list(body: str) -> list[str]:
    """Split a comma-separated inline list, respecting quotes."""
    out: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    for ch in body:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == "," and not in_single and not in_double:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _parse_scalar(token: str, lineno: int) -> Any:
    if not token:
        return ""
    if (token.startswith("'") and token.endswith("'")) or (
        token.startswith('"') and token.endswith('"')
    ):
        return token[1:-1]
    lower = token.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        pass
    return token
