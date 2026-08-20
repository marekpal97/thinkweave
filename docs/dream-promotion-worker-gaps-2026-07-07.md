# dream-promotion-worker: theme-shaped leakage fix, and a domain-minting gap

Found while reviewing the concept-promotion path with a user during a
personal_mem → thinkweave vault audit (2026-07-07). One item is fixed
in this same commit; the other is a design gap, written up so it can be
lifted directly into its own GitHub issue.

---

## 1. [Fixed here] Promotion worker had no rule against theme-shaped candidates

**Where:** `agents/dream-promotion-worker.md` (`## Decision rules`)

**What happened:** The worker's only two skip rules were "generic process
term" and "project-name leakage." There was no rule telling it to skip a
candidate that is actually a sector/story-arc term — a theme, not a
reusable concept. This distinction is already load-bearing elsewhere in
the system (concepts are flat reusable vocabulary; themes are narrative-arc
entities with an Essence + append-only Catalyst log), and it had already
bitten a real vault once: during onboarding, `luxury-turnaround`,
`optical-networking`, and `semiconductor-capex` cleared the promotion
filters and had to be pulled back by manual user pushback, because the
promotion worker's instructions never told it to recognize the pattern.
That lesson lived only in a user's private note, not in the worker prompt
— so nothing stopped the same misfire from happening again on the next
term that clears the count threshold (`ev-adoption` and similar single-use
sector terms are one recurrence away from being eligible).

**Fix applied:** Added an explicit third skip rule naming the failure
pattern and its resolution (defer to `/dream`'s theme-mint worker via
`proposed_concepts`, don't force a domain fit).

---

## 2. [Not implemented] No organic domain-minting path for single-concept promotion

**Where:** `agents/dream-promotion-worker.md` decision rules +
`operations/dream.py` apply-phase for `plan_fragment.promotions`.

**What happens:** The promotion worker can only assign a candidate to a
domain that already exists as a top-level key in `ontology.yaml` — its
instructions say "pick the best ontology domain... when in doubt, pick the
narrowest domain that still makes sense," with no option to say "none of
these fit, mint a new domain." This isn't just a prompt gap: the worker's
own documented failure mode confirms the apply-phase structurally rejects
it — "Assigning a domain that doesn't exist in `ontology.yaml` — apply
will silently no-op the promotion." So today, a concept with no good
existing-domain home either gets force-fit into an ill-matching bucket, or
silently dropped if the worker (correctly) names a domain that doesn't
exist yet.

**A narrower version of this already exists elsewhere:** `dream-merge-worker`'s
grain-coarsening path (N-ary cluster collapse) *can* mint a new domain —
`{"members": [...], "target": "eigen-decomposition", "target_domain":
"math-linalg", "target_is_new": true, "reason": "..."}` — and its apply
path is documented to write the new domain into the ontology. That proves
the mechanism is sound; it just isn't wired up for the single-concept
promotion path, which is the far more common route a new domain would
actually need to appear (grain-coarsening only fires on clusters of ≥2
near-duplicate concepts already sharing an embedding neighborhood — most
new domains would start from a single recurring concept, not a cluster).

**Suggested fix:**
1. Give `dream-promotion-worker` a `domain_is_new: true` output option,
   mirroring `dream-merge-worker`'s `target_is_new`, for use only when no
   existing domain is a reasonable fit — with instructions to prefer an
   existing domain whenever one is defensible, so this doesn't become a
   new-domain-per-candidate rubber stamp.
2. Extend the promotions apply-phase in `operations/dream.py` to accept
   `domain_is_new` and write the new top-level key into `ontology.yaml`
   (same code path `dream-merge-worker`'s coarsen-apply already uses to
   mint domains, if it's shared; otherwise mirror it) instead of
   silently no-oping on an unrecognized domain.
3. Cap new-domain minting per cycle (same spirit as `dream_promotion_cap`)
   so a bad cycle can't fragment the ontology with one-off domains.

Not implemented in this commit — this is a design + apply-phase change,
not a prompt tweak, and deserves its own review rather than riding in on
a one-line skip-rule fix.
