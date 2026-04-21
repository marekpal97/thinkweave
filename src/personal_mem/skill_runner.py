"""Run personal_mem skills via the Anthropic Messages API.

The same markdown files under ``commands/`` that Claude Code invokes via
its Skill tool can also be executed headless against the Anthropic API.
``mem skill run <name>`` reads the skill frontmatter, builds a Messages
request with tool use, and loops until the model stops issuing tool calls.

**Tool bridging**: instead of running the personal_mem MCP server as a
subprocess, this runner bridges a curated subset of ``mem_*`` tools
**in-process** by calling directly into ``VaultManager``, ``Search``,
``Indexer``, and the concepts module. Local tools (``Read``, ``Bash``,
``WebFetch``) are bridged via stdlib and ``httpx``.

The bridged subset covers the most common skills (``/research``,
``/substack``, ``/discover``). Tools declared in a skill's frontmatter but
not bridged here return an "unsupported tool" error to the model — the
skill author can still run the skill in Claude Code for the full surface.

**Safety rails**:

- ``Bash`` refuses a hard-coded deny-list (``rm -rf``, ``sudo``, destructive
  git operations, ``--no-verify``). Errors go back to the model; the user
  stays in control.
- ``--max-turns`` caps the tool-call loop (default 40).
- ``--dry-run`` prints the Messages request shape without hitting the API.

The ``anthropic`` package is an optional dependency. It ships with the
``hubs`` extra (already used by ``mem hubs run``); installing either that
or a dedicated ``skill-runner`` extra enables this module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from personal_mem.config import load_config
from personal_mem.vault import parse_frontmatter

# ---------------------------------------------------------------------------
# Bash deny-list — matched as substrings on the concatenated command string.
# ---------------------------------------------------------------------------

_BASH_DENYLIST = (
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $",
    "sudo ",
    "curl | sh",
    "curl | bash",
    "wget | sh",
    "wget | bash",
    "git push --force",
    "git push -f",
    "git reset --hard",
    "--no-verify",
    ":(){ :|:& };:",
    "dd if=",
    "mkfs",
)


def _bash_is_safe(cmd: str) -> tuple[bool, str]:
    """Return (allowed, reason). Conservative — refuses anything on the deny-list."""
    lowered = cmd.lower()
    for bad in _BASH_DENYLIST:
        if bad in lowered:
            return False, f"Blocked by skill-runner deny-list: {bad!r}"
    return True, ""


# ---------------------------------------------------------------------------
# Tool schemas — the subset of Anthropic-format tool definitions the runner
# knows how to bridge. Skills declare their tool surface in frontmatter; we
# filter these schemas to match what the skill asks for.
# ---------------------------------------------------------------------------


def _tool_schemas() -> dict[str, dict]:
    """Return name → Anthropic tool-schema dict for every bridged tool."""
    return {
        "Read": {
            "name": "Read",
            "description": "Read a file from the local filesystem. Returns text contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or vault-relative path to read.",
                    }
                },
                "required": ["file_path"],
            },
        },
        "Bash": {
            "name": "Bash",
            "description": (
                "Run a shell command and return stdout+stderr. Destructive "
                "operations (rm -rf, sudo, force-push, etc.) are refused."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "default": 60},
                },
                "required": ["command"],
            },
        },
        "WebFetch": {
            "name": "WebFetch",
            "description": "Fetch a URL via HTTP GET and return the response body.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
        "mem_search": {
            "name": "mem_search",
            "description": (
                "Search the vault via FTS. Returns a list of matching notes "
                "(title, id, type, project, tags, snippet)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "default": ""},
                    "type": {"type": "string", "description": "Note type filter"},
                    "project": {"type": "string", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
        "mem_read": {
            "name": "mem_read",
            "description": "Read a note by ID. Returns frontmatter + body.",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        "mem_create": {
            "name": "mem_create",
            "description": (
                "Create a new note in the vault. note_type is one of note, "
                "session, decision, source. Returns the new note's ID and path."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "note_type": {
                        "type": "string",
                        "enum": ["note", "session", "decision", "source"],
                    },
                    "title": {"type": "string"},
                    "body": {"type": "string", "default": ""},
                    "project": {"type": "string", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "concepts": {"type": "array", "items": {"type": "string"}},
                    "frontmatter": {"type": "object"},
                },
                "required": ["note_type", "title"],
            },
        },
        "mem_concepts": {
            "name": "mem_concepts",
            "description": (
                "List concepts with their note counts. Optional prefix filter "
                "and min_count floor."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "default": ""},
                    "min_count": {"type": "integer", "default": 1},
                },
            },
        },
        "mem_concept_source_counts": {
            "name": "mem_concept_source_counts",
            "description": (
                "For a list of concepts, return the number of SOURCE notes "
                "citing each — plus their URLs. Flags under-sourced concepts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "concepts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["concepts"],
            },
        },
    }


# ---------------------------------------------------------------------------
# Tool handlers — each takes (args_dict, ctx) and returns a JSON-serializable
# result that the runner stringifies into a tool_result block.
# ---------------------------------------------------------------------------


class _Context:
    """Long-lived handles shared across tool invocations in one skill run."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self._search = None
        self._vault = None
        self._indexer = None

    @property
    def search(self):
        if self._search is None:
            from personal_mem.search import Search

            self._search = Search(config=self.cfg)
        return self._search

    @property
    def vault(self):
        if self._vault is None:
            from personal_mem.vault import VaultManager

            self._vault = VaultManager(config=self.cfg)
        return self._vault

    @property
    def indexer(self):
        if self._indexer is None:
            from personal_mem.indexer import Indexer

            self._indexer = Indexer(config=self.cfg)
        return self._indexer

    def close(self) -> None:
        if self._search is not None:
            self._search.close()
        if self._indexer is not None:
            self._indexer.close()


