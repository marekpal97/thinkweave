"""The thinkweave half of the devloop seam (#346), through the real shim.

``scripts/issue_loop.py`` points devloop at ``<rsi_rail>/devloop`` (the
``DEVLOOP_POLICY_DIR`` env var) when the rail is configured, and hands
``trajectory`` the run's manifest (``--manifest-json``); ``record_run`` lands
the payload's manifest on the round. Both #346 demos run here end to end on
the fixture issue in ``tests/fixtures/devloop_seam/`` (offline: its ``gh``
stub is first on PATH), with and without a rail.

The suite's autouse fixture already hides the developer's rail (no
``THINKWEAVE_RSI_RAIL``, ``XDG_CONFIG_HOME`` in tmp), so "no rail" is the
default here and the rail case builds its own throwaway repo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest
from devloop import cli as devloop_cli
from devloop import pack as devloop_pack

from tests.conftest import init_repo, run_git
from thinkweave.core.config import Config
from thinkweave.core.task_contract import validate_task_note
from thinkweave.core.vault import parse_frontmatter
from thinkweave.operations import tasks

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "scripts" / "issue_loop.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "devloop_seam"
MARKER = "## Standing orders (seam-test)"


def _demo():
    spec = importlib.util.spec_from_file_location("devloop_seam_demo", FIXTURES / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pin() -> str:
    origin = json.loads(distribution("devloop").read_text("direct_url.json") or "{}")
    return origin["vcs_info"]["commit_id"]


@pytest.fixture
def rail(tmp_path: Path) -> Path:
    """A committed rsi-rail repo whose ``devloop/`` holds a marked standing
    orders text and a constitution, and no ``loop.toml``."""
    repo = init_repo(tmp_path / "rsi-rail")
    (repo / "thinkweave").mkdir()
    (repo / "thinkweave" / "config.toml").write_text("", encoding="utf-8")
    (repo / "devloop").mkdir()
    orders = devloop_pack.STANDING_ORDERS.replace("## Standing orders", MARKER, 1)
    (repo / "devloop" / "standing-orders.md").write_text(orders + "\n", encoding="utf-8")
    (repo / "devloop" / "constitution.md").write_text(
        devloop_cli.PACKAGE_CONSTITUTION.read_text(encoding="utf-8"), encoding="utf-8")
    run_git("add", "-A", cwd=repo)
    run_git("commit", "-q", "-m", "rail", cwd=repo)
    return repo


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "DEVLOOP_POLICY_DIR"}
    env.update(extra)
    return env


def _config(env: dict) -> dict:
    out = subprocess.run([sys.executable, str(SHIM), "config"], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# ---------------------------------------------------------------------------
# The policy directory


def test_rail_set_points_devloop_at_its_devloop_folder(rail: Path):
    cfg = _config(_env(THINKWEAVE_RSI_RAIL=str(rail)))
    assert cfg["constitution"] == [str(rail / "devloop" / "constitution.md")]


def test_rail_set_keeps_loop_toml_in_docs_agents(rail: Path):
    """The rail holds no loop.toml: the resolved config is the golden's,
    found by the cwd walk, with or without the rail."""
    with_rail = _config(_env(THINKWEAVE_RSI_RAIL=str(rail)))
    without = _config(_env())
    with_rail.pop("constitution"), without.pop("constitution")
    golden = json.loads((REPO_ROOT / "tests" / "devloop_golden_config.json").read_text())
    assert with_rail == without == golden


def test_explicit_policy_env_wins_over_the_rail(rail: Path):
    """``DEVLOOP_POLICY_DIR=`` (empty) is the opt-out: the packaged texts
    serve even with the rail configured."""
    cfg = _config(_env(THINKWEAVE_RSI_RAIL=str(rail), DEVLOOP_POLICY_DIR=""))
    assert cfg["constitution"] == [str(devloop_cli.PACKAGE_CONSTITUTION)]


def test_misconfigured_rail_is_a_json_error_not_a_traceback(tmp_path: Path):
    env = _env(THINKWEAVE_RSI_RAIL=str(tmp_path / "missing"))
    out = subprocess.run([sys.executable, str(SHIM), "config"], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 2
    assert "thinkweave host seam" in json.loads(out.stdout)["error"]


# ---------------------------------------------------------------------------
# The two #346 demos, end to end through the shim


def test_demo_with_the_rail(rail: Path, tmp_path: Path):
    """(a) The round's manifest names the rail commit and the funloops pin;
    the implementer pack shows the rail's edited standing-orders heading; the
    stage log keeps its ``skill`` key."""
    seen = _demo().run_demo(tmp_path / "vault", _env(THINKWEAVE_RSI_RAIL=str(rail)))

    assert MARKER in seen["pack"]
    manifest = seen["round"]["manifest"]
    assert manifest == seen["payload_manifest"], "the round carries the payload's manifest"
    assert manifest["rsi_rail"] == {"repo": str(rail), "commit": run_git("rev-parse", "HEAD",
                                    cwd=rail), "dirty": False}
    assert manifest["readers"]["funloops"] == _pin()
    assert [s.get("skill") for s in seen["round"]["skills"]] == ["tdd", None]
    assert Path(seen["trajectory_note"]).is_file()
    note = parse_frontmatter(Path(seen["task_note"]).read_text(encoding="utf-8"))[0]
    assert validate_task_note(note) == []


def test_demo_without_the_rail(tmp_path: Path):
    """(b) Pack text comes from the module constants and the run completes;
    the round's manifest names no rail but still the funloops pin."""
    seen = _demo().run_demo(tmp_path / "vault", _env())

    assert devloop_pack.STANDING_ORDERS in seen["pack"]
    assert MARKER not in seen["pack"]
    assert seen["constitution"] == [str(devloop_cli.PACKAGE_CONSTITUTION)]
    manifest = seen["round"]["manifest"]
    assert "rsi_rail" not in manifest
    assert manifest["readers"]["funloops"] == _pin()
    assert [s.get("skill") for s in seen["round"]["skills"]] == ["tdd", None]


