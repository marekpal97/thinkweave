"""Core layer — vault I/O, config, and the derived SQLite index.

Operations: load config (``load_config``/``Config``), read/write notes
through ``VaultManager`` + the frontmatter codec (``parse_frontmatter``/
``render_frontmatter``), rebuild the derived index (``Indexer``).

Invariants: markdown is the source of truth; SQLite is derived and
rebuildable. ``core`` sits at the bottom of the layer stack — the two
known upward edges (``vault.py`` → acquisition source registry,
``indexer.py`` → synthesis landing) are legacy and must not grow.

Storage: the vault directory tree (notes) and ``.weave/index.db``
(derived). Config resolves through ``vault/config/``.

Extension points: new note types register in ``schemas.NoteType``; new
index tables go through ``Indexer`` (see docs/SCHEMA.md).

Import order below is load-bearing: ``vault`` (→ acquisition door) and
``indexer`` (→ synthesis door) must come after the leaf modules they and
their transitive imports reach back for.
"""

from thinkweave.core._utils import as_list
from thinkweave.core.config import Config, load_config, resolve_config_file
from thinkweave.core.schemas import NoteMeta, NoteType
from thinkweave.core.vault import VaultManager, parse_frontmatter, render_frontmatter
from thinkweave.core.indexer import Indexer

__all__ = [
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
