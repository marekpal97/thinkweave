"""``weave_sources_config`` — read-only access to merged sources.yaml."""

from __future__ import annotations

import json

from thinkweave.core.config import Config


def tool_schemas() -> list:
    from mcp.types import Tool

    return [
        Tool(
            name="weave_sources_config",
            description=(
                "Read-only access to the merged sources.yaml config. "
                "Returns the dict from load_user_config(vault_root) — "
                "DEFAULT_CONFIG overlaid with the user's "
                "vault/.weave/sources.yaml.\n\n"
                "Use when a skill needs to know the active drain_strategy, "
                "queue path, dedup_keys, or research_skill binding without "
                "re-parsing the YAML itself."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


def handle(cfg: Config, args: dict):
    from mcp.types import TextContent

    from thinkweave.acquisition.sources import load_user_config

    merged = load_user_config(cfg.vault_root)
    return [TextContent(type="text", text=json.dumps(merged, indent=2))]
