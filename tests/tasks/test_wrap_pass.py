"""The wrap task pass: the model judges, the pass applies.

The pass runs inside ``weave wrap-finalize`` on a declaration file the
wrap LLM composed (canned here; no model call). Each declared entry
mints a task or appends a round to a continuing one, closes on explicit
done, attaches declared seam children, and stamps minted decisions with
the task id. Orphan flags are the one mechanical check, at boundary
sparsity only. The pass never fills ``outcome``, never reopens a closed
task, and never attributes a child by inference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thinkweave.core import harness
from thinkweave.core.config import Config
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import (
    TASK_ID_RE,
    validate_task_note,
    validate_wrap_declaration,
)
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.core.buffer import buffer_path
from thinkweave.operations import tasks
from thinkweave.operations.tasks import Register, TaskStore
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
    return buffer_path(cfg.weave_dir, SESSION)


def stream_rows(cfg: Config) -> list[dict]:
    return Register(cfg, SESSION, [stream(cfg)]).rows()


def note_path(cfg: Config, task_id: str) -> Path:
    task = TaskStore(cfg).get(task_id)
    assert task is not None and task.path is not None
    return task.path


def reconcile(cfg: Config, declaration: dict, folders: list[Path] | None = None):
    return tasks.apply_declaration(
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

    def test_rejects_malformed_children_ids(self):
        decl = {"declared": [{"title": "a", "children": ["nope"]}]}
        assert any("children" in e for e in validate_wrap_declaration(decl))

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
        assert TASK_ID_RE.fullmatch(task_id)

        fm = task_notes(cfg)[task_id]
        assert validate_task_note(fm) == []
        assert fm["status"] == "open"
        assert fm["grain"] == "work"
        assert fm["asked"] == "github:marekpal97/thinkweave#189"
        assert fm["consumes"] == ["dec-c839fb4e", "src-77778888"]
        assert len(fm["rounds"]) == 1
        assert fm["rounds"][0]["did"]["paths"] == [
            "src/thinkweave/operations/task_seam.py"
        ]
        # The declared ledger lands verbatim — commits included.
        assert fm["rounds"][0]["did"]["commits"] == ["4885b78"]

        rows = stream_rows(cfg)
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
        result = tasks.apply_declaration(
            cfg, cont, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
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
        r2 = finalize_wrap(cfg, session_id="s-2", project="t", tasks=cont)
        assert r2.tasks["appended"] == [task_id]
        assert r2.tasks["minted"] == []
        notes = task_notes(cfg)
        assert list(notes) == [task_id]
        assert len(notes[task_id]["rounds"]) == 2

    def test_rewrap_of_a_mint_reuses_the_note_and_replaces_its_round(
        self, cfg: Config
    ):
        first = reconcile(cfg, load_declaration())
        again = reconcile(cfg, load_declaration())
        assert again.errors == []
        assert again.minted == []
        notes = task_notes(cfg)
        assert list(notes) == first.minted
        assert len(notes[first.minted[0]]["rounds"]) == 1
        opens = [
            r for r in stream_rows(cfg)
            if r["type"] == "task_open"
        ]
        assert len(opens) == 1

    def test_rewrap_of_a_continuation_replaces_this_sessions_round(
        self, cfg: Config
    ):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        cont = {
            "declared": [
                {"continuing": task_id, "round": {"did": {"attempts": 2}}}
            ]
        }
        tasks.apply_declaration(
            cfg, cont, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
        cont["declared"][0]["round"] = {"did": {"attempts": 3}}
        tasks.apply_declaration(
            cfg, cont, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
        rounds = task_notes(cfg)[task_id]["rounds"]
        assert [r["did"]["attempts"] for r in rounds] == [1, 3]

    def test_rewrap_of_done_records_one_close(self, cfg: Config):
        task_id = reconcile(cfg, load_declaration()).minted[0]
        done = {"declared": [{"continuing": task_id, "done": True}]}
        reconcile(cfg, done)
        again = reconcile(cfg, done)
        assert again.errors == []
        closes = [
            r for r in stream_rows(cfg)
            if r["type"] == "task_close"
        ]
        assert len(closes) == 1

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
        types = [r["type"] for r in stream_rows(cfg)]
        assert types == ["task_open", "task_close"]


# ---------------------------------------------------------------------------
# Orphan flags — an open with no id-matched close, at boundary sparsity


class TestOrphans:
    def _seed_ledger(self, cfg: Config) -> tuple[str, str, str]:
        """Two per-dispatch opens (one closed) and one work-grain open."""
        hanging = tasks.open_child(
            cfg, session_key=SESSION, project="t", grain="per-dispatch"
        ).task_id
        paired = tasks.open_child(
            cfg, session_key=SESSION, project="t", grain="per-dispatch"
        ).task_id
        tasks.close_child(cfg, paired, session_key=SESSION)
        work = tasks.open_child(
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

    def test_a_close_after_the_wrap_clears_the_orphan_flag(self, cfg: Config):
        hanging, _paired, _work = self._seed_ledger(cfg)
        decl = load_declaration()
        decl["declared"][0]["children"] = [hanging]
        parent = reconcile(cfg, decl).minted[0]
        assert task_notes(cfg)[hanging]["orphan"] is True
        tasks.close_child(cfg, hanging, session_key=SESSION)
        fm = task_notes(cfg)[hanging]
        assert fm["status"] == "closed"
        assert "orphan" not in fm
        assert fm["parent"] == parent
        assert validate_task_note(fm) == []

    def test_task_id_only_sparsity_skips_orphan_flagging(self, cfg: Config):
        hanging, _paired, _work = self._seed_ledger(cfg)
        result = reconcile(
            cfg, {"sparsity": "task-id-only", "declared": []}
        )
        assert result.orphaned == []
        assert "orphan" not in task_notes(cfg)[hanging]


# ---------------------------------------------------------------------------
# Declared children — the model attributes dispatches, never timestamps


class TestChildren:
    def declaration_with_children(self, children: list[str]) -> dict:
        decl = load_declaration()
        decl["declared"][0]["children"] = children
        return decl

    def test_declared_children_attach_to_the_declared_task(self, cfg: Config):
        seed_stub(cfg, "tsk-11111111", grain="per-dispatch")
        seed_stub(cfg, "tsk-22222222", grain="per-dispatch")
        result = reconcile(
            cfg, self.declaration_with_children(["tsk-11111111"])
        )
        assert result.errors == []
        assert result.attached == ["tsk-11111111"]
        notes = task_notes(cfg)
        assert notes["tsk-11111111"]["parent"] == result.minted[0]
        # Undeclared children stay unattached — no timestamp guessing.
        assert "parent" not in notes["tsk-22222222"]

    def test_work_grain_peers_are_never_children(self, cfg: Config):
        seed_stub(cfg, "tsk-33333333", grain="work")
        result = reconcile(
            cfg, self.declaration_with_children(["tsk-33333333"])
        )
        assert any("work grain" in e for e in result.errors)
        assert result.attached == []
        assert "parent" not in task_notes(cfg)["tsk-33333333"]

    def test_child_attached_elsewhere_is_an_error_not_an_overwrite(
        self, cfg: Config
    ):
        seed_stub(
            cfg, "tsk-11111111", grain="per-dispatch", parent="tsk-99999999"
        )
        result = reconcile(
            cfg, self.declaration_with_children(["tsk-11111111"])
        )
        assert any("already attached" in e for e in result.errors)
        assert task_notes(cfg)["tsk-11111111"]["parent"] == "tsk-99999999"

    def test_a_round_child_whose_note_is_gone_is_announced(self, cfg: Config):
        seed_stub(cfg, "tsk-11111111", grain="per-dispatch")
        task_id = reconcile(
            cfg, self.declaration_with_children(["tsk-11111111"])
        ).minted[0]
        note_path(cfg, "tsk-11111111").unlink()
        cont = {"declared": [{"continuing": task_id, "round": {"did": {"attempts": 2}}}]}
        result = tasks.apply_declaration(
            cfg, cont, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
        assert result.errors == []
        assert any("tsk-11111111" in w and "no task note" in w for w in result.warnings)

    def test_a_closed_child_attaches_and_keeps_its_rounds(self, cfg: Config):
        child = tasks.open_child(cfg, session_key=SESSION, project="t").task_id
        tasks.close_child(cfg, child, session_key=SESSION)
        rounds = task_notes(cfg)[child]["rounds"]
        assert len(rounds) == 1
        result = reconcile(cfg, self.declaration_with_children([child]))
        assert result.errors == []
        assert result.attached == [child]
        fm = task_notes(cfg)[child]
        assert fm["parent"] == result.minted[0]
        assert fm["status"] == "closed"
        assert fm["rounds"] == rounds

    def test_unknown_child_is_an_error(self, cfg: Config):
        result = reconcile(
            cfg, self.declaration_with_children(["tsk-deadbeef"])
        )
        assert any("tsk-deadbeef" in e for e in result.errors)

    def test_a_child_dispatched_with_a_multiline_prompt_attaches(self, cfg: Config):
        prompt = (
            "Work on ticket #1.\n\n"
            "Interface contract (do NOT edit src/): greet --name NAME\n"
            "Your dispatch is /x/implementer-77.dispatch.md: read it whole"
        )
        child = tasks.open_child(cfg, session_key=SESSION, asked=prompt).task_id
        result = reconcile(cfg, self.declaration_with_children([child]))
        assert result.errors == []
        assert result.attached == [child]
        fm = task_notes(cfg)[child]
        assert fm["parent"] == result.minted[0]
        assert fm["asked"] == prompt
        assert validate_task_note(fm) == []

    def test_a_child_that_cannot_attach_names_itself_and_the_reason(
        self, cfg: Config
    ):
        seed_stub(cfg, "tsk-11111111", grain="per-dispatch")
        path = note_path(cfg, "tsk-11111111")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "kind: task", "kind: task\nbogus_key: stray"
            ),
            encoding="utf-8",
        )
        result = reconcile(cfg, self.declaration_with_children(["tsk-11111111"]))
        assert result.attached == []
        assert [e for e in result.errors if "tsk-11111111" in e and "bogus_key" in e]


# ---------------------------------------------------------------------------
# Decisions stamp task_id


class TestDecisionStamp:
    def test_a_decision_filed_outside_the_session_folder_gains_the_task_id(
        self, cfg: Config, tmp_path: Path
    ):
        from thinkweave.core.indexer import Indexer

        vm = VaultManager(config=cfg)
        vm.ensure_dirs()
        dec = vm.create_note(
            NoteType.DECISION, "Use SQLite", body="Body.", project="t",
            note_id="dec-aaaa1111",
        )
        idx = Indexer(config=cfg)
        idx.index_file(dec)
        idx.close()
        session_folder = tmp_path / "session-folder"
        session_folder.mkdir()
        result = reconcile(cfg, load_declaration(), folders=[session_folder])
        fm, _ = parse_frontmatter(dec.read_text(encoding="utf-8"))
        assert fm["task_id"] == result.minted[0]
        assert result.stamped == 1

    def test_a_decision_the_index_does_not_know_is_announced(self, cfg: Config):
        result = reconcile(cfg, load_declaration())
        assert result.stamped == 0
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


# ---------------------------------------------------------------------------
# The ledger: rounds reference the session's notes; the body is derived


def seed_session(cfg: Config, *, harness_derived: bool = False) -> tuple[Path, dict[str, str]]:
    """One wrapped session: session note, two insights, a decision, and a
    feedback verdict in its events stream. Returns (folder, ids).
    ``harness_derived`` points the insights at the harness session id, as
    ``weave_extract`` writes them, instead of the session note's id."""
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    ses = vm.create_note(
        NoteType.SESSION, "S", body="## Summary\nx\n", project="t",
        extra_frontmatter={"source_session": SESSION},
    )
    ses_id = vm.read_note(ses).id
    derived = SESSION if harness_derived else ses_id
    folder = ses.parent
    ids = {"session": ses_id}
    for key, title in (("insight", "Ledger owns no content"), ("insight2", "Refs anchor identity")):
        path = vm.create_note(
            NoteType.NOTE, title, body="b", project="t",
            extra_frontmatter={"derived_from": [derived]}, output_dir=folder,
        )
        ids[key] = vm.read_note(path).id
    (folder / "use-sqlite.md").write_text(
        "---\ntype: decision\nid: dec-aaaa1111\ntitle: Use SQLite\n---\n\nBody.\n",
        encoding="utf-8",
    )
    ids["decision"] = "dec-aaaa1111"
    feedback = {
        "ts": "2026-10-02T10:00:00+00:00", "type": "feedback",
        "session_id": SESSION, "register": "correction",
        "prompt_ref": "no, keep rounds sealed",
    }
    (folder / "events.jsonl").write_text(json.dumps(feedback) + "\n", encoding="utf-8")
    return folder, ids


