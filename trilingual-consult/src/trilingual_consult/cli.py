"""Gold-file in, synthetic JSON report out. Not a clinical quality claim."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.report import render_markdown
from trilingual_consult.state import ConsultInput


def _digest(payload: dict[object, object]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the isolated trilingual consult agent pipeline on gold JSON."
    )
    parser.add_argument("gold_json", type=Path)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "artifacts",
    )
    args = parser.parse_args(argv)
    payload = json.loads(args.gold_json.read_text(encoding="utf-8"))
    state = run_consult_pipeline(ConsultInput.from_dict(payload))
    report = state.to_dict()
    report["input_path"] = str(args.gold_json)
    report["digest_sha256"] = _digest(
        {key: value for key, value in report.items() if key != "agent_trace"}
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"switchcare-{state.consult_id}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path = args.out_dir / f"switchcare-{state.consult_id}.md"
    md_path.write_text(render_markdown(state), encoding="utf-8")
    print(out_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
