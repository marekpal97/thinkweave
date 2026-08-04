from __future__ import annotations

import re
from pathlib import Path

import yaml

from thinkweave.core import harness
from thinkweave.core.skill_projection import (
    codex_skill_name,
    iter_command_contracts,
    render_codex_metadata,
    render_codex_skill,
)
from thinkweave.surfaces.mcp.tools import DISPATCH


ROOT = Path(__file__).parents[1]
SKILLS_ROOT = ROOT / "skills"
COMMANDS_ROOT = ROOT / "commands"
AGENTS_ROOT = ROOT / "agents"


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    _, raw, _ = text.split("---\n", 2)
    return yaml.safe_load(raw)


def test_every_supported_command_has_a_drift_free_codex_projection() -> None:
    contracts = list(iter_command_contracts(COMMANDS_ROOT, AGENTS_ROOT))
    assert len(contracts) == 27

    for contract in contracts:
        skill_dir = SKILLS_ROOT / codex_skill_name(contract.name)
        skill_file = skill_dir / "SKILL.md"
        metadata_file = skill_dir / "agents" / "openai.yaml"

        assert skill_file.read_text(encoding="utf-8") == render_codex_skill(contract)
        assert metadata_file.read_text(encoding="utf-8") == render_codex_metadata(contract)

        metadata = _frontmatter(skill_file.read_text(encoding="utf-8"))
        assert metadata.keys() == {"name", "description"}
        assert metadata["name"] == codex_skill_name(contract.name)
        assert metadata["description"].strip()


def test_codex_worker_projection_uses_native_subagents_and_shared_contracts() -> None:
    worker_backed = [
        contract
        for contract in iter_command_contracts(COMMANDS_ROOT, AGENTS_ROOT)
        if contract.workers
    ]
    assert worker_backed

    for contract in worker_backed:
        text = (SKILLS_ROOT / codex_skill_name(contract.name) / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "spawn_agent" in text
        assert "Task({" not in text
        for worker in contract.workers:
            assert f"../../agents/{worker}.md" in text

    assert harness.codex(home=ROOT).subagents is True


def test_dream_and_drain_name_their_headless_degradation_without_hiding_workers() -> None:
    for name in ("dream", "drain"):
        text = (SKILLS_ROOT / codex_skill_name(name) / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Codex support status" in text
        assert "degraded" in text.lower()
        assert "#110" in text
        assert "spawn_agent" in text


def test_codex_skills_reference_registered_mcp_tools() -> None:
    for skill_file in SKILLS_ROOT.glob("*/SKILL.md"):
        referenced = set(
            re.findall(r"\bweave_[a-z_]+\b", skill_file.read_text(encoding="utf-8"))
        )
        assert referenced <= DISPATCH.keys(), (skill_file, referenced - DISPATCH.keys())


def test_codex_skill_metadata_declares_thinkweave_dependency() -> None:
    for metadata_file in SKILLS_ROOT.glob("*/agents/openai.yaml"):
        metadata = yaml.safe_load(metadata_file.read_text(encoding="utf-8"))
        assert metadata["interface"]["default_prompt"].startswith("Use $thinkweave-")
        assert metadata["dependencies"]["tools"] == [
            {
                "type": "mcp",
                "value": "thinkweave",
                "description": "ThinkWeave durable-memory tools",
            }
        ]


def test_codex_only_recall_skill_remains_valid() -> None:
    skill_file = SKILLS_ROOT / "thinkweave-recall" / "SKILL.md"
    metadata = _frontmatter(skill_file.read_text(encoding="utf-8"))
    assert metadata["name"] == "thinkweave-recall"
