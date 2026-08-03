from __future__ import annotations

import re
from pathlib import Path

import yaml

from thinkweave.surfaces.mcp.tools import DISPATCH


ROOT = Path(__file__).parents[1]
SKILLS_ROOT = ROOT / "skills"
EXPECTED = {
    "thinkweave-capture",
    "thinkweave-recall",
    "thinkweave-research",
    "thinkweave-wrap",
}


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    _, raw, _ = text.split("---\n", 2)
    return yaml.safe_load(raw)


def test_minimal_codex_skill_bundle_is_valid() -> None:
    assert {path.name for path in SKILLS_ROOT.iterdir() if path.is_dir()} == EXPECTED

    for name in EXPECTED:
        skill_file = SKILLS_ROOT / name / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        metadata = _frontmatter(text)

        assert metadata.keys() == {"name", "description"}
        assert metadata["name"] == name
        assert metadata["description"].strip()
        assert "TODO" not in text
        assert "Skill(skill=" not in text


def test_codex_skills_reference_registered_mcp_tools() -> None:
    for skill_file in SKILLS_ROOT.glob("*/SKILL.md"):
        referenced = set(re.findall(r"\bweave_[a-z_]+\b", skill_file.read_text(encoding="utf-8")))
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