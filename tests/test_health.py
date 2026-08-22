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
    _digest(handle, "2026-08-22")
    return handle, cron, logdir


class TestCollector:
    def test_healthy_is_ok_and_has_contract_keys(self, healthy):
        handle, cron, _ = healthy
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        # AC3: stable key set — the contract /brief (#170) reads.
        assert set(report) == {
            "ok", "checked_at", "flags", "jobs", "queues", "hooks", "digest"
        }
        assert set(report["jobs"][0]) == {
            "name", "cadence", "cadence_seconds", "last_run", "evidence",
            "stale", "missing",
        }
        assert set(report["digest"]) == {"latest", "age_days", "stale"}
        assert set(report["hooks"]) == {"recent_errors", "last_error"}
        assert report["ok"] is True
        assert report["flags"] == []

    def test_job_names_and_cadence_seconds(self, healthy):
        handle, cron, _ = healthy
        jobs = {j["name"]: j for j in health.collect(
            handle.config, now=NOW, crontab_text=cron)["jobs"]}
        assert set(jobs) == {"/dream", "weave index", "/drain paper"}
        assert jobs["/dream"]["cadence_seconds"] == 86400
        assert jobs["weave index"]["cadence_seconds"] == 4 * 3600
        assert jobs["/dream"]["evidence"] == "maintenance"
        assert jobs["weave index"]["evidence"] == "log"

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
        assert report["ok"] is False
        assert any("paper" in f for f in report["flags"])

    def test_hook_errors_counted(self, healthy):
        handle, cron, _ = healthy
        ts = (NOW - timedelta(hours=1)).isoformat()
        old = (NOW - timedelta(days=3)).isoformat()
        (handle.config.weave_dir / "hooks.log").write_text(
            f"[{old}] stop: boom\nTraceback (most recent call last):\n  x\n\n"
            f"[{ts}] prompt_time_enrichment: deadline miss for session s\n"
            f"[{ts}] stop: FileNotFoundError\nTraceback (most recent call last):\n  y\n\n",
            encoding="utf-8",
        )
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert report["hooks"] == {
            "recent_errors": 1,
            "last_error": f"[{ts}] stop: FileNotFoundError",
        }
        assert report["ok"] is False

    def test_digest_freshness(self, healthy):
        handle, cron, _ = healthy
        report = health.collect(handle.config, now=NOW, crontab_text=cron)
        assert report["digest"] == {"latest": "2026-08-22", "age_days": 1, "stale": False}
        _digest(handle, "2026-08-22")  # no newer one; push clock 3 days on
        report = health.collect(handle.config, now=NOW + timedelta(days=2), crontab_text=cron)
        assert report["digest"]["stale"] is True
        assert report["ok"] is False

    def test_no_crontab_yields_no_jobs(self, healthy):
        handle, _, _ = healthy
        report = health.collect(handle.config, now=NOW, crontab_text="")
        assert report["jobs"] == []


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
        assert data["ok"] is True and len(data["jobs"]) == 3

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

    def _rows(self, cfg):
        from thinkweave.core.indexer import Indexer

        idx = Indexer(config=cfg)
        try:
            idx._init_schema()
            return [
                r["note_id"]
                for r in idx.db.execute("SELECT note_id FROM context_served")
            ], idx
        except Exception:
            idx.close()
            raise

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

        rows, idx = self._rows(cfg)
        try:
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
