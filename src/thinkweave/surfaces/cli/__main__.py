"""``python -m thinkweave.surfaces.cli`` — module entry for the CLI.

Lets ``bin/weave`` launch through ``uv run --no-sync … python -m`` like the
hook and MCP launchers do (#156), instead of the ``weave`` console script,
whose ``uv run`` form implicitly re-syncs the venv to ``--extra mcp`` only and
silently uninstalls every other extra (news/embeddings/gemini/youtube).
"""

from thinkweave.surfaces.cli import main

if __name__ == "__main__":
    main()
