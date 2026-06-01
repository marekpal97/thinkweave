"""mail_poll discover strategy — config-driven mail-fetch planner.

Mail connectors (Gmail today; Outlook / IMAP later) only expose
themselves through the MCP runtime — there's no headless Python path
into ``mcp__claude_ai_Gmail__search_threads``. So this strategy
deliberately stops at planning: it reads per-source-type config,
validates that the allowlist is non-empty, and emits one
``mail_fetch_needed`` descriptor per source type. The ``/discover``
skill picks it up, runs the Gmail dance through its MCP tools, and
enqueues each fetched message via ``mem_queue``.

This keeps the strategy testable in plain Python (no MCP context
needed) while preserving the rule that "discover produces queue items".

Config shape (``sources.yaml: sources.<slug>``):

    mail_provider: gmail               # required — v1 is gmail-only; outlook/imap deferred
    senders: [a@b.com, example.com]    # required — empty allowlist halts (no whole-inbox fan-out)
    mail_query: ""                     # optional extra filter (e.g. "is:unread")
    processed_label: mem-processed     # required — excluded from query, applied after write
    lookback_days: 30                  # required — translated to provider syntax
    dedup_keys: [message_id, url]

The field was previously ``mail_connector`` (C21 rename, 2026-05-31).
Both names are accepted at read time — ``mail_provider`` takes
precedence; ``mail_connector`` is the back-compat fallback so existing
vault configs keep working.

Optional ``_runtime.source_type`` (set by ``mem discover --source-type``)
limits planning to one source type.
"""

from __future__ import annotations

from typing import Any


class MailPollStrategy:
    name = "mail_poll"

    def run(
        self,
        vault: Any,
        project: str | None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = config or {}
        runtime = config.get("_runtime") or {}
        filter_type = runtime.get("source_type") or None

        sources = config.get("sources") or {}
        out: list[dict[str, Any]] = []

        for slug, spec in sources.items():
            if filter_type and slug != filter_type:
                continue
            if not isinstance(spec, dict):
                continue
            # C21: prefer mail_provider; fall back to mail_connector
            # for pre-rename vault configs.
            if not (spec.get("mail_provider") or spec.get("mail_connector")):
                continue
            plan = self._plan_for(slug, spec)
            out.append(plan)
        return out

    @staticmethod
    def _plan_for(slug: str, spec: dict[str, Any]) -> dict[str, Any]:
        connector = str(
            spec.get("mail_provider") or spec.get("mail_connector") or ""
        )
        senders = list(spec.get("senders") or [])
        mail_query = str(spec.get("mail_query") or "").strip()
        processed_label = str(spec.get("processed_label") or "mem-processed")
        # 0 is a legitimate "no lookback bound" — only default when missing/None.
        raw_lookback = spec.get("lookback_days")
        lookback_days = int(raw_lookback) if raw_lookback is not None else 30
        dedup_keys = list(spec.get("dedup_keys") or ["message_id", "url"])

        if not senders and not mail_query:
            return {
                "strategy": "mail_poll",
                "kind": "external",
                "status": "error",
                "source_type": slug,
                "reason": "empty_allowlist",
                "hint": (
                    f"Add senders to vault/.mem/sources.yaml under "
                    f"sources.{slug}.senders — empty allowlist halts to "
                    f"avoid whole-inbox fan-out."
                ),
            }

        # Gmail is the only wired connector. Compose its query inline so the
        # skill has nothing to invent. Other connectors return their own
        # native filter when implemented.
        effective_query = ""
        if connector == "gmail":
            parts: list[str] = []
            if senders:
                parts.append("from:(" + " OR ".join(str(s) for s in senders) + ")")
            if mail_query:
                parts.append(mail_query)
            parts.append(f"-label:{processed_label}")
            if lookback_days > 0:
                parts.append(f"newer_than:{lookback_days}d")
            effective_query = " ".join(parts)
        elif connector in ("outlook", "imap"):
            return {
                "strategy": "mail_poll",
                "kind": "external",
                "status": "error",
                "source_type": slug,
                "reason": "connector_not_implemented",
                "hint": f"mail_provider '{connector}' is reserved; only 'gmail' is wired in v1 (outlook/imap deferred).",
            }

        return {
            "strategy": "mail_poll",
            "kind": "mail_fetch_needed",
            "source_type": slug,
            "connector": connector,
            "effective_query": effective_query,
            "processed_label": processed_label,
            "lookback_days": lookback_days,
            "dedup_keys": dedup_keys,
            "senders": senders,
            "mail_query_extras": mail_query,
        }


STRATEGY = MailPollStrategy()
