"""``weave health`` — deterministic "is everything running?" collector (#120).

Pure: no printing, no exit codes, no LLM — the CLI surface renders. Joins
*installed job → last-run evidence → stale?* for every cron line, because
cron death has been silent three times and nothing else looked at that
seam (``doctor`` = vault coherence, ``hooks status`` = hook errors,
``schedule`` = the rendered block, none of them "did it actually fire?").

JSON contract (``weave health --json``) — stable keys, read by ``/brief``
(#170)::

    {
      "ok":         bool,          # no flags — exit code mirrors this
      "checked_at": iso-8601,
      "flags":      [str, …],      # problems: jobs + hooks + digest only
      "advisories": [str, …],      # informational (queue backlog); never affects ok
      "jobs_note":  str|null,      # set when the job layer could not be inspected
      "jobs": [{
        "id":              str,    # stable row key: "<cadence> <name>"
        "name":            str,    # "/dream", "weave index", "/drain paper"
        "cadence":         str,    # the 5-field cron expression
        "cadence_seconds": int,    # expected firing interval
        "last_run":        iso|null,
        "evidence":        "maintenance" | "log" | "log(shared)" | null,
        "stale":           bool,   # now - last_run > cadence × health.stale_factor
        "missing":         bool    # no evidence at all (distinct from stale)
      }, …],
      "queues": [{"source_type": str, "depth": int, "backlog": int}, …],
                                   # lanes = sources.yaml registry;
                                   # backlog = items older than health.backlog_days
      "hooks":  {"recent_errors": int, "last_error": str|null},   # last 24h
      "digest": {"latest": "YYYY-MM-DD"|null, "age_days": int|null, "stale": bool}
    }

"""

from __future__ import annotations

import json
import re
from collections import Counter
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thinkweave.core.config import Config
from thinkweave.operations.dream import maintenance_log_path
from thinkweave.acquisition.sources import all_specs
from thinkweave.acquisition.sources.queue import Queue

