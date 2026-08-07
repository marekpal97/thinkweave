"""Claude Code Pre/PostToolUse/Stop/SessionStart/UserPromptSubmit hook handler.

Invoked as the `weave-hook` console script (declared in pyproject.toml).
pip/uv materialize this as a cross-platform executable, so Claude Code
calls it directly from settings.local.json with no shell wrapper.

Input: JSON via stdin (tool_name, tool_input, session_id, etc.)
Output: JSON to stdout following Claude Code hook protocol.
Exit 0 = protocol success. Persistence failures use the visible
``systemMessage`` response field as well as the best-effort hook log.

SessionStart: Injects ~7–10k tokens of structured project context
  (recent sessions, STATE, backlog, decisions, tool manifest) so Claude
  wakes up oriented. Never blocks the harness; failures remain visible.
UserPromptSubmit: Captures every user prompt as a structured "prompt"
  event in the JSONL buffer. Promotes user prompts into a first-class
  primitive (`Prompt`) — replaces the heuristic `probe`-tag flow.
PostToolUse (Write|Edit|Bash): Buffers events to JSONL. Session note
  materialization is deferred to Stop hook.
Stop: Reconstructs session from buffer, writes summary, indexes once.

Duplicate deliveries (#161): when a lifecycle event arrives carrying a
  wire id the harness minted for it (``tool_use_id`` / ``turn_id``), the
  buffer write is guarded by a delivery receipt so two registrations of
  the same hook persist the event once. Events with NO wire id — Claude
  Code's UserPromptSubmit, SessionStart, a PostToolUse without a
  ``tool_use_id`` — are deliberately NOT deduped: nothing on the wire
  distinguishes "the same delivery twice" from "the user really did send
  `continue` twice", and guessing via a content hash gets both wrong.
  Their cure is single-owner registration (`surfaces/hooks/install.py`),
  not a receipt.

Note: an earlier PreToolUse(Write|Edit) handler injected "Related vault
notes" before each file edit. It was redundant with SessionStart context
and the filename-stem heuristic produced noisy hits, so it was removed.
Re-running `weave hooks install` strips any stale PreToolUse entry from
existing settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Lazy imports to keep hook startup fast


def _log_error(hook_type: str, error: Exception) -> None:
    """Log hook errors to file. Never blocks Claude Code."""
    try:
        import traceback

        from thinkweave.core.config import load_config

        cfg = load_config()
        log_path = cfg.weave_dir / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {hook_type}: {error}\n")
            traceback.print_exc(file=f)
            f.write("\n")
    except Exception:
        pass  # Last resort: silent failure on logging itself


def _log_info(hook_type: str, message: str) -> None:
    """Log non-error hook telemetry (e.g. an R2 deadline miss) to file.

    Sibling to :func:`_log_error` minus the traceback — for events that are
    expected/handled outcomes, not failures. Never blocks Claude Code.
    """
    try:
        from thinkweave.core.config import load_config

        cfg = load_config()
        log_path = cfg.weave_dir / "hooks.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {hook_type}: {message}\n")
    except Exception:
        pass  # Last resort: silent failure on logging itself


def _failure_message(hook_type: str, error: Exception) -> str:
    """Return an actionable message on the hook protocol's visible channel."""
    return (
        f"ThinkWeave {hook_type} failed; lifecycle data was not persisted "
        f"({type(error).__name__}: {error}). Check vault access and "
        "<vault>/.weave/hooks.log."
    )


def _report_failure(hook_type: str, hook_input: dict, error: Exception) -> str:
    """The visible failure message, at most once per session per hook type.

    A persistent vault problem (a permission denial, a full disk) fails on
    every delivery — unthrottled, that is a systemMessage on every single tool
    call for the rest of the session. The first one carries the whole
    diagnosis; the rest are noise. Every failure still reaches
    ``<vault>/.weave/hooks.log`` via :func:`_log_error`.

    Returns ``""`` once the session has already been told, which
    :func:`_output` renders as no ``systemMessage`` at all.
    """
    if _first_failure(hook_type, hook_input):
        return _failure_message(hook_type, error)
    return ""


def _first_failure(hook_type: str, hook_input: dict) -> bool:
    """True the first time this session reports a failure of ``hook_type``.

    Each hook fires in its own process, so the "already told them" bit is a
    marker file in the session's buffer-side scratch dir (cleaned with the
    buffer at Stop). Best-effort in the honest direction: if the marker cannot
    be written — plausibly the same vault-access failure being reported — the
    message goes out.
    """
    try:
        from thinkweave.core.config import load_config

        session_id = str(hook_input.get("session_id", "")) or "_unknown"
        state = session_state_dir(load_config().weave_dir, session_id)
        state.mkdir(parents=True, exist_ok=True)
        marker = state / f"failed.{hook_type.replace('/', '-')}"
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _hook_harness() -> str:
    """Which harness fired this hook, from our own argv.

    ``weave hooks install`` appends ``--harness <id>`` for every harness but
    Claude Code; why argv rather than ``harness.active()`` is
    docs/HARNESSES.md § "Why the handler reads argv, not the profile". Empty
    string means the Claude Code default, which keeps its buffer bytes
    identical to every session written before #107.

    The value is unvalidated on purpose and that is safe: it is only ever
    stamped onto the buffer as ``surface``, and its one consumer looks it up
    in the closed ``indexer._STARTUP_SOURCES`` map, where anything unrecognised
    falls back to plain ``'startup'``. An argv nobody but this repo's installer
    writes therefore cannot reach the ``context_served`` CHECK constraint.
    """
    argv = sys.argv
    if "--harness" in argv:
        i = argv.index("--harness")
        if i + 1 < len(argv):
            return argv[i + 1]
    return ""


