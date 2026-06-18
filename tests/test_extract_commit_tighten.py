"""Tests for the B8 tighten — `accepted` status now requires `commit_refs`.

Before B8 (2026-05-29): `outcome=committed` unconditionally flipped status
to `accepted`, even when no session commits matched the decision's
file_paths. Empirically this produced 228/346 (66%) accepted decisions
with no commit_refs — the badge was not load-bearing.

After B8: every decision lands as `proposed`; the up-flip to `accepted`
fires only when the commit_refs match pass finds at least one matching
hash on the session. The user-asserted outcome remains visible via the
`committed: bool` field. New `accepted` decisions are guaranteed to
carry evidence.

These tests pin the four scenarios:

- committed + matching commits   → accepted with commit_refs (the happy path)
- committed + no session commits → proposed, commit_refs absent
- committed + non-matching files → proposed, commit_refs absent
- abandoned                       → proposed (unchanged from pre-B8)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.operations.extract import extract_session


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _make_session_with_commits(
    vault: VaultManager, *, commits: list[dict]
) -> tuple[str, Path]:
    """Pre-create a session note whose frontmatter carries ``commits[]``.

    Returns ``(session_id, session_path)``. The session_id is the ``id:``
    in the freshly-minted frontmatter — what ``extract_session`` matches on.
    """
    path = vault.create_note(
        note_type=NoteType.SESSION,
        title="t",
        body="## Events\n",
        project="t",
        extra_frontmatter={"commits": commits},
    )
    note = vault.read_note(path)
    return note.id, path


class TestCommitTighten:
    """B8 — accepted status requires commit_refs evidence."""

    def test_matching_commit_yields_accepted_with_commit_refs(
        self, config: Config, vault: VaultManager
    ):
        # outcome=committed + session commits that touch the decision's files
        # → status flips to accepted AND commit_refs is populated.
        sess_id, _ = _make_session_with_commits(
            vault,
            commits=[
                {"hash": "abc123", "files": ["src/foo.py", "src/bar.py"], "message": "x"},
            ],
        )
        _index(config)

        out = extract_session(
            config,
            session_id=sess_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Refactor foo",
                "rationale": "## Decision\nRefactored foo.\n",
                "outcome": "committed",
                "file_paths": ["src/foo.py"],
            }],
        )
        assert out.error == ""
        assert len(out.created_decisions) == 1
        dec_id = out.created_decisions[0].id

        # Re-read the on-disk note — extract performs an update_note pass
        # to write commit_refs + status flip; created_decisions[0] is the
        # pre-flip snapshot.
        from thinkweave.retrieval.search import Search
        s = Search(config=config)
        row = s.get_note_by_id(dec_id)
        s.close()
        from thinkweave.core.vault import parse_frontmatter
        fm, _ = parse_frontmatter(
            (config.vault_root / row["path"]).read_text(encoding="utf-8")
        )
        assert fm["status"] == "accepted"
        assert fm["commit_refs"] == ["abc123"]
        assert fm["committed"] is True

    def test_committed_without_session_commits_stays_proposed(
        self, config: Config, vault: VaultManager
    ):
        # outcome=committed but session has no commits[] → status stays
        # proposed; commit_refs absent.
        sess_id, _ = _make_session_with_commits(vault, commits=[])
        _index(config)

        out = extract_session(
            config,
            session_id=sess_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Refactor foo",
                "rationale": "## Decision\nRefactored foo.\n",
                "outcome": "committed",
                "file_paths": ["src/foo.py"],
            }],
        )
        assert len(out.created_decisions) == 1
        fm = out.created_decisions[0].frontmatter
        assert fm["status"] == "proposed"
        assert "commit_refs" not in fm
        # User-asserted classification still recorded.
        assert fm["committed"] is True

    def test_committed_with_non_matching_files_stays_proposed(
        self, config: Config, vault: VaultManager
    ):
        # outcome=committed and session has commits, but the decision's
        # file_paths don't intersect any commit's files → stays proposed.
        sess_id, _ = _make_session_with_commits(
            vault,
            commits=[
                {"hash": "xyz789", "files": ["other.py"], "message": "y"},
            ],
        )
        _index(config)

        out = extract_session(
            config,
            session_id=sess_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Refactor foo",
                "rationale": "## Decision\nRefactored foo.\n",
                "outcome": "committed",
                "file_paths": ["src/foo.py"],
            }],
        )
        # Re-read on-disk to confirm no commit_refs got written.
        from thinkweave.retrieval.search import Search
        from thinkweave.core.vault import parse_frontmatter

        dec_id = out.created_decisions[0].id
        s = Search(config=config)
        row = s.get_note_by_id(dec_id)
        s.close()
        fm, _ = parse_frontmatter(
            (config.vault_root / row["path"]).read_text(encoding="utf-8")
        )
        assert fm["status"] == "proposed"
        assert "commit_refs" not in fm

    def test_abandoned_outcome_stays_proposed_unchanged(
        self, config: Config, vault: VaultManager
    ):
        # outcome=abandoned: status=proposed, committed=False. This was true
        # before B8 too; pinning it so the tighten doesn't accidentally
        # flip non-committed decisions.
        sess_id, _ = _make_session_with_commits(vault, commits=[])
        _index(config)

        out = extract_session(
            config,
            session_id=sess_id,
            project="t",
            summary="ok",
            insights=[],
            decisions=[{
                "title": "Tried approach X",
                "rationale": "## Decision\nDidn't work.\n",
                "outcome": "abandoned",
                "file_paths": ["src/foo.py"],
            }],
        )
        fm = out.created_decisions[0].frontmatter
        assert fm["status"] == "proposed"
        assert fm["committed"] is False
        assert "commit_refs" not in fm
