from __future__ import annotations

import json
from pathlib import Path

from trilingual_consult.cli import main

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "datasets" / "nightingale_switchcare" / "scripts" / "consult-01.json"


def test_cli_writes_synthetic_report(tmp_path: Path) -> None:
    assert main([str(GOLD), "--out-dir", str(tmp_path)]) == 0
    report_path = tmp_path / "switchcare-consult-01.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["synthetic"] is True
    assert payload["not_clinical_validation"] is True
    assert payload["consult_id"] == "consult-01"
    assert payload["publish_blocked"] is True
    assert payload["digest_sha256"]
    markdown = (tmp_path / "switchcare-consult-01.md").read_text(encoding="utf-8")
    assert "synthetic" in markdown.lower()
    assert "publish_blocked" in markdown
    assert "盘尼西林" in markdown
    assert "speaker_role" in markdown
