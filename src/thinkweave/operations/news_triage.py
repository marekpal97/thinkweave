"""News-item triage on System 1.

Stage 1 of the news pipeline. :func:`triage` asks the ``news_triage``
question set two things per queued item: keep it, keep it unfiled or drop
it, and which active theme it belongs to. An item whose top verdict
probability falls inside the question set's ``escalate`` band is not decided
here: it goes to the ``news-triage-worker`` subagent. So does every item
while the question set is off, and any item whose call fails.
"""

from __future__ import annotations

from dataclasses import dataclass

from thinkweave.acquisition.sources.queue import QueueItem
from thinkweave.core.config import Config
from thinkweave.core.decision_provider import DecisionProvider
from thinkweave.operations.verdicts import (
    NEWS_TRIAGE,
    ask_question_set,
    record_miss,
    record_verdict,
)

KEEP = "keep"
KEEP_UNFILED = "keep_unfiled"

#: The outcome of an item System 1 did not decide.
ESCALATE = "escalate"

#: The option of the theme question that names no theme.
NO_THEME = "none"


def triage(
    cfg: Config, items: list[QueueItem], *, provider: DecisionProvider | None = None
) -> list["Triage"]:
    """One :class:`Triage` per item, in order; each call is recorded as a
    session-less verdict whose subject is the item's queue id."""
    policy = cfg.question_set(NEWS_TRIAGE)
    if not policy.enabled:
        off = "the news_triage question set is off"
        return [Triage(str(item.get("id") or ""), ESCALATE, reason=off) for item in items]
    themes = _active_themes(cfg)
    return [_triage_item(cfg, item, themes, policy.escalate, provider) for item in items]


@dataclass(frozen=True)
class Triage:
    """One item's stage-1 outcome: ``keep`` (with its ``theme_id``),
    ``keep_unfiled``, ``drop``, or ``escalate`` when the triage worker must
    decide. ``probability`` is the one behind the model's verdict."""

    id: str
    verdict: str
    theme_id: str | None = None
    probability: float | None = None
    reason: str = ""


def _triage_item(
    cfg: Config,
    item: QueueItem,
    themes: dict[str, dict[str, str]],
    band: tuple[float, float],
    provider: DecisionProvider | None,
) -> Triage:
    item_id = str(item.get("id") or "")
    try:
        decision = ask_question_set(
            cfg, NEWS_TRIAGE, {"item": _item_text(item)}, subjects=themes, provider=provider
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        record_miss(cfg, NEWS_TRIAGE, reason, source=item_id)
        return Triage(item_id, ESCALATE, reason=f"System 1 call failed: {reason}")
    answers = {answer.question: answer for answer in decision.answers}
    verdict, theme = str(answers["verdict"].value), str(answers["theme"].value)
    probability = float(answers["verdict"].probs.get(verdict, 0.0))
    low, high = band
    if low <= probability <= high:
        outcome = Triage(
            item_id, ESCALATE, None, probability,
            f"System 1 {verdict} at {probability:.2f}, inside the band {low} to {high}",
        )
    else:
        filed = verdict == KEEP and theme != NO_THEME
        outcome = Triage(
            item_id,
            KEEP_UNFILED if verdict == KEEP and not filed else verdict,
            theme if filed else None,
            probability,
            f"System 1 {verdict} at {probability:.2f}",
        )
    record_verdict(cfg, NEWS_TRIAGE, decision, subject_id=item_id, action=outcome.verdict)
    return outcome


def _active_themes(cfg: Config) -> dict[str, dict[str, str]]:
    """Each active theme's id mapped to its slots for the theme question."""
    from thinkweave.operations.dream import active_theme_meta

    return {
        theme_id: {"candidate": ": ".join(filter(None, (meta["title"], meta["essence_excerpt"])))}
        for theme_id, meta in active_theme_meta(cfg).items()
    }


def _item_text(item: QueueItem) -> str:
    """The item as the model reads it: title, outlet, tier and feed summary."""
    title = " ".join(str(item.get("title") or "").split())
    lines = [
        f"Title: {title}",
        f"Source type: {item.get('source_type') or 'news'}",
        f"Outlet: {item.get('outlet') or 'unknown'} (tier {item.get('tier', 'unknown')})",
    ]
    if item.get("summary"):
        lines.append(f"Summary: {' '.join(str(item['summary']).split())}")
    return "\n".join(lines)
