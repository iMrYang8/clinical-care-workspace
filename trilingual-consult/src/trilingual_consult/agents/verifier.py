"""Verifier: span quotes, dose range, hearsay, Hokkien fail-closed. Proposals only."""

from __future__ import annotations

from trilingual_consult.lexicon import DOSE_RANGE_MG
from trilingual_consult.state import ConsultState


def run_verifier(state: ConsultState) -> ConsultState:
    for fact in state.proposed_facts:
        turn = state.turns[fact.turn_index]
        actual = turn.text[fact.start : fact.end]
        if actual != fact.quote:
            state.add_warning("QUOTE_OFFSET_MISMATCH")
            state.trace(f"verifier:quote-mismatch/{fact.key}")
        if fact.fact_type == "dose":
            numeric = float("".join(ch for ch in fact.value if ch.isdigit() or ch == "."))
            bounds = DOSE_RANGE_MG.get(fact.key)
            if bounds and not bounds[0] <= numeric <= bounds[1]:
                state.add_warning("DOSE_OUT_OF_RANGE")
                state.trace(f"verifier:dose-out-of-range/{fact.key}")
        if fact.speaker_role in {"family", "unknown"} and fact.fact_type in {
            "allergy",
            "medication",
            "dose",
        }:
            if not fact.review_required:
                state.add_warning("HEARSAY_NOT_MARKED")
        if turn.overlap_group_id and not fact.review_required:
            state.add_warning("OVERLAP_FACT_NOT_MARKED")
    if state.proposed_conflicts:
        state.publish_blocked = True
        state.add_warning("PUBLISH_BLOCKED")
        state.trace("verifier:publish-blocked")
    if any(
        fact.fact_type == "allergy" and fact.polarity == "absent" and fact.key == "*"
        for fact in state.proposed_facts
    ):
        state.add_warning("BROAD_NKDA_FROM_WEAK_EVIDENCE")
    # Silence must never become NKDA.
    nan_turns = [
        turn
        for turn in state.turns
        if (turn.source_language or "").startswith("nan")
    ]
    if nan_turns and "HOKKIEN_ASR_UNSUPPORTED" in state.warning_codes:
        nkda = [
            fact
            for fact in state.proposed_facts
            if fact.fact_type == "allergy" and fact.polarity == "absent"
        ]
        if nkda:
            state.add_warning("HOKKIEN_FALSE_NKDA")
    state.trace("verifier:ok")
    return state
