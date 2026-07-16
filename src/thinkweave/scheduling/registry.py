"""Job registry — the single source of truth for thinkweave's scheduled
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
  ``weave index --embed --only-new``). The same string runs on both OSes;
  only the trigger mechanism differs.
- ``runner``   — ``direct`` (a ``claude`` invocation) or ``uv`` (a ``weave``
  subcommand). Drives binary resolution in :func:`resolve_command`.
- ``env``      — env-var names the job needs *in its environment*. NOTE:
  for ``claude -p`` jobs this is the ``claude`` CLI's own headless auth
  (e.g. ``ANTHROPIC_API_KEY``), NOT an Anthropic API call made by the
  skill. The only genuine job-level API dependency is ``OPENAI_API_KEY``
  on the embeddings keep-warm job. The field exists to reproduce the
  crontab faithfully and to drive the unset-var warning — it asserts
  nothing about whether a job is itself an API client.
- ``log``      — log filename, relative to :func:`thinkweave.core.config
  .user_cache_dir`. Each job logs to its own file (``dream.log`` etc.).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from thinkweave.core.config import Config, resolve_config_file
from thinkweave.core.plugin_route import namespace_prompt, plugin_namespace

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

    Prefer an absolute binary so the scheduler never depends on a sparse
    PATH at fire time. (The hooks installer used to share this pattern;
    since #50 it derives fire-time ``uv run --project`` commands from
    hooks/hooks.json instead.)

    - ``runner='direct'`` (``claude -p …``): swap the leading ``claude``
      token for ``shutil.which('claude')`` when found.
    - ``runner='uv'`` (``weave …``): if ``weave`` is a resolvable console
      script, swap the leading ``weave`` for its absolute path; otherwise
      fall back to ``uv run --project <repo_root> weave …`` (the dev-checkout
      case where ``weave`` isn't installed globally).

    ``repo_root`` is only consulted for the ``uv`` fallback; when omitted
    and ``weave`` is unresolved, ``uv run weave …`` is emitted (relies on the
    scheduler's working directory).
    """
    tokens = job.command.split()
    if not tokens:
        return job.command

    head, *rest = tokens

    if job.runner == "direct":
        # Plugin-route installs register skills namespaced (verified: no
        # bare-name aliasing), so `/dream` must render as
        # `/thinkweave:dream` in the scheduled line. The token after a
        # `-p` flag is the skill invocation.
        ns = plugin_namespace()
        if ns:
            for i, tok in enumerate(rest[:-1]):
                if tok == "-p":
                    rest[i + 1] = namespace_prompt(rest[i + 1], ns)
        # Headless `claude -p` runs unattended with no TTY to approve tool use,
        # so the skill's `weave …` Bash calls are denied under the default
        # permission mode (the cron silently no-ops at its first tool call).
        # Grant unattended tool use explicitly for `-p` invocations.
        if "-p" in rest and "--dangerously-skip-permissions" not in rest:
            rest.append("--dangerously-skip-permissions")
        resolved = shutil.which(head)
        if resolved:
            return " ".join([_quote(resolved), *rest])
        return " ".join([head, *rest])

    # runner == "uv": a `weave` subcommand.
    if head == "weave":
        resolved = shutil.which("weave")
        if resolved:
            return " ".join([_quote(resolved), *rest])
        prefix = ["uv", "run"]
        if repo_root is not None:
            prefix += ["--project", _quote(str(repo_root))]
        return " ".join([*prefix, "weave", *rest])

    return job.command


def _quote(s: str) -> str:
    """Quote a path/token if it contains spaces (Windows paths often do).

    Double quotes work in both POSIX sh and cmd.exe for whitespace
    grouping, which is all we need here.
    """
    return f'"{s}"' if " " in s else s
