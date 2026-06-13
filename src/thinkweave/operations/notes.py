"""Note operations — create / read / update / link.

Pure functions used by both CLI handlers and MCP tool implementations. The
`VaultManager` and `Indexer` classes still own the I/O; this module is the
single, narrow seam that both surfaces call into.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from thinkweave.core.config import Config
from thinkweave.core.indexer import EDGE_TYPE_TO_FIELD, Indexer
from thinkweave.core.schemas import NoteMeta, NoteType
from thinkweave.core.vault import VaultManager

logger = logging.getLogger(__name__)


class CreateResult(NamedTuple):
    """Return shape of :func:`create_note`.

    ``existed`` is ``True`` when a write-time dedup gate matched an existing
    source note and no new file was created — ``note`` then points to the
    pre-existing note. Callers that need to distinguish "created" from
    "existed" (workers, queue archivers) branch on this flag.
    """

    note: NoteMeta
    existed: bool


# Frontmatter key validator. SQL `json_extract($.<key>)` paths can't be
# parametrised, so we build the path with an f-string — keys must therefore
# come from a trusted source (sources.yaml) AND match a safe pattern. Reject
# anything else rather than risk SQL injection via a forged dedup_keys list.
_SAFE_FRONTMATTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_existing_source_by_dedup_keys(
    cfg: Config, source_type: str, frontmatter: dict
) -> str | None:
    """Return the id of an existing source note that matches ``frontmatter``
    on any of the dedup_keys configured for ``source_type`` — or ``None``.

    Case-folded string compare (LOWER+TRIM) mirrors
    :meth:`Queue._values_equal` so the queue's enqueue-time check and this
    write-time gate agree.

    Skipped (returns ``None``) when:
    - ``source_type`` has no entry / no ``dedup_keys`` in the merged config.
    - Every configured key has an empty/missing value in ``frontmatter``
      (no key to compare on — would false-positive across all empties).
    - The index DB is unavailable.
    """
    if not source_type:
        return None

    from thinkweave.acquisition.sources.config import load_user_config

    sources_cfg = load_user_config(cfg.vault_root).get("sources") or {}
    dedup_keys = (sources_cfg.get(source_type) or {}).get("dedup_keys") or []
    if not dedup_keys:
        return None

    # Filter to keys that (a) are safe to interpolate and (b) have a
    # non-empty value in the incoming frontmatter. No usable key = skip.
    usable: list[tuple[str, str]] = []
    for key in dedup_keys:
        if not isinstance(key, str) or not _SAFE_FRONTMATTER_KEY.match(key):
            continue
        value = frontmatter.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        usable.append((key, text))
    if not usable:
        return None

    idx = Indexer(config=cfg)
    try:
        for key, value in usable:
            row = idx.db.execute(
                f"""
                SELECT id FROM notes
                WHERE type = 'source'
                  AND json_extract(frontmatter, '$.{key}') IS NOT NULL
                  AND LOWER(TRIM(json_extract(frontmatter, '$.{key}'))) = LOWER(TRIM(?))
                LIMIT 1
                """,
                (value,),
            ).fetchone()
            if row:
                return row[0] if not isinstance(row, dict) else row["id"]
    finally:
        idx.close()
    return None


def create_note(
    cfg: Config,
    *,
    note_type: NoteType,
    title: str,
    body: str = "",
    project: str = "",
    tags: list[str] | None = None,
    extra_frontmatter: dict | None = None,
    session_id: str = "",
    output_dir: Path | None = None,
) -> CreateResult:
    """Create a note (or return an existing source dupe) and reindex.

    Returns a :class:`CreateResult` ``(note, existed)``. When ``note_type``
    is :attr:`NoteType.SOURCE` and the incoming ``source_type`` carries
    ``dedup_keys`` (per ``vault/.weave/sources.yaml`` overlaid on
    ``DEFAULT_CONFIG``), this function first looks for an existing source
    note whose frontmatter matches on any configured key. On hit, it
    returns that note with ``existed=True`` and never writes — the single
    write-time chokepoint that prevents the paper/news dupes already
    present in the vault from accumulating further.

    Strict ontology gating: any incoming ``concepts`` / ``proposed_concepts``
    in ``extra_frontmatter`` are split through the merged ontology —
    canonical terms stay in ``concepts:``, unrecognised ones get routed to
    ``proposed_concepts:`` for later promotion via ``/weave-resolve-concepts``.
    Both surfaces (CLI ``weave add`` and MCP ``weave_create``) get this gate
    uniformly; ``weave_extract`` runs its own equivalent split before reaching
    this function.
    """
    from thinkweave.synthesis.concepts import split_concepts_by_ontology

    fm = dict(extra_frontmatter) if extra_frontmatter else {}
    if "concepts" in fm or "proposed_concepts" in fm:
        canonical, proposed = split_concepts_by_ontology(
            fm.get("concepts"),
            proposed=fm.get("proposed_concepts"),
        )
        if canonical:
            fm["concepts"] = canonical
        else:
            fm.pop("concepts", None)
        if proposed:
            fm["proposed_concepts"] = proposed
        else:
            fm.pop("proposed_concepts", None)

    # Soft theme-ref validation gate. Drops `relates_to: [thm-X]` entries that
    # don't appear in the registry; log a warning. Mirrors how ``proposed_concepts:``
    # is the gentle counterpart to the strict ontology gate — broken refs don't
    # block the write, but they don't poison the index either.
    relates_to = fm.get("relates_to")
    if relates_to:
        if isinstance(relates_to, str):
            relates_to = [relates_to]
        thm_refs = [r for r in relates_to if str(r).startswith("thm-")]
        if thm_refs:
            try:
                from thinkweave.synthesis import theme_registry

                unknown = [
                    r for r in thm_refs
                    if not theme_registry.is_canonical(cfg, str(r))
                ]
                if unknown:
                    logger.warning(
                        "create_note: dropping unknown theme refs from relates_to: %s",
                        unknown,
                    )
                    kept = [r for r in relates_to if str(r) not in unknown]
                    if kept:
                        fm["relates_to"] = kept
                    else:
                        fm.pop("relates_to", None)
            except Exception:  # noqa: BLE001
                # Registry unavailable — pass through unvalidated.
                pass

    # Write-time dedup gate. Sources only; other note types (sessions,
    # decisions, themes) have their own identity rules and don't benefit
    # from frontmatter-key matching.
    if note_type == NoteType.SOURCE:
        source_type = str(fm.get("source_type") or "").strip()
        if source_type:
            # `title` is a positional arg that VaultManager folds into
            # frontmatter on render; surface it here so configs that list
            # `title` in dedup_keys (paper, article) match correctly.
            lookup_fm = dict(fm)
            if title and "title" not in lookup_fm:
                lookup_fm["title"] = title
            existing_id = find_existing_source_by_dedup_keys(cfg, source_type, lookup_fm)
            if existing_id:
                existing = read_note(cfg, existing_id)[0]
                if existing is not None:
                    return CreateResult(note=existing, existed=True)

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    path = vm.create_note(
        note_type=note_type,
        title=title,
        body=body,
        project=project,
        tags=tags,
        extra_frontmatter=fm or None,
        session_id=session_id,
        output_dir=output_dir,
    )

    idx = Indexer(config=cfg)
    idx.index_file(path)
    idx.close()

    created = vm.read_note(path)

    # Headless supersession enqueue. When weave_create writes a decision that
    # declares ``supersedes: [dec-X, ...]``, queue each predecessor for
    # re-judgment by the /judge-prediction skill. NOTE: we deliberately do
    # NOT flip predecessors' ``status: superseded`` here — that asymmetry
    # is intentional. ``operations/extract.py`` does the structural flip
    # within the wrap context (where it has the freshly-extracted decision
    # in hand); headless writes only signal the verdict pipeline. A future
    # explicit op can do the status flip if/when we need it.
    if note_type == NoteType.DECISION:
        predecessors = fm.get("supersedes") or []
        if isinstance(predecessors, str):
            predecessors = [predecessors]
        if predecessors:
            from thinkweave.operations import rejudge_queue

            for target_id in predecessors:
                if not target_id:
                    continue
                try:
                    rejudge_queue.enqueue(
                        cfg,
                        decision_id=str(target_id),
                        reason=f"superseded by {created.id}",
                        source="supersession",
                    )
                except Exception:
                    continue

    return CreateResult(note=created, existed=False)


def read_note(cfg: Config, note_id: str) -> tuple[NoteMeta | None, str | None]:
    """Read a note by id. Returns (NoteMeta, raw_text) — or (None, None) if missing."""
    from thinkweave.retrieval.search import Search

    s = Search(config=cfg)
    row = s.get_note_by_id(note_id)
    s.close()
    if not row:
        return None, None

    vm = VaultManager(config=cfg)
    full_path = vm.root / row["path"]
    if not full_path.exists():
        return vm.read_note(full_path) if False else None, None

    return vm.read_note(full_path), full_path.read_text(encoding="utf-8")


def update_note(
    cfg: Config,
    note_id: str,
    *,
    frontmatter_updates: dict | None = None,
    body_append: str = "",
    remove_tags: list[str] | None = None,
) -> NoteMeta:
    """Update a note's frontmatter / body. Re-indexes. Raises ValueError on bad input."""
    if not (frontmatter_updates or body_append or remove_tags):
        raise ValueError("Nothing to update.")

    idx = Indexer(config=cfg)
    row = idx.db.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchone()
    idx.close()
    if not row:
        raise FileNotFoundError(f"Note {note_id} not found")

    vm = VaultManager(config=cfg)
    path = vm.root / row["path"]
    if not path.exists():
        raise FileNotFoundError(f"File missing for {note_id}: {row['path']}")

    # Pre-read the existing supersedes list so we can diff against the
    # update and enqueue only newly-added predecessors. Same asymmetry as
    # create_note: headless ``supersedes:`` extension signals the verdict
    # pipeline; the structural status flip on predecessors stays the
    # responsibility of the wrap-context path in operations/extract.py.
    existing_pre = vm.read_note(path)
    pre_supersedes: set[str] = set()
    if existing_pre.type == NoteType.DECISION:
        raw = existing_pre.frontmatter.get("supersedes") or []
        if isinstance(raw, str):
            raw = [raw]
        pre_supersedes = {str(s) for s in raw if s}

    # Strict ontology gate on `concepts:` writes. Without this, a caller
    # passing ``frontmatter_updates={"concepts": [...]}`` could land arbitrary
    # strings as canonical concepts, bypassing the gate that ``create_note``
    # enforces. Only the ``concepts`` field is gated — every other key
    # (``tags``, ``status``, ``commit_refs``, ...) passes through untouched.
    # Non-canonical terms get merged into ``proposed_concepts:`` alongside any
    # already present on the note (de-duped, lowercased, stripped via
    # ``split_concepts_by_ontology``).
    if frontmatter_updates and "concepts" in frontmatter_updates:
        from thinkweave.synthesis.concepts import split_concepts_by_ontology

        frontmatter_updates = dict(frontmatter_updates)
        incoming_proposed = frontmatter_updates.get("proposed_concepts")
        if incoming_proposed is None:
            # Merge against the note's existing proposed_concepts so we don't
            # drop entries the caller didn't touch.
            existing_proposed = existing_pre.frontmatter.get("proposed_concepts") or []
            if isinstance(existing_proposed, str):
                existing_proposed = [existing_proposed]
            incoming_proposed = list(existing_proposed)
        canonical, proposed = split_concepts_by_ontology(
            frontmatter_updates.get("concepts"),
            proposed=incoming_proposed,
        )
        if canonical:
            frontmatter_updates["concepts"] = canonical
        else:
            frontmatter_updates.pop("concepts", None)
        if proposed:
            frontmatter_updates["proposed_concepts"] = proposed
        else:
            frontmatter_updates.pop("proposed_concepts", None)

    vm.update_note(
        path,
        frontmatter_updates=frontmatter_updates,
        body_append=body_append,
        remove_tags=remove_tags,
    )
    idx2 = Indexer(config=cfg)
    idx2.index_file(path)
    idx2.close()
    updated = vm.read_note(path)

    # Diff supersedes; enqueue only the new entries. Idempotent on
    # decision_id at the queue layer, but the diff avoids spurious calls.
    if updated.type == NoteType.DECISION:
        raw_post = updated.frontmatter.get("supersedes") or []
        if isinstance(raw_post, str):
            raw_post = [raw_post]
        post_supersedes = {str(s) for s in raw_post if s}
        added = post_supersedes - pre_supersedes
        if added:
            from thinkweave.operations import rejudge_queue

            for target_id in added:
                try:
                    rejudge_queue.enqueue(
                        cfg,
                        decision_id=target_id,
                        reason=f"superseded by {updated.id}",
                        source="supersession",
                    )
                except Exception:
                    continue

    return updated


