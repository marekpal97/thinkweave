---
title: News Focus (deprecated — see THEMES.md)
type: landing
project: news
updated: 2026-05-10
status: deprecated
---

# News Focus — deprecated

The v1 news pipeline used this file as a concept-set admission gate. v2 retired it: news admission is now a Haiku title-triage call against the active-themes catalog rendered in `vault/THEMES.md`'s `## Catalog (active)` section.

If you want to broaden or narrow what news the pipeline admits, **edit themes**, not this file:

- Edit the `## Essence` section of an active theme — the triage helper passes essences as cached system context, so a tighter or broader essence directly reshapes admission
- Mark a theme `dormant` or `resolved` — it drops out of the catalog and stops admitting items

This file is kept (rather than deleted) only as a redirect. Future `/onboard` runs will skip seeding it.
