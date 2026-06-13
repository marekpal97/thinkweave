"""LLM-assisted concept enrichment for vault notes.

Sends batches of notes to the configured completion provider (resolved from
``vault/config/api.yaml::overrides.enrich`` via ``core.agent_client``; default
gpt-5-mini) and assigns concepts from the ontology vocabulary. Writes concepts
directly to markdown frontmatter — permanent, visible in Obsidian.

Covers all note types including sessions (0% coverage) and imported sources.

Usage:
    weave enrich [--project X] [--type session,note,decision,source]
               [--limit N] [--dry-run] [--force]
"""

from __future__ import annotations

import json
import sqlite3
import time

from thinkweave.core.config import Config, load_config
from thinkweave.core.vault import VaultManager, parse_frontmatter, render_frontmatter

# Env var named in the "no key configured" error message. Provider + model
# are resolved at call time from api.yaml::overrides.enrich, not hardcoded here.
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

BATCH_SIZE = 25  # notes per provider call
BODY_PREVIEW_CHARS = 500  # chars of body to include per note


def _build_ontology_text(ontology: dict[str, list[str]]) -> str:
    """Format the merged ontology dict as a compact prompt-friendly list.

    The dict is the same shape returned by ``load_ontology()`` (seed layered
    beneath the vault override), so the prompt always reflects the user's
    most recent ``/tighten`` cleanup, not just the shipped seed.
    """
    if not ontology:
        return "(no ontology found)"
    sections: list[str] = []
    for domain, concepts in ontology.items():
        if domain.startswith("_"):
            continue
        clean = [c for c in concepts if not c.startswith("_")]
        if clean:
            sections.append(f"{domain}: {', '.join(clean)}")
    return "\n".join(sections)


def _ontology_concept_set(ontology: dict[str, list[str]]) -> set[str]:
    """Flat set of every valid concept across all domains.

    Used to validate the LLM's output server-side: anything outside this set
    is treated as a *proposed* concept, not a canonical one — keeps invented
    vocabulary out of the live ``concepts:`` field while still capturing it
    for ``/tighten`` to review.
    """
    valid: set[str] = set()
    for domain, concepts in ontology.items():
        if domain.startswith("_"):
            continue
        for c in concepts:
            if not c.startswith("_"):
                valid.add(c.lower())
    return valid


_SYSTEM_PROMPT_TEMPLATE = """\
You are a concept tagger for a personal knowledge vault. Assign concepts to notes.

## Available concepts (domain: concept1, concept2, ...)
{ontology}

## Rules
- Assign 2-6 concepts per note from the list above when applicable
- Put concepts that match the list above into "concepts"
- If a note clearly covers a topic NOT in the list above and no listed concept fits, propose a new term (kebab-case) in "proposed_concepts" — never invent into "concepts"
- Sessions: extract concepts from the work described (e.g. fts5, mcp, sqlite) — NOT meta-concepts like "session" or "meeting"
- Decisions: tag with what was decided about (e.g. "indexing", "sqlite", "fts5")
- Sources: tag with the subject matter of the source
- Short/empty notes: assign at least 1 concept from context clues in the title
- Return ONLY valid JSON, no commentary

## Response format
{{"results": [{{"id": "note-id", "concepts": ["c1", "c2"], "proposed_concepts": ["new-term"]}}]}}"""

_USER_PROMPT_TEMPLATE = """\
Tag these {n} notes with concepts:

{notes_json}

Return JSON only."""


def load_openai_api_key() -> str:
    """Get OpenAI API key from env or .env (vault → cwd → project root).

    Thin shim over :func:`thinkweave.core.api_keys.get_provider_key` —
    preserved as a name for back-compat with the half-dozen callers
    that already import it. Returns ``""`` on miss (callers branch on
    truthiness; ``None`` would break that idiom).
    """
    from thinkweave.core.api_keys import get_provider_key

    return get_provider_key("openai") or ""


# Alias preserved for back-compat within this module.
_load_api_key = load_openai_api_key


