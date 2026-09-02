"""Flag-gated consult-agent adapter. Proposals only; never publishes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal, cast

from app.services.clinical_formulary import allergy_category_for_assertion
from app.services.conflicts import NormalizedFact
from app.services.voice.providers.base import TranscriptSegmentResult

_SANDBOX_ROOT = Path(__file__).resolve().parent / "_sandbox"
if str(_SANDBOX_ROOT) not in sys.path:
    sys.path.insert(0, str(_SANDBOX_ROOT))

from trilingual_consult.pipeline import run_consult_pipeline  # noqa: E402
from trilingual_consult.state import (  # noqa: E402
    ConsultInput,
    ConsultState,
    ConsultTurn,
)

_ALLOWED_FACT_TYPES = frozenset({"allergy", "medication", "dose", "route", "frequency"})
_ASSERTION_SCOPES = frozenset({"specific_substance", "drug_allergies", "all_allergies"})


def run_consult_on_segments(
    segments: list[TranscriptSegmentResult], *, consult_id: str
) -> ConsultState:
    """Run the proposal-only consult pipeline on already-transcribed segments."""

    turns = [
        ConsultTurn(
            speaker_id=segment.speaker_id or f"SPEAKER_{index:02d}",
            text=segment.text,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            source_language=segment.source_language,
            language_confidence=segment.language_confidence,
            overlap_group_id=segment.overlap_group_id,
        )
        for index, segment in enumerate(segments)
    ]
    return run_consult_pipeline(ConsultInput(consult_id=consult_id, turns=turns))


def consult_warning_codes(state: ConsultState) -> list[str]:
    codes = list(state.warning_codes)
    if state.publish_blocked and "PUBLISH_BLOCKED" not in codes:
        codes.append("PUBLISH_BLOCKED")
    codes.append("MULTI_AGENT_CONSULT_PROPOSAL")
    return codes


def consult_summary(state: ConsultState) -> str:
    return (
        f"Multi-agent consult pipeline proposed {len(state.proposed_facts)} facts "
        f"and {len(state.proposed_conflicts)} unresolved conflicts. "
        "Agents propose only; clinician review is required before publication."
    )


def consult_agent_payload(state: ConsultState) -> dict[str, Any]:
    return {
        "enabled": True,
        "speaker_roles": dict(sorted(state.speaker_roles.items())),
        "conflicts": [
            {
                "fact_type": conflict.fact_type,
                "key": conflict.key,
                "reason": conflict.reason,
                "severity": conflict.severity,
                "auto_resolved": conflict.auto_resolved,
                "left_speaker_role": conflict.left_speaker_role,
                "right_speaker_role": conflict.right_speaker_role,
                "left_polarity": conflict.left_polarity,
                "right_polarity": conflict.right_polarity,
            }
            for conflict in state.proposed_conflicts
        ],
        "summaries": dict(sorted(state.summary_proposals.items())),
    }


def consult_fact_candidates(
    state: ConsultState,
    segments: list[TranscriptSegmentResult],
) -> tuple[list[tuple[NormalizedFact, int, int, TranscriptSegmentResult]], list[str]]:
    """Map agent proposals onto Nightingale fact candidates with global offsets."""

    extra_warnings: list[str] = []
    output: list[tuple[NormalizedFact, int, int, TranscriptSegmentResult]] = []
    for fact in state.proposed_facts:
        if fact.fact_type not in _ALLOWED_FACT_TYPES:
            continue
        if fact.turn_index < 0 or fact.turn_index >= len(segments):
            extra_warnings.append("AGENT_FACT_TURN_OUT_OF_RANGE")
            continue
        segment = segments[fact.turn_index]
        if segment.text_start is None or segment.text_end is None:
            extra_warnings.append("AGENT_FACT_SEGMENT_UNALIGNED")
            continue
        local = segment.text[fact.start : fact.end]
        if local != fact.quote:
            extra_warnings.append("AGENT_FACT_QUOTE_MISMATCH")
            continue
        start = segment.text_start + fact.start
        end = segment.text_start + fact.end
        if not (segment.text_start <= start <= end <= segment.text_end):
            extra_warnings.append("AGENT_FACT_QUOTE_MISMATCH")
            continue
        scope = (
            fact.assertion_scope
            if fact.assertion_scope in _ASSERTION_SCOPES
            else "specific_substance"
        )
        typed_scope = cast(
            Literal["specific_substance", "drug_allergies", "all_allergies"],
            scope,
        )
        polarity = cast(
            Literal["present", "absent", "unknown"],
            fact.polarity
            if fact.polarity in {"present", "absent", "unknown"}
            else "unknown",
        )
        value = fact.polarity if fact.fact_type == "allergy" else fact.value
        output.append(
            (
                NormalizedFact(
                    fact_type=fact.fact_type,
                    key=fact.key,
                    value=value,
                    start=fact.start,
                    end=fact.end,
                    quote=fact.quote,
                    assertion_scope=typed_scope,
                    polarity=polarity,
                    allergy_category=(
                        allergy_category_for_assertion(fact.key, typed_scope)
                        if fact.fact_type == "allergy"
                        else None
                    ),
                    source_language=fact.source_language,
                    review_required=fact.review_required,
                ),
                start,
                end,
                segment,
            )
        )
    return output, extra_warnings
