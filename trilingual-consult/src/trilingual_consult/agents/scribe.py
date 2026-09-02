"""Scribe: keep original wording. Working text may be a simulated ASR hypothesis."""

from __future__ import annotations

from trilingual_consult.lexicon import strip_language_tags
from trilingual_consult.state import ConsultState


def run_scribe(state: ConsultState) -> ConsultState:
    used_hypothesis = False
    for turn in state.turns:
        if turn.tagged_text and not turn.text:
            turn.text = strip_language_tags(turn.tagged_text)
        if not turn.raw_text:
            turn.raw_text = turn.text
        if turn.asr_hypothesis:
            turn.text = turn.asr_hypothesis
            used_hypothesis = True
            state.trace(f"scribe:asr-hypothesis/{turn.speaker_id}")
    state.trace("scribe:asr-hypothesis" if used_hypothesis else "scribe:gold-fixture")
    return state
