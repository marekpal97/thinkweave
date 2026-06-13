"""Tests for vault operations — CRUD, frontmatter parsing, wikilinks."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.schemas import DecisionStatus, NoteType
from personal_mem.core.vault import (
    VaultManager,
    content_hash,
    extract_wikilinks,
    parse_frontmatter,
    render_frontmatter,
    strip_section,
)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def vault(vault_dir: Path) -> VaultManager:
    cfg = Config(vault_root=vault_dir)
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


# --- Frontmatter parsing ---


class TestParseFrontmatter:
    def test_basic_kv(self):
        text = "---\ntype: note\nid: n-abc123\ndate: 2026-03-30\n---\n\n# Hello"
        fm, body = parse_frontmatter(text)
        assert fm["type"] == "note"
        assert fm["id"] == "n-abc123"
        assert fm["date"] == "2026-03-30"
        assert "# Hello" in body

    def test_inline_list(self):
        text = "---\ntags: [python, sqlite, gotcha]\n---\n\nBody"
        fm, body = parse_frontmatter(text)
        assert fm["tags"] == ["python", "sqlite", "gotcha"]

    def test_block_list(self):
        text = "---\nfiles_touched:\n  - foo/bar.py\n  - baz/qux.py\n---\n\nBody"
        fm, body = parse_frontmatter(text)
        assert fm["files_touched"] == ["foo/bar.py", "baz/qux.py"]

    def test_empty_list(self):
        text = "---\ntags: []\n---\n\nBody"
        fm, body = parse_frontmatter(text)
        assert fm["tags"] == []

    def test_boolean(self):
        text = "---\nactive: true\narchived: false\n---\n\nBody"
        fm, body = parse_frontmatter(text)
        assert fm["active"] is True
        assert fm["archived"] is False

    def test_numeric(self):
        text = "---\nconfidence: 0.85\ncount: 42\n---\n\nBody"
        fm, body = parse_frontmatter(text)
        assert fm["confidence"] == 0.85
        assert fm["count"] == 42

    def test_no_frontmatter(self):
        text = "# Just a heading\n\nSome text."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert "Just a heading" in body

    def test_quoted_string(self):
        text = '---\ntitle: "Lex Fridman #432"\n---\n\nBody'
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "Lex Fridman #432"


class TestRenderFrontmatter:
    def test_roundtrip(self):
        data = {
            "type": "note",
            "id": "n-abc123",
            "tags": ["python", "sqlite"],
            "date": "2026-03-30",
        }
        rendered = render_frontmatter(data)
        assert rendered.startswith("---")
        assert rendered.endswith("---")
        # Parse it back
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert fm["type"] == "note"
        assert fm["tags"] == ["python", "sqlite"]

    def test_skips_none_and_empty(self):
        data = {"type": "note", "empty": "", "none_val": None, "tags": ["a"]}
        rendered = render_frontmatter(data)
        assert "empty" not in rendered
        assert "none_val" not in rendered

    def test_dict_values(self):
        data = {"context": {"prompt": "do something", "plan": "dec-123"}}
        rendered = render_frontmatter(data)
        assert "prompt: do something" in rendered
        assert "plan: dec-123" in rendered

    def test_list_of_dicts_roundtrip(self):
        data = {
            "id": "dec-test",
            "prediction_history": [
                {"match": "pending", "judged_at": "2026-05-25T16:21Z", "reason": "awaiting evidence"},
                {"match": "confirmed", "judged_at": "2026-05-26T08:00Z", "reason": "drain produced 3/3 accepted"},
            ],
        }
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert isinstance(fm["prediction_history"], list)
        assert all(isinstance(e, dict) for e in fm["prediction_history"])
        assert fm["prediction_history"] == data["prediction_history"]

    def test_list_of_dicts_uses_json_format(self):
        data = {"entries": [{"k": "v", "n": 1}]}
        rendered = render_frontmatter(data)
        assert '- {"k": "v", "n": 1}' in rendered

    def test_list_field_string_literal_coerced(self):
        """Writer-subagent regression: a JSON-shaped string passed for a
        list-shaped field (``proposed_concepts``) must coerce to a real
        list, not get iterated character-by-character downstream.
        """
        data = {"id": "src-test", "proposed_concepts": "[liqudty]"}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert isinstance(fm["proposed_concepts"], list)
        assert fm["proposed_concepts"] == ["liqudty"]
        # And critically, NOT char-iterated
        assert fm["proposed_concepts"] != ["[", "l", "i", "q", "u", "d", "t", "y", "]"]

    def test_list_field_quoted_json_list_coerced(self):
        """Stringified JSON list with quoted elements parses cleanly."""
        data = {"id": "src-test", "concepts": '["llm", "ai-governance"]'}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert isinstance(fm["concepts"], list)
        assert fm["concepts"] == ["llm", "ai-governance"]

    def test_list_field_bare_scalar_wraps(self):
        """A bare scalar for a list field becomes a single-element list."""
        data = {"id": "src-test", "tags": "news"}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert fm["tags"] == ["news"]

    def test_list_field_char_iterated_damage_reconstructed(self):
        """If an upstream layer already char-iterated the value (the actual
        damage shape found in src-682d7b64, src-8208cf66, etc.), the
        render-time coercion stitches the chars back into a single string
        and re-parses. The reconstruction is best-effort but always
        produces a sane list rather than letting char damage propagate.
        """
        data = {
            "id": "src-test",
            "proposed_concepts": ["[", "l", "i", "q", "u", "d", "t", "y", "]"],
        }
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert fm["proposed_concepts"] == ["liqudty"]

    def test_short_legit_list_not_misclassified_as_damage(self):
        """Char-damage detection must not fire on legitimate short lists
        even when items happen to be short — only triggers when ALL
        items are length-1 AND total >3 elements."""
        # Three single-char items: still legit (below threshold)
        data = {"id": "n-test", "aliases": ["a", "b", "c"]}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert fm["aliases"] == ["a", "b", "c"]

    def test_list_field_already_a_list_passes_through(self):
        """The normal case — a proper list of strings — is unchanged."""
        data = {"id": "src-test", "concepts": ["llm", "ai-governance", "ai-ethics"]}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        assert fm["concepts"] == ["llm", "ai-governance", "ai-ethics"]

    def test_non_list_field_string_left_alone(self):
        """Coercion only fires for keys in ``LIST_FRONTMATTER_KEYS``.
        Scalar fields (title, url, status) keep their string form."""
        data = {"id": "src-test", "title": "[Some Title]", "url": "https://x.y"}
        rendered = render_frontmatter(data)
        fm, _ = parse_frontmatter(rendered + "\n\nBody")
        # Title contains brackets but is NOT a list field — it must stay a string
        assert isinstance(fm["title"], str)
        assert fm["url"] == "https://x.y"


# --- Wikilinks ---


class TestWikilinks:
    def test_extract_basic(self):
        text = "See [[legacy_proj]] and [[sqlite-wal]] for details."
        links = extract_wikilinks(text)
        assert links == ["legacy_proj", "sqlite-wal"]

    def test_extract_with_alias(self):
        text = "Check [[legacy_proj|Legacy Project]] docs."
        links = extract_wikilinks(text)
        assert links == ["legacy_proj"]

    def test_no_links(self):
        assert extract_wikilinks("No links here.") == []


class TestWikilinkIds:
    """``extract_wikilink_ids`` recovers a note id whether the link is bare
    (``[[id]]``) or path-based (``[[path|id]]``). Load-bearing: edge inference
    and the RLVR citation substrate route through it, so the bare→path body
    migration must not drop edges or citations.
    """

    def test_bare_id(self):
        from personal_mem.core.vault import extract_wikilink_ids

        assert extract_wikilink_ids("see [[dec-9988aaff]]") == ["dec-9988aaff"]

    def test_path_based_id_from_display(self):
        from personal_mem.core.vault import extract_wikilink_ids

        ids = extract_wikilink_ids("see [[notes/foo/bar|n-abc123ef]]")
        assert ids == ["n-abc123ef"]

    def test_title_link_returns_raw_target(self):
        from personal_mem.core.vault import extract_wikilink_ids

        # Not id-shaped on either side — caller (e.g. RLVR) filters it out.
        assert extract_wikilink_ids("see [[Some Title]]") == ["Some Title"]

    def test_mixed(self):
        from personal_mem.core.vault import extract_wikilink_ids

        body = "[[notes/x|n-aaaaaa11]] then [[dec-bbbbbb22]] and [[concepts/finance|Finance]]"
        ids = extract_wikilink_ids(body)
        assert ids == ["n-aaaaaa11", "dec-bbbbbb22", "concepts/finance"]


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_content(self):
        assert content_hash("hello") != content_hash("world")


# --- VaultManager ---


class TestVaultManager:
    def test_ensure_dirs(self, vault: VaultManager):
        assert (vault.root / ".mem").is_dir()
        assert (vault.root / "projects").is_dir()
        assert (vault.root / "daily").is_dir()
        assert (vault.root / "sources").is_dir()
        assert (vault.root / "templates").is_dir()

    def test_generate_id(self, vault: VaultManager):
        nid = vault.generate_id(NoteType.NOTE)
        assert nid.startswith("n-")
        assert len(nid) == 10  # "n-" + 8 hex chars

        sid = vault.generate_id(NoteType.SESSION)
        assert sid.startswith("ses-")

        did = vault.generate_id(NoteType.DECISION)
        assert did.startswith("dec-")

        src_id = vault.generate_id(NoteType.SOURCE)
        assert src_id.startswith("src-")

    def test_create_note(self, vault: VaultManager):
        path = vault.create_note(
            NoteType.NOTE,
            "SQLite WAL Gotcha",
            body="WAL mode requires exclusive lock for checkpointing.",
            tags=["gotcha", "sqlite"],
            project="personal_mem",
        )
        assert path.exists()
        assert path.suffix == ".md"

        # Read it back
        note = vault.read_note(path)
        assert note.type == NoteType.NOTE
        assert note.title == "SQLite WAL Gotcha"
        assert "gotcha" in note.tags
        assert "sqlite" in note.tags
        assert note.project == "personal_mem"
        assert "exclusive lock" in note.body

    def test_create_note_normalizes_project(self, vault: VaultManager):
        """A dash/case project name is canonicalized so `trade-ideas` and
        `trade_ideas` can never become two separate project folders."""
        path = vault.create_note(
            NoteType.NOTE, "X", project="Trade-Ideas",
        )
        assert "projects/trade_ideas/" in str(path).replace("\\", "/")
        assert "trade-ideas" not in str(path).lower()
        note = vault.read_note(path)
        assert note.project == "trade_ideas"

    def test_create_session(self, vault: VaultManager):
        path = vault.create_note(
            NoteType.SESSION,
            "DAG refactor",
            project="legacy_proj",
            extra_frontmatter={"source_session": "abc-123"},
        )
        assert path.exists()
        note = vault.read_note(path)
        assert note.type == NoteType.SESSION
        assert note.frontmatter.get("source_session") == "abc-123"
        assert note.frontmatter.get("files_touched") == []

    def test_create_decision(self, vault: VaultManager):
        path = vault.create_note(
            NoteType.DECISION,
            "Use markdown-first storage",
            body="## Context\nNeed portable storage.\n\n## Decision\nMarkdown + SQLite.",
            project="personal_mem",
            tags=["architecture"],
        )
        note = vault.read_note(path)
        assert note.type == NoteType.DECISION
        assert note.frontmatter.get("status") == "proposed"

    def test_create_source(self, vault: VaultManager):
        path = vault.create_note(
            NoteType.SOURCE,
            "Lex Fridman #432",
            body="## Key takeaways\n- Value alignment matters",
            extra_frontmatter={
                "source_type": "podcast",
                "url": "https://example.com",
                "authors": ["Lex Fridman"],
            },
        )
        note = vault.read_note(path)
        assert note.type == NoteType.SOURCE
        assert note.frontmatter.get("source_type") == "podcast"
        # Source notes live in a subdirectory as source.md
        assert path.name == "source.md"
        assert path.parent.name == "lex-fridman-432"

    def test_update_note_frontmatter(self, vault: VaultManager):
        path = vault.create_note(NoteType.NOTE, "Test Note", tags=["a"])
        vault.update_note(path, frontmatter_updates={"tags": ["b", "c"]})
        note = vault.read_note(path)
        assert "a" in note.tags
        assert "b" in note.tags
        assert "c" in note.tags

    def test_update_note_body_append(self, vault: VaultManager):
        path = vault.create_note(NoteType.NOTE, "Test Note", body="Line 1.")
        vault.update_note(path, body_append="Line 2.")
        note = vault.read_note(path)
        assert "Line 1." in note.body
        assert "Line 2." in note.body

    def test_update_note_list_of_dicts_no_typeerror(self, vault: VaultManager):
        """Regression: dedupe path on a list-of-dicts (e.g. prediction_history)
        must not raise ``TypeError: unhashable type: 'dict'``.

        Pre-fix, the merge branch did ``existing = set(fm[key])`` blindly,
        which exploded when ``fm[key]`` held dicts. The fix catches the
        TypeError and falls through to a replace-with-new-value branch.
        """
        path = vault.create_note(
            NoteType.DECISION,
            "Test Decision",
            body="## Context\n\n## Decision\n",
            extra_frontmatter={
                "prediction_history": [
                    {
                        "match": "pending",
                        "judged_at": "2026-05-25T16:21Z",
                        "reason": "awaiting evidence",
                    }
                ]
            },
        )
        # Trigger the merge branch: both old and new are lists.
        new_history = [
            {
                "match": "pending",
                "judged_at": "2026-05-25T16:21Z",
                "reason": "awaiting evidence",
            },
            {
                "match": "confirmed",
                "judged_at": "2026-05-26T08:00Z",
                "reason": "drain produced 3/3 accepted",
            },
        ]
        # Pre-fix this raised TypeError before write; post-fix it should
        # silently fall through to the replace branch.
        vault.update_note(
            path, frontmatter_updates={"prediction_history": new_history}
        )
        note = vault.read_note(path)
        # Fallback path replaces with the incoming value wholesale — the
        # merged shape is exactly ``new_history`` (2 dicts), not the old
        # 1-dict list.
        assert isinstance(note.frontmatter["prediction_history"], list)
        assert len(note.frontmatter["prediction_history"]) == 2
        assert note.frontmatter["prediction_history"] == new_history

    def test_list_notes(self, vault: VaultManager):
        vault.create_note(NoteType.NOTE, "Note A", project="proj1", tags=["x"])
        vault.create_note(NoteType.NOTE, "Note B", project="proj2", tags=["y"])
        vault.create_note(NoteType.SESSION, "Session", project="proj1")

        all_notes = vault.list_notes()
        assert len(all_notes) == 3

        proj1_notes = vault.list_notes(project="proj1")
        assert len(proj1_notes) == 2

        sessions = vault.list_notes(note_type=NoteType.SESSION)
        assert len(sessions) == 1

        tagged = vault.list_notes(tags=["x"])
        assert len(tagged) == 1

    def test_filename_collision(self, vault: VaultManager):
        p1 = vault.create_note(NoteType.NOTE, "Same Title", project="test")
        p2 = vault.create_note(NoteType.NOTE, "Same Title", project="test")
        assert p1 != p2
        assert p1.exists()
        assert p2.exists()

    def test_sanitize_filename(self, vault: VaultManager):
        assert vault._sanitize_filename("Hello World!") == "hello-world"
        assert vault._sanitize_filename("foo/bar:baz") == "foobarbaz"
        assert vault._sanitize_filename("") == "untitled"

    def test_source_global_default(self, vault: VaultManager):
        """Sources without project go to vault/sources/{slug}/source.md."""
        path = vault.create_note(NoteType.SOURCE, "Global Article")
        assert "sources" in str(path)
        assert "projects" not in str(path)
        assert path.name == "source.md"
        assert path.parent.name == "global-article"

    def test_source_global_despite_project(self, vault: VaultManager):
        """Sources are global: a `project:` never routes them under projects/.

        Source notes file strictly by the registry bucket under
        vault/sources/<bucket>/, exactly like themes. The project frontmatter
        is informational only.
        """
        path = vault.create_note(
            NoteType.SOURCE, "ML Paper",
            project="ml_study",
            extra_frontmatter={"source_type": "paper", "url": "https://arxiv.org/123"},
        )
        assert "projects" not in str(path)
        assert "sources/papers" in str(path)
        assert path.name == "source.md"
        assert path.parent.name == "ml-paper"
        assert path.exists()

    def test_source_collision(self, vault: VaultManager):
        """Duplicate source titles get incrementing subdirectory names."""
        p1 = vault.create_note(NoteType.SOURCE, "Same Source")
        p2 = vault.create_note(NoteType.SOURCE, "Same Source")
        assert p1 != p2
        assert p1.exists() and p2.exists()
        assert p1.parent.name == "same-source"
        assert p2.parent.name == "same-source-1"


class TestGetAllMdFiles:
    def test_underscore_archive_excluded(self, vault: VaultManager):
        """``concepts/topics/_archive/`` (merged/demoted hubs) must not be
        swept up by the index scan — it's the underscore analog of
        ``.archive`` (see synthesis.concepts.HUB_ARCHIVE_DIRNAME)."""
        topics = vault.root / "concepts" / "topics"
        (topics / "_archive").mkdir(parents=True, exist_ok=True)
        live = topics / "live-hub.md"
        live.write_text("---\ntype: concept-hub\n---\n", encoding="utf-8")
        archived = topics / "_archive" / "dead-hub.md"
        archived.write_text("---\ntype: concept-hub\n---\n", encoding="utf-8")

        files = vault.get_all_md_files()
        assert live in files
        assert archived not in files


class TestStripSection:
    def test_strip_events_section(self):
        body = "# Title\n\n## Events\n- 12:00 Edit foo.py\n- 12:01 Bash cmd\n\n## Summary\nDone.\n"
        result = strip_section(body, "## Events")
        assert "## Events" not in result
        assert "12:00 Edit" not in result
        assert "## Summary" in result
        assert "Done." in result

    def test_strip_last_section(self):
        body = "# Title\n\n## Summary\nDone.\n\n## Events\n- 12:00 Edit foo.py\n"
        result = strip_section(body, "## Events")
        assert "## Events" not in result
        assert "## Summary" in result

    def test_strip_missing_section(self):
        body = "# Title\n\nSome content.\n"
        result = strip_section(body, "## Events")
        assert result.strip() == body.strip()

    def test_strip_multiple_sections(self):
        body = "# T\n\n## A\nContent A\n\n## B\nContent B\n\n## C\nContent C\n"
        result = strip_section(body, "## B")
        assert "## A" in result
        assert "Content A" in result
        assert "## B" not in result
        assert "Content B" not in result
        assert "## C" in result
        assert "Content C" in result


class TestDirectoryStructure:
    @pytest.fixture
    def vault(self, tmp_path):
        cfg = Config(vault_root=tmp_path, default_project="proj")
        v = VaultManager(config=cfg)
        v.ensure_dirs()
        return v

    def test_session_gets_subdirectory(self, vault: VaultManager):
        path = vault.create_note(NoteType.SESSION, "My Session", project="proj")
        assert "sessions" in str(path)
        assert path.name == "session.md"
        assert path.parent.name.startswith("ses-")

    def test_note_goes_to_misc_session(self, vault: VaultManager):
        path = vault.create_note(NoteType.NOTE, "My Note", project="proj")
        assert "/sessions/misc/" in str(path)

    def test_decision_goes_to_misc_session(self, vault: VaultManager):
        path = vault.create_note(NoteType.DECISION, "My Decision", project="proj")
        assert "/sessions/misc/" in str(path)

    def test_output_dir_override(self, vault: VaultManager):
        session_path = vault.create_note(NoteType.SESSION, "Sess", project="proj")
        derived = vault.create_note(
            NoteType.NOTE, "Derived", project="proj",
            output_dir=session_path.parent,
        )
        assert derived.parent == session_path.parent
        assert derived.name == "derived.md"

    def test_session_id_routes_to_session_folder(self, vault: VaultManager):
        session_path = vault.create_note(
            NoteType.SESSION, "Target Session", project="proj",
        )
        session_note = vault.read_note(session_path)
        sid = session_note.id

        note_path = vault.create_note(
            NoteType.NOTE, "Attached Note", project="proj",
            session_id=sid,
        )
        assert note_path.parent == session_path.parent

    def test_eager_session_dir_reused_by_session_note(self, vault: VaultManager):
        """A todo created mid-session should share the folder with the later session note."""
        source_uuid = "abc12345-fake-uuid"

        # Mid-session: create a todo with session_id (eagerly creates folder)
        todo_path = vault.create_note(
            NoteType.NOTE, "Fix the widget", project="proj",
            tags=["todo"], session_id=source_uuid,
        )
        eager_dir = todo_path.parent
        assert source_uuid in eager_dir.name
        assert not (eager_dir / "session.md").exists()

        # Later: hook creates the session note with source_session matching the UUID
        session_path = vault.create_note(
            NoteType.SESSION, "Session 2026-04-05", project="proj",
            extra_frontmatter={"source_session": source_uuid},
        )
        # Session note should land in the same folder
        assert session_path.parent == eager_dir
        assert session_path.name == "session.md"

    def test_session_id_finds_by_source_session(self, vault: VaultManager):
        """session_id lookup should find folders via source_session in frontmatter."""
        session_path = vault.create_note(
            NoteType.SESSION, "Existing Session", project="proj",
            extra_frontmatter={"source_session": "real-uuid-here"},
        )
        # Now create a note using the source_session UUID, not the ses-xxx ID
        note_path = vault.create_note(
            NoteType.NOTE, "Late Note", project="proj",
            session_id="real-uuid-here",
        )
        assert note_path.parent == session_path.parent

    def test_misc_dir_created_on_demand(self, vault: VaultManager):
        misc_dir = vault.root / "projects" / "proj" / "sessions" / "misc"
        assert not misc_dir.exists()
        vault.create_note(NoteType.NOTE, "First Standalone", project="proj")
        assert misc_dir.is_dir()

    def test_substack_nests_by_author(self, vault: VaultManager):
        """Substack sources land at sources/substack/<author-slug>/<post-slug>/source.md."""
        path = vault.create_note(
            NoteType.SOURCE,
            "The Curious Case of Disappearing Liquidity",
            extra_frontmatter={
                "source_type": "substack",
                "url": "https://citrini.substack.com/p/curious-case",
                "author": "Citrini Research",
                "publication": "Citrini",
            },
        )
        assert path.name == "source.md"
        assert path.parent.name == "the-curious-case-of-disappearing-liquidity"
        assert path.parent.parent.name == "citrini-research"
        assert path.parent.parent.parent.name == "substack"
        assert "sources/substack/citrini-research" in str(path)

    def test_substack_missing_author_falls_back_flat(self, vault: VaultManager):
        """Substack source without author still works — flat under substack/."""
        path = vault.create_note(
            NoteType.SOURCE,
            "Orphan Post",
            extra_frontmatter={"source_type": "substack", "url": "https://x.substack.com/p/y"},
        )
        assert path.name == "source.md"
        assert path.parent.name == "orphan-post"
        assert path.parent.parent.name == "substack"

    def test_substack_author_slug_sanitized(self, vault: VaultManager):
        """Author names with punctuation/case get sanitized into a safe slug."""
        path = vault.create_note(
            NoteType.SOURCE,
            "Macro Monthly #4",
            extra_frontmatter={
                "source_type": "substack",
                "author": "Alexander Campbell, CFA",
            },
        )
        assert path.parent.parent.name == "alexander-campbell-cfa"

    def test_substack_two_posts_same_author_share_folder(self, vault: VaultManager):
        """Two posts from the same author should cluster under the same author folder."""
        p1 = vault.create_note(
            NoteType.SOURCE,
            "First Citrini Post",
            extra_frontmatter={"source_type": "substack", "author": "Citrini"},
        )
        p2 = vault.create_note(
            NoteType.SOURCE,
            "Second Citrini Post",
            extra_frontmatter={"source_type": "substack", "author": "Citrini"},
        )
        assert p1.parent.parent == p2.parent.parent
        assert p1.parent.parent.name == "citrini"


class TestSeedVaultTemplates:
    """`_seed_vault_templates` must copy every shipped config template into the vault."""

    def test_all_shipped_templates_seeded(self, tmp_path: Path):
        from personal_mem.surfaces.cli.util import _seed_vault_templates

        _seed_vault_templates(tmp_path)
        config_dir = tmp_path / "config"
        for filename in (
            "sources.yaml",
            "news_feeds.yaml",
            "PRIORITIES.yaml",
            "podcast_events_feeds.yaml",
            "podcast_concepts_feeds.yaml",
        ):
            assert (config_dir / filename).exists(), f"{filename} not seeded"
