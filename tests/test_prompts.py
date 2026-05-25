"""Phase 4 E — Prompt primitive integration tests.

Covers:

- ``operations.search.query_prompts`` over project session JSONL buffers.
- ``mem_prompts`` MCP tool wrapper.
- STATE.md "Open Probes" rendering with no probe-tagged notes (only
  classified prompts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.search import query_prompts
from personal_mem.synthesis.landing import state_of_play


def _write_events(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    return Config(vault_root=vault)


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


class TestQueryPrompts:
    def test_archived_session_events(self, cfg: Config, vault: VaultManager):
        # Layout: vault/projects/proj-a/sessions/ses-1/events.jsonl
        sess_dir = cfg.vault_root / "projects" / "proj-a" / "sessions" / "ses-1"
        _write_events(
            sess_dir / "events.jsonl",
            [
                {"type": "prompt", "text": "What's in the indexer?",
                 "session_id": "cc-1", "ts": "2026-05-02T15:00:00+00:00"},
                {"type": "prompt", "text": "Why does FTS skip MD?",
                 "session_id": "cc-1", "ts": "2026-05-02T15:05:00+00:00"},
                {"tool": "Edit", "file": "main.py",
                 "ts": "2026-05-02T15:06:00+00:00"},
            ],
        )

        rows = query_prompts(cfg, project="proj-a")
        assert len(rows) == 2
        # Recency-sorted desc
        assert rows[0]["text"] == "Why does FTS skip MD?"
        assert rows[1]["text"] == "What's in the indexer?"
        assert all(r["session_id"] == "cc-1" for r in rows)

    def test_since_filter(self, cfg: Config, vault: VaultManager):
        sess_dir = cfg.vault_root / "projects" / "proj-a" / "sessions" / "ses-1"
        _write_events(
            sess_dir / "events.jsonl",
            [
                {"type": "prompt", "text": "old prompt",
                 "session_id": "cc-1", "ts": "2026-04-01T10:00:00+00:00"},
                {"type": "prompt", "text": "new prompt",
                 "session_id": "cc-1", "ts": "2026-05-01T10:00:00+00:00"},
            ],
        )

        rows = query_prompts(cfg, project="proj-a", since="2026-04-15")
        assert len(rows) == 1
        assert rows[0]["text"] == "new prompt"

    def test_limit(self, cfg: Config, vault: VaultManager):
        sess_dir = cfg.vault_root / "projects" / "proj-a" / "sessions" / "ses-1"
        rows_in = [
            {"type": "prompt", "text": f"prompt {i}",
             "session_id": "cc-1", "ts": f"2026-05-0{i % 9 + 1}T15:00:00+00:00"}
            for i in range(20)
        ]
        _write_events(sess_dir / "events.jsonl", rows_in)
        rows = query_prompts(cfg, project="proj-a", limit=5)
        assert len(rows) == 5

    def test_active_buffer_killed_mid_session(
        self, cfg: Config, vault: VaultManager
    ):
        """E2E from the verification gate: simulate a buffer being killed
        mid-session (no events.jsonl archive yet) and verify mem_prompts
        still surfaces prior prompts."""
        # Create a session note that maps cc-uuid → proj-a so the buffer
        # gets project-scoped correctly.
        vault.create_note(
            note_type=NoteType.SESSION,
            title="Session 1",
            project="proj-a",
            extra_frontmatter={"source_session": "cc-uuid-mid"},
        )

        # Active buffer (not yet archived)
        buf_file = cfg.mem_dir / "buffer" / "cc-uuid-mid.jsonl"
        _write_events(
            buf_file,
            [
                {"type": "prompt", "text": "mid-session prompt",
                 "session_id": "cc-uuid-mid",
                 "ts": "2026-05-02T15:00:00+00:00"},
            ],
        )

        rows = query_prompts(cfg, project="proj-a")
        texts = [r["text"] for r in rows]
        assert "mid-session prompt" in texts

    def test_no_project_returns_empty(self, cfg: Config):
        assert query_prompts(cfg, project="") == []

    def test_unknown_project_returns_empty(self, cfg: Config):
        assert query_prompts(cfg, project="ghost") == []

    def test_active_buffer_other_project_excluded(
        self, cfg: Config, vault: VaultManager
    ):
        """A buffer whose session note maps to another project must not
        leak prompts into this query."""
        vault.create_note(
            note_type=NoteType.SESSION,
            title="Other session",
            project="proj-b",
            extra_frontmatter={"source_session": "cc-otherproj"},
        )
        buf_file = cfg.mem_dir / "buffer" / "cc-otherproj.jsonl"
        _write_events(
            buf_file,
            [
                {"type": "prompt", "text": "other project prompt",
                 "session_id": "cc-otherproj",
                 "ts": "2026-05-02T15:00:00+00:00"},
            ],
        )
        rows = query_prompts(cfg, project="proj-a")
        assert all(r["text"] != "other project prompt" for r in rows)


class TestStateOpenProbes:
    """STATE.md 'Open Probes' must populate from prompts even when no
    `probe`-tagged notes exist."""

    def test_open_probes_from_prompts_only(self, cfg: Config, vault: VaultManager):
        sess_dir = cfg.vault_root / "projects" / "proj-a" / "sessions" / "ses-1"
        sess_dir.mkdir(parents=True, exist_ok=True)
        _write_events(
            sess_dir / "events.jsonl",
            [
                {"type": "prompt",
                 "text": "How does the indexer detect duplicates?",
                 "session_id": "cc-1",
                 "ts": "2026-05-02T15:00:00+00:00"},
                # No follow-up Edit/Write → classifies as probe
            ],
        )

        # Need a session note + index for state_of_play to load decisions/
        # probes paths cleanly.
        vault.create_note(
            note_type=NoteType.SESSION,
            title="Session 1",
            project="proj-a",
            extra_frontmatter={"source_session": "cc-1"},
        )
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()

        rendered = state_of_play(cfg, "proj-a")

        assert "Open Probes" in rendered
        assert "How does the indexer detect duplicates?" in rendered
        assert "*prompt*" in rendered

    def test_renders_when_probes_empty(self, cfg: Config, vault: VaultManager):
        """No prompt buffers, no probe notes — section must be omitted, not crash."""
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        out = state_of_play(cfg, "proj-a")
        # Just verify we don't render an empty 'Open Probes' header.
        if "Open Probes" in out:
            # If present, it must actually contain a bullet
            section = out.split("Open Probes", 1)[1]
            assert "- " in section
