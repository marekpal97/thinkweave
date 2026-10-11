"""The task-360 oracle: assert scenario checkpoints against the throwaway vault.

Every read goes through the throwaway vault's index: task notes and session
notes are found by query, never by walking a project folder. Work tasks are
matched by tracker ref (`asked`), never by title, so an agent's wording cannot
fool a check. Rows print PASS, FAIL, KNOWN #n (a gap an open ticket owns) or
GAP (a PROBE that found a missing capability), each with its session id.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("TASK360_ROOT", Path.home() / ".local/state/task360"))

# Open tickets that own a known gap: a failing row named here prints
# KNOWN #n instead of FAIL, so a run separates new bugs from owned ones.
KNOWN = {
    "the round records the commit": "#228",
    "commits split between the two tasks": "#228",
    "PROBE #2 gained a Codex round": "#251",
    "PROBE #3 gained a round from Codex": "#251",
}

Row = tuple[str, bool, str]  # (assertion, ok, evidence)

# The mini run (S20–S24) names the real tracker tree, read only: #184 → #243 → #256.
T184, T243, T256 = (f"marekpal97/thinkweave#{n}" for n in (184, 243, 256))


def mark(name: str, ok: bool) -> str:
    """The verdict a row prints."""
    if ok:
        return "PASS"
    if name in KNOWN:
        return f"KNOWN {KNOWN[name]}"
    return "GAP" if name.startswith("PROBE") else "FAIL"


@dataclass
class View:
    """The questions checkpoints ask of one snapshot."""

    snap: dict

    @property
    def tasks(self) -> dict:
        return self.snap["tasks"]

    def ref(self, n: int | str) -> str:
        """Sandbox ticket ``n``'s tracker ref; a string is a qualified ``owner/repo#n``."""
        return f"github:{n}" if isinstance(n, str) else f"{self.snap['repo']}#{n}"

    def work(self, n: int | str) -> list[dict]:
        return [t | {"id": i} for i, t in self.tasks.items()
                if t["grain"] == "work" and t["asked"] == self.ref(n)]

    def one_work(self, n: int | str) -> dict | None:
        found = self.work(n)
        return found[0] if len(found) == 1 else None

    def children(self, task_id: str) -> list[dict]:
        return [t | {"id": i} for i, t in self.tasks.items() if t.get("parent") == task_id]

    def session(self, label: str) -> dict:
        return self.snap["sessions"].get(label) or {}

    def rounds_from(self, task: dict | None, label: str) -> list[dict]:
        s = self.session(label)
        refs = {s.get("key"), s.get("id")} - {None, ""}
        return [r for r in (task or {}).get("rounds", [])
                if (r.get("session_ref") or {}).get("value") in refs]


# ---------------------------------------------------------------------------
# Checkpoints. Each assumes its prerequisites ran. A scenario the driver ran
# scores the state it ended on (frozen/<label>.json); counts are floors, so a
# checkpoint still holds on the final state of a longer run.


def s0(v: View) -> list[Row]:
    s = v.session("S0")
    return [
        ("the throwaway index holds the S0 session note", bool(s.get("id")), str(s.get("id"))),
        ("the S0 session has an event register", s.get("events", 0) > 0,
         f"{s.get('events', 0)} event rows"),
        ("the live vault has no folder for the sandbox project", not v.snap["live_leak"],
         v.snap["live_leak"] or "absent"),
    ]


def s1(v: View) -> list[Row]:
    a = v.one_work(1)
    mine = v.rounds_from(a, "S1")
    return _base(v, 1, 1, "S1") + _children_closed(v, a, 1, "S1") + [
        ("solo session credits its insights to the round",
         bool(mine and mine[0].get("notes")), str(mine and mine[0].get("notes"))),
        ("the round records the commit", bool(mine and (mine[0].get("did") or {}).get("commits")),
         str(mine and mine[0].get("did"))),
    ]


def s2(v: View) -> list[Row]:
    return _base(v, 1, 2, "S2")