def _handle_read(args: dict, ctx: _Context) -> Any:
    path = Path(args["file_path"]).expanduser()
    if not path.is_absolute():
        path = ctx.cfg.vault_root / path
    if not path.exists():
        return {"error": f"File not found: {path}"}
    return {"content": path.read_text(encoding="utf-8")}


def _handle_bash(args: dict, ctx: _Context) -> Any:
    cmd = args["command"]
    ok, reason = _bash_is_safe(cmd)
    if not ok:
        return {"error": reason}
    timeout = int(args.get("timeout_sec", 60))
    try:
        result = subprocess.run(
            cmd, shell=True, check=False, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout[-20_000:],
        "stderr": result.stderr[-4_000:],
    }


def _handle_webfetch(args: dict, ctx: _Context) -> Any:
    try:
        import httpx
    except ImportError:
        return {"error": "WebFetch requires httpx (pip install httpx)"}
    try:
        resp = httpx.get(args["url"], timeout=30.0, follow_redirects=True)
    except Exception as e:
        return {"error": f"Fetch failed: {e}"}
    return {
        "status": resp.status_code,
        "url": str(resp.url),
        "text": resp.text[:100_000],
    }


def _handle_mem_search(args: dict, ctx: _Context) -> Any:
    results = ctx.search.search(
        query=args.get("query", ""),
        note_type=args.get("type", ""),
        project=args.get("project", ""),
        tags=args.get("tags") or [],
        limit=int(args.get("limit", 10)),
    )
    return [
        {
            "id": r.id,
            "type": r.type,
            "title": r.title,
            "project": r.project,
            "tags": r.tags,
            "date": r.date,
            "snippet": getattr(r, "snippet", ""),
        }
        for r in results
    ]


def _handle_mem_read(args: dict, ctx: _Context) -> Any:
    note_id = args["id"]
    row = ctx.indexer.db.execute(
        "SELECT path FROM notes WHERE id = ?", (note_id,)
    ).fetchone()
    if row is None:
        return {"error": f"Note not found: {note_id}"}
    path = ctx.cfg.vault_root / row["path"]
    if not path.exists():
        return {"error": f"Note file missing on disk: {path}"}
    note = ctx.vault.read_note(path)
    return {
        "id": note.id,
        "type": note.type.value if hasattr(note.type, "value") else str(note.type),
        "title": note.title,
        "project": note.project,
        "tags": note.tags,
        "frontmatter": note.frontmatter,
        "body": note.body,
        "path": str(path),
    }


