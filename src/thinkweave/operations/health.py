"""``weave health`` — deterministic "is everything running?" collector (#120).

Pure: no printing, no exit codes, no LLM — the CLI surface renders. Joins
*installed job → last-run evidence → stale?* for every cron line, because
cron death has been silent three times and nothing else looked at that
seam (``doctor`` = vault coherence, ``hooks status`` = hook errors,
``schedule`` = the rendered block, none of them "did it actually fire?").

JSON contract (``weave health --json``) — stable keys, read by ``/brief``
(#170)::

    {
      "ok":         bool,          # nothing flagged
      "checked_at": iso-8601,
      "flags":      [str, …],      # one human line per problem, empty when ok
      "jobs": [{
        "name":            str,    # "/dream", "weave index", "/drain paper"
        "cadence":         str,    # the 5-field cron expression
        "cadence_seconds": int,    # expected firing interval
        "last_run":        iso|null,
        "evidence":        "maintenance" | "log" | null,
        "stale":           bool,   # now - last_run > cadence × health.stale_factor
        "missing":         bool    # no evidence at all (distinct from stale)
      }, …],
      "queues": [{"source_type": str, "depth": int, "backlog": int}, …],
                                   # backlog = items older than health.backlog_days
      "hooks":  {"recent_errors": int, "last_error": str|null},   # last 24h
      "digest": {"latest": "YYYY-MM-DD"|null, "age_days": int|null, "stale": bool}
    }

Evidence join: every cron line appends ``>> <log>`` so the log's mtime is a
fire-time signal for any job (cron death = the file stops changing). The
dream rail additionally writes one ``maintenance.jsonl`` line per *completed*
cycle, keyed ``cycle_id: dream-…`` — where both exist the newer wins and
``evidence`` says which. The whole ``crontab -l`` is read, not just the
weave fence block: on a long-lived install most lines are hand-written
outside the fence, and a job the user scheduled is a job they expect to run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thinkweave.core.config import Config
from thinkweave.operations.dream import maintenance_log_path
from thinkweave.operations.queue import inspect, list_queues

_CRON_LINE = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$")
_LOG_REDIRECT = re.compile(r">>\s*(\S+)")
_PROMPT = re.compile(r"""-p\s+["']?(/[\w:-]+)(?:\s+--source-type\s+([\w-]+))?""")
_WEAVE = re.compile(r"\bweave\s+([a-z][\w-]*)")
_HOOK_HEADER = re.compile(r"^\[(\d{4}-\d\d-\d\dT[^\]]+)\] ")
_DIGEST_DAY = re.compile(r"^(\d{4}-\d\d-\d\d)-")
_HOOK_WINDOW = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_crontab() -> str:
    if not shutil.which("crontab"):
        return ""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def collect(
    cfg: Config, *, now: datetime | None = None, crontab_text: str | None = None
) -> dict:
    """Build the health report (schema in the module docstring)."""
    now = now or _now()
    cron = _read_crontab() if crontab_text is None else crontab_text
    flags: list[str] = []

    jobs = [_job(line, cfg, now) for line in _cron_lines(cron)]
    for j in jobs:
        if j["missing"]:
            flags.append(f"job {j['name']}: no run evidence")
        elif j["stale"]:
            flags.append(f"job {j['name']}: stale (last run {j['last_run']})")

    queues = []
    cutoff = now - timedelta(days=cfg.health_backlog_days)
    for q in list_queues(cfg):
        items = inspect(cfg, q["source_type"])
        backlog = sum(1 for it in items if _ts(it.get("enqueued_at")) and _ts(it["enqueued_at"]) < cutoff)
        queues.append({"source_type": q["source_type"], "depth": len(items), "backlog": backlog})
        if backlog:
            flags.append(f"queue {q['source_type']}: {backlog} item(s) older than {cfg.health_backlog_days}d")

    hooks = _hooks(cfg.weave_dir / "hooks.log", now)
    if hooks["recent_errors"]:
        flags.append(f"hooks: {hooks['recent_errors']} error(s) in last 24h")

    digest = _digest(cfg.vault_root / "digests", now, cfg.health_stale_factor)
    if digest["stale"]:
        flags.append(f"digest: latest {digest['latest']} is {digest['age_days']}d old")

    return {
        "ok": not flags,
        "checked_at": now.isoformat(),
        "flags": flags,
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


def _job(line: tuple[str, str], cfg: Config, now: datetime) -> dict:
    cadence, command = line
    name = _job_name(command)
    candidates: list[tuple[datetime, str]] = []

    m = _LOG_REDIRECT.search(command)
    if m:
        log = Path(m.group(1).strip("'\""))
        if log.exists():
            candidates.append(
                (datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc), "log")
            )
    skill = name.lstrip("/").split()[0] if name.startswith("/") else ""
    if skill:
        ts = _latest_maintenance(maintenance_log_path(cfg), skill)
        if ts:
            candidates.append((ts, "maintenance"))

    last, evidence = max(candidates, default=(None, None))
    seconds = _cadence_seconds(cadence)
    stale = bool(last) and (now - last) > timedelta(seconds=seconds * cfg.health_stale_factor)
    return {
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
        return f"{m.group(1)} {m.group(2)}" if m.group(2) else m.group(1)
    m = _WEAVE.search(command)
    if m:
        return f"weave {m.group(1)}"
    m = _LOG_REDIRECT.search(command)
    return Path(m.group(1).strip("'\"")).stem if m else command[:40]


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
    (``*/N`` steps, fixed hours/lists, weekly/monthly pins) — not a cron
    parser. Uneven lists ("0 7,19") average out. Upgrade path: croniter.
    """
    minute, hour, dom, _mon, dow = expr.split()
    if dow != "*":
        return 7 * 86400
    if dom != "*":
        return 30 * 86400
    if hour == "*":
        if minute == "*":
            return 60
        if minute.startswith("*/"):
            return int(minute[2:]) * 60
        return 3600
    if hour.startswith("*/"):
        return int(hour[2:]) * 3600
    return 86400 // max(1, len(hour.split(",")))


# --------------------------------------------------------------------------- #
# hooks / digest / helpers


def _hooks(path: Path, now: datetime) -> dict:
    """Count error entries (header line followed by a traceback) in the last 24h.

    ``_log_info`` lines share the header format but never carry a traceback,
    which is what separates telemetry from failures here.
    """
    out = {"recent_errors": 0, "last_error": None}
    if not path.exists():
        return out
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        m = _HOOK_HEADER.match(line)
        if not m or i + 1 >= len(lines) or not lines[i + 1].startswith("Traceback"):
            continue
        ts = _ts(m.group(1))
        if ts and now - ts <= _HOOK_WINDOW:
            out["recent_errors"] += 1
            out["last_error"] = line
    return out


def _digest(digests_dir: Path, now: datetime, stale_factor: float) -> dict:
    days = sorted(
        m.group(1)
        for p in (digests_dir.glob("*.md") if digests_dir.exists() else ())
        if (m := _DIGEST_DAY.match(p.name))
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
