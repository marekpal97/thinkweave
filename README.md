<p align="center">
  <img src="https://raw.githubusercontent.com/marekpal97/thinkweave/main/assets/logo.jpg" alt="Thinkweave logo" width="360">
</p>

# Thinkweave

**A self-maintaining knowledge layer for Claude Code — it actively gathers what
you read, captures what you do, and curates both into living markdown you own.**

Most "agent memory" is a passive store: it remembers what you told it and hands
it back. Thinkweave takes the opposite posture. It *goes and acquires* knowledge
on the topics you steer it toward, *synthesizes* your sessions and your sources
into structured pages, *maintains its own vocabulary* every night, and *tracks
which of its memories actually got used* — all in plain Obsidian-native markdown.
SQLite is a throwaway index, rebuildable from the vault at any time.

> Markdown is the source of truth · the vault acquires and curates itself ·
> MCP tools + hooks are the interface · nothing is trapped in a store you can't read.

It ships as a Claude Code plugin: `weave_*` MCP tools, slash-command skills,
subagent workers, and four hooks that auto-capture session events.

---

## Why Thinkweave

Retrieval over a pile of notes is table stakes — every memory tool does it, and
plenty do it well. Thinkweave's bet is on the three things *around* retrieval
that almost none of them do.

### 1. It actively acquires knowledge — steered by you, informed by your work

Thinkweave doesn't wait to be told what to remember. The **discover → drain
spine** runs a configurable strategy list that finds gaps and enqueues external
sources — papers, repos, articles, news, podcasts, newsletters, YouTube — then
fans out subagent workers to write each one up. Two forces aim it:

- **Your priorities.** Focus areas and intake rails (RSS feeds, mail labels,
  channel allowlists) live in `PRIORITIES.yaml` / `sources.yaml` — data you edit,
  not code you fork. You decide what flows in, and adding a source type is a
  registry entry, not a subclass.
- **What you're actually doing.** The exploratory questions you ask get
  classified and tallied into *probe pressure* per concept; the nightly priority
  worker reads that pressure, checks whether the vault already covers the angle
  you keep probing, and enqueues fresh acquisition where it doesn't.

The result is a knowledge base that grows *toward* your work — without you
hand-maintaining a reading list.

### 2. It maintains itself — and hands you a report

Every night the **`/dream` cycle** runs ten subagent workers in two phases. It
mints concept-hub pages and theme arcs from the day's material, **dedups and
coarsens the ontology for you** (cosine drift-v2 + remembered verdicts), promotes
proven proposed-concepts to canonical, reconciles Claude Code's own auto-memory
against the vault, and judges yesterday's predictions against what actually
happened. You wake up to fresh knowledge digests (`vault/digests/`) *and* a
maintenance log (`vault/.weave/maintenance.jsonl`) of exactly what changed — the
vocabulary curates itself instead of rotting, and you can audit every move.

### 3. It learns which memories were worth keeping

Thinkweave snapshots **what actually got retrieved**: every retrieval MCP call is
logged as a context-served event. Decisions are evidence-gated — `superseded`
only lands when git-blame proves the predecessor's lines were really replaced,
not merely *declared* dead — and a decision can carry a `predicted_outcome:` that
gets judged later against reality, with `weave rlvr export` shipping the substrate
for reward modeling. It's memory with a feedback loop, not a write-only log.

### …and the foundation under all three

- **Internal + external, one ontology you control.** Sessions and decisions (what
  you do) and sources and concepts (what you read) are the *same primitives* under
  one shared vocabulary — so a finding from a paper surfaces against an unrelated
  project when they cite the same concept. Retrieval (FTS5, embedding similarity,
  typed graph walk, plus budgeted compositions) is the *access mechanism* here, not
  the headline.
- **Yours, in the open.** Human-readable markdown in an Obsidian-native vault —
  git-friendly, browsable, hand-editable. The SQLite index is throwaway: delete it
  and `weave index --full` rebuilds it from the markdown.
- **Steerable, not opinionated.** Ontology, themes, source types, and priorities
  are data-shaped extension surfaces (YAML registries, markdown frontmatter) — no
  plugin classes to subclass, no baked-in schema; the shipped ontology is a minimal
  seed that grows with your vault.
- **Lightweight and local.** No always-on server eating RAM. SQLite index, optional
  embeddings (retrieval degrades gracefully to keyword search without an API key),
  MCP server launches on demand.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit, and
[`CLAUDE.md`](CLAUDE.md) for the in-session agent contract.

---

## How it works

**acquire + capture → synthesize → maintain → serve.** `/discover` and `/drain`
pull external sources in along the rails you configured; hooks log every session
event as you work. `/wrap` distills sessions into notes + decisions, and the
nightly `/dream` mints concept hubs and theme arcs, dedups the ontology, judges
predictions, and writes a digest + maintenance report. You then retrieve through
FTS, semantic similarity, or a typed graph walk — and every retrieval feeds back
into what the vault learns is worth keeping.

