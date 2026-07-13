"""Suite-wide fixtures.

The plugin-route detector (``core.plugin_route.plugin_namespace``) reads real
machine state — ``~/.claude/plugins/installed_plugins.json`` (marketplace) and
the ``~/.claude/skills/thinkweave`` symlink (dev-link). Point both probes at
nonexistent paths for every test so rendered commands (cron lines, flow
invocations) don't depend on whether the dev box happens to have the plugin
installed or dev-linked. Tests that exercise the plugin route override
explicitly via the ``manifest=`` / ``dev_link=`` kwargs or by patching
``plugin_namespace`` at the import site.

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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from thinkweave.core import plugin_route
from thinkweave.core.config import Config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager
from thinkweave.retrieval.search import Search


@pytest.fixture(autouse=True)
def _no_plugin_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        plugin_route, "_INSTALLED_PLUGINS", tmp_path / "absent-installed_plugins.json"
    )
    monkeypatch.setattr(
        plugin_route, "_DEV_LINK", tmp_path / "absent-skills-thinkweave"
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
