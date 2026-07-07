"""Package-edge contract test with a shrink-only grandfather baseline.

The documented layer stack (ARCHITECTURE.md §"Two layers") is a one-way
dependency ordering:

    surfaces → operations → {retrieval, synthesis, acquisition} → core

Read as ranks (``core`` lowest, ``surfaces`` highest), the contract is the
usual layered rule: **a package may import from any strictly-lower layer, and
only from a lower layer.** Same-rank sibling edges (``retrieval → synthesis``)
and upward edges (``core → synthesis``, ``synthesis → operations``) are
violations. That prose was, until this test, only prose — the *measured*
import graph is a near-complete mesh: ``core → synthesis`` (×6 sites),
``core → acquisition``, ``synthesis → operations`` (×8 sites),
``retrieval → synthesis/acquisition`` …, plus a handful of ``_underscore``
symbols reached across a package boundary (e.g. ``acquisition``'s YAML helper
``_parse_simple_yaml`` imported by ``core/api_config.py``).

This test pins two rules **now**, mechanically:

1. **Package edges** — every cross-package import in ``src/thinkweave/`` is
   either a documented downward edge (allowed silently) or listed in
   ``EDGE_BASELINE``. Nothing else.
2. **Private crossings** — no name beginning with a single underscore is
   imported across a package boundary unless listed in ``PRIVATE_BASELINE``.
   (This is independent of rule 1: a *downward* edge that still reaches for a
   neighbour's private — ``synthesis.geometry`` importing
   ``core.embeddings._unpack_embedding`` — is a rule-2 violation even though
   the edge itself is allowed.)

**Shrink-only grandfather baseline.** ``EDGE_BASELINE`` / ``PRIVATE_BASELINE``
snapshot the violations that existed when this test was written. The assertion
is *equality* — the measured violation set must equal the baseline. Two ways to
go red, both intended:

  * a **new** violation appears (a fresh disallowed edge or private crossing)
    → it is in the measured set but not the baseline → the test names it as
    unlisted. You must fix the import, not extend the baseline: the baseline
    may only **shrink**.
  * a violation is **fixed** but its baseline line is left behind (or a line is
    faked with no matching import) → it is in the baseline but not the measured
    set → the test names it as stale. Delete the freed line.

Because parallel refactors may fix grandfathered edges, whichever change lands
second is responsible for deleting the freed baseline lines it observes go
stale.

**Baseline keys are import *relationships*, not line numbers** —
``(importing_module, imported_module)`` for edges and
``(importing_module, imported_module, name)`` for privates. That keeps an entry
stable when a grandfathered file is edited for unrelated reasons (line numbers
would rot on every touch), and it makes removal atomic: drop a file's last
import of a target and exactly one baseline line becomes deletable.

**Progressive rules (added by later issues, not enforced here yet):**

3. *Per-primitive entry points.* As each primitive grows a front door (a single
   public module outside code is expected to import), a rule is added that
   imports from *outside* the primitive must target that front door, not its
   internal submodules. Added one primitive at a time, as each front door lands.
4. *Tests import privates only from the primitive they test.* A companion rule
   (scoped to ``tests/``) will assert that a ``test_<primitive>*.py`` file may
   reach into ``_underscore`` internals only of the primitive it names —
   enforcing the naming convention as an access-control boundary.

Dependency-light by design: no vault, no network, no imports of the package
under test — a pure ``ast`` sweep over ``src/thinkweave/`` (which catches
module-level *and* deferred/function-body imports, since ``ast.walk`` visits
every node) plus two frozen baselines.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
PKG_ROOT = SRC_ROOT / "thinkweave"

# --- The documented stack, as ranks -----------------------------------------
# core lowest, surfaces highest; the three capability lanes share a rank so
# sibling edges among them are *not* allowed. A package may import strictly
# downward (lower rank) only. Packages absent from this map (onboarding,
# scheduling, the legacy ``mcp`` shim, ``__main__``) are not documented layers,
# so every cross-package edge they participate in is a grandfathered violation
# until a future issue ranks them.
_RANK = {
    "core": 1,
    "retrieval": 2,
    "synthesis": 2,
    "acquisition": 2,
    "operations": 3,
    "surfaces": 4,
}


def _edge_allowed(importer_pkg: str, imported_pkg: str) -> bool:
    """A documented edge: both packages ranked and strictly downward."""
    return (
        importer_pkg in _RANK
        and imported_pkg in _RANK
        and _RANK[importer_pkg] > _RANK[imported_pkg]
    )


# --- Import scanner (ast) -----------------------------------------------------


def _module_name(path: Path) -> str:
    """Dotted module name for a file under ``src/`` (``__init__`` collapses to
    its package)."""
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _top_package(dotted: str) -> str | None:
    """Top-level thinkweave package of a dotted module, or None if the module
    is not an internal ``thinkweave.<pkg>.…`` target."""
    parts = dotted.split(".")
    if len(parts) < 2 or parts[0] != "thinkweave":
        return None
    return parts[1]


def _resolve_from(node: ast.ImportFrom, module: str, is_pkg: bool) -> str | None:
    """Resolve an ``ImportFrom`` (absolute or relative) to its target dotted
    module, mirroring CPython's relative-import arithmetic."""
    if node.level == 0:
        return node.module
    base = module.split(".")
    pkg = base if is_pkg else base[:-1]
    up = node.level - 1
    if up:
        pkg = pkg[:-up] if up <= len(pkg) else []
    target = ".".join(pkg)
    if node.module:
        target = f"{target}.{node.module}" if target else node.module
    return target or None


