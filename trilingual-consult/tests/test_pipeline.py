from __future__ import annotations

import hashlib
import json
from pathlib import Path

from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput, ConsultTurn

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "datasets" / "nightingale_switchcare" / "scripts" / "consult-01.json"
EXPECTED = ROOT / "datasets" / "nightingale_switchcare" / "expected" / "consult-01.json"
SRC = ROOT / "src" / "trilingual_consult"


def _run_gold() -> tuple[object, dict]:
    payload = json.loads(GOLD.read_text(encoding="utf-8"))
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    return run_consult_pipeline(ConsultInput.from_dict(payload)), expected


def _fact_index(state) -> set[tuple]:
    return {
        (
            fact.fact_type,
            fact.key,
            fact.polarity if fact.fact_type == "allergy" else fact.value,
            fact.speaker_role,
            fact.review_required,
        )
        for fact in state.proposed_facts
    }


def test_gold_consult_roles_spans_facts_conflicts() -> None:
    state, expected = _run_gold()
    assert state.speaker_roles == expected["speaker_roles"]
    assert {turn.speaker_id for turn in state.turns} == set(expected["speaker_roles"])
    for turn in state.turns:
        covering = [
            span
            for span in state.language_spans
            if span.turn_index == state.turns.index(turn)
        ]
        assert covering, turn.text
        assert covering[0].start == 0
        assert covering[-1].end == len(turn.text)
        cursor = 0
        for span in covering:
            assert span.start == cursor
            cursor = span.end
        assert cursor == len(turn.text)

    family_turn = next(turn for turn in state.turns if turn.speaker_id == "SPEAKER_02")
    family_langs = {
        span.language
        for span in state.language_spans
        if span.turn_index == state.turns.index(family_turn)
    }
    assert "ms" in family_langs
    assert "en" in family_langs
    penicillin_span = next(
        span
        for span in state.language_spans
        if span.turn_index == state.turns.index(family_turn)
        and family_turn.text[span.start : span.end].strip().lower() == "penicillin"
    )
    assert penicillin_span.language == "en"

    index = _fact_index(state)
    assert ("allergy", "penicillin", "absent", "patient", False) in index
    assert ("allergy", "penicillin", "present", "family", True) in index
    assert ("medication", "metformin", "metformin", "clinician", False) in index
    assert ("dose", "metformin", "500mg", "clinician", False) in index
    assert ("frequency", "metformin", "twice_daily", "clinician", False) in index

    assert state.proposed_conflicts
    conflict = state.proposed_conflicts[0]
    assert conflict.key == "penicillin"
    assert conflict.severity == "critical"
    assert conflict.auto_resolved is False
    assert {conflict.left_speaker_role, conflict.right_speaker_role} == {
        "patient",
        "family",
    }
    assert state.publish_blocked is True
    for code in expected["warning_codes_required"]:
        assert code in state.warning_codes
    patient_summary = state.summary_proposals["patient_zh"]
    for needle in expected["patient_summary_must_contain"]:
        assert needle in patient_summary
    for fact in state.proposed_facts:
        turn = state.turns[fact.turn_index]
        assert turn.text[fact.start : fact.end] == fact.quote


def test_mistagged_malay_allergy_in_english_span_still_reviews() -> None:
    text = "pesakit alahan kepada amoksisilin"
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="mistag-en",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text=text,
                    source_language="en",
                    language_confidence=0.96,
                    speaker_role="family",
                )
            ],
        )
    )
    allergies = [fact for fact in state.proposed_facts if fact.fact_type == "allergy"]
    assert allergies
    assert any(
        fact.key in {"amoxicillin", "penicillin"} and fact.review_required
        for fact in allergies
    )
    assert any(fact.penicillin_class or fact.key == "amoxicillin" for fact in allergies)
    assert all(fact.quote in text for fact in allergies)
    assert state.turns[0].text == text


def test_english_island_in_malay_matrix_canonicalises_metformin() -> None:
    text = "Dia mula metformin 500 mg dua kali sehari."
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="island-ms",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_02",
                    text=text,
                    tagged_text="Dia mula [[EN]] metformin 500 mg [[/EN]] dua kali sehari.",
                    source_language="ms",
                    speaker_role="family",
                )
            ],
        )
    )
    meds = {
        (fact.fact_type, fact.key, fact.value)
        for fact in state.proposed_facts
        if fact.fact_type in {"medication", "dose", "frequency"}
    }
    assert ("medication", "metformin", "metformin") in meds
    assert ("dose", "metformin", "500mg") in meds
    langs = {span.language for span in state.language_spans}
    assert "ms" in langs
    assert "en" in langs


