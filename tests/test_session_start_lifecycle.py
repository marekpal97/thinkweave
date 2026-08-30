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
    folder_name: str = "session-root",
    startup_ts: str = "2026-08-29T10:00:00+00:00",
) -> Path:
    """An archived earlier segment of the logical session: session note
    stamped with the chain key, empty events.jsonl, and a retrieval_log
    holding one already-served startup event."""
    folder = cfg.vault_root / "projects" / "t" / "sessions" / folder_name
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

    def test_resume_delta_is_disjoint_from_served_set(
        self, env, outputs, tmp_path: Path
    ):
        # Resume REPLAYS the transcript: prior injections are genuinely back
        # in the window, so suppressed ids must not even appear as text.
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        h._handle_session_start({
            "session_id": "uuid-seg2",
            "source": "resume",
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
        assert events[0]["lifecycle"] == "resume"
        # AC6: exposure counted once — only the new note id is recorded.
        assert events[0]["returned_ids"] == ["dec-bbbb2222"]
        # AC3: the delivery is keyed to the chain's ROOT session note.
        assert events[0]["chain_root"] == "ses-root0001"

    def test_compact_delta_lists_evicted_ids_for_restore(
        self, env, outputs, tmp_path: Path
    ):
        # Compaction EVICTED the prior injections from the window: the delta
        # lists the suppressed ids (id + title line) so the agent can
        # weave_read them back — as plain-text references, NOT exposure.
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        h._handle_session_start({
            "session_id": "uuid-seg2c",
            "source": "compact",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        payload = outputs[-1]["additional_context"]
        assert "dec-bbbb2222" in payload
        assert "2 notes already in context" in payload
        # The evicted ids are listed so they can be restored…
        assert "dec-aaaa1111" in payload
        assert "ses-cccc3333" in payload

        events = _buffer_events(env, "uuid-seg2c")
        assert len(events) == 1
        assert events[0]["disposition"] == "served_delta"
        assert events[0]["lifecycle"] == "compact"
        # …but exposure stays the NEW ids only (AC1/AC6 disjointness holds
        # at the context_served level).
        assert events[0]["returned_ids"] == ["dec-bbbb2222"]
        assert events[0]["chain_root"] == "ses-root0001"

    def test_compact_keeps_tools_manifest_and_footer_resume_does_not(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # The tools manifest and retrieval footer were compacted out of the
        # window too — a compact delta re-serves them; a resume (window
        # restored) does not.
        from thinkweave.surfaces.hooks import handler as h

        payload_with_tools = (
            FULL_PAYLOAD
            + "\n## Available MCP Tools\n- weave_search: find X\n"
            + "\n## Retrieval Hints\nUse weave_context for Y.\n"
        )
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: payload_with_tools,
        )
        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])

        h._handle_session_start({
            "session_id": "uuid-toolsr",
            "source": "resume",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })
        assert "weave_search: find X" not in outputs[-1]["additional_context"]

        h._handle_session_start({
            "session_id": "uuid-toolsc",
            "source": "compact",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })
        compact_payload = outputs[-1]["additional_context"]
        assert "weave_search: find X" in compact_payload
        assert "Use weave_context for Y." in compact_payload

    def test_chain_root_ranked_by_earliest_startup_ts_not_folder_name(
        self, env, outputs, tmp_path: Path
    ):
        # Two same-day segments: frontmatter date ties, and the folder whose
        # name sorts FIRST holds the LATER startup. The root must be the
        # segment that actually served first (earliest startup ts), or the
        # root would flip mid-conversation and split context_served.
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(
            env,
            note_id="ses-later0001",
            source_session="uuid-later",
            folder_name="session-x1",
            startup_ts="2026-08-29T12:00:00+00:00",
            served_ids=["dec-aaaa1111"],
        )
        _seed_chain_root(
            env,
            note_id="ses-first0001",
            source_session="uuid-first",
            folder_name="session-x2",
            startup_ts="2026-08-29T08:00:00+00:00",
            served_ids=["ses-cccc3333"],
        )

        h._handle_session_start({
            "session_id": "uuid-seg9",
            "source": "resume",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        events = _buffer_events(env, "uuid-seg9")
        assert events[0]["chain_root"] == "ses-first0001"

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


class TestCallShape:
    """#175 review round 1: SessionStart latency is syscall-shaped, so the
    scan topology is pinned — startup/clear never resolve the chain, and the
    resume-path scan opens a bounded number of session folders."""

    @pytest.mark.parametrize("source", ["startup", "clear"])
    def test_startup_and_clear_never_resolve_the_chain(
        self, env, outputs, tmp_path: Path, monkeypatch, source: str
    ):
        from thinkweave.surfaces.hooks import handler as h

        calls: list = []
        monkeypatch.setattr(
            h,
            "_chain_serve_state",
            lambda *a, **kw: calls.append(a) or ("", set(), set()),
        )
        h._handle_session_start({
            "session_id": f"uuid-cs-{source}",
            "source": source,
            "cwd": str(tmp_path),
        })

        assert calls == []
        assert outputs[-1]["additional_context"] == FULL_PAYLOAD

    def test_resume_scan_opens_a_bounded_number_of_folders(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        # Decoy folders whose date-stamped names sort OLDER than the chain
        # root — far more of them than the scan budget.
        sessions = env.vault_root / "projects" / "t" / "sessions"
        for i in range(60):
            d = sessions / f"session-2019-01-01-{i:04d}"
            d.mkdir(parents=True)
            (d / "session.md").write_text(
                "---\nid: ses-decoy001\ntype: session\n---\n", encoding="utf-8"
            )

        reads: list[Path] = []
        real = h._read_session_fm

        def counting(path: Path):
            reads.append(path)
            return real(path)

        monkeypatch.setattr(h, "_read_session_fm", counting)

        h._handle_session_start({
            "session_id": "uuid-bound1",
            "source": "resume",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        # The delta still resolves correctly through the bounded scan…
        assert "dec-bbbb2222" in outputs[-1]["additional_context"]
        assert "dec-aaaa1111" not in outputs[-1]["additional_context"]
        # …and the scan never opened more folders than its budget, despite
        # 61 candidates on disk.
        assert 0 < len(reads) <= h._CHAIN_SCAN_RECENT


class TestSeamGuardUnion:
    def test_guard_runs_on_prior_and_new_ids(
        self, env, outputs, tmp_path: Path, monkeypatch
    ):
        # The agent still relies on context served earlier in the chain, so
        # the stale-twin guard must consider prior ids too, not just the
        # delta's new ones.
        captured: list[set] = []
        monkeypatch.setattr(
            "thinkweave.synthesis.memory_seam.session_guard_section",
            lambda cfg, ids: captured.append(set(ids)) or "",
        )
        from thinkweave.surfaces.hooks import handler as h

        _seed_chain_root(env, served_ids=["dec-aaaa1111", "ses-cccc3333"])
        h._handle_session_start({
            "session_id": "uuid-guard1",
            "source": "resume",
            "cwd": str(tmp_path),
            "transcript_path": _transcript_with_bridge(tmp_path),
        })

        assert captured
        assert captured[-1] >= {
            "dec-aaaa1111", "ses-cccc3333", "dec-bbbb2222",
        }
