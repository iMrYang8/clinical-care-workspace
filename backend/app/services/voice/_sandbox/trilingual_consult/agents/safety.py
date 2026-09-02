"""Safety Sentinel: allergy proposals only. Never writes 'no allergy' from silence."""

from __future__ import annotations

from trilingual_consult.lexicon import extract_allergy_matches, normalize_language
from trilingual_consult.state import ProposedAlert, ProposedFact, ConsultState


def run_safety(state: ConsultState) -> ConsultState:
    facts: list[ProposedFact] = []
    alerts: list[ProposedAlert] = []
    for index, turn in enumerate(state.turns):
        role = state.speaker_roles.get(turn.speaker_id, "unknown")
        hint = normalize_language(turn.source_language)
        for match in extract_allergy_matches(turn.text):
            review = (
                match.polarity == "unknown"
                or match.key == "*"
                or role in {"family", "unknown"}
                or (hint not in {"und", match.language})
                or bool(turn.overlap_group_id)
            )
            fact = ProposedFact(
                fact_type="allergy",
                key=match.key,
                value=match.polarity,
                polarity=match.polarity,
                assertion_scope="specific_substance"
                if match.key != "*"
                else "all_allergies",
                start=match.start,
                end=match.end,
                quote=match.quote,
                source_language=match.language,
                speaker_id=turn.speaker_id,
                speaker_role=role,
                review_required=review,
                turn_index=index,
                penicillin_class=match.penicillin_class,
            )
            facts.append(fact)
            alerts.append(
                ProposedAlert(
                    concept_code=(
                        f"allergy:{match.key}"
                        if not review
                        else "allergy:review_required"
                        if match.key == "*"
                        else f"allergy:{match.key}"
                    ),
                    polarity="unknown" if review and role in {"family", "unknown"} else match.polarity,
                    speaker_role=role,
                    source_language=match.language,
                    quote=match.quote,
                    turn_index=index,
                    review_required=review,
                )
            )
            state.trace(f"safety:{role}/{match.language}/{match.key}:{match.polarity}")
    state.proposed_facts.extend(facts)
    state.proposed_alerts.extend(alerts)
    if any(fact.penicillin_class for fact in facts):
        state.trace("safety:penicillin-class")
    return state
