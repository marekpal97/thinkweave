"""Import ChatGPT conversations from the standard data export.

Reads conversations.json (ChatGPT Settings → Export data) and creates
one source note per conversation thread, with LLM-generated structured
summaries via GPT-4o-mini.

Usage:
    weave import chatgpt ~/path/to/conversations.json [--limit 5] [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thinkweave.acquisition.importers.common import ImportManifest, index_imported_notes
from thinkweave.acquisition.sources import build_source_frontmatter
from thinkweave.core.config import Config, load_config
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager

_MANIFEST_NAME = "chatgpt_import.json"

# Cap transcript length sent to the LLM to avoid outlier costs.
# ~100k chars ≈ 25k tokens per conversation with gpt-5-mini.
MAX_TRANSCRIPT_CHARS = 100_000

SUMMARIZE_PROMPT = """\
Summarize this conversation into a structured note. Return ONLY the following sections, nothing else:

## Summary
1-2 sentence overview of what was discussed and concluded.

## Key Questions
Bulleted list of the main questions/problems the user brought up.

## Key Insights
Bulleted list of the most valuable answers, conclusions, or discoveries.

## Concepts
Comma-separated list of specific technical terms, tools, frameworks, or domain concepts discussed (lowercase, hyphenated). Only include substantive domain concepts, not generic words.
"""


# ── Data model ─────────────────────────────────────────────────────


@dataclass
class Message:
    role: str  # user, assistant
    text: str
    timestamp: float | None = None


@dataclass
class Thread:
    id: str  # conversation_id
    title: str
    created: datetime
    updated: datetime
    model: str
    messages: list[Message] = field(default_factory=list)

    @property
    def user_messages(self) -> list[Message]:
        return [m for m in self.messages if m.role == "user"]

    @property
    def assistant_messages(self) -> list[Message]:
        return [m for m in self.messages if m.role == "assistant"]

    def transcript(self, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
        """Build a plain-text transcript for LLM consumption."""
        parts: list[str] = []
        total = 0
        for msg in self.messages:
            label = "User" if msg.role == "user" else "Assistant"
            line = f"{label}: {msg.text}\n"
            if total + len(line) > max_chars:
                remaining = max_chars - total
                if remaining > 100:
                    parts.append(line[:remaining] + "\n[truncated]")
                break
            parts.append(line)
            total += len(line)
        return "\n".join(parts)


# ── Parser ─────────────────────────────────────────────────────────


def _extract_text(parts: list) -> str:
    """Join string parts from a message's content.parts, ignoring dicts (images, tool results)."""
    return "".join(p for p in parts if isinstance(p, str)).strip()


def parse_thread(raw: dict) -> Thread:
    """Parse a single conversation object from conversations.json into a Thread."""
    mapping = raw.get("mapping", {})
    created_ts = raw.get("create_time") or 0
    updated_ts = raw.get("update_time") or created_ts

    thread = Thread(
        id=raw.get("conversation_id") or raw.get("id", ""),
        title=raw.get("title") or "Untitled",
        created=datetime.fromtimestamp(created_ts, tz=timezone.utc),
        updated=datetime.fromtimestamp(updated_ts, tz=timezone.utc),
        model=raw.get("default_model_slug") or "unknown",
    )

    # Walk the linked list from root
    current_id = "client-created-root"
    if current_id not in mapping:
        # Some exports may not have this sentinel — find the root
        for node_id, node in mapping.items():
            if node.get("parent") is None:
                current_id = node_id
                break

    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        node = mapping.get(current_id)
        if not node:
            break

        msg = node.get("message")
        if msg:
            role = msg.get("author", {}).get("role", "")
            content = msg.get("content", {})
            content_type = content.get("content_type", "")
            parts = content.get("parts", [])

            # Only keep user/assistant text messages with actual content
            if role in ("user", "assistant") and content_type == "text":
                text = _extract_text(parts)
                if text:
                    thread.messages.append(
                        Message(
                            role=role,
                            text=text,
                            timestamp=msg.get("create_time"),
                        )
                    )

        children = node.get("children", [])
        current_id = children[0] if children else None

    return thread


def parse_conversations(path: Path) -> list[Thread]:
    """Load and parse all conversations from a ChatGPT export file."""
    with open(path, encoding="utf-8") as f:
        raw_convos = json.load(f)

    threads = []
    for raw in raw_convos:
        thread = parse_thread(raw)
        # Skip empty conversations (no user/assistant messages)
        if thread.messages:
            threads.append(thread)

    # Sort by creation date (oldest first)
    threads.sort(key=lambda t: t.created)
    return threads


