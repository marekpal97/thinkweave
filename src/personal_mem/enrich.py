"""LLM-assisted concept enrichment for vault notes.

Sends batches of notes to the OpenAI API (gpt-5-mini) and assigns
concepts from the ontology vocabulary. Writes concepts directly to markdown
frontmatter — permanent, visible in Obsidian.

Covers all note types including sessions (0% coverage) and imported sources.

Usage:
    mem enrich [--project X] [--type session,note,decision,source]
               [--limit N] [--dry-run] [--force]
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from personal_mem.config import Config, load_config
from personal_mem.vault import VaultManager, parse_frontmatter, render_frontmatter

# OpenAI API constants
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
ENRICH_MODEL = "gpt-5-mini"

BATCH_SIZE = 25  # notes per API call
BODY_PREVIEW_CHARS = 500  # chars of body to include per note


def _build_ontology_text(ontology_path: Path) -> str:
    """Read ontology.yaml and format it as a compact vocabulary list for the prompt."""
    if not ontology_path.exists():
        return "(no ontology file found)"

    lines = ontology_path.read_text(encoding="utf-8").splitlines()
    domain = ""
    sections: list[str] = []
    concepts: list[str] = []

    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        if not line.startswith(" ") and not line.startswith("\t") and ":" in line:
            if domain and concepts:
                sections.append(f"{domain}: {', '.join(concepts)}")
            domain = line.rstrip(":").strip()
            concepts = []
        elif line.strip().startswith("- "):
            concept = line.strip()[2:].strip()
            if not concept.startswith("_"):  # skip _relationships section
                concepts.append(concept)

    if domain and concepts:
        sections.append(f"{domain}: {', '.join(concepts)}")

    return "\n".join(sections)


_SYSTEM_PROMPT_TEMPLATE = """\
You are a concept tagger for a personal knowledge vault. Assign concepts to notes.

## Available concepts (domain: concept1, concept2, ...)
{ontology}

## Rules
- Assign 2-6 concepts per note from the list above when applicable
- You MAY invent new concepts (kebab-case) if the note clearly covers a topic not listed — they will be registered
- Sessions: extract concepts from the work described (e.g. fts5, mcp, sqlite) — NOT meta-concepts like "session" or "meeting"
- Decisions: tag with what was decided about (e.g. "indexing", "sqlite", "fts5")
- Sources: tag with the subject matter of the source
- Short/empty notes: assign at least 1 concept from context clues in the title
- Return ONLY valid JSON, no commentary

## Response format
{{"results": [{{"id": "note-id", "concepts": ["concept1", "concept2"]}}]}}"""

_USER_PROMPT_TEMPLATE = """\
Tag these {n} notes with concepts:

{notes_json}

Return JSON only."""


def load_openai_api_key() -> str:
    """Get OpenAI API key from env or .env file in the project directory.

    Shared between `mem enrich`, `mem hubs run`, and any other caller that
    needs to hit the OpenAI API.
    """
    key = os.environ.get(OPENAI_API_KEY_ENV, "")
    if key:
        return key

    # Try loading from .env in the personal_mem project root
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{OPENAI_API_KEY_ENV}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()

    return ""


# Alias preserved for back-compat within this module.
_load_api_key = load_openai_api_key


def _call_openai(
    notes: list[dict],
    ontology_text: str,
    api_key: str,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Send a batch to OpenAI chat completions and return [{id, concepts}]."""
    if dry_run:
        return [{"id": n["id"], "concepts": ["[dry-run]"]} for n in notes]

    try:
        import httpx
    except ImportError:
        raise ImportError("mem enrich requires httpx: pip install personal-mem[embeddings]")

    system = _SYSTEM_PROMPT_TEMPLATE.format(ontology=ontology_text)
    notes_json = json.dumps(
        [{"id": n["id"], "title": n["title"], "body": n["body"]} for n in notes],
        ensure_ascii=False,
        indent=None,
    )
    user = _USER_PROMPT_TEMPLATE.format(n=len(notes), notes_json=notes_json)

    response = httpx.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": ENRICH_MODEL,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=60.0,
    )
    response.raise_for_status()

    raw = response.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    parsed = json.loads(raw)
    return parsed.get("results", [])


def _write_concepts_to_note(vault: VaultManager, rel_path: str, concepts: list[str]) -> bool:
    """Update the concepts frontmatter field of a markdown file.

    Returns True if the file was modified.
    """
    file_path = vault.root / rel_path
    if not file_path.exists():
        return False

    text = file_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    # Merge with existing concepts (deduplicate, preserve existing)
    existing = fm.get("concepts", [])
    if isinstance(existing, str):
        existing = [c.strip() for c in existing.split(",") if c.strip()]
    merged = list(dict.fromkeys(existing + [c.lower().strip() for c in concepts if c]))

    if set(merged) == set(existing):
        return False  # Nothing new

    fm["concepts"] = merged
    new_text = render_frontmatter(fm) + "\n" + body
    file_path.write_text(new_text, encoding="utf-8")
    return True


