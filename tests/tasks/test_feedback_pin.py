"""``weave task feedback``: verdicts pinned to the live session buffer
mid-session land on the round at the next wrap."""

from __future__ import annotations

import json

from thinkweave.core.buffer import append_event, buffer_path
from thinkweave.core.config import Config
from thinkweave.operations import wrap

TS = "2026-10-28T10:00:00+00:00"


def _rows(cfg: Config, session: str) -> list[dict]:
    path = buffer_path(cfg.weave_dir, session)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


class TestFeedbackEntryPoint:
    def test_a_verdict_pins_to_the_live_buffer(self, cfg: Config):
        live = buffer_path(cfg.weave_dir, "s-1")
        prompt = {"ts": TS, "type": "prompt", "text": "no, use a dict here", "session_id": "s-1"}
        append_event(live, prompt, harness="claude-code")

        result = wrap.pin_verdicts(
            cfg, "s-1", [{"prompt": "no, use a dict", "register": "correction", "about": "the shape"}]
        )
        assert result.verdicts_written == 1 and result.errors == []
        (feedback,) = [r for r in _rows(cfg, "s-1") if r.get("type") == "feedback"]
        assert feedback["register"] == "correction"
        assert feedback["ts"] == TS and feedback["about"] == "the shape"
        assert feedback["prompt_ref"] == "no, use a dict here"

    def test_an_unmatched_verdict_is_announced(self, cfg: Config):
        result = wrap.pin_verdicts(cfg, "s-2", [{"prompt": "never said", "register": "correction"}])
        assert result.verdicts_unmatched == 1
        assert _rows(cfg, "s-2") == []