# ── Summarizer ─────────────────────────────────────────────────────


def summarize_thread(thread: Thread, api_key: str, model: str = "gpt-5-mini") -> dict:
    """Produce a structured ChatGPT-thread summary via the agent_client wrapper.

    Switched from direct httpx → ``agent_client.get_completion_sync``
    on 2026-06-06 (plan B2). Provider + model resolve from
    ``vault/config/api.yaml::overrides.chatgpt_import``; the legacy
    ``api_key`` arg and ``model`` arg are accepted for back-compat:
    when ``model`` differs from the api.yaml-resolved default it takes
    precedence; ``api_key`` is ignored (the wrapper handles its own
    key lookup).

    Returns dict with keys: summary, key_questions, key_insights, concepts.
    """
    del api_key  # back-compat only — the wrapper resolves its own key

    from thinkweave.core.agent_client import get_completion_sync
    from thinkweave.core.api_config import load_api_config, resolve_for_op
    from thinkweave.core.config import load_config

    cfg = load_config()
    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "chatgpt_import")
    effective_model = model or op_cfg["model"]

    transcript = thread.transcript()
    content, _usage = get_completion_sync(
        transcript,
        provider=op_cfg["provider"],
        model=effective_model,
        max_tokens=1000,
        system=SUMMARIZE_PROMPT,
    )
    return _parse_summary_response(content or "")


def _parse_summary_response(content: str) -> dict:
    """Parse the LLM response into structured fields."""
    result = {
        "summary": "",
        "key_questions": "",
        "key_insights": "",
        "concepts": [],
    }

    sections = re.split(r"^## ", content, flags=re.MULTILINE)
    for section in sections:
        if not section.strip():
            continue
        lines = section.strip().split("\n", 1)
        heading = lines[0].strip().lower()
        body = lines[1].strip() if len(lines) > 1 else ""

        if heading == "summary":
            result["summary"] = body
        elif heading == "key questions":
            result["key_questions"] = body
        elif heading == "key insights":
            result["key_insights"] = body
        elif heading == "concepts":
            # Parse comma-separated concepts
            raw = body.strip().strip(".")
            result["concepts"] = [
                c.strip().lower().replace(" ", "-")
                for c in raw.split(",")
                if c.strip()
            ]

    return result


# ── Note builder ───────────────────────────────────────────────────


def _build_body(thread: Thread, summary: dict) -> str:
    """Build the markdown body for a source note."""
    user_count = len(thread.user_messages)
    asst_count = len(thread.assistant_messages)
    date_from = thread.created.strftime("%Y-%m-%d")
    date_to = thread.updated.strftime("%Y-%m-%d")
    period = date_from if date_from == date_to else f"{date_from} → {date_to}"

    parts: list[str] = []

    parts.append("## Metadata")
    parts.append(f"- **Messages**: {len(thread.messages)} ({user_count} user, {asst_count} assistant)")
    parts.append(f"- **Period**: {period}")
    parts.append(f"- **Model**: {thread.model}")
    parts.append("")

    if summary.get("summary"):
        parts.append("## Summary")
        parts.append(summary["summary"])
        parts.append("")

    if summary.get("key_questions"):
        parts.append("## Key Questions")
        parts.append(summary["key_questions"])
        parts.append("")

    if summary.get("key_insights"):
        parts.append("## Key Insights")
        parts.append(summary["key_insights"])
        parts.append("")

    return "\n".join(parts).rstrip()


# ── Main import ────────────────────────────────────────────────────


