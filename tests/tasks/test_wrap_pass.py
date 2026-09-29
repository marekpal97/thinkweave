"""The wrap task pass (#189): fixture-driven declaration reconciliation.

/wrap is the reconciler, never the collector — the pass runs inside
``weave wrap-finalize`` on a declaration file the wrap LLM composed
(canned here; no model call). It mints the solo lane's declared boundary,
appends one round per segment to open work-grain notes, writes consumes
edges, flags orphans at boundary sparsity, re-parents seam children by
interval containment from the session chain root, and stamps minted
decisions with the task id. It never fills ``outcome`` and never merges a
continuation silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import (
    TASK_ID_RE,
    validate_task_note,
    validate_wrap_declaration,
)
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations import hook_events, task_seam
from thinkweave.operations.wrap import finalize_wrap
from thinkweave.surfaces.cli.parser import build_parser
from thinkweave.surfaces.cli.wrap import cmd_wrap_finalize

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = "s-1"


def load_declaration() -> dict:
    return json.loads(
        (FIXTURES / "wrap-declaration.json").read_text(encoding="utf-8")
    )


def stream(cfg: Config) -> Path:
    return hook_events.register_path(cfg.weave_dir, SESSION)


def reconcile(cfg: Config, declaration: dict, folders: list[Path] | None = None):
    return task_seam.reconcile_tasks(
        cfg,
        declaration,
        session_key=SESSION,
        project="t",
        streams=[stream(cfg)],
        folders=folders or [],
    )


def task_notes(cfg: Config) -> dict[str, dict]:
    """{task_id: frontmatter} for every task stub in the vault."""
    out: dict[str, dict] = {}
    for path in cfg.vault_root.rglob("tsk-*.md"):
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        out[fm["id"]] = fm
    return out


def seed_stub(cfg: Config, task_id: str, *, grain: str, consumes=None, parent=""):
    fm: dict = {
        "kind": "task",
        "status": "open",
        "grain": grain,
        "rounds": [],
        "title": task_id,
    }
    if consumes:
        fm["consumes"] = list(consumes)
    if parent:
        fm["parent"] = parent
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    vm.create_note(
        NoteType.NOTE, title=task_id, project="t",
        extra_frontmatter=fm, note_id=task_id,
    )


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Declaration shape — declared contract, rejected loudly


class TestDeclarationShape:
    def test_canned_fixture_validates_clean(self):
        assert validate_wrap_declaration(load_declaration()) == []

    def test_rejects_unknown_keys_and_bad_sparsity(self):
        assert validate_wrap_declaration({"declared": [], "extra": 1})
        assert validate_wrap_declaration({"sparsity": "full", "declared": []})

    def test_rejects_malformed_continuing_id(self):
        decl = {"declared": [{"continuing": "not-a-task-id"}]}
        assert any("continuing" in e for e in validate_wrap_declaration(decl))

    def test_rejects_two_roots(self):
        decl = {
            "declared": [
                {"title": "a", "root": True},
                {"title": "b", "root": True},
            ]
        }
        assert any("root" in e for e in validate_wrap_declaration(decl))

    def test_rejects_mint_without_title(self):
        assert any(
            "title" in e
            for e in validate_wrap_declaration({"declared": [{"asked": "#1"}]})
        )

    def test_round_entry_is_shape_checked(self):
        decl = {"declared": [{"title": "a", "round": {"nonsense": True}}]}
        assert any("nonsense" in e for e in validate_wrap_declaration(decl))

    def test_invalid_declaration_aborts_with_no_writes(self, cfg: Config):
        result = reconcile(cfg, {"declared": [{"asked": "#1"}]})
        assert result.errors
        assert task_notes(cfg) == {}


# ---------------------------------------------------------------------------
# Solo-lane boundary declaration — declaration is not inference


class TestSoloLaneMint:
    def test_mint_writes_stub_round_consumes_and_register_row(self, cfg: Config):
        result = reconcile(cfg, load_declaration())
        assert result.errors == []
        assert len(result.minted) == 1
        task_id = result.minted[0]
        assert TASK_ID_RE.match(task_id)

        fm = task_notes(cfg)[task_id]
        assert validate_task_note(fm) == []
        assert fm["status"] == "open"
        assert fm["grain"] == "work"
        assert fm["asked"] == "#189"
        assert fm["consumes"] == ["dec-c839fb4e", "src-77778888"]
        assert len(fm["rounds"]) == 1
        assert fm["rounds"][0]["did"]["paths"] == [
            "src/thinkweave/operations/task_seam.py"
        ]

        rows = hook_events.task_rows(stream(cfg))
        assert [r["type"] for r in rows] == ["task_open"]
        assert rows[0]["task_id"] == task_id

    def test_wrap_never_fills_outcome(self, cfg: Config):
        result = reconcile(cfg, load_declaration())
        task_id = result.minted[0]
        done = {"declared": [{"continuing": task_id, "done": True}]}
        reconcile(cfg, done)
        for fm in task_notes(cfg).values():
            assert "outcome" not in fm


# ---------------------------------------------------------------------------
# Round append — one appended round per segment, never a new note


class TestRoundAppend:
    def test_continuing_appends_a_round_to_the_existing_note(self, cfg: Config):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        cont = {
            "declared": [
                {
                    "continuing": task_id,
                    "round": {"did": {"paths": ["docs/SKILLS.md"]}},
                }
            ]
        }
        result = reconcile(cfg, cont)
        assert result.errors == []
        assert result.appended == [task_id]
        assert result.minted == []

        notes = task_notes(cfg)
        assert list(notes) == [task_id]  # never a second note
        assert len(notes[task_id]["rounds"]) == 2
        assert notes[task_id]["status"] == "open"

    def test_second_finalize_appends_never_mints(self, cfg: Config):
        r1 = finalize_wrap(
            cfg, session_id=SESSION, project="t", tasks=load_declaration()
        )
        task_id = r1.tasks["minted"][0]
        cont = {
            "declared": [
                {"continuing": task_id, "round": {"did": {"attempts": 2}}}
            ]
        }
        r2 = finalize_wrap(cfg, session_id=SESSION, project="t", tasks=cont)
        assert r2.tasks["appended"] == [task_id]
        assert r2.tasks["minted"] == []
        notes = task_notes(cfg)
        assert list(notes) == [task_id]
        assert len(notes[task_id]["rounds"]) == 2

    def test_continuing_unknown_task_is_an_error(self, cfg: Config):
        result = reconcile(
            cfg, {"declared": [{"continuing": "tsk-deadbeef"}]}
        )
        assert any("tsk-deadbeef" in e for e in result.errors)

    def test_continuing_a_closed_task_is_an_error(self, cfg: Config):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        reconcile(cfg, {"declared": [{"continuing": task_id, "done": True}]})
        result = reconcile(cfg, {"declared": [{"continuing": task_id}]})
        assert any("closed" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Closure — only the user's explicit done; never wrap's implicit call


class TestClosure:
    def test_open_without_done_stays_open(self, cfg: Config):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        assert task_notes(cfg)[task_id]["status"] == "open"

    def test_explicit_done_closes_and_records_the_boundary(self, cfg: Config):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        result = reconcile(
            cfg, {"declared": [{"continuing": task_id, "done": True}]}
        )
        assert result.closed == [task_id]
        assert task_notes(cfg)[task_id]["status"] == "closed"
        types = [r["type"] for r in hook_events.task_rows(stream(cfg))]
        assert types == ["task_open", "task_close"]


# ---------------------------------------------------------------------------
# Orphan flags — an open with no id-matched close, at boundary sparsity


class TestOrphans:
    def _seed_ledger(self, cfg: Config) -> tuple[str, str, str]:
        """Two per-dispatch opens (one closed) and one work-grain open."""
        hanging = task_seam.open_task(
            cfg, session_key=SESSION, project="t", grain="per-dispatch"
        ).task_id
        paired = task_seam.open_task(
            cfg, session_key=SESSION, project="t", grain="per-dispatch"
        ).task_id
        task_seam.close_task(cfg, paired, session_key=SESSION)
        work = task_seam.open_task(
            cfg, session_key=SESSION, project="t", grain="work"
        ).task_id
        return hanging, paired, work

    def test_open_stub_with_no_close_is_flagged(self, cfg: Config):
        hanging, paired, work = self._seed_ledger(cfg)
        result = reconcile(cfg, {"declared": []})
        assert result.orphaned == [hanging]
        notes = task_notes(cfg)
        assert notes[hanging]["orphan"] is True
        assert notes[hanging]["status"] == "open"  # flagged, not closed
        assert "orphan" not in notes[paired]
        assert "orphan" not in notes[work]  # work grain stays open by design

    def test_task_id_only_sparsity_skips_orphan_flagging(self, cfg: Config):
        hanging, _paired, _work = self._seed_ledger(cfg)
        result = reconcile(
            cfg, {"sparsity": "task-id-only", "declared": []}
        )
        assert result.orphaned == []
        assert "orphan" not in task_notes(cfg)[hanging]


# ---------------------------------------------------------------------------
# Root-task segmentation — children re-parent by interval containment


class TestReparenting:
    def test_children_inside_the_activity_interval_reparent(self, cfg: Config):
        seed_stub(cfg, "tsk-11111111", grain="per-dispatch", parent="ses-old")
        seed_stub(cfg, "tsk-22222222", grain="per-dispatch", parent="ses-old")
        write_rows(
            stream(cfg),
            [
                {"ts": "2026-09-22T10:00:00+00:00", "type": "prompt", "text": "go"},
                {
                    "ts": "2026-09-22T09:00:00+00:00", "type": "task_open",
                    "task_id": "tsk-22222222", "session_id": SESSION,
                    "grain": "per-dispatch",
                },
                {
                    "ts": "2026-09-22T10:30:00+00:00", "type": "task_open",
                    "task_id": "tsk-11111111", "session_id": SESSION,
                    "grain": "per-dispatch",
                },
                {
                    "ts": "2026-09-22T10:40:00+00:00", "type": "task_close",
                    "task_id": "tsk-11111111", "session_id": SESSION,
                },
                {"ts": "2026-09-22T11:00:00+00:00", "type": "prompt", "text": "ok"},
            ],
        )
        result = reconcile(cfg, load_declaration())
        root = result.minted[0]
        assert result.reparented == ["tsk-11111111"]
        notes = task_notes(cfg)
        assert notes["tsk-11111111"]["parent"] == root
        # Opened before the segment's first activity: outside the chain.
        assert notes["tsk-22222222"]["parent"] == "ses-old"

    def test_work_grain_peers_are_never_reparented(self, cfg: Config):
        seed_stub(cfg, "tsk-33333333", grain="work", parent="ses-old")
        write_rows(
            stream(cfg),
            [
                {"ts": "2026-09-22T10:00:00+00:00", "type": "prompt", "text": "go"},
                {
                    "ts": "2026-09-22T10:30:00+00:00", "type": "task_open",
                    "task_id": "tsk-33333333", "session_id": SESSION,
                    "grain": "work",
                },
            ],
        )
        reconcile(cfg, load_declaration())
        assert task_notes(cfg)["tsk-33333333"]["parent"] == "ses-old"

    def test_no_declared_root_means_no_reparenting(self, cfg: Config):
        seed_stub(cfg, "tsk-11111111", grain="per-dispatch", parent="ses-old")
        write_rows(
            stream(cfg),
            [
                {"ts": "2026-09-22T10:00:00+00:00", "type": "prompt", "text": "go"},
                {
                    "ts": "2026-09-22T10:30:00+00:00", "type": "task_open",
                    "task_id": "tsk-11111111", "session_id": SESSION,
                    "grain": "per-dispatch",
                },
            ],
        )
        result = reconcile(cfg, {"declared": []})
        assert result.reparented == []
        assert task_notes(cfg)["tsk-11111111"]["parent"] == "ses-old"


# ---------------------------------------------------------------------------
# Continuation proposals — from consumes overlap, never a silent merge


class TestContinuationProposal:
    def test_consumes_overlap_proposes_but_still_mints(self, cfg: Config):
        seed_stub(
            cfg, "tsk-44444444", grain="work",
            consumes=["dec-c839fb4e", "dec-other"],
        )
        result = reconcile(cfg, load_declaration())
        assert result.errors == []
        assert len(result.minted) == 1  # the mint still happens
        assert result.appended == []  # nothing merged silently
        assert result.proposals == [
            {
                "task_id": "tsk-44444444",
                "overlap": ["dec-c839fb4e"],
                "declared_title": "wire the wrap task pass",
            }
        ]
        assert task_notes(cfg)["tsk-44444444"]["rounds"] == []

    def test_no_overlap_no_proposal(self, cfg: Config):
        seed_stub(cfg, "tsk-44444444", grain="work", consumes=["dec-other"])
        result = reconcile(cfg, load_declaration())
        assert result.proposals == []


# ---------------------------------------------------------------------------
# Decisions stamp task_id


class TestDecisionStamp:
    def test_minted_decisions_gain_the_task_id(self, cfg: Config, tmp_path: Path):
        folder = tmp_path / "session-folder"
        folder.mkdir()
        dec = folder / "use-sqlite.md"
        dec.write_text(
            "---\ntype: decision\nid: dec-aaaa1111\ntitle: Use SQLite\n---\n\nBody.\n",
            encoding="utf-8",
        )
        result = reconcile(cfg, load_declaration(), folders=[folder])
        fm, _ = parse_frontmatter(dec.read_text(encoding="utf-8"))
        assert fm["task_id"] == result.minted[0]

    def test_unlocatable_decision_is_announced(self, cfg: Config):
        result = reconcile(cfg, load_declaration())
        assert any("dec-aaaa1111" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# The wrap-finalize surface — the flag, and the pass in the chain


class TestFinalizeSurface:
    def test_finalize_reports_the_pass_and_stays_green(self, cfg: Config):
        result = finalize_wrap(
            cfg, session_id=SESSION, project="t", tasks=load_declaration()
        )
        assert result.errors == []
        assert result.tasks["minted"] and "tasks" in result.timings
        assert "tasks" in result.as_dict()

    def test_cli_flag_drives_the_pass_from_a_canned_file(
        self, cfg: Config, capsys
    ):
        args = build_parser().parse_args(
            [
                "wrap-finalize", SESSION, "--project", "t",
                "--tasks", str(FIXTURES / "wrap-declaration.json"), "--json",
            ]
        )
        with pytest.raises(SystemExit) as exc:
            cmd_wrap_finalize(args)
        assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["tasks"]["minted"]) == 1
        assert task_notes(cfg)

    def test_cli_rejects_an_unreadable_declaration(self, cfg: Config, tmp_path):
        bad = tmp_path / "decl.json"
        bad.write_text("not json", encoding="utf-8")
        args = build_parser().parse_args(
            ["wrap-finalize", SESSION, "--project", "t", "--tasks", str(bad)]
        )
        with pytest.raises(SystemExit) as exc:
            cmd_wrap_finalize(args)
        assert exc.value.code == 2
