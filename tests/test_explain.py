"""``weave explain`` seams: the explain-note contract and the idempotency lookup.

``validate_explain_note`` checks one note's frontmatter against its file;
``cmd_explain locate`` finds the note a re-run must update, keyed by
(session, scope, target).
"""

from __future__ import annotations

import argparse

import pytest

from thinkweave.core.schemas import NoteType
from thinkweave.surfaces.cli.explain import cmd_explain, validate_explain_note

UUID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"

GOOD_FM = {
    "type": "note",
    "kind": "explain",
    "scope": "diff",
    "target": "#281",
    "concepts": ["cli", "idempotency"],
}


def _explain(session: str, scope: str, target: str, title: str) -> dict:
    return {
        "title": title,
        "project": "thinkweave",
        "session_id": session,
        "extra_frontmatter": {
            "kind": "explain",
            "scope": scope,
            "target": target,
            "concepts": ["cli", "idempotency"],
        },
    }


@pytest.fixture
def sessions(vault_factory, monkeypatch):
    """Explain notes across two session folders, plus a plain note in one."""
    handle = vault_factory(
        notes=[
            _explain(UUID, "diff", "#281", "Explain: PR 281"),
            _explain(UUID, "session", "drift v2", "Explain: drift v2"),
            _explain(OTHER, "diff", "main..loop/x", "Explain: loop x"),
            {"title": "Unrelated", "project": "thinkweave", "session_id": UUID,
             "extra_frontmatter": {"scope": "diff", "target": "#281"}},
        ]
    ).indexed()
    monkeypatch.setattr("thinkweave.surfaces.cli.explain.load_config", lambda: handle.config)
    return handle


def _note_path(handle, note_id: str):
    return next(p for p in handle.dir.rglob("*.md") if f"id: {note_id}" in p.read_text())


def _locate(capsys, session: str, scope: str, target: str) -> str:
    cmd_explain(argparse.Namespace(
        explain_action="locate", session=session, scope=scope, target=target
    ))
    return capsys.readouterr().out.strip()


# ── contract ─────────────────────────────────────────────────────────


def test_valid_note_has_no_problems(tmp_path):
    assert validate_explain_note(GOOD_FM, tmp_path / "n.md") == []


def test_invalid_note_reports_every_problem(tmp_path):
    bad = {"kind": "learn", "scope": "concept", "target": " ", "concepts": ["cli"]}
    problems = validate_explain_note(bad, tmp_path / "n.md")
    assert len(problems) == 4
    for word in ("kind", "scope", "target", "concepts"):
        assert any(word in p for p in problems)


def test_html_path_needs_a_sibling_file(tmp_path):
    fm = dict(GOOD_FM, html_path="explain-pr-281.html")
    note = tmp_path / "explain-pr-281.md"
    problems = validate_explain_note(fm, note)
    assert len(problems) == 1 and "html_path" in problems[0]

    (tmp_path / "explain-pr-281.html").write_text("<html></html>")
    assert validate_explain_note(fm, note) == []


# ── locate ───────────────────────────────────────────────────────────


def test_locate_finds_the_note_for_its_key(sessions, capsys):
    found = _locate(capsys, UUID, "diff", "#281")
    assert found.startswith("n-")
    assert "PR 281" in _note_path(sessions, found).read_text()


def test_locate_matches_the_session_note_id_too(sessions, capsys):
    folder = _note_path(sessions, _locate(capsys, UUID, "diff", "#281")).parent
    (folder / "session.md").write_text(
        f"---\ntype: session\nid: ses-0000aaaa\nsource_session: {UUID}\n---\n"
    )
    assert _locate(capsys, "ses-0000aaaa", "diff", "#281") == _locate(capsys, UUID, "diff", "#281")


@pytest.mark.parametrize(
    "session, scope, target",
    [
        (OTHER, "diff", "#281"),      # same key in another session
        (UUID, "session", "#281"),    # same target, other scope
        (UUID, "diff", "#282"),       # same scope, other target
    ],
)
def test_locate_prints_nothing_without_a_match(sessions, capsys, session, scope, target):
    assert _locate(capsys, session, scope, target) == ""


# ── check ────────────────────────────────────────────────────────────


def test_check_exits_zero_on_a_valid_note(sessions, capsys):
    note_id = _locate(capsys, UUID, "diff", "#281")
    with pytest.raises(SystemExit) as exc:
        cmd_explain(argparse.Namespace(explain_action="check", note=note_id))
    assert exc.value.code == 0


def test_check_exits_non_zero_on_a_missing_html_sibling(sessions, capsys):
    from thinkweave.core.indexer import Indexer

    note_id = _locate(capsys, UUID, "diff", "#281")
    path = _note_path(sessions, note_id)
    path.write_text(path.read_text().replace("kind: explain", "kind: explain\nhtml_path: gone.html", 1))
    idx = Indexer(config=sessions.config)
    idx.index_file(path)
    idx.close()

    with pytest.raises(SystemExit) as exc:
        cmd_explain(argparse.Namespace(explain_action="check", note=note_id))
    assert exc.value.code == 1
    assert "html_path" in capsys.readouterr().out
