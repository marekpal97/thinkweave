---
name: source-scaffold
owns_mechanic: source_scaffold
capabilities: []
consumes: [weave_sources_config]
produces: [vault/config/source_types.yaml, vault/config/sources.yaml, ~/.claude/commands/<slug>.md]
tools:
  - Read
  - Bash
  - weave_sources_config
description: Generative wizard — register a new source type via vault overlay + machine-global skill file. No repo edits.
---

# /source-scaffold — register a new source type

Vault-scope skill. Writes:

- **`<vault>/config/source_types.yaml`** — registry overlay (one new entry)
- **`~/.claude/commands/<slug>.md`** — skill file (machine-global, so
  `/<slug>` works in every Claude Code session)

The per-type **config block** in `<vault>/config/sources.yaml`
(intake_folder, dedup_keys, drain_strategy, optional `url_patterns`)
is **not** written by the CLI — you append it manually in Step 4
because its shape depends on the capabilities you chose.

Idempotent + safe: refuses to overwrite existing source types or
existing skill files. Run `/source-fit` first if you're not sure
whether a new source type is even needed.

**Argument**: `<slug>` — short, lowercase, single word. e.g.
`podcast`, `email`, `youtube`, `rss`.

---

## Step 1 — Confirm the slug doesn't already exist

```
weave_sources_config()
```

If `<slug>` is already a key under `sources:` (either in-code default
or vault overlay), stop. Tell the user and suggest `/source-fit
<their-description>` to find the existing match.

## Step 2 — Five questions

Ask the user **once**, in order. Don't fan out.

1. **Bucket name** — subfolder under `vault/sources/`. Default:
   `<slug>s` (pluralize). Validate: lowercase, alphanumeric + `-`/`_`.
2. **Layout** — one of:
   - `flat` — single file at `bucket/<slug>.md`. Use for thin records
     (e.g. ChatGPT exports).
   - `folder` — `bucket/<item-slug>/source.md` with raw companion
     content alongside. **Default.** Use for most types.
   - `author_folder` — `bucket/<author>/<item-slug>/source.md`. Use
     for serial content (newsletters, podcasts with show structure).
3. **Capabilities** — any subset of `[import, acquire, discover]`:
   - `import` — one-shot from URL/file/identifier
   - `acquire` — batch drain from a queue or disk inbox
   - `discover` — gap analysis to find what to add next
4. **Dedup keys** — comma-separated list. Default: `url, title`.
   What makes two items "the same"? (e.g. `arxiv_id, doi, url, title`
   for papers; `url, episode_id` for podcasts.)
5. **Intake folder** — disk path for the `acquire` capability, OR
   blank if not applicable. e.g. `~/podcast_inbox`. The skill file
   you'll edit reads from this path on `/drain`.

## Step 3 — Run the CLI

```bash
THINKWEAVE_VAULT=<vault> weave sources scaffold <slug> \
    --bucket <bucket> \
    --layout <layout> \
    --description "<one-line description>" \
    --skill-target user
```

`--skill-target user` (default) writes to `~/.claude/commands/<slug>.md`
so the skill is available in every project. Pass `--skill-target none`
if you only want the registry/config entries (e.g. you're scaffolding
several types and will write skill files separately).

## Step 4 — Add config block to vault sources.yaml

The CLI writes the registry entry but **not** the per-type config
(intake_folder, dedup_keys, etc.) — that goes into `sources.yaml`. Use
Edit (not Write) to append under `sources:`:

```yaml
sources:
  <slug>:
    drain_strategy: inline
    dedup_keys: [<comma-list>]
    intake_folder: <path-or-omit>
```

## Step 5 — Smoke-test

```bash
THINKWEAVE_VAULT=<vault> weave sources show <slug>
```

Should print the registry entry with `origin: user`. If it does, the
scaffolding succeeded.

## Step 6 — Hand off to the user

Print:

> Scaffolded `<slug>`. Next:
>
> 1. Open `~/.claude/commands/<slug>.md` and fill in the FETCH
>    STRATEGY section under each capability. Pattern-match
>    `commands/research.md` (URL-driven) or `commands/substack.md`
>    (disk-inbox-driven).
> 2. Test with one item.
> 3. Add to `weave_sources_config().sources.<slug>.url_patterns` if you
>    want `/research` to auto-classify URLs into this type.

## What this skill never does

- Never edits `src/thinkweave/acquisition/sources/registry.py` (that's the
  in-code default set; user additions go to the vault overlay).
- Never edits files in the thinkweave repo. All artifacts are
  vault-scope (`<vault>/.weave/`) or machine-scope (`~/.claude/commands/`).
- Never overwrites existing entries — refuses with a hint.
