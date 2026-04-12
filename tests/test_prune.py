"""Tests for orphan session cleanup (src/personal_mem/prune.py)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_mem.config import Config
from personal_mem.prune import (
    EVENTS_MIN_BYTES,
    ORPHAN_MIN_AGE_SECONDS,
    PruneResult,
    find_orphans,
    is_orphan,
    iter_session_folders,
    prune_orphans,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    d = tmp_path / "vault"
    (d / ".mem").mkdir(parents=True)
    (d / "projects").mkdir()
    return d


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


def _make_session(
    vault_dir: Path,
    project: str,
    session_id: str,
    *,
    processed_at: str = "2026-04-05",
    files_touched: list[str] | None = None,
    commits: list[dict] | None = None,
    source_session: str = "",
    event_bytes: int | None = 0,
    derived_notes: list[str] | None = None,
) -> Path:
    """Create a session folder with controllable properties.

    Returns the session directory path.
    """
    sessions_dir = vault_dir / "projects" / project / "sessions"
    session_dir = sessions_dir / f"{session_id}-{processed_at}"
    session_dir.mkdir(parents=True, exist_ok=True)

    fm_lines = [
        "---",
        "type: session",
        f"id: {session_id}",
        f"date: {processed_at}",
        f"project: {project}",
        f"processed_at: {processed_at}",
    ]
    if files_touched:
        fm_lines.append("files_touched:")
        for f in files_touched:
            fm_lines.append(f"  - {f}")
    if commits:
        fm_lines.append("commits:")
        for c in commits:
            fm_lines.append(f"  - {c}")
    if source_session:
        fm_lines.append(f"source_session: {source_session}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(f"# Session {session_id}")
    fm_lines.append("")
    fm_lines.append("## Summary")
    fm_lines.append("Stub session.")

    (session_dir / "session.md").write_text(
        "\n".join(fm_lines) + "\n", encoding="utf-8"
    )

    if event_bytes:
        (session_dir / "events.jsonl").write_bytes(b"x" * event_bytes)

    if derived_notes:
        for name in derived_notes:
            (session_dir / name).write_text(
                "---\ntype: note\n---\n\n# Derived\nBody", encoding="utf-8"
            )

    return session_dir


# ---------------------------------------------------------------------------
# is_orphan — condition truth table
# ---------------------------------------------------------------------------


class TestIsOrphan:
    def test_empty_recent_session_is_orphan(self, vault_dir: Path):
        session = _make_session(vault_dir, "alpha", "ses-empty")
        # Force age > 1h by backdating mtime
        old_time = time.time() - 7200
        import os

        os.utime(session, (old_time, old_time))
        assert is_orphan(session)

    def test_session_with_derived_note_is_not_orphan(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-has-note",
            derived_notes=["derived-insight.md"],
        )
        assert not is_orphan(session, min_age_seconds=0)

    def test_session_with_big_events_jsonl_is_not_orphan(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-events",
            event_bytes=EVENTS_MIN_BYTES + 100,
        )
        assert not is_orphan(session, min_age_seconds=0)

    def test_session_with_tiny_events_jsonl_still_orphan(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-small-events",
            event_bytes=EVENTS_MIN_BYTES - 50,
        )
        assert is_orphan(session, min_age_seconds=0)

    def test_session_with_files_touched_is_not_orphan(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-touched",
            files_touched=["src/real.py"],
        )
        assert not is_orphan(session, min_age_seconds=0)

    def test_session_with_commits_is_not_orphan(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-committed",
            commits=["abc123 Fix stuff"],
        )
        assert not is_orphan(session, min_age_seconds=0)

    def test_too_recent_session_is_not_orphan(self, vault_dir: Path):
        # Full ISO timestamp for "right now" — age < 1h
        now_iso = datetime.now(timezone.utc).isoformat()
        session = _make_session(vault_dir, "alpha", "ses-fresh", processed_at=now_iso)
        assert not is_orphan(session)

    def test_current_wrap_session_is_protected(self, vault_dir: Path):
        session = _make_session(
            vault_dir,
            "alpha",
            "ses-wrap",
            source_session="cc-current",
        )
        # Even when all other conditions hold, current session is excluded
        assert not is_orphan(
            session, current_session_id="cc-current", min_age_seconds=0
        )

    def test_non_session_folder_ignored(self, vault_dir: Path):
        fake = vault_dir / "projects" / "alpha" / "sessions" / "misc"
        fake.mkdir(parents=True)
        (fake / "standalone-note.md").write_text("---\ntype: note\n---\n\nBody\n")
        # Not a real session (no session.md) — not an orphan
        assert not is_orphan(fake, min_age_seconds=0)

    def test_missing_folder_returns_false(self, tmp_path: Path):
        assert not is_orphan(tmp_path / "nonexistent", min_age_seconds=0)


# ---------------------------------------------------------------------------
# iter_session_folders + find_orphans
# ---------------------------------------------------------------------------


class TestFindOrphans:
    def test_iter_skips_misc_folder(self, config: Config, vault_dir: Path):
        _make_session(vault_dir, "alpha", "ses-1")
        misc = vault_dir / "projects" / "alpha" / "sessions" / "misc"
        misc.mkdir(parents=True)
        (misc / "standalone.md").write_text("---\ntype: note\n---\n\nBody\n")

        folders = list(iter_session_folders(config))
        names = [f.name for f in folders]
        assert any("ses-1" in n for n in names)
        assert "misc" not in names

    def test_find_orphans_respects_project_filter(
        self, config: Config, vault_dir: Path
    ):
        old = time.time() - 7200
        s1 = _make_session(vault_dir, "alpha", "ses-a")
        s2 = _make_session(vault_dir, "beta", "ses-b")
        import os

        os.utime(s1, (old, old))
        os.utime(s2, (old, old))

        alpha_orphans = find_orphans(config, project="alpha")
        assert len(alpha_orphans) == 1
        assert alpha_orphans[0].name.startswith("ses-a")

    def test_find_orphans_all_projects(self, config: Config, vault_dir: Path):
        old = time.time() - 7200
        import os

        s1 = _make_session(vault_dir, "alpha", "ses-a")
        s2 = _make_session(vault_dir, "beta", "ses-b")
        os.utime(s1, (old, old))
        os.utime(s2, (old, old))

        all_orphans = find_orphans(config)
        assert len(all_orphans) == 2

    def test_find_orphans_skips_substantive_sessions(
        self, config: Config, vault_dir: Path
    ):
        _make_session(
            vault_dir, "alpha", "ses-real", files_touched=["src/real.py"]
        )
        _make_session(vault_dir, "alpha", "ses-stub")
        # Force age
        import os

        for s in iter_session_folders(config):
            os.utime(s, (time.time() - 7200, time.time() - 7200))

        orphans = find_orphans(config)
        assert len(orphans) == 1
        assert orphans[0].name.startswith("ses-stub")


# ---------------------------------------------------------------------------
# prune_orphans — actual deletion
# ---------------------------------------------------------------------------


class TestPruneOrphans:
    def test_dry_run_does_not_delete(self, config: Config, vault_dir: Path):
        s = _make_session(vault_dir, "alpha", "ses-stub")
        import os

        os.utime(s, (time.time() - 7200, time.time() - 7200))

        orphans = find_orphans(config)
        result = prune_orphans(orphans, dry_run=True)

        assert isinstance(result, PruneResult)
        assert result.deleted == 1  # reported as would-delete
        assert s.exists()  # actually still there

    def test_actual_delete_removes_folder(self, config: Config, vault_dir: Path):
        s = _make_session(vault_dir, "alpha", "ses-stub")
        import os

        os.utime(s, (time.time() - 7200, time.time() - 7200))

        orphans = find_orphans(config)
        result = prune_orphans(orphans, dry_run=False)

        assert result.deleted == 1
        assert not s.exists()

    def test_reports_freed_bytes(self, config: Config, vault_dir: Path):
        s = _make_session(vault_dir, "alpha", "ses-stub", event_bytes=200)
        import os

        os.utime(s, (time.time() - 7200, time.time() - 7200))

        orphans = find_orphans(config)
        # session.md (~200 bytes) + events.jsonl (200 bytes) ≈ 400+
        result = prune_orphans(orphans, dry_run=True)
        assert result.freed_bytes > 200

    def test_empty_orphan_list_is_no_op(self):
        result = prune_orphans([], dry_run=False)
        assert result.deleted == 0
        assert result.freed_bytes == 0
        assert result.paths == []
