from __future__ import annotations

import json
from pathlib import Path

import pytest

from trilingual_consult.eval import expected_fact_index, fact_index
from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "datasets" / "nightingale_switchcare" / "scripts"
EXPECTED = ROOT / "datasets" / "nightingale_switchcare" / "expected"

STEMS = [
    "consult-01",
    "consult-02-unlabeled",
    "consult-03-asr-noise",
    "consult-04-hokkien",
    "consult-05-dose-correction",
    "consult-06-overlap",
    "consult-07-intrasentential",
]


@pytest.mark.parametrize("stem", STEMS)
def test_gold_matches_expected(stem: str) -> None:
    payload = json.loads((SCRIPTS / f"{stem}.json").read_text(encoding="utf-8"))
    expected = json.loads((EXPECTED / f"{stem}.json").read_text(encoding="utf-8"))
    state = run_consult_pipeline(ConsultInput.from_dict(payload))
    assert state.consult_id == expected["consult_id"]
    assert state.speaker_roles == expected["speaker_roles"]
    assert expected_fact_index(expected) <= fact_index(state)
    assert state.publish_blocked is expected["publish_blocked"]
    for code in expected.get("warning_codes_required") or []:
        assert code in state.warning_codes
    for conflict in state.proposed_conflicts:
        assert conflict.auto_resolved is False
    expected_keys = {item["key"] for item in expected.get("conflicts") or []}
    predicted_keys = {conflict.key for conflict in state.proposed_conflicts}
    assert expected_keys <= predicted_keys
    if expected.get("require_quote_match", True):
        for fact in state.proposed_facts:
            turn = state.turns[fact.turn_index]
            assert turn.text[fact.start : fact.end] == fact.quote
    for needle in expected.get("patient_summary_must_contain") or []:
        assert needle in state.summary_proposals["patient_zh"]
