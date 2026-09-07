"""The six package doors — curated ``__init__`` + ``__all__`` (issue #13).

Seam under test: the package-level import contract
(``from thinkweave.<pkg> import <workhorse>``), not any implementation
module. Workhorse names below come from the issue's demand measurement
(grep of ``from thinkweave.<pkg>… import`` across src/tests/scripts),
not from the door files themselves.
"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

# Per package: names the door MUST export (the measured workhorses).
# The door may export more; these are the demand-confirmed floor.
REQUIRED_EXPORTS = {
    "core": {
        "Config",
        "Indexer",
        "NoteType",
        "VaultManager",
        "load_config",
        "parse_frontmatter",
        "render_frontmatter",
    },
    "retrieval": {"Search", "SearchResult", "SemanticSearchUnavailable", "build_project_context"},
    "synthesis": {"load_ontology", "resolve_concept", "evaluate_decision"},
    "acquisition": {"Queue", "load_user_config", "all_specs", "get_spec"},
    "operations": {"create_note", "extract_session", "finalize_wrap", "judge_and_writeback"},
    "surfaces": {"main", "build_parser", "install_hooks"},
}


@pytest.mark.parametrize("pkg", sorted(REQUIRED_EXPORTS))
def test_door_declares_all_and_every_name_resolves(pkg):
    mod = importlib.import_module(f"thinkweave.{pkg}")
    exported = getattr(mod, "__all__", None)
    assert exported, f"thinkweave.{pkg} declares no __all__"
    missing = [name for name in exported if not hasattr(mod, name)]
    assert not missing, f"thinkweave.{pkg}.__all__ lists unresolvable names: {missing}"
    assert REQUIRED_EXPORTS[pkg] <= set(exported), (
        f"thinkweave.{pkg} door missing measured workhorses: "
        f"{sorted(REQUIRED_EXPORTS[pkg] - set(exported))}"
    )


def test_door_reexports_are_the_submodule_objects():
    import thinkweave.core
    import thinkweave.core.indexer
    import thinkweave.surfaces
    import thinkweave.surfaces.cli

    assert thinkweave.core.Indexer is thinkweave.core.indexer.Indexer
    assert thinkweave.surfaces.main is thinkweave.surfaces.cli.main


def test_load_bearing_omissions_stay_omitted():
    acquisition = importlib.import_module("thinkweave.acquisition")
    surfaces = importlib.import_module("thinkweave.surfaces")
    for name in ("importers", "discover"):
        assert name not in acquisition.__all__, (
            f"acquisition door must not re-export {name!r}: its modules import "
            "core.vault at top level, and vault.py imports acquisition's source "
            "registry — an eager re-export closes that cycle"
        )
    assert "mcp" not in surfaces.__all__, (
        "surfaces door must not re-export mcp: pulling the server into every "
        "surfaces import adds weight the hook path can't afford"
    )


def _thinkweave_modules_loaded_by(stmt: str) -> set[str]:
    """Modules a fresh interpreter loads for ``import <stmt>`` — subprocess so
    the measurement is not polluted by the test session's own imports."""
    code = (
        f"import {stmt}\nimport sys\n"
        "print('\\n'.join(m for m in sys.modules if m.startswith('thinkweave')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_hook_handler_import_stays_light():
    # weave-hook fires on every UserPromptSubmit/PostToolUse/Stop; handler.py
    # keeps its own imports lazy by design, and the surfaces door must not
    # undo that by eagerly pulling the CLI (25+ command modules → operations,
    # synthesis, retrieval, acquisition).
    loaded = _thinkweave_modules_loaded_by("thinkweave.surfaces.hooks.handler")
    heavy = {
        m
        for m in loaded
        if m.startswith(("thinkweave.surfaces.cli", "thinkweave.operations"))
    }
    assert not heavy, f"hook-handler import pulled the CLI/operations tree: {sorted(heavy)}"


def test_core_leaf_import_stays_light():
    # load_config from a hook body must not pay for the indexer's upward edge
    # into synthesis, nor vault's into acquisition (core/__init__ resolves
    # Indexer/VaultManager lazily).
    loaded = _thinkweave_modules_loaded_by("thinkweave.core.config")
    heavy = {
        m
        for m in loaded
        if m.startswith(("thinkweave.synthesis", "thinkweave.acquisition"))
    }
    assert not heavy, f"core.config import pulled heavy packages: {sorted(heavy)}"


# The hook handler imports these inside its per-event bodies — a fresh
# weave-hook process pays each import on every fire, under handler.py's
# stated hook-timeout budget. Values: package prefixes the import must NOT
# pull (what an eager operations door would drag in; extract legitimately
# needs synthesis/retrieval, so its check is unrelated-sibling modules).
HANDLER_BODY_BUDGETS = {
    "thinkweave.operations.retrieval_log": (
        "thinkweave.retrieval",
        "thinkweave.synthesis",
        "thinkweave.acquisition",
        "thinkweave.surfaces",
    ),
    "thinkweave.operations.prompt_time_retrieval": (
        "thinkweave.synthesis",
        "thinkweave.acquisition",
        "thinkweave.surfaces",
    ),
    "thinkweave.operations.extract": (
        "thinkweave.operations.hubs_batch",
        "thinkweave.operations.rlvr_export",
        "thinkweave.operations.migrations",
        "thinkweave.surfaces",
    ),
}


@pytest.mark.parametrize("module", sorted(HANDLER_BODY_BUDGETS))
def test_handler_body_imports_stay_light(module):
    loaded = _thinkweave_modules_loaded_by(module)
    heavy = {m for m in loaded if m.startswith(HANDLER_BODY_BUDGETS[module])}
    assert not heavy, f"{module} import pulled unneeded packages: {sorted(heavy)}"
