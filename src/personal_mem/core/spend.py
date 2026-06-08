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
  emits a ``type:"spend"`` event into a *dedicated per-session ledger*
  (``.mem/spend/<session_id>.jsonl``) — a sink decoupled from the action buffer
  so it never races the Stop-hook archive — or, when no session is in scope, into
  a dated headless log. That per-operation attribution is the thing neither other
  source can give.

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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from personal_mem.core.config import Config, load_config

logger = logging.getLogger(__name__)

# A raw Claude Code session UUID (transcript filename + session-folder prefix).
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# --------------------------------------------------------------------------- #
# Rates                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRate:
    """USD per 1M tokens, by token kind.

    Anthropic distinguishes two cache-creation TTLs (the native transcript
    carries the split under ``usage.cache_creation``): 5-minute writes bill at
    1.25× input, 1-hour writes at 2× input. ``cache_read`` is 0.1× input.
    Providers without a cache concept leave these 0.
    """

    input: float
    output: float
    cache_read: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0


# Flat rate table — the one place to edit when prices move (USD / 1M tokens).
# Keys match as *prefixes* of the model string, longest wins, so a single
# ``claude-opus-4`` entry covers ``claude-opus-4-7``, ``-4-8``, ``-4-20250514``.
# Verified 2026-06-02 against published Anthropic / OpenAI / Google pricing.
# Anthropic cache rows are the derived 0.1× / 1.25× / 2× multiples of input.
RATES: dict[str, ModelRate] = {
    # --- Anthropic (Layer A — Claude agent turns; output = 5× input) ---
    "claude-opus-4": ModelRate(5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-sonnet-4": ModelRate(3.0, 15.0, 0.30, 3.75, 6.0),
    "claude-haiku-4": ModelRate(1.0, 5.0, 0.10, 1.25, 2.0),
    "claude-3-5-haiku": ModelRate(0.80, 4.0, 0.08, 1.0, 1.6),
    # --- OpenAI (Layer B; cached input ~0.1× via auto-caching, no write fee) ---
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


def cost_of_turn(usage: dict, model: str) -> float | None:
    """USD cost of one turn's ``usage`` block against the model's rate card.

    Accepts the Anthropic shape (``input_tokens`` / ``output_tokens`` /
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` and the
    optional ``cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`` split).

    Returns **None** for an unpriced model (no rate card) so callers can surface
    the gap instead of silently booking $0. Never raises.
    """
    rate = _resolve_rate(model)
    if rate is None:
        logger.warning("spend: no rate card for model %r — surfaced as unpriced", model)
        return None
    ti = int(usage.get("input_tokens") or 0)
    to = int(usage.get("output_tokens") or 0)
    tcr = int(usage.get("cache_read_input_tokens") or 0)
    # Cache-creation TTL split (Anthropic native transcript carries it); fall
    # back to booking the whole creation total at the 5-minute rate.
    cc = usage.get("cache_creation") or {}
    tcw5 = cc.get("ephemeral_5m_input_tokens")
    tcw1 = cc.get("ephemeral_1h_input_tokens")
    if tcw5 is None and tcw1 is None:
        tcw5 = int(usage.get("cache_creation_input_tokens") or 0)
        tcw1 = 0
    return (
        ti * rate.input
        + to * rate.output
        + tcr * rate.cache_read
        + int(tcw5 or 0) * rate.cache_write_5m
        + int(tcw1 or 0) * rate.cache_write_1h
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
    # Turns/events whose model has no rate card — surfaced, not silently $0.
    unpriced_turns: int = 0
    unpriced_ops: int = 0
    unpriced_tokens: int = 0
    unpriced_models: set[str] = field(default_factory=set)

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

    def _add_unpriced_turn(self, model: str, usage: dict) -> None:
        self.n_turns += 1
        self.unpriced_turns += 1
        self.unpriced_tokens += int(usage.get("input_tokens") or 0) + int(
            usage.get("output_tokens") or 0
        )
        self.unpriced_models.add(model or "?")

    def _add_unpriced_op(self, model: str, ti: int, to: int) -> None:
        self.n_spend_events += 1
        self.unpriced_ops += 1
        self.unpriced_tokens += int(ti or 0) + int(to or 0)
        self.unpriced_models.add(model or "?")

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
            "unpriced_turns": self.unpriced_turns,
            "unpriced_ops": self.unpriced_ops,
            "unpriced_tokens": self.unpriced_tokens,
            "unpriced_models": sorted(self.unpriced_models),
        }


# --------------------------------------------------------------------------- #
# Layer B writer — record_spend                                                #
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_root() -> Path:
    from personal_mem.core.config import user_cache_dir

    return user_cache_dir() / "spend"


def _headless_log_path() -> Path:
    day = datetime.now(timezone.utc).date().isoformat()
    return _cache_root() / "headless" / f"{day}.spend.jsonl"


def _session_ledger_path(cfg: Config, sid: str) -> Path:
    """The dedicated Route-B ledger for one session: ``.mem/spend/<sid>.jsonl``.

    Keyed by the Claude session UUID (the same key Layer A joins on), so a
    session's total is just ``transcript + this ledger``. Lives under ``.mem``
    rather than the session folder because that folder is minted only at archive
    time — this path is always writable, even mid-live-session."""
    return cfg.mem_dir / "spend" / f"{sid}.jsonl"


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
    appended to that session's **dedicated spend ledger**
    (``.mem/spend/<sid>.jsonl``) — a sink decoupled from the action/retrieval
    buffer, so spend never races the Stop-hook archive nor orphans a
    post-archive buffer. With no session in scope (pure-CLI / cron) it lands in a
    dated headless log under the cache dir. The contract stays literal:
    transcript = Layer A, spend ledger = Layer B, joined on the session UUID.
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
        # `mem`); a long-lived MCP server sets PERSONAL_MEM_SESSION_ID per call
        # when the tool args carry one, else this falls back headless.
        sid = (
            session_id
            or os.environ.get("PERSONAL_MEM_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or ""
        )
        if sid:
            event["session_id"] = sid
            cfg = cfg or load_config()
            dest = _session_ledger_path(cfg, sid)
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


def resolve_native_session_id(session_id: str, project: str, cfg: Config) -> str:
    """Map a personal_mem session-note id (``ses-…``) to the Claude UUID that
    names its native transcript and session folder.

    The native jsonl and the vault session folder are both keyed by the Claude
    UUID (stored as ``source_session`` in the session note), while ``/mem-wrap``
    — like the judge — hands us the ``ses-…`` id. A raw UUID is returned
    unchanged. On any failure we return the input verbatim (callers then degrade
    to ``unknown``). Mirrors how ``find_decisions`` resolves the same alias.
    """
    if not session_id or _UUID_RE.match(session_id):
        return session_id
    from personal_mem.core.vault import parse_frontmatter

    roots = []
    base = cfg.vault_root / "projects"
    if project:
        roots.append(base / project / "sessions")
    else:
        roots.extend(p / "sessions" for p in base.glob("*") if p.is_dir())
    for sessions_dir in roots:
        for note in sessions_dir.glob("*/session.md"):
            try:
                fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if fm.get("id") == session_id or session_id in (fm.get("aliases") or []):
                src = fm.get("source_session")
                if src:
                    return str(src)
                # Fall back to the folder-name prefix (``<uuid>-<date>``).
                return note.parent.name.rsplit("-", 1)[0]
    return session_id


# Slash-command slugs that mark a transcript as personal_mem *operating* cost
# (running the framework's own machinery) rather than the user's coding work.
# Matched against the first user prompt's leading ``/<slug>``.
_MEM_OP_SLUGS: frozenset[str] = frozenset({
    "mem-wrap", "mem-resolve-concepts", "themes-resolve", "dream", "discover",
    "drain", "update-hubs", "newsletter", "youtube", "podcast", "research",
    "news", "substack", "capture", "ingest", "ingest-paper-file", "onboard",
    "source-fit", "source-scaffold", "judge-prediction",
})


def _native_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _first_user_text(path: Path) -> str | None:
    """The first non-empty user-turn text in a native transcript.

    Skips tool-result user rows (their content blocks carry no ``text``) so the
    value returned is the human/headless prompt that opened the run. Best-effort:
    returns None on any read/parse trouble."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "user":
                    continue
                content = (row.get("message") or {}).get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, str):
                            text = blk
                            break
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            text = blk.get("text")
                            break
                if text and text.strip():
                    return text.strip()
    except OSError:
        return None
    return None


def _transcript_op_label(path: Path) -> str | None:
    """The mem-op label for a transcript whose first prompt is a ``/<mem-skill>``
    invocation (e.g. ``/dream`` → ``dream``), else None.

    A headless ``claude -p "/dream"`` run opens its own transcript whose first
    user turn is the slash command — that is the signal that the whole transcript
    is mem-operating cost. An interactive coding session (first prompt is prose)
    returns None and is excluded from the ``--ops-only`` view. Limit: a mem skill
    invoked *inside* an interactive session blends its Claude turns with the
    user's and is not separable here (only its Layer-B portion is)."""
    text = _first_user_text(path)
    if not text or not text.startswith("/"):
        return None
    slug = text[1:].split(maxsplit=1)[0].strip().lower()
    return slug if slug in _MEM_OP_SLUGS else None


def find_native_jsonl(session_id: str) -> Path | None:
    """Locate Claude Code's native transcript for ``session_id``.

    Globs ``~/.claude/projects/*/<session_id>.jsonl`` — robust to the
    cwd-encoding of the parent directory name."""
    if not session_id:
        return None
    matches = sorted(_native_projects_dir().glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _accumulate_native_turns(
    path: Path, summary: SpendSummary, *, since=None, until=None, op_label: str | None = None
) -> None:
    """Sum Layer-A assistant turns from one native jsonl into ``summary``.

    Best-effort per line: a malformed or usage-less line is skipped, not fatal.
    ``since`` / ``until`` are ``date`` objects (inclusive) or None. When
    ``op_label`` is given (the transcript is a mem-operating run, e.g. ``dream``)
    each turn's cost is *also* bucketed under ``by_op[op_label]`` so the
    ``--ops-only`` view can sum Layer-A op cost next to native Layer-B ops.
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
            if usd is None:
                summary._add_unpriced_turn(model, usage)
            else:
                summary._add_claude(
                    model, usd, usage, sidechain=bool(row.get("isSidechain"))
                )
                if op_label:
                    summary.by_op[op_label] = summary.by_op.get(op_label, 0.0) + usd


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
            usd = cost_of_turn(usage, model)
            if usd is None:
                summary._add_unpriced_op(model, ev.get("tokens_input"), ev.get("tokens_output"))
            else:
                summary._add_op(model, ev.get("op") or "unknown", usd)


def _session_event_paths(cfg: Config, project: str, session_id: str) -> list[Path]:
    """Where this session's Layer-B spend events may live.

    Current sink: the dedicated ledger ``.mem/spend/<sid>.jsonl``. The legacy
    locations — the live buffer (pre-archive) and the archived ``events.jsonl``
    (where spend was meshed before the sink was decoupled) — are still read so
    historical sessions keep their Layer-B numbers. The three are disjoint
    namespaces (a given event is written to exactly one), so reading all three
    never double-counts."""
    paths: list[Path] = []
    ledger = _session_ledger_path(cfg, session_id)
    if ledger.exists():
        paths.append(ledger)
    buf = cfg.mem_dir / "buffer" / f"{session_id}.jsonl"  # legacy pre-archive
    if buf.exists():
        paths.append(buf)
    if project:  # legacy meshed-into-events.jsonl
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

    # ``ses-…`` (what /mem-wrap passes) → Claude UUID (what names the transcript
    # and the session folder). A raw UUID resolves to itself.
    uuid = resolve_native_session_id(session_id, project, cfg)

    native = find_native_jsonl(uuid)
    if native is None:
        summary.unknown = True
    else:
        try:
            _accumulate_native_turns(native, summary)
        except Exception:  # noqa: BLE001 — degrade, don't raise
            logger.debug("native parse failed for %s", native, exc_info=True)
            summary.unknown = True

    for p in _session_event_paths(cfg, project, uuid):
        try:
            _accumulate_spend_events(p, summary)
        except Exception:  # noqa: BLE001
            logger.debug("spend-event parse failed for %s", p, exc_info=True)

    return summary


def read_range_spend(
    since: str = "",
    until: str = "",
    *,
    ops_only: bool = False,
    cfg: Config | None = None,
) -> SpendSummary:
    """Spend across a date window (inclusive ``YYYY-MM-DD`` bounds, both optional).

    Layer A: every Claude transcript under ``~/.claude/projects/``, filtered by
    turn timestamp. Layer B: the dated headless logs (cron/CLI ops). In-session
    Layer-B events are surfaced per-session via :func:`read_session_spend`; the
    range view covers headless ops, which are the recurring spend worth trending.

    ``ops_only=True`` answers "what does running personal_mem's own machinery
    cost", distinct from the user's coding work: Layer A is restricted to
    transcripts whose first prompt is a ``/<mem-skill>`` invocation (each bucketed
    under ``by_op`` by its skill), and interactive coding transcripts are dropped.
    Layer B is already per-op, so all of it is mem-operating cost and is kept.
    """
    cfg = cfg or load_config()
    summary = SpendSummary()
    since_d = _parse_date(since)
    until_d = _parse_date(until)

    # Layer A — native transcripts. In --ops-only, keep only the mem-skill runs,
    # labelled by op; otherwise every transcript counts.
    for jsonl in _native_projects_dir().glob("*/*.jsonl"):
        label = _transcript_op_label(jsonl) if ops_only else None
        if ops_only and label is None:
            continue
        try:
            _accumulate_native_turns(
                jsonl, summary, since=since_d, until=until_d, op_label=label
            )
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
