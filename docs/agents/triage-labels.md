# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Canonical role     | Label in our tracker | Meaning                                  |
| ------------------ | -------------------- | ---------------------------------------- |
| `needs-triage`     | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`       | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`  | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`  | `ready-for-human`    | Requires human implementation            |
| `wontfix`          | `wontfix`            | Will not be actioned                     |
| `arch-proposal`    | `arch-proposal`      | Draft architectural proposal from the slow loop (`/arch-proposal`, #61); human greenlights → `ready-for-agent`, or closes to reject |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

The slow self-improvement loop (`/arch-proposal`) files evidence-gated draft issues labeled `arch-proposal`. These are proposals, not work orders: a human accepts one by relabeling it `ready-for-agent` (it then enters the fast loop's frontier), or rejects it by closing the issue. The slow loop never opens PRs.

thinkweave had no pre-existing triage labels, so these are the plain canonical defaults — nothing to remap. Edit the right-hand column later if you adopt different vocabulary.