def s3(v: View) -> list[Row]:
    return _base(v, 1, 3, "S3") + _children_closed(v, v.one_work(1), 3, "S3")


def s4(v: View) -> list[Row]:
    phantom = [i for i, t in v.tasks.items()
               if t["project"] not in (v.snap["project"], "") or "/worktrees/" in t["path"]]
    return _base(v, 1, 4, "S4") + _children_closed(v, v.one_work(1), 4, "S4") + [
        ("no task filed under a worktree's phantom project", not phantom, str(phantom)),
    ]


def s5(v: View) -> list[Row]:
    """A Claude Code parent opens a helper task and dispatches a herdr Claude worker."""
    a = v.one_work(1)
    herdr = [k for k in (v.children(a["id"]) if a else [])
             if any((r.get("session_ref") or {}).get("kind") == "session_id" for r in k["rounds"])]
    rows = _base(v, 1, 5, "S5") + [
        ("a herdr-bound helper is closed and attached", bool(herdr), str([k["id"] for k in herdr])),
    ]
    if herdr:
        rounds = herdr[-1]["rounds"]
        rows += [
            ("its digest counts tools from the bound slice only",
             any(r.get("tools") for r in rounds), str(rounds)),
            ("its digest excludes the worker's warm-up prompt",
             "WARMUP" not in json.dumps(rounds) and "WARMUP" not in str(herdr[-1].get("asked")),
             str(herdr[-1].get("asked"))),
        ]
    return rows


def s6(v: View) -> list[Row]:
    a = v.one_work(1)
    mine = v.rounds_from(a, "S6")
    late = [f for f in v.snap["feedback"] if a and a["id"] in f["feedback_for"]]
    return _base(v, 1, 6, "S6") + [
        ("the mid-session correction is credited as feedback on the round",
         bool(mine and mine[0].get("feedback")), str(mine and mine[0].get("feedback"))),
        ("the session recorded a verdict event", v.session("S6").get("verdicts", 0) > 0,
         f"verdicts={v.session('S6').get('verdicts')}"),
        ("late feedback lands as a note with a feedback_for edge to the task",
         bool(late), str(v.snap["feedback"])),
    ]


def s7(v: View) -> list[Row]:
    """One session works #2 and #3 (#3 depends on #2), one helper each."""
    b, c = v.one_work(2), v.one_work(3)
    rows = _base(v, 2, 1, "S7") + _base(v, 3, 1, "S7")
    if b and c:
        rb, rc = v.rounds_from(b, "S7"), v.rounds_from(c, "S7")
        cb = {x for r in rb for x in (r.get("did") or {}).get("commits") or []}
        cc = {x for r in rc for x in (r.get("did") or {}).get("commits") or []}
        nb = {x for r in rb for x in r.get("notes") or []}
        nc = {x for r in rc for x in r.get("notes") or []}
        rows += [
            ("commits split between the two tasks", bool(cb) and bool(cc) and not (cb & cc),
             f"{cb} | {cc}"),
            ("each task got its own helper",
             bool(v.children(b["id"])) and bool(v.children(c["id"])),
             f"{len(v.children(b['id']))} / {len(v.children(c['id']))}"),
            ("insights are not credited to both tasks", not (nb & nc), f"shared={nb & nc}"),
        ]
    return rows


def s8(v: View) -> list[Row]:
    a = v.one_work(1)
    loop = [r for r in (a["rounds"] if a else []) if r.get("route") == "devloop"]
    stray = [t for t in v.snap["devloop_buffer"] if t in v.tasks]
    return [
        ("a devloop round lands on task #1", len(loop) == 1, f"{len(loop)} devloop round(s)"),
        ("it points at the trajectory note",
         bool(loop and (loop[0].get("session_ref") or {}).get("kind") == "note"),
         str(loop and loop[0].get("session_ref"))),
        ("its PR is a deliverable output", bool(loop and any(
            o.get("kind") == "pr" and o.get("role") == "deliverable"
            for o in loop[0].get("outputs") or [])), str(loop and loop[0].get("outputs"))),
        ("still exactly one work task for #1", a is not None, f"found {len(v.work(1))}"),
        ("no register rows under the fake session 'devloop'", not stray, str(stray)),
    ]


