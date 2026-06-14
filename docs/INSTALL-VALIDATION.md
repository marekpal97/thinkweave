# Install validation runbook

End-to-end validation of a Thinkweave install, for **both** supported routes:

- **Route A — plugin via marketplace** (the end-user path)
- **Route B — plugin via symlink / `weave dev-link`** (the contributor path, `@skills-dir`)

The parts that need a live Claude Code session (plugin load, restart,
`/thinkweave:onboard` with its interactive prompts) are done by hand; everything
downstream (wiring, MCP, hooks, retrieval, scheduling, a live `/dream`) is
checked by [`scripts/validate-install.sh`](../scripts/validate-install.sh).
Manual setup + script green = the whole install surface is validated.

> Run the two routes **one at a time** — they share the server name `thinkweave`
> and collide. Use a **fresh `~/tw-test-vault` per route** so onboard's import
> truly re-runs from scratch.

---

## 0. Prerequisites

- `uv` on PATH (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- The repo cloned (this checkout)
- For `--heavy` (a live `/dream`): `claude` on PATH **and an authenticated
  Claude Code login** — your normal login. **No `ANTHROPIC_API_KEY` is
  required**; `/dream` runs on the session's model auth, it is not an Anthropic
  API call.
- Optional: `OPENAI_API_KEY` for similarity retrieval (without it, retrieval is
  FTS-only — still passes).
- Marketplace route needs the repo reachable. It is currently **private**, so
  the authenticated owner can install it; an external user needs it public.

---

## 1. Common prep (run once, before either route)

```bash
cd /path/to/thinkweave
uv sync --extra mcp                 # ensure weave / weave-hook / weave-mcp + deps (idempotent)
uv run weave uninstall --yes        # start new-user-clean: removes any raw ~/.claude.json entry
```

---

## 2. Route A — plugin via marketplace

### 2a. Set up
```bash
rm -rf ~/tw-test-vault                                   # fresh test vault
claude plugin marketplace add marekpal97/thinkweave      # one-time: register the marketplace
claude plugin install thinkweave@thinkweave              # install MCP + hooks + commands
```
> In-session equivalents also work: `/plugin marketplace add …`, `/plugin install …`.

### 2b. Launch a fresh session against the test vault
```bash
mkdir -p /tmp/tw-trial && cd /tmp/tw-trial
THINKWEAVE_VAULT=~/tw-test-vault claude
```

### 2c. Verify the plugin loaded (3 signals, in-session)
- `/mcp` → **thinkweave** listed and connected
- `/plugin list` → **thinkweave@thinkweave**
- type `/thinkweave:` → tab-completes (`onboard`, `dream`, `tighten`, …)

### 2d. Run first-run onboarding (interactive)
```
/thinkweave:onboard
```
It runs: pre-flight (uv + MCP doctor) → confirm vault path (**point at
`~/tw-test-vault`**) → import prior Claude Code history → bootstrap the
ontology → configure focus + source types → (hook-install step is a no-op on
the plugin route) → 5-check smoke test → write landing docs
(`STATE`/`DECISIONS`/`BACKLOG`/`THEMES`).

### 2e. (Optional) exercise hooks live
Do a `Write`/`Bash`, a `weave_search`, ask a question, then end the session —
fires UserPromptSubmit / PostToolUse / Stop for real.

### 2f. Run the validation script
```bash
THINKWEAVE_VAULT=~/tw-test-vault \
  /path/to/thinkweave/scripts/validate-install.sh --heavy
```
Expect: **`Validation PASSED — every hard check is green.`**
(`--heavy` runs a live `/thinkweave:dream`; add `VALIDATE_NEWS_URL=https://…`
to also validate a one-shot `/news` ingest.)

### 2g. Tear down Route A
```bash
claude plugin uninstall thinkweave@thinkweave
claude plugin marketplace remove thinkweave
rm -rf ~/tw-test-vault
rm -f ~/.config/thinkweave/config.toml          # if onboard wrote one
```

---

## 3. Route B — plugin via symlink (`weave dev-link`)

### 3a. Set up
```bash
rm -rf ~/tw-test-vault                           # fresh test vault
cd /path/to/thinkweave
uv run weave dev-link                            # symlink checkout → ~/.claude/skills/thinkweave
```

### 3b. Launch a fresh session against the test vault
```bash
mkdir -p /tmp/tw-trial && cd /tmp/tw-trial
THINKWEAVE_VAULT=~/tw-test-vault claude
```

### 3c. Verify the plugin loaded (3 signals)
- `/mcp` → **thinkweave** connected
- `/plugin list` → **thinkweave@skills-dir**
- `/thinkweave:` → tab-completes

### 3d. Run onboarding (same as 2d)
```
/thinkweave:onboard
```

### 3e. (Optional) exercise hooks live — same as 2e

### 3f. Run the validation script
```bash
THINKWEAVE_VAULT=~/tw-test-vault \
  /path/to/thinkweave/scripts/validate-install.sh --heavy
```
For this route the script additionally asserts the scheduler renders the
**namespaced** `/thinkweave:dream` cron line.

### 3g. (Optional) live-edit check
Edit a `commands/*.md` (picked up immediately) and a `hooks/` or `agents/` file
then `/reload-plugins` — confirms the contributor dev loop.

### 3h. Tear down Route B
```bash
cd /path/to/thinkweave
uv run weave dev-unlink
rm -rf ~/tw-test-vault
rm -f ~/.config/thinkweave/config.toml
```

---

## 4. Restore your real working setup (after both routes)

```bash
cd /path/to/thinkweave
uv run weave install --vault /path/to/your/real/vault --yes   # restore the raw MCP entry
# restart Claude Code → back to your normal setup, real vault untouched
```

---

## 5. What "all green" proves

The interactive new-user path done by hand (**install → load signals →
onboard**) plus the script's downstream checks together cover:

- route artifact (symlink or installed plugin)
- vault + index present
- `weave doctor --mcp` → overall PASS
- live MCP JSON-RPC `initialize` handshake
- all four lifecycle hooks + session-note materialization
- onboard outcome (seeded session notes, ontology concepts, landing docs)
- retrieval — FTS list-mode, context composition, graph walk, timeline
- the RLVR context-served substrate
- scheduler render (+ namespaced `/thinkweave:dream` on the symlink route)
- with `--heavy`: a real `/dream` cycle (digest + `maintenance.jsonl` line)

Both routes green ⇒ the full install surface is validated end-to-end.
