"""The oracle's assertion rules, on hand-built snapshots (no vault, no index).

    uv run --no-sync pytest evals/task360/test_oracle.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import oracle  # noqa: E402

REPO = "github:task360/sandbox"


def _round(session: str, **extra) -> dict:
    return {"route": "session", "session_ref": {"harness": "claude-code", "kind": "note",
                                                "value": session}, **extra}


def _snap(rounds: list[dict], opens: list[dict], closes: list[str], **extra) -> dict:
    work = {"grain": "work", "status": "open", "asked": f"{REPO}#1", "parent": None,
            "orphan": None, "rounds": rounds, "body": ["[[a]]"] * len(rounds), "errors": []}
    sessions = {
        label: {"key": f"uuid-{label}", "id": f"ses-{label}", "opens": opens, "closes": closes,
                "verdicts": 0}
        for label in ("S1", "S2", "S9")
    }
    return {"repo": REPO, "tasks": {"tsk-1": work}, "sessions": sessions,
            "feedback": [], "edges": {}, **extra}


def _marks(rows) -> dict[str, bool]:
    return {name: ok for name, ok, _ in rows}


def test_round_count_is_a_floor_not_an_exact_count():
    view = oracle.View(_snap([_round("ses-S1"), _round("ses-S2"), _round("ses-S9")], [], []))
    marks = _marks(oracle.s2(view))
    assert marks["#1 has at least 2 round(s)"] is True
    assert marks["S2 wrote a round on #1 under its session note"] is True


def test_an_unclosed_work_grain_open_is_not_a_missing_close():
    opens = [{"task_id": "tsk-1", "grain": "work"}]
    view = oracle.View(_snap([_round("ses-S1")], opens, []))
    assert oracle.open_rows_closed(view, "S1") == (True, "unclosed=[]")


def test_an_unclosed_dispatch_open_is_a_missing_close():
    opens = [{"task_id": "tsk-1", "grain": "work"}, {"task_id": "tsk-2", "grain": "per-dispatch"}]
    view = oracle.View(_snap([_round("ses-S1")], opens, []))
    assert oracle.open_rows_closed(view, "S1") == (False, "unclosed=['tsk-2']")


def test_s9_requires_its_own_round_and_an_idempotent_rewrap():
    rounds = [_round("ses-S1"), _round("ses-S9")]
    same = oracle.View(_snap(rounds, [], [], before={"rounds": rounds}))
    assert set(_marks(oracle.s9(same)).values()) == {True}

    no_round = oracle.View(_snap([_round("ses-S1")], [], [], before={"rounds": [_round("ses-S1")]}))
    marks = _marks(oracle.s9(no_round))
    assert marks["S9 added a round to #1"] is False
    assert marks["a second wrap changes nothing on #1"] is True



def test_s9_rewrap_may_remint_credited_insights_but_never_drop_them():
    before = [_round("ses-S1"), _round("ses-S9", notes=["n-old"])]
    reminted = [_round("ses-S1"), _round("ses-S9", notes=["n-new"])]
    view = oracle.View(_snap(reminted, [], [], before={"rounds": before}, note_ids=["n-new"]))
    assert set(_marks(oracle.s9(view)).values()) == {True}

    dangling = oracle.View(_snap(before, [], [], before={"rounds": before}, note_ids=[]))
    assert _marks(oracle.s9(dangling))["every note #1 credits exists"] is False

    dropped = [_round("ses-S1"), _round("ses-S9")]
    lost = oracle.View(_snap(dropped, [], [], before={"rounds": before}, note_ids=["n-old"]))
    assert _marks(oracle.s9(lost))["a second wrap changes nothing on #1"] is False

def test_marks_split_new_bugs_from_known_gaps_and_probes():
    assert oracle.mark("the round records the commit", False) == "KNOWN #228"
    assert oracle.mark("PROBE it closed", False) == "GAP"
    assert oracle.mark("#1 conforms to the contract", False) == "FAIL"
    assert oracle.mark("#1 conforms to the contract", True) == "PASS"


def test_check_scores_a_label_from_the_state_frozen_at_its_end(tmp_path, capsys):
    # No state.json: a live snapshot would fail, so a pass proves the frozen one was read.
    frozen = {"repo": REPO, "project": "p", "tasks": {}, "feedback": [], "edges": {},
              "live_leak": "",
              "sessions": {"S0": {"key": "uuid-S0", "id": "ses-S0", "events": 3}}}
    (tmp_path / "frozen").mkdir()
    (tmp_path / "frozen" / "S0.json").write_text(oracle.json.dumps(frozen))
    assert oracle.main(["--root", str(tmp_path), "check", "S0"]) == 0
    assert capsys.readouterr().out.count("PASS") == 3


# ---------------------------------------------------------------------------
# The mini run (S20–S24): refs name the real tree #184 → #243 → #256.

TREE = "github:marekpal97/thinkweave"


def _tree_snap(**over) -> dict:
    """The mini run's end state, every row met: S20 worked #256 onto the
    #243 task with one trailer-committing child, S21 closed it, S22's
    session re-wrapped onto it as S23, S24 ran on pi."""
    s20 = _round("ses-S20", asked=f"{TREE}#256")
    epic = {"grain": "work", "status": "closed", "asked": f"{TREE}#243", "parent": None,
            "orphan": None, "body": ["[[a]]"] * 3, "errors": [],
            "rounds": [s20, _round("ses-S22", asked=f"{TREE}#243"),
                       _round("ses-S21", asked=f"{TREE}#243", done=True)]}
    root = {"grain": "work", "status": "open", "asked": f"{TREE}#184", "parent": None,
            "rounds": [], "body": [], "errors": []}
    child = {"grain": "per-dispatch", "status": "closed", "asked": None, "parent": "tsk-243",
             "orphan": None, "rounds": [{"tools": {"Bash": 2}, "did": {"commits": ["c0ffee"]},
                                         "envelopes": [{"task_id": "tsk-kid",
                                                        "outcome": "wrote the greet test"}]}]}
    sessions = {
        "S20": {"key": "uuid-S20", "id": "ses-S20", "opens": [], "closes": [],
                "recorded": {"harness": "claude-code", "model": "claude-opus-5-5"}},
        "S21": {"key": "uuid-S21", "id": "ses-S21"},
        "S22": {"key": "uuid-S22", "id": "ses-S22"},
        "S23": {"key": "uuid-S22", "id": "ses-S22"},
        "S24": {"key": "uuid-S24", "id": "ses-S24",
                "want_model": "openrouter/deepseek/deepseek-v4-flash",
                "recorded": {"harness": "pi", "model": "deepseek/deepseek-v4-flash"}},
    }
    snap = {
        "repo": REPO, "tasks": {"tsk-243": epic, "tsk-184": root, "tsk-kid": child},
        "sessions": sessions, "feedback": [], "edges": {},
        "trailers": {"tsk-kid": ["c0ffee"]},
        "graph": {"tsk-243": ["tsk-kid", "ses-S20"]},
        "decisions": [{"id": "dec-1", "task_id": "tsk-243", "derived_from": ["ses-S20"]},
                      {"id": "dec-2", "task_id": "tsk-243", "derived_from": ["uuid-S22"]}],
        "s22_wrap": "  landing: DECISIONS.md, BACKLOG.md\n  warnings:\n    ~ the session "
                    "changed files but declared no task — its work lands on none\n",
    }
    for path, value in over.items():
        *keys, last = path.split(".")
        node = snap
        for k in keys:
            node = node[k]
        node[last] = value
    return snap


def _unmet(check, **over) -> set[str]:
    return {name for name, ok in _marks(check(oracle.View(_tree_snap(**over)))).items() if not ok}


def test_the_mini_run_end_state_meets_every_row():
    for check in (oracle.s20, oracle.s21, oracle.s22, oracle.s23, oracle.s24):
        assert _unmet(check) == set(), check.__name__


def test_s20_child_evidence_needs_a_trailer_tagged_commit_and_a_summary():
    untagged = _unmet(oracle.s20, **{"trailers": {}})
    assert untagged == {"a child round lists its Task-trailer commit"}
    claim_only = [{"task_id": "tsk-kid", "outcome": "success"}]
    assert _unmet(oracle.s20, **{"tasks.tsk-kid.rounds": [
        {"tools": {"Bash": 2}, "did": {"commits": ["c0ffee"]}, "envelopes": claim_only}]}) == {
        "a child round carries its summary envelope"}


def test_s20_round_must_name_the_sub_issue_on_the_epic_task():
    assert "the S20 round names sub-issue #256" in _unmet(
        oracle.s20, **{"tasks.tsk-243.rounds": [_round("ses-S20", asked=f"{TREE}#243")]})


def test_s21_done_on_243_must_leave_the_184_task_open():
    assert _unmet(oracle.s21, **{"tasks.tsk-184.status": "closed"}) == {
        "the #184 task stays open"}
    assert _unmet(oracle.s21, **{"tasks.tsk-243.status": "open"}) == {"the #243 task is closed"}


def test_s22_reads_the_no_task_warning_and_landing_line_from_the_wrap_output():
    assert _unmet(oracle.s22, s22_wrap="  landing: (none)\n") == {
        "the wrap of S22 warns that it declared no task", "the wrap of S22 prints its landing line"}


def test_s23_round_moved_only_when_no_other_task_keeps_it():
    stray = {"grain": "work", "status": "open", "asked": "fix the docstring",
             "rounds": [_round("ses-S22")]}
    assert _unmet(oracle.s23, **{"tasks.tsk-old": stray}) == {
        "no other task keeps a round from the S22 session"}


def test_s24_checks_pi_model_and_reads_the_trace_back():
    assert _unmet(oracle.s24, **{"sessions.S24.recorded.model": "qwen/qwen3-coder"}) == {
        "the S24 session note records its model"}
    assert _unmet(oracle.s24, **{"graph.tsk-243": ["ses-S20"]}) == {
        "read-back: weave_graph from the #243 task reaches its children"}
    unstamped = [{"id": "dec-1", "task_id": None, "derived_from": ["ses-S20"]}]
    assert _unmet(oracle.s24, decisions=unstamped) == {
        "read-back: the mini run's decisions carry the #243 task id"}
