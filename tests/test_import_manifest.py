"""Unit tests for the shared ImportManifest helper.

Three importers (chatgpt, claude_history, messenger) each hand-rolled a
structurally-identical manifest load/save. This pins the shared helper's
behaviour: load-missing, save, mark-imported, roundtrip, and the
configurable id-field (messenger keys its map ``imported_urls`` while the
others use ``imported_ids``).
"""

from __future__ import annotations

import json
from pathlib import Path

from thinkweave.acquisition.importers.common import ImportManifest


def test_load_missing_returns_empty_manifest(tmp_path: Path):
    m = ImportManifest.load(tmp_path, "does_not_exist.json")
    assert m.data["version"] == 1
    assert m.ids == {}
    assert m.is_imported("anything") is False


def test_mark_then_is_imported(tmp_path: Path):
    m = ImportManifest.load(tmp_path, "chatgpt_import.json")
    assert m.is_imported("conv-001") is False
    m.mark("conv-001", "n-abc123")
    assert m.is_imported("conv-001") is True
    assert m.ids["conv-001"] == "n-abc123"


def test_save_and_reload_roundtrip(tmp_path: Path):
    m = ImportManifest.load(tmp_path, "claude_mem_migration.json")
    m.mark("session-xyz", "n-def456")
    m.save()

    reloaded = ImportManifest.load(tmp_path, "claude_mem_migration.json")
    assert reloaded.ids["session-xyz"] == "n-def456"
    assert reloaded.is_imported("session-xyz") is True


def test_save_writes_indented_json_at_expected_path(tmp_path: Path):
    m = ImportManifest.load(tmp_path, "messenger_import.json", id_field="imported_urls")
    m.mark("https://arxiv.org/abs/1", "note-1.md")
    m.save()

    path = tmp_path / "messenger_import.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["imported_urls"]["https://arxiv.org/abs/1"] == "note-1.md"


def test_custom_id_field_for_messenger(tmp_path: Path):
    """Messenger keys its dedup map ``imported_urls`` instead of ``imported_ids``."""
    m = ImportManifest.load(tmp_path, "messenger_import.json", id_field="imported_urls")
    assert m.ids == {}
    m.mark("https://x", "queue-note.md")
    m.save()

    raw = json.loads((tmp_path / "messenger_import.json").read_text(encoding="utf-8"))
    assert "imported_urls" in raw
    assert "imported_ids" not in raw


def test_load_existing_preserves_prior_entries(tmp_path: Path):
    path = tmp_path / "chatgpt_import.json"
    path.write_text(
        json.dumps({"version": 1, "imported_ids": {"old": "n-1"}}),
        encoding="utf-8",
    )
    m = ImportManifest.load(tmp_path, "chatgpt_import.json")
    assert m.is_imported("old") is True
    m.mark("new", "n-2")
    m.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["imported_ids"] == {"old": "n-1", "new": "n-2"}


def test_set_meta_persists_extra_keys(tmp_path: Path):
    m = ImportManifest.load(tmp_path, "chatgpt_import.json")
    m.mark("conv-1", "n-1")
    m.set_meta(completed_at="2026-07-07T00:00:00Z", source_file="/tmp/conversations.json")
    m.save()

    raw = json.loads((tmp_path / "chatgpt_import.json").read_text(encoding="utf-8"))
    assert raw["completed_at"] == "2026-07-07T00:00:00Z"
    assert raw["source_file"] == "/tmp/conversations.json"
    assert raw["imported_ids"]["conv-1"] == "n-1"


def test_save_creates_parent_directory(tmp_path: Path):
    """weave_dir may not exist yet; save() must create it."""
    weave_dir = tmp_path / "not" / "yet" / "there"
    m = ImportManifest.load(weave_dir, "chatgpt_import.json")
    m.mark("conv-1", "n-1")
    m.save()
    assert (weave_dir / "chatgpt_import.json").exists()


def test_corrupt_manifest_degrades_to_empty_with_a_warning(
    tmp_path: Path, capsys
):
    """A truncated ledger must not abort the import.

    Re-importing content is visible and fixable; a traceback leaves the user
    with no way to run the importer at all.
    """
    (tmp_path / "codex.json").write_text('{"version": 1, "imported_i', encoding="utf-8")

    m = ImportManifest.load(tmp_path, "codex.json")

    assert m.ids == {}
    assert m.is_imported("anything") is False
    assert "unreadable" in capsys.readouterr().err


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path: Path):
    """Importers save mid-run; a half-written ledger would fail to load and
    re-import everything."""
    m = ImportManifest.load(tmp_path, "codex.json")
    m.mark("rollout-1", "ses-abc123")
    m.save()

    assert json.loads((tmp_path / "codex.json").read_text(encoding="utf-8"))[
        "imported_ids"
    ] == {"rollout-1": "ses-abc123"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_non_dict_manifest_is_treated_as_empty(tmp_path: Path):
    (tmp_path / "codex.json").write_text("[]", encoding="utf-8")

    assert ImportManifest.load(tmp_path, "codex.json").ids == {}