def _scan_module(module: str, source: str, is_pkg: bool):
    """Yield ``(imported_module, imported_name_or_None)`` for every internal
    ``thinkweave.*`` import in one module's source — module-level *and*
    deferred (function-body / ``TYPE_CHECKING``) imports alike, because
    ``ast.walk`` visits every node regardless of nesting.

    ``imported_name`` is the bound leaf of a ``from X import name`` (used for
    the private-crossing rule); it is ``None`` for plain ``import X`` and for
    ``from X import name`` records is repeated per name.
    """
    tree = ast.parse(source, filename=f"{module}.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_package(alias.name) is not None:
                    yield alias.name, None
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from(node, module, is_pkg)
            if target is None or _top_package(target) is None:
                continue
            for alias in node.names:
                yield target, alias.name


def _scan_tree(pkg_root: Path):
    """Sweep the package tree, returning ``(edge_violations, private_crossings)``.

    * ``edge_violations`` — set of ``(importing_module, imported_module)`` for
      cross-package edges that are not documented downward edges.
    * ``private_crossings`` — set of ``(importing_module, imported_module,
      name)`` where ``name`` begins with a single underscore and the import
      crosses a package boundary (independent of whether the edge is allowed).
    """
    edge_violations: set[tuple[str, str]] = set()
    private_crossings: set[tuple[str, str, str]] = set()
    files = sorted(pkg_root.rglob("*.py"))
    assert files, f"no python files under {pkg_root} — repo layout moved?"
    for path in files:
        module = _module_name(path)
        importer_pkg = _top_package(module)
        if importer_pkg is None:
            continue
        is_pkg = path.name == "__init__.py"
        for imported_module, name in _scan_module(
            module, path.read_text(encoding="utf-8"), is_pkg
        ):
            imported_pkg = _top_package(imported_module)
            if imported_pkg is None or imported_pkg == importer_pkg:
                continue
            if not _edge_allowed(importer_pkg, imported_pkg):
                edge_violations.add((module, imported_module))
            if name and name.startswith("_") and not name.startswith("__"):
                private_crossings.add((module, imported_module, name))
    return edge_violations, private_crossings


# --- Grandfather baselines (snapshot of main; shrink-only) --------------------
# Regenerate the *shape* by re-running _scan_tree(PKG_ROOT); every entry below
# is a real violation as of this commit. See module docstring for the rules.

EDGE_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        # --- non-stack packages (unranked → all edges grandfathered) ---------
        ("thinkweave.__main__", "thinkweave.surfaces.cli"),
        ("thinkweave.mcp.server", "thinkweave.surfaces.mcp.server"),
        ("thinkweave.onboarding.claude_code_seed", "thinkweave.core.config"),
        ("thinkweave.onboarding.claude_code_seed", "thinkweave.core.schemas"),
        ("thinkweave.onboarding.claude_code_seed", "thinkweave.core.vault"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.core.agent_client"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.core.api_config"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.core.config"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.core.indexer"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.core.vault"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.operations.extract"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.operations.landing"),
        ("thinkweave.onboarding.enrich_batch", "thinkweave.synthesis.concepts"),
        (
            "thinkweave.onboarding.enrich_batch",
            "thinkweave.synthesis.session_synthesis",
        ),
        ("thinkweave.scheduling", "thinkweave.core.config"),
        ("thinkweave.scheduling.cron", "thinkweave.core.config"),
        ("thinkweave.scheduling.registry", "thinkweave.core.config"),
        ("thinkweave.scheduling.registry", "thinkweave.core.plugin_route"),
        ("thinkweave.scheduling.taskscheduler", "thinkweave.core.config"),
        ("thinkweave.surfaces.cli.index", "thinkweave.onboarding.claude_code_seed"),
        ("thinkweave.surfaces.cli.index", "thinkweave.onboarding.enrich_batch"),
        ("thinkweave.surfaces.cli.schedule", "thinkweave.scheduling"),
        ("thinkweave.surfaces.cli.schedule", "thinkweave.scheduling.cron"),
        ("thinkweave.surfaces.cli.schedule", "thinkweave.scheduling.taskscheduler"),
        # --- upward: core → {synthesis, acquisition} -------------------------
        ("thinkweave.core.api_config", "thinkweave.acquisition.sources.config"),
        ("thinkweave.core.indexer", "thinkweave.synthesis.concepts"),
        ("thinkweave.core.indexer", "thinkweave.synthesis.hub"),
        ("thinkweave.core.indexer", "thinkweave.synthesis.landing"),
        ("thinkweave.core.vault", "thinkweave.acquisition.sources"),
        ("thinkweave.core.vault", "thinkweave.synthesis.theme_hub"),
        # --- sibling: retrieval ↔ {synthesis, acquisition} -------------------
        (
            "thinkweave.retrieval.context",
            "thinkweave.acquisition.sources.priorities",
        ),
        ("thinkweave.retrieval.context", "thinkweave.synthesis.landing"),
        # --- upward / sibling: acquisition → {operations, synthesis} ---------
        (
            "thinkweave.acquisition.discover.strategies.decision_review",
            "thinkweave.operations.prompts",
        ),
        (
            "thinkweave.acquisition.discover.strategies.prompt_gap",
            "thinkweave.operations.search",
        ),
        (
            "thinkweave.acquisition.discover.strategies.prompt_gap",
            "thinkweave.synthesis.concepts",
        ),
        # --- upward / sibling: synthesis → {operations, retrieval, acquisition}
        ("thinkweave.synthesis.concepts", "thinkweave.operations"),
        ("thinkweave.synthesis.concepts", "thinkweave.operations.dream"),
        ("thinkweave.synthesis.geometry", "thinkweave.operations.dream"),
        ("thinkweave.synthesis.hub", "thinkweave.retrieval.temporal"),
        ("thinkweave.synthesis.landing", "thinkweave.acquisition.sources.config"),
        ("thinkweave.synthesis.landing", "thinkweave.operations.dream"),
        ("thinkweave.synthesis.landing", "thinkweave.operations.reports"),
        ("thinkweave.synthesis.landing", "thinkweave.retrieval.temporal"),
        ("thinkweave.synthesis.theme_candidates", "thinkweave.acquisition.sources"),
        ("thinkweave.synthesis.theme_candidates", "thinkweave.operations"),
    }
)

