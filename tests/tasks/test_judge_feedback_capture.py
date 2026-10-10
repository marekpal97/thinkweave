"""The outcome judge files a PR's human feedback as notes edged
``feedback_for`` the task that holds the trajectory's round."""

from __future__ import annotations

from datetime import datetime, timezone

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.task_contract import Round, SessionRef
from thinkweave.core.vault import VaultManager, parse_frontmatter
from thinkweave.operations import tasks
from thinkweave.operations.trajectory_outcome import FollowupCommit, judge_trajectories

PR_URL = "https://github.com/o/r/pull/7"
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
AGENT = {"name": "Claude Opus 5.5", "email": "noreply@anthropic.com"}
HUMAN = {"name": "Marek", "email": "marek@example.com"}

# `gh pr view` merged into the `gh api` reviews and review comments.
PR = {
    "number": 7,
    "state": "MERGED",
    "mergedAt": "2026-09-01T00:00:00Z",
    "mergeCommit": {"oid": "m" * 40},
    "commits": [{"oid": "a1", "authors": [AGENT]}],
    "reviews": [
        {"html_url": f"{PR_URL}#pullrequestreview-1", "state": "COMMENTED", "body": ""},
    ],
    "reviewComments": [
        {
            "html_url": f"{PR_URL}#discussion_r1",
            "path": "src/pkg/a.py",
            "body": "This docstring hides the flow.",
        },
    ],
}

SIGNALS = {
    "total_lines": 10,
    "surviving_lines": 10,
    "reverted": False,
    "followup_commits": [
        FollowupCommit("h" * 40, "Rewrite the parser", (HUMAN,), ("src/pkg/a.py", "src/pkg/b.py")),
        FollowupCommit("c" * 40, "Agent touch-up", (HUMAN, AGENT), ("src/pkg/a.py",)),
    ],
}


def judge(cfg):
    return judge_trajectories(
        cfg,
        now=NOW,
        pr_fetcher=lambda url: PR,
        signals_fetcher=lambda pr: SIGNALS,
        issue_state=lambda repo, number: "open",
    )


def setup(cfg):
    """A trajectory note and the closed task whose round names it; returns
    ``(task id, trajectory path)``."""
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    traj = vm.create_note(
        NoteType.NOTE, title="loop trajectory #7", tags=["loop-run"],
        extra_frontmatter={"pr_url": PR_URL, "outcome": "shipped"},
    )
    idx = Indexer(config=cfg)
    idx.index_file(traj)
    idx.close()
    traj_id = parse_frontmatter(traj.read_text(encoding="utf-8"))[0]["id"]
    task = tasks.TaskStore(cfg).mint("work", "issue 7", "t", asked="github:o/r#7")
    task.put_round(Round.from_dict(
        {"route": "devloop", "session_ref": SessionRef.note("devloop", traj_id).to_dict()},
        grain="work",
    ))
    task.close()
    task.save(vm)
    return task.id, traj


def feedback_notes(cfg) -> dict[str, dict]:
    """{ref: frontmatter} for every note carrying a feedback ``source``."""
    out = {}
    for path in cfg.vault_root.rglob("*.md"):
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm.get("source") in ("pr-review", "post-merge-commit"):
            out[fm["ref"]] = fm
    return out


def feedback_edges(cfg) -> set[tuple[str, str]]:
    idx = Indexer(config=cfg)
    try:
        rows = idx.db.execute(
            "SELECT source, target FROM edges WHERE edge_type = 'feedback_for'"
        ).fetchall()
    finally:
        idx.close()
    return {(r[0], r[1]) for r in rows}


def test_review_comment_and_human_commit_land_as_feedback_notes(cfg):
    task_id, _ = setup(cfg)
    before = tasks.TaskStore(cfg).get(task_id).path.read_text(encoding="utf-8")
    judge(cfg)

    notes = feedback_notes(cfg)
    assert set(notes) == {
        f"{PR_URL}#discussion_r1",
        f"https://github.com/o/r/commit/{'h' * 40}",
    }
    review = notes[f"{PR_URL}#discussion_r1"]
    assert (review["source"], review["files"]) == ("pr-review", ["src/pkg/a.py"])
    commit = notes[f"https://github.com/o/r/commit/{'h' * 40}"]
    assert (commit["source"], commit["files"]) == (
        "post-merge-commit", ["src/pkg/a.py", "src/pkg/b.py"],
    )
    assert feedback_edges(cfg) == {(fm["id"], task_id) for fm in notes.values()}
    assert tasks.TaskStore(cfg).get(task_id).path.read_text(encoding="utf-8") == before


def test_second_judge_run_writes_no_new_note(cfg):
    _, traj = setup(cfg)
    judge(cfg)
    first = feedback_notes(cfg)
    assert judge(cfg)["feedback"] == []
    # Re-judging both phases from scratch still finds every note by its ref.
    VaultManager(config=cfg).update_note(traj, frontmatter_updates={"prediction_history": []})
    rejudged = judge(cfg)
    assert [j["phase"] for j in rejudged["judged"]] == [1, 2]
    assert rejudged["feedback"] == []
    assert feedback_notes(cfg) == first


def test_failed_followup_fetch_leaves_phase_two_for_the_next_run(cfg):
    setup(cfg)

    def failing(pr):
        raise RuntimeError("git log failed")

    result = judge_trajectories(
        cfg, now=NOW, pr_fetcher=lambda url: PR, signals_fetcher=failing,
        issue_state=lambda repo, number: "open",
    )
    assert [j["phase"] for j in result["judged"]] == [1]
    assert "git log failed" in result["errors"][0]["reason"]
    assert [f["source"] for f in judge(cfg)["feedback"]] == ["post-merge-commit"]
