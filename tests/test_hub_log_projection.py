"""Tests for the ``hub_log_entries`` SQL projection (S7).

The evolution-DAG substrate: every catalyst-log entry on a concept hub or
theme is projected into SQLite so dated/flagged hub history is queryable
without parsing markdown. Markdown stays truth — the table is rebuilt by
``weave index --full`` and kept fresh by per-file re-index.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


def _rebuild(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _rows(config: Config, hub_id: str) -> list[dict]:
    idx = Indexer(config=config)
    try:
        return [
            dict(r)
            for r in idx.db.execute(
                "SELECT * FROM hub_log_entries WHERE hub_id = ? "
                "ORDER BY seq",
                (hub_id,),
            )
        ]
    finally:
        idx.close()


def _write_concept_hub(config: Config, concept: str, entries: list[str]) -> Path:
    topics = config.vault_root / "concepts" / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: concept-hub",
        f"concept: {concept}",
        "---",
        "",
        f"# {concept}",
        "",
        "## Essence",
        "",
        "*No synthesis yet.*",
        "",
        "## Catalyst log",
        "",
        *entries,
    ]
    p = topics / f"{concept}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestConceptHubProjection:
    def test_entries_round_trip(self, config: Config, vault: VaultManager):
        d1, d2 = "2026-05-01", "2026-06-01"
        _write_concept_hub(
            config,
            "agentic-ai",
            [
                f"- {d1} · *new* — first artifact — [[n-aaaa1111]]",
                f"- {d2} · *extends {d1}* — second artifact — [[n-bbbb2222]]",
            ],
        )
        _rebuild(config)

        rows = _rows(config, "agentic-ai")
        assert len(rows) == 2
        assert rows[0]["hub_kind"] == "concept"
        assert rows[0]["entry_date"] == d1
        assert rows[0]["flag"] == "new"
        assert rows[0]["cited_note_id"] == "n-aaaa1111"
        assert rows[0]["text"] == "first artifact"
        # Intra-log ref (weave hubs link temporal DAG) survives projection.
        assert rows[1]["flag"] == "extends"
        assert rows[1]["ref_date"] == d1
        assert rows[1]["cited_note_id"] == "n-bbbb2222"

    def test_fold_section_entries_included(
        self, config: Config, vault: VaultManager
    ):
        """Entries collapsed into <details> by the fold still project."""
        from thinkweave.synthesis.hub import (
            FLAG_NEW,
            Hub,
            HubLogEntry,
            LOG_FOLD_THRESHOLD,
        )

        n = LOG_FOLD_THRESHOLD + 5
        log = [
            HubLogEntry(
                date=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                flag=FLAG_NEW,
                text=f"entry {i}",
                citation=f"n-{i:08x}",
            )
            for i in range(n)
        ]
        hub = Hub(id="folded-hub", title="folded-hub", essence="A real essence.", log=log)
        topics = config.vault_root / "concepts" / "topics"
        topics.mkdir(parents=True, exist_ok=True)
        body = hub.render()
        (topics / "folded-hub.md").write_text(
            "---\ntype: concept-hub\nconcept: folded-hub\n---\n\n" + body + "\n",
            encoding="utf-8",
        )
        assert "<details>" in body  # the fold actually fired
        _rebuild(config)

        rows = _rows(config, "folded-hub")
        assert len(rows) == n

    def test_reindex_is_idempotent(self, config: Config, vault: VaultManager):
        _write_concept_hub(
            config,
            "fts5",
            ["- 2026-06-01 · *new* — artifact — [[n-aaaa1111]]"],
        )
        _rebuild(config)
        _rebuild(config)
        assert len(_rows(config, "fts5")) == 1

    def test_hub_removal_clears_rows(self, config: Config, vault: VaultManager):
        p = _write_concept_hub(
            config,
            "dead-term",
            ["- 2026-06-01 · *new* — artifact — [[n-aaaa1111]]"],
        )
        _rebuild(config)
        assert len(_rows(config, "dead-term")) == 1
        p.unlink()
        _rebuild(config)
        assert _rows(config, "dead-term") == []


def _write_note(config: Config, rel: str, note_id: str, title: str) -> Path:
    """Write a minimal note file at a known vault-relative path with a known id."""
    p = config.vault_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {note_id}\ntype: note\ntitle: {title}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return p


class TestTitleAliasedCitationResolution:
    """Defect A — the lazily-cached path→id map is empty/partial during a
    full rebuild (notes table wiped up front), so title-aliased citations
    ([[path|Title]]) used to store paths in ``cited_note_id``. The second
    pass (``_resync_hub_logs``) re-resolves with a complete map."""

    def test_full_rebuild_twice_resolves_title_aliased_citation(
        self, config: Config, vault: VaultManager
    ):
        _write_note(config, "notes/sky-study.md", "n-feedbeef", "Sky Study")
        _write_concept_hub(
            config,
            "sky",
            ["- 2026-06-01 · *new* — artifact — [[notes/sky-study|Sky Study]]"],
        )
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)
            idx.rebuild(full=True)
        finally:
            idx.close()

        rows = _rows(config, "sky")
        assert len(rows) == 1
        assert rows[0]["cited_note_id"] == "n-feedbeef"

    def test_full_rebuild_resolves_note_indexed_after_map_was_cached(
        self, config: Config, vault: VaultManager
    ):
        """The poisoning case: the map gets cached before the cited note
        exists; a later full rebuild on the same Indexer must not reuse it."""
        _write_concept_hub(
            config,
            "late",
            ["- 2026-06-01 · *new* — artifact — [[notes/late-note|Late Note]]"],
        )
        idx = Indexer(config=config)
        try:
            idx.rebuild(full=True)  # caches a map lacking the note
            _write_note(config, "notes/late-note.md", "n-deadc0de", "Late Note")
            idx.rebuild(full=True)
        finally:
            idx.close()

        rows = _rows(config, "late")
        assert len(rows) == 1
        assert rows[0]["cited_note_id"] == "n-deadc0de"


class TestArchivedHubExclusion:
    """Defect B — ``concepts/topics/_archive/`` (merged/demoted hubs) must
    not be indexed: an archived hub shares its stem with the live hub, so
    its rows would clobber the live hub's in ``hub_log_entries``."""

    def test_archived_hub_not_indexed_and_does_not_clobber_live_rows(
        self, config: Config, vault: VaultManager
    ):
        _write_concept_hub(
            config,
            "regime",
            [
                "- 2026-05-01 · *new* — first — [[n-aaaa1111]]",
                "- 2026-06-01 · *extends 2026-05-01* — second — [[n-bbbb2222]]",
            ],
        )
        # Stale archived copy with the same stem but a different log.
        archive = config.vault_root / "concepts" / "topics" / "_archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "regime.md").write_text(
            "---\ntype: concept-hub\nconcept: regime\nmerged-into: other\n---\n\n"
            "# regime\n\n## Essence\n\nOld.\n\n## Catalyst log\n\n"
            "- 2026-04-01 · *new* — stale — [[n-cccc3333]]\n",
            encoding="utf-8",
        )
        _rebuild(config)

        rows = _rows(config, "regime")
        assert len(rows) == 2
        assert {r["cited_note_id"] for r in rows} == {"n-aaaa1111", "n-bbbb2222"}

        idx = Indexer(config=config)
        try:
            n = idx.db.execute(
                "SELECT COUNT(*) FROM notes "
                "WHERE path LIKE 'concepts/topics/_archive/%'"
            ).fetchone()[0]
        finally:
            idx.close()
        assert n == 0