def s9(v: View) -> list[Row]:
    """A second wrap re-extracts the session's insights under fresh ids, so
    the rounds must match with credited notes compared by presence, and every
    credited note must still exist."""
    a = v.one_work(1)
    before = v.snap.get("before")
    mine = v.rounds_from(a, "S9")
    after = a["rounds"] if a else None
    credited = {x for r in after or [] for x in r.get("notes") or []}
    missing = sorted(credited - set(v.snap.get("note_ids", [])))
    return [
        ("S9 added a round to #1", bool(mine), f"{len(mine)} round(s) from S9"),
        ("a second wrap changes nothing on #1",
         before is not None and after is not None
         and _credit_blind(before["rounds"]) == _credit_blind(after),
         "rounds before the second wrap == rounds after" if before else "no s9-before.json"),
        ("every note #1 credits exists", not missing, f"missing={missing}"),
    ]


def _credit_blind(rounds: list[dict]) -> list[dict]:
    return [{**r, "notes": bool(r.get("notes"))} for r in rounds]


def s10(v: View) -> list[Row]:
    a = v.one_work(1)
    closes = [k for s in v.snap["sessions"].values() for k in s.get("closes", [])
              if a and k == a["id"]]
    return [
        ("#1 is closed", bool(a and a["status"] == "closed"), str(a and a["status"])),
        ("exactly one close row for #1", len(closes) == 1, f"{len(closes)}"),
    ]


def s11(v: View) -> list[Row]:
    rows: list[Row] = []
    a = v.one_work(1)
    if a:
        targets = {t for t, _e in v.snap["edges"].get(a["id"], [])}
        notes = {x for r in a["rounds"] for x in r.get("notes") or []}
        sess = {(r.get("session_ref") or {}).get("value") for r in a["rounds"]}
        rows += [
            ("graph reaches every credited insight", bool(notes) and notes <= targets,
             f"credited={sorted(notes)} missing={sorted(notes - targets)}"),
            ("graph reaches every session note",
             {s for s in sess if s and s.startswith("ses-")} <= targets,
             f"edges={sorted(targets)[:12]}"),
        ]
    open_ids = [i for i, t in v.tasks.items() if t["grain"] == "work" and t["status"] == "open"]
    rows.append(("session start lists every open work task",
                 all(i in v.snap["served"] for i in open_ids), f"open={open_ids}"))
    return rows


def s12(v: View) -> list[Row]:
    """PROBE: a Codex session starts a native subagent while working #2."""
    s = v.session("S12")
    b = v.one_work(2)
    opens = [o["task_id"] for o in s.get("opens", [])]
    return _probe([
        ("Codex subagent opened a helper task", bool(opens), f"opens={opens}"),
        ("it closed", bool(opens) and set(opens) <= set(s.get("closes", [])),
         f"closes={s.get('closes')}"),
        ("#2 gained a Codex round", any((r.get("session_ref") or {}).get("harness") == "codex"
                                        for r in (b or {}).get("rounds", [])),
         str(b and [r.get("session_ref") for r in b["rounds"]])),
    ])


def s13(v: View) -> list[Row]:
    return _herdr_worker_probe(v, "S13", "codex")


def s14(v: View) -> list[Row]:
    return _herdr_worker_probe(v, "S14", "pi")


def s15(v: View) -> list[Row]:
    """PROBE: a Codex session continues #3 and wraps."""
    c = v.one_work(3)
    return _probe([
        ("still exactly one work task for #3", c is not None, f"found {len(v.work(3))}"),
        ("#3 gained a round from Codex", any((r.get("session_ref") or {}).get("harness") == "codex"
                                             for r in (c or {}).get("rounds", [])),
         str(c and [r.get("session_ref") for r in c["rounds"]])),
    ])


