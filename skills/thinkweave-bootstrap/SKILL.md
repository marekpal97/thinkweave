---
name: thinkweave-bootstrap
description: Install or diagnose ThinkWeave under Codex, especially on native Windows when weave is missing, resolves but cannot import, or is blocked by the sandbox.
---

# ThinkWeave Bootstrap

Run the read-only diagnostic first:

```powershell
weave doctor --mcp
```

If `weave` is missing or cannot import under the Codex sandbox, ask permission
to run the existing checkout's installer outside that restriction:

```powershell
weave install --yes --harness codex
```

The installer performs the dependency sync once, writes a native-Windows
launcher that uses module execution without live `uv` synchronization or the
editable `.pth`, and makes that launcher available on the user PATH. Restart
Codex after installation, then run the doctor again.

Do not silently substitute FTS when the doctor reports that semantic retrieval
is unavailable; surface that as a separate retrieval failure.
