#!/usr/bin/env python3
"""Self-contained tamper tests for the release evidence validator."""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
from pathlib import Path

from validate_release_evidence import (
    EvidenceError,
    validate_pdf_binding,
    validate_release_evidence,
    write_pdf_binding,
)


ROOT = Path(__file__).resolve().parents[1]


def read_kv(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def expect_invalid(root: Path) -> None:
    try:
        validate_release_evidence(root)
    except EvidenceError:
        return
    raise AssertionError("tampered evidence unexpectedly validated")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nightingale-evidence-test-") as raw:
        evidence = Path(raw)
        for name in (
            "glance-benchmark.json",
            "ffmpeg-container-version.txt",
            "release-candidate.txt",
        ):
            shutil.copy2(ROOT / "docs" / "evidence" / name, evidence / name)

        candidate = read_kv(evidence / "release-candidate.txt")
        commit = candidate["source_commit"]
        image = candidate["verified_backend_image_id"]
        backend = candidate["backend"].split("_")
        frontend = candidate["frontend_unit"].split("_", 1)[0]
        browser = candidate["playwright_scenarios_a_to_f_repeat_3"].split("_", 1)[0]
        (evidence / "release-commit.txt").write_text(f"{commit}\n")
        (evidence / "verified-backend-image-id.txt").write_text(f"{image}\n")
        (evidence / "release-verification-complete.txt").write_text(
            "status=complete\n"
            f"source_commit={commit}\n"
            f"verified_backend_image_id={image}\n"
            "gates=e2e,benchmark,ffmpeg\n"
            f"completed_at_utc={candidate['verification_date_utc']}T12:00:00Z\n"
        )
        ffmpeg_digest = hashlib.sha256(
            (evidence / "ffmpeg-container-version.txt").read_bytes()
        ).hexdigest()
        (evidence / "verify-release.log").write_text(
            "==> Backend PostgreSQL contracts, coverage, and migration roundtrip\n"
            f"{backend[0]} passed, {backend[2]} skipped\n"
            f"TOTAL 1000 90 {backend[5]}%\n"
            "==> Frontend type, lint, unit, and production build\n"
            f"Tests {frontend} passed\n"
            "==> Playwright Scenario A-F\n"
            f"{browser} passed\n"
            "==> Container FFmpeg release evidence\n"
            "Captured container FFmpeg evidence: fixture\n"
            f"SHA-256: {ffmpeg_digest}\n"
            "==> Release verification complete\n"
        )

        validated = validate_release_evidence(evidence, commit)
        benchmark_path = evidence / "glance-benchmark.json"
        original_benchmark = benchmark_path.read_text(encoding="utf-8")
        benchmark = json.loads(original_benchmark)
        benchmark["compose"]["backend_image_digest"] = "sha256:" + "0" * 64
        benchmark_path.write_text(json.dumps(benchmark))
        expect_invalid(evidence)
        benchmark_path.write_text(original_benchmark)

        candidate_path = evidence / "release-candidate.txt"
        original_candidate = candidate_path.read_text(encoding="utf-8")
        candidate_path.write_text(
            original_candidate.replace(
                candidate["backend"],
                f"{browser}_passed_{backend[2]}_skipped_coverage_{backend[5]}_percent",
            )
        )
        expect_invalid(evidence)
        candidate_path.write_text(original_candidate)

        pdf = evidence / "brief.pdf"
        pdf.write_bytes(b"%PDF-1.4\nsynthetic validator fixture\n")
        write_pdf_binding(pdf, evidence, validated)
        validate_pdf_binding(pdf, evidence, validated)
        pdf.write_bytes(pdf.read_bytes() + b"tampered\n")
        try:
            validate_pdf_binding(pdf, evidence, validated)
        except EvidenceError:
            pass
        else:
            raise AssertionError("tampered PDF unexpectedly validated")

    print("Release evidence commit/image/log/PDF tamper checks passed.")


if __name__ == "__main__":
    main()
