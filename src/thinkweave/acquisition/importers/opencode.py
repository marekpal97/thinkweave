"""Seed the vault from prior OpenCode (sst/opencode) sessions.

The OpenCode analogue of :mod:`thinkweave.acquisition.importers.pi`, with one
structural difference: OpenCode 1.19 keeps every session in ONE SQLite store
(``~/.local/share/opencode/opencode.db``), not a file per session. Format
verified against the live 1.19.0 store (2026-10-12):

- ``session`` — one row per session: ``directory`` (cwd), ``title``,
  ``model`` (JSON text), ``time_created``/``time_updated`` (ms epoch), and
  ``parent_id`` — non-null for a subagent's child session.
- ``message`` — ``data`` JSON carries ``role``; one assistant message per
  model step, so a single reply spans several rows.
- ``part`` — ``data`` JSON typed ``text``/``reasoning``/``tool``/
  ``step-start``/``step-finish``/``patch``. Only ``text`` is conversation;
  ``synthetic: true`` text is harness-injected (editor-context reminders).

Child sessions (``parent_id`` set) are **skipped** and tallied as
``skipped_subagent``: they are work the parent delegated, and the parent's
own transcript already carries the delegation and its result. Same rule as
the rollout importer's sidecar threads, minus the opt-in.

The store is opened read-only (``mode=ro``) — OpenCode owns it and may be
writing to it while we read.

Idempotency: ``vault/.weave/onboarding/opencode.json``, keyed by session id.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from thinkweave.acquisition.importers.common import ImportManifest
from thinkweave.core.config import Config, load_config, normalize_project
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
_DB_NAME = "opencode.db"

# The OpenCode shim injects the SessionStart payload as message text opening
# with this marker (the Pi shim and importer keep the same literal). It is
# harness plumbing, not something the user said.
_CONTEXT_MARKER = "[thinkweave session context]"


@dataclass
class OpenCodeSession:
    """Parsed view of one OpenCode session (a ``session`` row + its parts)."""

    session_id: str
    project: str
    cwd: str
    title: str
    model: str
    started_at: datetime | None
    ended_at: datetime | None
    turns: list[tuple[str, str]] = field(default_factory=list)
    db_path: Path | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def count(self, role: str) -> int:
        return sum(1 for r, _ in self.turns if r == role)


# ── Store access ───────────────────────────────────────────────────────


def resolve_db(root: Path | None) -> Path:
    """The store path from a profile root: the ``.db`` itself, or the data
    directory holding ``opencode.db``. None → :data:`DEFAULT_DB`."""
    if root is None:
        return DEFAULT_DB
    return root / _DB_NAME if root.is_dir() else root


def _connect(db_path: Path) -> sqlite3.Connection:
    """Read-only connection; never creates or writes the store."""
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _ms(value: object) -> datetime | None:
    """ms-epoch column → aware datetime; None for a missing/garbage value."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _loads(raw: object) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def discover_sessions(
    db_path: Path = DEFAULT_DB,
) -> Iterator[tuple[str, str | None, datetime | None]]:
    """Yield ``(session_id, parent_id, started_at)`` newest-first, so
    ``--limit`` keeps the most recent work."""
    if not db_path.exists():
        return
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, parent_id, time_created FROM session "
            "ORDER BY time_created DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()
    for sid, parent, created in rows:
        yield sid, parent, _ms(created)


# ── Parser ─────────────────────────────────────────────────────────────


def _part_text(part: dict) -> str:
    """A ``text`` part's conversation text; ``""`` for every other part
    type, synthetic (harness-injected) text, and the shim's context."""
    if part.get("type") != "text" or part.get("synthetic"):
        return ""
    text = part.get("text")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    return "" if text.startswith(_CONTEXT_MARKER) else text


