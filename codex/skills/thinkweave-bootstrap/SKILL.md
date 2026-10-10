---
name: thinkweave-bootstrap
description: Install or diagnose ThinkWeave under Codex, especially on native Windows when weave is missing, resolves but cannot import, or is blocked by the sandbox.
---

# ThinkWeave Bootstrap

Run the read-only diagnostic first, from the ThinkWeave checkout directory —
this route needs nothing on PATH, so it also works when `weave` itself is the
thing that is missing:

```powershell
uv run --project . weave doctor --mcp
```

If `weave` is missing or cannot import under the Codex sandbox, ask permission
to run the existing checkout's installer outside that restriction — again from
the checkout directory, and again without relying on `weave` resolving:

```powershell
uv run --project . weave install --yes --harness codex
```

The installer performs the dependency sync once, writes a native-Windows
launcher that uses module execution without live `uv` synchronization or the
editable `.pth`, and makes that launcher available on the user PATH. Restart
Codex after installation, then run the doctor again.

If `weave` still does not resolve after that restart, sign out of Windows and
back in once: the installer broadcasts the PATH change, but a process that
never re-reads its environment keeps the PATH it was launched with.

Do not silently substitute FTS when the doctor reports that semantic retrieval
is unavailable; surface that as a separate retrieval failure.