def reconcile_session(cfg: Config, declaration: dict, folder: Path, key: str = SESSION):
    return tasks.apply_declaration(
        cfg, declaration, session_key=key, project="t",
        streams=[folder / "events.jsonl"], folders=[folder],
    )


def task_body(cfg: Config, task_id: str) -> str:
    path = note_path(cfg, task_id)
    return parse_frontmatter(path.read_text(encoding="utf-8"))[1]


def round_lines(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("- ")]


class TestLedgerRound:
    def test_wrap_round_references_the_session_and_validates(self, cfg: Config):
        folder, ids = seed_session(cfg)
        decl = load_declaration()
        decl["declared"][0]["round"]["outputs"] = [
            {"kind": "url", "ref": "https://deck.example/s", "role": "deliverable"}
        ]
        result = reconcile_session(cfg, decl, folder)
        assert result.errors == []
        fm = task_notes(cfg)[result.minted[0]]
        assert validate_task_note(fm) == []
        entry = fm["rounds"][0]
        assert entry["route"] == "session"
        assert entry["session_ref"]["value"] == ids["session"]
        assert entry["outputs"] == [
            {"kind": "url", "ref": "https://deck.example/s", "role": "deliverable"}
        ]

    def test_single_task_session_attributes_every_insight_and_verdict(
        self, cfg: Config
    ):
        folder, ids = seed_session(cfg)
        result = reconcile_session(cfg, load_declaration(), folder)
        entry = task_notes(cfg)[result.minted[0]]["rounds"][0]
        assert sorted(entry["notes"]) == sorted([ids["insight"], ids["insight2"]])
        assert entry["feedback"] == [
            {
                "register": "correction",
                "prompt_ref": "no, keep rounds sealed",
                "ts": "2026-10-02T10:00:00+00:00",
            }
        ]

    def test_round_names_the_harness_the_wrap_runs_under(self, cfg: Config, monkeypatch):
        monkeypatch.setattr(harness, "_OVERRIDE", None)
        monkeypatch.setenv("THINKWEAVE_HARNESS", "codex")
        folder, _ids = seed_session(cfg)
        result = reconcile_session(cfg, load_declaration(), folder)
        entry = task_notes(cfg)[result.minted[0]]["rounds"][0]
        assert entry["session_ref"]["harness"] == "codex"

    def test_solo_session_credits_insights_derived_from_the_harness_id(
        self, cfg: Config
    ):
        folder, ids = seed_session(cfg, harness_derived=True)
        result = reconcile_session(cfg, load_declaration(), folder)
        entry = task_notes(cfg)[result.minted[0]]["rounds"][0]
        assert sorted(entry["notes"]) == sorted([ids["insight"], ids["insight2"]])

    @pytest.mark.parametrize("harness_derived", [False, True])
    def test_multi_task_session_attributes_only_what_is_declared(
        self, cfg: Config, harness_derived: bool
    ):
        folder, ids = seed_session(cfg, harness_derived=harness_derived)
        decl = {
            "declared": [
                {"title": "a", "round": {"notes": [ids["insight"]]}},
                {"title": "b", "round": {"did": {"attempts": 1}}},
            ]
        }
        result = reconcile_session(cfg, decl, folder)
        assert result.errors == []
        notes = task_notes(cfg)
        a, b = (notes[t]["rounds"][0] for t in result.minted)
        assert a["notes"] == [ids["insight"]]
        assert "notes" not in b and "feedback" not in b

    def test_body_lists_one_wikilink_line_per_round_in_order(self, cfg: Config):
        folder, ids = seed_session(cfg)
        task_id = reconcile_session(cfg, load_declaration(), folder).minted[0]
        cont = {"declared": [{"continuing": task_id, "round": {"did": {"attempts": 2}}}]}
        tasks.apply_declaration(  # a later session with no session note
            cfg, cont, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
        lines = round_lines(task_body(cfg, task_id))
        assert len(lines) == 2
        assert ids["session"] in lines[0] and "[[" in lines[0]
        assert ids["insight"] in lines[0] and "dec-aaaa1111" in lines[0]
        assert "s-2" in lines[1]

    def test_graph_walk_from_the_task_reaches_its_referenced_notes(
        self, cfg: Config
    ):
        from thinkweave.core.indexer import Indexer
        from thinkweave.retrieval.search import Search

        folder, ids = seed_session(cfg)
        task_id = reconcile_session(cfg, load_declaration(), folder).minted[0]
        idx = Indexer(config=cfg)
        try:
            idx.rebuild(full=True)
        finally:
            idx.close()
        s = Search(config=cfg)
        try:
            reached = {n.id for n in s.get_related(task_id, depth=1)}
        finally:
            s.close()
        assert {ids["session"], ids["insight"], ids["insight2"], "dec-aaaa1111"} <= reached

    def test_body_credits_a_child_whose_output_is_a_deliverable(
        self, cfg: Config
    ):
        folder, _ids = seed_session(cfg)
        child = tasks.open_child(cfg, session_key=SESSION, project="t").task_id
        stub = note_path(cfg, child)
        VaultManager(config=cfg).update_note(
            stub,
            frontmatter_updates={
                "rounds": [{"outputs": [{"kind": "note", "ref": "n-0babe000"}]}]
            },
        )
        decl = load_declaration()
        decl["declared"][0]["children"] = [child]
        decl["declared"][0]["round"]["outputs"] = [
            {"kind": "note", "ref": "n-0babe000", "role": "deliverable"}
        ]
        task_id = reconcile_session(cfg, decl, folder).minted[0]
        line = round_lines(task_body(cfg, task_id))[0]
        assert "n-0babe000" in line and child in line
        assert task_notes(cfg)[task_id]["rounds"][0]["children"] == [child]


# ---------------------------------------------------------------------------
# Identity: a normalized tracker ref resolves to the open task carrying it


class TestTrackerIdentity:
    def test_a_declared_ref_appends_to_the_open_task_carrying_it(
        self, cfg: Config
    ):
        first = reconcile(cfg, load_declaration()).minted[0]
        decl = {
            "declared": [
                {
                    "title": "picked up again",
                    "asked": "github:marekpal97/thinkweave#189",
                    "round": {"did": {"attempts": 1}},
                }
            ]
        }
        result = tasks.apply_declaration(
            cfg, decl, session_key="s-2", project="t",
            streams=[buffer_path(cfg.weave_dir, "s-2")],
        )
        assert result.minted == []
        assert result.appended == [first]
        assert len(task_notes(cfg)[first]["rounds"]) == 2

    def test_a_bare_issue_number_normalizes_to_the_current_repo(
        self, cfg: Config, tmp_path: Path, monkeypatch
    ):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin",
             "git@github.com:acme/widgets.git"],
            check=True,
        )
        monkeypatch.chdir(repo)
        decl = {"declared": [{"title": "t", "asked": "#7"}]}
        task_id = reconcile(cfg, decl).minted[0]
        assert task_notes(cfg)[task_id]["asked"] == "github:acme/widgets#7"