_CRON_LINE = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$")
_LOG_REDIRECT = re.compile(r">>\s*(\S+)")
_PROMPT = re.compile(r"""-p\s+["']?(/[\w:-]+)""")
_SOURCE_TYPE = re.compile(r"--source-type\s+([\w-]+)")
_WEAVE = re.compile(r"\bweave\s+([a-z][\w-]*)")
_HOOK_HEADER = re.compile(r"^\[(\d{4}-\d\d-\d\dT[^\]]+)\] ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_crontab() -> str | None:
    """``crontab -l`` text; ``None`` when there is no crontab binary at all
    (Windows / Task Scheduler hosts) — distinct from an empty crontab."""
    if not shutil.which("crontab"):
        return None
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout if result.returncode == 0 else ""


def collect(
    cfg: Config, *, now: datetime | None = None, crontab_text: str | None = None
) -> dict:
    """Build the health report (schema in the module docstring)."""
    now = now or _now()
    cron = _read_crontab() if crontab_text is None else crontab_text
    flags: list[str] = []
    advisories: list[str] = []

    jobs_note = None if cron is not None else (
        "job layer not inspectable on this platform (no crontab)"
    )
    lines = _cron_lines(cron or "")
    counts = Counter(_log_path(c) for _, c in lines)
    shared_logs = {p for p, n in counts.items() if p and n > 1}
    jobs = [_job(line, cfg, now, shared_logs) for line in lines]
    if jobs_note:
        flags.append(jobs_note)
    for j in jobs:
        if j["missing"]:
            flags.append(f"job {j['name']}: no run evidence")
        elif j["stale"]:
            flags.append(f"job {j['name']}: stale (last run {j['last_run']})")

    queues = []
    cutoff = now - timedelta(days=cfg.health_backlog_days)
    for spec in all_specs(cfg.vault_root):
        items = Queue.for_source_type(spec.slug, cfg.vault_root)._read_all()
        ages = [_ts(it.get("enqueued_at")) for it in items]
        backlog = sum(1 for t in ages if t and t < cutoff)
        queues.append({"source_type": spec.slug, "depth": len(items), "backlog": backlog})
        if backlog:
            advisories.append(
                f"queue {spec.slug}: {backlog} item(s) older than {cfg.health_backlog_days}d"
            )

    hooks = _hooks(cfg.weave_dir / "hooks.log", now)
    if hooks["recent_errors"]:
        flags.append(f"hooks: {hooks['recent_errors']} error(s) in last 24h")

    digest = _digest(cfg.vault_root / "digests", now, cfg.health_stale_factor)
    if digest["stale"]:
        flags.append(
            f"digest: latest {digest['latest']} is {digest['age_days']}d old"
            if digest["latest"] else "digest: none found"
        )

    return {
        "ok": not flags,
        "checked_at": now.isoformat(),
        "flags": flags,
        "advisories": advisories,
        "jobs_note": jobs_note,
        "jobs": jobs,
        "queues": queues,
        "hooks": hooks,
        "digest": digest,
    }


# --------------------------------------------------------------------------- #
# jobs


def _cron_lines(text: str) -> list[tuple[str, str]]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        m = _CRON_LINE.match(line)
        if m and m.group(1).split()[0][0] in "0123456789*":
            out.append((m.group(1), m.group(2)))
    return out


def _log_path(command: str) -> Path | None:
    m = _LOG_REDIRECT.search(command)
    return Path(m.group(1).strip("'\"")) if m else None


def _job(line: tuple[str, str], cfg: Config, now: datetime, shared_logs: set[Path]) -> dict:
    cadence, command = line
    name = _job_name(command)
    last, evidence = None, None

    # Completion evidence first; fire-time evidence (log mtime) only as fallback.
    skill = name.lstrip("/").split()[0] if name.startswith("/") else ""
    if skill:
        last = _latest_maintenance(maintenance_log_path(cfg), skill)
        evidence = "maintenance" if last else None
    log = _log_path(command)
    if last is None and log and log.exists():
        last = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
        evidence = "log(shared)" if log in shared_logs else "log"

    seconds = _cadence_seconds(cadence)
    stale = bool(last) and (now - last) > timedelta(seconds=seconds * cfg.health_stale_factor)
    return {
        "id": f"{cadence} {name}",
        "name": name,
        "cadence": cadence,
        "cadence_seconds": seconds,
        "last_run": last.isoformat() if last else None,
        "evidence": evidence,
        "stale": stale,
        "missing": last is None,
    }


def _job_name(command: str) -> str:
    m = _PROMPT.search(command)
    if m:
        lane = _SOURCE_TYPE.search(command)
        return f"{m.group(1)} {lane.group(1)}" if lane else m.group(1)
    m = _WEAVE.search(command)
    if m:
        return f"weave {m.group(1)}"
    log = _log_path(command)
    return log.stem if log else command[:40]


def _latest_maintenance(path: Path, skill: str) -> datetime | None:
    if not path.exists():
        return None
    latest = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(entry.get("cycle_id", "")).startswith(f"{skill}-"):
            ts = _ts(entry.get("ts"))
            if ts and (latest is None or ts > latest):
                latest = ts
    return latest


def _cadence_seconds(expr: str) -> int:
    """Expected interval for a 5-field cron expression.

    ponytail: a heuristic over the shapes thinkweave actually renders
    (``*/N`` steps, fixed hours/lists/ranges, weekday/weekly/monthly pins) —
    not a cron parser. Uneven lists ("0 7,19") average out; a single pinned
    weekday is weekly, any dow list/range is treated as daily (the gap
    inside the week, not the wrap-around). Upgrade path: croniter.
    """
    minute, hour, dom, _mon, dow = expr.split()
    if dow != "*" and dow.isdigit():
        return 7 * 86400
    if dom != "*" and dow == "*":
        return 30 * 86400
    if hour == "*":
        if minute == "*":
            return 60
        if minute.startswith("*/"):
            return int(minute[2:]) * 60
        return 3600
    if "/" in hour:
        return int(hour.split("/")[1]) * 3600
    return 86400 // max(1, len(hour.split(",")))


# --------------------------------------------------------------------------- #
# hooks / digest / helpers


def _hooks(path: Path, now: datetime) -> dict:
    """Count error entries in the last 24h.

    ``_log_info`` lines share the ``[ts] hook:`` header but never carry a
    traceback; an error entry is a header whose block (up to the next
    header) contains one — exception text may itself span lines.
    """
    out = {"recent_errors": 0, "last_error": None}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"(?m)^(?=\[\d{4}-\d\d-\d\dT)", text)
    for block in blocks:
        m = _HOOK_HEADER.match(block)
        if not m or "\nTraceback" not in block:
            continue
        ts = _ts(m.group(1))
        if ts and now - ts <= timedelta(days=1):
            out["recent_errors"] += 1
            out["last_error"] = block.splitlines()[0]
    return out


def _digest(digests_dir: Path, now: datetime, stale_factor: float) -> dict:
    days = sorted(
        p.name[:10]
        for p in (digests_dir.glob("*.md") if digests_dir.exists() else ())
        if p.name[:4].isdigit()
    )
    if not days:
        return {"latest": None, "age_days": None, "stale": True}
    latest = datetime.strptime(days[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    age = (now - latest).days
    # Nightly cadence: stale once the gap exceeds 1 day × stale_factor.
    return {"latest": days[-1], "age_days": age, "stale": age > stale_factor}


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
