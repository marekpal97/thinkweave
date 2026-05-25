"""Regression tests for scripts/ontology_cleanup_2026_05.py.

Locks the four cleanup operation classes (tag_renames, tag_to_concept,
tag_deletes, concept_deletes) and the per-note pure function plus the
vault-walking driver against a temp-vault fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ontology_cleanup_2026_05 import (  # noqa: E402
    CONCEPT_DELETES,
    SKIP_DIRS,
    TAG_DELETES,
    TAG_RENAMES,
    TAG_TO_CONCEPT,
    apply_operations,
    cleanup_vault,
)

from personal_mem.core.vault import parse_frontmatter, render_frontmatter  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _write_note(path: Path, fm: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_frontmatter(fm) + "\n\n" + body, encoding="utf-8")


def _read_fm(path: Path) -> dict:
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


# ----------------------------------------------------------------------
# Pure function tests — apply_operations
# ----------------------------------------------------------------------


class TestApplyOperations:
    def test_tag_renames_consolidates_paradigm_variants(self):
        fm = {
            "id": "n-1",
            "tags": ["paradigm-shift", "paradigm-extension", "todo"],
            "concepts": [],
        }
        new_fm, counts = apply_operations(fm)
        # Both rewrites fire, and dedup collapses them to a single 'paradigm'.
        assert new_fm["tags"] == ["paradigm", "todo"]
        assert counts["tag_renames"] == 2

    def test_tag_renames_paradigm_decision_also_collapses(self):
        # paradigm-decision → paradigm; verifies all three slate variants.
        fm = {"tags": ["paradigm-decision", "paradigm"], "concepts": []}
        new_fm, _ = apply_operations(fm)
        assert new_fm["tags"] == ["paradigm"]

    def test_tag_to_concept_moves_agents_to_agentic_systems(self):
        fm = {"tags": ["agents", "todo"], "concepts": ["llms"]}
        new_fm, counts = apply_operations(fm)
        assert "agents" not in new_fm["tags"]
        assert "todo" in new_fm["tags"]
        assert "agentic-systems" in new_fm["concepts"]
        assert "llms" in new_fm["concepts"]
        assert counts["tag_to_concept"] == 1

    def test_tag_to_concept_preserves_existing_target_concept(self):
        # If the target concept is already present, do not duplicate.
        fm = {"tags": ["agents"], "concepts": ["agentic-systems", "llms"]}
        new_fm, counts = apply_operations(fm)
        assert new_fm["concepts"].count("agentic-systems") == 1
        assert new_fm["concepts"] == ["agentic-systems", "llms"]
        assert counts["tag_to_concept"] == 1
        assert "tags" not in new_fm  # only tag was 'agents', moved → empty → dropped

    def test_tag_deletes_strips_lifecycle_noise(self):
        fm = {
            "tags": ["lesson", "pivot", "feedback", "todo"],
            "concepts": [],
        }
        new_fm, counts = apply_operations(fm)
        assert new_fm["tags"] == ["todo"]
        assert counts["tag_deletes"] == 3

    def test_concept_deletes_strips_generic_concepts(self):
        fm = {
            "tags": [],
            "concepts": ["framework", "infrastructure", "fts5"],
        }
        new_fm, counts = apply_operations(fm)
        assert new_fm["concepts"] == ["fts5"]
        assert counts["concept_deletes"] == 2

    def test_to_concept_wins_over_deletes_for_overlapping_keys(self):
        # 'continual-learning' is in BOTH TAG_TO_CONCEPT and TAG_DELETES.
        # Operation order ensures the tag is moved (info-preserving),
        # not silently dropped.
        assert "continual-learning" in TAG_TO_CONCEPT
        assert "continual-learning" in TAG_DELETES
        fm = {"tags": ["continual-learning"], "concepts": []}
        new_fm, counts = apply_operations(fm)
        assert "tags" not in new_fm
        assert new_fm["concepts"] == ["continual-learning"]
        assert counts["tag_to_concept"] == 1
        assert counts["tag_deletes"] == 0

    def test_empty_tags_array_drops_key_entirely(self):
        # If all tags are stripped, the key should not appear (no `tags: []`).
        fm = {"tags": ["lesson", "feedback"], "concepts": []}
        new_fm, _ = apply_operations(fm)
        assert "tags" not in new_fm
        assert "concepts" not in new_fm  # was empty input → also dropped

    def test_no_op_note_returns_unchanged_arrays(self):
        # Tags/concepts that match no rule round-trip identically.
        fm = {"id": "n-x", "tags": ["todo"], "concepts": ["fts5", "sqlite"]}
        new_fm, counts = apply_operations(fm)
        assert new_fm["tags"] == ["todo"]
        assert new_fm["concepts"] == ["fts5", "sqlite"]
        assert sum(counts.values()) == 0

    def test_dedup_preserves_first_occurrence_order(self):
        # Two renames collapsing to the same target preserve insertion
        # order — the first rewrite stays, the duplicate is dropped.
        fm = {
            "tags": ["benchmarking", "todo", "benchmark"],
            "concepts": [],
        }
        new_fm, _ = apply_operations(fm)
        assert new_fm["tags"] == ["benchmark", "todo"]


# ----------------------------------------------------------------------
# Vault-walker tests — cleanup_vault
# ----------------------------------------------------------------------


class TestCleanupVault:
    def test_dry_run_does_not_write(self, tmp_path: Path):
        note = tmp_path / "n-1.md"
        _write_note(
            note,
            {"id": "n-1", "tags": ["lesson", "todo"], "concepts": ["fts5"]},
        )
        original = note.read_text(encoding="utf-8")

        stats = cleanup_vault(tmp_path, apply=False)

        # Stats reflect the impact, but the file is untouched.
        assert stats.notes_scanned == 1
        assert stats.notes_modified == 1
        assert stats.tag_deletes.notes == 1
        assert stats.tag_deletes.total == 1
        assert note.read_text(encoding="utf-8") == original

    def test_apply_writes_updated_frontmatter(self, tmp_path: Path):
        note = tmp_path / "n-1.md"
        _write_note(
            note,
            {"id": "n-1", "tags": ["lesson", "todo"], "concepts": ["fts5"]},
            body="Body content.",
        )

        stats = cleanup_vault(tmp_path, apply=True)
        assert stats.notes_modified == 1

        fm = _read_fm(note)
        assert fm["tags"] == ["todo"]
        assert fm["concepts"] == ["fts5"]
        # Body preserved.
        assert "Body content." in note.read_text(encoding="utf-8")

    def test_skip_dirs_are_honoured(self, tmp_path: Path):
        # Notes under SKIP_DIRS must not be touched even with --apply.
        for skip in SKIP_DIRS:
            target = tmp_path / skip / "n.md"
            _write_note(
                target,
                {"id": "n", "tags": ["lesson"], "concepts": ["framework"]},
            )

        stats = cleanup_vault(tmp_path, apply=True)
        assert stats.notes_scanned == 0
        assert stats.notes_modified == 0

        # Files still contain the original (untouched) values.
        for skip in SKIP_DIRS:
            fm = _read_fm(tmp_path / skip / "n.md")
            assert fm["tags"] == ["lesson"]
            assert fm["concepts"] == ["framework"]

    def test_unchanged_note_is_not_rewritten(self, tmp_path: Path):
        # A note whose frontmatter survives all four ops untouched
        # must not be rewritten — protects against formatting drift.
        note = tmp_path / "n-stable.md"
        _write_note(
            note,
            {"id": "n-stable", "tags": ["todo"], "concepts": ["fts5", "sqlite"]},
            body="Stable body.",
        )
        before_mtime = note.stat().st_mtime_ns
        before_text = note.read_text(encoding="utf-8")

        stats = cleanup_vault(tmp_path, apply=True)
        assert stats.notes_scanned == 1
        assert stats.notes_modified == 0

        # Identical bytes — no rewrite happened.
        assert note.read_text(encoding="utf-8") == before_text
        assert note.stat().st_mtime_ns == before_mtime

    def test_end_to_end_all_four_ops_on_one_note(self, tmp_path: Path):
        # A single note exercising every op class in one pass.
        note = tmp_path / "n-multi.md"
        _write_note(
            note,
            {
                "id": "n-multi",
                "tags": [
                    "paradigm-shift",  # rename → paradigm
                    "agents",          # to_concept → agentic-systems
                    "lesson",          # delete
                    "todo",            # keep
                ],
                "concepts": [
                    "framework",       # delete
                    "fts5",            # keep
                ],
            },
        )

        stats = cleanup_vault(tmp_path, apply=True)
        assert stats.notes_modified == 1
        # Each op class fires once on this note.
        assert stats.tag_renames.notes == 1
        assert stats.tag_to_concept.notes == 1
        assert stats.tag_deletes.notes == 1
        assert stats.concept_deletes.notes == 1
        assert stats.tag_renames.total == 1
        assert stats.tag_to_concept.total == 1
        assert stats.tag_deletes.total == 1
        assert stats.concept_deletes.total == 1

        fm = _read_fm(note)
        assert fm["tags"] == ["paradigm", "todo"]
        # 'agentic-systems' moved into concepts; 'framework' stripped.
        assert fm["concepts"] == ["fts5", "agentic-systems"]

    def test_concept_deletes_drops_key_when_array_empties(self, tmp_path: Path):
        # If the only concept is one that gets deleted, the key disappears.
        note = tmp_path / "n-empty-concepts.md"
        _write_note(
            note,
            {"id": "n", "tags": ["todo"], "concepts": ["framework"]},
        )

        cleanup_vault(tmp_path, apply=True)

        text = note.read_text(encoding="utf-8")
        # Neither 'concepts: []' nor a 'concepts:' key should appear.
        assert "concepts:" not in text
        fm = _read_fm(note)
        assert "concepts" not in fm
        assert fm["tags"] == ["todo"]

    def test_samples_capped_at_sample_size(self, tmp_path: Path):
        # Five matches but sample_size=3 → only 3 sample paths recorded.
        for i in range(5):
            _write_note(
                tmp_path / f"n-{i}.md",
                {"id": f"n-{i}", "tags": ["lesson"], "concepts": []},
            )
        stats = cleanup_vault(tmp_path, apply=False, sample_size=3)
        assert stats.tag_deletes.notes == 5
        assert stats.tag_deletes.total == 5
        assert len(stats.samples["tag_deletes"]) == 3

    def test_errors_collected_for_unreadable_notes(self, tmp_path: Path):
        # Write a note with bytes that can't decode as UTF-8.
        bad = tmp_path / "n-bad.md"
        bad.write_bytes(b"\xff\xfe not utf-8")

        good = tmp_path / "n-good.md"
        _write_note(good, {"id": "n-g", "tags": ["lesson"], "concepts": []})

        stats = cleanup_vault(tmp_path, apply=False)
        # The bad file is recorded as an error; the good file is still processed.
        assert any(p == bad for p, _msg in stats.errors)
        assert stats.tag_deletes.notes == 1