def import_chatgpt(
    config: Config | None = None,
    conversations_path: Path | None = None,
    dry_run: bool = False,
    limit: int = 0,
    since: str = "",
    until: str = "",
) -> dict:
    """Import ChatGPT conversations into the vault as source notes.

    Args:
        config: Vault config.
        conversations_path: Path to conversations.json.
        dry_run: If True, parse and report without writing or calling the API.
        limit: Max conversations to import (0 = all).
        since: Only import conversations created on or after this date (YYYY-MM-DD).
        until: Only import conversations created on or before this date (YYYY-MM-DD).

    Returns:
        Stats dict: {total, imported, skipped, errors}.
    """
    config = config or load_config()

    if not conversations_path or not conversations_path.exists():
        return {"error": f"File not found: {conversations_path}"}

    # Load API key (not needed for dry run)
    api_key = ""
    if not dry_run:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set. Add it to .env or export it."}

    # Parse all conversations
    print(f"Parsing {conversations_path}...")
    threads = parse_conversations(conversations_path)
    print(f"Parsed {len(threads)} conversations with messages.")

    # Apply date filters
    if since:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        threads = [t for t in threads if t.created >= since_dt]
    if until:
        until_dt = datetime.strptime(until, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        threads = [t for t in threads if t.created <= until_dt]

    if limit:
        threads = threads[:limit]

    print(f"Processing {len(threads)} conversations after filters.")

    if dry_run:
        return _dry_run_report(threads)

    # Set up vault
    vm = VaultManager(config=config)
    vm.ensure_dirs()

    manifest = ImportManifest.load(config.weave_dir, _MANIFEST_NAME)

    stats = {"total": len(threads), "imported": 0, "skipped": 0, "errors": 0}
    written_paths: list[Path] = []

    for i, thread in enumerate(threads, 1):
        # Idempotency check
        if manifest.is_imported(thread.id):
            stats["skipped"] += 1
            continue

        print(f"  [{i}/{len(threads)}] {thread.title[:60]}...", end=" ", flush=True)

        try:
            # Summarize via LLM
            summary = summarize_thread(thread, api_key)

            # Create source note. build_source_frontmatter stamps the canonical
            # source_type/title/url/authors keys; the chatgpt-specific fields
            # ride along as extras.
            body = _build_body(thread, summary)
            extra_fm = build_source_frontmatter(
                source_type="conversation",
                title=thread.title,
                url="",
                imported_from="chatgpt",
                source_id=thread.id,
                date=thread.created.isoformat(),
            )
            # The summarize prompt asks the LLM to list concepts without
            # showing it the ontology, so everything it returns is unvetted.
            # Route to proposed_concepts: so /weave-resolve-concepts can review
            # and promote canonical ones, rather than polluting concepts:
            # directly. (See plan B4a — the sprawl faucet fix.)
            if summary.get("concepts"):
                extra_fm["proposed_concepts"] = summary["concepts"]

            path = vm.create_note(
                note_type=NoteType.SOURCE,
                title=thread.title,
                body=body,
                extra_frontmatter=extra_fm,
            )
            written_paths.append(path)

            manifest.mark(thread.id, vm.read_note(path).id)
            stats["imported"] += 1
            print("OK")

        except Exception as e:
            stats["errors"] += 1
            print(f"ERROR: {e}")

        # Save manifest periodically (every 10 conversations)
        if i % 10 == 0:
            manifest.save()

    # Final manifest save
    manifest.set_meta(
        completed_at=datetime.now(timezone.utc).isoformat(),
        source_file=str(conversations_path),
    )
    manifest.save()

    # Index everything written this run in one pass (shared policy).
    idx_stats = index_imported_notes(config, written_paths)
    stats["indexed"] = idx_stats.get("indexed", 0)

    return stats


def _dry_run_report(threads: list[Thread]) -> dict:
    """Print a summary of what would be imported."""
    print("\n── Dry Run Report ──────────────────────────────────\n")
    total_messages = sum(len(t.messages) for t in threads)
    total_user = sum(len(t.user_messages) for t in threads)
    total_chars = sum(sum(len(m.text) for m in t.messages) for t in threads)

    print(f"  Conversations: {len(threads)}")
    print(f"  Total messages: {total_messages} ({total_user} user)")
    print(f"  Total text: {total_chars:,} chars (~{total_chars // 4:,} tokens)")
    print(f"  Tokens: ~{total_chars // 4:,} in · ~{len(threads) * 500:,} out (model: gpt-5-mini — multiply by OpenAI $/token for cost)")

    if threads:
        oldest = threads[0]
        newest = threads[-1]
        print(f"  Date range: {oldest.created.strftime('%Y-%m-%d')} → {newest.created.strftime('%Y-%m-%d')}")

    # Show sample titles
    print("\n  Sample conversations:")
    for t in threads[:10]:
        msg_count = len(t.messages)
        print(f"    {t.created.strftime('%Y-%m-%d')}  ({msg_count:3d} msgs)  {t.title[:70]}")
    if len(threads) > 10:
        print(f"    ... and {len(threads) - 10} more")

    return {"total": len(threads), "imported": 0, "skipped": 0, "errors": 0, "dry_run": True}
