"""``weave health`` — deterministic system-health collector (#120).

Seams under test: the collector's return dict (= the ``--json`` contract
``/brief`` reads), the CLI exit code, and the ``context_served`` CHECK.
Fixtures are hand-written cron text / maintenance lines / queue files;
expected values are hand-computed from the issue's criteria.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from thinkweave.core.schemas import NoteType
from thinkweave.operations import health

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

CRON = """\
PATH=/usr/bin
# a comment
30 0 * * * /usr/bin/flock -n /tmp/x.lock -c 'cd /repo && claude -p "/dream" >> {logdir}/dream.log 2>&1'
15 */4 * * * cd /repo && weave index --embed --only-new >> {logdir}/embed-warm.log 2>&1
0 12 * * * claude -p "/drain --source-type paper --limit 5" >> {logdir}/research.log 2>&1
20 12 * * * claude -p "/drain --source-type repo --limit 5" >> {logdir}/research.log 2>&1
17 */4 * * * claude -p "/discover --strategy rss_poll --source-type news" >> {logdir}/pull_news.log 2>&1
"""


def _touch(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    import os

    os.utime(path, (when.timestamp(), when.timestamp()))


def _maint(handle, when: datetime, cycle="dream-20260823-000000-abcdef") -> None:
    p = handle.config.vault_root / ".weave" / "maintenance.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": when.isoformat(), "cycle_id": cycle}) + "\n")


def _digest(handle, day: str) -> None:
    d = handle.config.vault_root / "digests"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}-concept.md").write_text("---\ntype: digest\n---\n", encoding="utf-8")


@pytest.fixture
def healthy(vault_factory, tmp_path):
    handle = vault_factory()
    logdir = tmp_path / "logs"
    cron = CRON.format(logdir=logdir)
    _maint(handle, NOW - timedelta(hours=12))
    _touch(logdir / "dream.log", NOW - timedelta(hours=12))
    _touch(logdir / "embed-warm.log", NOW - timedelta(hours=2))
    _touch(logdir / "research.log", NOW - timedelta(hours=20))
    _touch(logdir / "pull_news.log", NOW - timedelta(hours=1))
    _digest(handle, "2026-08-22")
    return handle, cron, logdir


class TestCollector:
    def test_healthy_is_ok_and_has_contract_keys(self, healthy):
        handle, cron, _ = healthy
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        # AC3: stable key set — the contract /brief (#170) reads.
        assert set(report) == {
            "ok", "checked_at", "flags", "advisories", "jobs", "jobs_note",
            "queues", "hooks", "digest",
        }
        assert set(report["jobs"][0]) == {
            "id", "name", "cadence", "cadence_seconds", "last_run", "evidence",
            "stale", "missing",
        }
        assert set(report["digest"]) == {"latest", "age_days", "stale"}
        assert set(report["hooks"]) == {"recent_errors", "last_error"}
        assert report["ok"] is True
        assert report["flags"] == [] and report["advisories"] == []
        assert report["jobs_note"] is None

    def test_job_ids_names_cadence_and_evidence(self, healthy):
        handle, cron, _ = healthy
        jobs = {j["id"]: j for j in health.collect(
            handle.config, now=NOW, crontab_text=cron)["jobs"]}
        assert set(jobs) == {
            "30 0 * * * /dream", "15 */4 * * * weave index",
            "0 12 * * * /drain paper", "20 12 * * * /drain repo",
            "17 */4 * * * /discover news",
        }
        assert jobs["30 0 * * * /dream"]["cadence_seconds"] == 86400
        assert jobs["15 */4 * * * weave index"]["cadence_seconds"] == 4 * 3600
        assert jobs["15 */4 * * * weave index"]["evidence"] == "log"
        # Shared log: both drains append to research.log, so the mtime is
        # evidence for neither individually — say so.
        assert jobs["0 12 * * * /drain paper"]["evidence"] == "log(shared)"
        assert jobs["20 12 * * * /drain repo"]["evidence"] == "log(shared)"

    def test_maintenance_beats_log_even_when_older(self, healthy):
        # The log is touched on every fire (crashes included); the
        # maintenance line only on completion — the completion signal wins.
        handle, cron, logdir = healthy
        _touch(logdir / "dream.log", NOW - timedelta(minutes=5))
        job = next(j for j in health.collect(handle.config, now=NOW, crontab_text=cron)["jobs"]
                   if j["name"] == "/dream")
        assert job["evidence"] == "maintenance"
        assert job["last_run"] == (NOW - timedelta(hours=12)).isoformat()

    def test_cadence_heuristic_shapes(self):
        assert health._cadence_seconds("0 8 * * 1-5") == 86400
        assert health._cadence_seconds("0 8 * * 1,3") == 86400
        assert health._cadence_seconds("0 9-17/2 * * *") == 2 * 3600
        assert health._cadence_seconds("0 7,19 * * *") == 43200
        assert health._cadence_seconds("30 14 * * 0") == 7 * 86400

    def test_stale_job_uses_factor(self, healthy):
        # AC2: 4h cadence × 1.5 = 6h. 7h-old log → stale; 5h → fine.
        handle, cron, logdir = healthy
        _touch(logdir / "embed-warm.log", NOW - timedelta(hours=7))
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        job = next(j for j in report["jobs"] if j["name"] == "weave index")
        assert job["stale"] is True and job["missing"] is False
        assert report["ok"] is False
        assert any("weave index" in f for f in report["flags"])

        handle.config.health_stale_factor = 2.0  # 4h × 2 = 8h > 7h
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert next(j for j in report["jobs"] if j["name"] == "weave index")["stale"] is False

    def test_missing_evidence_is_distinct_from_stale(self, healthy):
        handle, cron, logdir = healthy
        (logdir / "research.log").unlink()
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        job = next(j for j in report["jobs"] if j["name"] == "/drain paper")
        assert job == {
            "id": "0 12 * * * /drain paper",
            "name": "/drain paper",
            "cadence": "0 12 * * *",
            "cadence_seconds": 86400,
            "last_run": None,
            "evidence": None,
            "stale": False,
            "missing": True,
        }
        assert report["ok"] is False

    def test_queue_depth_and_backlog(self, healthy):
        from thinkweave.acquisition.sources.queue import Queue

        handle, cron, _ = healthy
        q = Queue.for_source_type("paper", handle.config.vault_root)
        q.enqueue({"url": "u1", "enqueued_at": (NOW - timedelta(days=10)).isoformat()})
        q.enqueue({"url": "u2", "enqueued_at": (NOW - timedelta(days=1)).isoformat()})
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        paper = next(q for q in report["queues"] if q["source_type"] == "paper")
        assert paper == {"source_type": "paper", "depth": 2, "backlog": 1}
        # Backlog is a standing condition on a busy vault — advisory, never exit 1.
        assert report["ok"] is True
        assert report["flags"] == []
        assert any("paper" in a for a in report["advisories"])

    def test_queue_lanes_come_from_sources_config_not_fossil_files(self, healthy):
        handle, cron, _ = healthy
        qdir = handle.config.vault_root / ".weave" / "queues"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "paper.done.jsonl").write_text('{"url": "x"}\n', encoding="utf-8")
        lanes = {q["source_type"] for q in health.collect(
            handle.config, now=NOW, crontab_text=cron)["queues"]}
        assert "paper.done" not in lanes and "paper" in lanes

    def test_hook_errors_counted(self, healthy):
        handle, cron, _ = healthy
        ts = (NOW - timedelta(hours=1)).isoformat()
        old = (NOW - timedelta(days=3)).isoformat()
        (handle.config.weave_dir / "hooks.log").write_text(
            f"[{old}] stop: boom\nTraceback (most recent call last):\n  x\n\n"
            f"[{ts}] prompt_time_enrichment: deadline miss for session s\n"
            f"[{ts}] stop: CalledProcessError: cmd\nfailed\nTraceback (most recent call last):\n  y\n\n",
            encoding="utf-8",
        )
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert report["hooks"] == {
            "recent_errors": 1,
            "last_error": f"[{ts}] stop: CalledProcessError: cmd",
        }
        assert report["ok"] is False

    def test_digest_freshness(self, healthy):
        handle, cron, _ = healthy
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert report["digest"] == {"latest": "2026-08-22", "age_days": 1, "stale": False}
        # no newer digest; push the clock 2 days on → age 3d > 1.5
        report = health.collect(handle.config, now=NOW + timedelta(days=2), crontab_text=cron)
        assert report["digest"]["stale"] is True
        assert report["ok"] is False

    def test_no_digest_renders_none(self, healthy):
        import shutil

        handle, cron, _ = healthy
        shutil.rmtree(handle.config.vault_root / "digests")
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert report["digest"] == {"latest": None, "age_days": None, "stale": True}
        assert report["flags"] == ["digest: none found"]

    def test_no_crontab_is_not_green(self, healthy, monkeypatch):
        handle, _, _ = healthy
        monkeypatch.setattr(health, "_read_crontab", lambda: None)
        report = health.collect(handle.config, now=NOW)
        assert report["jobs"] == []
        assert report["jobs_note"] == "job layer not inspectable on this platform (no crontab)"
        assert report["ok"] is False


class TestCli:
    def _run(self, handle, cron, *, as_json: bool, capsys, monkeypatch):
        import argparse

        from thinkweave.surfaces.cli.health import cmd_health

        monkeypatch.setattr(health, "_read_crontab", lambda: cron)
        monkeypatch.setattr(
            "thinkweave.surfaces.cli.health.load_config", lambda: handle.config
        )
        with pytest.raises(SystemExit) as exc:
            cmd_health(argparse.Namespace(json=as_json))
        return exc.value.code, capsys.readouterr().out

    def test_exit_0_healthy_json(self, healthy, capsys, monkeypatch):
        handle, cron, _ = healthy
        monkeypatch.setattr(health, "_now", lambda: NOW)
        code, out = self._run(handle, cron, as_json=True, capsys=capsys, monkeypatch=monkeypatch)
        assert code == 0
        data = json.loads(out)
        assert data["ok"] is True and len(data["jobs"]) == 5

    def test_exit_1_flagged_table(self, healthy, capsys, monkeypatch):
        handle, cron, logdir = healthy
        monkeypatch.setattr(health, "_now", lambda: NOW)
        (logdir / "research.log").unlink()
        code, out = self._run(handle, cron, as_json=False, capsys=capsys, monkeypatch=monkeypatch)
        assert code == 1
        assert "/drain paper" in out and "missing" in out
        assert "digest" in out.lower() and "hook" in out.lower()


class TestContextServedSources:
    """AC6 — ``source IN (…, 'brief', 'learn')`` plus migration of an old DB."""

    def test_brief_and_learn_rows_project_from_retrieval_log(self, vault_factory):
        handle = vault_factory()
        cfg, vm = handle.config, handle.vault
        sess = vm.create_note(NoteType.SESSION, "S", body="## Summary\nx\n", project="p")
        lines = [
            {"ts": "2026-08-23T00:00:00Z", "type": "retrieval", "tool": "brief", "returned_ids": ["n-b"]},
            {"ts": "2026-08-23T00:00:00Z", "type": "retrieval", "tool": "learn", "returned_ids": ["n-l"]},
        ]
        (sess.parent / "retrieval_log.jsonl").write_text(
            "".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8"
        )
        handle.indexed()
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=cfg)
        try:
            rows = sorted(
                tuple(r) for r in idx.db.execute(
                    "SELECT note_id, source FROM context_served")
            )
        finally:
            idx.close()
        assert rows == [("n-b", "brief"), ("n-l", "learn")]

    def test_pre_brief_table_with_rows_is_migrated(self, vault_factory):
        from thinkweave.core.indexer import Indexer

        handle = vault_factory()
        cfg, vm = handle.config, handle.vault
        sess = vm.create_note(NoteType.SESSION, "S", body="## Summary\nx\n", project="p")
        (sess.parent / "retrieval_log.jsonl").write_text(
            json.dumps({"ts": "2026-08-23T00:00:00Z", "type": "startup",
                        "returned_ids": ["n-seeded"]}) + "\n",
            encoding="utf-8",
        )
        handle.indexed()
        idx = Indexer(config=cfg)
        idx.db.execute("DROP TABLE context_served")
        idx.db.execute(
            "CREATE TABLE context_served ("
            " session_id TEXT NOT NULL, note_id TEXT NOT NULL,"
            " source TEXT NOT NULL CHECK(source IN "
            "  ('startup', 'onthefly', 'prompttime', 'loop-prime', 'codex-startup')),"
            " ts TEXT, PRIMARY KEY (session_id, note_id, source))"
        )
        idx.db.execute(
            "INSERT INTO context_served VALUES ('ses-old', 'n-old', 'startup', '')"
        )
        idx.db.commit()
        idx.close()

        idx = Indexer(config=cfg)
        try:
            idx._init_schema()
            rows = [r["note_id"] for r in idx.db.execute("SELECT note_id FROM context_served")]
            # Existing rows are re-projected from retrieval_log (the table is
            # derived); the hand-inserted orphan has no log line so it is gone.
            assert rows == ["n-seeded"]
            for src in ("brief", "learn"):
                idx.db.execute(
                    "INSERT INTO context_served VALUES (?, ?, ?, '')",
                    ("ses-x", f"n-{src}", src),
                )
        finally:
            idx.close()