class TestEpicIdentity:
    """A declared sub-issue lands on its epic's task, found through
    GitHub's sub-issue parent."""

    SUB = "https://github.com/marekpal97/thinkweave/issues/241"
    EPIC = "github:marekpal97/funloops#89"

    def declare(self, cfg: Config):
        decl = {"declared": [{"title": "t", "asked": self.SUB, "round": {}}]}
        return reconcile(cfg, decl)

    def test_a_sub_issue_lands_on_its_epic_task(self, cfg: Config, monkeypatch):
        parents = {("marekpal97/thinkweave", "241"): self.EPIC}
        monkeypatch.setattr(
            tasks, "_gh_parent", lambda repo, number: parents.get((repo, number), "")
        )
        result = self.declare(cfg)
        assert result.errors == [] and result.warnings == []
        fm = task_notes(cfg)[result.minted[0]]
        assert fm["asked"] == self.EPIC
        assert fm["rounds"][0]["asked"] == "github:marekpal97/thinkweave#241"

    def test_a_failed_parent_lookup_keeps_the_sub_issue_and_warns(
        self, cfg: Config, monkeypatch
    ):
        import subprocess

        def unreachable(repo, number):
            raise subprocess.CalledProcessError(
                1, ["gh"], stderr="error connecting to api.github.com"
            )

        monkeypatch.setattr(tasks, "_gh_parent", unreachable)
        result = self.declare(cfg)
        fm = task_notes(cfg)[result.minted[0]]
        assert fm["asked"] == "github:marekpal97/thinkweave#241"
        (warning,) = result.warnings
        assert "github:marekpal97/thinkweave#241" in warning
        assert "error connecting to api.github.com" in warning


