"""Suite-wide fixtures.

The plugin-route detector reads real machine state through the active harness
profile — ``~/.claude/plugins/installed_plugins.json`` (marketplace) and the
``~/.claude/skills/thinkweave`` symlink (dev-link). Point both probes at
nonexistent paths for every test so rendered commands (cron lines, flow
invocations) don't depend on whether the dev box happens to have the plugin
installed or dev-linked. Tests that exercise the plugin route override
explicitly — via ``use_profile`` below, the ``plugin_route_active`` fixture,
the ``manifest=`` / ``dev_link=`` kwargs of ``plugin_namespace``, or by patching
at the import site.

Harness touchpoints (``mcp_config``, ``instructions_file``, …) are sandboxed by
the autouse ``_sandbox_harness_home`` fixture and aimed by ``use_profile``.

Test-vault lifecycle
--------------------
``vault_factory`` is the one owner of the tmp-vault setup ritual that used to
be copy-pasted (``vault_dir`` → ``Config`` → ``VaultManager`` → ``ensure_dirs``)
into dozens of suites. It is a *builder*: call it, chain intent-level
affordances, read the handle.

    def test_something(vault_factory):
        tv = vault_factory(notes=["A", {"title": "B", "tags": ["todo"]}]).indexed()
        assert tv.config.index_db.exists()
        tv.vault.create_note(note_type=NoteType.NOTE, title="C")

    # config knobs go straight through to Config(...):
    tv = vault_factory(default_project="proj")

The three ubiquitous lifecycle fixtures (``config``, ``vault``, ``indexer``,
``search``) are derived from a default ``vault_factory()`` so a suite that only
wants the plain chain can request them by name and drop its local copies. They
live here, not per-suite — that is the whole point of the fixture.

Opportunistic-migration rule
-----------------------------
Do NOT do a big-bang migration of all ~40 remaining ritual copies. Every future
issue migrates *only the test files it already touches* to ``vault_factory`` —
never as a blocking dependency of unrelated work, never a file another PR owns.
A suite migrates by deleting its local ``vault_dir``/``config``/``vault``/
``indexer``/``search`` fixtures (their names resolve here) and, where it helps,
switching setup to ``vault_factory(notes=[...])``. Suites that keep local copies
shadow these transparently, so the migration is safe to do one file at a time.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from thinkweave.core import harness
from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.plugin_route import PLUGIN_NAME
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.retrieval.search import Search

# ---------------------------------------------------------------------------
# Symlink capability
# ---------------------------------------------------------------------------

SYMLINK_SKIP_REASON = (
    "this process may not create filesystem symlinks (Windows withholds "
    "SeCreateSymbolicLinkPrivilege: WinError 1314). Enable Windows Developer "
    "Mode or run the suite elevated to exercise the dev-link tests"
)


@functools.cache
def symlinks_creatable() -> bool:
    """Probe *once* whether this process can actually create a symlink.

    Not a platform check: Windows with Developer Mode enabled (or an elevated
    shell) creates symlinks fine, and those runs must still execute the
    dev-link tests. So attempt the real syscall in a throwaway dir and cache
    the answer for the session.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        try:
            (root / "probe-link").symlink_to(root)
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


@pytest.fixture
def requires_symlinks() -> None:
    """Skip the requesting test unless a symlink can genuinely be created.

    Only for tests whose *subject* is the dev-link symlink. A test that merely
    needs the plugin route to look active should use ``plugin_route_active``
    instead — that needs no privilege anywhere.
    """
    if not symlinks_creatable():
        pytest.skip(SYMLINK_SKIP_REASON)


@pytest.fixture
def plugin_route_active() -> Callable[..., Path]:
    """``plugin_route_active()`` — make plugin-route detection answer
    ``'thinkweave'`` without needing a symlink.

    Writes the *marketplace* shape (a ``thinkweave@…`` key in the profile's
    ``installed_plugins.json``), which is the other half of
    ``plugin_route.plugin_namespace``'s ``or``. Tests that only need the route
    to be detectable — cron rendering, namespacing — want this rather than
    ``dev_link.symlink_to(...)``: the symlink was always incidental to what
    they assert, and creating one is a privileged operation on Windows.
    Pass an explicit path to aim it at a non-active profile's manifest.
    """

    def _activate(manifest: Path | None = None) -> Path:
        path = manifest if manifest is not None else harness.active().installed_plugins
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {f"{PLUGIN_NAME}@marketplace": [{"scope": "user"}]}
        path.write_text(
            json.dumps({"version": 2, "plugins": entry}), encoding="utf-8"
        )
        return path

    return _activate


@pytest.fixture
def use_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., harness.HarnessProfile]:
    """``use_profile(mcp_config=…, pause_marker=…)`` — swap the active harness
    profile for a copy with those fields replaced. Calls compose."""

    def _use(**fields: Any) -> harness.HarnessProfile:
        profile = dataclasses.replace(harness.active(), **fields)
        monkeypatch.setattr(harness, "_OVERRIDE", profile)
        return profile

    return _use


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Activate the ``codex`` profile against a throwaway ``$CODEX_HOME``.

    Returns the ``.codex`` dir itself: everything Codex owns lives under that
    one root, so it is the path assertions want. Selection goes through the env
    var rather than ``_OVERRIDE`` so ``active()`` resolves the profile the way
    a real run does — which means clearing ``_sandbox_harness_home``'s override
    first.
    """
    home = tmp_path / "codex-home" / ".codex"
    home.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("THINKWEAVE_HARNESS", "codex")
    monkeypatch.setattr(harness, "_OVERRIDE", None)
    return home


@pytest.fixture
def stub_install_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the three install-time validators (``_check_uv_available``,
    ``_check_pyproject_reachable``, ``_uv_sync``) so ``cmd_install`` tests
    don't require uv on PATH, a real pyproject in the sandbox, or pay sync
    time. Tests that specifically validate these helpers don't use this."""
    from thinkweave.surfaces.cli import install as inst

    monkeypatch.setattr(inst, "_check_uv_available", lambda: None)
    monkeypatch.setattr(inst, "_check_pyproject_reachable", lambda root: None)
    monkeypatch.setattr(inst, "_uv_sync", lambda root: None)


