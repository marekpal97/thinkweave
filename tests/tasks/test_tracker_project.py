"""A task minted for a tracker ref files under the project its repo maps
to; a free-text ref, or a repo with no vault project, keeps the detected
project."""

from __future__ import annotations

import json
from pathlib import Path

from thinkweave.core.buffer import buffer_path
from thinkweave.core.config import Config
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations import tasks

FIXTURES = Path(__file__).parent / "fixtures"


def declare(cfg: Config, asked: str) -> Path:
    """Wrap one minting entry from a session detected as ``python_projects``;
    returns the minted note's path."""
    decl = {"declared": [{"title": "t", "asked": asked, "round": {}}]}
    result = tasks.apply_declaration(
        cfg, decl, session_key="s-1", project="python_projects",
        streams=[buffer_path(cfg.weave_dir, "s-1")],
    )
    assert result.errors == []
    (task_id,) = result.minted
    (path,) = cfg.vault_root.rglob(f"{task_id}.md")
    return path


def filed_under(cfg: Config, path: Path) -> tuple[str, str]:
    """(project folder, frontmatter project) of a task note."""
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return path.relative_to(cfg.vault_root / "projects").parts[0], fm["project"]


def test_a_wrap_ref_files_under_its_repo_project(cfg: Config):
    (cfg.vault_root / "projects" / "thinkweave").mkdir(parents=True)
    path = declare(cfg, "github:marekpal97/thinkweave#276")
    assert filed_under(cfg, path) == ("thinkweave", "thinkweave")


def test_a_ref_to_a_repo_with_no_project_keeps_the_detected_project(cfg: Config):
    path = declare(cfg, "github:marekpal97/thinkweave#276")
    assert filed_under(cfg, path) == ("python_projects", "python_projects")


def test_a_free_text_ref_keeps_the_detected_project(cfg: Config):
    (cfg.vault_root / "projects" / "thinkweave").mkdir(parents=True)
    path = declare(cfg, "tidy the release notes")
    assert filed_under(cfg, path) == ("python_projects", "python_projects")


def test_a_recorded_run_files_under_its_repo_project(cfg: Config):
    (cfg.vault_root / "projects" / "thinkweave").mkdir(parents=True)
    run = json.loads((FIXTURES / "devloop-run-rich.json").read_text(encoding="utf-8"))
    landed = tasks.record_run(cfg, run, project="funloops")
    (path,) = cfg.vault_root.rglob(f"{landed.task_id}.md")
    assert filed_under(cfg, path) == ("thinkweave", "thinkweave")
