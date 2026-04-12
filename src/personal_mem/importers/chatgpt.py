"""Import ChatGPT conversations from the standard data export.

Reads conversations.json (ChatGPT Settings → Export data) and creates
one source note per conversation thread, with LLM-generated structured
summaries via GPT-4o-mini.

Usage:
    mem import chatgpt ~/path/to/conversations.json [--limit 5] [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.indexer import Indexer
from personal_mem.schemas import NoteType
from personal_mem.vault import VaultManager

_MANIFEST_NAME = "chatgpt_import.json"

# Cap transcript length sent to the LLM to avoid outlier costs.
# ~100k chars ≈ 25k tokens ≈ $0.004 per conversation with gpt-4o-mini.
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


def summarize_thread(thread: Thread, api_key: str, model: str = "gpt-4o-mini") -> dict:
    """Call OpenAI API to produce a structured summary.

    Returns dict with keys: summary, key_questions, key_insights, concepts.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError(
            "ChatGPT import requires httpx. Install with: pip install personal-mem[embeddings]"
        )

    transcript = thread.transcript()

    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.3,
            "max_tokens": 1000,
        },
        timeout=60.0,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]

    return _parse_summary_response(content)


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


# ── Manifest I/O ───────────────────────────────────────────────────


def _load_manifest(mem_dir: Path) -> dict:
    path = mem_dir / _MANIFEST_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "imported_ids": {}}


def _save_manifest(mem_dir: Path, manifest: dict) -> None:
    path = mem_dir / _MANIFEST_NAME
    mem_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


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

    manifest = _load_manifest(config.mem_dir)
    imported_ids = manifest.get("imported_ids", {})

    stats = {"total": len(threads), "imported": 0, "skipped": 0, "errors": 0}

    for i, thread in enumerate(threads, 1):
        # Idempotency check
        if thread.id in imported_ids:
            stats["skipped"] += 1
            continue

        print(f"  [{i}/{len(threads)}] {thread.title[:60]}...", end=" ", flush=True)

        try:
            # Summarize via LLM
            summary = summarize_thread(thread, api_key)

            # Create source note
            body = _build_body(thread, summary)
            extra_fm = {
                "source_type": "conversation",
                "imported_from": "chatgpt",
                "source_id": thread.id,
                "date": thread.created.isoformat(),
            }
            if summary.get("concepts"):
                extra_fm["concepts"] = summary["concepts"]

            path = vm.create_note(
                note_type=NoteType.SOURCE,
                title=thread.title,
                body=body,
                extra_frontmatter=extra_fm,
            )

            # Index immediately
            idx = Indexer(config=config)
            idx.index_file(path)
            idx.close()

            imported_ids[thread.id] = vm.read_note(path).id
            stats["imported"] += 1
            print("OK")

        except Exception as e:
            stats["errors"] += 1
            print(f"ERROR: {e}")

        # Save manifest periodically (every 10 conversations)
        if i % 10 == 0:
            manifest["imported_ids"] = imported_ids
            _save_manifest(config.mem_dir, manifest)

    # Final manifest save
    manifest["imported_ids"] = imported_ids
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["source_file"] = str(conversations_path)
    _save_manifest(config.mem_dir, manifest)

    return stats


def _dry_run_report(threads: list[Thread]) -> dict:
    """Print a summary of what would be imported."""
    print(f"\n── Dry Run Report ──────────────────────────────────\n")
    total_messages = sum(len(t.messages) for t in threads)
    total_user = sum(len(t.user_messages) for t in threads)
    total_chars = sum(sum(len(m.text) for m in t.messages) for t in threads)

    print(f"  Conversations: {len(threads)}")
    print(f"  Total messages: {total_messages} ({total_user} user)")
    print(f"  Total text: {total_chars:,} chars (~{total_chars // 4:,} tokens)")
    print(f"  Est. cost (gpt-4o-mini): ~${total_chars / 4 * 0.00000015 + len(threads) * 500 * 0.0000006:.2f}")

    if threads:
        oldest = threads[0]
        newest = threads[-1]
        print(f"  Date range: {oldest.created.strftime('%Y-%m-%d')} → {newest.created.strftime('%Y-%m-%d')}")

    # Show sample titles
    print(f"\n  Sample conversations:")
    for t in threads[:10]:
        msg_count = len(t.messages)
        print(f"    {t.created.strftime('%Y-%m-%d')}  ({msg_count:3d} msgs)  {t.title[:70]}")
    if len(threads) > 10:
        print(f"    ... and {len(threads) - 10} more")

    return {"total": len(threads), "imported": 0, "skipped": 0, "errors": 0, "dry_run": True}