def s20(v: View) -> list[Row]:
    """A Claude session works sub-issue #256 with one committing subagent;
    its round lands on the nearest epic's task, #243's."""
    a = v.one_work(T243)
    mine = v.rounds_from(a, "S20")
    kids = [r | {"task": k["id"]} for k in (v.children(a["id"]) if a else []) for r in k["rounds"]]
    trailed = [r["task"] for r in kids
               if set((r.get("did") or {}).get("commits") or [])
               & set(v.snap["trailers"].get(r["task"], []))]
    # The child's own envelope row; a bare success/failure is the harness's claim.
    summed = [e["outcome"] for r in kids for e in r.get("envelopes") or []
              if e.get("outcome") not in (None, "success", "failure")]
    return _base(v, T243, 1, "S20") + _children_closed(v, a, 1, "S20") + [
        ("the S20 round names sub-issue #256", any(r.get("asked") == v.ref(T256) for r in mine),
         str([r.get("asked") for r in mine])),
        ("no work task for the sub-issue #256", not v.work(T256), f"found {len(v.work(T256))}"),
        ("a child round lists its Task-trailer commit", bool(trailed),
         f"trailers={v.snap['trailers']} rounds={[r.get('did') for r in kids]}"),
        ("a child round carries its summary envelope", bool(summed), str(summed)),
    ] + _recorded(v, "S20", "claude-code")


def s21(v: View) -> list[Row]:
    """#243 marked done closes its own task; the #184 epic's task stays open."""
    a, root = v.one_work(T243), v.one_work(T184)
    return [
        ("S21 wrote a round on the #243 task", bool(v.rounds_from(a, "S21")),
         f"{len(v.rounds_from(a, 'S21'))} round(s) from S21"),
        ("the #243 task is closed", bool(a and a["status"] == "closed"), str(a and a["status"])),
        ("the #184 task stays open", bool(root and root["status"] == "open"),
         str(root and root["status"])),
    ]


def s22(v: View) -> list[Row]:
    """A session edits a file under no ref and wraps without declaring a task."""
    out = v.snap.get("s22_wrap") or ""
    landing = [line.strip() for line in out.splitlines() if line.strip().startswith("landing:")]
    return [
        ("the wrap of S22 warns that it declared no task", "declared no task" in out,
         out.strip()[-300:] or "no s22-wrap.txt"),
        ("the wrap of S22 prints its landing line",
         bool(landing) and "(none)" not in landing[0], str(landing)),
    ]


def s23(v: View) -> list[Row]:
    """The S22 session re-wraps continuing the S20 task: its round moves there."""
    a = v.one_work(T243)
    elsewhere = [i for i, t in v.tasks.items()
                 if not (a and i == a["id"]) and v.rounds_from(t, "S23")]
    return [
        ("the S20 task holds the S22 session's round", bool(v.rounds_from(a, "S23")),
         f"{len(v.rounds_from(a, 'S23'))} round(s) from S22's session"),
        ("no other task keeps a round from the S22 session", not elsewhere, str(elsewhere)),
    ]


def s24(v: View) -> list[Row]:
    """A pi session on an OpenRouter model wraps. The mini run's last
    scenario, so its checkpoint also reads the S20–S23 trace back."""
    s = v.session("S24")
    return [
        ("the throwaway index holds the S24 session note", bool(s.get("id")), str(s.get("id"))),
    ] + _recorded(v, "S24", "pi") + _readback(v)


def _recorded(v: View, label: str, harness: str) -> list[Row]:
    s = v.session(label)
    got = s.get("recorded") or {}
    want = s.get("want_model", "").rpartition("/")[2]
    return [
        (f"the {label} session note records harness {harness}", got.get("harness") == harness,
         str(got)),
        (f"the {label} session note records its model",
         bool(got.get("model")) and want in got["model"], f"{got.get('model')!r} wants {want!r}"),
    ]


