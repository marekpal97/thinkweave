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


def test_marks_split_new_bugs_from_known_gaps_and_probes():
    assert oracle.mark("the round records the commit", False) == "KNOWN #228"
    assert oracle.mark("PROBE it closed", False) == "GAP"
    assert oracle.mark("#1 conforms to the contract", False) == "FAIL"
    assert oracle.mark("#1 conforms to the contract", True) == "PASS"
