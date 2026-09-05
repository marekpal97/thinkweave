from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from thinkweave.acquisition.sources import DEFAULT_CONFIG
from thinkweave.core import harness
from thinkweave.core.skill_projection import (
    codex_skill_name,
    iter_command_contracts,
    render_codex_metadata,
    render_codex_skill,
    write_codex_projections,
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
    assert len(contracts) == 29

    for contract in contracts:
        skill_dir = SKILLS_ROOT / codex_skill_name(contract.name)
        skill_file = skill_dir / "SKILL.md"

        assert skill_file.read_text(encoding="utf-8") == render_codex_skill(contract)

        metadata = _frontmatter(skill_file.read_text(encoding="utf-8"))
        assert metadata.keys() == {"name", "description"}
        assert metadata["name"] == codex_skill_name(contract.name)
        assert metadata["description"].strip()
        assert "../../docs/CODEX-SKILL-PROJECTION.md" in skill_file.read_text(
            encoding="utf-8"
        )

        metadata_file = skill_dir / "agents" / "openai.yaml"
        assert metadata_file.read_text(encoding="utf-8") == render_codex_metadata(
            contract
        )


def test_command_frontmatter_accounts_for_every_worker_exactly_where_dispatched() -> None:
    # ``workers:`` is declared, not inferred from prose — so the union is the
    # only thing keeping a real dispatch from going unprojected, and the
    # per-command assertions below pin the prose mentions that used to be
    # misread as dispatches (/tighten contrasting itself with the nightly
    # workers, /onboard listing brief formats, /seed-enrich comparing tails).
    declared = {
        contract.name: set(contract.workers)
        for contract in iter_command_contracts(COMMANDS_ROOT, AGENTS_ROOT)
    }
    assert set().union(*declared.values()) == {
        path.stem for path in AGENTS_ROOT.glob("*.md")
    }

    assert declared["tighten"] == set()
    assert declared["onboard"] == set()
    assert declared["seed-enrich"] == {"seed-enrich-worker"}
    # /drain's fan-out is config-driven, not spelled out in its command text:
    # every ``subagent_type`` in the shipped sources.yaml — plus the triage
    # helper the news lane runs ahead of the writers — needs a contract.
    fan_out = {
        source["subagent_type"]
        for source in DEFAULT_CONFIG["sources"].values()
        if source.get("subagent_type")
    }
    assert declared["drain"] == fan_out | {"news-triage-worker"}


def test_codex_worker_projection_uses_native_subagents_and_shared_contracts() -> None:
    worker_backed = [
        contract
        for contract in iter_command_contracts(COMMANDS_ROOT, AGENTS_ROOT)
        if contract.workers
    ]
    assert worker_backed
    assert {worker for contract in worker_backed for worker in contract.workers} == {
        path.stem for path in AGENTS_ROOT.glob("*.md")
    }

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
    skill_dirs = sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))
    assert skill_dirs
    for skill_dir in skill_dirs:
        # Globbing only the files that exist can't catch an absent sidecar —
        # every skill directory must carry one.
        metadata_file = skill_dir / "agents" / "openai.yaml"
        assert metadata_file.is_file(), f"missing Codex metadata: {metadata_file}"
        metadata = yaml.safe_load(metadata_file.read_text(encoding="utf-8"))
        assert metadata["interface"]["display_name"].strip()
        assert metadata["interface"]["short_description"].strip()
        assert metadata["interface"]["default_prompt"].startswith("Use $thinkweave-")
        assert metadata["dependencies"]["tools"] == [
            {
                "type": "mcp",
                "value": "thinkweave",
                "description": "ThinkWeave durable-memory tools",
            }
        ]


def _write_command(root: Path, name: str, frontmatter: str) -> None:
    (root / "commands").mkdir(parents=True, exist_ok=True)
    (root / "commands" / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: A stub command.\n{frontmatter}---\n\nbody\n",
        encoding="utf-8",
    )


def test_unknown_worker_reference_is_rejected(tmp_path: Path) -> None:
    # A typo'd or deleted worker would otherwise render a dead link into the
    # projection and only surface when a Codex agent tried to spawn it.
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "real-worker.md").write_text("spec", encoding="utf-8")
    _write_command(tmp_path, "stub", "workers:\n  - ghost-worker\n")

    with pytest.raises(ValueError, match="ghost-worker"):
        iter_command_contracts(tmp_path / "commands", tmp_path / "agents")


def test_two_commands_cannot_claim_one_skill_name(tmp_path: Path) -> None:
    # `name:` need not match the filename, so two files can collide; the second
    # write would silently overwrite the first projection.
    (tmp_path / "agents").mkdir()
    (tmp_path / "commands" / "nested").mkdir(parents=True)
    _write_command(tmp_path, "stub", "")
    (tmp_path / "commands" / "nested" / "other.md").write_text(
        "---\nname: stub\ndescription: A colliding stub.\n---\n\nbody\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="thinkweave-stub"):
        write_codex_projections(tmp_path)


def test_codex_only_recall_skill_remains_valid() -> None:
    skill_file = SKILLS_ROOT / "thinkweave-recall" / "SKILL.md"
    metadata = _frontmatter(skill_file.read_text(encoding="utf-8"))
    assert metadata["name"] == "thinkweave-recall"
    assert "Use only when the user invokes" in metadata["description"]
    assert "ordinary history" in metadata["description"]
