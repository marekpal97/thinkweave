"""Serve-once SessionStart delivery (#175, reworked per the PR #203 review).

Harnesses emit SessionStart more than once per logical conversation
(startup, resume, clear, compact, replay), but the initial context is
served exactly ONCE per session. ``_handle_session_start`` branches on the
hook input's ``source``:

- ``startup`` / ``clear`` (or absent — legacy harnesses): the full
  project snapshot.
- ``resume`` / ``compact``: NOTHING is injected — resume replays the
  transcript (the context is still in the window), and after compaction
  the agent retrieves on demand via the weave_* MCP/CLI. A zero-id
  ``skipped_lifecycle`` telemetry event keeps exposure accounting honest.
- a redelivery of the startup/clear serve (double registration,
  post-archival replay): nothing is injected; a ``skipped_replay``
  telemetry event is recorded with no note ids.

The replay receipt is the ``delivery_id`` stamped on the buffered
``startup`` event itself: ``archive_buffer`` carries it into the session
folder's ``retrieval_log.jsonl``, so the guard survives live-buffer
archival (the buffer-side O_EXCL receipt file does not).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import read_jsonl, write_session_md, write_transcript
from thinkweave.core.harness import SESSION_START_BUDGET_TOKENS
from thinkweave.retrieval.context import (
    CHARS_PER_TOKEN,
    SESSION_START_GUARD_HEADROOM_CHARS,
    SESSION_START_SECTIONS,
)

# One full-snapshot payload used by every test; the chain fixture
# pre-serves dec-aaaa1111 + ses-cccc3333 so serve-state on disk is
# distinguishable from a fresh vault.
FULL_PAYLOAD = (
    "## Recent Decisions\n"
    "- (`dec-aaaa1111`) decision one\n"
    "- (`dec-bbbb2222`) decision two\n"
    "\n"
    "## Recent Wrapped Sessions\n"
    "- (`ses-cccc3333`) prior session\n"
)

CHAIN_KEY = "cse_chain1"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
    monkeypatch.delenv("THINKWEAVE_WEAVE_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "thinkweave.retrieval.context.build_project_context",
        lambda *a, **kw: FULL_PAYLOAD,
    )
    from thinkweave.core.config import load_config

    return load_config()


@pytest.fixture
def outputs(monkeypatch) -> list[dict]:
    """Capture every _output call's kwargs."""
    from thinkweave.surfaces.hooks import handler as h

    calls: list[dict] = []

    def fake_output(system_message="", additional_context="", hook_event_name=""):
        calls.append({
            "system_message": system_message,
            "additional_context": additional_context,
            "hook_event_name": hook_event_name,
        })

    monkeypatch.setattr(h, "_output", fake_output)
    return calls


def _buffer_events(cfg, session_id: str) -> list[dict]:
    return read_jsonl(cfg.weave_dir / "buffer" / f"{session_id}.jsonl")


def _write_session_md(
    folder: Path,
    note_id: str,
    source_session: str = "",
    logical_session: str = "",
    date: str = "",
) -> None:
    """One session.md with the frontmatter fields the serve paths read."""
    fm: dict = {"id": note_id, "type": "session", "project": "t"}
    if date:
        fm["date"] = date
    if source_session:
        fm["source_session"] = source_session
    if logical_session:
        fm["logical_session"] = logical_session
    write_session_md(folder, **fm)


def _seed_chain_root(
    cfg,
    *,
    note_id: str = "ses-root0001",
    source_session: str = "uuid-root",
    served_ids: list[str] | None = None,
    delivery_id: str = "",
    folder_name: str = "uuid-root-2026-08-29",
    startup_ts: str = "2026-08-29T10:00:00+00:00",
) -> Path:
    """An archived earlier segment of the logical session: session note
    stamped with the chain key, empty events.jsonl, and a retrieval_log
    holding one already-served startup event. The folder name follows the
    production ``{session_id}-{YYYY-MM-DD}`` shape (core/vault.py
    ``_session_dir``): a leading RANDOM uuid, date only at the tail — any
    recency logic keyed on the leading name is wrong by construction."""
    folder = cfg.vault_root / "projects" / "t" / "sessions" / folder_name
    _write_session_md(
        folder, note_id, source_session, CHAIN_KEY, date="2026-08-29"
    )
    (folder / "events.jsonl").touch()
    event: dict = {
        "ts": startup_ts,
        "type": "startup",
        "returned_ids": served_ids or [],
        "token_est": 100,
    }
    if delivery_id:
        event["delivery_id"] = delivery_id
    (folder / "retrieval_log.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8"
    )
    return folder