@pytest.fixture
def scheduled_job() -> Callable[..., Any]:
    """``scheduled_job("codex exec /dream")`` — one direct-runner job for the
    cron-rendering suites, whose name and cadence are never what's asserted."""
    from thinkweave.scheduling.registry import ScheduledJob

    def _job(command: str, cadence: str = "0 3 * * *") -> Any:
        return ScheduledJob(
            name="j", cadence=cadence, command=command, runner="direct"
        )

    return _job


@pytest.fixture(autouse=True)
def _sandbox_harness_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Derive the active profile from a tmp home for *every* test.

    Two jobs in one. It keeps rendered commands (cron lines, flow
    invocations) independent of whether the dev box happens to have the plugin
    installed or dev-linked — nothing exists under the tmp home, so
    ``namespace()`` is None unless a test says otherwise. And it makes the
    suite hermetic: no test can read or write the developer's real
    ``~/.claude`` (a hook-installer test writing the real
    ``~/.claude/settings.json`` is the concrete accident this prevents).
    """
    monkeypatch.setattr(
        harness, "_OVERRIDE", harness.claude_code(home=tmp_path / "harness-home")
    )


# ---------------------------------------------------------------------------
# Test-vault builder
# ---------------------------------------------------------------------------


@dataclass
class VaultHandle:
    """A ready-to-use tmp vault with intent-level affordances.

    Construction (dirs, Config, VaultManager, ensure_dirs) is done by the
    ``vault_factory`` fixture; the affordances below chain so seeding reads as
    one expression. ``with_note``/``with_theme`` forward straight to
    ``VaultManager.create_note`` so every create_note keyword is available.
    """

    dir: Path
    config: Config
    vault: VaultManager

    def with_note(self, title: str = "Note", **kwargs: Any) -> "VaultHandle":
        note_type = kwargs.pop("note_type", NoteType.NOTE)
        self.vault.create_note(note_type=note_type, title=title, **kwargs)
        return self

    def with_theme(self, title: str, **kwargs: Any) -> "VaultHandle":
        self.vault.create_note(note_type=NoteType.THEME, title=title, **kwargs)
        return self

    def indexed(self) -> "VaultHandle":
        idx = Indexer(config=self.config)
        try:
            idx.rebuild(full=True)
        finally:
            idx.close()
        return self


def _seed(handle: VaultHandle, notes: Any, method: str) -> None:
    for item in notes or []:
        if isinstance(item, dict):
            getattr(handle, method)(**item)
        else:  # a bare string is a title
            getattr(handle, method)(item)


@pytest.fixture
def vault_factory(tmp_path: Path) -> Callable[..., VaultHandle]:
    """Build tmp vaults on demand — the shared setup-ritual owner.

    ``vault_factory(notes=[...], themes=[...], indexed=False, **config_kwargs)``
    returns a :class:`VaultHandle`. ``notes``/``themes`` accept either a bare
    title string or a dict of ``create_note`` kwargs. ``config_kwargs`` flow
    straight into ``Config(...)`` — the escape hatch for suites that tweak
    knobs (e.g. ``default_project=``). Call it more than once in a test for
    independent vaults (each gets its own subdir).
    """
    made: list[Path] = []

    def _build(
        notes: Any = None,
        themes: Any = None,
        indexed: bool = False,
        **config_kwargs: Any,
    ) -> VaultHandle:
        vdir = tmp_path / ("vault" if not made else f"vault-{len(made) + 1}")
        made.append(vdir)
        config = Config(vault_root=vdir, **config_kwargs)
        vm = VaultManager(config=config)
        vm.ensure_dirs()
        handle = VaultHandle(dir=vdir, config=config, vault=vm)
        _seed(handle, notes, "with_note")
        _seed(handle, themes, "with_theme")
        if indexed:
            handle.indexed()
        return handle

    return _build


# ---------------------------------------------------------------------------
# Derived lifecycle fixtures — the plain chain, built once on vault_factory so
# migrated suites can drop their local copies and request these by name.
# A suite keeping its own definitions shadows these with no interaction.
# ---------------------------------------------------------------------------


@pytest.fixture
def _default_vault(vault_factory: Callable[..., VaultHandle]) -> VaultHandle:
    return vault_factory()


@pytest.fixture
def config(_default_vault: VaultHandle) -> Config:
    return _default_vault.config


@pytest.fixture
def vault(_default_vault: VaultHandle) -> VaultManager:
    return _default_vault.vault


@pytest.fixture
def indexer(config: Config):
    idx = Indexer(config=config)
    yield idx
    idx.close()


@pytest.fixture
def search(config: Config, indexer: Indexer):
    # Instantiate alongside the indexer so tests that only ask for `search`
    # still get a populated-on-rebuild db behind it.
    s = Search(config=config)
    yield s
    s.close()
