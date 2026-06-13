"""Crontab backend — renders the job registry into a fenced crontab block
and installs it via the ``crontab`` binary.

This is the Python home of the bash that used to live in
``commands/onboard.md`` Step 6: read the current crontab, replace the
content between fence markers (or append), pipe it back. ``crontab`` is
the only external dependency and it's POSIX-only — which is the whole
point of the per-OS backend split (see ``scheduling/__init__.py``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from thinkweave.core.config import Config, user_cache_dir
from thinkweave.scheduling.registry import ScheduledJob, resolve_command

FENCE_START = "# --- thinkweave cron block ---"
FENCE_END = "# --- end thinkweave ---"

# PATH hardening — cron's default PATH is minimal and won't find `uv` or
# `claude` after a standard installer drop into ~/.local/bin.
_PATH_LINE = "PATH=$HOME/.local/bin:$PATH"


class CrontabBackend:
    """Render + install scheduled jobs as a fenced user-crontab block."""

    name = "crontab"

    def __init__(self, config: Config) -> None:
        self.config = config

    # -- rendering ---------------------------------------------------------

    def render(self, jobs: list[ScheduledJob]) -> str:
        """Return the fenced crontab block for ``jobs`` (no trailing read of
        the existing crontab — pure function, used by ``--dry-run`` and by
        :meth:`install`'s splice step)."""
        repo_root = Path.cwd()
        log_dir = user_cache_dir()

        lines = [FENCE_START, _PATH_LINE]
        for job in jobs:
            line = self._render_job(job, repo_root=repo_root, log_dir=log_dir)
            lines.append(line if job.enabled else f"# (disabled) {line}")
        lines.append(FENCE_END)
        return "\n".join(lines) + "\n"

    def _render_job(
        self, job: ScheduledJob, *, repo_root: Path, log_dir: Path
    ) -> str:
        command = resolve_command(job, repo_root=repo_root)
        # Env passthrough, reproducing the example-crontab `KEY="${KEY}"`
        # form so cron's shell expands the value from its own environment.
        env_prefix = "".join(f'{name}="${{{name}}}" ' for name in job.env)
        redirect = ""
        if job.log:
            log_path = log_dir / job.log
            redirect = f" >> {log_path} 2>&1"
        return f"{job.cadence} {env_prefix}{command}{redirect}"

    # -- install / uninstall ----------------------------------------------

    def install(self, jobs: list[ScheduledJob]) -> None:
        """Splice the rendered block into the user crontab (idempotent)."""
        # Ensure the log dir exists so cron can write into it on first fire.
        user_cache_dir().mkdir(parents=True, exist_ok=True)

        existing = self._read_crontab()
        spliced = _splice(existing, self.render(jobs))
        self._write_crontab(spliced)
        self._warn_if_daemon_not_running()

    def _warn_if_daemon_not_running(self) -> None:
        """Surface a WSL footgun: crontab edits succeed, nothing ever fires.

        On WSL (and some minimal containers) the ``crontab`` binary works
        but the cron *daemon* isn't running by default, so an installed
        schedule silently never executes. Best-effort check via
        ``pidof cron``/``crond``; advisory only — never blocks install.
        """
        import shutil
        import sys

        pidof = shutil.which("pidof")
        if not pidof:
            return
        try:
            for daemon in ("cron", "crond"):
                if (
                    subprocess.run(
                        [pidof, daemon], capture_output=True
                    ).returncode
                    == 0
                ):
                    return
        except OSError:
            return
        print(
            "warning: crontab updated, but no cron daemon appears to be "
            "running — your jobs will never fire.\n"
            "  On WSL: start it with `sudo service cron start`, and make it "
            "persistent with `systemd=true` under `[boot]` in /etc/wsl.conf "
            "(then `wsl --shutdown` once) or add the service start to your "
            "shell profile.",
            file=sys.stderr,
        )

    def uninstall(self, jobs: list[ScheduledJob] | None = None) -> None:
        """Remove the thinkweave fence block, leaving foreign lines intact.

        ``jobs`` is accepted for interface symmetry with
        :class:`~thinkweave.scheduling.taskscheduler.TaskSchedulerBackend`
        (which deletes per-task) but is unused here — the fence strips the
        whole block at once.
        """
        existing = self._read_crontab()
        spliced = _splice(existing, "")  # empty block → just strips the fence
        self._write_crontab(spliced)

    # -- crontab I/O -------------------------------------------------------

    def _read_crontab(self) -> str:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
        )
        # `crontab -l` exits non-zero when no crontab exists for the user —
        # treat that as an empty crontab, not an error.
        if result.returncode != 0:
            return ""
        return result.stdout

    def _write_crontab(self, content: str) -> None:
        subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _splice(existing: str, block: str) -> str:
    """Replace the fenced thinkweave block in ``existing`` with ``block``.

    Drops any lines between (and including) the fence markers, then appends
    the new block. An empty ``block`` strips the fence entirely (uninstall).
    Foreign crontab lines are preserved in order.
    """
    lines = existing.splitlines()
    kept: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == FENCE_START:
            inside = True
            continue
        if stripped == FENCE_END:
            inside = False
            continue
        if not inside:
            kept.append(line)

    head = "\n".join(kept).rstrip("\n")
    block = block.strip("\n")
    if not block:
        return (head + "\n") if head else ""
    if head:
        return head + "\n" + block + "\n"
    return block + "\n"