def main() -> None:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    hook_input = _read_stdin()
    tool_name = hook_input.get("tool_name", "")

    # Early-return gate: if the vault hasn't been initialised yet, every
    # hook is a no-op. Replaces the bash gate in hooks/hooks.json — that
    # gate (a) checked a stale Phase-3.1 path and (b) used bash idioms
    # that don't parse under cmd.exe. Matches the "fail-silent, exit 0"
    # posture of the existing try/except below.
    try:
        from thinkweave.core.config import is_vault_initialized, load_config

        if not is_vault_initialized(load_config()):
            _output()
            return
    except Exception as e:
        _log_error(hook_type, e)
        _output(system_message=_report_failure(hook_type, hook_input, e))
        return

    try:
        if hook_type == "post_tool_use":
            _handle_post(tool_name, hook_input)
        elif hook_type == "stop":
            _handle_stop(hook_input)
        elif hook_type == "session_start":
            _handle_session_start(hook_input)
        elif hook_type == "user_prompt_submit":
            _handle_user_prompt_submit(hook_input)
        else:
            # Includes legacy `pre_tool_use` invocations from settings.json
            # entries written before that hook was retired. Falls through
            # to an empty {} payload, which Claude Code treats as a no-op.
            _output()
    except Exception as e:
        _log_error(hook_type, e)
        _output(system_message=_report_failure(hook_type, hook_input, e))


def _handle_post(tool_name: str, hook_input: dict) -> None:
    """PostToolUse: buffer event to JSONL and ensure session note exists.

    Lean by design — all heavy work (frontmatter updates, summary,
    FTS indexing) is deferred to the Stop hook. The JSONL buffer is
    the source of truth during the session.

    Gated to Write/Edit/Bash (file/command activity) plus the closed set
    of thinkweave MCP retrieval tools. Retrieval events feed the RLVR
    decision-context substrate — see ``operations/retrieval_log.py``.

    Performance note: on retrieval-tool calls (``mcp__thinkweave__weave_search``
    etc.) we deliberately *skip* the ``_ensure_session`` materialisation that
    action tools trigger. The session note will be lazily created by the
    next action/prompt event (already cheap there), or by the Stop hook's
    fallback path for retrieval-only sessions. Without this skip, every
    MCP retrieval call would pay an ``rglob(*.md)`` scan over the entire
    vault — for large vaults that blows past Claude Code's 5s hook timeout
    and the hook is cancelled before the buffer write lands, dropping the
    retrieval event entirely. Measured 2026-05-26 against a ~1.5k-note
    vault on WSL→9P: every ``weave_search`` PostToolUse hook was cancelled.
    """
    from thinkweave.operations.retrieval_log import RETRIEVAL_TOOLS

    is_action_tool = tool_name in ACTION_TOOLS
    is_retrieval_tool = tool_name in RETRIEVAL_TOOLS
    if not (is_action_tool or is_retrieval_tool):
        _output()
        return

    tool_input = hook_input.get("tool_input", {})

    try:
        from thinkweave.core.config import load_config

        cfg = load_config()

        session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
        now = datetime.now(timezone.utc).isoformat()

        # Buffer the event (crash-safe, append-only). Retrieval and action
        # events are kept in the same buffer file — the Stop-time finalizer
        # partitions them into events.jsonl vs retrieval_log.jsonl.
        if is_action_tool:
            # Claude Code's PostToolUse payload uses ``tool_response`` (newer,
            # an object with stdout/stderr) or ``tool_output`` (older string
            # form); _extract_tool_output_text normalises both so the parsers
            # (_parse_commit_from_output, _extract_insight_blocks) don't need
            # provider-version awareness. Only recognised text shapes survive
            # it — see its docstring.
            events = _build_events(
                tool_name, tool_input, _extract_tool_output_text(hook_input), now
            )
        else:
            from thinkweave.operations.retrieval_log import build_retrieval_event

            # Raw, not normalised: a retrieval response IS the rendered answer,
            # whatever shape the harness wraps it in, and `build_retrieval_event`
            # mines the whole object for note ids (#107).
            events = [
                build_retrieval_event(
                    tool_name, tool_input, _raw_tool_response(hook_input), now
                )
            ]
        recorded = False
        for i, event in enumerate(events):
            if event:
                delivery_id = _delivery_id(
                    "post_tool_use", hook_input, suffix=str(i)
                )
                if delivery_id:
                    event["delivery_id"] = delivery_id
                recorded = _buffer_event(cfg.weave_dir, session_id, event) or recorded

        # Action-tool path materialises the session note (so MCP tools can
        # discover it mid-conversation). Retrieval path defers — Stop hook
        # creates one from the buffer if nothing else does. Keeps the
        # retrieval hook latency O(buffer-append) rather than O(vault-scan).
        if is_action_tool and recorded:
            _ensure_session(cfg, session_id, hook_input)

        _output()
    except Exception as e:
        _log_error("post_tool_use", e)
        _output(system_message=_report_failure("post_tool_use", hook_input, e))


