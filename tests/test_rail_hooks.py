"""The rsi-rail hook registration set, ``hooks/hooks.toml`` (dec-4f158b58
layers 2 and 3): the reader refuses unknown events and keys by name, an
absent file is the canonical set, the manifest records the set in force, and
the Pi and OpenCode shims honour the set their stub hands them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from thinkweave.core import harness, manifest, rail_hooks
from thinkweave.core.config import Config
from thinkweave.core.harness import CANONICAL_EVENTS
from thinkweave.surfaces.hooks import install as hooks_install

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]


def _rail(tmp_path: Path, toml: str, scripts: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "rsi-rail"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "hooks.toml").write_text(toml)
    for name, body in (scripts or {}).items():
        path = repo / "hooks" / name
        path.write_text(body)
        path.chmod(0o755)
    return repo


NOTE = '#!/bin/sh\necho stop >> "$(dirname "$0")/note.log"\n'

DISABLE_AND_FOREIGN = """
[SubagentStart]
enabled = false

[[foreign]]
name = "note"
event = "Stop"
script = "note.sh"
"""


class TestReader:
    def test_no_repo_is_the_canonical_set(self):
        assert rail_hooks.load(None, CANONICAL) == rail_hooks.DEFAULT

    def test_absent_file_is_the_canonical_set(self, tmp_path):
        repo = tmp_path / "rsi-rail"
        repo.mkdir()
        reg = rail_hooks.load(repo, CANONICAL)
        assert reg.is_default
        assert reg.hooks_dir == repo / "hooks"

    def test_all_commented_file_is_the_canonical_set(self, tmp_path):
        repo = _rail(tmp_path, "# [SubagentStart]\n# enabled = true\n")
        assert rail_hooks.load(repo, CANONICAL).is_default

    def test_disabled_event_and_foreign_row(self, tmp_path):
        repo = _rail(tmp_path, DISABLE_AND_FOREIGN, {"note.sh": NOTE})
        reg = rail_hooks.load(repo, CANONICAL)
        assert reg.disabled == {"SubagentStart"}
        (row,) = reg.foreign
        assert (row.name, row.event, row.matcher) == ("note", "Stop", "")
        assert row.script == repo / "hooks" / "note.sh"

    def test_unknown_event_is_refused_by_name(self, tmp_path):
        repo = _rail(tmp_path, "[SubagentBegin]\nenabled = false\n")
        with pytest.raises(ValueError, match="unknown event 'SubagentBegin'"):
            rail_hooks.load(repo, CANONICAL)

    def test_unknown_key_is_refused_by_name(self, tmp_path):
        repo = _rail(tmp_path, "[Stop]\nenable = false\n")
        with pytest.raises(ValueError, match=r"unknown key Stop\.enable"):
            rail_hooks.load(repo, CANONICAL)

    def test_unknown_foreign_key_is_refused_by_name(self, tmp_path):
        repo = _rail(
            tmp_path,
            '[[foreign]]\nname = "n"\nevent = "Stop"\nscript = "note.sh"\ntimeout = 3\n',
            {"note.sh": NOTE},
        )
        with pytest.raises(ValueError, match="unknown key 'timeout'"):
            rail_hooks.load(repo, CANONICAL)

    def test_foreign_unknown_event_is_refused_by_name(self, tmp_path):
        repo = _rail(
            tmp_path,
            '[[foreign]]\nname = "n"\nevent = "Teardown"\nscript = "note.sh"\n',
            {"note.sh": NOTE},
        )
        with pytest.raises(ValueError, match="unknown event 'Teardown'"):
            rail_hooks.load(repo, CANONICAL)

    def test_foreign_script_must_live_under_hooks(self, tmp_path):
        repo = _rail(tmp_path, '[[foreign]]\nname = "n"\nevent = "Stop"\nscript = "../x.sh"\n')
        with pytest.raises(ValueError, match="must be a path inside"):
            rail_hooks.load(repo, CANONICAL)

    def test_missing_script_is_named(self, tmp_path):
        repo = _rail(tmp_path, '[[foreign]]\nname = "n"\nevent = "Stop"\nscript = "gone.sh"\n')
        with pytest.raises(ValueError, match="gone.sh does not exist"):
            rail_hooks.load(repo, CANONICAL)

    def test_matchers_only_where_the_canonical_event_has_them(self, tmp_path):
        repo = _rail(tmp_path, '[PostToolUse]\nmatchers = ["Write|Edit"]\n')
        assert rail_hooks.load(repo, CANONICAL).matchers == {"PostToolUse": ("Write|Edit",)}
        repo2 = _rail(tmp_path / "b", '[Stop]\nmatchers = ["x"]\n')
        with pytest.raises(ValueError, match="Stop.matchers is not allowed"):
            rail_hooks.load(repo2, CANONICAL)

    def test_every_canonical_event_is_addressable(self, tmp_path):
        toml = "".join(f"[{e}]\nenabled = false\n" for e in CANONICAL_EVENTS)
        reg = rail_hooks.load(_rail(tmp_path, toml), CANONICAL)
        assert reg.disabled == set(CANONICAL_EVENTS)


class TestManifest:
    def _cfg(self, tmp_path, monkeypatch, repo):
        monkeypatch.setattr(manifest, "_rail", lambda repo: {"repo": str(repo)})
        return Config(vault_root=tmp_path / "vault", rsi_rail=repo)

    def test_no_rail_file_records_the_profiles_full_list(self, tmp_path, monkeypatch):
        repo = tmp_path / "rsi-rail"
        repo.mkdir()
        stamp = manifest.collect(self._cfg(tmp_path, monkeypatch, repo), "claude-code")
        assert stamp["hook_events"] == sorted(CANONICAL_EVENTS)
        assert "foreign" not in stamp

    def test_rendered_list_and_foreign_pairs(self, tmp_path, monkeypatch):
        repo = _rail(tmp_path, DISABLE_AND_FOREIGN, {"note.sh": NOTE})
        stamp = manifest.collect(self._cfg(tmp_path, monkeypatch, repo), "claude-code")
        assert "SubagentStart" not in stamp["hook_events"]
        assert "Stop" in stamp["hook_events"]
        assert stamp["foreign"] == [{"name": "note", "event": "Stop"}]

    def test_plugin_route_records_what_still_fires(self, tmp_path, monkeypatch):
        """Claude Code cannot drop one plugin hook, so a disabled event stays
        in force on the plugin route and the manifest says so."""
        home = tmp_path / "home"
        cc = harness.claude_code(home=home)
        cc.installed_plugins.parent.mkdir(parents=True)
        cc.installed_plugins.write_text('{"plugins": {"thinkweave@mp": []}}')
        monkeypatch.setattr(harness, "_OVERRIDE", cc)
        repo = _rail(tmp_path, DISABLE_AND_FOREIGN, {"note.sh": NOTE})
        stamp = manifest.collect(self._cfg(tmp_path, monkeypatch, repo), "claude-code")
        assert "SubagentStart" in stamp["hook_events"]
        assert stamp["foreign"] == [{"name": "note", "event": "Stop"}]

    def test_pi_carries_no_subagent_events_or_hooks(self, tmp_path, monkeypatch):
        repo = _rail(
            tmp_path,
            '[[foreign]]\nname = "sub"\nevent = "SubagentStop"\nscript = "note.sh"\n',
            {"note.sh": NOTE},
        )
        stamp = manifest.collect(self._cfg(tmp_path, monkeypatch, repo), "pi")
        assert "SubagentStop" not in stamp["hook_events"]
        assert "foreign" not in stamp


# --------------------------------------------------------------------------- #
# The shims honour the stub's registration set
# --------------------------------------------------------------------------- #

LAUNCHER = """#!/bin/sh
echo "$1" >> "$(dirname "$0")/launcher.log"
cat > /dev/null
echo '{}'
"""

PI_DRIVER = """
const stub = await import(process.argv[2]);
const handlers = {};
const pi = { on: (name, fn) => { handlers[name] = fn; } };
stub.default(pi);
const ctx = { sessionManager: { getSessionId: () => "s-1" } };
await handlers["before_agent_start"]({ prompt: "hello there" }, ctx);
await handlers["agent_end"]({}, ctx);
"""

OPENCODE_DRIVER = """
const stub = await import(process.argv[2]);
const hooks = await stub.default({ directory: process.cwd() });
await hooks.event({ event: { type: "session.created", properties: { info: { id: "s-1" } } } });
await hooks["chat.message"](
  { sessionID: "s-1" },
  { parts: [{ type: "text", text: "hello there" }] },
);
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s-1" } } });
await hooks.dispose();
"""


def _node_strips_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    try:
        major = int(out.strip().lstrip("v").split(".")[0])
    except ValueError:
        return False
    return major >= 23


@pytest.mark.skipif(not _node_strips_types(), reason="needs node >= 23 (TS type stripping)")
@pytest.mark.parametrize(
    "factory,driver",
    [(harness.pi, PI_DRIVER), (harness.opencode, OPENCODE_DRIVER)],
    ids=["pi", "opencode"],
)
def test_shim_skips_a_disabled_event_and_runs_the_foreign_hook(
    tmp_path, monkeypatch, factory, driver
):
    """A throwaway copy of the shim tree (so the shim's launcher is a recorder,
    not the real handler) loaded through the stub the installer renders:
    UserPromptSubmit disabled never reaches the launcher, Stop does, and the
    foreign Stop hook appends its one line."""
    checkout = tmp_path / "checkout"
    for rel in ("shims/pi", "shims/opencode", "shims/shim-core/src"):
        shutil.copytree(REPO_ROOT / rel, checkout / rel)
    (checkout / "bin").mkdir()
    launcher = checkout / "bin" / "weave-hook-launch"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)

    repo = _rail(
        tmp_path,
        "[UserPromptSubmit]\nenabled = false\n" + DISABLE_AND_FOREIGN,
        {"note.sh": NOTE},
    )
    reg = rail_hooks.load(repo, CANONICAL)
    profile = factory(home=tmp_path / "home")
    monkeypatch.setattr(hooks_install, "_repo_root", lambda: checkout)
    stub = tmp_path / "stub" / "thinkweave.ts"
    stub.parent.mkdir()
    stub.write_text(hooks_install._extension_stub_text(profile, reg))
    script = tmp_path / "driver.mjs"
    script.write_text(driver)

    run = subprocess.run(
        ["node", str(script), str(stub)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert run.returncode == 0, run.stderr
    phases = [p for p in (checkout / "bin" / "launcher.log").read_text().split() if p != "warmup"]
    assert "user_prompt_submit" not in phases
    assert "stop" in phases
    assert (repo / "hooks" / "note.log").read_text() == "stop\n"
