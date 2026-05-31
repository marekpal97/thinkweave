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
    prompt_gap,
)


# --- Registry ---------------------------------------------------------------


class TestRegistry:
    def test_built_ins_register_on_import(self) -> None:
        assert "concept_coverage" in REGISTRY
        assert "decision_review" in REGISTRY
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
                extra_frontmatter={"concepts": ["graph-memory", "ai-memory"]},
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
                    "concepts": ["graph-memory", "ai-memory"],
                },
            )
        for i in range(5):
            vault.create_note(
                NoteType.NOTE,
                f"Note {i}",
                body="See it.",
                project="test",
                extra_frontmatter={"concepts": ["graph-memory", "ai-memory"]},
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
            extra_frontmatter={"concepts": ["rare-thing", "ai-memory"]},
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


# --- Probe-pressure bias (Slice 1.3) ----------------------------------------


def _seed_probe_events(
    config: Config, project: str, prompts: list[str]
) -> None:
    """Write a session events.jsonl with the given prompt texts; each is
    framed as a question so ``classify_probe`` flags it as ``probe``."""
    import datetime as _dt
    sess_dir = config.vault_root / "projects" / project / "sessions" / "ses-pp"
    sess_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    rows = []
    for i, text in enumerate(prompts):
        rows.append({
            "type": "prompt",
            "text": text,
            "session_id": "cc-pp",
            "ts": (now - _dt.timedelta(days=1, minutes=i)).isoformat(),
        })
    (sess_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


class TestProbePressureBias:
    """Probe-pressure horizontally biases the existing strategies'
    ordering. Without probes, behaviour is identical to pre-bias
    (covered by the existing tests above). With probes, the strategies
    surface the probed concept ahead of an equally-thin sibling."""

    def test_concept_coverage_pressure_reorders(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # Two equally-thin concepts (3 mentions each, 0 sources). Probe
        # only ``llm`` — it must surface ahead of ``training`` despite
        # equal mention count (sort is pressure DESC then mentions DESC).
        for i in range(3):
            vault.create_note(
                NoteType.NOTE,
                f"NoteA {i}",
                body="A",
                project="test",
                extra_frontmatter={"concepts": ["llm", "ai-memory"]},
            )
        for i in range(3):
            vault.create_note(
                NoteType.NOTE,
                f"NoteB {i}",
                body="B",
                project="test",
                extra_frontmatter={"concepts": ["training", "ai-memory"]},
            )
        _index(vault, indexer)
        _seed_probe_events(config, "test", ["How does the llm reason?"])

        result = concept_coverage.STRATEGY.run(
            config, "test", {"projects": {"default": {}}}
        )
        # Both concepts surface as gaps; ``llm`` ranks first due to
        # pressure (probe count 1 > 0 for training).
        ordered = [item["concept"] for item in result]
        assert "llm" in ordered and "training" in ordered
        assert ordered.index("llm") < ordered.index("training")
        # Probe-pressure surfaces on the descriptor for downstream
        # consumers (the /discover skill UI shows *why* it ranked).
        llm_item = next(c for c in result if c["concept"] == "llm")
        assert llm_item["probe_pressure"] == 1

    def test_decision_review_pressure_reorders(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        old = (date.today() - timedelta(days=60)).isoformat()
        # Two equally-stale proposed decisions; one touches ``llm``,
        # the other touches ``training``. Probe ``llm`` and assert
        # the llm decision surfaces first.
        vault.create_note(
            NoteType.DECISION,
            "Decision-LLM",
            project="test",
            extra_frontmatter={
                "status": "proposed",
                "date": old,
                "concepts": ["llm", "ai-memory"],
            },
        )
        vault.create_note(
            NoteType.DECISION,
            "Decision-Training",
            project="test",
            extra_frontmatter={
                "status": "proposed",
                "date": old,
                "concepts": ["training", "ai-memory"],
            },
        )
        _index(vault, indexer)
        _seed_probe_events(config, "test", ["How does the llm choose?"])

        result = decision_review.STRATEGY.run(config, "test", {})
        titles = [item["title"] for item in result]
        llm_idx = next(i for i, t in enumerate(titles) if "Decision-LLM" in t)
        training_idx = next(
            i for i, t in enumerate(titles) if "Decision-Training" in t
        )
        assert llm_idx < training_idx
        llm_item = next(
            d for d in result if "Decision-LLM" in d["title"]
        )
        assert llm_item["probe_pressure"] >= 1


# --- prompt_gap (Slice 1.4) --------------------------------------------------


class TestPromptGap:
    """Residual strategy: emits gaps for hyphenated kebab tokens that
    appear in probe-classified prompts but aren't in the ontology
    (canonical OR proposed)."""

    def test_empty_when_no_probes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        _index(vault, indexer)
        result = prompt_gap.STRATEGY.run(config, "test", {})
        assert result == []

    def test_surfaces_unknown_kebab_token(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        _index(vault, indexer)
        _seed_probe_events(
            config,
            "test",
            [
                "How does dynamic-batching work?",
                "What's the trade-off with dynamic-batching?",
            ],
        )
        result = prompt_gap.STRATEGY.run(config, "test", {})
        concepts = [r["concept"] for r in result]
        assert "dynamic-batching" in concepts
        item = next(r for r in result if r["concept"] == "dynamic-batching")
        assert item["concept_status"] == "unknown"
        assert item["probe_pressure"] >= 2
        assert item["queue"] == "ontology"

    def test_skips_known_canonical_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # ``ai-memory`` is a canonical ontology concept — must NOT
        # surface as a prompt_gap even though it's hyphenated.
        _index(vault, indexer)
        _seed_probe_events(
            config,
            "test",
            [
                "How does ai-memory work?",
                "Where is ai-memory stored?",
            ],
        )
        result = prompt_gap.STRATEGY.run(config, "test", {})
        assert all(r["concept"] != "ai-memory" for r in result)

    def test_skips_proposed_concepts(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # ``frobnicate-widget`` is in proposed_concepts on another note —
        # the residual strategy must defer to concept_coverage for it.
        vault.create_note(
            NoteType.NOTE,
            "Stub",
            project="test",
            extra_frontmatter={"proposed_concepts": ["frobnicate-widget"]},
        )
        _index(vault, indexer)
        _seed_probe_events(
            config,
            "test",
            [
                "How does frobnicate-widget work?",
                "What does frobnicate-widget output?",
            ],
        )
        result = prompt_gap.STRATEGY.run(config, "test", {})
        assert all(r["concept"] != "frobnicate-widget" for r in result)

    def test_respects_min_pressure(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # Single probe (count=1) is below default min_pressure=2.
        _index(vault, indexer)
        _seed_probe_events(
            config, "test", ["How does dynamic-batching work?"]
        )
        result = prompt_gap.STRATEGY.run(config, "test", {})
        assert all(r["concept"] != "dynamic-batching" for r in result)

    def test_ignores_single_word_probes(
        self, vault: VaultManager, indexer: Indexer, config: Config
    ) -> None:
        # Hyphenated only — single-word "frobnicate" should not surface.
        _index(vault, indexer)
        _seed_probe_events(
            config,
            "test",
            [
                "How does frobnicate work?",
                "Where is frobnicate?",
                "What does frobnicate do?",
            ],
        )
        result = prompt_gap.STRATEGY.run(config, "test", {})
        assert all(r["concept"] != "frobnicate" for r in result)
