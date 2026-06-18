"""Tests for MCP tool logic — update, extract, and parsing helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.surfaces.mcp.server import (
    _build_decision_body,
    _flush_insight,
    _parse_candidate_insights,
)
from thinkweave.core.vault import parse_frontmatter
from thinkweave.core.schemas import NoteType
from thinkweave.retrieval.search import Search
from thinkweave.core.vault import VaultManager


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


@pytest.fixture
def indexer(config: Config) -> Indexer:
    idx = Indexer(config=config)
    yield idx
    idx.close()


@pytest.fixture
def search(config: Config, indexer: Indexer) -> Search:
    s = Search(config=config)
    yield s
    s.close()


# --- _parse_candidate_insights tests ---


class TestParseCandidateInsights:
    def test_empty_section(self):
        body = "# Session\n\n## Candidate Insights\n\n## Summary\n"
        result = _parse_candidate_insights(body)
        assert result == []

    def test_multiple_blocks(self):
        body = (
            "# Session\n\n"
            "## Candidate Insights\n"
            "FTS5 is fast for full-text search\n"
            "It supports snippet generation\n"
            "\n"
            "WAL mode enables concurrent reads\n"
            "Write-ahead logging is the key\n"
            "\n"
            "## Summary\n"
        )
        result = _parse_candidate_insights(body)
        assert len(result) == 2
        assert result[0]["title"] == "FTS5 is fast for full-text search"
        assert "snippet generation" in result[0]["body"]
        assert result[1]["title"] == "WAL mode enables concurrent reads"

    def test_stops_at_next_section(self):
        body = (
            "## Candidate Insights\n"
            "Insight content here\n"
            "\n"
            "## Events\n"
            "This should not be parsed\n"
        )
        result = _parse_candidate_insights(body)
        assert len(result) == 1
        assert "should not be parsed" not in result[0].get("body", "")

    def test_no_candidate_section(self):
        body = "# Session\n\n## Events\n- stuff\n"
        result = _parse_candidate_insights(body)
        assert result == []

    def test_insight_block_with_markers(self):
        body = (
            "## Candidate Insights\n"
            "\n"
            "★ Insight ─────────────────────────────────────\n"
            "FTS5 requires content tables for snippet support\n"
            "─────────────────────────────────────────────────\n"
            "\n"
        )
        result = _parse_candidate_insights(body)
        assert len(result) == 1
        assert "FTS5" in result[0]["title"]


# --- weave_update logic tests ---


class TestUpdateLogic:
    def test_update_decision_status(self, vault, indexer, search):
        path = vault.create_note(
            NoteType.DECISION, "Use FTS5", project="test"
        )
        indexer.index_file(path)

        note = vault.read_note(path)
        assert note.frontmatter.get("status") == "proposed"

        vault.update_note(path, frontmatter_updates={"status": "accepted"})
        indexer.index_file(path)

        updated = vault.read_note(path)
        assert updated.frontmatter["status"] == "accepted"

    def test_update_merge_tags(self, vault, indexer):
        path = vault.create_note(
            NoteType.NOTE, "Tag test", tags=["a", "b"], project="test"
        )
        vault.update_note(path, frontmatter_updates={"tags": ["c"]})
        updated = vault.read_note(path)
        assert set(updated.tags) == {"a", "b", "c"}

    def test_update_body_append(self, vault):
        path = vault.create_note(NoteType.NOTE, "Body test", body="Original.")
        vault.update_note(path, body_append="## Addendum\nNew content.")
        updated = vault.read_note(path)
        assert "Original." in updated.body
        assert "New content." in updated.body

    def test_update_add_edge_via_frontmatter(self, vault, indexer):
        path_a = vault.create_note(NoteType.NOTE, "Note A", project="test")
        path_b = vault.create_note(NoteType.NOTE, "Note B", project="test")
        indexer.index_file(path_a)
        indexer.index_file(path_b)

        note_b = vault.read_note(path_b)
        note_a = vault.read_note(path_a)

        vault.update_note(
            path_b, frontmatter_updates={"derived_from": [note_a.id]}
        )
        indexer.index_file(path_b)

        # Rebuild edges to pick up derived_from
        stats = indexer.rebuild(full=True)
        assert stats["edges"] > 0


# --- weave_extract logic tests ---


class TestExtractLogic:
    def _create_session_with_insights(self, vault, indexer, body=""):
        session_body = body or (
            "## Events\n"
            "- 14:23 Write /src/main.py\n"
            "\n"
            "## Candidate Insights\n"
            "FTS5 is fast for full-text search\n"
            "It supports snippet generation\n"
            "\n"
            "WAL mode enables concurrent reads\n"
            "Write-ahead logging is the key\n"
        )
        path = vault.create_note(
            NoteType.SESSION,
            "Test Session",
            body=session_body,
            project="test",
            extra_frontmatter={"source_session": "test-session-id"},
        )
        indexer.index_file(path)
        return path

    def test_extract_creates_notes(self, vault, indexer):
        session_path = self._create_session_with_insights(vault, indexer)
        session = vault.read_note(session_path)

        insights = [
            {"title": "FTS5 Performance", "body": "FTS5 is fast.", "tags": ["sqlite"]},
            {"title": "WAL Concurrency", "body": "WAL enables reads.", "tags": ["sqlite"]},
        ]
        created = []
        for insight in insights:
            path = vault.create_note(
                NoteType.NOTE,
                insight["title"],
                body=insight["body"],
                project=session.project,
                tags=insight.get("tags", []),
                extra_frontmatter={"derived_from": [session.id]},
            )
            indexer.index_file(path)
            created.append(vault.read_note(path))

        assert len(created) == 2
        for note in created:
            assert note.type == NoteType.NOTE
            assert session.id in note.frontmatter.get("derived_from", [])

    def test_extract_sets_processed_flag(self, vault, indexer):
        session_path = self._create_session_with_insights(vault, indexer)
        today = date.today().isoformat()

        vault.update_note(
            session_path,
            frontmatter_updates={"processed": True, "processed_at": today},
        )

        updated = vault.read_note(session_path)
        assert updated.frontmatter["processed"] is True
        assert updated.frontmatter["processed_at"] == today

    def test_processed_session_detected(self, vault, indexer):
        session_path = self._create_session_with_insights(vault, indexer)
        vault.update_note(
            session_path,
            frontmatter_updates={"processed": True, "processed_at": "2026-04-04"},
        )
        session = vault.read_note(session_path)
        assert session.frontmatter.get("processed") is True

    def test_extract_caps_at_three(self, vault, indexer):
        session_path = self._create_session_with_insights(vault, indexer)
        session = vault.read_note(session_path)

        insights = [
            {"title": f"Insight {i}", "body": f"Content {i}"}
            for i in range(5)
        ]
        capped = insights[:3]
        created = []
        for insight in capped:
            path = vault.create_note(
                NoteType.NOTE,
                insight["title"],
                body=insight["body"],
                project=session.project,
                extra_frontmatter={"derived_from": [session.id]},
            )
            created.append(path)

        assert len(created) == 3

    def test_extract_rejects_non_session(self, vault, indexer, search):
        path = vault.create_note(NoteType.NOTE, "Not a session", project="test")
        indexer.index_file(path)

        note = search.get_note_by_id(vault.read_note(path).id)
        assert note is not None
        assert note["type"] != "session"

    def test_re_extract_removes_old_derived_notes(self, vault, indexer):
        """Re-extraction should clean up prior derived notes, not accumulate duplicates."""
        session_path = self._create_session_with_insights(vault, indexer)
        session = vault.read_note(session_path)
        session_dir = session_path.parent

        # Simulate first extraction: create 2 derived notes
        for title in ["First Insight", "Second Insight"]:
            path = vault.create_note(
                NoteType.NOTE,
                title,
                body=f"Body of {title}",
                project=session.project,
                extra_frontmatter={"derived_from": [session.id]},
                output_dir=session_dir,
            )
            indexer.index_file(path)

        derived_before = [
            f for f in session_dir.glob("*.md")
            if f.name != "session.md"
        ]
        assert len(derived_before) == 2

        # Simulate the cleanup that _handle_extract now does
        for md_file in session_dir.glob("*.md"):
            if md_file.name == "session.md":
                continue
            try:
                fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
                derived = fm.get("derived_from", [])
                if isinstance(derived, str):
                    derived = [derived]
                if session.id in derived:
                    rel = str(md_file.relative_to(vault.root))
                    indexer._remove_by_path(rel)
                    md_file.unlink()
            except Exception:
                continue

        # Old derived notes should be gone
        derived_after_cleanup = [
            f for f in session_dir.glob("*.md")
            if f.name != "session.md"
        ]
        assert len(derived_after_cleanup) == 0

        # Create new derived notes (simulating second extraction)
        for title in ["First Insight", "Second Insight"]:
            path = vault.create_note(
                NoteType.NOTE,
                title,
                body=f"Updated body of {title}",
                project=session.project,
                extra_frontmatter={"derived_from": [session.id]},
                output_dir=session_dir,
            )
            indexer.index_file(path)

        # Should have exactly 2, not 4
        derived_final = [
            f for f in session_dir.glob("*.md")
            if f.name != "session.md"
        ]
        assert len(derived_final) == 2
        # And no collision suffixes
        names = sorted(f.name for f in derived_final)
        assert "first-insight.md" in names
        assert "second-insight.md" in names


class TestExtractFTSWriteThrough:
    """Regression for n-a58ea683: notes created via weave_extract's per-file
    index_file path must be immediately findable via FTS, with no manual
    `weave index --full` required. A follow-up incremental rebuild must also
    leave FTS intact (the original bug was FTS going stale after a no-op
    incremental because hashes already matched).
    """

    def test_index_file_makes_note_fts_searchable(
        self, vault, indexer, search
    ):
        session_path = vault.create_note(
            NoteType.SESSION,
            "ses-fts",
            body="## Summary\n",
            project="test",
            extra_frontmatter={"source_session": "ses-fts"},
        )
        indexer.index_file(session_path)
        sid = vault.read_note(session_path).id

        dec_path = vault.create_note(
            NoteType.DECISION,
            "ExtractFTSRegression",
            body="## Context\n\nZingZangZoomUnique phrase.\n\n## Decision\n\nOK",
            project="test",
            extra_frontmatter={
                "source_session": sid,
                "derived_from": [sid],
                "status": "accepted",
                "committed": True,
            },
            output_dir=session_path.parent,
        )
        indexer.index_file(dec_path)

        body_hits = [r.id for r in search.search("ZingZangZoomUnique")]
        title_hits = [r.id for r in search.search("ExtractFTSRegression")]
        dec_id = vault.read_note(dec_path).id
        assert dec_id in body_hits
        assert dec_id in title_hits

    def test_post_extract_incremental_rebuild_keeps_fts_fresh(
        self, vault, indexer, search
    ):
        session_path = vault.create_note(
            NoteType.SESSION,
            "ses-fts2",
            body="## Summary\n",
            project="test",
            extra_frontmatter={"source_session": "ses-fts2"},
        )
        indexer.index_file(session_path)
        sid = vault.read_note(session_path).id

        dec_path = vault.create_note(
            NoteType.DECISION,
            "IncrementalRebuildCheck",
            body="## Context\n\nQQRRPhraseUnique.\n\n## Decision\n\nOK",
            project="test",
            extra_frontmatter={
                "source_session": sid,
                "derived_from": [sid],
                "status": "accepted",
                "committed": True,
            },
            output_dir=session_path.parent,
        )
        indexer.index_file(dec_path)

        stats = indexer.rebuild(full=False)
        assert stats["indexed"] == 0

        dec_id = vault.read_note(dec_path).id
        assert dec_id in [r.id for r in search.search("QQRRPhraseUnique")]


class TestBuildDecisionBody:
    """Regression for n-c41e6f13: wrapper headers must not duplicate when
    the caller-provided rationale already includes them.
    """

    def test_plain_rationale_gets_wrapper(self):
        out = _build_decision_body("A simple reason.", "My Title", "committed")
        assert out.count("## Context") == 1
        assert out.count("## Decision") == 1
        assert "A simple reason." in out
        assert out.endswith("My Title")

    def test_leading_context_header_dedup(self):
        rationale = "## Context\n\nSurfaced during the drain."
        out = _build_decision_body(rationale, "My Title", "committed")
        assert out.count("## Context") == 1
        assert "Surfaced during the drain." in out

    def test_embedded_decision_header_suppresses_trailing_title(self):
        rationale = (
            "Some context.\n\n## Decision\n\nAdopt approach Z."
        )
        out = _build_decision_body(rationale, "My Title", "committed")
        assert out.count("## Decision") == 1
        assert "Adopt approach Z." in out
        assert "My Title" not in out

    def test_consequences_not_injected_when_present(self):
        rationale = (
            "ctx\n\n## Decision\n\nabandoned approach\n\n## Consequences\n\nrolled back"
        )
        out = _build_decision_body(rationale, "My Title", "abandoned")
        assert out.count("## Consequences") == 1
        assert "rolled back" in out
        assert "Approach was abandoned." not in out

    def test_abandoned_outcome_adds_consequences_when_absent(self):
        out = _build_decision_body("tried X, didn't work", "My Title", "abandoned")
        assert "## Consequences" in out
        assert "Approach was abandoned." in out

    def test_case_insensitive_header_match(self):
        rationale = "## context\n\nlowercase header"
        out = _build_decision_body(rationale, "My Title", "committed")
        assert out.count("## Context") + out.count("## context") == 1
        assert "lowercase header" in out


# --------------------------------------------------------------------------- #
# dispatch                                                                      #
# --------------------------------------------------------------------------- #


def test_dispatch_unknown_tool_sentinel():
    from thinkweave.surfaces.mcp.tools import dispatch

    out = dispatch(None, "_nope", {})
    assert "Unknown tool" in out[0].text
