"""Tests for the hub fold (synthesis/hub.py) + `mem hubs apply-linkage`.

The fold is the deterministic half of a hub merge: interleave two catalyst
logs, dedup shared citations, stamp ``fold_pending_*`` provenance, route
essence reconciliation to the essence worker. apply-linkage is the
validated write path the seam-link worker uses afterwards.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.synthesis.hub import (
    FOLD_PENDING_DATES_KEY,
    FOLD_PENDING_FROM_KEY,
    Hub,
    HubLogEntry,
    essence_is_placeholder,
    fold_hub_logs,
    merge_log_entries,
    replace_section_body,
    set_frontmatter_keys,
)


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


def _hub_file(path: Path, *, concept: str, essence: str, entries: list[str],
              extra_fm: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = "\n".join(entries)
    path.write_text(
        f"""---
type: concept-hub
concept: {concept}
{extra_fm}---
# {concept}

## Essence

{essence}

## Catalyst log

{log}
""",
        encoding="utf-8",
    )
    return path


class TestEssencePlaceholder:
    def test_placeholders(self):
        assert essence_is_placeholder("")
        assert essence_is_placeholder("*No synthesis yet.*")
        assert essence_is_placeholder("_Awaiting first synthesis pass._")
        assert not essence_is_placeholder("A real thesis about things.")

    def test_generic_skeleton_stub_is_placeholder(self):
        """The theme skeleton's italic instruction line counts as a stub."""
        from personal_mem.synthesis.theme_hub import render_theme_body_skeleton

        body = render_theme_body_skeleton("X")
        # Pull the ## Essence section text the skeleton writes.
        essence = body.split("## Essence\n\n", 1)[1].split("\n\n", 1)[0]
        assert essence.startswith("_") and essence.endswith("_")
        assert essence_is_placeholder(essence)

    def test_real_essence_opening_with_emphasis_not_flagged(self):
        """A genuine essence that merely starts/ends with emphasis survives."""
        # Multi-line paragraph wrapped in emphasis markers.
        assert not essence_is_placeholder(
            "*Regime shifts* dominate the framing here.\n"
            "The model treats drawdowns as state transitions, "
            "not noise — see the catalyst log for the pivots. *Updated often.*"
        )
        # Single-line but far longer than any system-written stub.
        assert not essence_is_placeholder(
            "_" + "A substantive working mental model sentence. " * 6 + "_"
        )

    def test_short_emphasis_wrapped_stub_variants_flagged(self):
        assert essence_is_placeholder("*No essence composed yet.*")
        assert essence_is_placeholder("_Placeholder — fill me in._")


class TestMergeLogEntries:
    def test_interleave_and_fold_dates(self):
        w = [HubLogEntry(date="2026-05-01", flag="new", text="a", citation="n-1")]
        l = [HubLogEntry(date="2026-04-20", flag="new", text="b", citation="n-2")]
        merged, fold_dates = merge_log_entries(w, l)
        assert [e.citation for e in merged] == ["n-2", "n-1"]
        assert fold_dates == ["2026-04-20"]

    def test_dedup_keeps_richer(self):
        w = [HubLogEntry(date="2026-05-01", flag="new", text="short", citation="n-1")]
        l = [
            HubLogEntry(
                date="2026-05-02",
                flag="extends",
                ref="2026-05-01",
                text="much longer distillation",
                citation="n-1",
            )
        ]
        merged, fold_dates = merge_log_entries(w, l)
        assert len(merged) == 1
        assert merged[0].flag == "extends"  # linked copy wins
        assert fold_dates == ["2026-05-02"]

    def test_dedup_winner_richer_no_fold_date(self):
        w = [
            HubLogEntry(
                date="2026-05-01", flag="agrees", ref="2026-04-01",
                text="rich winner copy", citation="n-1",
            )
        ]
        l = [HubLogEntry(date="2026-05-02", flag="new", text="x", citation="n-1")]
        merged, fold_dates = merge_log_entries(w, l)
        assert len(merged) == 1 and merged[0].flag == "agrees"
        assert fold_dates == []