- **The vault** — markdown notes in an Obsidian-native layout
  (`concepts/`, `sources/`, `themes/`, per-project session folders). Source of
  truth.
- **The index** — `<vault>/.weave/index.db` + `embeddings.db`, rebuilt from
  markdown by `weave index`. Powers retrieval; never authoritative.
- **MCP tools** — 17 `weave_*` tools are the agent's operation surface
  (search, create, read, extract, graph walk, …).
- **Hooks** — four Claude Code hooks (SessionStart / UserPromptSubmit /
  PostToolUse / Stop) auto-capture events into a session note; nothing is
  manual.
- **Skills** — slash-commands (`/onboard`, `/research`, `/drain`, `/wrap`,
  `/dream`, …) drive the knowledge layer; the heavy nightly work fans out to
  subagent workers.

Deeper references live in the [`docs/`](docs/) directory (lifecycles, skills,
CLI/MCP contract).

---

## Install

Two paths, both Claude-Code-native: the **plugin** (recommended — marketplace
install, fully namespaced) or a **clone + `weave dev-link`** for development /
private forks (also flagless and namespaced, with live edits). Both are the
*same plugin* — you either install a published copy or symlink your checkout in.

> **Supported surface.** Claude Code is currently the only proven, recommended
> way to use Thinkweave. The MCP server is a standard MCP stdio server, so other
> agents (Codex, Cursor, Zed, Claude Desktop, …) *can in principle* connect — but
> those paths are **unproven and unsupported at this stage**. See
> [Other agents (experimental)](#other-agents-experimental) below.

**Prerequisites either way:**

- **`uv` is required.** All MCP invocations route through `uv run`; install it:
  - Unix (bash/zsh): `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
- **The `weave` CLI must be on your PATH.** The plugin wires the MCP server and
  the hooks (those run via `uv run --project`), but `/thinkweave:onboard` and the
  scheduled jobs shell out to the `weave` console script directly — so install it
  once:
  - `uv tool install "thinkweave[all]"` — *once published to PyPI*
  - pre-PyPI / interim: `uv tool install "thinkweave[all] @ git+https://github.com/marekpal97/thinkweave"`

  `[all]` pulls every optional feature except the heavy local-embeddings extra;
  the default OpenAI embedding path is included. On a clone/dev checkout you can
  run the CLI as `uv run weave …` from the repo (or `uv tool install --editable .`
  for a fork) so `/onboard` and crons can find it.
- **Windows note.** Fully supported on native Windows. `weave schedule`
  auto-selects the host scheduler — `crontab` on Linux/macOS, Windows Task
  Scheduler (`schtasks`, tasks created under `Thinkweave\*`) on Windows — so the
  nightly `/dream` and embeddings keep-warm jobs run on all three platforms with
  no WSL required. The scheduled `claude -p` jobs run headless on your stored
  Claude Code login (no API key) with unattended tool-use, from the vault
  working directory.

### Plugin install (recommended)

Two steps, once per machine — add the marketplace, then install the plugin.
The install collapses MCP registration, hook installation, and slash-command
discovery into a single operation:

```bash
claude plugin marketplace add marekpal97/thinkweave   # one-time: register the marketplace
claude plugin install thinkweave@thinkweave           # registers MCP server, hooks, commands
# → restart Claude Code so the plugin's MCP server is picked up
```

`thinkweave@thinkweave` is `plugin@marketplace` — this repo ships its own
`.claude-plugin/marketplace.json`, so it *is* the marketplace (no separate
listing to find). The plugin manifest declares the MCP server inline (no
separate `weave install` MCP-registration step — distinct from the `weave` CLI
prerequisite above) and ships the subagent workers that `/dream` and `/drain`
fan out to.

**Namespacing.** Claude Code registers plugin commands under the plugin's
namespace: type `/thinkweave:onboard`, not `/onboard` (tab-complete after
`/thinkweave:` lists everything). `weave schedule` renders namespaced cron lines
automatically when it detects the plugin route.

### Clone / dev install

For developing against the source or running a private fork. `weave dev-link`
symlinks the checkout into `~/.claude/skills/`, where Claude Code auto-loads it
every session as the `thinkweave@skills-dir` plugin — **flagless** (no
`--plugin-dir` each launch), namespaced `/thinkweave:*`, and edits to the
working tree are live:

```bash
git clone https://github.com/marekpal97/thinkweave.git
cd thinkweave
uv sync --extra mcp        # installs weave, weave-hook, weave-mcp into the repo venv
uv run weave dev-link      # symlink into ~/.claude/skills/ (flagless auto-load)
# → restart Claude Code; commands are now /thinkweave:onboard, :tighten, …
```

Same plugin manifest as the marketplace route — MCP server, hooks, and subagent
workers, no separate `weave install`. Live-edit reload: `commands/*.md` and vault
config are picked up immediately; `hooks/`, `agents/`, or `mcpServers` changes need
`/reload-plugins`. `weave dev-link` is idempotent, refuses to shadow a marketplace
install, warns if a leftover `weave install` entry would double-register the server,
and is reversed by `weave dev-unlink`.

<details><summary>Alternative: <code>weave install</code> (MCP-only, machine-scope)</summary>

If you want only the MCP server registered in `~/.claude.json` *without* the
plugin — e.g. to wire the same server into another host — `weave install`
writes just the `mcpServers.thinkweave` block (idempotent, diff-on-overwrite,
`--vault PATH` to bake in the vault). This path installs **no** slash commands
or hooks (those come from the plugin); add `weave hooks install --scope
user|project` separately if you need the lifecycle hooks.

**No clone needed for the CLI.** The package is published on PyPI, so
`uv tool install thinkweave` (or `pipx install thinkweave`) puts the `weave`
CLI and `weave-mcp` server on your PATH without a checkout — handy for running
`weave install` on a host you don't develop on. Like the rest of this path it
ships **only** the engine: no slash commands, hooks, or subagent workers, so use
the plugin route above for the full Claude Code experience.

</details>

### Recommended companion plugin — Explanatory output style

Thinkweave pairs well with the **Explanatory** output style, which adds short
`★ Insight` notes about *why* the agent made a choice — onboarding and `/dream`
narration read best with it on. Once a built-in, it's now Anthropic's official
plugin from the `claude-plugins-official` marketplace (no `marketplace add` needed):

```text
/plugin install explanatory-output-style@claude-plugins-official
```

It's a SessionStart hook that conflicts with nothing here. Optional and a matter of
taste — we recommend rather than bundle it; skip it if you prefer a terser agent.

### Other agents (experimental)

Thinkweave's MCP server (`weave-mcp`) is a standard MCP stdio server, so any
MCP-capable host *can* connect — but **only Claude Code is proven and supported
today** (Codex, Cursor, Zed, Claude Desktop, etc. are untested). The repo-root
`.mcp.json` is the canonical launch reference to copy into another agent's config:

```jsonc
//   command: <path-to-clone>/bin/weave-mcp-launch
//   args:    []
//   env:     { "THINKWEAVE_VAULT": "<your-vault>" }   // or rely on config.toml
```

The launcher resolves `uv` (PATH, then `~/.local/bin/uv`, then
`$UV_INSTALL_DIR/uv`) and execs `uv run --project <path-to-clone> --extra mcp
weave-mcp`; if uv is missing it fails with a one-line error instead of
silently not launching. Hosts that guarantee `uv` on PATH can inline that
`uv run …` invocation directly.

Slash commands, hooks, and the SessionStart context payload are Claude-Code-only.
Other agents get the MCP tools (search / create / graph / …) and the `weave`
CLI — not the `/thinkweave:*` skills.

### Vault path

On first run, `/onboard` asks for your vault path and persists it to your
platform config dir (`~/.config/thinkweave/config.toml` on Linux/macOS,
`%APPDATA%\thinkweave\config.toml` on Windows). No shell-rc edits needed — both
the CLI and MCP server read this file. Inspect or change it anytime with the
`weave config` command:

```bash
weave config show               # resolved config path, vault_root, init status
weave config set-vault PATH     # persist a different vault_root
```

`THINKWEAVE_VAULT` still wins as an env override for per-shell experimentation.

Initialize a vault once:

```bash
THINKWEAVE_VAULT=~/vault weave init   # creates .weave/, the index, ontology seed, config defaults
```

Optional environment:

- `OPENAI_API_KEY` — embeddings (`weave index --embed`) and concept-hub bulk
  backfill. Without it, retrieval still works (FTS-only); similarity degrades
  gracefully.
- A provider key for the `--via batch` backfill routes (session-synthesis seed,
  ChatGPT import, hub linkage). The async fan-out runs against whichever provider
  `vault/config/api.yaml` selects (`openai` / `anthropic` / `gemini`) — set that
  key. The `inline` route needs none (it uses the running model).

---

## Getting started — `/onboard`

After install, run the first-run flow from any repo:

```bash
cd <your-repo>
claude
> /onboard      # (or /thinkweave:onboard on the plugin path)
```

`/onboard` is the spine of new-user UX — it makes your *existing* work legible
to Thinkweave from the first query:

1. **Import prior Claude Code history (always first, no skip).** Auto-discovers
   every project under `~/.claude/projects/` and seeds the vault from your past
   conversations. This is what makes the vault useful immediately.
2. **Bootstrap the ontology** from high-frequency `proposed_concepts:` surfaced
   by the import.
3. **Configure focus + source types** — which projects are active, which source
   types you want enabled (`PRIORITIES.yaml`).
4. **Per-project hooks + first landing docs** (`DECISIONS` / `BACKLOG` / `STATE`
   / `THEMES`) for each active project.

Idempotent — re-running only does what's still missing.

---

## Daily loop

Day to day you touch only a handful of things:

- **Session context auto-loads.** The SessionStart hook injects recent sessions,
  decisions, and project state into your context — read it first, no command
  needed.
- **`/wrap` before `/clear`.** There's no clear hook; this is how you
  preserve mid-session knowledge. It composes insights + decisions inline, calls
  `weave_extract` once, then runs a deterministic tail (prune → index → judge →
  landing → drift). Zero API cost beyond the composition itself.
- **Retrieve with `weave search` / `weave context`** (or the `weave_*` MCP tools
  in-session) — keyword, semantic, or graph walk.
- **Ingest with `/research <url>` or `/drain`.** `/research` classifies one URL
  (paper / repo / article / news) and writes a source note. `/drain` processes a
  queued batch.
- **Nightly `/dream` (cron).** One headless orchestrator does all the synthesis:
  mints concept hubs and theme arcs, dedups near-duplicates, judges decision
  predictions, catches up any unwrapped sessions, and writes a daily knowledge
  digest. Self-deciding, never prompts.

```bash
# Canonical nightly cron — headless, no prompts
claude -p "/dream"
```

---

## Sources

External content lands as `type: source` notes. Source types ship in two
*temporal grains*:

- **Event-grain** (`news`, `substack`, `newsletter-events`, `youtube-events`,
  `podcast-events`) — these float **themes** (narrative arcs) in `/dream`.
- **Concept-grain** (`paper`, `repo`, `article`, `newsletter-concepts`,
  `youtube-concepts`, `podcast-concepts`) — these feed **concept hubs**.

Every source type rides the same `discover → drain` spine, and you can add your
own without forking via `/source-scaffold` (registry overlay + a generated
skill file).

### Feed configuration

Feed registries live under **`PRIORITIES.yaml::intake.<slug>`** in your vault
config:

```yaml
intake:
  news:               {outlets: [...], drain_window_days: 7}
  podcast_events:     {outlets: [...]}
  youtube_events:     {channels: [UCxxxx, ...], lookback_days: 7}
  newsletter_events:  {senders: [...], mail_query: "...", label_overrides: {}}
```

- **News / podcast** use `intake.<slug>.outlets` (per-outlet caps).
- **YouTube** uses `intake.<slug>.channels` (one feed per channel).
- **Newsletters** use `intake.<slug>.senders` + a Gmail label workflow.

The `rss_poll` and `mail_poll` discover strategies read these registries and
enqueue items for `/drain`. (Headless cron:
`claude -p "/discover --strategy rss_poll --source-type news"`.)

### Optional extras

- **News / RSS / YouTube intake:** `uv pip install -e .[news]`
  (feedparser + readability-lxml + httpx).
- **Podcast audio:** transcription runs through Gemini's Files API; supply the
  configured provider key.

### Embeddings keep-warm

Similarity and hybrid retrieval read from `<vault>/.weave/embeddings.db`. Fresh
notes have no cached embedding until you re-run `weave index --embed`, so add a
cron line that re-embeds only the delta:

```cron
15 */4 * * * cd /path/to/vault && OPENAI_API_KEY="${OPENAI_API_KEY}" uv run weave index --embed --only-new >> ~/.cache/thinkweave/embed-warm.log 2>&1
```

`--only-new` filters to notes whose `updated_at` exceeds the most recent cached
embedding — cheap enough to run every few hours even on a 10k-note vault.
`weave doctor` warns when the DB is stale (> 7 days) and a key is set, so a
stalled cron surfaces in the standard health check.

---

## Architecture & docs

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — layer boundaries, the source primitive,
  capability lanes, the dream orchestrator, coherence mechanics. Start here if
  you're reading code or adding a source type.
- [`CLAUDE.md`](CLAUDE.md) — the in-session agent runtime: retrieval contract,
  note lifecycles, operational rules.
- [`docs/`](docs/) — deeper references (lifecycles, skills catalog, the
  CLI ↔ MCP surface contract).

At a glance: two layers with a one-way dependency (a Claude Code skills+hooks
layer over the `src/thinkweave/` knowledge layer); a three-modality retrieval
contract (FTS / similarity / graph); a shared hub spine for concepts and themes
(`## Essence` + append-only `## Catalyst log`); and an open-world source
registry where experimentation is cheap but production paths require a
`SourceTypeSpec` entry.

---

## License

[MIT](LICENSE) © 2026 marekpal97 and thinkweave contributors.
