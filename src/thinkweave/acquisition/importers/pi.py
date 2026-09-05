"""Seed the vault from prior Pi (badlogic/pi-mono) sessions.

The Pi analogue of :mod:`thinkweave.acquisition.importers.codex`: walks
``~/.pi/agent/sessions/--<cwd-dashes>--/<timestamp>_<uuid>.jsonl`` and
materialises one ``session`` note per file, holding the verbatim transcript
and *no* ``processed`` flag. Synthesis is the shared downstream pass
(``weave import <harness> --enrich``).

What the rollout walk never had to deal with (blueprint n-a1d3beba §6,
format verified against live Pi 0.84.4 session files): **entries form a
tree, not a log.** Every entry carries ``id``/``parentId``, and a fork or
resume grows a new leaf chain inside the SAME file rather than a new file.
Reading the file top-to-bottom would interleave branches and re-attribute
replies across them. :func:`parse_session` therefore reconstructs one linear
conversation by walking parent links back from the newest leaf — the chain
the session actually ended on. Abandoned fork branches are dropped with it;
that is the honest v1 trade (they are exploration the user backed out of),
recorded here rather than silently implied.

Idempotency: ``vault/.weave/onboarding/pi.json``, keyed by the session
header's ``id`` (the filename's uuid tail is its fallback), so a re-run
never re-parses an imported file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Format-agnostic line streamer + timestamp parser, shared with the rollout
# importer rather than re-derived: the defensive shape (bounded memory, drop
# oversized tool-output lines undecoded) is exactly what a Pi ``toolResult``
# entry needs too.
from thinkweave.acquisition.importers.codex import _iter_lines, _parse_ts
from thinkweave.acquisition.importers.common import ImportManifest
from thinkweave.core.config import Config, load_config, normalize_project
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter

DEFAULT_PI_SESSIONS_ROOT = Path.home() / ".pi" / "agent" / "sessions"

# The Pi shim injects the SessionStart payload as a synthetic user message
# opening with this marker (shims/pi/thinkweave-pi.ts keeps the same literal).
# It is harness plumbing, not something the user said — same rule as the
# rollout importer's <environment_context> filter.
_CONTEXT_MARKER = "[thinkweave session context]"


@dataclass
class PiSession:
    """Parsed view of one Pi session JSONL (the surviving branch only)."""

    session_id: str
    project: str
    cwd: str
    started_at: datetime | None
    ended_at: datetime | None
    turns: list[tuple[str, str]] = field(default_factory=list)
    file_path: Path | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def count(self, role: str) -> int:
        return sum(1 for r, _ in self.turns if r == role)


# ── Walker + parser ────────────────────────────────────────────────────


def session_file_id(path: Path) -> str:
    """The filename's identity: the uuid tail of ``<timestamp>_<uuid>.jsonl``.

    A fallback for manifests only — the header line's ``id`` is canonical
    (n-a1d3beba §6 leaves the two unconfirmed as always-identical). Whole
    stem when the name doesn't follow the convention.
    """
    stem = path.stem
    _, sep, tail = stem.partition("_")
    return tail if sep else stem


def _filename_date(path: Path) -> datetime | None:
    """Session start date off ``YYYY-MM-DDT…_<uuid>.jsonl``; None = unknown,
    which ``--since`` treats as "keep" rather than silently dropping."""
    try:
        return datetime.strptime(path.stem[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def discover_sessions(sessions_root: Path = DEFAULT_PI_SESSIONS_ROOT) -> Iterator[Path]:
    """Yield session JSONLs newest-first.

    Filenames open with the full start timestamp, so reverse-lexicographic on
    the name is chronological — ``--limit`` keeps the most recent work
    without opening anything.
    """
    if not sessions_root.exists():
        return
    yield from sorted(
        sessions_root.glob("*/*.jsonl"), key=lambda p: p.name, reverse=True
    )


def _entry_text(message: dict) -> str:
    """Plain text out of a Pi ``message`` payload.

    Content is a list of typed blocks; ``text`` blocks carry the
    conversation, while ``thinking``/``toolCall`` blocks (assistant) and
    tool-result payloads are operational.
    """
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"].strip()
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    return "\n\n".join(parts)


def parse_session(path: Path) -> PiSession | None:
    """Parse one Pi session file into a linear session; None when the
    surviving branch holds no conversation.

    Tree walk: collect every entry keyed by ``id``, take the LAST ``message``
    entry in file order as the live leaf (entries are appended as they
    happen, so the newest message ends the branch the session was actually
    on), then follow ``parentId`` links back to the root and emit that chain
    in forward order. ``model_change`` / ``thinking_level_change`` /
    ``compaction`` entries participate in the chain (they carry parent links)
    but contribute no turns.
    """
    header_id = ""
    cwd = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    entries: dict[str, dict] = {}
    last_message_id = ""

    for line in _iter_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        if entry.get("type") == "session":
            header_id = str(entry.get("id") or "")
            if isinstance(entry.get("cwd"), str):
                cwd = entry["cwd"]
            started_at = _parse_ts(entry.get("timestamp"))
            continue

        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            continue
        entries[entry_id] = entry
        ts = _parse_ts(entry.get("timestamp"))
        if ts and (ended_at is None or ts > ended_at):
            ended_at = ts
        if entry.get("type") == "message":
            last_message_id = entry_id

    if not last_message_id:
        return None

    # Walk the surviving branch leaf → root. The visited set makes a cyclic
    # parentId (corrupt file) terminate instead of spinning.
    chain: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = last_message_id
    while cursor and cursor in entries and cursor not in seen:
        seen.add(cursor)
        entry = entries[cursor]
        chain.append(entry)
        parent = entry.get("parentId")
        cursor = parent if isinstance(parent, str) else None
    chain.reverse()

    turns: list[tuple[str, str]] = []
    for entry in chain:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue  # toolResult and custom roles are operational
        text = _entry_text(message)
        if not text or (role == "user" and text.startswith(_CONTEXT_MARKER)):
            continue
        turns.append((role, text))

    if not turns:
        return None

    return PiSession(
        session_id=header_id or session_file_id(path),
        project=normalize_project(cwd),
        cwd=cwd,
        started_at=started_at,
        ended_at=ended_at,
        turns=turns,
        file_path=path,
    )


# ── Materialisation ────────────────────────────────────────────────────


def _build_session_body(session: PiSession) -> str:
    """Same sections and turn headings the other importers produce, so the
    downstream archival and synthesis prompt see one shape."""
    lines = ["## Source", ""]
    lines.append(f"Imported from Pi session `{session.session_id}`.")
    if session.cwd:
        lines.append(f"Original cwd: `{session.cwd}`")
    lines += ["", "## Transcript", ""]
    numbered: dict[str, int] = {}
    for role, text in session.turns:
        numbered[role] = numbered.get(role, 0) + 1
        lines += [f"### {role.capitalize()} (turn {numbered[role]})", "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def materialize_session(vm: VaultManager, session: PiSession) -> str:
    """Write one session note; returns its vault note id."""
    title_ts = (session.started_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M")
    title = f"Pi session {title_ts} ({session.project})"

    extra_fm: dict = {
        "imported_from": "pi",
        "pi_session_id": session.session_id,
        "original_jsonl": str(session.file_path) if session.file_path else "",
        "user_turn_count": session.count("user"),
        "assistant_turn_count": session.count("assistant"),
    }
    if session.cwd:
        extra_fm["source_cwd"] = session.cwd
    if session.started_at:
        extra_fm["started_at"] = session.started_at.isoformat()
    if session.ended_at:
        extra_fm["ended_at"] = session.ended_at.isoformat()

    path = vm.create_note(
        note_type=NoteType.SESSION,
        title=title,
        body=_build_session_body(session),
        project=session.project,
        tags=["imported", "pi"],
        extra_frontmatter=extra_fm,
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm.get("id", "")


# ── Top-level entry ────────────────────────────────────────────────────


def import_pi(
    cfg: Config | None = None,
    *,
    project_filter: str = "",
    dry_run: bool = False,
    sessions_root: Path | None = None,
    since: str = "",
    limit: int = 0,
) -> dict:
    """Walk Pi session files and materialise them as vault session notes.

    Same argument and stats contract as ``import_codex`` — the CLI drives
    every importer through one profile-resolved call.
    """
    cfg = cfg or load_config()
    root = sessions_root or DEFAULT_PI_SESSIONS_ROOT

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
        stats["errors"].append(f"Pi sessions root not found: {root}")
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
    manifest = ImportManifest.load(cfg.weave_dir / "onboarding", "pi.json")

    for path in discover_sessions(root):
        stats["discovered"] += 1

        # Both gates run off the filename, so an already-imported or
        # out-of-window session is never opened.
        if manifest.is_imported(session_file_id(path)):
            stats["skipped_already_imported"] += 1
            continue
        if since_dt is not None:
            started = _filename_date(path)
            if started is not None and started < since_dt:
                stats["skipped_since"] += 1
                continue

        try:
            session = parse_session(path)
        except Exception as e:  # noqa: BLE001 — one bad file shouldn't kill the walk
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
            # Marked under the FILENAME id — the same key the pre-open gate
            # checks — with the header id alongside it in the note.
            manifest.mark(session_file_id(path), note_id)
            per_proj["materialized"] += 1
            stats["materialized"] += 1

        if limit and stats["materialized"] >= limit:
            break

    if not dry_run:
        manifest.save()

    return stats
