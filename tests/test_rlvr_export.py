"""Tests for ``operations/rlvr_export.py`` — slice 5 of the RLVR substrate.

Three layers of coverage:

- ``extract_cited_ids`` — pure body-text scan, with negatives (titles aren't
  ids) and positives across every supported prefix.
- ``assemble_row`` — joins decision frontmatter + body + context_served into
  the locked schema. Tests seed a vault that looks like a real post-extract
  state (decision in a session folder with sibling retrieval_log.jsonl).
- ``export_rows`` — batch iteration, filters, ordering.

The schema this enforces comes from ``project_decision_context_rl`` and is
load-bearing for any downstream RL training step. If you need to change a
field name, change the memory note in the same PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager
from personal_mem.operations.rlvr_export import (
    assemble_row,
    export_rows,
    extract_cited_ids,
)


# ---------------------------------------------------------------------------
# extract_cited_ids — body-only, canonical-id-only
# ---------------------------------------------------------------------------


class TestExtractCitedIds:
    def test_canonical_ids_match(self):
        body = (
            "Background from [[n-aaa111aa]] and [[dec-bbb222bb]].\n"
            "Later [[ses-ccc333cc]] confirmed it; also [[thm-ddd444dd]].\n"
        )
        assert extract_cited_ids(body) == [
            "n-aaa111aa", "dec-bbb222bb", "ses-ccc333cc", "thm-ddd444dd"
        ]

    def test_title_wikilinks_are_not_citations(self):
        # Free-form titles must not match — only canonical-id targets count.
        body = "See [[Some Page Title]] and [[Another Doc]] and [[n-aaa111aa]]."
        assert extract_cited_ids(body) == ["n-aaa111aa"]

    def test_alias_form_is_stripped(self):
        # [[target|display]] — extract_wikilinks already returns target only.
        body = "See [[n-aaa111aa|the indexer note]] for details."
        assert extract_cited_ids(body) == ["n-aaa111aa"]

    def test_dedup_preserves_first_order(self):
        body = "[[n-aaa111aa]] then [[dec-bbb222bb]] then [[n-aaa111aa]] again"
        assert extract_cited_ids(body) == ["n-aaa111aa", "dec-bbb222bb"]

    def test_empty_body_returns_empty_list(self):
        assert extract_cited_ids("") == []
        assert extract_cited_ids("Just prose, no links here.") == []

    def test_does_not_match_substring_in_title(self):
        # The id must BE the wikilink target, not just be a substring.
        body = "[[Look at n-aaa111aa in the docs]]"
        assert extract_cited_ids(body) == []


# ---------------------------------------------------------------------------
# Vault fixture helpers — build a "just-extracted" session + decision setup
# ---------------------------------------------------------------------------


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


def _seed(
    vault: VaultManager,
    *,
    project: str = "t",
    dec_body: str = "## Context\n\n## Decision\n",
    extra_dec_fm: dict | None = None,
    log_lines: list[dict] | None = None,
) -> tuple[str, str, Path]:
    """Build a session note, a decision in its folder, and an optional retrieval log.

    Returns ``(session_id, decision_id, session_dir)``.
    """
    sess_path = vault.create_note(
        NoteType.SESSION,
        "S",
        body="## Summary\nseed\n",
        project=project,
        extra_frontmatter={"processed": True},
    )
    session_id = vault.read_note(sess_path).id

    dec_fm = {
        "status": "accepted",
        "committed": True,
        "source_session": session_id,
        "derived_from": [session_id],
        "concepts": ["a", "b"],
    }
    if extra_dec_fm:
        dec_fm.update(extra_dec_fm)
    dec_path = vault.create_note(
        NoteType.DECISION,
        "D",
        body=dec_body,
        project=project,
        extra_frontmatter=dec_fm,
        output_dir=sess_path.parent,
    )
    decision_id = vault.read_note(dec_path).id

    if log_lines is not None:
        (sess_path.parent / "retrieval_log.jsonl").write_text(
            "\n".join(json.dumps(line) for line in log_lines) + "\n",
            encoding="utf-8",
        )
    return session_id, decision_id, sess_path.parent


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


# ---------------------------------------------------------------------------
# assemble_row — happy path + edge cases
# ---------------------------------------------------------------------------


class TestAssembleRow:
    def test_basic_row_shape(self, config: Config, vault: VaultManager):
        sess_id, dec_id, _ = _seed(vault)
        _index(config)
        row = assemble_row(config, dec_id)
        assert row is not None
        d = row.as_dict()
        # Top-level keys match the locked schema.
        assert set(d.keys()) == {
            "decision_id", "project", "session_id", "created_at",
            "prediction", "outcome", "context",
        }
        assert d["decision_id"] == dec_id
        assert d["session_id"] == sess_id
        assert d["project"] == "t"
        # Sub-dicts have the expected shape too.
        assert set(d["prediction"].keys()) == {"text", "match"}
        assert set(d["outcome"].keys()) == {
            "verdict", "committed", "blame_lines", "days_alive",
        }
        assert set(d["context"].keys()) == {
            "n_retrievals_onthefly", "cited_onthefly_ids",
            "cited_startup_only_ids", "startup_token_est",
        }

    def test_cited_ids_bucketed_correctly(self, config: Config, vault: VaultManager):
        # Decision cites three notes: one served on-the-fly, one served via
        # startup only, one not served at all (the last shouldn't appear in
        # either bucket).
        body = (
            "## Context\n\n"
            "Based on [[n-onthefly1]] (fetched mid-session) and "
            "[[n-startup12]] (visible at startup) "
            "and [[n-never9999]] (not in any retrieval).\n\n"
            "## Decision\n"
        )
        _, dec_id, _ = _seed(
            vault,
            dec_body=body,
            log_lines=[
                {"ts": "t1", "type": "startup",
                 "returned_ids": ["n-startup12", "n-other999"],
                 "token_est": 1234},
                {"ts": "t2", "type": "retrieval",
                 "returned_ids": ["n-onthefly1"]},
            ],
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()

        assert row["context"]["cited_onthefly_ids"] == ["n-onthefly1"]
        assert row["context"]["cited_startup_only_ids"] == ["n-startup12"]
        # n-never9999 is in the body but in no context_served row → not bucketed.
        assert "n-never9999" not in row["context"]["cited_onthefly_ids"]
        assert "n-never9999" not in row["context"]["cited_startup_only_ids"]

    def test_note_in_both_sources_counts_as_onthefly(
        self, config: Config, vault: VaultManager
    ):
        # Per memory note: a note served via startup AND fetched on the fly
        # counts as onthefly, not startup-only.
        body = "Context from [[n-everywhere]]."
        _, dec_id, _ = _seed(
            vault,
            dec_body=body,
            log_lines=[
                {"ts": "t1", "type": "startup",
                 "returned_ids": ["n-everywhere"], "token_est": 100},
                {"ts": "t2", "type": "retrieval",
                 "returned_ids": ["n-everywhere"]},
            ],
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["context"]["cited_onthefly_ids"] == ["n-everywhere"]
        assert row["context"]["cited_startup_only_ids"] == []

    def test_n_retrievals_counts_events_not_notes(
        self, config: Config, vault: VaultManager
    ):
        # Each retrieval event with N returned notes is still ONE event.
        # Count = DISTINCT ts among onthefly rows.
        _, dec_id, _ = _seed(
            vault,
            log_lines=[
                {"ts": "t1", "type": "retrieval",
                 "returned_ids": ["n-aaa111aa", "n-bbb222bb", "n-ccc333cc"]},
                {"ts": "t2", "type": "retrieval",
                 "returned_ids": ["n-ddd444dd"]},
                # Startup event doesn't count toward onthefly.
                {"ts": "t3", "type": "startup",
                 "returned_ids": ["n-eee555ee"], "token_est": 0},
            ],
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["context"]["n_retrievals_onthefly"] == 2

    def test_startup_token_est_pulled_from_log(
        self, config: Config, vault: VaultManager
    ):
        _, dec_id, _ = _seed(
            vault,
            log_lines=[
                {"ts": "t1", "type": "startup",
                 "returned_ids": ["n-aaaabbbb"], "token_est": 9876},
            ],
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["context"]["startup_token_est"] == 9876

    def test_session_without_retrieval_log_is_graceful(
        self, config: Config, vault: VaultManager
    ):
        # Decisions from sessions captured before the RLVR slices landed:
        # no retrieval_log.jsonl, but the row should still be assembled with
        # zeros / empty lists in context.
        _, dec_id, _ = _seed(
            vault,
            dec_body="Context from [[n-aaa111aa]].",
            log_lines=None,  # no retrieval_log.jsonl created
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        ctx = row["context"]
        assert ctx["n_retrievals_onthefly"] == 0
        assert ctx["cited_onthefly_ids"] == []
        assert ctx["cited_startup_only_ids"] == []
        assert ctx["startup_token_est"] == 0

    def test_predicted_outcome_and_match_flow_through(
        self, config: Config, vault: VaultManager
    ):
        _, dec_id, _ = _seed(
            vault,
            extra_dec_fm={
                "predicted_outcome": "this will land in one commit",
                "prediction_match": "confirmed",
            },
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["prediction"] == {
            "text": "this will land in one commit",
            "match": "confirmed",
        }

    def test_outcome_fields_from_frontmatter(
        self, config: Config, vault: VaultManager
    ):
        _, dec_id, _ = _seed(
            vault,
            extra_dec_fm={
                "verdict": "kept",
                "blame_lines": 42,
                "judged_at": "2026-05-13T00:00:00Z",
                "date": "2026-05-10",
            },
        )
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["outcome"]["verdict"] == "kept"
        assert row["outcome"]["committed"] is True
        assert row["outcome"]["blame_lines"] == 42
        # judged_at - created_at = 3 days
        assert row["outcome"]["days_alive"] == 3

    def test_blame_lines_minus_one_when_missing(
        self, config: Config, vault: VaultManager
    ):
        # The judge writes -1 when blame can't be determined — preserve it
        # through to the row as the same sentinel.
        _, dec_id, _ = _seed(vault)
        _index(config)
        row = assemble_row(config, dec_id).as_dict()
        assert row["outcome"]["blame_lines"] == -1

    def test_missing_decision_returns_none(
        self, config: Config, vault: VaultManager
    ):
        _seed(vault)
        _index(config)
        assert assemble_row(config, "dec-nonexistent") is None


# ---------------------------------------------------------------------------
# export_rows — batch path + filters
# ---------------------------------------------------------------------------


def _seed_n_decisions(
    vault: VaultManager, n: int, *, project: str = "t",
    committed: bool = True,
) -> list[str]:
    """Seed N independent (session, decision) pairs in the given project."""
    ids: list[str] = []
    for i in range(n):
        sess_path = vault.create_note(
            NoteType.SESSION, f"S{i}", body="## Summary\n", project=project,
        )
        sess_id = vault.read_note(sess_path).id
        dec_path = vault.create_note(
            NoteType.DECISION, f"D{i}",
            body="## Context\n\n## Decision\n",
            project=project,
            extra_frontmatter={
                "status": "accepted",
                "committed": committed,
                "source_session": sess_id,
                "derived_from": [sess_id],
                "concepts": ["a", "b"],
                "date": f"2026-05-{10 + i:02d}",
            },
            output_dir=sess_path.parent,
        )
        ids.append(vault.read_note(dec_path).id)
    return ids


class TestExportRows:
    def test_emits_one_row_per_decision(
        self, config: Config, vault: VaultManager
    ):
        _seed_n_decisions(vault, 3)
        _index(config)
        rows = list(export_rows(config))
        assert len(rows) == 3

    def test_project_filter(self, config: Config, vault: VaultManager):
        _seed_n_decisions(vault, 2, project="alpha")
        _seed_n_decisions(vault, 1, project="beta")
        _index(config)
        alpha = list(export_rows(config, project="alpha"))
        beta = list(export_rows(config, project="beta"))
        assert len(alpha) == 2
        assert len(beta) == 1
        assert all(r["project"] == "alpha" for r in alpha)

    def test_committed_only_filter(
        self, config: Config, vault: VaultManager
    ):
        _seed_n_decisions(vault, 2, committed=True)
        _seed_n_decisions(vault, 1, committed=False)
        _index(config)
        all_rows = list(export_rows(config))
        committed_rows = list(export_rows(config, committed_only=True))
        assert len(all_rows) == 3
        assert len(committed_rows) == 2

    def test_date_window(self, config: Config, vault: VaultManager):
        _seed_n_decisions(vault, 4)
        _index(config)
        windowed = list(export_rows(
            config, since="2026-05-11", until="2026-05-12"
        ))
        assert len(windowed) == 2
        dates = sorted(r["created_at"] for r in windowed)
        assert dates == ["2026-05-11", "2026-05-12"]

    def test_deterministic_order(self, config: Config, vault: VaultManager):
        _seed_n_decisions(vault, 5)
        _index(config)
        rows1 = [r["decision_id"] for r in export_rows(config)]
        rows2 = [r["decision_id"] for r in export_rows(config)]
        assert rows1 == rows2
        # Order by date — the seed creates them with monotonically increasing
        # dates, so the IDs (which include the date) should sort the same way.
        dates = [r["created_at"] for r in export_rows(config)]
        assert dates == sorted(dates)
