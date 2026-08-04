"""Project canonical ThinkWeave commands into Codex Agent Skills.

The command and worker markdown files remain the semantic source of truth.
Codex projections are deliberately small: they point at that source and add
only the harness vocabulary needed to execute it with native tools.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from dataclasses import dataclass
from pathlib import Path

from thinkweave.core.vault import parse_frontmatter


@dataclass(frozen=True)
class CommandContract:
    name: str
    description: str
    source: Path
    source_relpath: Path
    workers: tuple[str, ...]


def codex_skill_name(name: str) -> str:
    return f"thinkweave-{name}"


def iter_command_contracts(
    commands_root: Path, agents_root: Path
) -> tuple[CommandContract, ...]:
    """Return every supported command and the workers its contract names."""
    worker_names = {path.stem for path in agents_root.glob("*.md")}
    contracts: list[CommandContract] = []
    for source in sorted(commands_root.rglob("*.md")):
        if source.name.startswith("_"):
            continue
        text = source.read_text(encoding="utf-8")
        metadata, _ = parse_frontmatter(text)
        name = str(metadata.get("name") or source.stem)
        description = " ".join(str(metadata.get("description") or "").split())
        if not description:
            raise ValueError(f"command has no description: {source}")
        named_workers = {name for name in worker_names if name in text}
        for match in re.finditer(r"([a-z0-9-]*)\{([^}]+)\}([a-z0-9-]*)", text):
            prefix, choices, suffix = match.groups()
            named_workers.update(
                candidate
                for choice in choices.split(",")
                if (candidate := f"{prefix}{choice.strip()}{suffix}") in worker_names
            )
        for pattern in re.findall(r"agents/([a-z0-9*-]+)\.md", text):
            named_workers.update(name for name in worker_names if fnmatch(name, pattern))
        workers = tuple(sorted(named_workers))
        contracts.append(
            CommandContract(
                name=name,
                description=description,
                source=source,
                source_relpath=source.relative_to(commands_root.parent),
                workers=workers,
            )
        )
    return tuple(contracts)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _display_name(name: str) -> str:
    return "ThinkWeave " + name.replace("-", " ").title()


def render_codex_skill(contract: CommandContract) -> str:
    command_ref = "../../" + contract.source_relpath.as_posix()
    lines = [
        "---",
        f"name: {codex_skill_name(contract.name)}",
        f"description: {_quoted(contract.description)}",
        "---",
        "",
        f"# Codex projection for `/{contract.name}`",
        "",
        "Read the [canonical ThinkWeave command contract]"
        f"({command_ref}) completely, then execute it. The linked file is the",
        "semantic source of truth; this file only adapts harness vocabulary.",
        "",
        "Use Codex-native equivalents for capabilities named in the canonical",
        "contract: filesystem read/search for `Read`/`Grep`, the shell runner for",
        "`Bash`, `apply_patch` for `Write`/`Edit`, the web tool for",
        "`WebFetch`/`WebSearch`, and the user-input tool for `AskUserQuestion`.",
        "When it names another `/skill`, read and follow the sibling",
        "`../thinkweave-<skill>/SKILL.md` projection; `$thinkweave-<skill>` is",
        "Codex's user-facing invocation spelling.",
    ]

    if contract.workers:
        lines += [
            "",
            "## Native subagent projection",
            "",
            "Translate every canonical `Task` dispatch to Codex's native",
            "`spawn_agent` tool. Read the relevant worker contract below in full",
            "before spawning, and include its complete instructions plus the",
            "task-specific prompt in `message`; a generic subagent does not inherit",
            "the contract by name. Normalize the worker name to a valid `task_name`",
            "by replacing hyphens with underscores. Use `followup_task` for the",
            "contract's retry path and `wait_agent` for fan-in/dependency waves.",
            "Do not use or emit Claude Code Task-call syntax.",
            "",
            "Shared worker contracts:",
            "",
        ]
        lines += [f"- [`{worker}`](../../agents/{worker}.md)" for worker in contract.workers]

    if contract.name in {"dream", "drain"}:
        lines += [
            "",
            "> **Codex support status:** Interactive worker fan-out is supported",
            "> through the native subagent projection above. Unattended/headless",
            "> orchestration remains **degraded** until issue #110 supplies the",
            "> dedicated executor; do not claim cron parity in the meantime.",
        ]

    return "\n".join(lines) + "\n"


def render_codex_metadata(contract: CommandContract) -> str:
    name = codex_skill_name(contract.name)
    short = contract.description[:80].rstrip()
    return (
        "interface:\n"
        f"  display_name: {_quoted(_display_name(contract.name))}\n"
        f"  short_description: {_quoted(short)}\n"
        f"  default_prompt: {_quoted(f'Use ${name} to run this ThinkWeave workflow.')}\n"
        "dependencies:\n"
        "  tools:\n"
        "    - type: \"mcp\"\n"
        "      value: \"thinkweave\"\n"
        "      description: \"ThinkWeave durable-memory tools\"\n"
    )


def write_codex_projections(repo_root: Path) -> None:
    commands_root = repo_root / "commands"
    agents_root = repo_root / "agents"
    skills_root = repo_root / "skills"
    for contract in iter_command_contracts(commands_root, agents_root):
        target = skills_root / codex_skill_name(contract.name)
        metadata_dir = target / "agents"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            render_codex_skill(contract), encoding="utf-8"
        )
        (metadata_dir / "openai.yaml").write_text(
            render_codex_metadata(contract), encoding="utf-8"
        )


if __name__ == "__main__":
    write_codex_projections(Path(__file__).resolve().parents[3])
