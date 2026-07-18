"""Regression tests for `weave pause` / `weave resume`.

The helpers must round-trip cleanly: pause → resume must restore the
machine to (logically) the pre-pause state, and the CLAUDE.md block
remover must be the inverse of the splice helper for the bytes it
touches. We monkeypatch the module-level path constants so the tests
never touch the real ``~/.claude``.
"""

from __future__ import annotations

import json

import pytest

from thinkweave.surfaces.cli import install as install_mod
from thinkweave.surfaces.cli import pause as pause_mod


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """Point all install/pause path constants at a sandbox tmp dir."""
    fake_home = tmp_path / "claude_home"
    fake_home.mkdir()
    claude_json = fake_home / ".claude.json"
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    marker = fake_home / ".claude" / "thinkweave_paused.json"
    monkeypatch.setattr(install_mod, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(install_mod, "CLAUDE_MD", claude_md)
    monkeypatch.setattr(install_mod, "MARKER", marker)
    # pause_mod imports MARKER by name, so its bound copy needs patching too;
    # other constants are read through install_mod at call time and don't.
    monkeypatch.setattr(pause_mod, "MARKER", marker)
    return {"claude_json": claude_json, "claude_md": claude_md, "marker": marker}


class TestClaudeMdRemoval:
    def test_remove_block_is_inverse_of_splice(self, fake_claude_home):
        md = fake_claude_home["claude_md"]
        md.parent.mkdir(parents=True, exist_ok=True)
        original = "# my notes\n\nsome content\n"
        md.write_text(original, encoding="utf-8")

        block = install_mod._render_claude_md_block()
        spliced = install_mod._splice_claude_md_block(original, block)
        md.write_text(spliced, encoding="utf-8")
        assert install_mod.CLAUDE_MD_BLOCK_START in md.read_text()

        assert install_mod._remove_claude_md_block() is True
        # bytes outside the sentinels survive
        assert "# my notes" in md.read_text()
        assert "some content" in md.read_text()
        assert install_mod.CLAUDE_MD_BLOCK_START not in md.read_text()

    def test_remove_block_returns_false_when_absent(self, fake_claude_home):
        md = fake_claude_home["claude_md"]
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# just notes, no block\n", encoding="utf-8")
        assert install_mod._remove_claude_md_block() is False

    def test_remove_block_returns_false_when_file_missing(self, fake_claude_home):
        assert install_mod._remove_claude_md_block() is False


class TestMcpEntryRemoval:
    def test_remove_returns_false_when_file_missing(self, fake_claude_home):
        assert install_mod._remove_mcp_entry() is False

    def test_remove_returns_false_when_entry_absent(self, fake_claude_home):
        fake_claude_home["claude_json"].write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
        )
        assert install_mod._remove_mcp_entry() is False

    def test_remove_strips_only_thinkweave(self, fake_claude_home):
        cj = fake_claude_home["claude_json"]
        cj.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        install_mod.SERVER_NAME: {"command": "uv", "args": []},
                        "other": {"command": "x"},
                    },
                    "otherTopLevel": "preserved",
                }
            ),
            encoding="utf-8",
        )
        assert install_mod._remove_mcp_entry() is True
        cfg = json.loads(cj.read_text())
        assert install_mod.SERVER_NAME not in cfg["mcpServers"]
        assert "other" in cfg["mcpServers"]
        assert cfg["otherTopLevel"] == "preserved"

    def test_restore_creates_file_when_missing(self, fake_claude_home):
        install_mod._restore_mcp_entry()
        cfg = json.loads(fake_claude_home["claude_json"].read_text())
        assert install_mod.SERVER_NAME in cfg["mcpServers"]
        entry = cfg["mcpServers"][install_mod.SERVER_NAME]
        assert entry["args"][-1] == "weave-mcp"

    def test_restore_preserves_other_servers(self, fake_claude_home):
        cj = fake_claude_home["claude_json"]
        cj.write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}}),
            encoding="utf-8",
        )
        install_mod._restore_mcp_entry()
        cfg = json.loads(cj.read_text())
        assert install_mod.SERVER_NAME in cfg["mcpServers"]
        assert "other" in cfg["mcpServers"]


class TestPauseResumeRoundTrip:
    def test_status_when_not_paused(self, fake_claude_home, capsys):
        pause_mod.cmd_pause(_ns(status=True))
        out = capsys.readouterr().out
        assert "active" in out

    def test_pause_writes_marker_and_status_reports_it(
        self, fake_claude_home, capsys, monkeypatch
    ):
        # stub hook (un)install to avoid touching ~/.claude in tests where
        # the hook installer reads other config we don't fake here
        monkeypatch.setattr(pause_mod, "uninstall_hooks", lambda **kw: None)
        # seed an MCP entry + CLAUDE.md block so pause has something to do
        install_mod._restore_mcp_entry()
        fake_claude_home["claude_md"].parent.mkdir(parents=True, exist_ok=True)
        fake_claude_home["claude_md"].write_text(
            install_mod._splice_claude_md_block(
                "# notes\n", install_mod._render_claude_md_block()
            ),
            encoding="utf-8",
        )

        pause_mod.cmd_pause(_ns(status=False))
        assert fake_claude_home["marker"].exists()
        data = json.loads(fake_claude_home["marker"].read_text())
        assert "user-scope hooks" in data["removed"]
        assert "MCP entry" in data["removed"]
        assert "CLAUDE.md block" in data["removed"]

        pause_mod.cmd_pause(_ns(status=True))
        out = capsys.readouterr().out
        assert "PAUSED" in out

    def test_pause_then_resume_clears_marker(
        self, fake_claude_home, monkeypatch, capsys
    ):
        monkeypatch.setattr(pause_mod, "uninstall_hooks", lambda **kw: None)
        monkeypatch.setattr(pause_mod, "install_hooks", lambda **kw: None)
        install_mod._restore_mcp_entry()

        pause_mod.cmd_pause(_ns(status=False))
        assert fake_claude_home["marker"].exists()

        pause_mod.cmd_resume(_ns())
        assert not fake_claude_home["marker"].exists()
        # MCP entry was restored
        cfg = json.loads(fake_claude_home["claude_json"].read_text())
        assert install_mod.SERVER_NAME in cfg["mcpServers"]

    def test_pause_when_already_paused_exits(
        self, fake_claude_home, monkeypatch
    ):
        monkeypatch.setattr(pause_mod, "uninstall_hooks", lambda **kw: None)
        fake_claude_home["marker"].parent.mkdir(parents=True, exist_ok=True)
        fake_claude_home["marker"].write_text("{}", encoding="utf-8")
        with pytest.raises(SystemExit):
            pause_mod.cmd_pause(_ns(status=False))

    def test_resume_without_marker_is_noop(self, fake_claude_home, capsys):
        pause_mod.cmd_resume(_ns())
        out = capsys.readouterr().out
        assert "not paused" in out


def _ns(**kw):
    """Minimal argparse.Namespace stand-in."""
    import argparse

    return argparse.Namespace(**{"status": False, **kw})