def _call_enrich_model(
    notes: list[dict],
    ontology_text: str,
    api_key: str,  # accepted for back-compat — get_provider_key resolves internally
    *,
    dry_run: bool = False,
) -> list[dict]:
    """Send a batch through the agent_client wrapper and return [{id, concepts}].

    Switched from direct httpx → ``agent_client.get_completion_sync``
    on 2026-06-06 (plan B3). Provider + model resolve from
    ``vault/config/api.yaml::overrides.enrich`` (default openai /
    gpt-5-mini, mirroring legacy behaviour). The ``api_key`` arg is
    accepted for back-compat but ignored — the wrapper reads via
    :func:`thinkweave.core.api_keys.get_provider_key`.
    """
    del api_key  # back-compat only

    if dry_run:
        return [{"id": n["id"], "concepts": ["[dry-run]"]} for n in notes]

    system = _SYSTEM_PROMPT_TEMPLATE.format(ontology=ontology_text)
    notes_json = json.dumps(
        [{"id": n["id"], "title": n["title"], "body": n["body"]} for n in notes],
        ensure_ascii=False,
        indent=None,
    )
    user = _USER_PROMPT_TEMPLATE.format(n=len(notes), notes_json=notes_json)

    # Resolve provider + model from api.yaml::overrides.enrich.
    from thinkweave.core.agent_client import get_completion_sync
    from thinkweave.core.api_config import load_api_config, resolve_for_op

    cfg = load_config()
    op_cfg = resolve_for_op(load_api_config(cfg.vault_root), "enrich")

    raw, _usage = get_completion_sync(
        user,
        provider=op_cfg["provider"],
        model=op_cfg["model"],
        max_tokens=op_cfg["max_tokens"],
        system=system,
        response_format={"type": "json_object"},
    )
    raw = (raw or "").strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed.get("results", [])


def _write_concepts_to_note(
    vault: VaultManager,
    rel_path: str,
    concepts: list[str],
    proposed_concepts: list[str] | None = None,
) -> bool:
    """Update the concepts and proposed_concepts frontmatter fields.

    Canonical concepts (validated against the ontology) go to ``concepts:``;
    LLM-invented terms go to ``proposed_concepts:``. The split happens at
    the caller — this function trusts what it's given.

    Returns True if the file was modified.
    """
    file_path = vault.root / rel_path
    if not file_path.exists():
        return False

    text = file_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    # Merge canonical concepts.
    existing = fm.get("concepts", [])
    if isinstance(existing, str):
        existing = [c.strip() for c in existing.split(",") if c.strip()]
    merged = list(dict.fromkeys(existing + [c.lower().strip() for c in concepts if c]))

    # Merge proposed concepts.
    existing_proposed = fm.get("proposed_concepts", []) or []
    if isinstance(existing_proposed, str):
        existing_proposed = [c.strip() for c in existing_proposed.split(",") if c.strip()]
    proposed_in = proposed_concepts or []
    merged_proposed = list(dict.fromkeys(
        existing_proposed + [c.lower().strip() for c in proposed_in if c]
    ))

    nothing_new = (
        set(merged) == set(existing)
        and set(merged_proposed) == set(existing_proposed)
    )
    if nothing_new:
        return False

    fm["concepts"] = merged
    if merged_proposed:
        fm["proposed_concepts"] = merged_proposed
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
            f"Set {OPENAI_API_KEY_ENV} environment variable (or add to .env) to use weave enrich."
        )

    # Load the merged ontology (seed + vault override) so the prompt reflects
    # the user's most recent /tighten cleanup, not just the seed.
    from thinkweave.synthesis.concepts import load_ontology

    ontology = load_ontology()
    ontology_text = _build_ontology_text(ontology)
    valid_concept_set = _ontology_concept_set(ontology)

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
            results = _call_enrich_model(
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
            llm_concepts = result.get("concepts", []) or []
            llm_proposed = result.get("proposed_concepts", []) or []
            rel_path = id_to_path.get(note_id, "")

            # Server-side split: anything the LLM put under "concepts" that's
            # not actually in the ontology gets routed to proposed_concepts.
            # The LLM is asked to do this itself via the prompt, but we validate
            # because invented concepts in canonical fields are exactly the
            # sprawl faucet we're plugging.
            canonical = [
                c.lower().strip() for c in llm_concepts
                if c and c.lower().strip() in valid_concept_set
            ]
            invented = [
                c.lower().strip() for c in llm_concepts
                if c and c.lower().strip() not in valid_concept_set
            ]
            invented += [c.lower().strip() for c in llm_proposed if c]
            invented = list(dict.fromkeys(invented))

            if not rel_path or (not canonical and not invented):
                stats["skipped"] += 1
                continue

            if dry_run:
                stats["enriched"] += 1
                stats["new_concepts"] += len(canonical)
                continue

            try:
                modified = _write_concepts_to_note(
                    vault, rel_path, canonical, proposed_concepts=invented
                )
                if modified:
                    stats["enriched"] += 1
                    stats["new_concepts"] += len(canonical)
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"  Write error ({rel_path}): {e}")

        # Brief pause between batches to avoid rate limits
        if not dry_run and batch_start + BATCH_SIZE < total:
            time.sleep(0.5)

    return stats
