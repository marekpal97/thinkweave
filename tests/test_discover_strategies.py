"""Tests for the discovery-strategy registry and built-in strategies.

Each test sets up a tiny vault with the smallest fixture needed to
exercise the strategy in isolation. None of these touch the network or
spawn LLMs.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.discover import REGISTRY, get, names, register
from personal_mem.discover.strategies import (
    concept_coverage,
    decision_review,
    external_tool_runner,
    theme_drift,
)


# --- Registry ---------------------------------------------------------------


class TestRegistry:
    def test_built_ins_register_on_import(self) -> None:
        assert "concept_coverage" in REGISTRY
        assert "decision_review" in REGISTRY
        assert "theme_drift" in REGISTRY
        assert "external_tool_runner" in REGISTRY

    def test_get_returns_strategy(self) -> None:
        s = get("concept_coverage")
        assert s.name == "concept_coverage"
        assert hasattr(s, "run")

    def test_get_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            get("nope")

    def test_register_overwrites(self) -> None:
        class FakeStrategy:
            name = "fake_strategy"

            def run(self, vault, project, config):
                return [{"hello": "world"}]

        register(FakeStrategy())
        assert "fake_strategy" in names()
        assert get("fake_strategy").run(None, None, {}) == [{"hello": "world"}]
        # cleanup
        REGISTRY.pop("fake_strategy", None)

    def test_register_rejects_unnamed(self) -> None:
        class Bad:
            name = ""

        with pytest.raises(ValueError):
            register(Bad())


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def config(vault_dir: Path) -> Config:
    return Config(vault_root=vault_dir)


@pytest.fixture
def vault(config: Config) -> VaultManager:
    vm = VaultManager(config=config)
    vm.ensure_dirs()
    return vm


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


def _index(vault: VaultManager, indexer: Indexer) -> None:
    indexer.rebuild(full=True)


# --- concept_coverage -------------------------------------------------------


class TestConceptCoverage:
    def test_returns_empty_without_index(self, config: Config) -> None:
        result = concept_coverage.STRATEGY.run(config, None, {})
        assert result == []

    def test_finds_under_sourced_concept(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # 5 notes mention "graph-memory", 0 sources → it's a gap.
        for i in range(5):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                body="See [[graph-memory]].",
                project="test",
                extra_frontmatter={"concepts": ["graph-memory", "ai/memory"]},
            )
        _index(vault, indexer)

        result = concept_coverage.STRATEGY.run(
            config, "test", {"projects": {"default": {}}}
        )
        concepts = {item["concept"] for item in result}
        assert "graph-memory" in concepts

    def test_skips_well_sourced_concept(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # Concept has 3 source notes — should NOT be flagged as a gap.
        for i in range(3):
            vault.create_note(
                NoteType.SOURCE,
                f"Paper {i}",
                body="Background on graph-memory.",
                project="test",
                extra_frontmatter={
                    "source_type": "paper",
                    "url": f"https://x/{i}",
                    "concepts": ["graph-memory", "ai/memory"],
                },
            )
        for i in range(5):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                body="See it.",
                project="test",
                extra_frontmatter={"concepts": ["graph-memory", "ai/memory"]},
            )
        _index(vault, indexer)

        result = concept_coverage.STRATEGY.run(
            config, "test", {"projects": {"default": {}}}
        )
        for item in result:
            assert item["concept"] != "graph-memory"

    def test_respects_min_mentions(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # Only 1 mention of "rare-thing" → shouldn't surface even though
        # it has 0 source coverage.
        vault.create_note(
            NoteType.NOTE,
            "Lone",
            body="See [[rare-thing]].",
            project="test",
            extra_frontmatter={"concepts": ["rare-thing", "ai/memory"]},
        )
        _index(vault, indexer)

        cfg = {
            "projects": {
                "default": {
                    "concept_coverage": {"min_mentions": 5},
                }
            }
        }
        result = concept_coverage.STRATEGY.run(config, "test", cfg)
        for item in result:
            assert item["concept"] != "rare-thing"


# --- decision_review --------------------------------------------------------


class TestDecisionReview:
    def test_flags_old_proposed(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        old = (date.today() - timedelta(days=60)).isoformat()
        vault.create_note(
            NoteType.DECISION,
            "Old proposal",
            project="test",
            extra_frontmatter={
                "status": "proposed",
                "date": old,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        # Patch the indexed `date` column directly: indexer derives `date`
        # from frontmatter when present; the create_note path already
        # respects extra_frontmatter['date'], so this should work.
        result = decision_review.STRATEGY.run(config, "test", {})
        titles = [item["title"] for item in result]
        assert any("Old proposal" in t for t in titles)

    def test_skips_recent_proposed(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        recent = date.today().isoformat()
        vault.create_note(
            NoteType.DECISION,
            "Fresh proposal",
            project="test",
            extra_frontmatter={
                "status": "proposed",
                "date": recent,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        result = decision_review.STRATEGY.run(config, "test", {})
        for item in result:
            assert "Fresh proposal" not in item["title"]

    def test_skips_superseded(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        old = (date.today() - timedelta(days=60)).isoformat()
        vault.create_note(
            NoteType.DECISION,
            "Old superseded",
            project="test",
            extra_frontmatter={
                "status": "superseded",
                "date": old,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        result = decision_review.STRATEGY.run(config, "test", {})
        for item in result:
            assert "Old superseded" not in item["title"]


# --- theme_drift ------------------------------------------------------------


class TestThemeDrift:
    def test_flags_silent_theme(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        old = (date.today() - timedelta(days=120)).isoformat()
        vault.create_note(
            NoteType.THEME,
            "Stale Theme",
            body="## Essence\nFoo.\n\n## Catalyst log\n- 2024-01-01 · *new* — old.\n",
            project="test",
            extra_frontmatter={
                "status": "active",
                "date": old,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        result = theme_drift.STRATEGY.run(config, None, {})
        titles = [item["title"] for item in result]
        assert any("Stale Theme" in t for t in titles)

    def test_skips_active_recent_theme(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        recent = date.today().isoformat()
        vault.create_note(
            NoteType.THEME,
            "Hot Theme",
            body=(
                "## Essence\nFresh.\n\n"
                f"## Catalyst log\n- {recent} · *new* — yes.\n"
            ),
            project="test",
            extra_frontmatter={
                "status": "active",
                "date": recent,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        result = theme_drift.STRATEGY.run(config, None, {})
        for item in result:
            assert "Hot Theme" not in item["title"]

    def test_skips_dormant_status(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        old = (date.today() - timedelta(days=120)).isoformat()
        vault.create_note(
            NoteType.THEME,
            "Sleeping",
            body="## Essence\nx.\n## Catalyst log\n",
            project="test",
            extra_frontmatter={
                "status": "dormant",
                "date": old,
                "concepts": ["x", "y"],
            },
        )
        _index(vault, indexer)

        result = theme_drift.STRATEGY.run(config, None, {})
        for item in result:
            assert "Sleeping" not in item["title"]


# --- external_tool_runner ---------------------------------------------------


class TestExternalToolRunner:
    def test_returns_empty_when_no_tools(self) -> None:
        result = external_tool_runner.STRATEGY.run(None, "anyproj", {})
        assert result == []

    def test_parses_jsonl_stdout(self, tmp_path: Path) -> None:
        # Tiny fixture script that emits two JSONL items.
        script = tmp_path / "fixture.py"
        script.write_text(
            'import sys, json\n'
            'print(json.dumps({"id": "test1", "url": "https://x/1", "title": "A"}))\n'
            'print(json.dumps({"id": "test2", "url": "https://x/2", "title": "B"}))\n',
            encoding="utf-8",
        )

        cfg = {
            "projects": {
                "trade_ideas": {
                    "external_tool_runner": {
                        "tools": [["python3", str(script)]],
                    }
                }
            }
        }
        result = external_tool_runner.STRATEGY.run(None, "trade_ideas", cfg)
        ids = [item["id"] for item in result]
        assert "test1" in ids and "test2" in ids
        assert all(item["strategy"] == "external_tool_runner" for item in result)

    def test_skips_non_dict_output(self, tmp_path: Path) -> None:
        script = tmp_path / "bad.py"
        script.write_text(
            'import sys\n'
            'print("[1,2,3]")\nprint("not json")\nprint("")\n',
            encoding="utf-8",
        )
        cfg = {
            "projects": {
                "default": {
                    "external_tool_runner": {
                        "tools": [["python3", str(script)]],
                    }
                }
            }
        }
        result = external_tool_runner.STRATEGY.run(None, None, cfg)
        assert result == []

    def test_handles_missing_executable(self) -> None:
        cfg = {
            "projects": {
                "default": {
                    "external_tool_runner": {
                        "tools": [["/nonexistent/binary/path"]],
                    }
                }
            }
        }
        # Should not raise — missing exes are silently dropped.
        result = external_tool_runner.STRATEGY.run(None, None, cfg)
        assert result == []

    def test_string_command_form(self, tmp_path: Path) -> None:
        script = tmp_path / "echo.py"
        script.write_text(
            'import json\nprint(json.dumps({"id": "ok"}))\n',
            encoding="utf-8",
        )
        cfg = {
            "projects": {
                "default": {
                    "external_tool_runner": {
                        "tools": [{"command": f"python3 {script}"}],
                    }
                }
            }
        }
        result = external_tool_runner.STRATEGY.run(None, None, cfg)
        assert any(item.get("id") == "ok" for item in result)
