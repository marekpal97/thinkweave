"""Tests for the Codex rollout importer (``weave import codex``).

Mirrors ``test_claude_code_seed.py`` in intent but tests at the four seams the
importer actually owns:

(a) the ``weave import codex`` CLI entry (argv → parser → handler → stdout),
(b) the parser over hand-written fixture rollout JSONL,
(c) the idempotency manifest (re-run is a no-op),
(d) absent ``~/.codex`` handling.

Plus the two amendments the issue calls out: bounded memory over a rollout with
a huge tool-output line, and compaction-replay dedup.

All expectations are hand-written here; nothing is recomputed by the code under
test. No test touches a real vault or a real ``~/.codex``.
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from thinkweave.acquisition.importers.codex import (
    DEFAULT_CODEX_SESSIONS_ROOT,
    _build_session_body,
    discover_rollouts,
    import_codex,
    parse_rollout,
    rollout_id,
)
from thinkweave.core.config import Config
from thinkweave.core.vault import VaultManager, parse_frontmatter

ROLLOUT_NAME = "rollout-2025-12-04T16-53-57-019aea48-e569-7360-b278-9f0fb5f4e70a.jsonl"
ROLLOUT_ID = "019aea48-e569-7360-b278-9f0fb5f4e70a"


# ── Fixture builders ───────────────────────────────────────────────────


def _meta(cwd: str = "/home/u/projects/thinkmesh", branch: str = "feature/geo") -> dict:
    return {
        "timestamp": "2025-12-04T16:53:57.235Z",
        "type": "session_meta",
        "payload": {
            "id": ROLLOUT_ID,
            "timestamp": "2025-12-04T16:53:57.225Z",
            "cwd": cwd,
            "originator": "codex_vscode",
            "cli_version": "0.61.1-alpha.1",
            "instructions": None,
            "source": "vscode",
            "model_provider": "openai",
            "git": {"commit_hash": "55f92dc", "branch": branch},
        },
    }


def _msg(role: str, text: str, ts: str) -> dict:
    block = "input_text" if role == "user" else "output_text"
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block, "text": text}],
        },
    }


def _env_context(ts: str = "2025-12-04T16:53:57.348Z") -> dict:
    """Codex injects an <environment_context> pseudo-user message per turn."""
    return _msg(
        "user",
        "<environment_context>\n  <cwd>/home/u/projects/thinkmesh</cwd>\n"
        "  <sandbox_mode>workspace-write</sandbox_mode>\n</environment_context>",
        ts,
    )


def _tool_noise(ts: str, output: str = "ok") -> list[dict]:
    return [
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":["bash","-lc","ls"]}',
            },
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": output},
        },
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": 1234}},
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [{"text": "thinking..."}]},
        },
    ]


def _write_rollout(root: Path, events: list[dict], name: str = ROLLOUT_NAME) -> Path:
    day = root / "2025" / "12" / "04"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return path


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    root = tmp_path / "codex" / "sessions"
    root.mkdir(parents=True)
    return root


def _simple_events() -> list[dict]:
    """One turn: env-context noise, a real user request, tool noise, a reply.

    Also carries the ``event_msg`` mirrors Codex writes alongside the
    ``response_item`` messages — the parser must read exactly one of the two
    streams, so these must not double the turn counts.
    """
    return [
        _meta(),
        _env_context(),
        _msg("user", "Why does the z-score threshold not change the count?", "2025-12-04T16:54:00.000Z"),
        {
            "timestamp": "2025-12-04T16:54:00.100Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Why does the z-score threshold not change the count?",
                "images": [],
            },
        },
        *_tool_noise("2025-12-04T16:54:05.000Z"),
        _msg("assistant", "The threshold is read before the events are added.", "2025-12-04T16:55:10.000Z"),
        {
            "timestamp": "2025-12-04T16:55:10.100Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "The threshold is read before the events are added.",
            },
        },
    ]


# ── (b) Parser seam ────────────────────────────────────────────────────


class TestParseRollout:
    def test_extracts_metadata_and_turns(self, codex_root: Path):
        path = _write_rollout(codex_root, _simple_events())

        session = parse_rollout(path)

        assert session is not None
        assert session.rollout_id == ROLLOUT_ID
        assert session.cwd == "/home/u/projects/thinkmesh"
        assert session.project == "thinkmesh"
        assert session.git_branch == "feature/geo"
        assert session.turns == [
            ("user", "Why does the z-score threshold not change the count?"),
            ("assistant", "The threshold is read before the events are added."),
        ]
        assert session.started_at is not None
        assert session.started_at.isoformat().startswith("2025-12-04T16:53:57")
        assert session.ended_at is not None
        assert session.ended_at.isoformat().startswith("2025-12-04T16:55:10")

    def test_metadata_only_rollout_is_skipped(self, codex_root: Path):
        """``history.persistence`` off / aborted before the first reply."""
        path = _write_rollout(codex_root, [_meta(), _env_context()])

        assert parse_rollout(path) is None

    def test_compaction_replay_block_is_recorded_once(self, codex_root: Path):
        """Compaction re-records earlier history inside the same rollout."""
        replayed_user = "Why does the z-score threshold not change the count?"
        replayed_reply = "The threshold is read before the events are added."
        events = [
            *_simple_events(),
            # --- post-compaction replay of the exchange above ---
            _msg("user", replayed_user, "2025-12-04T17:10:00.000Z"),
            _msg("assistant", replayed_reply, "2025-12-04T17:10:05.000Z"),
            # --- genuinely new content after the replay ---
            _msg("user", "Now fix it.", "2025-12-04T17:11:00.000Z"),
            _msg("assistant", "Moved the threshold read inside the loop.", "2025-12-04T17:11:30.000Z"),
        ]
        path = _write_rollout(codex_root, events)

        session = parse_rollout(path)

        assert session is not None
        assert session.turns == [
            ("user", replayed_user),
            ("assistant", replayed_reply),
            ("user", "Now fix it."),
            ("assistant", "Moved the threshold read inside the loop."),
        ]

    def test_genuine_repeats_are_kept_and_pairing_survives(self, codex_root: Path):
        """A user typing "continue" twice is not a compaction replay.

        Suppressing it would also shift every later pairing by one, silently
        attributing "Opened the PR" to "Now ship it" in the permanent record.
        """
        events = [
            _meta(),
            _msg("user", "Add the parser", "2025-12-04T16:54:00.000Z"),
            _msg("assistant", "Added the parser", "2025-12-04T16:54:30.000Z"),
            _msg("user", "continue", "2025-12-04T16:55:00.000Z"),
            _msg("assistant", "Added the tests", "2025-12-04T16:55:30.000Z"),
            _msg("user", "continue", "2025-12-04T16:56:00.000Z"),
            _msg("assistant", "Opened the PR", "2025-12-04T16:56:30.000Z"),
            _msg("user", "Now ship it", "2025-12-04T16:57:00.000Z"),
            _msg("assistant", "Shipped", "2025-12-04T16:57:30.000Z"),
        ]
        path = _write_rollout(codex_root, events)

        session = parse_rollout(path)

        assert session is not None
        assert session.turns == [
            ("user", "Add the parser"),
            ("assistant", "Added the parser"),
            ("user", "continue"),
            ("assistant", "Added the tests"),
            ("user", "continue"),
            ("assistant", "Opened the PR"),
            ("user", "Now ship it"),
            ("assistant", "Shipped"),
        ]

    def test_turn_order_survives_a_suppressed_replay_block(self, codex_root: Path):
        """The rendered body must never pair a reply with the wrong request.

        Replay of a block, then new content — the markdown is what lands in the
        vault permanently and what synthesis reads, so pairing is asserted on
        the rendered text, not just the parsed list.
        """
        events = [
            _meta(),
            _msg("user", "Add the parser", "2025-12-04T16:54:00.000Z"),
            _msg("assistant", "Added the parser", "2025-12-04T16:54:30.000Z"),
            _msg("user", "continue", "2025-12-04T16:55:00.000Z"),
            _msg("assistant", "Added the tests", "2025-12-04T16:55:30.000Z"),
            # compaction: the whole exchange above is re-emitted verbatim
            _msg("user", "Add the parser", "2025-12-04T17:00:00.000Z"),
            _msg("assistant", "Added the parser", "2025-12-04T17:00:01.000Z"),
            _msg("user", "continue", "2025-12-04T17:00:02.000Z"),
            _msg("assistant", "Added the tests", "2025-12-04T17:00:03.000Z"),
            _msg("user", "Now ship it", "2025-12-04T17:01:00.000Z"),
            _msg("assistant", "Shipped", "2025-12-04T17:01:30.000Z"),
        ]
        path = _write_rollout(codex_root, events)

        session = parse_rollout(path)

        assert session is not None
        assert session.turns == [
            ("user", "Add the parser"),
            ("assistant", "Added the parser"),
            ("user", "continue"),
            ("assistant", "Added the tests"),
            ("user", "Now ship it"),
            ("assistant", "Shipped"),
        ]
        body = _build_session_body(session)
        # Each request is immediately followed by its own reply.
        for request, reply in [
            ("Add the parser", "Added the parser"),
            ("continue", "Added the tests"),
            ("Now ship it", "Shipped"),
        ]:
            assert f"{request}\n\n### Assistant" in body, request
            assert reply in body.split(request, 1)[1][:120], (request, reply)

    def test_malformed_lines_are_skipped(self, codex_root: Path):
        path = _write_rollout(codex_root, _simple_events())
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
            fh.write("\n")

        session = parse_rollout(path)

        assert session is not None
        assert session.count("user") == 1


@pytest.mark.parametrize("junk_mb", [4, 16])
def test_memory_stays_bounded_regardless_of_rollout_size(
    codex_root: Path, junk_mb: int
):
    """Rollouts reach 700MB-2GB because raw tool output is stored verbatim.

    The parser must never materialise such a line. The bound asserted here is a
    small constant that does **not** grow with the junk line — parsing a 16MB
    rollout must peak no higher than parsing a 4MB one.
    """
    giant = "x" * (junk_mb * 1024 * 1024)
    events = [
        _meta(),
        _msg("user", "before the giant tool output", "2025-12-04T16:54:00.000Z"),
        {
            "timestamp": "2025-12-04T16:54:05.000Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": giant},
        },
        _msg("assistant", "after the giant tool output", "2025-12-04T16:55:00.000Z"),
    ]
    path = _write_rollout(codex_root, events)
    del giant, events  # don't count the fixture against the measured peak

    tracemalloc.start()
    try:
        session = parse_rollout(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Content on both sides of the oversized line still lands.
    assert session is not None
    assert session.turns == [
        ("user", "before the giant tool output"),
        ("assistant", "after the giant tool output"),
    ]
    # Constant bound: generous enough for the reader's buffers, far below the
    # smallest junk line, and identical for both parametrised sizes.
    assert peak < 3 * 1024 * 1024, f"peak {peak} bytes on a {junk_mb}MB rollout"


# ── discovery ──────────────────────────────────────────────────────────


def test_discovery_is_newest_first(codex_root: Path):
    older = _write_rollout(
        codex_root,
        _simple_events(),
        name="rollout-2025-12-04T09-00-00-aaaaaaaa-0000-0000-0000-000000000001.jsonl",
    )
    newer = _write_rollout(
        codex_root,
        _simple_events(),
        name="rollout-2025-12-04T21-00-00-bbbbbbbb-0000-0000-0000-000000000002.jsonl",
    )

    assert list(discover_rollouts(codex_root)) == [newer, older]


def test_rollout_id_comes_from_the_filename():
    assert rollout_id(Path("/x/2025/12/04") / ROLLOUT_NAME) == ROLLOUT_ID
    assert rollout_id(Path("/x/rollout-oddly-named.jsonl")) == "rollout-oddly-named"


# ── (c) Import + manifest seam ─────────────────────────────────────────


@pytest.fixture
def vault_cfg(tmp_path: Path) -> Config:
    cfg = Config(vault_root=tmp_path / "vault")
    VaultManager(config=cfg).ensure_dirs()
    return cfg


def _session_notes(cfg: Config) -> list[Path]:
    return sorted((Path(cfg.vault_root) / "projects").rglob("sessions/*/session.md"))


class TestImportCodex:
    def test_materializes_one_session_note_per_rollout(
        self, vault_cfg: Config, codex_root: Path
    ):
        _write_rollout(codex_root, _simple_events())

        stats = import_codex(vault_cfg, sessions_root=codex_root)

        assert stats["discovered"] == 1
        assert stats["materialized"] == 1
        assert stats["errors"] == []
        assert stats["per_project"]["thinkmesh"]["materialized"] == 1

        notes = _session_notes(vault_cfg)
        assert len(notes) == 1
        fm, body = parse_frontmatter(notes[0].read_text(encoding="utf-8"))
        assert fm["imported_from"] == "codex"
        assert fm["codex_rollout_id"] == ROLLOUT_ID
        assert fm["project"] == "thinkmesh"
        assert fm["git_branch"] == "feature/geo"
        assert fm["user_turn_count"] == 1
        assert fm["assistant_turn_count"] == 1
        assert "codex" in fm["tags"] and "imported" in fm["tags"]
        assert "## Transcript" in body
        assert "Why does the z-score threshold not change the count?" in body

    def test_rerun_is_a_noop(self, vault_cfg: Config, codex_root: Path):
        _write_rollout(codex_root, _simple_events())
        import_codex(vault_cfg, sessions_root=codex_root)
        first = _session_notes(vault_cfg)

        stats = import_codex(vault_cfg, sessions_root=codex_root)

        assert stats["materialized"] == 0
        assert stats["skipped_already_imported"] == 1
        assert _session_notes(vault_cfg) == first

    def test_manifest_lands_at_the_documented_path(
        self, vault_cfg: Config, codex_root: Path
    ):
        _write_rollout(codex_root, _simple_events())

        import_codex(vault_cfg, sessions_root=codex_root)

        manifest = Path(vault_cfg.vault_root) / ".weave" / "onboarding" / "codex.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert ROLLOUT_ID in data["imported_ids"]

    def test_dry_run_writes_nothing(self, vault_cfg: Config, codex_root: Path):
        _write_rollout(codex_root, _simple_events())

        stats = import_codex(vault_cfg, sessions_root=codex_root, dry_run=True)

        assert stats["materialized"] == 1
        assert _session_notes(vault_cfg) == []
        assert not (Path(vault_cfg.vault_root) / ".weave" / "onboarding").exists()

    def test_project_filter_skips_other_projects(
        self, vault_cfg: Config, codex_root: Path
    ):
        _write_rollout(codex_root, _simple_events())

        stats = import_codex(
            vault_cfg, sessions_root=codex_root, project_filter="something_else"
        )

        assert stats["materialized"] == 0
        assert stats["skipped_filter"] == 1

    def test_since_skips_older_rollouts(self, vault_cfg: Config, codex_root: Path):
        _write_rollout(codex_root, _simple_events())

        stats = import_codex(vault_cfg, sessions_root=codex_root, since="2026-01-01")

        assert stats["materialized"] == 0
        assert stats["skipped_since"] == 1

    def test_limit_keeps_the_newest(self, vault_cfg: Config, codex_root: Path):
        _write_rollout(
            codex_root,
            [_meta(), _msg("user", "old work", "2025-12-04T09:00:00.000Z")],
            name="rollout-2025-12-04T09-00-00-aaaaaaaa-0000-0000-0000-000000000001.jsonl",
        )
        _write_rollout(
            codex_root,
            [_meta(), _msg("user", "recent work", "2025-12-04T21:00:00.000Z")],
            name="rollout-2025-12-04T21-00-00-bbbbbbbb-0000-0000-0000-000000000002.jsonl",
        )

        stats = import_codex(vault_cfg, sessions_root=codex_root, limit=1)

        assert stats["materialized"] == 1
        notes = _session_notes(vault_cfg)
        assert len(notes) == 1
        assert "recent work" in notes[0].read_text(encoding="utf-8")


# ── (d) Absent-root seam ───────────────────────────────────────────────


def test_missing_codex_root_reports_cleanly(vault_cfg: Config, tmp_path: Path):
    stats = import_codex(vault_cfg, sessions_root=tmp_path / "nope" / "sessions")

    assert stats["materialized"] == 0
    assert len(stats["errors"]) == 1
    assert "not found" in stats["errors"][0]


def test_default_root_points_at_the_codex_sessions_dir():
    assert DEFAULT_CODEX_SESSIONS_ROOT == Path.home() / ".codex" / "sessions"


# ── (a) CLI seam ───────────────────────────────────────────────────────


@pytest.fixture
def cli_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
    VaultManager(config=Config(vault_root=vault)).ensure_dirs()
    return vault


class TestCli:
    def test_import_codex_materializes(
        self, cli_vault: Path, codex_root: Path, capsys: pytest.CaptureFixture
    ):
        from thinkweave.surfaces.cli import main

        _write_rollout(codex_root, _simple_events())

        main(["import", "codex", "--cc-root", str(codex_root)])

        out = capsys.readouterr().out
        assert "Materialized: 1 session(s)" in out
        assert "thinkmesh" in out
        assert len(sorted((cli_vault / "projects").rglob("sessions/*/session.md"))) == 1

    def test_missing_root_prints_a_message_and_does_not_raise(
        self, cli_vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ):
        from thinkweave.surfaces.cli import main

        main(["import", "codex", "--cc-root", str(tmp_path / "absent")])

        out = capsys.readouterr().out
        assert "not found" in out


# ── Shape parity with the CC importer ──────────────────────────────────


def test_note_shape_matches_a_claude_code_import(vault_cfg: Config, tmp_path: Path):
    """Acceptance: codex session notes are indistinguishable in shape from
    CC-imported ones — same note type, same frontmatter keys (modulo the
    per-harness id key), same body sections."""
    from thinkweave.onboarding.claude_code_seed import import_claude_code

    codex_root = tmp_path / "codex" / "sessions"
    codex_root.mkdir(parents=True)
    _write_rollout(codex_root, _simple_events())

    cc_root = tmp_path / "cc" / "projects" / "-home-u-projects-thinkmesh"
    cc_root.mkdir(parents=True)
    cc_lines = [
        {
            "type": "user",
            "timestamp": "2025-12-04T16:54:00.000Z",
            "cwd": "/home/u/projects/thinkmesh",
            "gitBranch": "feature/geo",
            "message": {"content": "Why does the z-score threshold not change the count?"},
        },
        {
            "type": "assistant",
            "timestamp": "2025-12-04T16:55:10.000Z",
            "cwd": "/home/u/projects/thinkmesh",
            "message": {
                "content": [
                    {"type": "text", "text": "The threshold is read before the events are added."}
                ]
            },
        },
    ]
    cc_file = cc_root / "11111111-2222-3333-4444-555555555555.jsonl"
    with cc_file.open("w", encoding="utf-8") as fh:
        for ev in cc_lines:
            fh.write(json.dumps(ev) + "\n")

    import_codex(vault_cfg, sessions_root=codex_root)
    import_claude_code(vault_cfg, claude_projects_root=cc_root.parent)

    notes = _session_notes(vault_cfg)
    assert len(notes) == 2
    parsed = [parse_frontmatter(p.read_text(encoding="utf-8")) for p in notes]
    by_source = {fm["imported_from"]: (fm, body) for fm, body in parsed}
    assert set(by_source) == {"codex", "claude-code"}

    codex_fm, codex_body = by_source["codex"]
    cc_fm, cc_body = by_source["claude-code"]

    per_harness_id = {"codex_rollout_id", "claude_session_uuid"}
    assert set(codex_fm) - per_harness_id == set(cc_fm) - per_harness_id
    assert codex_fm["type"] == cc_fm["type"]
    assert set(codex_fm["tags"]) - {"codex", "claude-code"} == set(cc_fm["tags"]) - {
        "codex",
        "claude-code",
    }
    for section in ("## Source", "## Transcript", "### User (turn 1)", "### Assistant (turn 1)"):
        assert section in codex_body, section
        assert section in cc_body, section


# ── --enrich picks up codex sessions ───────────────────────────────────


def test_enrich_finds_imported_codex_sessions(vault_cfg: Config, codex_root: Path):
    """The dual-route ``--enrich`` pass consumes the session note, not the
    transcript format — so an imported codex session is pending synthesis on
    exactly the same terms as a CC one."""
    from thinkweave.onboarding.enrich_batch import find_pending_sessions

    _write_rollout(codex_root, _simple_events())
    import_codex(vault_cfg, sessions_root=codex_root)

    pending = find_pending_sessions(vault_cfg)

    assert len(pending) == 1
    assert pending[0].project == "thinkmesh"
    assert "## Transcript" in pending[0].transcript