def _handle_mem_create(args: dict, ctx: _Context) -> Any:
    from personal_mem.schemas import NoteType

    note_type = NoteType(args["note_type"])
    fm = dict(args.get("frontmatter") or {})
    if "concepts" in args:
        fm["concepts"] = args["concepts"]
    path = ctx.vault.create_note(
        note_type=note_type,
        title=args["title"],
        body=args.get("body", ""),
        project=args.get("project", "") or ctx.cfg.default_project,
        tags=args.get("tags") or [],
        extra_frontmatter=fm,
    )
    # Index the new note so it's immediately searchable within the skill run
    ctx.indexer.index_file(path)
    note = ctx.vault.read_note(path)
    return {
        "id": note.id,
        "path": str(path),
        "type": note.type.value if hasattr(note.type, "value") else str(note.type),
        "title": note.title,
    }


def _handle_mem_concepts(args: dict, ctx: _Context) -> Any:
    db = ctx.indexer.db
    prefix = args.get("prefix", "")
    min_count = int(args.get("min_count", 1))
    rows = db.execute(
        """
        SELECT concept, COUNT(DISTINCT note_id) as n
        FROM note_concepts
        GROUP BY concept
        HAVING n >= ?
        ORDER BY n DESC, concept ASC
        """,
        (min_count,),
    ).fetchall()
    out = []
    for row in rows:
        concept = row["concept"]
        if prefix and not concept.startswith(prefix):
            continue
        out.append({"concept": concept, "count": row["n"]})
    return out


def _handle_mem_concept_source_counts(args: dict, ctx: _Context) -> Any:
    db = ctx.indexer.db
    concepts = args.get("concepts") or []
    out = []
    for concept in concepts:
        rows = db.execute(
            """
            SELECT n.id, n.title, n.frontmatter
            FROM notes n
            JOIN note_concepts nc ON nc.note_id = n.id
            WHERE nc.concept = ? AND n.type = 'source'
            """,
            (concept,),
        ).fetchall()
        sources = []
        for row in rows:
            try:
                fm = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
            except Exception:
                fm = {}
            sources.append(
                {"id": row["id"], "title": row["title"], "url": fm.get("url", "")}
            )
        out.append(
            {
                "concept": concept,
                "source_count": len(sources),
                "under_sourced": len(sources) < 2,
                "sources": sources[:10],
            }
        )
    return out


_HANDLERS: dict[str, Callable[[dict, _Context], Any]] = {
    "Read": _handle_read,
    "Bash": _handle_bash,
    "WebFetch": _handle_webfetch,
    "mem_search": _handle_mem_search,
    "mem_read": _handle_mem_read,
    "mem_create": _handle_mem_create,
    "mem_concepts": _handle_mem_concepts,
    "mem_concept_source_counts": _handle_mem_concept_source_counts,
}


# ---------------------------------------------------------------------------
# Skill loading + runner entry point
# ---------------------------------------------------------------------------

_SKILL_SYSTEM_PROMPT = (
    "You are executing a personal_mem skill. The user message contains the "
    "skill document — follow its procedure exactly, calling tools as "
    "specified. Report in the format the skill specifies. Do not ask the "
    "user clarifying questions; make reasonable choices and proceed."
)


def _commands_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "commands"


def _load_skill(name: str) -> dict | None:
    path = _commands_dir() / f"{name}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return {"name": name, "path": path, "fm": fm, "body": body}


