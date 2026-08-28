"""``weave brief mark`` — /brief's one deterministic write (#170).

Seams: the ``context_served(source='brief')`` rows ``served.mark`` writes
(rows keyed by ``ses-`` id, the durable buffer twin keyed by
``source_session``; unresolvable → nothing) and the CLI parser shape.
The payload side has no collect — the skill composes ``weave health
--json`` + the ``weave_*`` retrieval tools (dec-696bacfb); coverage of
the health half lives in ``test_health.py``.
"""

from __future__ import annotations

import json

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations import served


def _rows(tv):
    idx = Indexer(config=tv.config)
    try:
        return [tuple(r) for r in idx.db.execute(
            "SELECT session_id, note_id, source FROM context_served ORDER BY note_id"
        )]
    finally:
        idx.close()


def test_mark_logs_context_served_and_durable_buffer_twin(vault_factory):
    tv = vault_factory()
    path = tv.vault.create_note(NoteType.SESSION, "live", project="p",
                                extra_frontmatter={"source_session": "cc-1"})
    tv.indexed()
    ses_id = tv.vault.read_note(path).id

    written = served.mark(tv.config, "brief", "cc-1", "dig-x", ["n-aaaaaaaa", "dec-bbbbbbbb"])
    assert written == 2
    assert _rows(tv) == [
        (ses_id, "dec-bbbbbbbb", "brief"), (ses_id, "n-aaaaaaaa", "brief")
    ]
    # Durable twin: the buffer event the indexer re-projects on rebuild.
    buf = (tv.config.weave_dir / "buffer" / "cc-1.jsonl").read_text(encoding="utf-8")
    ev = json.loads(buf.strip().splitlines()[-1])
    assert ev["type"] == "retrieval" and ev["tool"] == "brief"
    assert ev["returned_ids"] == ["n-aaaaaaaa", "dec-bbbbbbbb"]


def test_mark_resolves_harness_uuid_to_session_note(vault_factory):
    tv = vault_factory()
    path = tv.vault.create_note(NoteType.SESSION, "live", project="p",
                                extra_frontmatter={"source_session": "11111111-uuid"})
    tv.indexed()
    ses_id = tv.vault.read_note(path).id

    assert served.mark(tv.config, "brief", "11111111-uuid", "dig-x", ["n-aaaaaaaa"]) == 1
    assert _rows(tv) == [(ses_id, "n-aaaaaaaa", "brief")]
    buf = tv.config.weave_dir / "buffer" / "11111111-uuid.jsonl"
    assert json.loads(buf.read_text().strip())["returned_ids"] == ["n-aaaaaaaa"]


def test_mark_unresolvable_session_writes_nothing(vault_factory):
    tv = vault_factory()
    tv.indexed()
    assert served.mark(tv.config, "brief", "", "dig-x", ["n-aaaaaaaa"]) == 0
    assert served.mark(tv.config, "brief", "nope-uuid", "dig-x", ["n-aaaaaaaa"]) == 0
    assert _rows(tv) == []
    if (tv.config.weave_dir / "buffer").exists():
        assert not list((tv.config.weave_dir / "buffer").glob("*.jsonl"))


def test_mark_with_session_note_id_names_buffer_by_source_session(vault_factory):
    tv = vault_factory()
    path = tv.vault.create_note(NoteType.SESSION, "live", project="p",
                                extra_frontmatter={"source_session": "2222-uuid"})
    tv.indexed()
    ses_id = tv.vault.read_note(path).id
    assert served.mark(tv.config, "brief", ses_id, "dig-x", ["n-aaaaaaaa"]) == 1
    assert _rows(tv) == [(ses_id, "n-aaaaaaaa", "brief")]
    assert (tv.config.weave_dir / "buffer" / "2222-uuid.jsonl").exists()
    assert not (tv.config.weave_dir / "buffer" / f"{ses_id}.jsonl").exists()


def test_bare_brief_requires_a_subaction():
    import pytest

    from thinkweave.surfaces.cli.parser import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["brief"])


def test_brief_collect_is_gone():
    """dec-696bacfb: no payload collect — the skill composes existing tools."""
    import pytest

    from thinkweave.surfaces.cli.parser import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["brief", "collect"])
