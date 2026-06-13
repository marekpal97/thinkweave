"""Dream collectors read catalyst logs from ``hub_log_entries`` SQL.

Three scan collectors (`_collect_essence_candidates`,
`_collect_theme_log_gaps`, `_collect_knowledge_delta`) used to re-parse
every hub's indexed ``body_text`` per cycle; they now read the
``hub_log_entries`` projection (written by the same ``Indexer._index_file``
pass, so freshness is identical). These tests pin the substitution:
collector output computed from SQL must match a reference computed from the
old body-parse path on the same indexed rows.

Also covers the essence-stamp round-trip fix: stamping ``essence_updated``
is a targeted frontmatter line edit, leaving every other frontmatter byte
(YAML comments, empty keys, value formatting) untouched — the old
``parse_frontmatter`` → ``render_frontmatter`` round-trip dropped comments
and re-serialized every value.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from personal_mem.core.config import Config
from personal_mem.core.indexer import Indexer
from personal_mem.core.schemas import NoteType
from personal_mem.core.vault import VaultManager, parse_frontmatter
from personal_mem.operations.dream import (
    _collect_essence_candidates,
    _collect_knowledge_delta,
    _collect_theme_log_gaps,
    _rewrite_hub_essence,
    _set_frontmatter_line,
)


def _ref_log(body: str):
    """The pre-change reference path: parse the catalyst log from body text.

    Mirrors the deleted ``_parse_hub_body`` (canonical heading with legacy
    fallback, no path→id map) so equivalence is asserted against the exact
    logic the SQL reads replaced.
    """
    from personal_mem.synthesis.hub import (
        CATALYST_LOG_HEADING,
        LEGACY_LEARNING_LOG_HEADING,
        parse_log_section,
    )

    log = parse_log_section(body, CATALYST_LOG_HEADING)
    return log or parse_log_section(body, LEGACY_LEARNING_LOG_HEADING)


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


def _index(config: Config) -> None:
    idx = Indexer(config=config)
    idx.rebuild(full=True)
    idx.close()


def _make_active_theme(vm: VaultManager, title: str, *, concepts: list[str]) -> str:
    theme = vm.create_note(
        NoteType.THEME,
        title,
        body=(
            "## Essence\n\nA substantive essence paragraph.\n\n"
            "## Catalyst log\n\n## Open questions\n"
        ),
        extra_frontmatter={"concepts": concepts, "status": "active"},
    )
    fm, _ = parse_frontmatter(theme.read_text(encoding="utf-8"))
    return fm["id"]


def _theme_path(config: Config) -> Path:
    return next((config.vault_root / "themes").glob("*.md"))


def _add_catalyst(
    hub_path: Path,
    *,
    days_ago: int,
    flag: str = "new",
    text: str = "",
    citation: str = "n-abcd1234",
) -> str:
    """Append one catalyst entry; returns the entry date."""
    entry_date = (date.today() - timedelta(days=days_ago)).isoformat()
    body = text or f"Catalyst from {days_ago}d ago"
    cite = f" — [[{citation}]]" if citation else ""
    line = f"- {entry_date} · *{flag}* — {body}{cite}"
    content = hub_path.read_text(encoding="utf-8")
    hub_path.write_text(
        content.replace("## Catalyst log\n", f"## Catalyst log\n\n{line}\n", 1),
        encoding="utf-8",
    )
    return entry_date


def _write_concept_hub(config: Config, concept: str, *, entries: int) -> Path:
    """Placeholder-essence concept hub with ``entries`` catalyst lines."""
    topics = config.vault_root / "concepts" / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    p = topics / f"{concept}.md"
    p.write_text(
        f"---\ntype: concept-hub\nconcept: {concept}\n---\n\n"
        f"# {concept}\n\n## Essence\n\n*No synthesis yet.*\n\n"
        "## Catalyst log\n",
        encoding="utf-8",
    )
    for i in range(entries):
        # Distinct dates so ordering is unambiguous; last one uncited to
        # pin the cited_note_id NULL → "" coercion.
        _add_catalyst(
            p,
            days_ago=i + 1,
            citation="" if i == entries - 1 else f"n-cc{i:02d}cc{i:02d}",
        )
    return p


def _body_text(config: Config, *, note_id: str = "", path_like: str = "") -> str:
    idx = Indexer(config=config)
    try:
        if note_id:
            row = idx.db.execute(
                "SELECT body_text FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
        else:
            row = idx.db.execute(
                "SELECT body_text FROM notes WHERE path LIKE ?", (path_like,)
            ).fetchone()
    finally:
        idx.close()
    return row["body_text"] or ""


def _note_id_for_path(config: Config, path_like: str) -> str:
    idx = Indexer(config=config)
    try:
        row = idx.db.execute(
            "SELECT id FROM notes WHERE path LIKE ?", (path_like,)
        ).fetchone()
    finally:
        idx.close()
    return row["id"]


# ---------------------------------------------------------------------------
# Collector equivalence — SQL projection vs the old body-parse path
# ---------------------------------------------------------------------------


class TestEssenceCandidatesFromSql:
    def test_theme_catalysts_match_body_parse(
        self, config: Config, vault: VaultManager
    ):
        theme_id = _make_active_theme(vault, "AI capex", concepts=["ai-capex"])
        tp = _theme_path(config)
        _add_catalyst(tp, days_ago=0, citation="n-aaaa1111")
        _add_catalyst(tp, days_ago=5, flag="contradicts", citation="n-bbbb2222")
        _add_catalyst(tp, days_ago=40, citation="")  # old + uncited
        _index(config)

        cands = _collect_essence_candidates(config)
        match = [c for c in cands if c.get("theme_id") == theme_id]
        assert len(match) == 1
        c = match[0]

        # Reference: the pre-change path — parse the same indexed body_text.
        log_ref = _ref_log(_body_text(config, note_id=theme_id))
        ref_sorted = sorted(log_ref, key=lambda e: e.date, reverse=True)
        expected = [
            {"date": e.date, "flag": e.flag, "text": e.text, "citation": e.citation}
            for e in ref_sorted[:10]
        ]
        assert c["recent_catalysts"] == expected
        assert c["total_catalysts"] == len(log_ref) == 3
        assert c["catalysts_since_essence"] == 3  # no stamp → all count
        assert c["recent_contradicts"] == 1
        assert c["last_catalyst_date"] == date.today().isoformat()
        # Uncited entry comes back as "" (parity with HubLogEntry default),
        # never None from the SQL NULL.
        assert c["recent_catalysts"][-1]["citation"] == ""

    def test_concept_hub_catalysts_match_body_parse(
        self, config: Config, vault: VaultManager
    ):
        hub = _write_concept_hub(config, "agentic-ai", entries=5)
        _index(config)

        cands = _collect_essence_candidates(config)
        match = [c for c in cands if c.get("concept") == "agentic-ai"]
        assert len(match) == 1
        c = match[0]
        assert c["hub_kind"] == "concept"
        assert c["essence_is_placeholder"] is True

        log_ref = _ref_log(
            _body_text(config, path_like="concepts/topics/agentic-ai.md")
        )
        ref_sorted = sorted(log_ref, key=lambda e: e.date, reverse=True)
        expected = [
            {"date": e.date, "flag": e.flag, "text": e.text, "citation": e.citation}
            for e in ref_sorted[:25]  # placeholder_max_catalysts
        ]
        assert c["recent_catalysts"] == expected
        assert c["total_catalysts"] == 5
        assert hub.exists()

    def test_essence_updated_stamp_still_gates_count(
        self, config: Config, vault: VaultManager
    ):
        """`catalysts_since_essence` compares SQL entry dates to the stamp."""
        theme_id = _make_active_theme(vault, "Rates arc", concepts=["rates"])
        tp = _theme_path(config)
        _add_catalyst(tp, days_ago=0)
        _add_catalyst(tp, days_ago=20)
        # Stamp between the two entries: only the newer one counts.
        stamp = (date.today() - timedelta(days=10)).isoformat()
        text = tp.read_text(encoding="utf-8")
        tp.write_text(
            text.replace("---\n", f"---\nessence_updated: {stamp}\n", 1),
            encoding="utf-8",
        )
        _index(config)

        cands = _collect_essence_candidates(config)
        c = next(x for x in cands if x.get("theme_id") == theme_id)
        assert c["essence_updated"] == stamp
        assert c["catalysts_since_essence"] == 1


class TestThemeLogGapsFromSql:
    def test_logged_citations_come_from_sql(
        self, config: Config, vault: VaultManager
    ):
        theme_id = _make_active_theme(vault, "bond-vigilantes", concepts=["rates"])

        def _src(title: str) -> str:
            p = vault.create_note(
                NoteType.SOURCE,
                title,
                body=f"# {title}\n\nbody text\n",
                extra_frontmatter={
                    "source_type": "news",
                    "concepts": ["rates", "bonds"],
                    "relates_to": [theme_id],
                },
            )
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
            return fm["id"]

        logged_id = _src("Already logged")
        missing_id = _src("Never logged")
        # Cite the first source in the theme's catalyst log.
        _add_catalyst(_theme_path(config), days_ago=1, citation=logged_id)
        _index(config)

        gaps = _collect_theme_log_gaps(config)
        match = [g for g in gaps if g["theme_id"] == theme_id]
        assert len(match) == 1
        ids = [s["id"] for s in match[0]["sources"]]
        assert missing_id in ids
        assert logged_id not in ids  # SQL-projected citation excludes it

    def test_fully_logged_theme_has_no_gap(
        self, config: Config, vault: VaultManager
    ):
        theme_id = _make_active_theme(vault, "fully-logged", concepts=["rates"])
        p = vault.create_note(
            NoteType.SOURCE,
            "Covered source",
            body="# Covered source\n\nbody\n",
            extra_frontmatter={
                "source_type": "news",
                "concepts": ["rates", "bonds"],
                "relates_to": [theme_id],
            },
        )
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        _add_catalyst(_theme_path(config), days_ago=0, citation=fm["id"])
        _index(config)

        gaps = _collect_theme_log_gaps(config)
        assert [g for g in gaps if g["theme_id"] == theme_id] == []


class TestKnowledgeDeltaFromSql:
    def test_catalyst_additions_window_split_and_identity(
        self, config: Config, vault: VaultManager
    ):
        theme_id = _make_active_theme(vault, "AI capex", concepts=["ai-capex"])
        tp = _theme_path(config)
        _add_catalyst(tp, days_ago=0, citation="n-aaaa1111")
        _add_catalyst(tp, days_ago=10, citation="n-bbbb2222")  # outside 24h
        _write_concept_hub(config, "agentic-ai", entries=0)
        hub_path = config.vault_root / "concepts" / "topics" / "agentic-ai.md"
        _add_catalyst(hub_path, days_ago=0, citation="")  # in-window, uncited
        _index(config)

        delta = _collect_knowledge_delta(config)

        event_adds = delta["event"]["catalyst_additions_24h"]
        assert [a["hub"] for a in event_adds] == [theme_id]
        assert event_adds[0]["hub_kind"] == "theme"
        assert event_adds[0]["line_date"] == date.today().isoformat()
        assert event_adds[0]["flag"] == "new"
        assert event_adds[0]["cited_note_id"] == "n-aaaa1111"
        # The 10d-old entry never crosses the day-level cutoff.
        assert all(a["cited_note_id"] != "n-bbbb2222" for a in event_adds)

        concept_adds = delta["concept"]["catalyst_additions_24h"]
        assert len(concept_adds) == 1
        # "hub" is the hub note's index id (not the vocabulary term) —
        # exactly what the body-parse path emitted.
        hub_note_id = _note_id_for_path(config, "concepts/topics/agentic-ai.md")
        assert concept_adds[0]["hub"] == hub_note_id
        assert concept_adds[0]["hub_kind"] == "concept"
        # Uncited entry: "" (HubLogEntry parity), not SQL NULL/None.
        assert concept_adds[0]["cited_note_id"] == ""


# ---------------------------------------------------------------------------
# Essence stamp — targeted frontmatter line edit
# ---------------------------------------------------------------------------


FUNKY_FM = [
    "---",
    "type: theme",
    "id: thm-aaaa1111",
    "# hand-written comment render_frontmatter would have dropped",
    "status: active",
    "empty_key:",
    "weird:   'oddly   spaced  value'",
    "concepts:",
    "  - ai-capex",
    "---",
]

HUB_BODY = (
    "\n# AI capex\n\n## Essence\n\nold essence\n\n"
    "## Catalyst log\n\n- 2026-06-01 · *new* — x — [[n-aaaa1111]]\n"
)


class TestEssenceStampRoundTrip:
    def test_stamp_preserves_unrelated_frontmatter_bytes(self, tmp_path: Path):
        p = tmp_path / "thm-aaaa1111-ai-capex.md"
        p.write_text("\n".join(FUNKY_FM) + HUB_BODY, encoding="utf-8")

        _rewrite_hub_essence(p, "Fresh essence.")

        lines = p.read_text(encoding="utf-8").split("\n")
        close = lines.index("---", 1)
        fm_lines = lines[: close + 1]
        stamp_lines = [l for l in fm_lines if l.startswith("essence_updated:")]
        assert stamp_lines == [f"essence_updated: {date.today().isoformat()}"]
        # Every other frontmatter byte — comment, empty key, quoting,
        # spacing, ordering — is exactly as authored.
        assert [l for l in fm_lines if not l.startswith("essence_updated:")] == FUNKY_FM
        # The essence rewrite itself still landed, log untouched.
        text = "\n".join(lines)
        assert "Fresh essence." in text
        assert "old essence" not in text
        assert "- 2026-06-01 · *new* — x — [[n-aaaa1111]]" in text

    def test_existing_stamp_replaced_in_place(self, tmp_path: Path):
        fm = FUNKY_FM[:5] + ["essence_updated: 2020-01-01"] + FUNKY_FM[5:]
        p = tmp_path / "thm-bbbb2222-rates.md"
        p.write_text("\n".join(fm) + HUB_BODY, encoding="utf-8")

        _rewrite_hub_essence(p, "Even fresher.")

        lines = p.read_text(encoding="utf-8").split("\n")
        close = lines.index("---", 1)
        fm_after = lines[: close + 1]
        # Same line count, same position — replaced, not duplicated.
        assert len(fm_after) == len(fm)
        assert fm_after[5] == f"essence_updated: {date.today().isoformat()}"
        assert [l for i, l in enumerate(fm_after) if i != 5] == [
            l for i, l in enumerate(fm) if i != 5
        ]

    def test_set_frontmatter_line_no_frontmatter_is_noop(self):
        text = "# Just a body\n\nno fences here\n"
        assert _set_frontmatter_line(text, "essence_updated", "2026-06-13") == text

    def test_set_frontmatter_line_ignores_nested_key(self):
        text = (
            "---\n"
            "type: theme\n"
            "meta:\n"
            "  essence_updated: never\n"
            "---\n"
            "body\n"
        )
        out = _set_frontmatter_line(text, "essence_updated", "2026-06-13")
        # Nested key untouched; top-level line inserted before the close.
        assert "  essence_updated: never\n" in out
        assert out.split("\n")[4] == "essence_updated: 2026-06-13"
        assert out.endswith("---\nbody\n")