def _handle_user_prompt_submit(hook_input: dict) -> None:
    """UserPromptSubmit: append a structured prompt event to the JSONL buffer.

    Schema written to ``buffer/<session_id>.jsonl``::

        {"ts": "...", "type": "prompt", "text": "...",
         "session_id": "...", "cwd": "..."}

    Promotes user prompts into a first-class primitive that ``extract.py``
    can lift into ``Prompt`` objects + classify as probes — replacing the
    older heuristic ``probe`` tag flow. Never blocks the harness; persistence
    failures use its visible diagnostic channel.
    """
    try:
        from thinkweave.core.config import load_config

        cfg = load_config()

        session_id = hook_input.get(
            "session_id", os.environ.get("CLAUDE_SESSION_ID", "")
        )
        prompt_text = hook_input.get("prompt", hook_input.get("user_prompt", ""))
        if not session_id or not prompt_text:
            _output()
            return

        now = datetime.now(timezone.utc).isoformat()
        cwd = hook_input.get("cwd", "")
        # Claude Code's UserPromptSubmit carries no wire id, so this is ""
        # there and the prompt is captured unconditionally — sending the same
        # prompt twice is a real repeat, not a replay. Codex stamps a
        # ``turn_id``, and there the receipt collapses duplicate deliveries.
        delivery_id = _delivery_id("user_prompt_submit", hook_input)
        event = {
            "ts": now,
            "type": "prompt",
            "text": prompt_text,
            "session_id": session_id,
            "cwd": cwd,
        }
        if delivery_id:
            event["delivery_id"] = delivery_id
        if not _buffer_event(cfg.weave_dir, session_id, event):
            _output()
            return

        # Eagerly create the session note too, so a buffer that begins
        # with prompts (no Edit/Bash yet) still has a note to attach to.
        _ensure_session(cfg, session_id, hook_input)

        # R2 — prompt-time retrieval enrichment. Bounded, deduped against the
        # live buffer, hard-capped. Any failure here must fall through to a
        # plain (empty) response — never break the user's turn.
        block = _prompt_time_enrichment(
            cfg, session_id, prompt_text, now, delivery_id=delivery_id
        )

        # Feedback classification does NOT happen here (#101): hooks capture,
        # never judge. The raw prompt event above is the whole substrate;
        # ``feedback`` events are appended asynchronously by the /wrap LLM
        # pass via ``weave wrap-finalize --verdicts`` (catch-up in /dream's
        # wrap-worker). The old inline lexicon misread machine-generated
        # prompt text (<task-notification> blobs) as endorsements — a model
        # judging with conversation context is the labeler, not a regex.

        if block:
            _output(
                additional_context=block,
                hook_event_name="UserPromptSubmit",
            )
            return

        _output()
    except Exception as e:
        _log_error("user_prompt_submit", e)
        _output(
            system_message=_report_failure("user_prompt_submit", hook_input, e)
        )


def _prompt_time_enrichment(
    cfg, session_id: str, prompt_text: str, now: str, *, delivery_id: str = ""
) -> str | None:
    """Build the R2 enrichment block and record the outcome to the buffer.

    Returns the block to inject, or ``None`` to no-op. Self-contained: on any
    error it logs and returns ``None`` so the caller emits a plain response.
    ``build_enrichment`` is pure (read-only) — this function owns every
    buffer write-back on its behalf:

    - On a fresh block: a ``retrieval`` event tagged with ``PROMPT_TIME_TOOL``
      so (1) the next turn's dedup sees these ids and (2) the indexer
      projects them to ``context_served`` with ``source='prompttime'``.
    - On a deadline miss: a distinct ``prompt_time_miss`` telemetry event
      (never tagged ``PROMPT_TIME_TOOL``, never typed ``retrieval`` — see
      ``operations/prompt_time_retrieval``'s module docstring for why that
      distinction matters) plus an info line in the hooks log, so a run of
      misses is visible instead of silently re-paying the embedding deadline
      every turn.
    """
    try:
        from thinkweave.operations.prompt_time_retrieval import (
            PROMPT_TIME_MISS,
            PROMPT_TIME_TOOL,
            build_enrichment,
        )

        block, served_ids, missed = build_enrichment(cfg, session_id, prompt_text)

        if missed:
            miss = {
                "ts": now,
                "type": PROMPT_TIME_MISS,
                "session_id": session_id,
            }
            if delivery_id:
                miss["delivery_id"] = f"{delivery_id}:prompt-time-miss"
            _buffer_event(cfg.weave_dir, session_id, miss)
            _log_info(
                "prompt_time_enrichment",
                f"deadline miss for session {session_id}",
            )

        if not block:
            return None

        served = {
            "ts": now,
            "type": "retrieval",
            "tool": PROMPT_TIME_TOOL,
            "returned_ids": served_ids,
            "chars": len(block),
            "token_est": len(block) // 4,
        }
        if delivery_id:
            served["delivery_id"] = f"{delivery_id}:prompt-time-retrieval"
        _buffer_event(cfg.weave_dir, session_id, served)
        return block
    except Exception as e:
        _log_error("prompt_time_enrichment", e)
        return None


