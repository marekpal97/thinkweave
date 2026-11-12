"""The System 1 decision provider, its site config and its rail text seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from thinkweave.core import decision_provider
from thinkweave.core.config import Config, _check_rail_keys
from thinkweave.core.decision_provider import (
    FakeDecisionProvider,
    Question,
    SystemOneProvider,
    build_from_vault,
)
from thinkweave.operations.verdicts import run_site


# A real `typesafe/jev-1.14` reply via OpenRouter, captured 2026-11-01.
_JEV_REPLY = {
    "model": "typesafe/jev-1.14-20261014",
    "answers": {
        "relevant": {"type": "noul", "noul": 0.57},
        "kind": {
            "type": "choice",
            "choice": "cooking",
            "probabilities": {"cooking": 1, "coding": 0, "travel": 0},
            "confidence": 1,
        },
    },
    "usage": {"input_tokens": 352, "output_tokens": 60, "cost": 0.000014784},
}


def test_systemone_backend_speaks_the_wire_format(monkeypatch):
    sent: dict = {}

    def fake_post(url, *, headers, json, timeout, verify):
        sent.update(url=url, headers=headers, body=json)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: _JEV_REPLY)

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    provider = build_from_vault(None)
    assert isinstance(provider, SystemOneProvider)
    decision = provider.decide(
        "The user asks how to bake bread.",
        [
            Question("relevant", "noul", "Is a sourdough note relevant?"),
            Question(
                "kind",
                "choice",
                "What kind of request is this?",
                {"cooking": "about food", "coding": "about software", "travel": "trips"},
            ),
        ],
    )

    assert sent["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["body"] == {
        "model": "typesafe/jev-1.14",
        "state": "The user asks how to bake bread.",
        "questions": {
            "relevant": {"type": "noul", "instructions": "Is a sourdough note relevant?"},
            "kind": {
                "type": "choice",
                "instructions": "What kind of request is this?",
                "criteria": {
                    "cooking": "about food",
                    "coding": "about software",
                    "travel": "trips",
                },
            },
        },
    }
    relevant, kind = decision.answers
    assert (relevant.question, relevant.value, relevant.probs) == (
        "relevant", 0.57, {"noul": 0.57},
    )
    assert (kind.value, kind.probs) == ("cooking", {"cooking": 1, "coding": 0, "travel": 0})
    assert decision.model == "typesafe/jev-1.14-20261014"
    assert decision.backend == "openrouter"
    assert decision.cost == pytest.approx(0.000014784)


def test_systemone_backend_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(decision_provider, "get_provider_key", lambda _p: None)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_from_vault(None).decide("s", [Question("q", "noul", "?")])


def _rail(tmp_path: Path, smoke_toml: str | None) -> Path:
    rail = tmp_path / "rail"
    (rail / "thinkweave" / "questions").mkdir(parents=True)
    (rail / "thinkweave" / "config.toml").write_text("", encoding="utf-8")
    if smoke_toml is not None:
        (rail / "thinkweave" / "questions" / "smoke.toml").write_text(
            smoke_toml, encoding="utf-8"
        )
    return rail


_SMOKE_SLOTS = {"prompt": "How do I bake bread?", "candidate": "Sourdough starter notes"}


def test_packaged_default_text_renders_without_a_rail_file(tmp_path):
    cfg = Config(vault_root=tmp_path / "vault", rsi_rail=_rail(tmp_path, None))
    fake = FakeDecisionProvider()
    run_site(cfg, "smoke", _SMOKE_SLOTS, provider=fake)

    state, questions = fake.calls[0]
    assert "How do I bake bread?" in state
    assert "Sourdough starter notes" in state
    assert [q.name for q in questions] == ["relevance"]


def test_rail_site_text_changes_the_state_the_backend_receives(tmp_path):
    rail = _rail(
        tmp_path,
        'state = "RAIL says: {prompt} / {candidate}"\n'
        "[questions.bears]\n"
        'type = "noul"\n'
        'instructions = "Does {candidate} bear on it?"\n',
    )
    cfg = Config(vault_root=tmp_path / "vault", rsi_rail=rail)
    fake = FakeDecisionProvider()
    run_site(cfg, "smoke", _SMOKE_SLOTS, provider=fake)

    state, questions = fake.calls[0]
    assert state == "RAIL says: How do I bake bread? / Sourdough starter notes"
    assert questions == [
        Question("bears", "noul", "Does Sourdough starter notes bear on it?")
    ]


def test_decision_site_table_is_a_rail_key(tmp_path, monkeypatch):
    rail = _rail(tmp_path, None)
    (rail / "thinkweave" / "config.toml").write_text(
        "[verdicts.prompt_gate]\n"
        "enabled = true\n"
        'backend = "openrouter"\n'
        'model = "typesafe/jev-1.14"\n'
        "accept = 0.7\n"
        "pool_size = 30\n"
        "deadline_ms = 1500\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THINKWEAVE_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("THINKWEAVE_RSI_RAIL", str(rail))
    from thinkweave.core.config import load_config

    cfg = load_config()
    site = cfg.decision_site("prompt_gate")
    assert (site.enabled, site.accept, site.pool_size) == (True, 0.7, 30)
    assert cfg.decision_site("other").enabled is False

    with pytest.raises(ValueError, match="verdicts.prompt_gate.thresh"):
        _check_rail_keys({"verdicts": {"prompt_gate": {"thresh": 1}}}, Path("x"))


def test_an_unknown_decision_site_is_refused_by_name(tmp_path, monkeypatch):
    """The site names are a closed set (``operations.verdicts.SITES``): a
    table for any other name fails the load and the refusal lists the
    known sites, in the rail file and in the vault file alike."""
    from thinkweave.core.config import Config, _apply_knobs

    with pytest.raises(ValueError, match=r"unknown key 'verdicts.promt_gate'"):
        _check_rail_keys({"verdicts": {"promt_gate": {"enabled": True}}}, Path("x"))
    with pytest.raises(
        ValueError, match=r"unknown site 'promt_gate'.*prompt_gate, ex_post, smoke"
    ):
        _apply_knobs(Config(vault_root=tmp_path), {"verdicts": {"promt_gate": {"enabled": True}}})


def test_the_rail_template_lists_every_decision_site():
    """``weave policy inventory --toml`` renders one ``[verdicts.<site>]``
    table per site from the knob metadata, so the rail file never names a
    key the loader does not read."""
    from thinkweave.core.config import iter_rail_addresses
    from thinkweave.operations.verdicts import SITES

    addresses = set(iter_rail_addresses())
    for site in SITES:
        assert (f"verdicts.{site}", "enabled") in addresses
        assert (f"verdicts.{site}", "accept") in addresses
