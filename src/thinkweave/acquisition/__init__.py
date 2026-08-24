"""Acquisition layer — the discover → drain producer/consumer spine.

- ``sources``   — source-type registry, queues, extractors (the atomic units)
- ``discover``  — producer rail: strategies that emit queue items / plans
- ``importers`` — bulk historical imports (claude-code, chatgpt, files)

Operations: source-type lookup (``get_spec``/``all_specs``/``REGISTRY``),
per-type JSONL queues (``Queue``), sources.yaml config
(``load_user_config``/``DEFAULT_CONFIG``), source-note frontmatter
(``build_source_frontmatter``).

Invariants: acquisition state (queued URLs) lives outside the knowledge
graph — a queued item is not yet vault content. Adding a source type is
one ``SourceTypeSpec`` + one skill file; ``vault.py`` dispatches on
``spec.layout`` and needs no edit.

Storage: ``vault/.weave/queues/<source_type>.jsonl`` (+ ``_processed/``);
config in ``vault/config/sources.yaml``.

Extension points: register specs via ``sources.registry``; discover
strategies plug into ``discover.strategies``.

``importers`` and ``discover`` are deliberately NOT re-exported here:
their modules import ``core.vault`` at module top level, and ``core``'s
``vault.py`` imports this package's source registry — an eager re-export
would close that cycle. Import them by submodule path
(``thinkweave.acquisition.importers.claude_history`` etc.).
"""

from thinkweave.acquisition.sources import (
    DEFAULT_CONFIG,
    REGISTRY,
    Layout,
    Queue,
    SourceTypeSpec,
    all_specs,
    build_source_frontmatter,
    get_spec,
    load_user_config,
    load_user_specs,
    normalize,
)

__all__ = [
    "DEFAULT_CONFIG",
    "Layout",
    "Queue",
    "REGISTRY",
    "SourceTypeSpec",
    "all_specs",
    "build_source_frontmatter",
    "get_spec",
    "load_user_config",
    "load_user_specs",
    "normalize",
]
