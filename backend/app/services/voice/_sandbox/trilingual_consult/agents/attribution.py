"""Attribution: language spans + consult speaker_role. Fail closed to unknown."""

from __future__ import annotations

import re
from dataclasses import replace

from trilingual_consult.lexicon import detect_language_spans, has_hokkien_cue, normalize_language
from trilingual_consult.state import ConsultState, SpeakerRole


def infer_speaker_role(text: str, source_language: str | None) -> SpeakerRole:
    lowered = text.casefold()
    if re.search(r"\b(dia|anak|isteri|suami)\b", lowered):
        return "family"
    if "我" in text and re.search(r"过敏|過敏|不舒服|痛", text):
        return "patient"
    if re.search(r"\b(we'll|we will|continue|twice daily|started)\b", lowered):
        return "clinician"
    language = normalize_language(source_language)
    if language == "ms" and re.search(r"\b(dia|pesakit)\b", lowered):
        return "family"
    if language == "zh" and "我" in text:
        return "patient"
    if language == "en" and re.search(r"\b(mg|dose|daily)\b", lowered):
        return "clinician"
    return "unknown"


def run_attribution(state: ConsultState) -> ConsultState:
    spans = []
    roles: dict[str, str] = {}
    for index, turn in enumerate(state.turns):
        if turn.speaker_role in {"clinician", "patient", "family", "unknown"}:
            role: SpeakerRole = turn.speaker_role
        else:
            role = infer_speaker_role(turn.text, turn.source_language)
        previous = roles.get(turn.speaker_id)
        if previous is not None and previous != role:
            role = "unknown"
            state.add_warning("ROLE_INCONSISTENT")
            state.trace(f"attribution:{turn.speaker_id}/role-inconsistent")
        roles[turn.speaker_id] = role
        turn.speaker_role = role
        turn_spans = detect_language_spans(
            turn.text,
            turn_index=index,
            source_language=turn.source_language,
            tagged_text=turn.tagged_text,
        )
        if turn.overlap_group_id:
            state.add_warning("OVERLAP_REVIEW")
            turn_spans = [replace(span, review_required=True) for span in turn_spans]
            state.trace(f"attribution:{turn.speaker_id}/overlap/{turn.overlap_group_id}")
        if len({span.language for span in turn_spans if span.language != "und"}) > 1:
            state.add_warning("MIXED_LANGUAGE_TURN")
            state.trace(f"attribution:{turn.speaker_id}/mixed")
        spans.extend(turn_spans)
        state.trace(f"attribution:{role}/{normalize_language(turn.source_language)}")
        if role in {"family", "unknown"}:
            state.add_warning("FAMILY_OR_UNKNOWN_SPEAKER")
        if (
            "nan" in state.enabled_languages
            and normalize_language(turn.source_language) == "nan"
        ):
            if not has_hokkien_cue(turn.text):
                state.add_warning("HOKKIEN_ASR_UNSUPPORTED")
                state.trace("attribution:hokkien-unsupported")
    for turn in state.turns:
        turn.speaker_role = roles.get(turn.speaker_id)  # type: ignore[assignment]
    state.speaker_roles = roles
    state.language_spans = spans
    return state