def link_notes(cfg: Config, source_id: str, target_id: str, edge_type: str) -> None:
    """Add a typed edge from source to target."""
    idx = Indexer(config=cfg)
    src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()
    tgt = idx.db.execute("SELECT id FROM notes WHERE id = ?", (target_id,)).fetchone()
    if not src:
        idx.close()
        raise FileNotFoundError(f"Source note {source_id} not found")
    if not tgt:
        idx.close()
        raise FileNotFoundError(f"Target note {target_id} not found")

    vm = VaultManager(config=cfg)
    fm_field = EDGE_TYPE_TO_FIELD[edge_type]
    source_path = vm.root / src["path"]
    vm.update_note(source_path, frontmatter_updates={fm_field: [target_id]})
    idx.index_file(source_path)
    idx.close()


def unlink_notes(cfg: Config, source_id: str, target_id: str, edge_type: str) -> bool:
    """Remove a typed edge. Returns True if removed, False if no matching edge."""
    from thinkweave.core.vault import parse_frontmatter, render_frontmatter

    idx = Indexer(config=cfg)
    src = idx.db.execute("SELECT path FROM notes WHERE id = ?", (source_id,)).fetchone()
    if not src:
        idx.close()
        raise FileNotFoundError(f"Source note {source_id} not found")

    vm = VaultManager(config=cfg)
    source_path = vm.root / src["path"]
    note = vm.read_note(source_path)
    fm_field = EDGE_TYPE_TO_FIELD[edge_type]
    targets = note.frontmatter.get(fm_field, [])
    if isinstance(targets, str):
        targets = [targets] if targets else []
    if target_id not in targets:
        idx.close()
        return False

    new_targets = [t for t in targets if t != target_id]
    text = source_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if new_targets:
        fm[fm_field] = new_targets
    else:
        fm.pop(fm_field, None)
    source_path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")
    idx.index_file(source_path)
    idx.close()
    return True
