"""Regressions for drug names damaged by real speech recognition.

Every case here comes from an actual faster-whisper transcription of macOS TTS
audio, not from an invented typo. The measured failure was a single dropped
letter: `penicillin` came back as `penicilin`, which the alias table did not
carry, so the family's reported allergy and the clinician's denial became two
different substances and the contradiction disappeared.
"""

from __future__ import annotations

import pytest

from trilingual_consult.lexicon import canonicalize_drug, canonicalize_drug_fuzzy
from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput, ConsultTurn


def test_the_alias_table_alone_does_not_cover_what_asr_actually_produces() -> None:
    """Pins the gap that motivated fuzzy recovery, so it cannot silently return."""

    assert canonicalize_drug("penisilin") == "penicillin"  # the guessed spelling
    assert canonicalize_drug("penicilin") is None  # what the ASR really emitted


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("penicilin", "penicillin"),
        ("amoxicilin", "amoxicillin"),
        ("metfomin", "metformin"),
    ],
)
def test_one_edit_recovers_the_canonical_drug(spelling: str, canonical: str) -> None:
    resolved, exact = canonicalize_drug_fuzzy(spelling)
    assert resolved == canonical
    assert exact is False


@pytest.mark.parametrize("spelling", ["asprin", "xyzabcd", "pen"])
def test_recovery_refuses_to_guess_when_the_evidence_is_thin(spelling: str) -> None:
    """Short or distant strings stay unmatched rather than resolving to a drug."""

    assert canonicalize_drug_fuzzy(spelling)[0] is None


def test_a_misspelt_drug_still_conflicts_with_its_correct_spelling() -> None:
    """The clinically dangerous case: a dropped letter hiding a contradiction.

    A clinician denies the allergy in English; the family reports it in Malay
    through a transcript that lost a letter. Before recovery these were two
    unrelated substances, so there was no conflict and nothing blocked
    publication of the denial.
    """

    state = run_consult_pipeline(
        ConsultInput(
            consult_id="asr-misspelling",
            turns=[
                ConsultTurn(
                    speaker_id="S0",
                    text="Patient is not allergic to penicillin.",
                    source_language="en",
                    speaker_role="clinician",
                ),
                ConsultTurn(
                    speaker_id="S1",
                    text="dia ada alahan kepada penicilin masa kecil.",
                    source_language="ms",
                    speaker_role="family",
                ),
            ],
        )
    )
    keys = {fact.key for fact in state.proposed_facts if fact.fact_type == "allergy"}
    assert keys == {"penicillin"}
    assert len(state.proposed_conflicts) == 1
    assert state.publish_blocked is True
    assert "DRUG_NAME_RECOVERED_FUZZILY" in state.warning_codes


def test_a_recovered_name_is_never_trusted_silently() -> None:
    state = run_consult_pipeline(
        ConsultInput(
            consult_id="asr-misspelling-review",
            turns=[
                ConsultTurn(
                    speaker_id="S0",
                    text="Patient is allergic to penicilin.",
                    source_language="en",
                    speaker_role="clinician",
                )
            ],
        )
    )
    facts = [f for f in state.proposed_facts if f.fact_type == "allergy"]
    assert facts and all(fact.review_required for fact in facts)
