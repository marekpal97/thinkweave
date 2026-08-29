"""Render harness-profile data for humans — degradation notes and, for
``docs/HARNESSES.md``, the generated capability matrix.

The epic's anti-goal (#103 / dec-5a076384) is a capability silently faked or
a hand-maintained capability table that rots (the vercel/memorix/hol-guard
pattern the issue names). So the prose people read is *rendered from* the same
``HarnessProfile`` rows the installer runs on — degradations included.
"""

from __future__ import annotations

from thinkweave.core.harness import HarnessProfile


def render_degradations(profile: HarnessProfile) -> str:
    """The profile's degradations as a markdown bullet list, '' when none.

    Every row names the capability, whether it is documented-degraded or
    refused outright, the honest note, and the upstream evidence ref.
    """
    lines = []
    for d in profile.degradations:
        ref = f" ({d.upstream_ref})" if d.upstream_ref else ""
        lines.append(f"- **{d.capability}** — {d.mode}: {d.note}{ref}")
    return "\n".join(lines)
