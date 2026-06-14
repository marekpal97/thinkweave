---
name: research-repo
source_type: repo
capabilities: [import]
tools:
  - Read
  - Write
  - WebFetch
  - Bash
  - weave_search
  - weave_concepts
  - weave_create
  - weave_update
  - weave_link
  - weave_queue
description: Fetch a GitHub / GitLab repo, extract architectural summary + key files, write it as a `source_type: repo` note. Called from `/research` (router) or `/drain --source-type repo`.
---

# /research-repo — Ingest a repo

Single-URL pipeline. The router classified the URL as a repo.

## Steps

### 1. Size check + metadata

```
Bash("gh repo view <owner/repo> --json diskUsage,name,description,primaryLanguage,stargazerCount,languages,defaultBranchRef")
```

- `diskUsage > 50000` (50MB, KB unit): skip — too big.
- `diskUsage > 10000` (10MB): README-only mode (skip the clone in step 3).
- Else: full clone for deeper analysis.

For awesome-list / curated-index repos (description matches "awesome",
"list of", "curated"): metadata + taxonomy summary only — do NOT clone
deeply. Light ingestion is the right shape.

### 2. Fetch README

```
WebFetch("https://raw.githubusercontent.com/<owner>/<repo>/<branch>/README.md")
```

### 3. Clone (if under 10MB and not awesome-list)

```
Bash("git clone --depth 1 <url> /tmp/research_clone_<slug>")
```

`Read` the entry point, main module, architecture docs, `pyproject.toml` /
`setup.py`. Concatenate the meaningful files into `snapshot.md`.

```
Bash("rm -rf /tmp/research_clone_<slug>")
```

### 4. Load ontology + check vault

```
Read src/thinkweave/ontology.yaml
weave_concepts(min_count=2)
weave_search(query="<repo description>", mode="hybrid", limit=5)
```

### 5. Write the source note

```
weave_create(
  type="source",
  title="<repo name — short tagline>",
  body="<architectural brief — structured per vault/config/note_formats/repo.md>",
  tags=["repo"],
  concepts=["<≥3 ontology concepts>"],
  frontmatter={
    "source_type": "repo",
    "url": "<canonical GitHub URL>",
    "authors": ["<owner / org>"],
    "repo_url": "<URL>",
    "languages": [<list>],
    "stars": <int>,
    "proposed_concepts": ["<new concepts>"]
  }
)
```

Save `snapshot.md` to the source directory:
```
Write <source_dir>/snapshot.md
weave_update(note_id="<src-id>", frontmatter_updates={"raw_path": "snapshot.md"})
```

### 6. Link + archive queue

Link related vault sources via `relates_to`. If invoked from `/drain`,
archive the queue item with status `done`.

### 7. Report

`src-id`, title, concepts, proposed concepts, related vault notes.

---

## Body template (repo)

`Read` `<vault_root>/config/note_formats/repo.md` and compose the body to
the sections it lists. That file is seeded at init and **user-editable** —
the user reshapes every repo brief by editing it directly, no skill change.
Keep `## Vault Connections` and `## Raw Content` so graph links and the
snapshot pointer land. If the file is missing, fall back to a clear,
well-structured architectural brief ending with `## Vault Connections` and
`## Raw Content`.

## Concept rules

Same as `/research-paper`: ≥3 concepts, ontology-first, propose new ones
under `proposed_concepts`. Always use `repo` (never the legacy `github`).