PRIVATE_BASELINE: frozenset[tuple[str, str, str]] = frozenset(
    {
        (
            "thinkweave.core.api_config",
            "thinkweave.acquisition.sources.config",
            "_parse_simple_yaml",
        ),
        (
            "thinkweave.operations.dream",
            "thinkweave.synthesis.concept_hub",
            "_safe_hub_maps",
        ),
        (
            "thinkweave.operations.dream",
            "thinkweave.synthesis.theme_candidates",
            "_excerpt",
        ),
        (
            "thinkweave.surfaces.cli._hubs_link",
            "thinkweave.synthesis.concept_hub",
            "_safe_hub_maps",
        ),
        (
            "thinkweave.surfaces.cli.hubs",
            "thinkweave.synthesis.concept_hub",
            "_strip_inline_wikilinks",
        ),
        (
            "thinkweave.surfaces.mcp.tools.extract",
            "thinkweave.operations.extract",
            "_build_decision_body",
        ),
        (
            "thinkweave.surfaces.mcp.tools.extract",
            "thinkweave.operations.extract",
            "_flush_insight",
        ),
        (
            "thinkweave.synthesis.geometry",
            "thinkweave.core.embeddings",
            "_unpack_embedding",
        ),
    }
)


def _fmt(rows) -> str:
    return "\n".join(f"  {r}" for r in sorted(rows)) or "  (none)"


