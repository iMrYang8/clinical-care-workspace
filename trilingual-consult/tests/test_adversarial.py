"""Boundaries of the multilingual extractor, including the ones it fails.

The seven gold consults all score 1.00 because the lexicon and the gold were
written together. These cases are the opposite: inputs chosen to break it. Cases
that currently fail are marked xfail with the reason rather than deleted,
because a known limit that a reviewer can read is worth more than a green suite
that never went looking.

`strict=True` throughout, so fixing a limit turns the marker into a failure and
forces it to be removed.
"""

from __future__ import annotations

import pytest

from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput, ConsultTurn


def allergy_facts(
    text: str, language: str, *, role: str = "clinician"
) -> list[tuple[str, str]]:
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="adversarial",
            turns=[
                ConsultTurn(
                    speaker_id="S0",
                    text=text,
                    source_language=language,
                    speaker_role=role,
                )
            ],
        )
    )
    return [
        (fact.key, fact.polarity)
        for fact in state.proposed_facts
        if fact.fact_type == "allergy"
    ]


# --- negation, the direction where an error is dangerous ---------------------


def test_hokkien_negation_reads_as_absent() -> None:
    """`bo` is the negator. No gold consult covers it, so pin it here."""

    assert allergy_facts("bo tui penicillin koe-bin.", "nan") == [
        ("penicillin", "absent")
    ]


def test_malay_tiada_negation_reads_as_absent() -> None:
    assert allergy_facts("Dia tiada alahan kepada amoxicillin.", "ms") == [
        ("amoxicillin", "absent")
    ]


def test_a_dropped_negator_over_alerts_rather_than_under_alerts() -> None:
    """The safe direction, and worth pinning so it stays that way.

    Recognition drops short function words. Losing `bo` turns a denial into a
    report of an allergy: the clinician sees a warning that is not real, which
    costs an unnecessary substitution. Losing it the other way would hide a real
    allergy, which is the failure that harms someone.
    """

    assert allergy_facts("tui penicillin koe-bin.", "nan") == [
        ("penicillin", "present")
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Malay recognises only `tiada`. `tidak ada` and `tak ada` are the "
        "commoner spoken forms and fall through to the positive pattern, so a "
        "denial is read as a report of an allergy. Over-alerting, so not "
        "dangerous, but wrong."
    ),
)
def test_malay_tidak_ada_negation_is_not_yet_recognised() -> None:
    assert allergy_facts("Dia tidak ada alahan kepada penicillin.", "ms") == [
        ("penicillin", "absent")
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "English has no blanket-denial pattern, so `NKDA` yields nothing at all "
        "and the statement is lost rather than recorded as a denial needing "
        "confirmation."
    ),
)
def test_english_nkda_is_not_yet_recognised() -> None:
    assert allergy_facts("Patient is NKDA.", "en") == [("*", "absent")]


# --- drug names the extractor cannot reach -----------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The Chinese allergy patterns hardcode the four penicillin aliases in "
        "their capture group, while Malay, Hokkien and English use a generic "
        "one. A patient naming any other drug in Chinese degrades to an "
        "unknown-substance keyword hit, so their own language is the only one "
        "where the substance is lost."
    ),
)
def test_chinese_allergy_to_a_second_drug_is_not_yet_extracted() -> None:
    assert allergy_facts("我对阿莫西林过敏。", "zh") == [("amoxicillin", "present")]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The Chinese patterns match simplified `对` and `过敏` only, so "
        "traditional script degrades to an unknown-substance hit. Traditional "
        "is in everyday use across the region this is built for."
    ),
)
def test_traditional_chinese_is_not_yet_extracted() -> None:
    assert allergy_facts("我對盤尼西林過敏。", "zh") == [("penicillin", "present")]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The Malay, Hokkien and English capture groups are ASCII-anchored, so a "
        "Han-script drug name inside those matrices is unreachable. Chinese "
        "works only because it hardcodes the names, so the two designs fail in "
        "opposite directions."
    ),
)
def test_han_script_drug_in_a_malay_sentence_is_not_yet_extracted() -> None:
    assert allergy_facts("Dia ada alahan kepada 盘尼西林.", "ms") == [
        ("penicillin", "present")
    ]


# --- what must never be invented ---------------------------------------------


def test_conversational_filler_produces_no_fact() -> None:
    assert allergy_facts("ok lah, boleh.", "ms") == []


def test_an_unnamed_denial_never_becomes_a_confident_absent() -> None:
    """`denies` is recognised as allergy-adjacent but names no substance.

    It is recorded as unknown rather than as a denial, which is the fail-closed
    reading: an unsupported blanket NKDA is the output that gets a patient dosed
    with something they react to.
    """

    facts = allergy_facts("She denies any drug allergies.", "en")
    assert facts == [("*", "unknown")]