class TestThemeProjection:
    def test_theme_log_projects_with_thm_id(
        self, config: Config, vault: VaultManager
    ):
        theme = vault.create_note(
            NoteType.THEME,
            "iran-war",
            body=(
                "## Essence\n\nThe arc.\n\n## Catalyst log\n\n"
                "- 2026-06-01 · *new* — strikes begin — [[src-aaaa1111]]\n"
                "\n## Open questions\n"
            ),
            extra_frontmatter={"concepts": ["geopolitics"], "status": "active"},
        )
        fm, _ = parse_frontmatter(theme.read_text(encoding="utf-8"))
        thm_id = fm["id"]
        _rebuild(config)

        rows = _rows(config, thm_id)
        assert len(rows) == 1
        assert rows[0]["hub_kind"] == "theme"
        assert rows[0]["cited_note_id"] == "src-aaaa1111"

    def test_non_hub_note_produces_no_rows(
        self, config: Config, vault: VaultManager
    ):
        vault.create_note(
            NoteType.NOTE,
            "plain note",
            body=(
                "## Catalyst log\n\n"
                "- 2026-06-01 · *new* — looks like a log — [[n-aaaa1111]]\n"
            ),
            project="t",
        )
        _rebuild(config)
        idx = Indexer(config=config)
        try:
            n = idx.db.execute(
                "SELECT COUNT(*) FROM hub_log_entries"
            ).fetchone()[0]
        finally:
            idx.close()
        assert n == 0