def test_demo_refuses_the_live_vault():
    with pytest.raises(SystemExit, match="live vault"):
        _demo().run_demo(Path("/mnt/c/Users/marek/vault"), _env())


# ---------------------------------------------------------------------------
# record_run: the payload's manifest wins


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    config = Config(vault_root=tmp_path / "vault")
    (config.vault_root / "config").mkdir(parents=True)
    (config.vault_root / "config" / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    monkeypatch.setattr("thinkweave.core.config.load_config", lambda: config)
    monkeypatch.setattr(tasks, "_gh_issue", lambda repo, number: tasks.IssueNode.from_api({}))
    return config


def _run_payload() -> dict:
    return json.loads(
        (REPO_ROOT / "tests" / "tasks" / "fixtures" / "devloop-run-rich.json").read_text())


def test_record_run_lands_the_payload_manifest(cfg: Config):
    carried = {"harness": "claude-code", "readers": {"thinkweave": "x", "funloops": "y"},
               "rsi_rail": {"repo": "/r", "commit": "a" * 40, "dirty": True}}
    run = {**_run_payload(), "manifest": carried}
    landed = tasks.record_run(cfg, run, project="t")
    (path,) = cfg.vault_root.rglob(f"{landed.task_id}.md")
    (entry,) = parse_frontmatter(path.read_text(encoding="utf-8"))[0]["rounds"]
    assert entry["manifest"] == carried


def test_record_run_refuses_a_manifest_that_is_not_a_mapping(cfg: Config):
    with pytest.raises(ValueError, match="manifest"):
        tasks.record_run(cfg, {**_run_payload(), "manifest": "c3c82c3"}, project="t")
    assert not list(cfg.vault_root.rglob("tsk-*.md"))
