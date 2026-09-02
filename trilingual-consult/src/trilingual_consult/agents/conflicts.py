"""In-memory polarity/scope conflicts. Nothing auto-resolves."""

from __future__ import annotations

from trilingual_consult.state import ConsultState, ProposedConflict, ProposedFact


def _allergy_facts(state: ConsultState) -> list[ProposedFact]:
    return [fact for fact in state.proposed_facts if fact.fact_type == "allergy"]


def _dose_facts(state: ConsultState) -> list[ProposedFact]:
    return [fact for fact in state.proposed_facts if fact.fact_type == "dose"]


def run_conflicts(state: ConsultState) -> ConsultState:
    allergies = _allergy_facts(state)
    seen: set[tuple[str, str, str]] = set()
    for left in allergies:
        for right in allergies:
            if left is right:
                continue
            if left.key == "*" or right.key == "*":
                continue
            if left.key != right.key:
                continue
            if left.polarity == right.polarity:
                continue
            pair = tuple(sorted((left.polarity, right.polarity)))
            fingerprint = (left.key, pair[0], pair[1])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            state.proposed_conflicts.append(
                ProposedConflict(
                    fact_type="allergy",
                    key=left.key,
                    left_polarity=left.polarity,
                    right_polarity=right.polarity,
                    left_speaker_role=left.speaker_role,
                    right_speaker_role=right.speaker_role,
                    left_source_language=left.source_language,
                    right_source_language=right.source_language,
                    severity="critical",
                    auto_resolved=False,
                    reason="polarity",
                )
            )
            state.add_warning("UNRESOLVED_ALLERGY_CONFLICT")
            state.trace(
                f"conflict:{left.speaker_role}/{left.polarity}"
                f" vs {right.speaker_role}/{right.polarity} on {left.key}"
            )
    seen_dose: set[tuple[str, str, str]] = set()
    doses = _dose_facts(state)
    for left in doses:
        for right in doses:
            if left is right:
                continue
            if left.key != right.key:
                continue
            if left.value == right.value:
                continue
            pair = tuple(sorted((left.value, right.value)))
            fingerprint = (left.key, pair[0], pair[1])
            if fingerprint in seen_dose:
                continue
            seen_dose.add(fingerprint)
            state.proposed_conflicts.append(
                ProposedConflict(
                    fact_type="dose",
                    key=left.key,
                    left_polarity=left.value,
                    right_polarity=right.value,
                    left_speaker_role=left.speaker_role,
                    right_speaker_role=right.speaker_role,
                    left_source_language=left.source_language,
                    right_source_language=right.source_language,
                    severity="high",
                    auto_resolved=False,
                    reason="dose_value",
                )
            )
            state.add_warning("UNRESOLVED_DOSE_CONFLICT")
            state.trace(
                f"conflict:dose/{left.speaker_role}/{left.value}"
                f" vs {right.speaker_role}/{right.value} on {left.key}"
            )
    return state