def _readback(v: View) -> list[Row]:
    a = v.one_work(T243)
    kids = {k["id"] for k in v.children(a["id"])} if a else set()
    reached = set(v.snap["graph"].get(a["id"], [])) if a else set()
    refs = {x for label in ("S20", "S21", "S22", "S23")
            for x in (v.session(label).get("key"), v.session(label).get("id")) if x}
    decs = [d for d in v.snap["decisions"] if refs & set(d["derived_from"])]
    return [
        ("read-back: weave_graph from the #243 task reaches its children",
         bool(kids) and kids <= reached, f"children={sorted(kids)} missing={sorted(kids - reached)}"),
        ("read-back: the mini run's decisions carry the #243 task id",
         bool(decs) and all(a and d["task_id"] == a["id"] for d in decs), str(decs)),
    ]


CHECKS = {f"S{i}": f for i, f in enumerate(
    [s0, s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14, s15])} | {
    "S20": s20, "S21": s21, "S22": s22, "S23": s23, "S24": s24}


def open_rows_closed(v: View, label: str) -> tuple[bool, str]:
    """Whether every dispatch the session opened has a close; work-grain
    tasks stay open by design, so their opens are skipped."""
    s = v.session(label)
    unclosed = [o["task_id"] for o in s.get("opens", [])
                if o.get("grain") != "work" and o["task_id"] not in s.get("closes", [])]
    return not unclosed, f"unclosed={unclosed}"


def _base(v: View, n: int | str, rounds: int, label: str) -> list[Row]:
    a = v.one_work(n)
    have = len(a["rounds"]) if a else -1
    tag = n if isinstance(n, str) else f"#{n}"
    rows: list[Row] = [
        (f"exactly one work task for {tag}", a is not None, f"found {len(v.work(n))}"),
        (f"{tag} has at least {rounds} round(s)", have >= rounds, f"has {have}"),
        (f"{tag} conforms to the contract", bool(a) and not a["errors"], str(a and a["errors"])),
    ]
    if a:
        mine = v.rounds_from(a, label)
        rows += [
            (f"{label} wrote a round on {tag} under its session note", bool(mine),
             f"{len(mine)} round(s) from {label}"),
            ("body has one wikilink line per round",
             sum("[[" in line for line in a["body"]) >= len(a["rounds"]),
             f"{len(a['body'])} body lines"),
        ]
    return rows


def _children_closed(v: View, parent: dict | None, expect: int, label: str) -> list[Row]:
    kids = v.children(parent["id"]) if parent else []
    closed, unclosed = open_rows_closed(v, label)
    return [
        (f"at least {expect} helper task(s) attached", len(kids) >= expect,
         f"{len(kids)} attached"),
        ("every attached helper is closed and not orphaned",
         all(k["status"] == "closed" and not k.get("orphan") for k in kids),
         str([(k["id"], k["status"], k.get("orphan")) for k in kids])),
        ("every helper round carries a digest (tools counted)",
         all(any(r.get("tools") for r in k["rounds"]) for k in kids),
         str([[sorted(r) for r in k["rounds"]] for k in kids])),
        ("each dispatch open in the session's register has a close", closed, unclosed),
    ]


def _herdr_worker_probe(v: View, label: str, harness: str) -> list[Row]:
    task_id = v.session(label).get("task_id", "")
    t = v.tasks.get(task_id)
    return _probe([
        (f"{harness} worker's helper task exists", t is not None,
         task_id or "no task id recorded"),
        ("it was bound to the worker's transcript", bool(t and any(
            (r.get("session_ref") or {}).get("kind") == "session_id" for r in t["rounds"])),
         str(t and t["rounds"])),
        ("it closed with a digest",
         bool(t and t["status"] == "closed" and any(r.get("tools") for r in t["rounds"])),
         str(t and t["status"])),
    ])


def _probe(rows: list[Row]) -> list[Row]:
    return [(f"PROBE {name}", ok, ev) for name, ok, ev in rows]