# ---------------------------------------------------------------------------
# Migration: existing task notes take the ledger shape


class TestLedgerMigration:
    def seed_existing(self, cfg: Config) -> None:
        folder = cfg.vault_root / "projects" / "t" / "sessions" / "old"
        folder.mkdir(parents=True)
        for name in ("devloop-rich", "envelope-thin", "declared-only"):
            text = (FIXTURES / f"{name}.md").read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
            (folder / f"{fm['id']}.md").write_text(text, encoding="utf-8")
        seed_stub(cfg, "tsk-0dd0dd00", grain="work")
        stub = note_path(cfg, "tsk-0dd0dd00")
        VaultManager(config=cfg).update_note(
            stub,
            frontmatter_updates={
                "asked": "github:o/r#5",
                "outcome": [{"label": "merged-clean"}],
                "rounds": [
                    {
                        "session_ref": {
                            "harness": "claude-code", "kind": "session_id", "value": "s-old",
                        },
                        "did": {"attempts": 1},
                    }
                ],
            },
        )

    def test_every_existing_task_note_stays_valid_and_gains_the_ledger_shape(
        self, cfg: Config
    ):
        from thinkweave.operations.migrations import migrate_task_notes_to_ledger

        self.seed_existing(cfg)
        assert migrate_task_notes_to_ledger(cfg).rewritten == 4
        notes = task_notes(cfg)
        for fm in notes.values():
            assert validate_task_note(fm) == []
        assert notes["tsk-3f9a1c2e"]["rounds"][0]["route"] == "devloop"
        assert notes["tsk-0dd0dd00"]["rounds"][0]["route"] == "session"
        assert "route" not in notes["tsk-9b2d4e6f"]["rounds"][0]  # batch grain
        assert "outcome" not in notes["tsk-0dd0dd00"]  # no writer, so no field
        assert len(round_lines(task_body(cfg, "tsk-0dd0dd00"))) == 1
        assert migrate_task_notes_to_ledger(cfg).rewritten == 0  # idempotent

    def test_a_note_split_by_a_multiline_asked_is_rejoined(self, cfg: Config):
        from thinkweave.operations.migrations import migrate_task_notes_to_ledger

        folder = cfg.vault_root / "projects" / "t" / "sessions" / "old"
        folder.mkdir(parents=True)
        note = folder / "tsk-7a0710e0.md"
        note.write_text(
            (FIXTURES / "split-asked.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        assert migrate_task_notes_to_ledger(cfg).rewritten == 1
        fm = task_notes(cfg)["tsk-7a0710e0"]
        assert validate_task_note(fm) == []
        assert fm["asked"] == (
            'Write the tests for ticket #1: the "dogfood greet --name NAME" subcommand.\n'
            "\n"
            "Interface contract (do NOT edit src/): dogfood greet --name NAME prints Hello, NAME!\n"
            "Create tests/test_greet.py only. Cover: the greeting, a missing --name, a name with spaces.\n"
            "- run them with uv run pytest -q\n"
            "Report back the test names and C:\\sandbox\\tests\\"
        )
        assert migrate_task_notes_to_ledger(cfg).rewritten == 0


class TestRawRefMigration:
    """A task stored under a raw ``owner/repo#N`` folds to its ref; tasks the
    fold makes share one ``asked`` merge when at most one is open."""

    RAW = "o/r#77"
    REF = "github:o/r#77"

    def seed(self, cfg: Config, task_id: str, *, asked: str, status: str, date: str,
             rounds: list[dict] | None = None, parent: str = ""):
        seed_stub(cfg, task_id, grain="per-dispatch" if parent else "work", parent=parent)
        updates = {"status": status, "date": date, "rounds": rounds or []}
        if asked:
            updates["asked"] = asked
        VaultManager(config=cfg).update_note(
            note_path(cfg, task_id), frontmatter_updates=updates
        )

    def session_round(self, value: str) -> dict:
        return {
            "route": "session",
            "session_ref": {"harness": "claude-code", "kind": "session_id", "value": value},
        }

    def migrate(self, cfg: Config):
        from thinkweave.operations.migrations import migrate_task_notes_to_ledger

        return migrate_task_notes_to_ledger(cfg)

    def test_a_wrap_on_the_raw_ref_continues_the_migrated_open_task(self, cfg: Config):
        self.seed(cfg, "tsk-0a0a0a0a", asked=self.RAW, status="open", date="2026-10-01")
        self.migrate(cfg)
        decl = {"declared": [{"title": "more", "asked": self.RAW, "round": {}}]}
        result = reconcile(cfg, decl)
        assert result.minted == [] and result.appended == ["tsk-0a0a0a0a"]
        assert task_notes(cfg)["tsk-0a0a0a0a"]["asked"] == self.REF

    def test_a_raw_and_a_folded_task_on_one_ref_merge_into_the_open_one(self, cfg: Config):
        self.seed(cfg, "tsk-0a0a0a0a", asked=self.RAW, status="open", date="2026-10-01",
                  rounds=[self.session_round("s-1")])
        self.seed(cfg, "tsk-0b0b0b0b", asked=self.REF, status="closed", date="2026-10-03",
                  rounds=[self.session_round("s-2")])
        self.seed(cfg, "tsk-0c0c0c0c", asked="", status="closed", date="2026-10-03",
                  parent="tsk-0b0b0b0b")
        report = self.migrate(cfg)
        (shared,) = report.shared
        assert shared.asked == self.REF and shared.kept == "tsk-0a0a0a0a"
        assert set(shared.task_ids) == {"tsk-0a0a0a0a", "tsk-0b0b0b0b"}
        notes = task_notes(cfg)
        assert "tsk-0b0b0b0b" not in notes
        kept = notes["tsk-0a0a0a0a"]
        assert validate_task_note(kept) == []
        assert kept["asked"] == self.REF and kept["status"] == "open"
        assert [r["session_ref"]["value"] for r in kept["rounds"]] == ["s-1", "s-2"]
        assert "tsk-0b0b0b0b" in kept["aliases"]
        assert notes["tsk-0c0c0c0c"]["parent"] == "tsk-0a0a0a0a"
        assert self.migrate(cfg).shared == ()

    def test_two_open_tasks_on_one_ref_are_reported_not_merged(self, cfg: Config):
        self.seed(cfg, "tsk-0a0a0a0a", asked=self.RAW, status="open", date="2026-10-01")
        self.seed(cfg, "tsk-0b0b0b0b", asked=self.REF, status="open", date="2026-10-03")
        (shared,) = self.migrate(cfg).shared
        assert shared.kept == ""
        assert set(task_notes(cfg)) == {"tsk-0a0a0a0a", "tsk-0b0b0b0b"}

    def test_a_merged_away_id_resolves_to_the_task_that_absorbed_it(self, cfg: Config):
        self.seed(cfg, "tsk-0a0a0a0a", asked=self.RAW, status="open", date="2026-10-01")
        self.seed(cfg, "tsk-0b0b0b0b", asked=self.REF, status="closed", date="2026-10-03")
        self.migrate(cfg)
        from thinkweave.core.vault import indexed_alias_path

        assert indexed_alias_path(cfg, "tsk-0b0b0b0b") == note_path(cfg, "tsk-0a0a0a0a")
        store = tasks.TaskStore(cfg)
        assert store.get("tsk-0b0b0b0b").id == "tsk-0a0a0a0a"  # by the index
        cfg.index_db.unlink()
        assert store.get("tsk-0b0b0b0b").id == "tsk-0a0a0a0a"  # by the filing folders
