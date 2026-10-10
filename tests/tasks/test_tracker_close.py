"""An open task keyed to a GitHub issue closes when the outcome judge finds
that issue closed."""

from __future__ import annotations

import subprocess

from thinkweave.core.vault import VaultManager
from thinkweave.operations import tasks
from thinkweave.operations.trajectory_outcome import judge_trajectories

ISSUES = {"1": "closed", "2": "open"}


def issue_state(repo: str, number: str) -> str:
    if number not in ISSUES:
        raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 404")
    return ISSUES[number]


def minted(cfg, asked: str) -> str:
    task = tasks.TaskStore(cfg).mint("work", f"task for {asked}", "t", asked=asked)
    task.save(VaultManager(config=cfg))
    return task.id


def status(cfg, task_id: str) -> str:
    return tasks.TaskStore(cfg).get(task_id).frontmatter["status"]


def test_trajectory_judge_closes_a_task_whose_issue_closed_once(cfg):
    task_id = minted(cfg, "github:o/r#1")
    first = judge_trajectories(cfg, issue_state=issue_state)
    assert first["closed_tasks"] == [task_id]
    assert status(cfg, task_id) == "closed"
    second = judge_trajectories(cfg, issue_state=issue_state)
    assert second["closed_tasks"] == []
    assert status(cfg, task_id) == "closed"


def test_trajectory_judge_leaves_open_issue_and_free_text_tasks_open(cfg):
    on_open = minted(cfg, "github:o/r#2")
    free = minted(cfg, "tidy the ledger renderer")
    result = judge_trajectories(cfg, issue_state=issue_state)
    assert result["closed_tasks"] == []
    assert (status(cfg, on_open), status(cfg, free)) == ("open", "open")


def test_trajectory_judge_lists_a_gh_failure_and_keeps_going(cfg):
    failing = minted(cfg, "github:o/r#3")
    closing = minted(cfg, "github:o/r#1")
    result = judge_trajectories(cfg, issue_state=issue_state)
    assert result["closed_tasks"] == [closing]
    assert [e["id"] for e in result["errors"]] == [failing]
    assert "HTTP 404" in result["errors"][0]["reason"]
    assert status(cfg, failing) == "open"


def test_trajectory_judge_closed_task_refuses_a_later_round(cfg):
    task_id = minted(cfg, "github:o/r#1")
    judge_trajectories(cfg, issue_state=issue_state)
    task = tasks.TaskStore(cfg).get(task_id)
    assert task.refusal("devloop").startswith("is closed")
