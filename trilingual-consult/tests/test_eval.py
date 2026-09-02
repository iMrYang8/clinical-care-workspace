from __future__ import annotations

import json
from pathlib import Path

from trilingual_consult.eval import main

ROOT = Path(__file__).resolve().parents[1]
GOLD_DIR = ROOT / "datasets" / "nightingale_switchcare"


def test_eval_scores_gold_family_and_consult_01_invariants(tmp_path: Path) -> None:
    assert main(["--gold-dir", str(GOLD_DIR), "--out-dir", str(tmp_path)]) == 0
    summary_path = tmp_path / "eval-summary.json"
    assert summary_path.is_file()
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["not_wer"] is True
    assert payload["not_polywer"] is True
    ids = {item["consult_id"] for item in payload["consults"]}
    assert ids == {
        "consult-01",
        "consult-02-unlabeled",
        "consult-03-asr-noise",
        "consult-04-hokkien",
        "consult-05-dose-correction",
        "consult-06-overlap",
        "consult-07-intrasentential",
    }
    by_id = {item["consult_id"]: item for item in payload["consults"]}
    assert by_id["consult-01"]["invariants_ok"] is True
    assert by_id["consult-02-unlabeled"]["invariants_ok"] is True
    assert by_id["consult-04-hokkien"]["invariants_ok"] is True
    assert by_id["consult-05-dose-correction"]["publish_blocked"] is True
    assert "OVERLAP_REVIEW" in by_id["consult-06-overlap"]["warning_codes"]
    assert by_id["consult-07-intrasentential"]["invariants_ok"] is True
    assert by_id["consult-07-intrasentential"]["publish_blocked"] is False
    assert payload["invariants_ok"] is True
