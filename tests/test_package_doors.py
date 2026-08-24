"""The six package doors — curated ``__init__`` + ``__all__`` (issue #13).

Seam under test: the package-level import contract
(``from thinkweave.<pkg> import <workhorse>``), not any implementation
module. Workhorse names below come from the issue's demand measurement
(grep of ``from thinkweave.<pkg>… import`` across src/tests/scripts),
not from the door files themselves.
"""

import importlib
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


def test_synthesis_door_is_marked_provisional():
    mod = importlib.import_module("thinkweave.synthesis")
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "provisional" in source, (
        "synthesis door must carry the '# provisional — finalized by Track A' marker"
    )
