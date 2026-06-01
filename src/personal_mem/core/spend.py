"""Cost tracking — a two-layer, disjoint ledger over what's already on disk.

personal_mem is the only place that can put *both* LLM-spend layers on one
ledger and mesh them with the context/quality signals it already records:

- **Layer A — Claude agent turns.** Every assistant turn Claude Code writes to
  its native transcript ``~/.claude/projects/<encoded_cwd>/<session_id>.jsonl``
  already carries ``message.usage`` (input / output / cache tokens) and
  ``message.model``. We *read* it — no hook, no parallel sidecar. Covers both
  interactive sessions and headless ``claude -p`` runs (``/mem-wrap``,
  ``/dream``, ``/discover``, ``/drain``); subagent turns are flagged
  ``isSidechain``.

- **Layer B — personal_mem internal ops.** The OpenAI / Gemini / Anthropic-batch
  calls *inside* mem operations (``enrich``, ``news_triage``, ``gemini_extract``,
  embeddings keep-warm, hub Batches) spend money that no Claude turn records and
  that the provider dashboards can only show as an undifferentiated per-key lump
  sum. :func:`record_spend` captures it at the call site with an ``op`` label and
  emits a ``type:"spend"`` event into the *existing* per-session buffer
  (``buffer/<session_id>.jsonl`` → ``events.jsonl`` via ``archive_buffer``), or,
  when no session is in scope, into a dated headless log. That per-operation
  attribution is the thing neither other source can give.

The two layers never overlap (A = Claude turns, B = non-Claude LLM calls), so a
session's total is simply A + B. Tokens are stored; dollars are derived on read
against :data:`RATES` — tokens are stable, prices drift.

Design choices (see ``.claude/plans/cost-tracking.md``):

- **Read, don't re-materialize.** Layer A is read-only; the native jsonl is the
  source of truth. If Anthropic ever migrates that schema, the reader degrades
  gracefully (``SpendSummary.unknown=True`` for the window) — it never raises and
  never corrupts state.
- **Reuse, don't rebuild.** ``record_spend`` rides the same buffer machinery the
  retrieval/prompt events already use; there is no pricing subsystem, no
  reconciler, no rate-card versioning.

Lives in ``core/`` because both the ``operations``/``synthesis`` call sites and
the CLI surface need it, and ``core`` is the only layer everything may import.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config, load_config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Rates                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRate:
    """USD per 1M tokens, by token kind. ``cache_write`` is Anthropic's 5-minute
    cache-creation surcharge; providers without a cache concept leave it 0."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


# Flat rate table — the one place to edit when prices move (USD / 1M tokens).
# Keys are matched as *prefixes* of the model string, longest wins, so a single
# ``claude-opus-4`` entry covers ``claude-opus-4-7``, ``-4-8``, ``-4-20250514``.
# Sanity-check against codeburn's published numbers when updating.
RATES: dict[str, ModelRate] = {
    # --- Anthropic (Layer A — Claude agent turns) ---
    "claude-opus-4": ModelRate(15.0, 75.0, 1.50, 18.75),
    "claude-sonnet-4": ModelRate(3.0, 15.0, 0.30, 3.75),
    "claude-haiku-4": ModelRate(1.0, 5.0, 0.10, 1.25),
    "claude-3-5-haiku": ModelRate(0.80, 4.0, 0.08, 1.0),
    # --- OpenAI (Layer B) ---
    "gpt-5-mini": ModelRate(0.25, 2.0, 0.025),
    "gpt-4o-mini": ModelRate(0.15, 0.60, 0.075),
    "text-embedding-3-small": ModelRate(0.02, 0.0),
    "text-embedding-3-large": ModelRate(0.13, 0.0),
    # --- Gemini (Layer B) ---
    "gemini-2.0-flash": ModelRate(0.10, 0.40),
    "gemini-2.5-flash": ModelRate(0.30, 2.50),
}


