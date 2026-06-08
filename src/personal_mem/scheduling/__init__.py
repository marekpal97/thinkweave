"""Cross-platform scheduler — one job registry, two rendering backends.

``scheduling.yaml`` is the single source of truth for personal_mem's
recurring work. :func:`select_backend` picks the native scheduler for the
host: ``crontab`` on Linux/macOS, Windows Task Scheduler (``schtasks``)
elsewhere. The job bodies (``mem flow run X``, ``claude -p "/dream"``) are
identical on both — only the trigger mechanism differs, so there is no
job-logic duplication.

This module owns the *only* ``platform.system()`` branch in the
scheduling stack.
"""

from __future__ import annotations

import platform

from personal_mem.core.config import Config
from personal_mem.scheduling.cron import CrontabBackend
from personal_mem.scheduling.registry import (
    ScheduledJob,
    load_jobs,
    resolve_command,
    scheduling_path,
)
from personal_mem.scheduling.taskscheduler import (
    TaskSchedulerBackend,
    cron_to_schtasks,
)

Backend = CrontabBackend | TaskSchedulerBackend


def select_backend(config: Config) -> Backend:
    """Return the native scheduler backend for this host."""
    if platform.system() == "Windows":
        return TaskSchedulerBackend(config)
    return CrontabBackend(config)


__all__ = [
    "ScheduledJob",
    "load_jobs",
    "scheduling_path",
    "resolve_command",
    "CrontabBackend",
    "TaskSchedulerBackend",
    "cron_to_schtasks",
    "select_backend",
]
