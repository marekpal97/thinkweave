"""Unit tests for ``core._utils`` — small but load-bearing shared helpers.

``as_list`` normalises frontmatter list-or-csv-string fields and is imported at
10+ sites (indexer, synthesis, operations, both surfaces). A regression here
would silently corrupt concept/cite/tag parsing everywhere, so its edge cases
are pinned directly rather than only exercised transitively.
"""

from __future__ import annotations

import pytest

from thinkweave.core._utils import as_list


class TestAsList:
    @pytest.mark.parametrize("empty", [None, "", [], (), 0, False])
    def test_falsy_yields_empty_list(self, empty):
        assert as_list(empty) == []

    def test_csv_string_split_and_stripped(self):
        assert as_list("a, b ,c") == ["a", "b", "c"]

    def test_csv_string_drops_empty_segments(self):
        assert as_list("a,,  ,b") == ["a", "b"]

    def test_single_token_string(self):
        assert as_list("solo") == ["solo"]

    def test_list_is_stringified_and_stripped(self):
        assert as_list([" a ", "b", 3]) == ["a", "b", "3"]

    def test_list_drops_blank_members(self):
        assert as_list(["a", "", "  ", "b"]) == ["a", "b"]

    def test_tuple_input(self):
        assert as_list(("x", "y")) == ["x", "y"]

    def test_return_is_always_a_fresh_list(self):
        src = ["a", "b"]
        out = as_list(src)
        assert out == ["a", "b"] and out is not src
