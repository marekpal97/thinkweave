"""Tests for slice 2 of RLVR — buffer split + SessionStart capture.

The Stop-time ``archive_buffer`` partitions a session's JSONL buffer into:

- ``events.jsonl``  — action + prompt + untyped events (the legacy stream)
- ``retrieval_log.jsonl`` — ``type: retrieval`` and ``type: startup`` events

Pre-existing buffer shapes (no ``type`` field) roundtrip into ``events.jsonl``
unchanged. The sibling retrieval log file is created only when needed.

``_handle_session_start`` is also covered here — it must record a single
``type: startup`` event with the returned_ids extracted from the SessionStart
payload and a rough token estimate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# archive_buffer — pure FS partitioning
# ---------------------------------------------------------------------------


class TestArchiveBufferPartition:
    def test_no_retrieval_events_writes_only_events_jsonl(self, tmp_path: Path):
        from thinkweave.core.buffer import archive_buffer

        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf = buf_dir / "ses-x.jsonl"
        buf.write_text(
            '{"ts":"14:00","tool":"Edit","file":"a.py"}\n'
            '{"ts":"14:05","tool":"Bash","command":"pytest"}\n',
            encoding="utf-8",
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-x", session_dir)

        events = session_dir / "events.jsonl"
        retrieval = session_dir / "retrieval_log.jsonl"
        assert events.exists()
        assert not retrieval.exists()  # never created if no retrieval lines
        assert not buf.exists()
        # Both action lines preserved.
        action_lines = events.read_text(encoding="utf-8").splitlines()
        assert len(action_lines) == 2

    def test_mixed_buffer_partitions_correctly(self, tmp_path: Path):
        from thinkweave.core.buffer import archive_buffer

        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf = buf_dir / "ses-y.jsonl"
        buf.write_text(
            '{"ts":"14:00","tool":"Edit","file":"a.py"}\n'
            '{"ts":"14:01","type":"prompt","text":"hi"}\n'
            '{"ts":"14:02","type":"startup","returned_ids":["n-aaa111aa"],"token_est":1234}\n'
            '{"ts":"14:03","type":"retrieval","tool":"mcp__thinkweave__weave_search","returned_ids":["n-bbb222bb"]}\n'
            '{"ts":"14:04","tool":"Bash","command":"pytest"}\n',
            encoding="utf-8",
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-y", session_dir)

        events = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        retrievals = (session_dir / "retrieval_log.jsonl").read_text(encoding="utf-8").splitlines()

        # Action: Edit + prompt + Bash (3 lines). Type "prompt" is NOT retrieval.
        assert len(events) == 3
        assert all(
            json.loads(line).get("type") != "retrieval"
            and json.loads(line).get("type") != "startup"
            for line in events
        )

        # Retrieval: startup + retrieval (2 lines)
        assert len(retrievals) == 2
        types = [json.loads(line)["type"] for line in retrievals]
        assert types == ["startup", "retrieval"]
        # Buffer cleaned up.
        assert not buf.exists()

    def test_malformed_line_falls_to_events(self, tmp_path: Path):
        from thinkweave.core.buffer import archive_buffer

        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf = buf_dir / "ses-z.jsonl"
        buf.write_text(
            'not valid json\n'
            '{"type":"retrieval","tool":"x","returned_ids":[]}\n',
            encoding="utf-8",
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-z", session_dir)

        events = (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        retrievals = (session_dir / "retrieval_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert events == ["not valid json"]
        assert len(retrievals) == 1

    def test_idempotent_rerun_with_existing_archives(self, tmp_path: Path):
        # Catch-up wraps may invoke archive a second time; once the buffer
        # is gone, the call must be a graceful no-op (not crash, not duplicate).
        from thinkweave.core.buffer import archive_buffer

        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        existing_events = session_dir / "events.jsonl"
        existing_events.write_text('{"ts":"prior"}\n', encoding="utf-8")

        archive_buffer(tmp_path, "ses-gone", session_dir)

        # Existing archive unchanged.
        assert existing_events.read_text(encoding="utf-8") == '{"ts":"prior"}\n'

    def test_only_retrieval_events_touches_empty_events_file(self, tmp_path: Path):
        # An agent doing pure research with zero file edits should still leave
        # an events.jsonl (possibly empty) so prune.py doesn't treat the
        # session as an orphan stub on the first finalize pass.
        from thinkweave.core.buffer import archive_buffer

        buf_dir = tmp_path / "buffer"
        buf_dir.mkdir()
        buf = buf_dir / "ses-r.jsonl"
        buf.write_text(
            '{"type":"retrieval","tool":"mcp__thinkweave__weave_search","returned_ids":["n-aaaabbbb"]}\n',
            encoding="utf-8",
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()

        archive_buffer(tmp_path, "ses-r", session_dir)

        events = session_dir / "events.jsonl"
        retrievals = session_dir / "retrieval_log.jsonl"
        assert events.exists()  # touched
        assert events.read_text(encoding="utf-8") == ""
        assert retrievals.exists()
        assert len(retrievals.read_text(encoding="utf-8").splitlines()) == 1


# ---------------------------------------------------------------------------
# SessionStart capture
# ---------------------------------------------------------------------------


class TestSessionStartCapture:
    def test_writes_startup_event_to_buffer(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        monkeypatch.setenv("THINKWEAVE_PROJECT", "t")

        from thinkweave.surfaces.hooks import handler as h

        # Stub the payload builder so the test doesn't need a real vault.
        fake_payload = (
            "# Header\n\n"
            "- [n-aaa111aa] Some title — context\n"
            "- (`dec-bbb222bb`) prior decision\n"
            "- some prose without an id\n"
        )
        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: fake_payload,
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        session_id = "ses-startup1"
        h._handle_session_start({"session_id": session_id, "cwd": str(tmp_path)})

        from thinkweave.core.config import load_config

        cfg = load_config()
        buf_path = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
        assert buf_path.exists()
        lines = buf_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["type"] == "startup"
        assert event["returned_ids"] == ["n-aaa111aa", "dec-bbb222bb"]
        assert event["token_est"] == len(fake_payload) // 4

    def test_empty_payload_still_records_zero_ids(self, tmp_path: Path, monkeypatch):
        # Cold-vault SessionStart should still leave a marker — n_retrievals_onthefly
        # and startup_token_est=0 is itself a finding the RLVR row will use.
        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: "",
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)

        session_id = "ses-cold"
        h._handle_session_start({"session_id": session_id, "cwd": str(tmp_path)})

        from thinkweave.core.config import load_config

        cfg = load_config()
        buf_path = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
        assert buf_path.exists()
        event = json.loads(buf_path.read_text(encoding="utf-8").splitlines()[0])
        assert event["type"] == "startup"
        assert event["returned_ids"] == []
        assert event["token_est"] == 0

    def test_no_session_id_skips_capture(self, tmp_path: Path, monkeypatch):
        # Defensive: hook input without a session_id can't be buffered.
        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        # Clear env var so the missing session_id can't be backfilled.
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        from thinkweave.surfaces.hooks import handler as h

        monkeypatch.setattr(
            "thinkweave.retrieval.context.build_project_context",
            lambda *a, **kw: "[n-aaaabbbb]",
        )
        monkeypatch.setattr(h, "_output", lambda *a, **kw: None)
        # Capture buffer-write attempts.
        called = []
        monkeypatch.setattr(h, "_buffer_event", lambda *a, **kw: called.append(a))

        h._handle_session_start({"cwd": str(tmp_path)})  # no session_id

        assert called == []
