"""Windows Task Scheduler backend — renders the job registry into
``schtasks.exe`` invocations.

``schtasks`` ships with every Windows install and is callable straight
from ``subprocess``, so the whole backend stays in Python — no separate
PowerShell artifact to maintain. The one genuinely new piece of logic is
:func:`cron_to_schtasks`, which maps the cron cadences the shipped
template uses onto Task Scheduler triggers. Anything outside that subset
**raises** at install time — a loud failure beats a silently mis-scheduled
job.

The scheduled *action* only ever launches ``claude`` / ``weave`` at the
``cmd.exe`` level. The skills' own bash internals (``mktemp`` etc.) run
under Claude Code's bundled Git Bash when ``claude -p`` spawns — the
scheduler never needs to reproduce bash semantics. That's what keeps this
backend small.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from thinkweave.core.config import Config, user_cache_dir
from thinkweave.scheduling.registry import ScheduledJob, _quote, resolve_command

_TASK_FOLDER = "Thinkweave"

# cron day-of-week (0/7 = Sunday) → schtasks /D token.
_DOW = {0: "SUN", 1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT", 7: "SUN"}


def cron_to_schtasks(expr: str) -> list[str]:
    """Translate a cron expression into schtasks trigger flags.

    Supported subset (everything the shipped ``scheduling.yaml`` uses):

    ===========  ==============================  =================================
    shape        example                         schtasks flags
    ===========  ==============================  =================================
    daily        ``0 3 * * *``                   ``/SC DAILY /ST 03:00``
    every N hrs  ``15 */4 * * *``                ``/SC HOURLY /MO 4 /ST 00:15``
    every N min  ``*/15 * * * *``                ``/SC MINUTE /MO 15``
    weekly       ``0 4 * * 0``                   ``/SC WEEKLY /D SUN /ST 04:00``
    ===========  ==============================  =================================

    Raises :class:`ValueError` for any expression outside this subset
    (ranges, lists, day-of-month, month constraints, multi-field steps).
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"unsupported cron expression {expr!r}: expected 5 fields, got {len(fields)}"
        )
    minute, hour, dom, month, dow = fields

    if dom != "*" or month != "*":
        raise ValueError(
            f"unsupported cron expression {expr!r}: day-of-month / month "
            "constraints have no Task Scheduler equivalent in this subset"
        )

    # every N minutes: */N * * * *
    if minute.startswith("*/"):
        if hour != "*" or dow != "*":
            raise ValueError(
                f"unsupported cron expression {expr!r}: minute-step must pair "
                "with wildcard hour and day-of-week"
            )
        return ["/SC", "MINUTE", "/MO", str(_step(minute))]

    m = _int(minute, expr)

    # every N hours: M */N * * *
    if hour.startswith("*/"):
        if dow != "*":
            raise ValueError(
                f"unsupported cron expression {expr!r}: hour-step must pair "
                "with wildcard day-of-week"
            )
        return ["/SC", "HOURLY", "/MO", str(_step(hour)), "/ST", f"00:{m:02d}"]

    h = _int(hour, expr)

    # weekly: M H * * D
    if dow != "*":
        n = _int(dow, expr)
        if n not in _DOW:
            raise ValueError(f"unsupported cron expression {expr!r}: bad day-of-week {dow!r}")
        return ["/SC", "WEEKLY", "/D", _DOW[n], "/ST", f"{h:02d}:{m:02d}"]

    # daily: M H * * *
    return ["/SC", "DAILY", "/ST", f"{h:02d}:{m:02d}"]


def _int(token: str, expr: str) -> int:
    try:
        return int(token)
    except ValueError:
        raise ValueError(
            f"unsupported cron expression {expr!r}: {token!r} is not a plain integer"
        ) from None


def _step(token: str) -> int:
    """Parse the N from a ``*/N`` step token (caller guarantees the prefix)."""
    try:
        return int(token[2:])
    except ValueError:
        raise ValueError(f"bad step token {token!r}") from None


class TaskSchedulerBackend:
    """Render + install scheduled jobs as Windows Task Scheduler tasks."""

    name = "taskscheduler"

    def __init__(self, config: Config) -> None:
        self.config = config

    # -- rendering ---------------------------------------------------------

    def task_name(self, job: ScheduledJob) -> str:
        return f"{_TASK_FOLDER}\\{job.name}"

    def build_create_argv(self, job: ScheduledJob) -> list[str]:
        """The full ``schtasks /Create`` argv for one job.

        Pure / side-effect-free so tests can assert it without a Windows
        host. ``/F`` force-overwrites, making re-install idempotent.
        """
        repo_root = Path.cwd()
        log_dir = user_cache_dir()
        command = resolve_command(job, repo_root=repo_root)
        redirect = f" >> {log_dir / job.log} 2>&1" if job.log else ""
        # Task Scheduler runs tasks with cwd=%windir%\System32 (non-writable),
        # which breaks skills that write scratch files relative to the cwd
        # (e.g. /dream's scan/plan handoffs). cd to the vault first so those
        # land in a writable dir. `cmd /c` is required so both the `&&` chain
        # and the `>>` redirect are honoured; the whole string is one /TR
        # element that subprocess quoting carries intact.
        cd = f"cd /d {_quote(str(self.config.vault_root))} && "
        action = f"cmd /c {cd}{command}{redirect}"
        return [
            "schtasks",
            "/Create",
            "/TN",
            self.task_name(job),
            "/TR",
            action,
            *cron_to_schtasks(job.cadence),
            "/F",
        ]

    def build_delete_argv(self, job: ScheduledJob) -> list[str]:
        return ["schtasks", "/Delete", "/TN", self.task_name(job), "/F"]

    def render(self, jobs: list[ScheduledJob]) -> str:
        """Human-readable preview (for ``--dry-run``) — one line per task."""
        out: list[str] = []
        for job in jobs:
            if not job.enabled:
                out.append(f"(disabled) {self.task_name(job)}")
                continue
            out.append(subprocess.list2cmdline(self.build_create_argv(job)))
        return "\n".join(out) + "\n"

    # -- install / uninstall ----------------------------------------------

    def env_warnings(self, jobs: list[ScheduledJob]) -> list[str]:
        """Warn about declared env vars missing from the install environment.

        Task Scheduler runs under the user account and inherits persistent
        (user/system) env vars, so we can only check what's visible now —
        but flagging a missing key here beats a silent 3am failure. The
        ``ANTHROPIC_API_KEY`` warning is advisory (subscription/OAuth-authed
        Claude Code won't need it); ``OPENAI_API_KEY`` is a real
        prerequisite for the embeddings job.
        """
        warnings: list[str] = []
        for job in jobs:
            if not job.enabled:
                continue
            for var in job.env:
                if not os.environ.get(var):
                    advisory = (
                        " (advisory — not needed if Claude Code is authed via "
                        "subscription/OAuth)"
                        if var == "ANTHROPIC_API_KEY"
                        else ""
                    )
                    warnings.append(
                        f"job '{job.name}' declares {var} but it is unset in this "
                        f"environment{advisory}"
                    )
        return warnings

    def install(self, jobs: list[ScheduledJob]) -> None:
        user_cache_dir().mkdir(parents=True, exist_ok=True)
        for job in jobs:
            if not job.enabled:
                continue
            subprocess.run(self.build_create_argv(job), check=True)

    def uninstall(self, jobs: list[ScheduledJob]) -> None:
        for job in jobs:
            # /Delete is best-effort: a job never installed isn't an error.
            subprocess.run(self.build_delete_argv(job), check=False)
