"""The trajectory note — one primitive, two faces.

``mint`` writes the note, ``prime`` reads prior ones back; both share one
vocabulary (the frontmatter keys, the ``loop-run`` tag), so they are one
module with two implementation files. This file is the module's interface;
everything else in ``mint``/``prime`` is internal.
"""

from devloop.trajectory.mint import build_trajectory
from devloop.trajectory.prime import (
    LOOP_PRIME_TOOL,
    append_served_event,
    build_prime_payload,
    is_holdout,
)

__all__ = [
    "LOOP_PRIME_TOOL",
    "append_served_event",
    "build_prime_payload",
    "build_trajectory",
    "is_holdout",
]