def _resolve_rate(model: str) -> ModelRate | None:
    """Longest-prefix match of ``model`` against :data:`RATES`.

    Strips a leading ``models/`` (Gemini SDK sometimes prefixes it). Returns
    None for an unknown model — the caller treats that as $0 + a warning.
    """
    if not model:
        return None
    m = model.split("/")[-1]
    best_key = ""
    for key in RATES:
        if m.startswith(key) and len(key) > len(best_key):
            best_key = key
    return RATES[best_key] if best_key else None


def cost_of_turn(usage: dict, model: str) -> float:
    """USD cost of one turn's ``usage`` block against the model's rate card.

    Accepts the Anthropic shape (``input_tokens`` / ``output_tokens`` /
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``). Unknown
    model → 0.0 with a logged warning (graceful — never raises)."""
    rate = _resolve_rate(model)
    if rate is None:
        logger.warning("spend: no rate card for model %r — counted as $0", model)
        return 0.0
    ti = int(usage.get("input_tokens") or 0)
    to = int(usage.get("output_tokens") or 0)
    tcr = int(usage.get("cache_read_input_tokens") or 0)
    tcw = int(usage.get("cache_creation_input_tokens") or 0)
    return (
        ti * rate.input
        + to * rate.output
        + tcr * rate.cache_read
        + tcw * rate.cache_write
    ) / 1_000_000


# --------------------------------------------------------------------------- #
# Summary shape                                                                #
# --------------------------------------------------------------------------- #


@dataclass
class SpendSummary:
    """Aggregated spend over a session or a date range.

    ``unknown`` is set when Layer A could not be read (native jsonl missing or
    unparseable) — the Layer-B numbers are still valid in that case.
    """

    total_usd: float = 0.0
    claude_usd: float = 0.0  # Layer A
    ops_usd: float = 0.0  # Layer B
    by_model: dict[str, float] = field(default_factory=dict)
    by_op: dict[str, float] = field(default_factory=dict)  # Layer B op -> usd
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    n_turns: int = 0  # Layer A assistant turns
    subagent_usd: float = 0.0  # Layer A, isSidechain
    n_spend_events: int = 0  # Layer B
    unknown: bool = False

    @property
    def cache_pct(self) -> float:
        """Share of input tokens served from cache (Layer A signal)."""
        denom = self.tokens_input + self.tokens_cache_read + self.tokens_cache_write
        return 100.0 * self.tokens_cache_read / denom if denom else 0.0

    def _add_claude(self, model: str, usd: float, usage: dict, *, sidechain: bool) -> None:
        self.claude_usd += usd
        self.total_usd += usd
        self.n_turns += 1
        if sidechain:
            self.subagent_usd += usd
        self.by_model[model] = self.by_model.get(model, 0.0) + usd
        self.tokens_input += int(usage.get("input_tokens") or 0)
        self.tokens_output += int(usage.get("output_tokens") or 0)
        self.tokens_cache_read += int(usage.get("cache_read_input_tokens") or 0)
        self.tokens_cache_write += int(usage.get("cache_creation_input_tokens") or 0)

    def _add_op(self, model: str, op: str, usd: float) -> None:
        self.ops_usd += usd
        self.total_usd += usd
        self.n_spend_events += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + usd
        self.by_op[op] = self.by_op.get(op, 0.0) + usd

    def as_dict(self) -> dict:
        return {
            "total_usd": round(self.total_usd, 6),
            "claude_usd": round(self.claude_usd, 6),
            "ops_usd": round(self.ops_usd, 6),
            "by_model": {k: round(v, 6) for k, v in self.by_model.items()},
            "by_op": {k: round(v, 6) for k, v in self.by_op.items()},
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_cache_read": self.tokens_cache_read,
            "tokens_cache_write": self.tokens_cache_write,
            "n_turns": self.n_turns,
            "subagent_usd": round(self.subagent_usd, 6),
            "n_spend_events": self.n_spend_events,
            "cache_pct": round(self.cache_pct, 1),
            "unknown": self.unknown,
        }


