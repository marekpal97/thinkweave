"""C24 — CLI parity for weave_unlink / weave_timeline / weave_project_snapshot
/ weave_prompts.

Each test calls the cmd_* handler directly with a fixture-vault and
captures stdout, mirroring the pattern used by test_dream.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.surfaces.cli.parity import (
    cmd_project_snapshot,
    cmd_prompts,
    cmd_timeline,
    cmd_unlink,
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    vault = tmp_path / "vault"
    monkeypatch.setenv("THINKWEAVE_VAULT", str(vault))
    monkeypatch.setenv("THINKWEAVE_PROJECT", "t")
    return Config(vault_root=vault, default_project="t")


@pytest.fixture
def vault(cfg: Config) -> VaultManager:
    vm = VaultManager(config=cfg)
    vm.ensure_dirs()
    return vm


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class TestUnlink:
    def test_unlinks_existing_edge(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        from thinkweave.core.vault import parse_frontmatter
        from thinkweave.operations.notes import link_notes

        a = vault.create_note(NoteType.NOTE, "A", project="t")
        b = vault.create_note(NoteType.NOTE, "B", project="t")
        a_id = parse_frontmatter(a.read_text(encoding="utf-8"))[0]["id"]
        b_id = parse_frontmatter(b.read_text(encoding="utf-8"))[0]["id"]
        idx = Indexer(config=cfg)
        idx.rebuild(full=True)
        idx.close()
        # link_notes is the canonical pre-state: it writes the edge into
        # frontmatter AND re-indexes, so the unlink lookup finds the source.
        link_notes(cfg, a_id, b_id, "relates_to")

        cmd_unlink(_args(source=a_id, target=b_id, type="relates_to"))
        out = capsys.readouterr().out
        assert "Unlinked" in out

    def test_missing_source_note_exits_one(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        # No notes seeded → the index doesn't contain n-aaa.
        Indexer(config=cfg).rebuild(full=True)
        with pytest.raises(SystemExit) as exc:
            cmd_unlink(
                _args(source="n-aaa", target="n-bbb", type="relates_to")
            )
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()


class TestTimeline:
    def test_with_project_lists_recent_sessions(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        recent = date.today().isoformat()
        vault.create_note(
            NoteType.SESSION, "Recent work", project="t",
            extra_frontmatter={"date": recent},
        )
        Indexer(config=cfg).rebuild(full=True)
        cmd_timeline(_args(project="t", days=7, json=False))
        out = capsys.readouterr().out
        assert "Recent work" in out

    def test_json_format(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        recent = date.today().isoformat()
        vault.create_note(
            NoteType.SESSION, "Recent", project="t",
            extra_frontmatter={"date": recent},
        )
        Indexer(config=cfg).rebuild(full=True)
        cmd_timeline(_args(project="t", days=7, json=True))
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert isinstance(payload, list)
        assert any(p["title"] == "Recent" for p in payload)

    def test_cross_project_without_project(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        recent = date.today().isoformat()
        vault.create_note(
            NoteType.SESSION, "A", project="t",
            extra_frontmatter={"date": recent},
        )
        vault.create_note(
            NoteType.SESSION, "B", project="u",
            extra_frontmatter={"date": recent},
        )
        Indexer(config=cfg).rebuild(full=True)
        cmd_timeline(_args(project="", days=7, json=False))
        out = capsys.readouterr().out
        # Cross-project banner appears.
        assert "Cross-project activity" in out or "No session" in out


class TestProjectSnapshot:
    def test_emits_text_for_known_project(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        vault.create_note(NoteType.NOTE, "seed", project="t")
        Indexer(config=cfg).rebuild(full=True)
        cmd_project_snapshot(
            _args(project="t", sections="", budget_tokens=0)
        )
        out = capsys.readouterr().out
        # The snapshot has a header for vault stats — assert non-empty.
        assert out.strip() != ""


class TestPrompts:
    def test_no_prompts_message(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        Indexer(config=cfg).rebuild(full=True)
        cmd_prompts(
            _args(project="t", since="", limit=10, classified_as="",
                  json=False)
        )
        out = capsys.readouterr().out
        assert "No prompts" in out

    def test_classified_as_filter_path(
        self, cfg: Config, vault: VaultManager, capsys
    ):
        sess_dir = cfg.vault_root / "projects" / "t" / "sessions" / "ses-1"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "events.jsonl").write_text(
            json.dumps({
                "type": "prompt",
                "text": "How does FTS5 tokenize?",
                "session_id": "cc-1",
                "ts": "2026-05-02T15:00:00+00:00",
            }) + "\n",
            encoding="utf-8",
        )
        Indexer(config=cfg).rebuild(full=True)
        cmd_prompts(
            _args(project="t", since="", limit=10,
                  classified_as="probe", json=True)
        )
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert any(p["classification"] == "probe" for p in payload)

    def test_missing_project_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        # Isolated config — no THINKWEAVE_PROJECT env var, no
        # default_project on disk. cmd_prompts must exit 1 cleanly.
        monkeypatch.delenv("THINKWEAVE_PROJECT", raising=False)
        monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
        with pytest.raises(SystemExit) as exc:
            cmd_prompts(
                _args(project="", since="", limit=10, classified_as="",
                      json=False)
            )
        assert exc.value.code == 1


class TestUpdateStructuredValues:
    """`weave update` must round-trip the structured frontmatter the
    dream-judge-worker writes via its MCP-fallback rail: prediction_history
    is a list of dicts, which key=value tokens historically mangled
    (comma-split into strings)."""

    HISTORY = [
        {"match": "pending", "judged_at": "2026-07-01T00:00:00+00:00", "reason": "seeded"},
        {"match": "confirmed", "judged_at": "2026-07-17T00:00:00+00:00", "reason": "successor dec-b declared supersedes"},
    ]

    def _seed(self, vault: VaultManager) -> str:
        path = vault.create_note(
            NoteType.DECISION, "Test decision", body="Body.", project="t",
            extra_frontmatter={"predicted_outcome": "check X", "prediction_history": []},
        )
        from thinkweave.core.vault import parse_frontmatter
        return parse_frontmatter(path.read_text(encoding="utf-8"))[0]["id"]

    def _read_fm(self, cfg: Config, note_id: str) -> dict:
        from thinkweave.core.vault import parse_frontmatter
        idx = Indexer(config=cfg)
        row = idx.db.execute(
            "SELECT path FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        idx.close()
        path = cfg.vault_root / row["path"]
        return parse_frontmatter(path.read_text(encoding="utf-8"))[0]

    def test_fm_token_json_value(self, cfg, vault):
        from thinkweave.surfaces.cli.notes import cmd_update
        note_id = self._seed(vault)
        Indexer(config=cfg).rebuild(full=True)
        cmd_update(_args(
            note_id=note_id,
            frontmatter=[f"prediction_history={json.dumps(self.HISTORY)}"],
            body_append="", frontmatter_json="",
        ))
        fm = self._read_fm(cfg, note_id)
        assert fm["prediction_history"] == self.HISTORY

    def test_frontmatter_json_file(self, cfg, vault, tmp_path):
        from thinkweave.surfaces.cli.notes import cmd_update
        note_id = self._seed(vault)
        Indexer(config=cfg).rebuild(full=True)
        payload = tmp_path / "updates.json"
        payload.write_text(json.dumps({
            "prediction_history": self.HISTORY,
            "prediction_match": "confirmed",
            "judged_at": "2026-07-17T00:00:00+00:00",
        }), encoding="utf-8")
        cmd_update(_args(
            note_id=note_id, frontmatter=[],
            body_append="", frontmatter_json=str(payload),
        ))
        fm = self._read_fm(cfg, note_id)
        assert fm["prediction_history"] == self.HISTORY
        assert fm["prediction_match"] == "confirmed"

    def test_explicit_token_wins_over_json_file(self, cfg, vault, tmp_path):
        from thinkweave.surfaces.cli.notes import cmd_update
        note_id = self._seed(vault)
        Indexer(config=cfg).rebuild(full=True)
        payload = tmp_path / "updates.json"
        payload.write_text(json.dumps({"prediction_match": "contradicted"}), encoding="utf-8")
        cmd_update(_args(
            note_id=note_id, frontmatter=["prediction_match=confirmed"],
            body_append="", frontmatter_json=str(payload),
        ))
        assert self._read_fm(cfg, note_id)["prediction_match"] == "confirmed"

    def test_legacy_comma_list_unchanged(self, cfg, vault):
        from thinkweave.surfaces.cli.notes import _parse_fm_token
        assert _parse_fm_token("tags=a,b,c") == ("tags", ["a", "b", "c"])
        assert _parse_fm_token("flag=true") == ("flag", True)
        assert _parse_fm_token("plain=hello") == ("plain", "hello")

    def test_malformed_json_falls_back_to_string(self):
        from thinkweave.surfaces.cli.notes import _parse_fm_token
        key, val = _parse_fm_token("x=[not json")
        assert key == "x" and val == "[not json"