def _transcript_with_bridge(tmp_path: Path) -> str:
    return str(write_transcript(
        tmp_path, [{"type": "bridge-session", "bridgeSessionId": CHAIN_KEY}]
    ))


class TestFullServe:
    """AC1 (startup/clear legs) + AC5: full snapshot for fresh sessions."""

    @pytest.mark.parametrize("source", ["startup", "clear"])
    def test_startup_and_clear_serve_the_full_snapshot(
        self, env, outputs, tmp_path: Path, source: str
    ):
        from thinkweave.surfaces.hooks import handler as h

        h._handle_session_start({
            "session_id": f"uuid-{source}1",
            "source": source,
            "cwd": str(tmp_path),
        })

        assert outputs[-1]["additional_context"] == FULL_PAYLOAD
        events = _buffer_events(env, f"uuid-{source}1")
        assert len(events) == 1
        assert events[0]["disposition"] == "served_full"
        assert events[0]["lifecycle"] == source
        assert events[0]["returned_ids"] == [
            "dec-aaaa1111", "dec-bbbb2222", "ses-cccc3333",
        ]

    def test_genuinely_new_session_gets_full_snapshot_despite_other_chains(
        self, env, outputs, tmp_path: Path
    ):
        # AC5: prior serve state for a DIFFERENT logical session must not
        # bleed into a new session's delivery.
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        h._handle_session_start({
            "session_id": "uuid-fresh1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        assert outputs[-1]["additional_context"] == FULL_PAYLOAD
        assert _buffer_events(env, "uuid-fresh1")[0]["disposition"] == "served_full"


class TestLifecycleSkip:
    """Serve-once (#175 rework, PR #203 review): the initial context is
    served once per session — resume replays the transcript (context still
    present) and after compaction the agent retrieves on demand via the
    weave_* MCP/CLI, so resume/compact inject NOTHING. Only a telemetry
    event with zero ids is buffered, keeping exposure accounting honest."""

    @pytest.mark.parametrize("source", ["resume", "compact"])
    def test_resume_and_compact_inject_nothing(
        self, env, outputs, tmp_path: Path, monkeypatch, source: str
    ):
        from thinkweave.surfaces.hooks import handler as h

        builds: list = []
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: builds.append(1) or FULL_PAYLOAD,
        )
        # Even with chain serve-state on disk: nothing is injected.
        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])

        h._handle_session_start({
            "session_id": f"uuid-lc-{source}",
            "source": source,
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        assert outputs[-1]["additional_context"] == ""
        # The snapshot is not even built — the skip path is payload-free.
        assert builds == []
        events = _buffer_events(env, f"uuid-lc-{source}")
        assert len(events) == 1
        assert events[0]["disposition"] == "skipped_lifecycle"
        assert events[0]["lifecycle"] == source
        # Zero exposure: nothing for context_served to project.
        assert events[0]["returned_ids"] == []
        assert events[0]["token_est"] == 0


class TestReplayGuard:
    """AC2 (in-process duplicate) + AC4 (receipt survives archival)."""

    def test_second_identical_registration_is_a_no_op(
        self, env, outputs, tmp_path: Path
    ):
        from thinkweave.surfaces.hooks import handler as h

        hook_input = {
            "session_id": "uuid-dup1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        h._handle_session_start(dict(hook_input))
        h._handle_session_start(dict(hook_input))

        # One delivery: the first injects, the duplicate injects nothing.
        assert outputs[0]["additional_context"] == FULL_PAYLOAD
        assert outputs[1]["additional_context"] == ""

        events = _buffer_events(env, "uuid-dup1")
        assert [e["disposition"] for e in events] == [
            "served_full", "skipped_replay",
        ]
        # AC6: the skip event records zero exposure.
        assert events[1]["returned_ids"] == []

    def test_replay_after_buffer_archival_is_still_skipped(
        self, env, outputs, tmp_path: Path
    ):
        from thinkweave.core.buffer import archive_buffer
        from thinkweave.surfaces.hooks import handler as h

        hook_input = {
            "session_id": "uuid-arch1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        h._handle_session_start(dict(hook_input))
        assert outputs[0]["additional_context"] == FULL_PAYLOAD

        # Stop-time archival: buffer (and its receipt scratch dir) retired
        # into the session folder — production {uuid}-{date} name. The
        # decoy crowd (higher-sorting uuids, ancient dates) pins that the
        # own-folder lookup keys on the trailing DATE: a name-keyed sort
        # would fill its window with decoys and leave the replay guard
        # inert after archival.
        sessions = env.vault_root / "projects" / "t" / "sessions"
        for i in range(20):
            _write_session_md(
                sessions / f"zzzz{i:04x}beef-2019-01-01", "ses-decoy002"
            )
        folder = sessions / "uuid-arch1-2026-08-30"
        _write_session_md(
            folder, "ses-arch00001", "uuid-arch1", date="2026-08-30"
        )
        archive_buffer(env.weave_dir, "uuid-arch1", folder)
        assert not (env.weave_dir / "buffer" / "uuid-arch1.jsonl").exists()

        h._handle_session_start(dict(hook_input))

        # AC4: the delivery_id persisted in retrieval_log.jsonl still guards.
        assert outputs[1]["additional_context"] == ""
        events = _buffer_events(env, "uuid-arch1")
        assert [e["disposition"] for e in events] == ["skipped_replay"]

    def test_changed_served_set_is_not_a_replay(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # A later SessionStart whose snapshot serves different notes (the
        # vault moved on) must serve — identity is the served id set, not
        # the session_id.
        from thinkweave.surfaces.hooks import handler as h

        hook_input = {
            "session_id": "uuid-grow1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        h._handle_session_start(dict(hook_input))
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: FULL_PAYLOAD + "- (`n-ddd44444`) new note\n",
        )
        h._handle_session_start(dict(hook_input))

        assert "n-ddd44444" in outputs[1]["additional_context"]
        events = _buffer_events(env, "uuid-grow1")
        assert [e["disposition"] for e in events] == [
            "served_full", "served_full",
        ]

    def test_replay_guard_is_time_independent(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # The full payload's header carries a minute-resolution timestamp: a
        # re-registration straddling a minute boundary must still be caught,
        # so identity hashes the served id set, not raw payload bytes.
        from thinkweave.surfaces.hooks import handler as h

        payloads = iter([
            "_Today: 2026-08-30 11:59_\n" + FULL_PAYLOAD,
            "_Today: 2026-08-30 12:00_\n" + FULL_PAYLOAD,
        ])
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: next(payloads),
        )
        hook_input = {
            "session_id": "uuid-clock1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        h._handle_session_start(dict(hook_input))
        h._handle_session_start(dict(hook_input))

        assert outputs[1]["additional_context"] == ""
        events = _buffer_events(env, "uuid-clock1")
        assert [e["disposition"] for e in events] == [
            "served_full", "skipped_replay",
        ]

    def test_repeated_replays_write_one_skip_event(
        self, env, outputs, tmp_path: Path
    ):
        # Bounded telemetry: one skip line per (session, replay_of), not one
        # per redelivery.
        from thinkweave.surfaces.hooks import handler as h

        hook_input = {
            "session_id": "uuid-multi1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        for _ in range(3):
            h._handle_session_start(dict(hook_input))

        events = _buffer_events(env, "uuid-multi1")
        assert [e["disposition"] for e in events] == [
            "served_full", "skipped_replay",
        ]


class TestSeamGuardUnion:
    def test_guard_runs_on_prior_and_new_ids(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # The agent still relies on everything this session was served, so
        # the stale-twin guard considers the session's own prior serves too
        # — not just this delivery's ids.
        captured: list[set] = []
        monkeypatch.setattr(
            "thinkweave.synthesis.memory_seam.session_guard_section",
            lambda cfg, ids: captured.append(set(ids)) or "",
        )
        from thinkweave.surfaces.hooks import handler as h

        hook_input = {
            "session_id": "uuid-guard1",
            "source": "startup",
            "cwd": str(tmp_path),
        }
        h._handle_session_start(dict(hook_input))
        # The vault moved on: the next serve carries a different id set…
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: "- (`n-ddd44444`) new note\n",
        )
        h._handle_session_start(dict(hook_input))

        # …and the guard still sees the earlier serve's ids alongside it.
        assert captured
        assert captured[-1] >= {
            "dec-aaaa1111", "dec-bbbb2222", "ses-cccc3333", "n-ddd44444",
        }


# --------------------------------------------------------------------------- #
# The harness inline cap
# --------------------------------------------------------------------------- #


# Claude Code renders an ``additionalContext`` this long inline; above it it
# files the text to a temp path and shows the model a 2KB preview. Hand-written
# from that harness behaviour, independent of the profile field it compares to.
CAP = 10_000

# Long enough that ten of these cannot ride inline beside the tasks section —
# the fixture has to be at least as fat as a vault that overflows the cap.
_FAT_SUMMARY = (
    "Reworked the SessionStart payload assembly and chased an FTS5 quoting "
    "bug through the indexer; the drop order now protects the cue sections. "
) * 6


def _session_body(i: int) -> str:
    return (
        f"## Summary\n{_FAT_SUMMARY}\n\n"
        "## Candidate Insights\n\n"
        f"- **Insight one of session {i}** body text\n\n"
        f"- **Insight two of session {i}** body text\n\n"
        f"- **Insight three of session {i}** body text\n"
    )


@pytest.fixture
def fat_vault(vault_factory, monkeypatch):
    """Ten wrapped sessions, seven open tasks, and a 15KB STATE.md.

    The overflow shape: the STATE.md excerpt alone can fill 12KB of the
    payload, so a builder that still serves it blows the cap here.
    """
    from thinkweave.core.indexer import Indexer
    from thinkweave.core.schemas import NoteType

    handle = vault_factory()
    for i in range(10):
        handle.with_note(
            f"Wrapped session {i}",
            note_type=NoteType.SESSION,
            body=_session_body(i),
            project="t",
            extra_frontmatter={
                "processed": True,
                "processed_at": f"2026-10-{1 + i:02d}T09:00:00+00:00",
                "source_session": f"cc-fat{i}",
            },
        )
    for i in range(7):
        handle.with_note(
            f"Open task {i}",
            note_id=f"tsk-open000{i}",
            project="t",
            extra_frontmatter={
                "kind": "task",
                "status": "open",
                "grain": "work",
                "rounds": [],
                "title": f"Open task {i}",
                "asked": f"#{300 + i}",
            },
        )
    project_dir = handle.dir / "projects" / "t"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "STATE.md").write_text(
        "# State\n\n" + "Stale state prose. " * 800, encoding="utf-8"
    )
    idx = Indexer(config=handle.config)
    try:
        idx.rebuild(full=True)
    finally:
        idx.close()
    monkeypatch.setattr(
        "thinkweave.core.config.load_config", lambda: handle.config
    )
    monkeypatch.setattr(
        "thinkweave.surfaces.hooks.handler.detect_project", lambda cwd: "t"
    )
    return handle


class TestHookContextCap:
    """The delivered payload lands inline, inside the harness's own cap."""

    def test_claude_code_payload_lands_inside_the_cap(
        self, fat_vault, outputs, tmp_path
    ):
        from thinkweave.core import harness
        from thinkweave.surfaces.hooks import handler as h

        cap = harness.active().additional_context_cap_chars
        assert cap == CAP

        h._handle_session_start({
            "session_id": "uuid-cap1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        ctx = outputs[-1]["additional_context"]
        assert len(ctx) <= cap
        # It fits by whole-section selection, not by a mid-string cut.
        assert "payload capped" not in ctx
        # Nothing spilled, so there is nothing to announce.
        assert outputs[-1]["system_message"] == ""
        assert "## State of Play" not in ctx
        assert "Stale state prose" not in ctx
        # Ordering: the first 2KB carries the continuation cue and the
        # wrapped-session history.
        head = ctx[:2048]
        assert "## Open Tasks" in head
        assert "tsk-open" in head
        assert "## Recent Wrapped Sessions" in head

    def test_handler_spends_a_cap_derived_budget(
        self, env, outputs, tmp_path, monkeypatch
    ):
        from thinkweave.core import harness
        from thinkweave.surfaces.hooks import handler as h

        spent: dict = {}

        def spy(*args, **kwargs):
            spent.update(kwargs)
            return FULL_PAYLOAD

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context", spy
        )
        h._handle_session_start({
            "session_id": "uuid-budget1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        cap = harness.active().additional_context_cap_chars
        assert spent["budget_tokens"] * CHARS_PER_TOKEN + (
            SESSION_START_GUARD_HEADROOM_CHARS
        ) <= cap
        # STATE.md is served on demand, never at startup.
        assert "state" not in spent["sections"]
        assert spent["sections"] == list(SESSION_START_SECTIONS)

    def test_profile_without_a_cap_spends_the_default_budget(
        self, env, outputs, tmp_path, monkeypatch, use_profile
    ):
        from thinkweave.surfaces.hooks import handler as h

        spent: dict = {}

        def spy(*args, **kwargs):
            spent.update(kwargs)
            return FULL_PAYLOAD

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context", spy
        )
        use_profile(additional_context_cap_chars=None)
        h._handle_session_start({
            "session_id": "uuid-default1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        assert spent["budget_tokens"] == SESSION_START_BUDGET_TOKENS

    def test_a_guard_that_pushes_past_the_cap_announces_the_spill(
        self, env, outputs, tmp_path, monkeypatch
    ):
        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: "x" * 8_000,
        )
        monkeypatch.setattr(
            "thinkweave.synthesis.memory_seam.session_guard_section",
            lambda cfg, ids: "y" * 3_000,
        )
        h._handle_session_start({
            "session_id": "uuid-spill1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        # The guard is a correctness interrupt: it is never cut to fit, and
        # the residual overflow announces itself instead of passing silently.
        assert len(outputs[-1]["additional_context"]) == 11_001
        msg = outputs[-1]["system_message"]
        assert str(CAP) in msg
        assert "preview" in msg

    def test_a_stamp_naming_no_harness_is_announced_not_absorbed(
        self, env, tmp_path, monkeypatch, capsys
    ):
        """The ``--harness`` stamp selects the profile whose cap and session-id
        envs the handler spends. A stamp that names nothing must not silently
        spend Claude Code's values for a harness they do not describe.

        Asserted on the wire (stdout), because the notice rides the
        ``systemMessage`` channel the real :func:`_output` composes.
        """
        import sys

        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(
            sys, "argv", ["weave-hook", "session_start", "--harness", "not-a-harness"]
        )
        h._handle_session_start({
            "session_id": "uuid-stamp1",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        reply = json.loads(capsys.readouterr().out)
        assert "not-a-harness" in reply["systemMessage"]
        # The payload still ships — the notice is a degraded path, not a halt.
        assert (
            reply["hookSpecificOutput"]["additionalContext"] == FULL_PAYLOAD
        )

    def test_a_known_stamp_stays_silent(
        self, env, tmp_path, monkeypatch, capsys
    ):
        import sys

        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(
            sys, "argv", ["weave-hook", "session_start", "--harness", "codex"]
        )
        h._handle_session_start({
            "session_id": "uuid-stamp2",
            "source": "startup",
            "cwd": str(tmp_path),
        })

        assert "systemMessage" not in json.loads(capsys.readouterr().out)
