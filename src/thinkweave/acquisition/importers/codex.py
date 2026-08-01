"""Seed the vault from prior Codex conversations.

The Codex analogue of :mod:`thinkweave.onboarding.claude_code_seed`: walks
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` and materialises one
``session`` note per rollout, holding the verbatim transcript and *no*
``processed`` flag. Synthesis is the same downstream pass both harnesses share
(``weave import <harness> --enrich``, inline or batch) — it consumes the
session note, not the transcript format.

It lives here rather than beside its Claude Code twin in ``onboarding/``
because that is where ARCHITECTURE.md puts one-shot ``weave import`` bulk
importers, and because ``onboarding`` is an unranked package whose every
cross-package import is a grandfathered package-contract violation (see
``tests/test_package_contract.py``) — a new module there could only add to a
shrink-only baseline. From ``acquisition`` the same imports are legal downward
edges. The cwd→project rules the two importers must share therefore live in
``core.config.normalize_project``.

Two things differ from the Claude Code walk, both forced by the rollout format:

**Rollouts are huge.** Codex stores raw tool output verbatim and compaction
re-records history, so a long session's JSONL reaches 700MB-2GB
(openai/codex#24948). Nothing here reads a whole file, or even a whole *line*:
:func:`_iter_lines` streams fixed-size chunks and discards any line over
:data:`MAX_LINE_BYTES` without decoding it, so peak memory is a constant
(``MAX_LINE_BYTES + _CHUNK_BYTES``) independent of rollout size.

**Compaction replays repeat exchanges.** After a compaction the earlier turns
are re-emitted as fresh ``response_item`` messages inside the same rollout, so
a replayed exchange would otherwise read downstream as "discussed N times".
:class:`_ReplayFilter` suppresses those — anchored to contiguous replayed
*runs*, never to bare text repetition, so a user typing "continue" twice keeps
both turns and no reply is re-attributed to the wrong request.

Project resolution reuses :func:`thinkweave.core.config.normalize_project` —
the same worktree-stripping and homedir → ``_unscoped`` rules the Claude Code
seed applies, so a repo worked on from both harnesses lands in one vault
project.

Idempotency: ``vault/.weave/onboarding/codex.json``, keyed by rollout id.
Already-imported rollouts are skipped *before* parsing, so a re-run never
re-reads a multi-gigabyte file.

Not covered: Codex's newer ``sqlite_home`` state DB. It holds session metadata
we already get from ``session_meta``, and the dev machines this was written
against have no such DB to probe, so it is skipped rather than guessed at.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from thinkweave.acquisition.importers.common import ImportManifest
from thinkweave.core.config import Config, load_config, normalize_project
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter

DEFAULT_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

# Lines above this are tool-result payloads, not conversation: dropped without
# being decoded or parsed. ponytail: a single chat message larger than this is
# lost with it. Upgrade path is a streaming JSON reader that can tell a
# `function_call_output` from a `message` before consuming the value; today's
# cost of that is far above the value of a >1MiB single message.
MAX_LINE_BYTES = 1 << 20
_CHUNK_BYTES = 1 << 16

# Codex injects these as pseudo-user messages: the per-turn sandbox/cwd block
# and the AGENTS.md dump. Harness plumbing, not something the user said.
# (Only <environment_context> appears in the Dec-2025 rollouts on hand; the
# instructions wrapper arrived with AGENTS.md support.)
_SYNTHETIC_USER_PREFIXES = ("<environment_context>", "<user_instructions>")


@dataclass
class CodexSession:
    """Parsed view of one Codex rollout JSONL.

    ``turns`` is one ordered ``(role, text)`` sequence rather than a pair of
    per-role lists. That is load-bearing, not tidiness: the body renders
    straight from this order, so dropping a turn can never shift which reply
    is attributed to which request.
    """

    rollout_id: str
    project: str
    cwd: str
    git_branch: str
    started_at: datetime | None
    ended_at: datetime | None
    turns: list[tuple[str, str]] = field(default_factory=list)
    file_path: Path | None = None

    def count(self, role: str) -> int:
        return sum(1 for r, _ in self.turns if r == role)


# ── Walker + parser ────────────────────────────────────────────────────


def rollout_id(path: Path) -> str:
    """The rollout's identity: the UUID tail of ``rollout-<ISO>-<uuid>.jsonl``.

    Taken from the filename rather than the ``session_meta`` payload so an
    already-imported rollout can be skipped without opening it. Falls back to
    the whole stem for a file that doesn't follow the naming convention.
    """
    stem = path.stem
    tail = stem.rsplit("-", 5)[-5:]
    if len(tail) == 5 and len("-".join(tail)) == 36:
        return "-".join(tail)
    return stem


def _filename_date(path: Path) -> datetime | None:
    """The rollout's start date, read off ``rollout-YYYY-MM-DDT...``.

    ``None`` for a filename that doesn't follow the convention; callers treat
    that as "don't know", which means ``--since`` keeps the rollout rather than
    silently dropping it.
    """
    try:
        return datetime.strptime(path.stem[8:18], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def discover_rollouts(sessions_root: Path = DEFAULT_CODEX_SESSIONS_ROOT) -> Iterator[Path]:
    """Yield rollout JSONLs newest-first.

    Filenames carry the full start timestamp, so reverse-lexicographic on the
    name is chronological — which makes ``--limit`` keep the most recent work
    without parsing anything first (the Claude Code importer needs a full
    pre-pass for that; here the ordering is free).
    """
    if not sessions_root.exists():
        return
    yield from sorted(
        sessions_root.rglob("rollout-*.jsonl"), key=lambda p: p.name, reverse=True
    )


def _iter_lines(path: Path) -> Iterator[str]:
    """Stream a JSONL as decoded lines, dropping any line over the cap.

    The whole point: a rollout can be gigabytes and a *single* tool-output line
    can be hundreds of megabytes, so neither ``read()`` nor ``for line in fh``
    is safe here.

    The cap is applied to whatever *partial* line remains after complete lines
    have been drained — checking before the drain would discard a buffer full
    of perfectly good short lines. Peak resident bytes are therefore
    ``MAX_LINE_BYTES + _CHUNK_BYTES``, not ``MAX_LINE_BYTES``.
    """
    buf = bytearray()
    dropping = False
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if dropping:
                    dropping = False  # this newline ends the oversized line
                    continue
                yield line.decode("utf-8", errors="replace")
            if len(buf) > MAX_LINE_BYTES:
                buf.clear()
                dropping = True
    if buf and not dropping:
        yield bytes(buf).decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    """Pull plain text out of a Codex ``message`` payload.

    Content is a list of typed blocks; ``input_text`` (user) and ``output_text``
    (assistant) carry the conversation, everything else is operational.
    """
    content = payload.get("content", [])
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


class _ReplayFilter:
    """Records turns in order, suppressing compaction *replays* only.

    A compaction replay re-emits a contiguous block of earlier turns verbatim,
    in their original order. A user typing "continue" twice does not. Keying
    suppression on "have I seen this text before" cannot tell those apart, and
    getting it wrong is not a cosmetic loss: the recorded order is what the
    session body renders and what the synthesis pass reads, so a wrongly
    dropped turn silently re-attributes every later reply to the wrong request.

    So suppression is anchored to a *run*: a repeat is held back one turn
    (``_pending``) and only dropped once the next turn also continues the same
    earlier sequence. If it doesn't, the held turn is a genuine repeat and gets
    recorded. Deliberately conservative in one direction — a replayed block of
    exactly one turn reads as a genuine repeat and is kept, because recording a
    turn twice is recoverable and dropping a real one is not.
    """

    def __init__(self) -> None:
        self._turns: list[tuple[str, str]] = []
        self._first_seen: dict[tuple[str, str], int] = {}
        self._pending: tuple[str, str] | None = None
        self._cursor: int | None = None  # next index a replay run would match

    def _record(self, turn: tuple[str, str]) -> None:
        self._first_seen.setdefault(turn, len(self._turns))
        self._turns.append(turn)

    def feed(self, role: str, text: str) -> None:
        turn = (role, text)

        # Does this continue the run the pending repeat opened?
        if (
            self._cursor is not None
            and self._cursor < len(self._turns)
            and self._turns[self._cursor] == turn
        ):
            self._pending = None  # confirmed replay: drop its first turn too
            self._cursor += 1
            return

        # The run (if any) ended here, so anything held back was genuine.
        if self._pending is not None:
            self._record(self._pending)
            self._pending = None
        self._cursor = None

        if turn in self._first_seen:
            # Might open a replay; decide when the next turn arrives.
            self._pending = turn
            self._cursor = self._first_seen[turn] + 1
        else:
            self._record(turn)

    def result(self) -> list[tuple[str, str]]:
        """The ordered turns. Flushes a trailing unresolved repeat — nothing
        followed it to confirm a replay, so it counts as genuine."""
        if self._pending is not None:
            self._record(self._pending)
            self._pending = None
        return self._turns


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def parse_rollout(path: Path) -> CodexSession | None:
    """Parse a rollout into a session. None when it holds no conversation.

    Only ``response_item`` messages are read — Codex mirrors each of them as an
    ``event_msg`` (``user_message`` / ``agent_message``), and reading both
    streams would double every turn. Compaction replays are suppressed by
    :class:`_ReplayFilter`; see its docstring for why a global "seen this text"
    set is the wrong rule.
    """
    cwd = ""
    git_branch = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turns = _ReplayFilter()

    for line in _iter_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        payload = ev.get("payload")
        payload = payload if isinstance(payload, dict) else {}

        ts = _parse_ts(ev.get("timestamp"))
        if ts and (started_at is None or ts < started_at):
            started_at = ts
        if ts and (ended_at is None or ts > ended_at):
            ended_at = ts

        if ev.get("type") == "session_meta":
            if not cwd and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            git = payload.get("git")
            if not git_branch and isinstance(git, dict) and isinstance(git.get("branch"), str):
                git_branch = git["branch"]
            continue

        if ev.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(payload)
        if not text or text.startswith(_SYNTHETIC_USER_PREFIXES):
            continue
        turns.feed(role, text)

    recorded = turns.result()
    if not recorded:
        return None

    return CodexSession(
        rollout_id=rollout_id(path),
        project=normalize_project(cwd),
        cwd=cwd,
        git_branch=git_branch,
        started_at=started_at,
        ended_at=ended_at,
        turns=recorded,
        file_path=path,
    )


# ── Materialisation ────────────────────────────────────────────────────


def _build_session_body(session: CodexSession) -> str:
    """Render the session note's markdown body — same sections and turn
    headings the Claude Code import produces, so the downstream transcript
    archival and synthesis prompt see one shape."""
    lines = ["## Source", ""]
    lines.append(f"Imported from Codex rollout `{session.rollout_id}`.")
    if session.cwd:
        lines.append(f"Original cwd: `{session.cwd}`")
    if session.git_branch:
        lines.append(f"Git branch at session start: `{session.git_branch}`")
    lines += ["", "## Transcript", ""]
    # Rendered straight from the recorded order, so a suppressed turn can never
    # pair a reply with the wrong request.
    numbered: dict[str, int] = {}
    for role, text in session.turns:
        numbered[role] = numbered.get(role, 0) + 1
        lines += [f"### {role.capitalize()} (turn {numbered[role]})", "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def materialize_session(vm: VaultManager, session: CodexSession) -> str:
    """Write one session note; returns its vault note id."""
    title_ts = (session.started_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M")
    title = f"Codex session {title_ts} ({session.project})"

    extra_fm: dict = {
        "imported_from": "codex",
        "codex_rollout_id": session.rollout_id,
        "original_jsonl": str(session.file_path) if session.file_path else "",
        "user_turn_count": session.count("user"),
        "assistant_turn_count": session.count("assistant"),
    }
    if session.cwd:
        extra_fm["source_cwd"] = session.cwd
    if session.git_branch:
        extra_fm["git_branch"] = session.git_branch
    if session.started_at:
        extra_fm["started_at"] = session.started_at.isoformat()
    if session.ended_at:
        extra_fm["ended_at"] = session.ended_at.isoformat()

    path = vm.create_note(
        note_type=NoteType.SESSION,
        title=title,
        body=_build_session_body(session),
        project=session.project,
        tags=["imported", "codex"],
        extra_frontmatter=extra_fm,
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm.get("id", "")


# ── Top-level entry ────────────────────────────────────────────────────


def import_codex(
    cfg: Config | None = None,
    *,
    project_filter: str = "",
    dry_run: bool = False,
    sessions_root: Path | None = None,
    since: str = "",
    limit: int = 0,
) -> dict:
    """Walk Codex rollouts and materialise them as vault session notes.

    Args:
        cfg: Vault config; loaded from defaults if None.
        project_filter: If non-empty, only import rollouts whose *normalized*
            project matches.
        dry_run: If True, count without writing.
        sessions_root: Override for ``~/.codex/sessions``.
        since: ISO date (``YYYY-MM-DD``); older rollouts are tallied as
            ``skipped_since``. Read off the filename, so a skipped rollout is
            never opened.
        limit: Cap on materialised sessions (0 = unbounded). Rollouts are
            always walked newest-first, so the cap keeps the most recent work.

    Returns the same stats shape as ``import_claude_code``.
    """
    cfg = cfg or load_config()
    root = sessions_root or DEFAULT_CODEX_SESSIONS_ROOT

    stats: dict = {
        "discovered": 0,
        "skipped_no_content": 0,
        "skipped_filter": 0,
        "skipped_already_imported": 0,
        "skipped_since": 0,
        "materialized": 0,
        "per_project": {},
        "errors": [],
    }

    if not root.exists():
        stats["errors"].append(f"Codex sessions root not found: {root}")
        return stats

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            stats["errors"].append(f"--since must be YYYY-MM-DD, got {since!r}")
            return stats

    vm = VaultManager(config=cfg) if not dry_run else None
    if vm:
        vm.ensure_dirs()
    manifest = ImportManifest.load(cfg.weave_dir / "onboarding", "codex.json")

    for path in discover_rollouts(root):
        stats["discovered"] += 1

        # Both gates below run off the filename, so an already-imported or
        # out-of-window rollout is never opened — the difference between a
        # cheap re-run and re-reading gigabytes.
        if manifest.is_imported(rollout_id(path)):
            stats["skipped_already_imported"] += 1
            continue
        if since_dt is not None:
            started = _filename_date(path)
            if started is not None and started < since_dt:
                stats["skipped_since"] += 1
                continue

        try:
            session = parse_rollout(path)
        except Exception as e:  # noqa: BLE001 — one bad rollout shouldn't kill the walk
            stats["errors"].append(f"{path}: {type(e).__name__}: {e}")
            continue
        if session is None:
            stats["skipped_no_content"] += 1
            continue

        if project_filter and session.project != project_filter:
            stats["skipped_filter"] += 1
            continue

        per_proj = stats["per_project"].setdefault(
            session.project, {"materialized": 0, "discovered": 0}
        )
        per_proj["discovered"] += 1

        if dry_run:
            per_proj["materialized"] += 1
            stats["materialized"] += 1
        else:
            try:
                note_id = materialize_session(vm, session)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"{path}: {type(e).__name__}: {e}")
                continue
            manifest.mark(session.rollout_id, note_id)
            per_proj["materialized"] += 1
            stats["materialized"] += 1

        if limit and stats["materialized"] >= limit:
            break

    if not dry_run:
        manifest.save()

    return stats
