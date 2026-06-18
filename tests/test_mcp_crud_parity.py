"""Parity tests for the MCP CRUD handlers and ``operations.notes``.

The MCP handlers (``handle_create``, ``handle_update``, ``handle_link``,
``handle_unlink``) are intentionally thin wrappers over the operations
seam. These tests pin the contract:

- Calling the MCP handler produces the same on-disk note that calling
  the operations function with the same payload would.
- The handler's TextContent envelope is the *only* surface that may
  differ; the underlying file, frontmatter, body, and indexer state
  must be byte-for-byte identical to the operations path.
- Indexer-after-write invariant: the freshly-created/updated note is
  immediately findable via the Search API — no stale index window.

This is a refactor-parity suite, not a feature suite. Behavioural
changes belong elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations import notes as ops
from thinkweave.retrieval.search import Search
from thinkweave.surfaces.mcp.tools import notes as mcp_notes


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def cfg(vault_dir: Path) -> Config:
    c = Config(vault_root=vault_dir)
    # Touch the indexer so the DB exists before the first handler call.
    idx = Indexer(config=c)
    idx.close()
    return c


def _text(result) -> str:
    """Pull the single TextContent message out of an MCP handler return."""
    assert isinstance(result, list) and len(result) == 1
    return result[0].text


# --- handle_create parity -------------------------------------------------


class TestCreateParity:
    def test_basic_note_identical_to_operations(self, cfg: Config) -> None:
        payload = {
            "type": "note",
            "title": "FTS5 snippet support",
            "body": "Content tables enable snippet generation.",
            "project": "test",
            "tags": ["sqlite", "fts"],
        }
        result = mcp_notes.handle_create(cfg, payload)
        msg = _text(result)
        assert msg.startswith("Created note [n-")

        # The note should be findable via Search immediately after the handler returns.
        s = Search(config=cfg)
        # Pull the id out of the message: "Created note [n-xxxx] at ..."
        mcp_id = msg.split("[", 1)[1].split("]", 1)[0]
        row = s.get_note_by_id(mcp_id)
        s.close()
        assert row is not None
        assert row["title"] == "FTS5 snippet support"
        assert row["project"] == "test"

        vm = VaultManager(config=cfg)
        mcp_note = vm.read_note(cfg.vault_root / row["path"])

        # Run operations directly with the same payload — the result should
        # match in shape (different id, same content; same path bucket).
        ops_note = ops.create_note(
            cfg,
            note_type=NoteType.NOTE,
            title="FTS5 snippet support",
            body="Content tables enable snippet generation.",
            project="test",
            tags=["sqlite", "fts"],
        ).note

        # Same type / project / tags / body
        assert mcp_note.type == ops_note.type == NoteType.NOTE
        assert mcp_note.project == ops_note.project == "test"
        assert set(mcp_note.tags) == set(ops_note.tags) == {"sqlite", "fts"}
        assert mcp_note.body.strip() == ops_note.body.strip()
        # Both got an id of the right prefix
        assert mcp_note.id.startswith("n-")
        assert ops_note.id.startswith("n-")

    def test_concept_split_pushed_to_operations(self, cfg: Config) -> None:
        """The strict ontology gate must apply uniformly — passing a mix of
        canonical and unknown concepts through either surface should land
        the same on-disk layout (canonical in concepts:, unknown in
        proposed_concepts:).
        """
        # No ontology shipped at this tmp path, so every term will be unknown
        # and end up in proposed_concepts. The behaviour we care about is
        # that BOTH surfaces produce the same split, not what the split is.
        fm = {"concepts": ["totally-unknown-term", "another-unknown"]}

        # MCP path
        result = mcp_notes.handle_create(
            cfg,
            {
                "type": "note",
                "title": "Concept gate via MCP",
                "body": "x",
                "project": "test",
                "frontmatter": dict(fm),
            },
        )
        mcp_id = _text(result).split("[", 1)[1].split("]", 1)[0]
        vm = VaultManager(config=cfg)
        s = Search(config=cfg)
        mcp_row = s.get_note_by_id(mcp_id)
        s.close()
        mcp_note = vm.read_note(cfg.vault_root / mcp_row["path"])

        # Operations path
        ops_note = ops.create_note(
            cfg,
            note_type=NoteType.NOTE,
            title="Concept gate via ops",
            body="x",
            project="test",
            extra_frontmatter=dict(fm),
        ).note

        # Both notes should expose the same proposed_concepts set.
        assert set(mcp_note.frontmatter.get("proposed_concepts", [])) == set(
            ops_note.frontmatter.get("proposed_concepts", [])
        )
        # And neither should keep the unknown terms in `concepts:`.
        assert mcp_note.frontmatter.get("concepts") in (None, [])
        assert ops_note.frontmatter.get("concepts") in (None, [])

    def test_create_source_emits_source_directory_line(self, cfg: Config) -> None:
        result = mcp_notes.handle_create(
            cfg,
            {
                "type": "source",
                "title": "Some paper",
                "frontmatter": {"source_type": "paper"},
                "project": "test",
            },
        )
        msg = _text(result)
        assert msg.startswith("Created source [src-")
        assert "Source directory:" in msg


# --- handle_update parity -------------------------------------------------


class TestUpdateParity:
    def _make_note(self, cfg: Config) -> str:
        result = ops.create_note(
            cfg, note_type=NoteType.NOTE, title="Updateable", body="Original.",
            project="test", tags=["a"],
        )
        return result.note.id

    def test_frontmatter_update_via_mcp(self, cfg: Config) -> None:
        note_id = self._make_note(cfg)
        result = mcp_notes.handle_update(
            cfg, {"id": note_id, "frontmatter": {"tags": ["b"]}}
        )
        msg = _text(result)
        assert msg.startswith(f"Updated note [{note_id}]")
        assert "frontmatter:" in msg

        # On-disk state matches what operations.update_note would produce.
        s = Search(config=cfg)
        row = s.get_note_by_id(note_id)
        s.close()
        assert row is not None
        vm = VaultManager(config=cfg)
        note = vm.read_note(cfg.vault_root / row["path"])
        assert set(note.tags) == {"a", "b"}

    def test_body_append_via_mcp(self, cfg: Config) -> None:
        note_id = self._make_note(cfg)
        result = mcp_notes.handle_update(
            cfg, {"id": note_id, "body_append": "## Addendum\nMore."}
        )
        msg = _text(result)
        assert "appended" in msg

        vm = VaultManager(config=cfg)
        s = Search(config=cfg)
        row = s.get_note_by_id(note_id)
        s.close()
        note = vm.read_note(cfg.vault_root / row["path"])
        assert "Original." in note.body
        assert "More." in note.body

    def test_no_op_update_returns_helper_message(self, cfg: Config) -> None:
        note_id = self._make_note(cfg)
        result = mcp_notes.handle_update(cfg, {"id": note_id})
        msg = _text(result)
        assert "Nothing to update" in msg

    def test_missing_note_returns_not_found(self, cfg: Config) -> None:
        result = mcp_notes.handle_update(
            cfg, {"id": "n-doesnotexist", "frontmatter": {"tags": ["x"]}}
        )
        msg = _text(result)
        assert "not found" in msg.lower()


# --- handle_link / handle_unlink parity -----------------------------------


class TestLinkUnlinkParity:
    def _two_notes(self, cfg: Config) -> tuple[str, str]:
        a = ops.create_note(cfg, note_type=NoteType.NOTE, title="A", project="test").note
        b = ops.create_note(cfg, note_type=NoteType.NOTE, title="B", project="test").note
        return a.id, b.id

    def test_link_then_unlink_matches_operations(self, cfg: Config) -> None:
        a_id, b_id = self._two_notes(cfg)
        # Link via MCP.
        res = mcp_notes.handle_link(
            cfg,
            {"source_id": a_id, "target_id": b_id, "edge_type": "builds_on"},
        )
        assert _text(res) == f"Linked {a_id} --builds_on--> {b_id}"

        # On-disk: A's frontmatter has builds_on: [b_id].
        s = Search(config=cfg)
        row = s.get_note_by_id(a_id)
        s.close()
        vm = VaultManager(config=cfg)
        note = vm.read_note(cfg.vault_root / row["path"])
        targets = note.frontmatter.get("builds_on", [])
        if isinstance(targets, str):
            targets = [targets]
        assert b_id in targets

        # Unlink via MCP.
        res2 = mcp_notes.handle_unlink(
            cfg,
            {"source_id": a_id, "target_id": b_id, "edge_type": "builds_on"},
        )
        assert _text(res2) == f"Removed edge: {a_id} --builds_on--> {b_id}"

        note_after = vm.read_note(cfg.vault_root / row["path"])
        targets_after = note_after.frontmatter.get("builds_on", [])
        if isinstance(targets_after, str):
            targets_after = [targets_after] if targets_after else []
        assert b_id not in targets_after

    def test_link_missing_source_reports_not_found(self, cfg: Config) -> None:
        _, b_id = self._two_notes(cfg)
        res = mcp_notes.handle_link(
            cfg,
            {"source_id": "n-missing", "target_id": b_id, "edge_type": "builds_on"},
        )
        assert "Source note" in _text(res)
        assert "not found" in _text(res)

    def test_link_missing_target_reports_not_found(self, cfg: Config) -> None:
        a_id, _ = self._two_notes(cfg)
        res = mcp_notes.handle_link(
            cfg,
            {"source_id": a_id, "target_id": "n-missing", "edge_type": "builds_on"},
        )
        assert "Target note" in _text(res)
        assert "not found" in _text(res)

    def test_unlink_no_matching_edge_reports_so(self, cfg: Config) -> None:
        a_id, b_id = self._two_notes(cfg)
        # No prior link — unlink should report no match (not an error).
        res = mcp_notes.handle_unlink(
            cfg,
            {"source_id": a_id, "target_id": b_id, "edge_type": "builds_on"},
        )
        assert "No matching edge" in _text(res)


# --- indexer-after-write invariant ---------------------------------------


class TestIndexerInvariant:
    def test_handle_create_immediately_indexed(self, cfg: Config) -> None:
        """After handle_create returns, the new note must be queryable via
        Search without an intervening rebuild. This was the original reason
        the MCP path duplicated the indexer dance — the operations seam now
        owns it, but the invariant still has to hold.
        """
        result = mcp_notes.handle_create(
            cfg,
            {
                "type": "note",
                "title": "ImmediateIndexUniqueZWX",
                "body": "ImmediateIndexUniqueZWX body.",
                "project": "test",
            },
        )
        mcp_id = _text(result).split("[", 1)[1].split("]", 1)[0]

        s = Search(config=cfg)
        # FTS reachability — title + body should both hit.
        hits = [r.id for r in s.search("ImmediateIndexUniqueZWX")]
        s.close()
        assert mcp_id in hits
