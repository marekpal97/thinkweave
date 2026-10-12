"""OpenCode importer: parse sessions out of a synthetic SQLite store and
materialise them into a tmp vault.

The store mirrors the relevant columns of OpenCode 1.19's ``session`` /
``message`` / ``part`` tables (verified against the live store 2026-10-12);
the live database and vault are never touched.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.vault import parse_frontmatter

T0 = 1_791_837_305_000  # 2026-10-12, ms epoch


def build_store(path: Path) -> Path:
    """Write a minimal OpenCode store: two top-level sessions (one older,
    one newest) and a subagent child of the newest."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
            id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
            slug text NOT NULL, directory text NOT NULL, title text NOT NULL,
            version text NOT NULL, model text,
            time_created integer NOT NULL, time_updated integer NOT NULL,
            time_archived integer
        );
        CREATE TABLE message (
            id text PRIMARY KEY, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL,
            data text NOT NULL
        );
        CREATE TABLE part (
            id text PRIMARY KEY, message_id text NOT NULL,
            session_id text NOT NULL, time_created integer NOT NULL,
            time_updated integer NOT NULL, data text NOT NULL
        );
        """
    )
    model = json.dumps({"id": "qwen/qwen3.8-flash", "providerID": "openrouter"})

    def session(sid, parent, cwd, created):
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
            (sid, "prj", parent, "slug", cwd, f"title {sid}", "1.19.0", model,
             created, created + 60_000),
        )

    seq = iter(range(1000))

    def message(sid, role, ts, *parts):
        mid = f"msg_{next(seq):04d}"
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (mid, sid, ts, ts, json.dumps({"role": role, "time": {"created": ts}})),
        )
        for part in parts:
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"prt_{next(seq):04d}", mid, sid, ts, ts, json.dumps(part)),
            )

    def text(t, **extra):
        return {"type": "text", "text": t, **extra}

    tool = {
        "type": "tool", "tool": "thinkweave_weave_search", "callID": "c1",
        "state": {"status": "completed", "input": {"query": "x"}, "output": "noise"},
    }

    # Older session in project "alpha".
    session("ses_old", None, "/home/u/alpha", T0 - 86_400_000 * 30)
    message("ses_old", "user", T0 - 86_400_000 * 30 + 1, text("old question"))
    message("ses_old", "assistant", T0 - 86_400_000 * 30 + 2, text("old answer"))

    # Newest session in project "beta": injected context, a synthetic
    # editor reminder, a multi-step assistant reply, a tool call.
    session("ses_new", None, "/home/u/beta", T0)
    message(
        "ses_new", "user", T0 + 1,
        text("[thinkweave session context]\n## project beta ..."),
        text("<system-reminder>opened foo.py</system-reminder>", synthetic=True),
        text("Add divide() to calc.py"),
    )
    message(
        "ses_new", "assistant", T0 + 2,
        {"type": "step-start"}, {"type": "reasoning", "text": "think"},
        text("Adding it now."), tool, {"type": "step-finish"},
    )
    message("ses_new", "assistant", T0 + 3, text("Done — 6/6 passed."))
    message("ses_new", "user", T0 + 4, text("thanks"))

    # Subagent child of the newest session.
    session("ses_child", "ses_new", "/home/u/beta", T0 + 5)
    message("ses_child", "user", T0 + 6, text("delegated task"))
    message("ses_child", "assistant", T0 + 7, text("delegated result"))

    # A top-level session with no conversation text at all.
    session("ses_empty", None, "/home/u/beta", T0 - 1000)
    message("ses_empty", "assistant", T0 - 999, tool)

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return build_store(tmp_path / "opencode.db")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


class TestParseSession:
    def test_newest_top_level_by_default(self, store: Path):
        from thinkweave.acquisition.importers.opencode import parse_session

        session = parse_session(store)
        assert session is not None
        assert session.session_id == "ses_new"
        assert session.project == "beta"
        assert session.cwd == "/home/u/beta"
        assert session.model == "openrouter/qwen/qwen3.8-flash"

    def test_turns_filter_plumbing_and_merge_steps(self, store: Path):
        from thinkweave.acquisition.importers.opencode import parse_session

        session = parse_session(store, "ses_new")
        assert session.turns == [
            ("user", "Add divide() to calc.py"),
            ("assistant", "Adding it now.\n\nDone — 6/6 passed."),
            ("user", "thanks"),
        ]
        assert session.turn_count == 3

    def test_accepts_data_directory(self, store: Path):
        from thinkweave.acquisition.importers.opencode import parse_session

        session = parse_session(store.parent, "ses_old")
        assert session is not None and session.project == "alpha"

    def test_missing_and_empty(self, store: Path, tmp_path: Path):
        from thinkweave.acquisition.importers.opencode import parse_session

        assert parse_session(store, "ses_nope") is None
        assert parse_session(store, "ses_empty") is None
        assert parse_session(tmp_path / "absent.db") is None

    def test_store_is_opened_read_only(self, store: Path):
        from thinkweave.acquisition.importers.opencode import _connect

        conn = _connect(store)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM session")
        conn.close()


class TestImportOpenCode:
    def test_materializes_top_level_skips_children(self, config: Config, store: Path):
        from thinkweave.acquisition.importers.opencode import import_opencode

        stats = import_opencode(config, sessions_root=store)
        assert stats["errors"] == []
        assert stats["discovered"] == 4
        assert stats["materialized"] == 2
        assert stats["skipped_subagent"] == 1
        assert stats["skipped_no_content"] == 1
        assert set(stats["per_project"]) == {"alpha", "beta"}

        notes = sorted(config.vault_root.rglob("*.md"))
        fms = [parse_frontmatter(p.read_text(encoding="utf-8")) for p in notes]
        by_sid = {fm.get("opencode_session_id"): (fm, body) for fm, body in fms}
        fm, body = by_sid["ses_new"]
        assert fm["imported_from"] == "opencode"
        assert fm["user_turn_count"] == 2
        assert "processed" not in fm
        assert "### Assistant (turn 1)" in body
        assert "session context" not in body
        assert "delegated" not in body
        assert "ses_child" not in by_sid

    def test_idempotent(self, config: Config, store: Path):
        from thinkweave.acquisition.importers.opencode import import_opencode

        import_opencode(config, sessions_root=store)
        again = import_opencode(config, sessions_root=store)
        assert again["materialized"] == 0
        assert again["skipped_already_imported"] == 2

    def test_dry_run_writes_nothing(self, config: Config, store: Path):
        from thinkweave.acquisition.importers.opencode import import_opencode

        stats = import_opencode(config, sessions_root=store, dry_run=True)
        assert stats["materialized"] == 2
        assert not config.vault_root.exists() or not list(config.vault_root.rglob("*.md"))

    def test_filters(self, config: Config, store: Path):
        from thinkweave.acquisition.importers.opencode import import_opencode

        since = import_opencode(config, db_path=store, dry_run=True, since="2026-10-01")
        assert since["skipped_since"] == 1 and since["materialized"] == 1
        proj = import_opencode(config, db_path=store, dry_run=True, project_filter="alpha")
        assert proj["skipped_filter"] == 1 and proj["materialized"] == 1
        capped = import_opencode(config, db_path=store, dry_run=True, limit=1)
        assert capped["materialized"] == 1

    def test_db_path_overrides_root_and_missing_store_errors(
        self, config: Config, store: Path, tmp_path: Path
    ):
        from thinkweave.acquisition.importers.opencode import import_opencode

        stats = import_opencode(
            config, sessions_root=tmp_path / "nowhere.db", db_path=store, dry_run=True
        )
        assert stats["materialized"] == 2
        missing = import_opencode(config, sessions_root=tmp_path / "nowhere.db")
        assert missing["errors"] and missing["materialized"] == 0


class TestCli:
    def test_import_opencode_passes_db(self, tmp_path: Path, store: Path, monkeypatch, capsys):
        """`weave import opencode --db PATH` reaches the importer as db_path
        once the profile names it (wired here; the live row is the
        profile's job)."""
        import dataclasses

        from thinkweave.core import harness
        from thinkweave.surfaces.cli import index as cli_index
        from thinkweave.surfaces.cli.parser import build_parser

        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        base = harness.PROFILES["opencode"]
        monkeypatch.setitem(
            harness.PROFILES,
            "opencode",
            lambda: dataclasses.replace(
                base(),
                transcript_importer="thinkweave.acquisition.importers.opencode:import_opencode",
            ),
        )
        args = build_parser().parse_args(
            ["import", "opencode", "--db", str(store), "--dry-run"]
        )
        cli_index.cmd_import(args)
        out = capsys.readouterr().out
        assert "Would materialize: 2 session(s) across 2 project(s)" in out
        assert "skipped_subagent=1" in out
        assert not list((tmp_path / "vault").rglob("*.md"))


class TestDigest:
    """``opencode_digest`` reads a session addressed as ``<store>#<id>`` —
    the shim's prompt-envelope transcript path (no per-session file)."""

    def _add_worker_session(self, store: Path) -> None:
        conn = sqlite3.connect(store)
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
            ("ses_w", "prj", None, "slug", "/home/u/beta", "t", "1.19.0", "{}",
             T0, T0 + 60_000),
        )
        rows = [
            ("user", {"type": "text", "text": "Work tsk-12345678: add cube"}),
            ("assistant", {"type": "tool", "tool": "edit", "callID": "c1", "state": {
                "status": "completed", "input": {"filePath": "/home/u/beta/calc.py"},
                "output": "ok"}}),
            ("assistant", {"type": "tool", "tool": "bash", "callID": "c2", "state": {
                "status": "completed",
                "input": {"command": 'git add calc.py && git commit -m "cube"'},
                "output": "[master 7d96da2] cube\n 1 file changed\n"}}),
            ("assistant", {"type": "tool", "tool": "bash", "callID": "c3", "state": {
                "status": "error", "input": {"command": "false"}, "error": "exit 1"}}),
        ]
        for n, (role, part) in enumerate(rows):
            ts = T0 + 10 + n
            data = {"role": role}
            if role == "assistant":
                data["modelID"] = "anthropic/claude-haiku-5.5"
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?,?)",
                (f"msg_w{n}", "ses_w", ts, ts, json.dumps(data)),
            )
            conn.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"prt_w{n}", f"msg_w{n}", "ses_w", ts, ts, json.dumps(part)),
            )
        conn.commit()
        conn.close()

    def test_digest_reads_paths_commits_tools_and_errors(self, store: Path):
        from thinkweave.operations.tasks import opencode_digest

        self._add_worker_session(store)
        digest = opencode_digest(Path(f"{store}#ses_w"), "", "")
        assert digest.asked == "Work tsk-12345678: add cube"
        assert digest.version == "1.19.0"
        assert digest.model == "anthropic/claude-haiku-5.5"
        assert digest.paths == ("calc.py",)
        assert digest.commits == ("7d96da2",)
        assert digest.tools == {"edit": 1, "bash": 2}
        assert digest.tool_errors == 1
        assert digest.gaps == ()

    def test_unknown_session_and_bad_address_are_gaps(self, store: Path):
        from thinkweave.operations.tasks import opencode_digest

        assert opencode_digest(Path(f"{store}#ses_nope"), "", "").gaps
        assert opencode_digest(store, "", "").gaps