def test_han_penicillin_allergy_and_english_island_in_chinese() -> None:
    han = run_consult_pipeline(
        ConsultInput(
            consult_id="han",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_01",
                    text="我对盘尼西林过敏。",
                    source_language="zh",
                    speaker_role="patient",
                )
            ],
        )
    )
    mixed = run_consult_pipeline(
        ConsultInput(
            consult_id="zh-en-island",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_01",
                    text="我对 penicillin 过敏。",
                    tagged_text="我对 [[EN]] penicillin [[/EN]] 过敏。",
                    source_language="zh",
                    speaker_role="patient",
                )
            ],
        )
    )
    assert any(
        fact.key == "penicillin" and fact.polarity == "present"
        for fact in han.proposed_facts
        if fact.fact_type == "allergy"
    )
    assert any(
        fact.key == "penicillin" and fact.polarity == "present"
        for fact in mixed.proposed_facts
        if fact.fact_type == "allergy"
    )


def test_consult_07_one_utterance_has_ms_en_nan_and_family_hearsay() -> None:
    payload = json.loads(
        (
            ROOT
            / "datasets"
            / "nightingale_switchcare"
            / "scripts"
            / "consult-07-intrasentential.json"
        ).read_text(encoding="utf-8")
    )
    state = run_consult_pipeline(ConsultInput.from_dict(payload))
    turn = state.turns[0]
    langs = {span.language for span in state.language_spans}
    assert langs >= {"ms", "en", "nan"}
    covering = [span for span in state.language_spans if span.turn_index == 0]
    assert covering[0].start == 0
    assert covering[-1].end == len(turn.text)
    penicillin = next(
        span
        for span in covering
        if turn.text[span.start : span.end].strip().lower() == "penicillin"
    )
    assert penicillin.language == "en"
    allergies = [
        fact
        for fact in state.proposed_facts
        if fact.fact_type == "allergy" and fact.key == "penicillin"
    ]
    assert allergies
    assert all(fact.polarity == "present" for fact in allergies)
    assert all(fact.speaker_role == "family" for fact in allergies)
    assert all(fact.review_required for fact in allergies)
    assert not any(
        fact.fact_type == "allergy" and fact.polarity == "absent"
        for fact in state.proposed_facts
    )
    assert "MIXED_LANGUAGE_TURN" in state.warning_codes
    assert state.publish_blocked is False
    assert "盘尼西林" in state.summary_proposals["patient_zh"]
    assert "家属" in state.summary_proposals["patient_zh"]


def test_empty_hokkien_is_unsupported_and_not_nkda() -> None:
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="nan-empty",
            enabled_languages=("en", "ms", "zh", "nan"),
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_01",
                    text="ok lah",
                    source_language="nan",
                    speaker_role="patient",
                )
            ],
        )
    )
    assert "HOKKIEN_ASR_UNSUPPORTED" in state.warning_codes
    assert not any(
        fact.fact_type == "allergy" and fact.polarity == "absent"
        for fact in state.proposed_facts
    )


def test_family_vs_patient_conflict_does_not_autoresolve() -> None:
    state, _expected = _run_gold()
    assert state.proposed_conflicts
    assert all(conflict.auto_resolved is False for conflict in state.proposed_conflicts)
    assert state.publish_blocked is True


def test_pipeline_is_deterministic() -> None:
    first, _ = _run_gold()
    second, _ = _run_gold()
    left = json.dumps(first.to_dict(), sort_keys=True, ensure_ascii=False)
    right = json.dumps(second.to_dict(), sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(left.encode()).hexdigest() == hashlib.sha256(
        right.encode()
    ).hexdigest()


def test_package_does_not_import_nightingale_or_app_services() -> None:
    forbidden = (
        "from app",
        "import app",
        "app.services",
        "fastapi",
        "sqlmodel",
        "sqlalchemy",
    )
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for token in forbidden:
            assert token not in lowered, f"{path} contains {token}"
        # lexicon.py may mention Nightingale paths as snapshot provenance.
        if path.name != "lexicon.py":
            assert "from nightingale" not in lowered
            assert "import nightingale" not in lowered