# ---------------------------------------------------------------------------
# Command line


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="the eval run root that drive.sh setup made")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="PASS/FAIL/KNOWN/GAP per assertion; exit 1 on a FAIL")
    check.add_argument("labels", nargs="*", help="checkpoints to run (default: every one)")
    sub.add_parser("snapshot", help="the whole state as JSON")
    freeze = sub.add_parser("freeze", help="keep the state a scenario ended on for its check")
    freeze.add_argument("label")
    args = parser.parse_args(argv)

    if args.cmd == "snapshot":
        print(json.dumps(snapshot(args.root), indent=1, default=str))
        return 0
    if args.cmd == "freeze":
        path = args.root / "frozen" / f"{args.label}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(snapshot(args.root), indent=1, default=str))
        return 0
    failed, live = 0, None
    ran = json.loads((args.root / "state.json").read_text())["labels"] if not args.labels else {}
    for label in args.labels or [label for label in CHECKS if label in ran]:
        frozen = args.root / "frozen" / f"{label}.json"
        if frozen.exists():
            view = View(json.loads(frozen.read_text()))
        else:
            live = live or View(snapshot(args.root))
            view = live
        session = view.session(label)
        who = "/".join(x for x in (session.get("key"), session.get("id")) if x) or "no session"
        for name, ok, evidence in CHECKS[label](view):
            verdict = mark(name, ok)
            failed += verdict == "FAIL"
            print(f"{label:<4} {verdict:<10} {who}  {name}\n                {evidence}")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Snapshot: the throwaway vault's index, the session registers, the driver state


def snapshot(root: Path) -> dict:
    """Everything a checkpoint reads, from the throwaway vault named in the
    run state. Refuses to run when config would resolve another vault."""
    state = json.loads((root / "state.json").read_text())
    cfg = _throwaway_config(state)

    import sqlite3

    from thinkweave.core.buffer import buffer_path
    from thinkweave.core.events import iter_jsonl
    from thinkweave.core.indexer import Indexer
    from thinkweave.retrieval.context import build_project_context
    from thinkweave.retrieval.search import Search

    Indexer(config=cfg).rebuild()
    db = sqlite3.connect(str(cfg.index_db))
    db.row_factory = sqlite3.Row
    before = root / "s9-before.json"
    s22_wrap = root / "s22-wrap.txt"
    live = Path(state["live_vault"]) / "projects" / state["project"]
    tasks = _tasks(cfg, db)
    search = Search(config=cfg)
    return {
        "repo": f"github:{state['repo']}",
        "project": state["project"],
        "tasks": tasks,
        "sessions": {label: _session(cfg, db, d) for label, d in state["labels"].items()},
        "feedback": _feedback(db),
        "edges": _edges(db),
        "note_ids": [r[0] for r in db.execute("SELECT id FROM notes")],
        "served": build_project_context(cfg, state["project"]),
        "devloop_buffer": [
            r.get("task_id") for r in iter_jsonl(buffer_path(cfg.weave_dir, "devloop"))
        ],
        "before": json.loads(before.read_text()) if before.exists() else None,
        "s22_wrap": s22_wrap.read_text() if s22_wrap.exists() else "",
        "trailers": _trailers(state["sandbox"]),
        "graph": {i: [n.id for n in search.get_related(i, depth=1)]
                  for i, t in tasks.items() if t["grain"] == "work"},
        "decisions": _decisions(db),
        "live_leak": str(live) if live.exists() else "",
    }


def _throwaway_config(state: dict):
    os.environ["THINKWEAVE_VAULT"] = state["vault"]
    os.environ["THINKWEAVE_PROJECT"] = state["project"]
    os.environ.pop("THINKWEAVE_WEAVE_DIR", None)
    from thinkweave.core.config import load_config

    cfg = load_config()
    if cfg.vault_root.resolve() != Path(state["vault"]).resolve():
        sys.exit(f"oracle: config resolved {cfg.vault_root}, not the throwaway {state['vault']}")
    return cfg