class TestScanner:
    """Unit tests for the scanner primitive, against synthetic sources — the
    contract tests are only as trustworthy as the scanner underneath them."""

    def test_catches_module_level_import(self):
        got = set(
            _scan_module(
                "thinkweave.synthesis.x",
                "from thinkweave.core.config import load_config\n",
                is_pkg=False,
            )
        )
        assert got == {("thinkweave.core.config", "load_config")}

    def test_catches_deferred_function_body_import(self):
        # The mesh is partly deferred imports written to dodge import cycles;
        # a module-level-only scanner would miss exactly the edges we care about.
        src = (
            "def f():\n"
            "    from thinkweave.operations.dream import scan\n"
            "    return scan\n"
        )
        got = set(_scan_module("thinkweave.synthesis.concepts", src, is_pkg=False))
        assert got == {("thinkweave.operations.dream", "scan")}

    def test_catches_type_checking_and_plain_import(self):
        src = (
            "import typing\n"
            "if typing.TYPE_CHECKING:\n"
            "    import thinkweave.core.schemas\n"
        )
        got = set(_scan_module("thinkweave.surfaces.cli.x", src, is_pkg=False))
        assert got == {("thinkweave.core.schemas", None)}

    def test_resolves_relative_imports(self):
        # from ..core.config import X inside thinkweave.synthesis.geometry
        src = "from ..core.config import load_config\n"
        got = set(_scan_module("thinkweave.synthesis.geometry", src, is_pkg=False))
        assert got == {("thinkweave.core.config", "load_config")}

    def test_relative_import_from_package_init(self):
        # from .config import X inside the thinkweave.core package __init__
        src = "from .config import load_config\n"
        got = set(_scan_module("thinkweave.core", src, is_pkg=True))
        # same-package target; _top_package is still 'core' — boundary logic in
        # _scan_tree drops same-package edges, but the scanner still resolves it.
        assert got == {("thinkweave.core.config", "load_config")}

    def test_ignores_third_party_imports(self):
        src = "import os\nfrom pydantic import BaseModel\n"
        assert set(_scan_module("thinkweave.core.x", src, is_pkg=False)) == set()

    def test_flags_private_name_leaf(self):
        # The scanner surfaces the bound leaf; the private-crossing rule keys on
        # a single leading underscore.
        got = set(
            _scan_module(
                "thinkweave.core.api_config",
                "from thinkweave.acquisition.sources.config import "
                "_parse_simple_yaml\n",
                is_pkg=False,
            )
        )
        assert got == {
            ("thinkweave.acquisition.sources.config", "_parse_simple_yaml")
        }


class TestPackageEdges:
    def test_no_undocumented_package_edges(self):
        edges, _ = _scan_tree(PKG_ROOT)
        unlisted = edges - EDGE_BASELINE  # new violations — must be fixed
        stale = EDGE_BASELINE - edges  # freed grandfather lines — delete them
        assert not unlisted and not stale, (
            "package-edge contract broke.\n"
            "UNLISTED cross-package edges (fix the import; do NOT extend the "
            f"baseline — it may only shrink):\n{_fmt(unlisted)}\n"
            "STALE baseline lines (a grandfathered edge was removed or faked; "
            f"delete these lines):\n{_fmt(stale)}"
        )

    def test_baseline_holds_no_allowed_edges(self):
        # A documented downward edge must never be grandfathered — that would
        # let the baseline hide a legal edge and rot silently.
        offenders = {
            (imp, tgt)
            for imp, tgt in EDGE_BASELINE
            if _edge_allowed(_top_package(imp), _top_package(tgt) or "")
        }
        assert not offenders, (
            "EDGE_BASELINE lists edges that are actually documented/allowed — "
            f"remove them:\n{_fmt(offenders)}"
        )


class TestPrivateCrossings:
    def test_no_underscore_symbol_crosses_a_package_boundary(self):
        _, privates = _scan_tree(PKG_ROOT)
        unlisted = privates - PRIVATE_BASELINE
        stale = PRIVATE_BASELINE - privates
        assert not unlisted and not stale, (
            "private-crossing contract broke.\n"
            "UNLISTED underscore-private imports across a package boundary "
            f"(route through the public surface instead):\n{_fmt(unlisted)}\n"
            "STALE baseline lines (a private crossing was removed or faked; "
            f"delete these lines):\n{_fmt(stale)}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
