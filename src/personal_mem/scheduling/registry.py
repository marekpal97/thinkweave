"""Job registry — the single source of truth for personal_mem's scheduled
work, rendered to whatever scheduler the host OS provides.

Mirrors the declarative-spec pattern used by ``flows.py`` and
``sources/registry.py``: a tiny dataclass plus a loader. The cadence is a
**cron expression** (the most expressive, widely-understood cadence
language); the Linux backend passes it through verbatim, the Windows
backend translates the common subset to Task Scheduler triggers.

Job definitions live at ``vault/config/scheduling.yaml``. When the file is
missing, :func:`load_jobs` returns an empty dict — same posture as
``flows.load_flows``.

A job carries everything needed to reproduce the historic
``scripts/example-crontab`` line faithfully on either backend:

- ``cadence``  — cron expression.
- ``command``  — the literal command (e.g. ``claude -p /dream`` or
  ``mem index --embed --only-new``). The same string runs on both OSes;
  only the trigger mechanism differs.
- ``runner``   — ``direct`` (a ``claude`` invocation) or ``uv`` (a ``mem``
  subcommand). Drives binary resolution in :func:`resolve_command`.
- ``env``      — env-var names the job needs *in its environment*. NOTE:
  for ``claude -p`` jobs this is the ``claude`` CLI's own headless auth
  (e.g. ``ANTHROPIC_API_KEY``), NOT an Anthropic API call made by the
  skill. The only genuine job-level API dependency is ``OPENAI_API_KEY``
  on the embeddings keep-warm job. The field exists to reproduce the
  crontab faithfully and to drive the unset-var warning — it asserts
  nothing about whether a job is itself an API client.
- ``log``      — log filename, relative to :func:`personal_mem.core.config
  .user_cache_dir`. Each job logs to its own file (``dream.log`` etc.).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from personal_mem.core.config import Config, resolve_config_file

Runner = Literal["direct", "uv"]


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    cadence: str
    command: str
    runner: Runner = "uv"
    env: tuple[str, ...] = ()
    log: str | None = None
    enabled: bool = True


def scheduling_path(config: Config) -> Path:
    """Vault-local ``scheduling.yaml`` (same dir as flows.yaml etc.)."""
    return resolve_config_file(config.vault_root, "scheduling.yaml")


def load_jobs(
    config: Config, *, path: Path | None = None
) -> dict[str, ScheduledJob]:
    """Load scheduled jobs from ``vault/config/scheduling.yaml``.

    Returns an empty dict if the file is absent. Best-effort: a malformed
    file yields ``{}`` rather than crashing the CLI (mirrors
    ``flows.load_flows``).
    """
    p = path or scheduling_path(config)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return _parse(raw)


def _parse(raw: dict) -> dict[str, ScheduledJob]:
    jobs_block = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(jobs_block, dict):
        return {}

    out: dict[str, ScheduledJob] = {}
    for name, spec in jobs_block.items():
        if not isinstance(spec, dict):
            continue
        cadence = str(spec.get("cadence", "")).strip()
        command = str(spec.get("command", "")).strip()
        if not cadence or not command:
            continue  # an entry without both is not actionable — skip it
        runner = spec.get("runner", "uv")
        if runner not in ("direct", "uv"):
            runner = "uv"
        env_raw = spec.get("env") or []
        env = tuple(str(e).strip() for e in env_raw if str(e).strip())
        log = spec.get("log")
        log = str(log).strip() if log else None
        enabled = bool(spec.get("enabled", True))
        out[name] = ScheduledJob(
            name=name,
            cadence=cadence,
            command=command,
            runner=runner,  # type: ignore[arg-type]
            env=env,
            log=log,
            enabled=enabled,
        )
    return out


def resolve_command(job: ScheduledJob, *, repo_root: Path | None = None) -> str:
    """Resolve ``job.command`` to an absolute, OS-runnable command string.

    Mirrors ``surfaces/hooks/install.py:_resolve_hook_cmd`` — prefer an
    absolute binary so the scheduler never depends on a sparse PATH at fire
    time.

    - ``runner='direct'`` (``claude -p …``): swap the leading ``claude``
      token for ``shutil.which('claude')`` when found.
    - ``runner='uv'`` (``mem …``): if ``mem`` is a resolvable console
      script, swap the leading ``mem`` for its absolute path; otherwise
      fall back to ``uv run --project <repo_root> mem …`` (the dev-checkout
      case where ``mem`` isn't installed globally).

    ``repo_root`` is only consulted for the ``uv`` fallback; when omitted
    and ``mem`` is unresolved, ``uv run mem …`` is emitted (relies on the
    scheduler's working directory).
    """
    tokens = job.command.split()
    if not tokens:
        return job.command

    head, *rest = tokens

    if job.runner == "direct":
        resolved = shutil.which(head)
        if resolved:
            return " ".join([_quote(resolved), *rest])
        return job.command

    # runner == "uv": a `mem` subcommand.
    if head == "mem":
        resolved = shutil.which("mem")
        if resolved:
            return " ".join([_quote(resolved), *rest])
        prefix = ["uv", "run"]
        if repo_root is not None:
            prefix += ["--project", _quote(str(repo_root))]
        return " ".join([*prefix, "mem", *rest])

    return job.command


def _quote(s: str) -> str:
    """Quote a path/token if it contains spaces (Windows paths often do).

    Double quotes work in both POSIX sh and cmd.exe for whitespace
    grouping, which is all we need here.
    """
    return f'"{s}"' if " " in s else s
