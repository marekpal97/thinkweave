"""Lifecycle-aware SessionStart delivery (#175).

Harnesses emit SessionStart more than once per logical conversation
(startup, resume, clear, compact, replay). ``_handle_session_start``
branches on the hook input's ``source``:

- ``startup`` / ``clear`` (or absent — legacy harnesses): the full
  project snapshot, as before.
- ``resume`` / ``compact``: a delta — only notes not already served to
  the logical-session chain (#180), plus one "N notes already in
  context" line.
- a byte-identical redelivery (double registration, post-archival
  replay): nothing is injected; a ``skipped_replay`` telemetry event is
  recorded with no note ids, so context exposure is never double-counted.

The replay receipt is the ``delivery_id`` stamped on the buffered
``startup`` event itself: ``archive_buffer`` carries it into the session
folder's ``retrieval_log.jsonl``, so the guard survives live-buffer
archival (the buffer-side O_EXCL receipt file does not).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# One full-snapshot payload used by every test. Ids chosen so the delta
# math has an independent source of truth: the chain fixture pre-serves
# dec-aaaa1111 + ses-cccc3333, leaving dec-bbbb2222 the only new note.
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
    buf = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
    if not buf.exists():
        return []
    return [
        json.loads(line)
        for line in buf.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_chain_root(
    cfg,
    *,
    note_id: str = "ses-root0001",
    source_session: str = "uuid-root",
    served_ids: list[str] | None = None,
    delivery_id: str = "",
) -> Path:
    """An archived earlier segment of the logical session: session note
    stamped with the chain key, empty events.jsonl, and a retrieval_log
    holding one already-served startup event."""
    folder = cfg.vault_root / "projects" / "t" / "sessions" / "session-root"
    folder.mkdir(parents=True)
    (folder / "session.md").write_text(
        "---\n"
        f"id: {note_id}\n"
        "type: session\n"
        "project: t\n"
        "date: '2026-08-29'\n"
        f"source_session: {source_session}\n"
        f"logical_session: {CHAIN_KEY}\n"
        "---\n",
        encoding="utf-8",
    )
    (folder / "events.jsonl").touch()
    event: dict = {
        "ts": "2026-08-29T10:00:00+00:00",
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
    t = tmp_path / "transcript.jsonl"
    t.write_text(
        json.dumps({"type": "bridge-session", "bridgeSessionId": CHAIN_KEY})
        + "\n",
        encoding="utf-8",
    )
    return str(t)


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


class TestDeltaServe:
    """AC1 (resume/compact legs) + AC3 chain-root stamping + AC6."""

    @pytest.mark.parametrize("source", ["resume", "compact"])
    def test_resume_and_compact_serve_a_disjoint_delta(
        self, env, outputs, tmp_path: Path, source: str
    ):
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        h._handle_session_start({
            "session_id": "uuid-seg2",
            "source": source,
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        payload = outputs[-1]["additional_context"]
        # Disjoint from the chain's served set…
        assert "dec-aaaa1111" not in payload
        assert "ses-cccc3333" not in payload
        # …carrying only the genuinely new note…
        assert "dec-bbbb2222" in payload
        # …plus the single already-in-context summary line.
        assert "2 notes already in context" in payload

        events = _buffer_events(env, "uuid-seg2")
        assert len(events) == 1
        assert events[0]["disposition"] == "served_delta"
        assert events[0]["lifecycle"] == source
        # AC6: exposure counted once — only the new note id is recorded.
        assert events[0]["returned_ids"] == ["dec-bbbb2222"]
        # AC3: the delivery is keyed to the chain's ROOT session note.
        assert events[0]["chain_root"] == "ses-root0001"

    def test_resume_with_no_chain_state_falls_back_to_full(
        self, env, outputs, tmp_path: Path
    ):
        # A resume the vault has no serve record for is effectively a fresh
        # session: the full snapshot is the only useful delivery.
        from thinkweave.surfaces.hooks import handler as h

        h._handle_session_start({
            "session_id": "uuid-lonely1",
            "source": "resume",
            "cwd": str(tmp_path),
        })

        assert outputs[-1]["additional_context"] == FULL_PAYLOAD
        assert _buffer_events(env, "uuid-lonely1")[0]["disposition"] == "served_full"

    def test_everything_already_served_yields_summary_only(
        self, env, outputs, tmp_path: Path
    ):
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(
            env,
            served_ids=["dec-aaaa1111", "dec-bbbb2222", "ses-cccc3333"],
        )
        h._handle_session_start({
            "session_id": "uuid-seg3",
            "source": "resume",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        payload = outputs[-1]["additional_context"]
        assert "3 notes already in context" in payload
        for nid in ("dec-aaaa1111", "dec-bbbb2222", "ses-cccc3333"):
            assert nid not in payload
        assert _buffer_events(env, "uuid-seg3")[0]["returned_ids"] == []


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
        # into the session folder the chain resolver can find.
        folder = env.vault_root / "projects" / "t" / "sessions" / "session-arch"
        folder.mkdir(parents=True)
        (folder / "session.md").write_text(
            "---\n"
            "id: ses-arch00001\n"
            "type: session\n"
            "project: t\n"
            "date: '2026-08-30'\n"
            "source_session: uuid-arch1\n"
            "---\n",
            encoding="utf-8",
        )
        archive_buffer(env.weave_dir, "uuid-arch1", folder)
        assert not (env.weave_dir / "buffer" / "uuid-arch1.jsonl").exists()

        h._handle_session_start(dict(hook_input))

        # AC4: the delivery_id persisted in retrieval_log.jsonl still guards.
        assert outputs[1]["additional_context"] == ""
        events = _buffer_events(env, "uuid-arch1")
        assert [e["disposition"] for e in events] == ["skipped_replay"]

    def test_changed_payload_is_not_a_replay(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # A later SessionStart whose snapshot content differs (the vault
        # moved on) must serve — the guard is byte-identity, not session_id.
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
