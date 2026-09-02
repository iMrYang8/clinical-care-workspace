"""Term Anchor: English drug islands canonicalise via the sandbox formulary."""

from __future__ import annotations

from trilingual_consult.lexicon import extract_medication_matches
from trilingual_consult.state import ConsultState, ProposedFact


def run_term_anchor(state: ConsultState) -> ConsultState:
    for index, turn in enumerate(state.turns):
        role = state.speaker_roles.get(turn.speaker_id, "unknown")
        review = role in {"family", "unknown"} or bool(turn.overlap_group_id)
        for match in extract_medication_matches(turn.text):
            state.proposed_facts.append(
                ProposedFact(
                    fact_type=match.fact_type,
                    key=match.key,
                    value=match.value,
                    polarity="present",
                    assertion_scope="specific_substance",
                    start=match.start,
                    end=match.end,
                    quote=match.quote,
                    source_language=match.language,
                    speaker_id=turn.speaker_id,
                    speaker_role=role,
                    review_required=review,
                    turn_index=index,
                    penicillin_class=match.key in {"penicillin", "amoxicillin"},
                )
            )
            state.trace(f"term-anchor:{match.fact_type}/{match.key}={match.value}")
    return state
