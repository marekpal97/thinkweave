"""Core layer — vault I/O, config, and the derived SQLite index.

Operations: load config (``load_config``/``Config``/``resolve_config_file``),
schema types (``NoteType``/``NoteMeta``), the ``as_list`` frontmatter
coercion helper, read/write notes through ``VaultManager`` + the
frontmatter codec (``parse_frontmatter``/``render_frontmatter``), rebuild
the derived index (``Indexer``).

Invariants: markdown is the source of truth; SQLite is derived and
rebuildable. ``core`` sits at the bottom of the layer stack — the two
known upward edges (``vault.py`` → acquisition source registry,
``indexer.py`` → synthesis landing) are legacy and must not grow.
Because of those edges, the ``vault``/``indexer`` names below resolve
lazily (PEP 562): a leaf import like ``thinkweave.core.config`` — the
hook handler's hot path — must not pay for synthesis/acquisition.
Intra-package code keeps submodule paths (see ARCHITECTURE.md
§"Package front doors").

Storage: the vault directory tree (notes) and ``.weave/index.db``
(derived). Config resolves through ``vault/config/``.

Extension points: new note types register in ``schemas.NoteType``; new
index tables go through ``Indexer`` (see docs/SCHEMA.md).
"""

from thinkweave.core._utils import as_list
from thinkweave.core.config import Config, load_config, resolve_config_file
from thinkweave.core.schemas import NoteMeta, NoteType

# Lazy-door idiom — kept verbatim in core/operations/surfaces __init__.
_DOOR = {
    "Indexer": "thinkweave.core.indexer",
    "VaultManager": "thinkweave.core.vault",
    "parse_frontmatter": "thinkweave.core.vault",
    "render_frontmatter": "thinkweave.core.vault",
}

__all__ = [  # noqa: F822 — _DOOR names resolve via __getattr__
    "Config",
    "Indexer",
    "NoteMeta",
    "NoteType",
    "VaultManager",
    "as_list",
    "load_config",
    "parse_frontmatter",
    "render_frontmatter",
    "resolve_config_file",
]


def __getattr__(name: str):
    try:
        module = _DOOR[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    obj = getattr(importlib.import_module(module), name)
    globals()[name] = obj  # cache: later accesses skip __getattr__
    return obj


def __dir__():
    return sorted(set(globals()) | set(__all__))