def _tasks(cfg, db) -> dict:
    from thinkweave.core.task_contract import validate_task_note
    from thinkweave.core.vault import parse_frontmatter

    tasks = {}
    for row in db.execute(
        "SELECT id, path, project FROM notes WHERE json_extract(frontmatter, '$.kind') = 'task'"
    ):
        path = cfg.vault_root / row["path"]
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        tasks[row["id"]] = {
            **{k: fm.get(k) for k in ("grain", "status", "asked", "parent", "orphan", "title")},
            "rounds": fm.get("rounds") or [],
            "body": [line for line in body.splitlines() if line.strip()],
            "errors": validate_task_note(fm),
            "path": str(path),
            "project": row["project"] or "",
        }
    return tasks


def _session_key(session: str) -> str:
    """The harness session id a driven label recorded. pi records its
    transcript path (``<ts>_<uuid>.jsonl``); the note keys on the uuid."""
    if session.endswith(".jsonl"):
        return Path(session).stem.rsplit("_", 1)[-1]
    return session


def _session(cfg, db, driven: dict) -> dict:
    """One driven session: its note id, task rows and verdict count."""
    from thinkweave.core.buffer import archived_events_path, buffer_path
    from thinkweave.core.events import feedback_events, iter_jsonl
    from thinkweave.operations.tasks import Register

    key = _session_key(driven.get("session", ""))
    note = db.execute(
        "SELECT id, path, frontmatter FROM notes WHERE type = 'session' AND frontmatter LIKE ?",
        (f'%"source_session": "{key}"%',),
    ).fetchone() if key else None
    folder = (cfg.vault_root / note["path"]).parent if note else None
    streams = ([archived_events_path(folder)] if folder else []) + (
        [buffer_path(cfg.weave_dir, key)] if key else [])
    rows = Register(cfg, key, streams).rows() if key else []
    fm = json.loads(note["frontmatter"]) if note else {}
    return {
        **driven,
        "key": key,
        "id": note["id"] if note else "",
        "recorded": {"harness": fm.get("harness", ""), "model": fm.get("model", "")},
        "events": sum(1 for st in streams for _ in iter_jsonl(st)),
        "opens": [{"task_id": r.get("task_id"), "grain": r.get("grain")}
                  for r in rows if r["type"] == "task_open"],
        "closes": [r.get("task_id") for r in rows if r["type"] == "task_close"],
        "verdicts": sum(len(feedback_events(st)) for st in streams),
    }


def _feedback(db) -> list[dict]:
    return [
        {"id": r["id"],
         "feedback_for": json.loads(r["ff"]) if r["ff"].startswith("[") else [r["ff"]]}
        for r in db.execute(
            "SELECT id, json_extract(frontmatter, '$.feedback_for') AS ff FROM notes "
            "WHERE json_extract(frontmatter, '$.feedback_for') IS NOT NULL"
        )
    ]


def _decisions(db) -> list[dict]:
    out = []
    for r in db.execute(
        "SELECT id, json_extract(frontmatter, '$.task_id') AS task_id, "
        "json_extract(frontmatter, '$.derived_from') AS df FROM notes WHERE type = 'decision'"
    ):
        df = r["df"] or "[]"
        out.append({"id": r["id"], "task_id": r["task_id"],
                    "derived_from": json.loads(df) if df.startswith("[") else [df]})
    return out


def _trailers(sandbox: str) -> dict:
    """Each task id's commits in the sandbox repo, by their ``Task:`` trailer."""
    log = subprocess.run(
        ["git", "-C", sandbox, "log", "--all",
         "--format=%H %(trailers:key=Task,valueonly,separator=%x20)"],
        capture_output=True, text=True, check=True,
    ).stdout
    found: dict[str, list[str]] = {}
    for sha, *ids in (line.split() for line in log.splitlines() if line.strip()):
        for task_id in ids:
            found.setdefault(task_id, []).append(sha)
    return found


def _edges(db) -> dict:
    out: dict[str, list] = {}
    for r in db.execute(
        "SELECT source, target, edge_type FROM edges WHERE source LIKE 'tsk-%' "
        "AND edge_type NOT LIKE '%concept%' AND edge_type NOT LIKE '%tag%'"
    ):
        out.setdefault(r["source"], []).append([r["target"], r["edge_type"]])
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
