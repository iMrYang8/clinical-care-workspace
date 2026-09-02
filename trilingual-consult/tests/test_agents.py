from __future__ import annotations

import json
from pathlib import Path

from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput, ConsultTurn

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "datasets" / "nightingale_switchcare" / "scripts" / "consult-01.json"


def test_consult_01_optional_fields_default_empty() -> None:
    payload = json.loads(GOLD.read_text(encoding="utf-8"))
    consult = ConsultInput.from_dict(payload)
    assert consult.consult_id == "consult-01"
    assert all(turn.asr_hypothesis is None for turn in consult.turns)
    assert all(turn.overlap_group_id is None for turn in consult.turns)
    assert all(turn.raw_text is None for turn in consult.turns)


def test_turn_round_trips_asr_and_overlap() -> None:
    turn = ConsultTurn.from_dict(
        {
            "speaker_id": "SPEAKER_02",
            "text": "Dia ada alahan kepada penicillin masa kecil.",
            "asr_hypothesis": "Dia ada alahan kepada penisilin masa kechil.",
            "overlap_group_id": "overlap-1",
            "raw_text": "Dia ada alahan kepada penicillin masa kecil.",
            "source_language": "ms",
        }
    )
    assert turn.asr_hypothesis == "Dia ada alahan kepada penisilin masa kechil."
    assert turn.overlap_group_id == "overlap-1"
    assert turn.raw_text == "Dia ada alahan kepada penicillin masa kecil."


def test_unlabeled_consult_01_text_infers_roles() -> None:
    payload = json.loads(GOLD.read_text(encoding="utf-8"))
    for turn in payload["turns"]:
        turn.pop("speaker_role", None)
    state = run_consult_pipeline(ConsultInput.from_dict(payload))
    assert state.speaker_roles == {
        "SPEAKER_00": "clinician",
        "SPEAKER_01": "patient",
        "SPEAKER_02": "family",
    }


def test_overlap_marks_spans_and_facts_for_review() -> None:
    text = "Dia ada alahan kepada penicillin masa kecil."
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="overlap-unit",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_02",
                    text=text,
                    source_language="ms",
                    speaker_role="family",
                    overlap_group_id="overlap-1",
                    tagged_text="Dia ada alahan kepada [[EN]] penicillin [[/EN]] masa kecil.",
                )
            ],
        )
    )
    assert "OVERLAP_REVIEW" in state.warning_codes
    assert state.language_spans
    assert all(span.review_required for span in state.language_spans)
    allergies = [fact for fact in state.proposed_facts if fact.fact_type == "allergy"]
    assert allergies
    assert all(fact.review_required for fact in allergies)


def test_inconsistent_roles_fail_closed_to_unknown() -> None:
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="role-inconsistent",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text="We'll continue metformin 500 mg twice daily.",
                    source_language="en",
                ),
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text="Dia ada alahan kepada penicillin masa kecil.",
                    source_language="ms",
                ),
            ],
        )
    )
    assert state.speaker_roles["SPEAKER_00"] == "unknown"
    assert "ROLE_INCONSISTENT" in state.warning_codes
    assert all(turn.speaker_role == "unknown" for turn in state.turns)


def test_dose_mismatch_conflicts_and_blocks_publish() -> None:
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="dose-mismatch",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text="We'll start metformin 5000 mg twice daily.",
                    source_language="en",
                    speaker_role="clinician",
                ),
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text="Sorry, metformin 500 mg twice daily.",
                    source_language="en",
                    speaker_role="clinician",
                ),
            ],
        )
    )
    doses = {
        fact.value
        for fact in state.proposed_facts
        if fact.fact_type == "dose" and fact.key == "metformin"
    }
    assert doses == {"5000mg", "500mg"}
    assert any(
        conflict.reason == "dose_value" and conflict.auto_resolved is False
        for conflict in state.proposed_conflicts
    )
    assert "UNRESOLVED_DOSE_CONFLICT" in state.warning_codes
    assert "DOSE_OUT_OF_RANGE" in state.warning_codes
    assert state.publish_blocked is True


def test_asr_hypothesis_canonicalises_penisilin_and_keeps_raw_text() -> None:
    gold = "Dia ada alahan kepada penicillin masa kecil."
    hypothesis = "Dia ada alahan kepada penisilin masa kechil."
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="asr-noise-unit",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_02",
                    text=gold,
                    asr_hypothesis=hypothesis,
                    tagged_text="Dia ada alahan kepada [[EN]] penisilin [[/EN]] masa kechil.",
                    source_language="ms",
                    speaker_role="family",
                )
            ],
        )
    )
    assert state.turns[0].raw_text == gold
    assert state.turns[0].text == hypothesis
    allergies = [
        fact
        for fact in state.proposed_facts
        if fact.fact_type == "allergy" and fact.key == "penicillin"
    ]
    assert allergies
    assert all(fact.review_required for fact in allergies)
    assert all(fact.quote in hypothesis for fact in allergies)
