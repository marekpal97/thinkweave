"""Landing-document operation — thin orchestrator over ``synthesis.landing``.

The MCP ``mem_landing`` tool and any future CLI parity call into here. The
heavy lifting (assembling DECISIONS / BACKLOG / STATE / THEMES from the
indexed vault) lives in :mod:`personal_mem.synthesis.landing`; this module
only owns argument validation and the ``state_context`` branch dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from personal_mem.core.config import Config
from personal_mem.synthesis.landing import (
    state_of_play_context,
    write_landing_docs,
)


@dataclass
class LandingResult:
    """Outcome of :func:`render_landing`. Surfaces format the human report."""

    project: str = ""
    doc: str = ""
    written: dict[str, Path] = field(default_factory=dict)
    state_context_text: str = ""
    error: str = ""


def render_landing(
    cfg: Config,
    *,
    project: str = "",
    doc: str = "all",
    state_context: bool = False,
) -> LandingResult:
    """Generate landing documents (or fetch the state-of-play context blob).

    - ``state_context=True`` requires ``project`` and short-circuits to the
      pre-LLM context payload.
    - ``doc='themes'`` is global; every other doc requires ``project``.
    """
    out = LandingResult(project=project, doc=doc)
    if state_context:
        if not project:
            out.error = "state_context=true requires a project argument."
            return out
        out.state_context_text = state_of_play_context(cfg, project)
        return out
    if doc != "themes" and not project:
        out.error = (
            "Project argument required for doc="
            f"{doc!r} (only doc='themes' is global)."
        )
        return out
    out.written = write_landing_docs(cfg, project, docs=doc)
    return out