def parse_session(
    db_path: Path = DEFAULT_DB, session_id: str = ""
) -> OpenCodeSession | None:
    """Parse one session out of the store into a linear conversation.

    ``session_id`` empty → the newest top-level session (the one-argument
    ``parser(path)`` call the harness profile's ``transcript_parser`` contract
    makes). ``db_path`` may also be the data directory. None when the session
    is missing or holds no conversation.

    Turns: each message's surviving text parts, in ``time_created`` order;
    consecutive same-role messages merge into one turn, because OpenCode
    writes one assistant message per model step and a single reply would
    otherwise shatter into a turn per tool call.
    """
    db_path = resolve_db(db_path)
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        if session_id:
            row = conn.execute(
                "SELECT id, directory, title, model, time_created, time_updated "
                "FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, directory, title, model, time_created, time_updated "
                "FROM session WHERE parent_id IS NULL "
                "ORDER BY time_created DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        sid, cwd, title, model_raw, created, updated = row
        parts = conn.execute(
            "SELECT m.data, p.data FROM part p "
            "JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? "
            "ORDER BY m.time_created, m.id, p.time_created, p.id",
            (sid,),
        ).fetchall()
    finally:
        conn.close()

    turns: list[tuple[str, str]] = []
    for message_raw, part_raw in parts:
        role = _loads(message_raw).get("role")
        if role not in ("user", "assistant"):
            continue
        text = _part_text(_loads(part_raw))
        if not text:
            continue
        if turns and turns[-1][0] == role:
            turns[-1] = (role, f"{turns[-1][1]}\n\n{text}")
        else:
            turns.append((role, text))

    if not turns:
        return None

    cwd = cwd if isinstance(cwd, str) else ""
    model = _loads(model_raw)
    return OpenCodeSession(
        session_id=sid,
        project=normalize_project(cwd),
        cwd=cwd,
        title=title if isinstance(title, str) else "",
        model="/".join(
            str(model[k]) for k in ("providerID", "id") if model.get(k)
        ),
        started_at=_ms(created),
        ended_at=_ms(updated),
        turns=turns,
        db_path=db_path,
    )


def store_rows(db_path: Path, session_id: str) -> tuple[dict, list[dict]]:
    """One session's raw part rows, for the task digest reader.

    Returns ``({"version", "directory"}, rows)``: one row per part in
    conversation order, ``{"timestamp", "role", "model", "user_text",
    "part"}`` — ``timestamp`` ISO (the digest slices on it), ``user_text``
    the human prompt text of a user part (``""`` otherwise, harness
    injections excluded). Missing store or session → ``({}, [])``.
    """
    db_path = resolve_db(db_path)
    if not db_path.exists():
        return {}, []
    conn = _connect(db_path)
    try:
        meta = conn.execute(
            "SELECT version, directory FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if meta is None:
            return {}, []
        parts = conn.execute(
            "SELECT m.data, p.data, p.time_created FROM part p "
            "JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? "
            "ORDER BY m.time_created, m.id, p.time_created, p.id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    rows = []
    for message_raw, part_raw, created in parts:
        message, part = _loads(message_raw), _loads(part_raw)
        stamp = _ms(created)
        role = str(message.get("role", ""))
        rows.append(
            {
                "timestamp": stamp.isoformat() if stamp else "",
                "role": role,
                "model": str(message.get("modelID", "")),
                "user_text": _part_text(part) if role == "user" else "",
                "part": part,
            }
        )
    return {"version": meta[0] or "", "directory": meta[1] or ""}, rows


# ── Materialisation ────────────────────────────────────────────────────


def _build_session_body(session: OpenCodeSession) -> str:
    """Same sections and turn headings the other importers produce, so the
    downstream archival and synthesis prompt see one shape."""
    lines = ["## Source", ""]
    lines.append(f"Imported from OpenCode session `{session.session_id}`.")
    if session.cwd:
        lines.append(f"Original cwd: `{session.cwd}`")
    lines += ["", "## Transcript", ""]
    numbered: dict[str, int] = {}
    for role, text in session.turns:
        numbered[role] = numbered.get(role, 0) + 1
        lines += [f"### {role.capitalize()} (turn {numbered[role]})", "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def materialize_session(vm: VaultManager, session: OpenCodeSession) -> str:
    """Write one session note; returns its vault note id."""
    title_ts = (session.started_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M")
    title = f"OpenCode session {title_ts} ({session.project})"

    extra_fm: dict = {
        "imported_from": "opencode",
        "opencode_session_id": session.session_id,
        "original_db": str(session.db_path) if session.db_path else "",
        "user_turn_count": session.count("user"),
        "assistant_turn_count": session.count("assistant"),
    }
    if session.title:
        extra_fm["opencode_title"] = session.title
    if session.model:
        extra_fm["model"] = session.model
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
        tags=["imported", "opencode"],
        extra_frontmatter=extra_fm,
    )
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm.get("id", "")


# ── Top-level entry ────────────────────────────────────────────────────


def import_opencode(
    cfg: Config | None = None,
    *,
    project_filter: str = "",
    dry_run: bool = False,
    sessions_root: Path | None = None,
    db_path: Path | None = None,
    since: str = "",
    limit: int = 0,
) -> dict:
    """Walk the OpenCode store and materialise sessions as vault notes.

    Same argument and stats contract as ``import_pi``. ``sessions_root`` is
    the profile's transcript root (the ``.db`` or its data directory);
    ``db_path`` (``--db``) overrides it.
    """
    cfg = cfg or load_config()
    db = resolve_db(db_path or sessions_root)

    stats: dict = {
        "discovered": 0,
        "skipped_no_content": 0,
        "skipped_filter": 0,
        "skipped_already_imported": 0,
        "skipped_since": 0,
        "skipped_subagent": 0,
        "materialized": 0,
        "per_project": {},
        "errors": [],
    }

    if not db.exists():
        stats["errors"].append(f"OpenCode database not found: {db}")
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
    manifest = ImportManifest.load(cfg.weave_dir / "onboarding", "opencode.json")

    try:
        listing = list(discover_sessions(db))
    except sqlite3.Error as e:
        stats["errors"].append(f"{db}: {type(e).__name__}: {e}")
        return stats

    for sid, parent, started in listing:
        stats["discovered"] += 1

        # All three gates run off the session row, so a skipped session's
        # parts are never read.
        if parent:
            stats["skipped_subagent"] += 1
            continue
        if manifest.is_imported(sid):
            stats["skipped_already_imported"] += 1
            continue
        if since_dt is not None and started is not None and started < since_dt:
            stats["skipped_since"] += 1
            continue

        try:
            session = parse_session(db, sid)
        except Exception as e:  # noqa: BLE001 — one bad session shouldn't kill the walk
            stats["errors"].append(f"{sid}: {type(e).__name__}: {e}")
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
                stats["errors"].append(f"{sid}: {type(e).__name__}: {e}")
                continue
            manifest.mark(sid, note_id)
            per_proj["materialized"] += 1
            stats["materialized"] += 1

        if limit and stats["materialized"] >= limit:
            break

    if not dry_run:
        manifest.save()

    return stats
