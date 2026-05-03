"""Per-tool MCP modules.

The legacy MCP surface registers all tools inline inside
``surfaces/mcp/server.py``. As Phase 4 C splits the surface up, new tools
land here as small modules holding the schema descriptor + handler body
the server imports.
"""
