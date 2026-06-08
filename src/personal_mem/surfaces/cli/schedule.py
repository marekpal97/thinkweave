"""``mem schedule`` — install personal_mem's recurring jobs onto the host's
native scheduler (crontab on Linux/macOS, Task Scheduler on Windows).

All three subcommands read the one ``vault/config/scheduling.yaml`` job
registry and dispatch through :func:`personal_mem.scheduling.select_backend`,
so the cadence + job list live in a single place regardless of OS.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from personal_mem.core.config import load_config
from personal_mem.scheduling import load_jobs, scheduling_path, select_backend
from personal_mem.scheduling.cron import CrontabBackend
from personal_mem.scheduling.taskscheduler import TaskSchedulerBackend


def cmd_schedule(args: argparse.Namespace) -> None:
    cfg = load_config()
    action = getattr(args, "schedule_action", None) or "list"

    all_jobs = load_jobs(cfg)
    if not all_jobs:
        print(
            f"No jobs found. Expected a job registry at "
            f"{scheduling_path(cfg)} (run `mem init` to seed it)."
        )
        return

    only = _parse_only(getattr(args, "only", None))
    jobs = _select_jobs(all_jobs, only)
    if only and not jobs:
        print(f"No jobs matched --only {','.join(only)}.")
        return

    backend = select_backend(cfg)

    if action == "list":
        _cmd_list(backend, list(all_jobs.values()))
        return
    if action == "install":
        _cmd_install(backend, jobs, dry_run=getattr(args, "dry_run", False))
        return
    if action == "uninstall":
        _cmd_uninstall(backend, jobs, dry_run=getattr(args, "dry_run", False))
        return

    _cmd_list(backend, list(all_jobs.values()))


# --------------------------------------------------------------------------- #


def _cmd_list(backend, jobs: list) -> None:
    print(f"Scheduler backend: {backend.name}\n")
    print(f"{'JOB':<22} {'CADENCE':<16} {'ON':<4} COMMAND")
    print("-" * 78)
    for job in jobs:
        on = "yes" if job.enabled else "no"
        print(f"{job.name:<22} {job.cadence:<16} {on:<4} {job.command}")
    print()
    print("Preview the rendered scheduler entries with "
          "`mem schedule install --dry-run`.")


def _cmd_install(backend, jobs: list, *, dry_run: bool) -> None:
    _warn_unset_env(backend, jobs)

    if dry_run:
        print(f"# Would install via {backend.name} backend:\n")
        print(backend.render(jobs), end="")
        return

    backend.install(jobs)
    print(f"Installed {sum(j.enabled for j in jobs)} job(s) via {backend.name}.")


def _cmd_uninstall(backend, jobs: list, *, dry_run: bool) -> None:
    if dry_run:
        if isinstance(backend, CrontabBackend):
            print("# Would remove the personal-mem crontab fence block.")
        else:
            for job in jobs:
                print(f"# Would delete task {backend.task_name(job)}")
        return

    backend.uninstall(jobs)
    print(f"Removed personal-mem schedule via {backend.name}.")


def _warn_unset_env(backend, jobs: list) -> None:
    """Surface declared-but-unset env vars (Windows backend exposes this)."""
    if isinstance(backend, TaskSchedulerBackend):
        for w in backend.env_warnings(jobs):
            print(f"warning: {w}")


def _parse_only(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _select_jobs(all_jobs: dict, only: list[str]) -> list:
    """Pick the jobs to act on.

    No ``--only`` → every job (the per-job ``enabled`` flag then governs
    what actually installs). With ``--only`` → exactly the named jobs,
    **force-enabled** — naming a job explicitly is a request to install it,
    so an otherwise default-off job (e.g. ``daily-research``) still lands.
    """
    if not only:
        return list(all_jobs.values())
    return [
        replace(all_jobs[name], enabled=True)
        for name in only
        if name in all_jobs
    ]