def _select_tools(declared: list[str]) -> list[dict]:
    """Return Anthropic tool schemas for every declared tool that the runner
    knows how to bridge. Unknown tools are silently dropped; the runner
    prints a warning so the user knows why they weren't available."""
    schemas = _tool_schemas()
    selected = []
    missing = []
    for name in declared:
        if name in schemas:
            selected.append(schemas[name])
        else:
            missing.append(name)
    if missing:
        print(
            f"Warning: {len(missing)} tool(s) declared by skill are not "
            f"bridged by the runner: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "  Run the skill in Claude Code for the full tool surface.",
            file=sys.stderr,
        )
    return selected


def run_skill(
    name: str,
    skill_args: list[str] | None = None,
    model: str = "claude-opus-4-5",
    max_turns: int = 40,
    dry_run: bool = False,
) -> None:
    """Entry point for ``mem skill run``.

    Args:
        name: skill file stem under ``commands/`` (e.g. ``research``).
        skill_args: extra strings appended to the user message as arguments.
        model: Anthropic model ID.
        max_turns: hard cap on the tool-call loop.
        dry_run: print the Messages request shape and exit without calling the API.
    """
    skill = _load_skill(name)
    if skill is None:
        print(f"No skill found at commands/{name}.md")
        sys.exit(1)

    declared_tools = skill["fm"].get("tools") or []
    if isinstance(declared_tools, str):
        declared_tools = [declared_tools]
    tool_schemas = _select_tools(declared_tools)

    arg_str = " ".join(skill_args or [])
    user_message = skill["body"]
    if arg_str:
        user_message = f"{user_message}\n\n## Arguments\n{arg_str}"

    if dry_run:
        print(f"# DRY RUN: mem skill run {name}")
        print(f"model:       {model}")
        print(f"max_turns:   {max_turns}")
        print(f"system:      {len(_SKILL_SYSTEM_PROMPT)} chars")
        print(f"user:        {len(user_message)} chars")
        print(f"tools:       {len(tool_schemas)} bridged")
        for t in tool_schemas:
            print(f"  - {t['name']}")
        print()
        print("--- user message (first 400 chars) ---")
        print(user_message[:400])
        if len(user_message) > 400:
            print("...")
        return

    try:
        from anthropic import Anthropic
    except ImportError:
        print(
            "mem skill run requires the Anthropic SDK.\n"
            "Install with: pip install anthropic  "
            "(or `uv sync --extra hubs` if using the repo's optional deps)"
        )
        sys.exit(2)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Export it before running "
            "`mem skill run`."
        )
        sys.exit(2)

    client = Anthropic()
    ctx = _Context()

    messages: list[dict] = [{"role": "user", "content": user_message}]

    try:
        for turn in range(max_turns):
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=_SKILL_SYSTEM_PROMPT,
                tools=tool_schemas or None,
                messages=messages,
            )

            tool_uses = [
                block for block in response.content if getattr(block, "type", "") == "tool_use"
            ]

            if not tool_uses:
                # Final assistant message — print and exit.
                text_blocks = [
                    block.text
                    for block in response.content
                    if getattr(block, "type", "") == "text"
                ]
                print("\n".join(text_blocks))
                return

            # Append the assistant turn so history stays consistent.
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in response.content],
                }
            )

            # Dispatch every tool_use in the turn and build tool_result blocks.
            tool_results = []
            for use in tool_uses:
                handler = _HANDLERS.get(use.name)
                if handler is None:
                    result_payload = {
                        "error": (
                            f"Tool '{use.name}' is declared by the skill but "
                            "not bridged by mem skill run."
                        )
                    }
                else:
                    try:
                        result_payload = handler(use.input, ctx)
                    except Exception as e:
                        result_payload = {"error": f"{type(e).__name__}: {e}"}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": json.dumps(result_payload, default=str)[:50_000],
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        print(f"\nReached max_turns ({max_turns}) without completion.")
        sys.exit(1)
    finally:
        ctx.close()


def _block_to_dict(block) -> dict:
    """Convert an Anthropic content block back into dict form for the history."""
    btype = getattr(block, "type", "")
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback: best-effort dump
    return {"type": btype, "raw": str(block)}
