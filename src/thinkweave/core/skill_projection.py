"""Project canonical ThinkWeave commands into Codex Agent Skills.

The command and worker markdown files remain the semantic source of truth.
Codex projections are deliberately small: they point at that source and add
only the harness vocabulary needed to execute it with native tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from thinkweave.core.vault import parse_frontmatter


@dataclass(frozen=True)
class CommandContract:
    name: str
    description: str
    source_relpath: Path
    workers: tuple[str, ...]


def codex_skill_name(name: str) -> str:
    return f"thinkweave-{name}"


def iter_command_contracts(
    commands_root: Path, agents_root: Path
) -> tuple[CommandContract, ...]:
    """Return every supported command and the workers its contract declares.

    ``workers:`` in a command's frontmatter is the source of truth: the worker
    contracts an agent executing that command spawns, either through its own
    ``Task`` calls or through the drain rail it sequences inline. Prose that
    merely names a worker (``/tighten`` contrasting itself with the nightly
    ``dream-merge-worker``) declares nothing.
    """
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
        declared = metadata.get("workers") or []
        if isinstance(declared, str):
            raise ValueError(f"workers: must be a list: {source}")
        unknown = sorted(set(declared) - worker_names)
        if unknown:
            raise ValueError(
                f"command names workers without an agents/ contract: "
                f"{source} -> {unknown}"
            )
        contracts.append(
            CommandContract(
                name=name,
                description=description,
                source_relpath=source.relative_to(commands_root.parent),
                workers=tuple(sorted(set(declared))),
            )
        )
    return tuple(contracts)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


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
        "Read the [shared Codex adapter](../../docs/CODEX-SKILL-PROJECTION.md)",
        "and the [canonical ThinkWeave command contract]"
        f"({command_ref}) completely, then execute the canonical contract through",
        "that adapter.",
    ]

    if contract.workers:
        lines += [
            "",
            "Use the adapter's native `spawn_agent` procedure for these shared",
            "worker contracts:",
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


def write_codex_projections(repo_root: Path) -> None:
    commands_root = repo_root / "commands"
    agents_root = repo_root / "agents"
    skills_root = repo_root / "skills"
    for contract in iter_command_contracts(commands_root, agents_root):
        target = skills_root / codex_skill_name(contract.name)
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            render_codex_skill(contract), encoding="utf-8"
        )


if __name__ == "__main__":
    write_codex_projections(Path(__file__).resolve().parents[3])
