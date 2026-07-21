"""CLI-surface execution tests — the argv → parser → dispatch → handler → stdout
vertical slice for the read/derive command cluster.

Most ``weave`` subcommands wrap a well-tested operation, but the *wiring*
between argparse and the handler had no execution coverage: the existing
``test_cli_parity`` hand-builds ``argparse.Namespace(...)`` objects, which can
silently drift from what the parser actually produces (a renamed ``dest``, a
changed default, a positional that became a subparser). These tests instead
drive the real entry point — ``thinkweave.surfaces.cli.main(argv)`` — so the
parser and the handler are pinned *together*. If a subcommand's argparse
definition and its ``cmd_*`` handler disagree, one of these fails.

Scope is deliberately the safe read/derive surface (search / context / graph /
show / backlog / decisions / stats / doctor / index / concepts / queue /
sources / themes / landing / project). Network- or LLM-driven verbs (drain,
discover, import, dream, seam) are covered by their own operation-level tests;
the surface-contract test pins that every subcommand *resolves* to a handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.surfaces.cli import main


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A small indexed vault wired via ``THINKWEAVE_VAULT`` so the handlers'
    own ``load_config()`` resolves it — nothing is passed to the handler that
    the real CLI wouldn't pass."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    monkeypatch.delenv("PERSONAL_MEM_VAULT", raising=False)
    cfg = Config(vault_root=vault, default_project="t")
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()

    ids: dict[str, str] = {}
    note = vm.create_note(
        NoteType.NOTE,
        "Widget architecture",
        body="Vector search design and the embeddings index.",
        project="t",
        extra_frontmatter={"concepts": ["embeddings"]},
    )
    ids["note"] = parse_frontmatter(note.read_text(encoding="utf-8"))[0]["id"]

    vm.create_note(
        NoteType.NOTE, "Follow up on caching", project="t", tags=["todo"]
    )
    dec = vm.create_note(
        NoteType.DECISION,
        "Chose SQLite for the derived index",
        body="Markdown stays truth; SQLite is rebuildable.",
        project="t",
        extra_frontmatter={
            # The indexer harvests the plural ``file_paths`` list into the
            # decision_files table; ``weave decisions --file`` matches against
            # it. (The singular query arg matches a member of the list.)
            "file_paths": ["src/thinkweave/core/indexer.py"],
            "status": "accepted",
            "predicted_outcome": "Rebuilds stay under a second.",
        },
    )
    ids["decision"] = parse_frontmatter(dec.read_text(encoding="utf-8"))[0]["id"]
    vm.create_note(NoteType.SESSION, "Recent session", project="t")
    vm.create_note(
        NoteType.THEME,
        "Retrieval arc",
        body="## Essence\n\nArc.\n\n## Catalyst log\n\n## Open questions\n",
        extra_frontmatter={"status": "active", "concepts": ["embeddings"]},
    )

    Indexer(config=cfg).rebuild(full=True)
    return cfg, ids


# ---------------------------------------------------------------------------
# Retrieval-debug surface (mirrors the MCP read tools)
# ---------------------------------------------------------------------------


class TestSearchSurface:
    def test_keyword_query_finds_seeded_note(self, seeded, capsys):
        main(["search", "Widget", "--project", "t"])
        assert "Widget architecture" in capsys.readouterr().out

    def test_empty_query_is_list_mode(self, seeded, capsys):
        main(["search", "--project", "t", "--type", "note"])
        assert "Widget architecture" in capsys.readouterr().out

    def test_concept_filter(self, seeded, capsys):
        main(["search", "--concept", "embeddings", "--project", "t"])
        assert "Widget architecture" in capsys.readouterr().out

    def test_explicit_fts_mode(self, seeded, capsys):
        main(["search", "vector", "--mode", "fts", "--project", "t"])
        assert "Widget architecture" in capsys.readouterr().out


class TestShowSurface:
    def test_prints_note(self, seeded, capsys):
        _, ids = seeded
        main(["show", ids["note"]])
        assert "Widget architecture" in capsys.readouterr().out

    def test_missing_id_exits_nonzero(self, seeded, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["show", "n-doesnotexist"])
        assert exc.value.code != 0


class TestGraphSurface:
    def test_walk_from_center(self, seeded, capsys):
        _, ids = seeded
        main(["graph", ids["note"]])
        # The center note's title anchors the walk output.
        assert "Widget architecture" in capsys.readouterr().out


class TestContextSurface:
    def test_budgeted_blob(self, seeded, capsys):
        main(["context", "--query", "embeddings", "--project", "t"])
        # Composition returns *something* for a matching query.
        assert capsys.readouterr().out.strip() != ""


class TestBacklogSurface:
    def test_lists_todo_note(self, seeded, capsys):
        main(["backlog", "--project", "t"])
        assert "caching" in capsys.readouterr().out.lower()


class TestDecisionsSurface:
    def test_file_filter_finds_decision(self, seeded, capsys):
        main(["decisions", "--file", "src/thinkweave/core/indexer.py"])
        assert "SQLite" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Admin / derive surface
# ---------------------------------------------------------------------------


class TestVaultHealthSurface:
    def test_stats_runs(self, seeded, capsys):
        main(["stats"])
        # Deprecated shim still prints its banner + counts.
        assert capsys.readouterr().out.strip() != ""

    def test_doctor_readonly_runs(self, seeded, capsys):
        main(["doctor"])
        assert capsys.readouterr().out.strip() != ""

    def test_index_full_rebuild(self, seeded, capsys):
        main(["index", "--full"])
        assert capsys.readouterr().out.strip() != ""


class TestConceptsSurface:
    def test_list_shows_seeded_concept(self, seeded, capsys):
        main(["concepts", "list"])
        assert "embeddings" in capsys.readouterr().out


class TestQueueSurface:
    def test_list_runs(self, seeded, capsys):
        main(["queue", "list"])
        assert capsys.readouterr().out.strip() != ""


class TestSourcesSurface:
    def test_list_runs(self, seeded, capsys):
        main(["sources", "list"])
        assert capsys.readouterr().out.strip() != ""


class TestThemesSurface:
    def test_rebuild_registry_runs(self, seeded, capsys):
        main(["themes", "rebuild-registry"])
        # Registry file materialises from the canonical theme markdown.
        cfg, _ = seeded
        assert (cfg.vault_root / "themes" / "themes.yaml").exists() or (
            capsys.readouterr().out.strip() != ""
        )


class TestLandingSurface:
    def test_state_doc_generates(self, seeded, capsys):
        main(["landing", "--doc", "state", "--project", "t"])
        assert capsys.readouterr().out.strip() != ""


class TestProjectSnapshotSurface:
    def test_snapshot_emits(self, seeded, capsys):
        main(["project", "t"])
        assert capsys.readouterr().out.strip() != ""


# ---------------------------------------------------------------------------
# Entry-point contract
# ---------------------------------------------------------------------------


class TestNoCommand:
    def test_bare_invocation_prints_help_and_exits_one(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 1


class TestHelpFormatting:
    """argparse renders help via ``help % params``; a literal ``%`` that
    isn't doubled (e.g. ``%APPDATA%``) makes ``print_help()`` raise
    ValueError at runtime — which broke bare ``weave`` / ``weave --help``.
    Format every subparser's help so any unescaped ``%`` is caught here."""

    def test_every_subparser_help_formats(self):
        import argparse

        from thinkweave.surfaces.cli import build_parser

        parser = build_parser()
        # Top-level help must format.
        parser.format_help()
        sub = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        for name, subparser in sub.choices.items():
            # Raises ValueError on an unescaped ``%`` in any help string.
            subparser.format_help()


def _registered_subcommands() -> list[str]:
    import argparse

    from thinkweave.surfaces.cli import build_parser

    parser = build_parser()
    sub = next(
        a for a in parser._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    return sorted(sub.choices)


class TestHelpExitCode:
    """Regression seam for #51: ``weave --help`` crashed with ValueError
    (unescaped ``%`` rendered via argparse's ``help % params``) instead of
    printing help. Drive the real entry point with ``--help`` argv — argparse's
    help action must print and raise ``SystemExit(0)``; any unescaped ``%`` in
    a reachable help string surfaces here as a ValueError instead."""

    @staticmethod
    def _assert_help_exits_zero(argv: list[str], capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0, (
            f"`weave {' '.join(argv)}` exited {exc.value.code}"
        )
        assert capsys.readouterr().out.strip() != ""

    def test_top_level_help_exits_zero(self, capsys):
        self._assert_help_exits_zero(["--help"], capsys)

    @pytest.mark.parametrize("name", _registered_subcommands())
    def test_subcommand_help_exits_zero(self, name: str, capsys):
        self._assert_help_exits_zero([name, "--help"], capsys)
