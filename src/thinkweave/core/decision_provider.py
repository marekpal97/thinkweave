"""System 1 decision provider abstraction — typed questions in, probabilities out.

One call sends a state and typed questions to a decision model and returns one
answer per question. Backends are configured in
``vault/config/api.yaml::decisions.backends``; :func:`build_provider` returns
the right one. Mirrors :mod:`thinkweave.core.embedding_provider`.

Backends:

  • ``systemone`` — any HTTP endpoint speaking the ``{model, state, questions}``
    wire format (Jev via OpenRouter by default, Jev direct, local clones).
  • ``fake`` — deterministic, records every call; serves tests.

Question kinds: ``noul`` (one number in [0, 1]) and ``choice`` (one option
from ``criteria``, with a probability per option).
"""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from thinkweave.core.api_keys import get_provider_key


@dataclass(frozen=True)
class Question:
    """One typed question; ``criteria`` maps each ``choice`` option to its test.

    ``subject`` names what a per-subject question asks about, so one call can
    ask the same question of many candidates.
    """

    name: str
    kind: str
    instructions: str
    criteria: dict[str, str] = field(default_factory=dict)
    subject: str = ""

    @property
    def key(self) -> str:
        """The question's unique key on the wire."""
        return f"{self.name}:{self.subject}" if self.subject else self.name

    def wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria:
            body["criteria"] = dict(self.criteria)
        return body


@dataclass(frozen=True)
class Answer:
    """One question's verdict: the chosen value and the probabilities behind it."""

    question: str
    kind: str
    value: float | str
    probs: dict[str, float]
    subject: str = ""


@dataclass(frozen=True)
class Decision:
    """The answers to one call, with what served them and what it cost."""

    answers: tuple[Answer, ...]
    model: str
    backend: str
    latency_ms: int
    cost: float


@runtime_checkable
class DecisionProvider(Protocol):
    """Protocol every decision backend implements."""

    def decide(self, state: str, questions: list[Question]) -> Decision: ...


class SystemOneProvider:
    """The ``systemone`` wire format over HTTP, at a configurable base URL."""

    def __init__(self, name: str, base_url: str, key_provider: str, model: str):
        self.name = name
        self.base_url = base_url
        self.key_provider = key_provider
        self.model = model

    def decide(self, state: str, questions: list[Question]) -> Decision:
        import httpx

        api_key = get_provider_key(self.key_provider)
        if not api_key:
            raise ValueError(
                f"no API key for decision backend {self.name!r}: set "
                f"{_key_var(self.key_provider)} (checked env, vault .env, cwd .env, "
                "project .env)"
            )
        started = time.monotonic()
        response = httpx.post(
            self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "state": state,
                "questions": {q.key: q.wire() for q in questions},
            },
            timeout=60.0,
            verify=ssl.create_default_context(),
        )
        response.raise_for_status()
        latency_ms = int((time.monotonic() - started) * 1000)
        data = response.json()
        return Decision(
            answers=tuple(_parse_answer(q, data["answers"][q.key]) for q in questions),
            model=data.get("model") or self.model,
            backend=self.name,
            latency_ms=latency_ms,
            cost=float((data.get("usage") or {}).get("cost") or 0.0),
        )


class FakeDecisionProvider:
    """Deterministic backend: ``choice`` answers the first option with
    probability ``confidence``, ``noul`` the subject's value in ``nouls``
    (default 0.5); with ``error`` every call raises it.

    Records each ``(state, questions)`` call in :attr:`calls`.
    """

    def __init__(
        self,
        nouls: dict[str, float] | None = None,
        error: Exception | None = None,
        confidence: float = 1.0,
    ) -> None:
        self.calls: list[tuple[str, list[Question]]] = []
        self.nouls = nouls or {}
        self.error = error
        self.confidence = confidence

    def decide(self, state: str, questions: list[Question]) -> Decision:
        self.calls.append((state, list(questions)))
        if self.error is not None:
            raise self.error
        answers = []
        for q in questions:
            if q.kind == "choice":
                first, *rest = q.criteria
                probs = {first: self.confidence}
                probs.update(dict.fromkeys(rest, (1.0 - self.confidence) / max(len(rest), 1)))
                answers.append(Answer(q.name, q.kind, first, probs, q.subject))
            else:
                value = self.nouls.get(q.subject, 0.5)
                answers.append(Answer(q.name, q.kind, value, {"noul": value}, q.subject))
        return Decision(tuple(answers), model="fake", backend="fake", latency_ms=0, cost=0.0)


def build_provider(
    backend: str, model: str, backends: dict[str, dict[str, Any]]
) -> DecisionProvider:
    """Return the provider for ``backend`` from the configured ``backends`` block.

    An empty ``model`` takes the backend's first listed model. Unknown backend
    raises :class:`ValueError`.
    """
    if backend == "fake":
        return FakeDecisionProvider()
    spec = backends.get(backend)
    if not isinstance(spec, dict):
        raise ValueError(
            f"unknown decision backend {backend!r}; configured: "
            f"{', '.join(sorted(backends)) or 'none'}, fake"
        )
    models = spec.get("models") or []
    chosen = model or (models[0] if models else "")
    if not chosen or not spec.get("base_url"):
        raise ValueError(f"decision backend {backend!r} needs base_url and a model")
    return SystemOneProvider(backend, spec["base_url"], spec.get("key_provider", backend), chosen)


def build_from_vault(vault_root: Any, backend: str = "", model: str = "") -> DecisionProvider:
    """Build a provider from a vault's ``api.yaml``; empty ``backend`` takes its default."""
    from pathlib import Path

    from thinkweave.core.api_config import decisions_config, load_api_config

    cfg = decisions_config(load_api_config(Path(vault_root) if vault_root else None))
    return build_provider(backend or cfg["default_backend"], model, cfg["backends"])


def _parse_answer(question: Question, raw: dict[str, Any]) -> Answer:
    if question.kind == "choice":
        return Answer(
            question.name, question.kind, raw["choice"], dict(raw["probabilities"]),
            question.subject,
        )
    value = float(raw["noul"])
    return Answer(question.name, question.kind, value, {"noul": value}, question.subject)


def _key_var(provider: str) -> str:
    from thinkweave.core.api_keys import _PROVIDER_ENV_VARS

    return " or ".join(_PROVIDER_ENV_VARS.get(provider, (f"<{provider} key>",)))
