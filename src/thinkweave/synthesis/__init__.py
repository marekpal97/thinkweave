# provisional — finalized by Track A. These names are the stable door even
# as Track A moves implementations between modules; import from here, not
# from the submodules, in new code.
"""Synthesis layer — ontology, hubs, themes, and judgment.

Operations: ontology load/gate (``load_ontology``, ``resolve_concept``,
``split_concepts_by_ontology``, ``promote_proposed_concept``), hub
path/frontmatter plumbing (``concept_hub_path``, ``build_id_path_map``,
``set_frontmatter_keys``), theme minting and catalyst-log parse/render
(``detect_signals``, ``mint_theme_from_signal``,
``parse_theme_catalyst_log``, ``render_theme_body_skeleton``), decision
judgment read-side (``evaluate_decision``), landing composition
(``state_of_play``, ``write_landing_docs``).

Invariants: concepts must pass the strict ontology gate — non-matches are
shunted to ``proposed_concepts``, never silently canonicalised. Judgment
here is read-side only; decision writeback lives in
``operations.decisions`` (the other half of the decision primitive, see
``synthesis/judge.py``).

Storage: ontology YAMLs under ``vault/config/``; hubs and themes are
vault notes.

Extension points: drift-v2 geometry (``synthesis.geometry``) and the
memory seam (``synthesis.memory_seam``) are consumed as modules — their
per-function surface is Track A's to finalize.
"""

from thinkweave.synthesis.concept_hub import concept_hub_path
from thinkweave.synthesis.concepts import (
    load_ontology,
    promote_proposed_concept,
    resolve_concept,
    split_concepts_by_ontology,
)
from thinkweave.synthesis.hub import build_id_path_map, set_frontmatter_keys
from thinkweave.synthesis.judge import evaluate_decision
from thinkweave.synthesis.landing import state_of_play, write_landing_docs
from thinkweave.synthesis.theme_candidates import detect_signals, mint_theme_from_signal
from thinkweave.synthesis.theme_hub import (
    parse_theme_catalyst_log,
    render_theme_body_skeleton,
)

__all__ = [
    "build_id_path_map",
    "concept_hub_path",
    "detect_signals",
    "evaluate_decision",
    "load_ontology",
    "mint_theme_from_signal",
    "parse_theme_catalyst_log",
    "promote_proposed_concept",
    "render_theme_body_skeleton",
    "resolve_concept",
    "set_frontmatter_keys",
    "split_concepts_by_ontology",
    "state_of_play",
    "write_landing_docs",
]