def _find_session_note(vm, session_id: str) -> Path | None:
    """Find an existing session note for this Claude Code session.

    Fast path: SQL probe against the indexer's ``notes`` table for any
    ``type='session'`` row whose ``frontmatter`` blob contains
    ``"source_session": "<id>"``. O(rows-with-type-session) substring
    match, no markdown reads, no rglob.

    Slow path: a bounded, sessions-only glob —
    ``projects/*/sessions/*/session.md`` — never a vault-wide walk.
    Candidates are checked newest-first with a hard cap: this path is only
    reached when the index DB is missing, locked, or stale (session note was
    just created and hasn't been indexed yet), and in that just-created case
    the note we want is the most recently modified one, so the common case
    is a single frontmatter read. Session folder names are ``<slug>-<date>``
    (see ``VaultManager``), never derived from the Claude Code session UUID,
    so a name match is impossible — frontmatter is the only place the id
    lives.

    Measured 16s for the previous fallback (``vm.list_notes(note_type=
    SESSION, limit=20)``) on a ~1k-note vault over WSL2's 9P filesystem —
    that helper's ``rglob("*.md")`` reads and parses EVERY note's
    frontmatter across the whole vault (decisions, sources, themes, ...)
    until it accumulates ``limit`` session matches. Scoping the glob to the
    ``sessions/<id>/session.md`` shape skips every non-session note's
    content entirely, bounding the scan to the sessions that actually exist
    instead of the whole vault.
    """
    if not session_id:
        return None

    # Fast path — SQLite probe. Substring LIKE on frontmatter is fine here:
    # ``type='session'`` filter is selective (sessions are a tiny fraction
    # of the notes table) and ``source_session`` values are UUIDs, so the
    # match is unambiguous. We open a read-only connection so a contended
    # write lock (e.g. ``weave index`` running concurrently) never blocks us.
    try:
        import sqlite3

        cfg = vm.config
        if cfg.index_db.exists():
            uri = f"file:{cfg.index_db}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
                row = db.execute(
                    "SELECT path FROM notes "
                    "WHERE type='session' AND frontmatter LIKE ? "
                    "LIMIT 1",
                    (f'%"source_session": "{session_id}"%',),
                ).fetchone()
                if row and row[0]:
                    p = Path(row[0])
                    abs_p = p if p.is_absolute() else vm.root / p
                    if abs_p.exists():
                        return abs_p
    except Exception:
        # Fall through to the bounded glob on any DB issue.
        pass

    # Slow path — sessions-only glob, no vault-wide rglob of any kind.
    # Newest-first, capped: the stale-index window this backstop covers is
    # "created moments ago", so the target is at (or near) the front. A miss
    # under the cap means "not found" — creation dedupes on source_session,
    # so the worst case is a rare duplicate session note, not data loss.
    try:
        candidates = sorted(
            vm.root.glob("projects/*/sessions/*/session.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for note_path in candidates[:15]:
        try:
            note = vm.read_note(note_path)
            if note.frontmatter.get("source_session") == session_id:
                return note_path
        except Exception:
            continue
    return None


def _ensure_session(cfg, session_id: str, hook_input: dict) -> None:
    """Create session note on first event, index it once for MCP discoverability."""
    if not session_id:
        return

    from thinkweave.core.schemas import NoteType
    from thinkweave.core.vault import VaultManager

    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    if _find_session_note(vm, session_id):
        return

    project = _detect_project(hook_input)
    session_path = vm.create_note(
        NoteType.SESSION,
        f"Session {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        project=project,
        extra_frontmatter={"source_session": session_id},
    )

    # Index once so MCP tools (weave_search) can find this session mid-conversation
    from thinkweave.core.indexer import Indexer

    idx = Indexer(config=cfg)
    idx.index_file(session_path)
    idx.close()


_EPHEMERAL_CWD_RE = re.compile(r"^(agent-[a-f0-9]{12,}|[a-f0-9-]{32,})$")


def _detect_project(hook_input: dict) -> str:
    """Detect the current project from env var, git, or cwd.

    Priority: THINKWEAVE_PROJECT env var > git repo name > cwd directory name.

    When cwd looks ephemeral (e.g. ``agent-a4701018f1189051e/`` from a
    cloud-agent run, or a bare UUID), fall through to ``_unscoped`` instead
    of letting the runtime's session-id leak in as a project name.
    """
    # PERSONAL_MEM_PROJECT: pre-rename migration fallback (→ thinkweave 2026-06-13).
    env_proj = os.environ.get("THINKWEAVE_PROJECT") or os.environ.get("PERSONAL_MEM_PROJECT")
    if env_proj:
        return env_proj

    cwd = hook_input.get("cwd", os.getcwd())
    cwd_path = Path(cwd)

    # Walk up to find a .git directory — use that repo's directory name
    for parent in [cwd_path, *cwd_path.parents]:
        if (parent / ".git").exists():
            return parent.name
        if parent == parent.parent:
            break

    if _EPHEMERAL_CWD_RE.match(cwd_path.name):
        return "_unscoped"
    return cwd_path.name


# Directory markers — matched anywhere in the path, since every file under
# one is the harness's own furniture.
_INTERNAL_DIRS = (".claude/", ".codex/", ".weave/")
# Whole-filename markers. `settings.json` stays here for the bare-root case;
# `.claude/settings.json` is already covered above.
_INTERNAL_FILES = frozenset(
    {"claude.md", "claude.local.md", "agents.md", "settings.json"}
)


def _is_internal(path: str) -> bool:
    """Check if a path is an internal/config file we should ignore.

    A union of both harnesses' furniture (Claude Code's ``.claude/`` +
    ``CLAUDE.md``, Codex's ``.codex/`` + ``AGENTS.md``) rather than profile
    data — see docs/HARNESSES.md § "Why the handler reads argv, not the
    profile" (#107).

    Note ``hooks.json`` is deliberately absent: thinkweave's own canonical
    ``hooks/hooks.json`` is project work. ``.codex/`` already covers both the
    repo-local and the ``$CODEX_HOME`` copies of Codex's.

    Filenames match as a whole path component, not as a substring: the latter
    read ``docs/subagents.md`` and ``multi-agents.md`` as Codex's ``AGENTS.md``
    and dropped them from ``files_touched`` silently.
    """
    p = path.lower().replace("\\", "/")
    if any(d in p for d in _INTERNAL_DIRS):
        return True
    return p.rsplit("/", 1)[-1] in _INTERNAL_FILES


# ---------------------------------------------------------------------------
# Event buffer — crash-safe append-only JSONL
# ---------------------------------------------------------------------------


def _delivery_id(phase: str, hook_input: dict, *, suffix: str = "") -> str:
    """Identity of one harness delivery — or ``""`` when the wire has none.

    Only ``tool_use_id`` / ``turn_id`` count: those are minted by the harness
    per delivery, so two registrations receiving the same envelope agree on
    the id and the second write can be dropped (#161).

    An event without one gets no identity, and :func:`_buffer_event` then
    writes it unconditionally. Deriving a surrogate from the payload was tried
    and reverted: it is wrong in both directions. A content hash collides on
    genuine repeats (the user sending ``continue`` twice would persist once),
    and mixing in a volatile discriminator like the transcript's size lets a
    single byte written between two registrations' deliveries split the hash —
    reproducing exactly the duplicate #161 reported. Duplicate id-less
    deliveries are cured upstream, by single-owner registration.
    """
    wire_id = hook_input.get("tool_use_id") or hook_input.get("turn_id")
    if not wire_id:
        return ""
    session_id = str(hook_input.get("session_id", ""))
    tail = f":{suffix}" if suffix else ""
    return f"{phase}:{session_id}:{wire_id}{tail}"


def _buffer_event(weave_dir: Path, session_id: str, event: dict) -> bool:
    """Append one event unless its delivery receipt already exists.

    Returns True when the line was written. An event with no ``delivery_id``
    (see :func:`_delivery_id`) is always written.
    """
    buf_dir = weave_dir / "buffer"
    buf_dir.mkdir(parents=True, exist_ok=True)
    receipt: Path | None = None
    delivery_id = event.get("delivery_id")
    if delivery_id:
        receipts = session_state_dir(weave_dir, session_id)
        receipts.mkdir(parents=True, exist_ok=True)
        receipt = receipts / hashlib.sha256(
            str(delivery_id).encode("utf-8")
        ).hexdigest()
        try:
            fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return False

    try:
        with open(buf_dir / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        if receipt is not None:
            receipt.unlink(missing_ok=True)
        raise
    return True


def _extract_tool_output_text(hook_input: dict) -> str:
    """Pull the tool's output as a text string from a PostToolUse payload.

    Claude Code's PostToolUse hook delivers the tool result under the
    ``tool_response`` key (not ``tool_output``). For the ``Bash`` tool
    specifically, ``tool_response`` is an *object* shaped like::

        {"stdout": "...", "stderr": "...", "interrupted": false, "isImage": false}

    For other tools (Write/Edit/MCP) it can be a string or an object with
    tool-specific fields. This helper normalises any of those into a single
    text blob the downstream parsers (``_parse_commit_from_output``,
    ``_parse_test_result``, ``_extract_insight_blocks``, retrieval-event
    builder) can scan with regex.

    Order of preference:

    1. ``tool_response`` — current Claude Code key. When a dict, concatenate
       ``stdout`` + ``stderr`` (``git commit`` prints to stdout; ``pytest``
       splits between the two; both regexes are fine on the concatenation).
       When a string, use as-is.
    2. ``tool_output`` — legacy key, kept for back-compat with any older
       harness build or test fixture that still uses it.

    Returns an empty string when nothing usable is present, which downstream
    parsers already treat as a clean no-op.

    Root-cause note: until this normalisation landed, ``_handle_post`` read
    ``tool_output`` and got ``""`` for every Bash invocation — which meant
    ``_parse_commit_from_output`` returned ``None`` and the ``commit``
    subfield was never written. Empirically 0/405 native hook-emitted
    sessions ever carried ``commits[]``. Audit item A1.
    """
    raw = _raw_tool_response(hook_input)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        stdout = raw.get("stdout", "") or ""
        stderr = raw.get("stderr", "") or ""
        if not isinstance(stdout, str):
            stdout = str(stdout)
        if not isinstance(stderr, str):
            stderr = str(stderr)
        if stdout and stderr:
            return stdout + "\n" + stderr
        if stdout or stderr:
            return stdout or stderr

        # Any other dict shape is deliberately *not* mined for text. Write's
        # and Edit's `tool_response` echo back what was written (`content`,
        # `originalFile`), which is the edited file, not tool output — harvest
        # it and `_extract_insight_blocks` re-captures every ★ Insight block
        # living in the source on every touch. Unknown-shape recovery belongs
        # to the retrieval path, where the payload really is a rendered answer;
        # see `retrieval_log.response_text` (#107).
        return ""
    return ""


def _raw_tool_response(hook_input: dict):
    """The tool result exactly as the harness sent it, un-normalised.

    Same key preference as :func:`_extract_tool_output_text` — that helper
    flattens for the action path, the retrieval path wants the object.
    """
    raw = hook_input.get("tool_response")
    return hook_input.get("tool_output", "") if raw is None else raw


# Every tool name that counts as file/command activity, across both
# harnesses. Claude Code edits files as `Write`/`Edit`; Codex routes every
# edit through a single `apply_patch` call and reports that as the tool name
# (`Edit`/`Write` are matcher aliases only — they never appear in the
# payload). Union rather than profile data, for the reason in `_is_internal`.
ACTION_TOOLS = ("Write", "Edit", "Bash", "apply_patch")

# Codex's apply_patch envelope. Markers transcribed from the codex-cli 0.146.0
# binary: `*** Add File: `, `*** Update File:`, `*** Delete File: `,
# `*** Move to: `. Note `Move to` carries no `File` keyword.
_PATCH_OP_RE = re.compile(
    r"^\*\*\* (Add|Update|Delete|Move to)(?: File)?: (.+?)\s*$",
    re.MULTILINE,
)

# Which buffer-vocabulary verb each patch operation becomes. Downstream
# consumers (`core/events.py`, `_summarize_events`) match on
# `tool in ("Edit", "Write")`; normalising here — at the one wire boundary —
# keeps them harness-agnostic instead of teaching each a second vocabulary.
# Codex documents `Edit`/`Write` as its own aliases for `apply_patch`, so this
# is its mapping, not ours.
_PATCH_OP_TOOL = {
    "Add": "Write",
    "Move to": "Write",
    "Update": "Edit",
    "Delete": "Edit",
}


def _build_events(
    tool_name: str, tool_input: dict, tool_output, now: str
) -> list[dict]:
    """Structured buffer events for one PostToolUse call.

    A list because Codex's ``apply_patch`` can touch several files in one
    call, where Claude Code would have fired one ``Write``/``Edit`` per file.
    Every other tool yields at most one event.
    """
    if tool_name != "apply_patch":
        event = _build_event(tool_name, tool_input, tool_output, now)
        return [event] if event else []

    # ★ Insight blocks are not scanned here: apply_patch's output is a list of
    # touched files, never model prose. Bash keeps that enrichment.
    events = []
    for op, path in _PATCH_OP_RE.findall(tool_input.get("command", "")):
        if _is_internal(path):
            continue
        events.append(
            {
                "ts": now,
                "tool": _PATCH_OP_TOOL[op],
                "file": path,
                "context": f" — apply_patch ({op.lower()})",
            }
        )
    return events


def _build_event(tool_name: str, tool_input: dict, tool_output, now: str) -> dict | None:
    """Build a structured event dict for the buffer.

    Enriches Bash events with parsed commit/test/push metadata so the
    Stop hook can reconstruct session frontmatter from the buffer alone.
    """
    output_str = tool_output if isinstance(tool_output, str) else ""

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", tool_input.get("path", ""))
        if not file_path or _is_internal(file_path):
            return None
        context = _diff_context(tool_name, tool_input)
        event: dict = {"ts": now, "tool": tool_name, "file": file_path, "context": context}
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if not _is_significant_command(command):
            return None
        event = {"ts": now, "tool": "Bash", "command": command[:80]}

        # Enrich with structured metadata
        if _is_git_commit(command):
            commit_info = _parse_commit_from_output(command, output_str)
            if commit_info:
                if commit_info.get("hash"):
                    files = _get_commit_files(commit_info["hash"])
                    if files:
                        commit_info["files"] = files
                event["commit"] = commit_info
        if _is_test_command(command):
            test_info = _parse_test_result(command, output_str)
            if test_info:
                event["test_run"] = test_info
        if "git push" in command.lower():
            branch = _parse_push_branch(command)
            if branch:
                event["git_branch"] = branch
    else:
        return None

    # Capture ★ Insight blocks from tool output
    if output_str:
        insights = _extract_insight_blocks(output_str)
        if insights:
            event["insights"] = insights

    return event


def _read_buffer(weave_dir: Path, session_id: str) -> list[dict]:
    """Read all events from the JSONL buffer for a session."""
    buf_file = weave_dir / "buffer" / f"{session_id}.jsonl"
    if not buf_file.exists():
        return []
    events = []
    for line in buf_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _summarize_events(events: list[dict]) -> dict:
    """Extract structured metadata from buffered events.

    Returns dict with files_touched, commits, test_runs, insights, git_branch.
    """
    files: list[str] = []
    commits: list[dict] = []
    test_runs: list[dict] = []
    insights: list[str] = []
    git_branch = ""

    for ev in events:
        if "file" in ev:
            files.append(ev["file"])
        if "commit" in ev:
            commits.append(ev["commit"])
        if "test_run" in ev:
            test_runs.append(ev["test_run"])
        if "insights" in ev:
            insights.extend(ev["insights"])
        if "git_branch" in ev:
            git_branch = ev["git_branch"]

    # Deduplicate files preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    return {
        "files_touched": deduped,
        "commits": commits,
        "test_runs": test_runs,
        "insights": insights,
        "git_branch": git_branch,
    }


def _build_auto_summary(
    files_touched: list[str],
    commits: list[dict],
    test_runs: list[dict],
    event_count: int,
) -> str:
    """Build a metadata-based auto-summary for the Stop hook."""
    parts: list[str] = []
    if files_touched:
        basenames = [Path(f).name for f in files_touched[:5]]
        more = f" (+{len(files_touched) - 5} more)" if len(files_touched) > 5 else ""
        parts.append(f"Edited {len(files_touched)} files: {', '.join(basenames)}{more}")
    if commits:
        msgs = []
        for c in commits[:3]:
            if isinstance(c, dict):
                msgs.append(c.get("message", "")[:60])
            else:
                msgs.append(str(c)[:60])
        parts.append(f"Commits: {'; '.join(msgs)}")
    if test_runs:
        for tr in test_runs[:2]:
            if isinstance(tr, dict):
                p = tr.get("passed", 0)
                f = tr.get("failed", 0)
                parts.append(f"Tests: {p} passed, {f} failed")
    if not parts:
        parts.append(f"{event_count} tool events recorded")
    return ". ".join(parts) + "."


# Buffer I/O lives in thinkweave.core.buffer so MCP tools can call it
# without crossing the surfaces/ → surfaces/ boundary. Re-exported here so
# legacy imports (`from thinkweave.surfaces.hooks.handler import ...`)
# keep working.
from thinkweave.core.buffer import (  # noqa: E402, F401
    archive_buffer,
    cleanup_buffer,
    session_state_dir,
)


def _is_significant_command(command: str) -> bool:
    """Only capture meaningful bash commands, not noise."""
    significant = ["git commit", "git push", "pytest", "python", "uv run", "make", "npm", "deploy"]
    cmd_lower = command.lower().strip()
    return any(cmd_lower.startswith(s) for s in significant)


def _diff_context(tool_name: str, tool_input: dict) -> str:
    """Extract brief diff context from tool_input for enriched event lines."""
    if tool_name == "Edit":
        old = tool_input.get("old_string", "")[:80].replace("\n", " ").strip()
        new = tool_input.get("new_string", "")[:80].replace("\n", " ").strip()
        if old and new:
            return f" — `{old}` → `{new}`"
    elif tool_name == "Write":
        content = tool_input.get("content", "")
        first = _first_meaningful_line(content)
        if first:
            return f" — {first[:80]}"
    return ""


def _first_meaningful_line(text: str) -> str:
    """Return first non-blank, non-comment line from text."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            return stripped
    return ""


def _is_git_commit(command: str) -> bool:
    """Check if a bash command is a git commit."""
    cmd = command.strip().lower()
    return cmd.startswith("git commit") and "--amend" not in cmd


def _parse_commit_from_output(command: str, output: str) -> dict | None:
    """Extract commit info from git commit output.

    Git commit output looks like:
      [branch abc1234] Commit message
       N files changed, M insertions(+), K deletions(-)
    """
    if not output:
        return None

    info: dict = {}

    # Extract hash from [branch hash] pattern
    m = re.search(r"\[[\w/.-]+\s+([0-9a-f]{7,})\]", output)
    if m:
        info["hash"] = m.group(1)

    # Extract message from -m flag or from output
    m_flag = re.search(r'-m\s+["\'](.+?)["\']', command)
    if m_flag:
        info["message"] = m_flag.group(1)[:120]
    else:
        # Message is after the hash bracket
        m_msg = re.search(r"\[[^\]]+\]\s+(.+)", output)
        if m_msg:
            info["message"] = m_msg.group(1).strip()[:120]

    # Extract files from "N file(s) changed" line
    m_files = re.search(r"(\d+)\s+files?\s+changed", output)
    if m_files:
        info["files_changed"] = int(m_files.group(1))

    return info if info else None


def _get_commit_files(commit_hash: str) -> list[str]:
    """Get the list of files changed in a specific commit via git diff-tree."""
    try:
        result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def _is_test_command(command: str) -> bool:
    """Check if a bash command runs tests.

    Handles chained commands like ``cd foo && uv run pytest -x``
    by splitting on shell operators and checking each segment.
    """
    _TEST_PREFIXES = ("pytest", "python -m pytest", "uv run pytest", "uv run python -m pytest")
    # Split on shell chain operators (&&, ||, ;) and check each segment
    segments = re.split(r"\s*(?:&&|\|\||;)\s*", command.strip().lower())
    return any(seg.startswith(p) for seg in segments for p in _TEST_PREFIXES)


def _parse_test_result(command: str, output: str) -> dict | None:
    """Extract pass/fail counts from pytest output.

    Pytest summary line looks like: "12 passed, 1 failed" or "12 passed"
    """
    if not output:
        return None

    info: dict = {"command": command[:80]}

    # Match pytest summary: "N passed", "N failed", "N error"
    m_passed = re.search(r"(\d+)\s+passed", output)
    m_failed = re.search(r"(\d+)\s+failed", output)
    m_error = re.search(r"(\d+)\s+error", output)

    if m_passed:
        info["passed"] = int(m_passed.group(1))
    if m_failed:
        info["failed"] = int(m_failed.group(1))
    if m_error:
        info["errors"] = int(m_error.group(1))

    # Only return if we found at least a pass/fail count
    return info if ("passed" in info or "failed" in info) else None


def _parse_push_branch(command: str) -> str | None:
    """Extract branch name from a git push command."""
    # git push origin branch-name
    parts = command.strip().split()
    if len(parts) >= 3 and parts[0] == "git" and parts[1] == "push":
        # Skip flags
        for p in parts[2:]:
            if not p.startswith("-"):
                # Could be remote or branch — take the last non-flag arg
                pass
        # Simple heuristic: last non-flag argument
        non_flags = [p for p in parts[2:] if not p.startswith("-")]
        if len(non_flags) >= 2:
            return non_flags[1]  # git push <remote> <branch>
        elif len(non_flags) == 1:
            return non_flags[0]  # git push <remote-or-branch>
    return None


_INSIGHT_RE = re.compile(
    r"★ Insight[─ ]+\n(.*?)\n─+",
    re.DOTALL,
)


def _extract_insight_blocks(text: str) -> list[str]:
    """Extract ★ Insight blocks from Claude output."""
    return _INSIGHT_RE.findall(text)


def _handle_stop(hook_input: dict) -> None:
    """Stop hook: reconstruct session from JSONL buffer and finalize.

    Reads all buffered events, extracts metadata (files, commits, tests,
    insights), writes the session note once, archives the buffer, and
    indexes once. This is the only place that materializes buffer → note.
    """
    session_id = hook_input.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))
    if not session_id:
        _output()
        return

    try:
        from thinkweave.core.config import load_config
        from thinkweave.core.indexer import Indexer
        from thinkweave.core.vault import (
            VaultManager,
            render_frontmatter,
        )

        cfg = load_config()
        vm = VaultManager(config=cfg)

        session_path = _find_session_note(vm, session_id)
        if not session_path:
            # Retrieval-only fallback: there's a buffer (e.g. just retrieval
            # events from an MCP-only agent turn) but no session note yet.
            # Materialise one so ``archive_buffer`` has somewhere to land
            # the retrieval log — without this the buffer would be orphaned
            # and ``context_served`` would never receive an ``onthefly`` row.
            buf_file = cfg.weave_dir / "buffer" / f"{session_id}.jsonl"
            if buf_file.exists() and buf_file.stat().st_size > 0:
                _ensure_session(cfg, session_id, hook_input)
                session_path = _find_session_note(vm, session_id)
            if not session_path:
                _output()
                return

        note = vm.read_note(session_path)

        # Already processed → nothing to do
        if note.frontmatter.get("processed"):
            _output()
            return

        # Reconstruct session from JSONL buffer
        source_session = note.frontmatter.get("source_session", session_id)
        events = _read_buffer(cfg.weave_dir, source_session)

        if not events:
            _output()
            return

        from thinkweave.core.events import extract_deterministic

        result = extract_deterministic(events)

        # Build final frontmatter
        fm = note.frontmatter
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fm["processed"] = True
        fm["processed_at"] = today
        fm["auto_extracted"] = True
        if result.files_touched:
            fm["files_touched"] = result.files_touched
        if result.commits:
            fm["commits"] = result.commits
        if result.test_runs:
            fm["test_runs"] = result.test_runs
        if result.git_branch:
            fm["git_branch"] = result.git_branch
        if result.concepts:
            fm["concepts"] = result.concepts
        if result.decision_skeletons:
            fm["candidate_decisions"] = len(result.decision_skeletons)
        if result.failure_signals:
            fm["has_failures"] = True

        # Build body — summary + candidate insights + decision skeletons
        body_parts = [f"## Summary\n{result.summary}"]
        if result.insights:
            insight_lines = "\n".join(f"\n{ins}" for ins in result.insights)
            body_parts.append(f"## Candidate Insights{insight_lines}")
        if result.decision_skeletons:
            dec_lines = []
            for sk in result.decision_skeletons:
                files = ", ".join(sk.file_paths[:5])
                concepts = f" [{', '.join(sk.concepts)}]" if sk.concepts else ""
                dec_lines.append(f"- **{sk.title}** ({files}){concepts}")
            body_parts.append("## Candidate Decisions\n" + "\n".join(dec_lines))
        if result.failure_signals:
            fail_lines = [f"- {fs.title}" for fs in result.failure_signals]
            body_parts.append("## Failure Signals\n" + "\n".join(fail_lines))

        original_text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            render_frontmatter(fm) + "\n\n"
            + "\n\n".join(body_parts) + "\n",
            encoding="utf-8",
        )

        # Archive buffer → events.jsonl in session folder
        try:
            archive_buffer(cfg.weave_dir, source_session, session_path.parent)
        except Exception:
            # Keep Stop retriable: a processed note with a live buffer would
            # make the next delivery return early and strand the evidence.
            session_path.write_text(original_text, encoding="utf-8")
            raise

        # Index once
        diagnostic = ""
        try:
            idx = Indexer(config=cfg)
            try:
                idx.index_file(session_path)
            finally:
                idx.close()
        except Exception as e:
            _log_error("stop/index", e)
            diagnostic = _report_failure("stop/index", hook_input, e)

        # Stop-hook opportunistic embed deleted 2026-06-06 (plan A1,
        # go-back-to-the-scalable-firefly.md). Embeddings are now driven
        # exclusively by the cron path (`weave index --embed --only-new`);
        # query-time similarity retrieval reads the same cache.
        _output(system_message=diagnostic)
    except Exception as e:
        _log_error("stop", e)
        _output(system_message=_report_failure("stop", hook_input, e))


def _handle_session_start(hook_input: dict) -> None:
    """SessionStart: inject structured project context before the first user turn.

    Emits a ``hookSpecificOutput.additionalContext`` payload (~7–10k tokens)
    built by ``thinkweave.retrieval.context.build_project_context``. Never blocks;
    exceptions produce a valid hook response with an actionable diagnostic.

    Also records a single ``type: startup`` event in the session buffer with
    the set of note IDs the payload contains and the token estimate. This
    feeds the RLVR substrate's ``startup`` source — distinct from
    ``onthefly`` retrievals, and per the design weighted *lower* than
    on-the-fly hits when computing context value (a decision citing a note
    that was only in the startup payload is a weaker "context helped"
    signal than one that fetched the note mid-session).
    """
    try:
        from thinkweave.core.config import load_config
        from thinkweave.core.harness import SESSION_START_BUDGET_TOKENS
        from thinkweave.retrieval.context import build_project_context

        from thinkweave.operations.retrieval_log import parse_returned_ids

        cfg = load_config()
        project = _detect_project(hook_input)
        payload = build_project_context(
            cfg, project, budget_tokens=SESSION_START_BUDGET_TOKENS
        )

        # Served note ids — computed once, reused for the RLVR startup event
        # AND the memory-seam guard. (Parsed from the payload *before* the
        # guard is prepended, so the guard's own [[twin]] wikilinks — which
        # reference already-served notes — don't double-count.)
        served_ids = parse_returned_ids(payload)

        # Memory-seam serving lens — NOT a whole-seam dump. Cross-matches the
        # served notes against the flagged-twin index and injects a small
        # guard ONLY when a note in this session's context is the twin of a
        # durable CC memory flagged stale/diverged. Empty string = inject
        # nothing (the common case). Best-effort; never blocks the payload.
        capture_error: Exception | None = None
        try:
            from thinkweave.synthesis.memory_seam import session_guard_section

            guard = session_guard_section(cfg, served_ids)
        except Exception as e:
            _log_error("session_start_seam", e)
            guard = ""

        # Record the startup event regardless of whether we emit the payload —
        # an empty payload (cold vault) is itself a fact the RLVR row should
        # carry (n_retrievals_onthefly stays 0, startup_token_est = 0).
        try:
            session_id = hook_input.get(
                "session_id", os.environ.get("CLAUDE_SESSION_ID", "")
            )
            if session_id:
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "startup",
                    "returned_ids": served_ids,
                    # Rough token estimate — matches the SessionStart budget
                    # math (CHARS_PER_TOKEN ≈ 4 in retrieval/context.py).
                    "token_est": len(payload) // 4,
                }
                # Which harness served it. The indexer projects this to its
                # own `context_served.source`; why that split exists is on the
                # CHECK in core/indexer.py's SCHEMA_SQL.
                surface = _hook_harness()
                if surface:
                    event["surface"] = surface
                # Deliberately un-deduped (#161 review). SessionStart carries
                # no wire id, and every discriminator available here is wrong:
                # keyed on session_id alone the *second* SessionStart for a
                # session — a resume, a compact, a /clear — injects nothing,
                # silently costing the session its context for good (receipts
                # are never re-opened). Adding `source` only narrows that to
                # repeated resumes and compacts, which are routine. Duplicate
                # startup rows are the cheaper failure, and the registration
                # single-owner sweep in surfaces/hooks/install.py is what
                # actually stops them being produced.
                _buffer_event(cfg.weave_dir, session_id, event)
        except Exception as e:
            _log_error("session_start_capture", e)
            capture_error = e

        if not payload.strip() and not guard:
            _output(
                system_message=(
                    _report_failure(
                        "session_start_capture", hook_input, capture_error
                    )
                    if capture_error
                    else ""
                )
            )
            return

        # Guard rides at the TOP — it's a correctness interrupt on notes the
        # model is about to rely on, so it must be seen before the context.
        full = f"{guard}\n{payload}" if guard else payload
        _output(
            system_message=(
                _report_failure(
                    "session_start_capture", hook_input, capture_error
                )
                if capture_error
                else ""
            ),
            additional_context=full,
            hook_event_name="SessionStart",
        )
    except Exception as e:
        _log_error("session_start", e)
        _output(system_message=_report_failure("session_start", hook_input, e))


def _read_stdin() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (json.JSONDecodeError, EOFError):
        return {}


def _output(
    system_message: str = "",
    additional_context: str = "",
    hook_event_name: str = "",
) -> None:
    """Write hook response to stdout.

    Args:
        system_message: Legacy ``systemMessage`` channel used by PreToolUse.
        additional_context: Payload for ``hookSpecificOutput.additionalContext``
            (SessionStart). Injected as a system message before the first turn.
        hook_event_name: The Claude Code hook event name (e.g. ``SessionStart``).
            Required when ``additional_context`` is set.
    """
    result: dict = {}
    if system_message:
        result["systemMessage"] = system_message
    if additional_context and hook_event_name:
        result["hookSpecificOutput"] = {
            "hookEventName": hook_event_name,
            "additionalContext": additional_context,
        }
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