class TestFoldHubLogs:
    def test_full_fold(self, config, vault):
        topics = config.vault_root / "concepts" / "topics"
        w = _hub_file(
            topics / "derivatives.md",
            concept="derivatives",
            essence="Financial derivatives thesis.",
            entries=[
                "- 2026-05-01 · *new* — Options pricing, the richer copy. — [[src-aaaa1111]]",
            ],
            extra_fm='essence_updated: "2026-06-01"\n',
        )
        l = _hub_file(
            topics / "derivative.md",
            concept="derivative",
            essence="Calculus thesis.",
            entries=[
                "- 2026-05-05 · *new* — Chain rule. — [[n-cccc3333]]",
                "- 2026-05-06 · *new* — Options copy. — [[src-aaaa1111]]",
            ],
        )
        stats = fold_hub_logs(w, l, loser_id="derivative")
        assert stats["deduped"] == 1
        assert stats["fold_dates"] == ["2026-05-05"]
        assert stats["essence_stashed"] is True

        hub = Hub.parse(w)
        assert {e.citation for e in hub.log} == {"src-aaaa1111", "n-cccc3333"}
        fm = hub.frontmatter
        assert fm[FOLD_PENDING_FROM_KEY] == "derivative"
        assert fm[FOLD_PENDING_DATES_KEY] == ["2026-05-05"]
        assert "essence_updated" not in fm
        assert "Calculus thesis." in hub.essence  # stashed in section

    def test_placeholder_winner_adopts_loser_essence(self, config, vault):
        topics = config.vault_root / "concepts" / "topics"
        w = _hub_file(
            topics / "a.md", concept="a", essence="*No synthesis yet.*",
            entries=["- 2026-05-01 · *new* — x. — [[n-1aaa1111]]"],
        )
        l = _hub_file(
            topics / "b.md", concept="b", essence="Real loser thesis.",
            entries=["- 2026-05-02 · *new* — y. — [[n-2bbb2222]]"],
        )
        stats = fold_hub_logs(w, l, loser_id="b")
        assert stats["essence_stashed"] is False
        hub = Hub.parse(w)
        assert hub.essence.strip() == "Real loser thesis."

    def test_refold_unions_dates(self, config, vault):
        topics = config.vault_root / "concepts" / "topics"
        w = _hub_file(
            topics / "a.md", concept="a", essence="t",
            entries=["- 2026-05-01 · *new* — x. — [[n-1aaa1111]]"],
            extra_fm="fold_pending_from: old\nfold_pending_dates: [2026-04-01]\n",
        )
        l = _hub_file(
            topics / "b.md", concept="b", essence="",
            entries=["- 2026-05-02 · *new* — y. — [[n-2bbb2222]]"],
        )
        fold_hub_logs(w, l, loser_id="b")
        fm, _ = parse_frontmatter(w.read_text(encoding="utf-8"))
        assert fm[FOLD_PENDING_DATES_KEY] == ["2026-04-01", "2026-05-02"]
        assert fm[FOLD_PENDING_FROM_KEY] == "old"

    def test_empty_loser_noop(self, config, vault):
        topics = config.vault_root / "concepts" / "topics"
        w = _hub_file(
            topics / "a.md", concept="a", essence="t",
            entries=["- 2026-05-01 · *new* — x. — [[n-1aaa1111]]"],
        )
        l = _hub_file(topics / "b.md", concept="b",
                      essence="*No synthesis yet.*", entries=[])
        before = w.read_text(encoding="utf-8")
        stats = fold_hub_logs(w, l, loser_id="b")
        assert stats["folded"] == 0
        assert w.read_text(encoding="utf-8") == before


class TestSectionHelpers:
    def test_replace_existing_section(self):
        body = "# t\n\n## Essence\n\nold\n\n## Catalyst log\n\n- x\n"
        out = replace_section_body(body, "## Essence", ["new text"])
        assert "new text" in out and "old" not in out
        assert "## Catalyst log" in out

    def test_missing_section_appended(self):
        out = replace_section_body("# t\n", "## Essence", ["e"])
        assert out.rstrip().endswith("e")

    def test_set_frontmatter_keys_roundtrip(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("---\na: 1\n---\nbody\n", encoding="utf-8")
        set_frontmatter_keys(p, {"merged-into": "x", "a": None})
        fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert fm == {"merged-into": "x"}
        assert "body" in body


class TestApplyLinkageCLI:
    def _setup_hub(self, config, vault) -> Path:
        topics = config.vault_root / "concepts" / "topics"
        hub = _hub_file(
            topics / "derivatives.md",
            concept="derivatives",
            essence="t",
            entries=[
                "- 2026-05-01 · *new* — Options pricing insight twenty chars plus. — [[src-aaaa1111]]",
                "- 2026-05-05 · *new* — Chain rule note. — [[n-cccc3333]]",
            ],
            extra_fm="fold_pending_from: derivative\nfold_pending_dates: [2026-05-05]\n",
        )
        idx = Indexer(config=config)
        idx.rebuild(full=True)
        idx.close()
        return hub

    def _run(self, config, hub, revisions, *, clear_fold=True, monkeypatch=None):
        from personal_mem.surfaces.cli._hubs_link import hubs_apply_linkage

        rev_path = config.vault_root / "rev.json"
        rev_path.write_text(json.dumps({"revisions": revisions}), encoding="utf-8")
        args = Namespace(
            hub="derivatives",
            kind="concept",
            revisions=str(rev_path),
            clear_fold=clear_fold,
            json=False,
        )
        with pytest.raises(SystemExit) as exc:
            hubs_apply_linkage(config, args)
        return exc.value.code

    def test_valid_revision_applies_and_clears_fold(self, config, vault):
        hub = self._setup_hub(config, vault)
        code = self._run(
            config,
            hub,
            [
                {
                    "date": "2026-05-05",
                    "citation": "n-cccc3333",
                    "flag": "extends",
                    "ref": "2026-05-01",
                    "ref_quote": "Options pricing insight twenty chars plus",
                }
            ],
        )
        assert code == 0
        parsed = Hub.parse(hub)
        entry = next(e for e in parsed.log if e.citation == "n-cccc3333")
        assert entry.flag == "extends" and entry.ref == "2026-05-01"
        assert FOLD_PENDING_FROM_KEY not in parsed.frontmatter
        assert FOLD_PENDING_DATES_KEY not in parsed.frontmatter

    def test_bad_quote_demotes_to_new(self, config, vault):
        hub = self._setup_hub(config, vault)
        code = self._run(
            config,
            hub,
            [
                {
                    "date": "2026-05-05",
                    "citation": "n-cccc3333",
                    "flag": "agrees",
                    "ref": "2026-05-01",
                    "ref_quote": "this quote appears nowhere in the cited entry",
                }
            ],
        )
        assert code == 0
        parsed = Hub.parse(hub)
        entry = next(e for e in parsed.log if e.citation == "n-cccc3333")
        assert entry.flag == "new" and entry.ref == ""

    def test_dequeues_seam_item(self, config, vault):
        from personal_mem.operations import seam_link_queue

        hub = self._setup_hub(config, vault)
        seam_link_queue.enqueue(
            config,
            hub_kind="concept",
            hub_id="derivatives",
            folded_from="derivative",
            fold_dates=["2026-05-05"],
        )
        self._run(config, hub, [])
        assert seam_link_queue.peek(config) == []
