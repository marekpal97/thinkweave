"""Tests for prediction history primitives."""

from __future__ import annotations

from datetime import datetime

import pytest

from personal_mem.synthesis.prediction import VERDICTS, append_verdict, read_history


class TestReadHistory:
    def test_empty_fm_returns_empty_list(self) -> None:
        assert read_history({}) == []

    def test_legacy_prediction_match_synthesizes_single_entry(self) -> None:
        history = read_history(
            {"prediction_match": "confirmed", "judged_at": "2026-05-14T00:00:00+00:00"}
        )
        assert history == [
            {
                "match": "confirmed",
                "judged_at": "2026-05-14T00:00:00+00:00",
                "reason": "legacy",
            }
        ]

    def test_legacy_prediction_match_without_judged_at(self) -> None:
        history = read_history({"prediction_match": "confirmed"})
        assert history == [
            {"match": "confirmed", "judged_at": "", "reason": "legacy"}
        ]

    def test_legacy_unknown_match_clamps_to_unevaluable(self) -> None:
        history = read_history({"prediction_match": "wat"})
        assert history[0]["match"] == "unevaluable"

    def test_empty_history_list_does_not_fall_back_to_legacy(self) -> None:
        """Explicit empty list signals migration ran — trust it."""
        fm = {"prediction_history": [], "prediction_match": "confirmed"}
        assert read_history(fm) == []

    def test_history_list_returned_as_is(self) -> None:
        entries = [
            {"match": "pending", "judged_at": "2026-05-25T00:00:00+00:00", "reason": "initial"},
            {"match": "confirmed", "judged_at": "2026-05-26T00:00:00+00:00", "reason": "drain confirmed it"},
        ]
        assert read_history({"prediction_history": entries}) == entries

    def test_non_dict_entries_filtered_out(self) -> None:
        fm = {
            "prediction_history": [
                {"match": "confirmed", "judged_at": "x", "reason": "y"},
                "garbage",
                42,
                {"match": "stale", "judged_at": "z", "reason": "w"},
            ]
        }
        history = read_history(fm)
        assert len(history) == 2
        assert all(isinstance(e, dict) for e in history)

    def test_unknown_match_in_history_clamps_to_unevaluable(self) -> None:
        fm = {
            "prediction_history": [
                {"match": "wat", "judged_at": "x", "reason": "y"},
                {"match": "confirmed", "judged_at": "z", "reason": "w"},
            ]
        }
        history = read_history(fm)
        assert history[0]["match"] == "unevaluable"
        assert history[1]["match"] == "confirmed"

    def test_idempotent(self) -> None:
        fm = {"prediction_match": "confirmed", "judged_at": "2026-05-14T00:00:00+00:00"}
        assert read_history(fm) == read_history(fm)


class TestAppendVerdict:
    def test_append_to_empty_fm(self) -> None:
        delta = append_verdict(
            {},
            match="pending",
            reason="awaiting evidence",
            judged_at="2026-05-25T00:00:00+00:00",
        )
        assert delta == {
            "prediction_history": [
                {
                    "match": "pending",
                    "judged_at": "2026-05-25T00:00:00+00:00",
                    "reason": "awaiting evidence",
                }
            ],
            "prediction_match": "pending",
            "judged_at": "2026-05-25T00:00:00+00:00",
        }

    def test_append_extends_existing_history(self) -> None:
        fm = {
            "prediction_history": [
                {
                    "match": "pending",
                    "judged_at": "2026-05-25T00:00:00+00:00",
                    "reason": "awaiting evidence",
                }
            ],
            "prediction_match": "pending",
        }
        delta = append_verdict(
            fm,
            match="confirmed",
            reason="drain produced 3/3 accepted",
            judged_at="2026-05-26T00:00:00+00:00",
        )
        assert len(delta["prediction_history"]) == 2
        assert delta["prediction_history"][-1]["match"] == "confirmed"
        assert delta["prediction_match"] == "confirmed"
        assert delta["judged_at"] == "2026-05-26T00:00:00+00:00"

    def test_append_promotes_legacy_match_into_history(self) -> None:
        fm = {
            "prediction_match": "unevaluable",
            "judged_at": "2026-05-14T00:00:00+00:00",
        }
        delta = append_verdict(
            fm,
            match="stale",
            reason="superseded by dec-NEW",
            judged_at="2026-05-26T00:00:00+00:00",
        )
        assert len(delta["prediction_history"]) == 2
        assert delta["prediction_history"][0]["reason"] == "legacy"
        assert delta["prediction_history"][1]["match"] == "stale"
        assert delta["prediction_match"] == "stale"

    def test_unknown_match_clamps_to_unevaluable(self) -> None:
        delta = append_verdict(
            {}, match="wat", reason="x", judged_at="2026-05-26T00:00:00+00:00"
        )
        assert delta["prediction_match"] == "unevaluable"
        assert delta["prediction_history"][-1]["match"] == "unevaluable"

    def test_default_judged_at_is_iso_utc(self) -> None:
        delta = append_verdict({}, match="pending", reason="x")
        ts = delta["judged_at"]
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None

    @pytest.mark.parametrize("verdict", sorted(VERDICTS))
    def test_all_verdicts_round_trip(self, verdict: str) -> None:
        delta = append_verdict(
            {}, match=verdict, reason="x", judged_at="2026-05-26T00:00:00+00:00"
        )
        assert delta["prediction_match"] == verdict
        assert delta["prediction_history"][-1]["match"] == verdict