# --------------------------------------------------------------------------- #
# Layer B writer — record_spend                                                #
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "personal_mem" / "spend"


def _headless_log_path() -> Path:
    day = datetime.now(timezone.utc).date().isoformat()
    return _cache_root() / "headless" / f"{day}.spend.jsonl"


def record_spend(
    provider: str,
    model: str,
    op: str,
    tokens_input: int,
    tokens_output: int,
    *,
    tokens_cache_read: int = 0,
    tokens_cache_write: int = 0,
    mode: str = "mcp",
    session_id: str | None = None,
    cfg: Config | None = None,
) -> None:
    """Record one internal-op LLM call (Layer B) as a ``type:"spend"`` event.

    Best-effort and silent: spend tracking must **never** break the operation it
    is observing, so every failure is swallowed.

    Routing: if a Claude ``session_id`` is known (argument, else the
    ``PERSONAL_MEM_SESSION_ID`` env var set by the hook/MCP entry) the event is
    appended to that session's buffer — ``archive_buffer`` later folds it into
    ``events.jsonl`` next to the action/retrieval stream. With no session in
    scope (pure-CLI / cron) it lands in a dated headless log under the cache dir.
    """
    try:
        event = {
            "ts": _now_iso(),
            "type": "spend",
            "op": op,
            "provider": provider,
            "model": model,
            "tokens_input": int(tokens_input or 0),
            "tokens_output": int(tokens_output or 0),
            "tokens_cache_read": int(tokens_cache_read or 0),
            "tokens_cache_write": int(tokens_cache_write or 0),
            "mode": mode,
        }
        # Session routing: explicit arg > the contract env var > the session id
        # Claude Code exports to Bash-tool subprocesses. The last is a free win
        # for CLI ops run mid-session (e.g. a /drain skill shelling out to
        # `mem`); a long-lived MCP server without it simply falls back headless.
        sid = (
            session_id
            or os.environ.get("PERSONAL_MEM_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or ""
        )
        if sid:
            event["session_id"] = sid
            cfg = cfg or load_config()
            dest = cfg.mem_dir / "buffer" / f"{sid}.jsonl"
        else:
            dest = _headless_log_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:  # noqa: BLE001 — never propagate into the real op
        logger.debug("record_spend failed for op=%s model=%s", op, model, exc_info=True)


# --------------------------------------------------------------------------- #
# Layer A reader — native Claude transcript                                    #
# --------------------------------------------------------------------------- #


def _native_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def find_native_jsonl(session_id: str) -> Path | None:
    """Locate Claude Code's native transcript for ``session_id``.

    Globs ``~/.claude/projects/*/<session_id>.jsonl`` — robust to the
    cwd-encoding of the parent directory name."""
    if not session_id:
        return None
    matches = sorted(_native_projects_dir().glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _accumulate_native_turns(path: Path, summary: SpendSummary, *, since=None, until=None) -> None:
    """Sum Layer-A assistant turns from one native jsonl into ``summary``.

    Best-effort per line: a malformed or usage-less line is skipped, not fatal.
    ``since`` / ``until`` are ``date`` objects (inclusive) or None.
    """
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "assistant":
                continue
            if since is not None or until is not None:
                d = _row_date(row.get("timestamp"))
                if d is None:
                    continue
                if since is not None and d < since:
                    continue
                if until is not None and d > until:
                    continue
            msg = row.get("message") or {}
            usage = msg.get("usage")
            model = msg.get("model") or ""
            if not isinstance(usage, dict):
                continue
            usd = cost_of_turn(usage, model)
            summary._add_claude(
                model, usd, usage, sidechain=bool(row.get("isSidechain"))
            )


def _row_date(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Layer B reader — spend events                                                #
# --------------------------------------------------------------------------- #


def _accumulate_spend_events(path: Path, summary: SpendSummary, *, since=None, until=None) -> None:
    """Sum Layer-B ``type:"spend"`` events from one jsonl into ``summary``."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "spend":
                continue
            if since is not None or until is not None:
                d = _row_date(ev.get("ts"))
                if d is None:
                    continue
                if since is not None and d < since:
                    continue
                if until is not None and d > until:
                    continue
            model = ev.get("model") or ""
            usage = {
                "input_tokens": ev.get("tokens_input"),
                "output_tokens": ev.get("tokens_output"),
                "cache_read_input_tokens": ev.get("tokens_cache_read"),
                "cache_creation_input_tokens": ev.get("tokens_cache_write"),
            }
            summary._add_op(model, ev.get("op") or "unknown", cost_of_turn(usage, model))


def _session_event_paths(cfg: Config, project: str, session_id: str) -> list[Path]:
    """Where this session's Layer-B spend events may live: the archived
    ``events.jsonl`` (post-extract) and the live buffer (pre-archive). Reading
    both is safe — ``archive_buffer`` deletes the buffer when it writes
    ``events.jsonl``, so they never double-count."""
    paths: list[Path] = []
    buf = cfg.mem_dir / "buffer" / f"{session_id}.jsonl"
    if buf.exists():
        paths.append(buf)
    if project:
        sessions = cfg.vault_root / "projects" / project / "sessions"
        paths.extend(sorted(sessions.glob(f"{session_id}-*/events.jsonl")))
    return paths


# --------------------------------------------------------------------------- #
# Public read entry points                                                     #
# --------------------------------------------------------------------------- #


def read_session_spend(
    session_id: str, *, project: str = "", cfg: Config | None = None
) -> SpendSummary:
    """Total spend for one session = Layer A (native jsonl) + Layer B (events).

    Missing/garbled native transcript → ``unknown=True`` with Layer-B numbers
    still populated. Never raises.
    """
    cfg = cfg or load_config()
    summary = SpendSummary()

    native = find_native_jsonl(session_id)
    if native is None:
        summary.unknown = True
    else:
        try:
            _accumulate_native_turns(native, summary)
        except Exception:  # noqa: BLE001 — degrade, don't raise
            logger.debug("native parse failed for %s", native, exc_info=True)
            summary.unknown = True

    for p in _session_event_paths(cfg, project, session_id):
        try:
            _accumulate_spend_events(p, summary)
        except Exception:  # noqa: BLE001
            logger.debug("spend-event parse failed for %s", p, exc_info=True)

    return summary


def read_range_spend(
    since: str = "",
    until: str = "",
    *,
    cfg: Config | None = None,
) -> SpendSummary:
    """Spend across a date window (inclusive ``YYYY-MM-DD`` bounds, both optional).

    Layer A: every Claude transcript under ``~/.claude/projects/``, filtered by
    turn timestamp. Layer B: the dated headless logs (cron/CLI ops). In-session
    Layer-B events are surfaced per-session via :func:`read_session_spend`; the
    range view covers headless ops, which are the recurring spend worth trending.
    """
    cfg = cfg or load_config()
    summary = SpendSummary()
    since_d = _parse_date(since)
    until_d = _parse_date(until)

    # Layer A — all native transcripts.
    for jsonl in _native_projects_dir().glob("*/*.jsonl"):
        try:
            _accumulate_native_turns(jsonl, summary, since=since_d, until=until_d)
        except Exception:  # noqa: BLE001
            logger.debug("native parse failed for %s", jsonl, exc_info=True)

    # Layer B — dated headless spend logs.
    headless = _cache_root() / "headless"
    if headless.exists():
        for log in sorted(headless.glob("*.spend.jsonl")):
            try:
                _accumulate_spend_events(log, summary, since=since_d, until=until_d)
            except Exception:  # noqa: BLE001
                logger.debug("spend-event parse failed for %s", log, exc_info=True)

    return summary


def _parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None
