# thinkweave (Claude Code plugin shell)

This directory is the Claude Code plugin manifest for thinkweave. It
registers the MCP server and exposes the skills under `commands/` as
slash commands.

The skill files in `commands/` are symlinks back to the canonical
sources at the repo root (`../../../../commands/*.md`) so the plugin
tracks the same files developers edit.

## Install

Install the Python package first (it provides the `weave` CLI that this
plugin invokes):

```bash
pip install -e ".[all]"     # from the repo root; [all] = mcp+embeddings+hubs
```

Then add the plugin to Claude Code:

```
/plugin add ./.claude/plugins/thinkweave
/onboard
```

`post_install` hook will run `weave hooks install` automatically; the
`/onboard` skill walks the rest of the bootstrap (vault location,
source types, retroactive Claude session import, first index +
landing-doc generation).

See the [repo README](../../../README.md) for the full picture.
