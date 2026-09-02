"""Gold-text extraction eval. Not WER, not PolyWER, not a clinical quality claim."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.state import ConsultInput, ConsultState, ProposedFact

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD_DIR = ROOT / "datasets" / "nightingale_switchcare"
DEFAULT_OUT_DIR = ROOT / "artifacts"


def fact_signature_from_state(fact: ProposedFact) -> tuple[str, str, str, str]:
    value = fact.polarity if fact.fact_type == "allergy" else fact.value
    return (fact.fact_type, fact.key, value, fact.speaker_role)


def fact_signature_from_expected(item: dict[str, Any]) -> tuple[str, str, str, str]:
    fact_type = str(item["fact_type"])
    key = str(item["key"])
    if fact_type == "allergy":
        value = str(item.get("polarity") or item.get("value") or "")
    else:
        value = str(item.get("value") or key)
    return (fact_type, key, value, str(item["speaker_role"]))


def fact_index(state: ConsultState) -> set[tuple[str, str, str, str, bool]]:
    return {
        (*fact_signature_from_state(fact), fact.review_required)
        for fact in state.proposed_facts
    }


def expected_fact_index(expected: dict[str, Any]) -> set[tuple[str, str, str, str, bool]]:
    index: set[tuple[str, str, str, str, bool]] = set()
    for item in expected.get("facts") or []:
        review = bool(item.get("review_required"))
        index.add((*fact_signature_from_expected(item), review))
    return index


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 4)


def score_consult(state: ConsultState, expected: dict[str, Any]) -> dict[str, Any]:
    predicted_roles = dict(sorted(state.speaker_roles.items()))
    expected_roles = {
        str(key): str(value)
        for key, value in (expected.get("speaker_roles") or {}).items()
    }
    role_matches = sum(
        1 for speaker_id, role in expected_roles.items() if predicted_roles.get(speaker_id) == role
    )
    role_accuracy = _ratio(role_matches, len(expected_roles))

    predicted_facts = {fact_signature_from_state(fact) for fact in state.proposed_facts}
    expected_facts = {fact_signature_from_expected(item) for item in expected.get("facts") or []}
    fact_true_positive = len(predicted_facts & expected_facts)
    fact_precision = _ratio(fact_true_positive, len(predicted_facts))
    fact_recall = _ratio(fact_true_positive, len(expected_facts))

    expected_conflict_keys = {
        str(item["key"]) for item in expected.get("conflicts") or [] if "key" in item
    }
    predicted_conflicts = list(state.proposed_conflicts)
    found_keys = {conflict.key for conflict in predicted_conflicts} & expected_conflict_keys
    all_unresolved = all(conflict.auto_resolved is False for conflict in predicted_conflicts)
    conflict_recall = _ratio(len(found_keys), len(expected_conflict_keys))
    if predicted_conflicts and not all_unresolved:
        conflict_recall = 0.0

    invariant_failures: list[str] = []
    if bool(expected.get("publish_blocked")) != state.publish_blocked:
        invariant_failures.append("publish_blocked")
    for code in expected.get("warning_codes_required") or []:
        if code not in state.warning_codes:
            invariant_failures.append(f"missing_warning:{code}")
    if "HOKKIEN_ASR_UNSUPPORTED" in state.warning_codes:
        nkda = [
            fact
            for fact in state.proposed_facts
            if fact.fact_type == "allergy" and fact.polarity == "absent"
        ]
        if nkda:
            invariant_failures.append("hokkien_false_nkda")
    for fact in state.proposed_facts:
        if fact.fact_type == "allergy" and fact.speaker_role == "family" and not fact.review_required:
            invariant_failures.append("family_allergy_not_review_required")
            break

    return {
        "consult_id": state.consult_id,
        "role_accuracy": role_accuracy,
        "fact_precision": fact_precision,
        "fact_recall": fact_recall,
        "conflict_recall": conflict_recall,
        "invariants_ok": not invariant_failures,
        "invariant_failures": invariant_failures,
        "cmi": state.cmi,
        "switch_points": state.switch_points,
        "publish_blocked": state.publish_blocked,
        "warning_codes": list(state.warning_codes),
    }


def load_gold_pair(script_path: Path, expected_dir: Path) -> tuple[ConsultInput, dict[str, Any]]:
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    expected_path = expected_dir / script_path.name
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    return ConsultInput.from_dict(payload), expected


def evaluate_gold_dir(gold_dir: Path) -> dict[str, Any]:
    scripts_dir = gold_dir / "scripts"
    expected_dir = gold_dir / "expected"
    consults: list[dict[str, Any]] = []
    for script_path in sorted(scripts_dir.glob("consult-*.json")):
        consult, expected = load_gold_pair(script_path, expected_dir)
        state = run_consult_pipeline(consult)
        consults.append(score_consult(state, expected))
    return {
        "synthetic": True,
        "not_clinical_validation": True,
        "metric": "gold-text extraction eval",
        "not_wer": True,
        "not_polywer": True,
        "consults": consults,
        "invariants_ok": all(item["invariants_ok"] for item in consults),
    }


def _markdown_table(summary: dict[str, Any]) -> str:
    lines = [
        "# Gold-text extraction eval",
        "",
        "Synthetic gold. Not WER. Not PolyWER. Not clinical validation.",
        "",
        "| consult | role_acc | fact_P | fact_R | conflict_R | invariants | cmi | switches |",
        "|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for item in summary["consults"]:
        lines.append(
            "| {consult_id} | {role_accuracy:.2f} | {fact_precision:.2f} | {fact_recall:.2f} | "
            "{conflict_recall:.2f} | {invariants} | {cmi} | {switch_points} |".format(
                consult_id=item["consult_id"],
                role_accuracy=item["role_accuracy"],
                fact_precision=item["fact_precision"],
                fact_recall=item["fact_recall"],
                conflict_recall=item["conflict_recall"],
                invariants="ok" if item["invariants_ok"] else "FAIL",
                cmi=item["cmi"],
                switch_points=item["switch_points"],
            )
        )
    lines.append("")
    lines.append(
        "Family invariants "
        + ("passed." if summary["invariants_ok"] else "FAILED — do not average this away.")
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score NightingaleSwitchCare gold scripts (text extraction, not ASR)."
    )
    parser.add_argument("--gold-dir", type=Path, default=DEFAULT_GOLD_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    summary = evaluate_gold_dir(args.gold_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "eval-summary.json"
    out_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(_markdown_table(summary), end="")
    print(out_path)
    return 0 if summary["invariants_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
