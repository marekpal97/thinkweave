---
name: themes-resolve
tools:
  - Read
  - Edit
  - Bash
  - mem_search
  - mem_read
  - mem_update
  - mem_link
description: Periodic theme hygiene — find duplicates, dormant narratives, and rewrite stale essences. Mirrors /mem-resolve-concepts for the global theme set.
---

# /themes-resolve — Theme Hygiene

Periodic maintenance of the global theme set at `vault/themes/`. Themes accumulate as catalysts come in; over time you accrue near-duplicate themes, themes whose essence has drifted from their catalyst log, and themes that have resolved or gone dormant. This skill surfaces those and writes the user-approved fixes.

Designed to run in under 2 minutes. Same posture as `/mem-resolve-concepts`: advisory first, structural changes only on approval.

## Steps

### 1. Scan

```
mem_search(query="", type="theme", limit=100)
```

That gets you all themes. Then for each one (or batch):

- Read its `## Essence` and the most recent ~5 `## Catalyst log` entries.
- Note its `status`, `project`, `concepts`, and `relates_to` from frontmatter.

You can also surface the redundant-hub Jaccard pre-filter for theme essences if the inventory is large:

```bash
uv run python -c "from personal_mem.synthesis.concepts import find_redundant_hub_candidates; from personal_mem.core.config import load_config; print(find_redundant_hub_candidates(load_config(), min_jaccard=0.4))"
```

(That helper was built for concept hubs but works on any text under `vault/concepts/topics/*.md`. For themes, fall back to LLM judgment over the theme essences.)

### 2. Three judgments per theme

Apply LLM judgment (no thresholds) to each theme:

- **Duplicate**: another theme covers materially the same narrative arc. Same target asset, same time horizon, same mechanism. Output: `merge: <thm-A> + <thm-B>`.
- **Dormant**: catalyst log hasn't moved in months and the thesis no longer feels load-bearing. Output: `archive: <thm-X>` (status → `dormant`).
- **Resolved**: the narrative played out — either confirmed (decisions implemented and exited) or invalidated (decisions stopped out, thesis broken). Output: `resolve: <thm-X>` (status → `resolved`).
- **Stale essence**: the catalyst log has diverged from the essence — recent entries contradict or extend the working thesis. Output: `rewrite essence: <thm-X>`.

These are observational, not prescriptive — surface, don't autofix.

### 3. Compact action plan

```
## Theme Hygiene — Action Plan

Stats: T total themes, A active, D dormant, R resolved

### Merges (N pairs)
| From → Into | Reason |
|-------------|--------|
| `thm-aaaa1111` → `thm-bbbb2222` | same AI capex narrative; aaaa's catalysts are absorbed |

### Status changes (N)
| Theme | New status | Reason |
|-------|------------|--------|
| `thm-cccc3333` | dormant | no catalysts since 2026-01 |

### Essence rewrites (N)
| Theme | Reason |
|-------|--------|
| `thm-dddd4444` | last 4 catalysts contradict the original thesis |

Approve all and go, or list exceptions.
```

### 4. Execute on approval

**Merges**:
- Read both themes. Append catalyst entries from the from-theme into the into-theme's `## Catalyst log` (preserve dates + linkage).
- Set the from-theme's status to `merged-into:thm-XXXX` (sentinel form). Optionally add a body note.
- Update any decision frontmatter that has `implements: [thm-old]` to `implements: [thm-new]`.

```python
# Pattern: read both, edit both, refresh THEMES.md last.
mem_update(thm_old, frontmatter_updates={"status": "merged-into:thm-new"})
# append catalysts to thm-new via Edit on the file
```

**Status changes**:
```python
mem_update(thm_id, frontmatter_updates={"status": "dormant"})
```

**Essence rewrites** — Edit the theme file directly. Read the last ~10 catalyst log entries, rewrite the `## Essence` section to reflect current understanding, keep ≤500 words. Don't touch `## Catalyst log` or `## Open questions`.

### 5. Refresh THEMES.md

After all edits:

```bash
uv run mem index
uv run mem landing --doc themes
```

THEMES.md will pick up the new statuses, merge sentinels, and dropped duplicates. Per-theme temporal DAGs render automatically when catalyst-log linkage exists.

### 6. Report (3 lines)

```
Done. Merged N pairs. M dormant, K resolved. R essence rewrites.
THEMES.md refreshed. Active themes: before → after.
```
