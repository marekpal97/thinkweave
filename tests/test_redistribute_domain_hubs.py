"""Regression tests for scripts/redistribute_domain_path_hubs.py.

Locks the routing rule for redistributing entries from domain-path
topic hubs into specific-concept hubs (n-4dd8ad62). The rule itself is
small but several edge cases need locking: domain-path filtering,
own-slug filtering (both dotted and dashed), highest-count selection,
and alphabetical tiebreakers.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from redistribute_domain_path_hubs import pick_target_concept  # noqa: E402


class TestPickTargetConcept:
    def _counts(self, **kwargs) -> Counter:
        return Counter(kwargs)

    def test_picks_highest_count_concept(self):
        target = pick_target_concept(
            source_concepts={"pytest", "fastapi", "logging"},
            hub_slug="swe-python",
            concept_counts=self._counts(pytest=20, fastapi=5, logging=3),
        )
        assert target == "pytest"

    def test_alphabetical_tiebreaker_when_counts_equal(self):
        # Both candidates have the same count → alphabetical wins.
        target = pick_target_concept(
            source_concepts={"zebra", "apple"},
            hub_slug="swe-python",
            concept_counts=self._counts(zebra=10, apple=10),
        )
        assert target == "apple"

    def test_drops_domain_path_concepts(self):
        # Obsolete after the slash→dash rename (n-23928237 follow-up):
        # `pick_target_concept`'s "domain-path = contains '/'" filter is now
        # always satisfied because no canonical concept carries a slash.
        # Detecting parent-domain concepts (e.g. ``swe-python``) would
        # require an ontology lookup the script doesn't do; that's a
        # separate redistribution arc. For now: when only dash-form
        # parent-domain candidates are present, the script picks one
        # alphabetically rather than refusing.
        target = pick_target_concept(
            source_concepts={"swe-python", "ml-deep-learning"},
            hub_slug="swe-python",
            concept_counts=self._counts(),
        )
        # ``swe-python`` is filtered as the hub's own slug; only
        # ``ml-deep-learning`` remains.
        assert target == "ml-deep-learning"

    def test_drops_hubs_own_dotted_slug(self):
        # Hub is swe-python.md; the dotted form swe-python on the source
        # note must NOT be picked. Other concept must win.
        target = pick_target_concept(
            source_concepts={"swe-python", "pytest"},
            hub_slug="swe-python",
            concept_counts=self._counts(pytest=5),
        )
        assert target == "pytest"

    def test_drops_hubs_own_dashed_slug(self):
        # If the source note also has the dashed form (rare but possible
        # if the hub-slug crept into a concepts: array), filter that too.
        target = pick_target_concept(
            source_concepts={"swe-python", "pytest"},
            hub_slug="swe-python",
            concept_counts=self._counts(pytest=5, **{"swe-python": 100}),
        )
        assert target == "pytest"

    def test_returns_none_when_only_hub_slug_remains(self):
        target = pick_target_concept(
            source_concepts={"swe-python"},
            hub_slug="swe-python",
            concept_counts=self._counts(**{"swe-python": 50}),
        )
        assert target is None

    def test_returns_none_when_source_has_no_concepts(self):
        target = pick_target_concept(
            source_concepts=set(),
            hub_slug="swe-python",
            concept_counts=self._counts(),
        )
        assert target is None

    def test_concept_with_zero_count_is_still_a_valid_target(self):
        # A specific concept that hasn't accrued elsewhere is still a
        # valid target — better than dropping the entry. Tiebreaker
        # falls to alphabetical when both counts are zero.
        target = pick_target_concept(
            source_concepts={"foo", "bar"},
            hub_slug="swe-python",
            concept_counts=self._counts(),
        )
        assert target == "bar"

    def test_does_not_drop_concept_just_because_it_starts_with_swe(self):
        # "swe-tooling" is a specific concept, not the hub's own slug.
        # Make sure we filter on equality, not prefix match.
        target = pick_target_concept(
            source_concepts={"swe-tooling"},
            hub_slug="swe-python",
            concept_counts=self._counts(**{"swe-tooling": 5}),
        )
        assert target == "swe-tooling"
