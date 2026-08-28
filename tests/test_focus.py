"""``operations.focus`` — behavioral project focus (#170).

Seam: ``focus.active_projects()`` — the one computed focus signal, shared
by ``/dream`` and ``/brief`` so both agree on a single definition. The
concept-level merge (asked ▸ done ▸ declared) is the narrating model's
judgment over raw substrates (dec-696bacfb) and has no Python seam to test.
"""

from __future__ import annotations

from datetime import datetime, timezone

from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.operations import focus
from thinkweave.operations.dream import scan

NOW = datetime.now(timezone.utc)


def test_active_projects_matches_dream(vault_factory):
    tv = vault_factory()
    tv.with_note("alpha work", note_type=NoteType.SESSION, project="alpha")
    tv.with_note("meta", note_type=NoteType.SESSION, project="_unscoped")
    prio = tv.config.vault_root / "config" / "PRIORITIES.yaml"
    prio.parent.mkdir(parents=True, exist_ok=True)
    prio.write_text("focus:\n  active_projects: [pinned-proj]\n", encoding="utf-8")
    tv.indexed()

    dream_ap = scan(tv.config, project="t").knowledge_delta["active_focus"]["active_projects"]
    idx = Indexer(config=tv.config)
    try:
        ours = focus.active_projects(
            idx.db, now=NOW, window_days=tv.config.salience_activity_window_days,
            pins=["pinned-proj"],
        )
    finally:
        idx.close()
    assert ours == dream_ap == ["alpha", "pinned-proj"]


def test_active_projects_excludes_meta_buckets_and_caps(vault_factory):
    tv = vault_factory()
    for name in ("a", "b", "_personal"):
        tv.with_note(f"{name} work", note_type=NoteType.SESSION, project=name)
    tv.indexed()
    idx = Indexer(config=tv.config)
    try:
        out = focus.active_projects(idx.db, now=NOW, window_days=14, pins=[])
    finally:
        idx.close()
    assert "_personal" not in out
    assert set(out) == {"a", "b"}
