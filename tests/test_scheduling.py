"""Tests for the cross-platform scheduler (registry + cron + Task Scheduler).

All runnable on Linux CI: the Windows backend is exercised by asserting the
``schtasks`` argv it *builds*, with execution mocked — no Windows host
needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.scheduling import (
    CrontabBackend,
    TaskSchedulerBackend,
    cron_to_schtasks,
    load_jobs,
    select_backend,
)
from thinkweave.scheduling.cron import FENCE_END, FENCE_START, _splice
from thinkweave.scheduling.registry import ScheduledJob, _parse, resolve_command


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(vault_root=tmp_path / "vault")


# --------------------------------------------------------------------------- #
# cron_to_schtasks translation
# --------------------------------------------------------------------------- #


class TestCronToSchtasks:
    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("0 3 * * *", ["/SC", "DAILY", "/ST", "03:00"]),
            ("0 8 * * *", ["/SC", "DAILY", "/ST", "08:00"]),
            ("15 */4 * * *", ["/SC", "HOURLY", "/MO", "4", "/ST", "00:15"]),
            ("0 */2 * * *", ["/SC", "HOURLY", "/MO", "2", "/ST", "00:00"]),
            ("*/15 * * * *", ["/SC", "MINUTE", "/MO", "15"]),
            ("0 4 * * 0", ["/SC", "WEEKLY", "/D", "SUN", "/ST", "04:00"]),
            ("30 6 * * 1", ["/SC", "WEEKLY", "/D", "MON", "/ST", "06:30"]),
            ("0 4 * * 7", ["/SC", "WEEKLY", "/D", "SUN", "/ST", "04:00"]),
        ],
    )
    def test_supported(self, expr, expected):
        assert cron_to_schtasks(expr) == expected

    @pytest.mark.parametrize(
        "expr",
        [
            "bad",  # not 5 fields
            "0 3 1 * *",  # day-of-month constraint
            "0 3 * 6 *",  # month constraint
            "*/13 7 * * *",  # minute-step with concrete hour
            "0 0 * * 1-5",  # day-of-week range
            "0,30 * * * *",  # minute list
            "15 */4 * * 1",  # hour-step with concrete dow
        ],
    )
    def test_unsupported_raises(self, expr):
        with pytest.raises(ValueError):
            cron_to_schtasks(expr)


# --------------------------------------------------------------------------- #
# registry parsing
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_parse_full_job(self):
        raw = {
            "jobs": {
                "dream": {
                    "cadence": "0 3 * * *",
                    "command": "claude -p /dream",
                    "runner": "direct",
                    "env": ["ANTHROPIC_API_KEY"],
                    "log": "dream.log",
                    "enabled": True,
                }
            }
        }
        jobs = _parse(raw)
        job = jobs["dream"]
        assert job.cadence == "0 3 * * *"
        assert job.command == "claude -p /dream"
        assert job.runner == "direct"
        assert job.env == ("ANTHROPIC_API_KEY",)
        assert job.log == "dream.log"
        assert job.enabled is True

    def test_defaults(self):
        jobs = _parse({"jobs": {"x": {"cadence": "0 3 * * *", "command": "weave foo"}}})
        job = jobs["x"]
        assert job.runner == "uv"  # default
        assert job.env == ()
        assert job.log is None
        assert job.enabled is True

    def test_enabled_false_honored(self):
        jobs = _parse(
            {"jobs": {"x": {"cadence": "0 3 * * *", "command": "weave foo", "enabled": False}}}
        )
        assert jobs["x"].enabled is False

    def test_serialize_parsed_and_defaults_false(self):
        jobs = _parse(
            {
                "jobs": {
                    "locked": {
                        "cadence": "0 3 * * *",
                        "command": "claude -p /dream",
                        "serialize": True,
                    },
                    "plain": {"cadence": "0 4 * * *", "command": "weave foo"},
                }
            }
        )
        assert jobs["locked"].serialize is True
        assert jobs["plain"].serialize is False

    def test_entry_missing_command_skipped(self):
        jobs = _parse({"jobs": {"x": {"cadence": "0 3 * * *"}}})
        assert "x" not in jobs

    def test_missing_file_yields_empty(self, config):
        assert load_jobs(config) == {}

    def test_load_from_template(self, config):
        # The shipped vault template should parse cleanly with the dream +
        # embeddings jobs enabled by default.
        pkg = Path(__file__).resolve().parents[1] / "src" / "thinkweave"
        template = pkg / "vault_templates" / "config" / "scheduling.yaml"
        jobs = load_jobs(config, path=template)
        assert "dream" in jobs and jobs["dream"].enabled
        assert "embeddings-keepwarm" in jobs and jobs["embeddings-keepwarm"].enabled
        assert jobs["dream"].env == ("ANTHROPIC_API_KEY",)
        assert jobs["embeddings-keepwarm"].env == ("OPENAI_API_KEY",)
        # /dream must never overlap itself (SQLite index race) — the template
        # ships it serialized; nothing else needs the lock.
        assert jobs["dream"].serialize is True
        assert jobs["embeddings-keepwarm"].serialize is False


# --------------------------------------------------------------------------- #
# resolve_command
# --------------------------------------------------------------------------- #


class TestResolveCommand:
    def test_direct_resolves_claude(self, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: "/abs/claude" if name == "claude" else None,
        )
        job = ScheduledJob("dream", "0 3 * * *", "claude -p /dream", runner="direct")
        # headless `claude -p` jobs get an unattended permission grant appended
        assert resolve_command(job) == "/abs/claude -p /dream --dangerously-skip-permissions"

    def test_uv_resolves_weave(self, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: "/abs/weave" if name == "weave" else None,
        )
        job = ScheduledJob("x", "0 3 * * *", "weave index --embed", runner="uv")
        assert resolve_command(job) == "/abs/weave index --embed"

    def test_uv_falls_back_to_uv_run(self, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which", lambda name: None
        )
        job = ScheduledJob("x", "0 3 * * *", "weave index --embed", runner="uv")
        out = resolve_command(job, repo_root=Path("/repo"))
        assert out == "uv run --project /repo weave index --embed"

    def test_direct_namespaces_skill_under_plugin_route(
        self, monkeypatch, tmp_path
    ):
        # Plugin route active → the `-p` skill token renders namespaced
        # (plugin commands have no bare-name aliasing).
        import json

        from thinkweave.core import plugin_route

        manifest = tmp_path / "installed_plugins.json"
        manifest.write_text(
            json.dumps({"version": 2, "plugins": {"thinkweave@mp": []}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(plugin_route, "_INSTALLED_PLUGINS", manifest)
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: "/abs/claude" if name == "claude" else None,
        )
        job = ScheduledJob(
            "dream",
            "0 3 * * *",
            "claude --model sonnet -p /dream --dangerously-skip-permissions",
            runner="direct",
        )
        out = resolve_command(job)
        assert out == (
            "/abs/claude --model sonnet -p /thinkweave:dream"
            " --dangerously-skip-permissions"
        )

    def test_uv_jobs_unaffected_by_plugin_route(self, monkeypatch, tmp_path):
        import json

        from thinkweave.core import plugin_route

        manifest = tmp_path / "installed_plugins.json"
        manifest.write_text(
            json.dumps({"version": 2, "plugins": {"thinkweave@mp": []}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(plugin_route, "_INSTALLED_PLUGINS", manifest)
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: "/abs/weave" if name == "weave" else None,
        )
        job = ScheduledJob("x", "0 3 * * *", "weave index --embed", runner="uv")
        assert resolve_command(job) == "/abs/weave index --embed"


# --------------------------------------------------------------------------- #
# CrontabBackend
# --------------------------------------------------------------------------- #


class TestCrontabBackend:
    def _jobs(self):
        return [
            ScheduledJob(
                "dream", "0 3 * * *", "claude -p /dream", runner="direct",
                env=("ANTHROPIC_API_KEY",), log="dream.log",
            ),
            ScheduledJob(
                "embeddings", "15 */4 * * *", "weave index --embed --only-new",
                runner="uv", env=("OPENAI_API_KEY",), log="embed-warm.log",
            ),
            ScheduledJob(
                "news", "0 */2 * * *", "claude -p /news", runner="direct",
                log="news.log", enabled=False,
            ),
        ]

    def test_render_reproduces_crontab_semantics(self, config, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: f"/abs/{name}",
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir", lambda: Path("/cache/pm")
        )
        block = CrontabBackend(config).render(self._jobs())
        assert block.startswith(FENCE_START)
        assert block.rstrip().endswith(FENCE_END)
        # Fully expanded — cron does no variable expansion in env lines, so
        # a $HOME/$PATH reference would be a literal (and broken) PATH.
        assert f"PATH={Path.home()}/.local/bin:/usr/local/sbin" in block
        assert "$PATH" not in block and "$HOME" not in block
        # env passthrough, per-job log, cadence preserved
        assert (
            '0 3 * * * ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" /abs/claude -p /dream '
            "--dangerously-skip-permissions >> /cache/pm/dream.log 2>&1" in block
        )
        assert (
            '15 */4 * * * OPENAI_API_KEY="${OPENAI_API_KEY}" /abs/weave index --embed '
            "--only-new >> /cache/pm/embed-warm.log 2>&1" in block
        )
        # disabled job rendered commented
        assert "# (disabled) 0 */2 * * *" in block

    def test_serialize_wraps_in_flock(self, config, monkeypatch):
        # registry and cron share the global shutil module — one patch
        # serves both call sites (two setattrs would clobber each other).
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: "/usr/bin/flock" if name == "flock" else f"/abs/{name}",
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir", lambda: Path("/cache/pm")
        )
        job = ScheduledJob(
            "dream", "0 3 * * *", "claude -p /dream", runner="direct",
            env=("ANTHROPIC_API_KEY",), log="dream.log", serialize=True,
        )
        block = CrontabBackend(config).render([job])
        assert (
            '0 3 * * * ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" '
            "/usr/bin/flock -n /tmp/thinkweave-dream.lock /abs/claude -p /dream "
            "--dangerously-skip-permissions >> /cache/pm/dream.log 2>&1" in block
        )

    def test_serialize_degrades_without_flock(self, config, monkeypatch):
        """Stock macOS has no flock: render the line unguarded, not broken."""
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: None if name == "flock" else f"/abs/{name}",
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir", lambda: Path("/cache/pm")
        )
        job = ScheduledJob(
            "dream", "0 3 * * *", "claude -p /dream", runner="direct",
            serialize=True,
        )
        block = CrontabBackend(config).render([job])
        assert "flock" not in block
        assert "/abs/claude -p /dream" in block

    def test_install_splices_and_preserves_foreign(self, config, monkeypatch):
        captured = {}

        def fake_read(self):
            return "# my own line\n0 0 * * * echo hi\n"

        def fake_write(self, content):
            captured["content"] = content

        monkeypatch.setattr(CrontabBackend, "_read_crontab", fake_read)
        monkeypatch.setattr(CrontabBackend, "_write_crontab", fake_write)
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir",
            lambda: config.vault_root / "cache",
        )
        CrontabBackend(config).install(self._jobs())
        out = captured["content"]
        assert "# my own line" in out  # foreign preserved
        assert "0 0 * * * echo hi" in out
        assert FENCE_START in out and FENCE_END in out

    def test_install_warns_when_cron_daemon_absent(
        self, config, monkeypatch, capsys
    ):
        """WSL footgun: crontab edits succeed but no daemon runs the jobs."""
        import subprocess as _sp

        monkeypatch.setattr(
            CrontabBackend, "_read_crontab", lambda self: ""
        )
        monkeypatch.setattr(
            CrontabBackend, "_write_crontab", lambda self, content: None
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir",
            lambda: config.vault_root / "cache",
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pidof")
        monkeypatch.setattr(
            _sp,
            "run",
            lambda *a, **k: _sp.CompletedProcess(a, returncode=1),
        )
        CrontabBackend(config).install(self._jobs())
        err = capsys.readouterr().err
        assert "no cron daemon" in err
        assert "sudo service cron start" in err

    def test_install_silent_when_cron_daemon_running(
        self, config, monkeypatch, capsys
    ):
        import subprocess as _sp

        monkeypatch.setattr(
            CrontabBackend, "_read_crontab", lambda self: ""
        )
        monkeypatch.setattr(
            CrontabBackend, "_write_crontab", lambda self, content: None
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.cron.user_cache_dir",
            lambda: config.vault_root / "cache",
        )
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pidof")
        monkeypatch.setattr(
            _sp,
            "run",
            lambda *a, **k: _sp.CompletedProcess(a, returncode=0),
        )
        CrontabBackend(config).install(self._jobs())
        assert "no cron daemon" not in capsys.readouterr().err

    def test_idempotent_replace(self):
        existing = (
            "# foreign\n"
            f"{FENCE_START}\n"
            "0 3 * * * old-stuff\n"
            f"{FENCE_END}\n"
            "# trailing foreign\n"
        )
        new_block = f"{FENCE_START}\n0 3 * * * new-stuff\n{FENCE_END}\n"
        out = _splice(existing, new_block)
        assert "old-stuff" not in out
        assert "new-stuff" in out
        assert "# foreign" in out
        assert "# trailing foreign" in out
        # only one fence pair
        assert out.count(FENCE_START) == 1

    def test_uninstall_strips_fence_only(self):
        existing = (
            "# foreign\n"
            f"{FENCE_START}\n"
            "0 3 * * * stuff\n"
            f"{FENCE_END}\n"
        )
        out = _splice(existing, "")
        assert FENCE_START not in out
        assert "stuff" not in out
        assert "# foreign" in out


# --------------------------------------------------------------------------- #
# TaskSchedulerBackend
# --------------------------------------------------------------------------- #


class TestTaskSchedulerBackend:
    def test_build_create_argv(self, config, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which",
            lambda name: f"C:\\bin\\{name}.exe",
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.taskscheduler.user_cache_dir",
            lambda: Path("C:/cache/pm"),
        )
        job = ScheduledJob(
            "dream", "0 3 * * *", "claude -p /dream", runner="direct", log="dream.log"
        )
        argv = TaskSchedulerBackend(config).build_create_argv(job)
        assert argv[0] == "schtasks"
        assert argv[1] == "/Create"
        assert "/TN" in argv and "Thinkweave\\dream" in argv
        assert "/F" in argv
        # trigger flags appended
        assert "/SC" in argv and "DAILY" in argv
        # action wraps the resolved command in cmd /c with redirect
        tr_idx = argv.index("/TR")
        action = argv[tr_idx + 1]
        assert action.startswith("cmd /c ")
        assert "C:\\bin\\claude.exe -p /dream" in action
        assert ">>" in action and "dream.log" in action
        # cd to the writable vault dir (Task Scheduler cwd is System32) +
        # unattended permission grant for the headless claude -p invocation
        assert "cd /d" in action
        assert "--dangerously-skip-permissions" in action

    def test_delete_argv(self, config):
        job = ScheduledJob("dream", "0 3 * * *", "claude -p /dream", runner="direct")
        argv = TaskSchedulerBackend(config).build_delete_argv(job)
        assert argv == ["schtasks", "/Delete", "/TN", "Thinkweave\\dream", "/F"]

    def test_env_warning_for_unset_var(self, config, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        job = ScheduledJob(
            "embeddings", "15 */4 * * *", "weave index", runner="uv",
            env=("OPENAI_API_KEY",),
        )
        warnings = TaskSchedulerBackend(config).env_warnings([job])
        assert any("OPENAI_API_KEY" in w for w in warnings)

    def test_anthropic_warning_is_advisory(self, config, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        job = ScheduledJob(
            "dream", "0 3 * * *", "claude -p /dream", runner="direct",
            env=("ANTHROPIC_API_KEY",),
        )
        warnings = TaskSchedulerBackend(config).env_warnings([job])
        assert warnings and "advisory" in warnings[0]

    def test_install_calls_schtasks(self, config, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "thinkweave.scheduling.taskscheduler.subprocess.run",
            lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.taskscheduler.user_cache_dir",
            lambda: config.vault_root / "cache",
        )
        monkeypatch.setattr(
            "thinkweave.scheduling.registry.shutil.which", lambda name: f"/abs/{name}"
        )
        jobs = [
            ScheduledJob("dream", "0 3 * * *", "claude -p /dream", runner="direct"),
            ScheduledJob("off", "0 3 * * *", "claude -p /x", runner="direct", enabled=False),
        ]
        TaskSchedulerBackend(config).install(jobs)
        # enabled job creates a task; disabled one is skipped
        assert len(calls) == 1
        assert calls[0][0] == "schtasks"


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #


class TestSelectBackend:
    def test_windows(self, config, monkeypatch):
        monkeypatch.setattr(
            "thinkweave.scheduling.platform.system", lambda: "Windows"
        )
        assert isinstance(select_backend(config), TaskSchedulerBackend)

    def test_linux(self, config, monkeypatch):
        monkeypatch.setattr("thinkweave.scheduling.platform.system", lambda: "Linux")
        assert isinstance(select_backend(config), CrontabBackend)

    def test_darwin(self, config, monkeypatch):
        monkeypatch.setattr("thinkweave.scheduling.platform.system", lambda: "Darwin")
        assert isinstance(select_backend(config), CrontabBackend)