def enrich(
    cfg: Config | None = None,
    *,
    project: str = "",
    note_types: list[str] | None = None,
    limit: int = 0,
    min_concepts: int = 0,  # only enrich notes with fewer than N concepts
    force: bool = False,    # re-enrich even if already has concepts
    dry_run: bool = False,
    progress_cb=None,       # optional callable(current, total, note_title)
) -> dict:
    """Run LLM concept enrichment across the vault.

    Args:
        project: Scope to one project. Empty = all projects.
        note_types: List of types to enrich. Default: all types.
        limit: Max notes to enrich. 0 = no limit.
        min_concepts: Only enrich notes with fewer than this many concepts.
                      0 = enrich all notes without concepts.
        force: Also enrich notes that already have concepts.
        dry_run: Show what would be done without writing.
        progress_cb: Optional callback for progress reporting.

    Returns:
        Stats dict: enriched, skipped, errors, new_concepts.
    """
    cfg = cfg or load_config()
    vault = VaultManager(cfg)

    api_key = _load_api_key()
    if not api_key and not dry_run:
        raise ValueError(
            f"Set {OPENAI_API_KEY_ENV} environment variable (or add to .env) to use mem enrich."
        )

    ontology_path = Path(__file__).parent / "ontology.yaml"
    ontology_text = _build_ontology_text(ontology_path)

    # Query candidates from index
    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row

    type_filter = note_types or ["note", "session", "decision", "source"]
    placeholders = ",".join("?" for _ in type_filter)

    params: list = list(type_filter)
    where_clauses = [f"n.type IN ({placeholders})"]

    if project:
        where_clauses.append("n.project = ?")
        params.append(project)

    if not force:
        # Only notes with fewer concepts than min_concepts threshold
        threshold = min_concepts if min_concepts > 0 else 1
        where_clauses.append(
            f"(SELECT COUNT(*) FROM note_concepts WHERE note_id = n.id) < {threshold}"
        )

    where_sql = " AND ".join(where_clauses)
    query = f"""
        SELECT n.id, n.title, n.path, n.type, n.project, n.body_text
        FROM notes n
        WHERE {where_sql}
        ORDER BY
            CASE n.type
                WHEN 'session' THEN 1
                WHEN 'decision' THEN 2
                WHEN 'note' THEN 3
                WHEN 'source' THEN 4
                ELSE 5
            END,
            n.date DESC
    """
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = db.execute(query, params).fetchall()
    db.close()

    total = len(rows)
    stats = {"enriched": 0, "skipped": 0, "errors": 0, "new_concepts": 0}

    if total == 0:
        return stats

    # Process in batches
    for batch_start in range(0, total, BATCH_SIZE):
        batch_rows = rows[batch_start : batch_start + BATCH_SIZE]

        notes_payload = []
        for row in batch_rows:
            body = (row["body_text"] or "").strip()
            # Strip See Also section from body (we added those, not useful for tagging)
            if "## See Also" in body:
                body = body[:body.index("## See Also")].strip()
            preview = body[:BODY_PREVIEW_CHARS]
            notes_payload.append({
                "id": row["id"],
                "title": row["title"],
                "body": preview,
                "type": row["type"],
                "project": row["project"] or "",
                "path": row["path"],
            })

        if progress_cb:
            progress_cb(batch_start, total, batch_rows[0]["title"])

        try:
            results = _call_openai(
                notes_payload, ontology_text, api_key, dry_run=dry_run
            )
        except Exception as e:
            stats["errors"] += len(batch_rows)
            print(f"  Batch error ({batch_rows[0]['title'][:40]}...): {e}")
            time.sleep(1)
            continue

        # Write results back to frontmatter
        id_to_path = {n["id"]: n["path"] for n in notes_payload}
        for result in results:
            note_id = result.get("id", "")
            concepts = result.get("concepts", [])
            rel_path = id_to_path.get(note_id, "")

            if not rel_path or not concepts:
                stats["skipped"] += 1
                continue

            if dry_run:
                stats["enriched"] += 1
                stats["new_concepts"] += len(concepts)
                continue

            try:
                modified = _write_concepts_to_note(vault, rel_path, concepts)
                if modified:
                    stats["enriched"] += 1
                    stats["new_concepts"] += len(concepts)
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"  Write error ({rel_path}): {e}")

        # Brief pause between batches to avoid rate limits
        if not dry_run and batch_start + BATCH_SIZE < total:
            time.sleep(0.5)

    return stats
