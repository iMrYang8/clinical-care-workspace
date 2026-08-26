#!/usr/bin/env python3
"""Cross-check Nightingale release evidence and optional PDF binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FULL_GATES = {"e2e", "benchmark", "ffmpeg"}
EVIDENCE_FILES = (
    "release-commit.txt",
    "verified-backend-image-id.txt",
    "release-verification-complete.txt",
    "verify-release.log",
    "glance-benchmark.json",
    "ffmpeg-container-version.txt",
    "release-candidate.txt",
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class EvidenceError(ValueError):
    """Raised when release evidence is incomplete or internally inconsistent."""


def _required_file(root: Path, name: str) -> Path:
    path = root / name
    if not path.is_file() or path.stat().st_size == 0:
        raise EvidenceError(f"release evidence is missing or empty: {path}")
    return path


def _read_kv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise EvidenceError(f"duplicate key {key!r} in {path}")
        values[key] = value
    return values


def _require_equal(label: str, *values: str) -> str:
    if not values or any(value != values[0] for value in values[1:]):
        raise EvidenceError(f"{label} mismatch: {values}")
    return values[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_log_section(log_text: str, heading: str) -> str:
    marker = f"==> {heading}"
    start = log_text.find(marker)
    if start < 0:
        raise EvidenceError(f"release log is missing section: {heading}")
    end = log_text.find("\n==> ", start + len(marker))
    return log_text[start:] if end < 0 else log_text[start:end]


def validate_release_evidence(
    evidence_root: Path | str, expected_commit: str | None = None
) -> dict[str, Any]:
    root = Path(evidence_root).resolve()
    for name in EVIDENCE_FILES:
        _required_file(root, name)

    commit = (root / "release-commit.txt").read_text(encoding="utf-8").strip()
    image_id = (
        (root / "verified-backend-image-id.txt").read_text(encoding="utf-8").strip()
    )
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise EvidenceError(f"invalid source commit: {commit!r}")
    if expected_commit is not None:
        _require_equal("expected source commit", commit, expected_commit)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise EvidenceError(f"invalid verified backend image ID: {image_id!r}")

    complete = _read_kv(root / "release-verification-complete.txt")
    if complete.get("status") != "complete":
        raise EvidenceError("release verification completion marker is not complete")
    gates = {gate for gate in complete.get("gates", "").split(",") if gate}
    if gates != FULL_GATES:
        raise EvidenceError(f"full release gates were not completed: {sorted(gates)}")
    _require_equal("completion commit", commit, complete.get("source_commit", ""))
    _require_equal(
        "completion image", image_id, complete.get("verified_backend_image_id", "")
    )
    completed_at = complete.get("completed_at_utc", "")
    try:
        datetime.strptime(completed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise EvidenceError(f"invalid completion timestamp: {completed_at!r}") from exc

    benchmark = json.loads((root / "glance-benchmark.json").read_text(encoding="utf-8"))
    compose = benchmark.get("compose", {})
    if benchmark.get("dirty") is not False:
        raise EvidenceError("Glance benchmark was not recorded from a clean worktree")
    if benchmark.get("gate", {}).get("p95_lte_300ms") is not True:
        raise EvidenceError("Glance p95 gate did not pass")
    _require_equal(
        "benchmark commit",
        commit,
        str(benchmark.get("commit", "")),
        str(compose.get("backend_image_revision", "")),
    )
    _require_equal(
        "benchmark image",
        image_id,
        str(compose.get("backend_image_digest", "")),
    )

    ffmpeg = _read_kv(root / "ffmpeg-container-version.txt")
    _require_equal(
        "FFmpeg commit",
        commit,
        ffmpeg.get("nightingale_source_commit", ""),
        ffmpeg.get("backend_image_revision_label", ""),
    )
    _require_equal("FFmpeg image", image_id, ffmpeg.get("backend_image_id", ""))
    ffmpeg_text = (root / "ffmpeg-container-version.txt").read_text(encoding="utf-8")
    ffmpeg_match = re.search(r"^ffmpeg version ([^\s]+)", ffmpeg_text, re.MULTILINE)
    if not ffmpeg_match:
        raise EvidenceError("FFmpeg evidence does not contain a version line")
    ffmpeg_version = ffmpeg_match.group(1)

    candidate = _read_kv(root / "release-candidate.txt")
    _require_equal("candidate commit", commit, candidate.get("source_commit", ""))
    _require_equal(
        "candidate image", image_id, candidate.get("verified_backend_image_id", "")
    )
    _require_equal(
        "candidate verification date",
        completed_at[:10],
        candidate.get("verification_date_utc", ""),
    )
    _require_equal("candidate FFmpeg", ffmpeg_version, candidate.get("ffmpeg", ""))

    backend_match = re.fullmatch(
        r"(\d+)_passed_(\d+)_skipped_coverage_(\d+)_percent",
        candidate.get("backend", ""),
    )
    frontend_match = re.fullmatch(r"(\d+)_passed", candidate.get("frontend_unit", ""))
    browser_match = re.fullmatch(
        r"(\d+)_passed",
        candidate.get("playwright_scenarios_a_to_f_repeat_3", ""),
    )
    if not backend_match or not frontend_match or not browser_match:
        raise EvidenceError("candidate test summary has an unexpected format")
    backend_passed, backend_skipped, coverage = backend_match.groups()
    frontend_passed = frontend_match.group(1)
    browser_passed = browser_match.group(1)
    if int(browser_passed) == 0 or int(browser_passed) % 3:
        raise EvidenceError("browser result is not a non-zero three-run total")

    target = benchmark.get("target", {})
    latency = benchmark.get("latency_ms", {})
    expected_glance = (
        f"{target.get('card_count')}_of_{target.get('expected_card_count')}_cards_"
        f"p95_{float(latency.get('p95')):.3f}_ms"
    )
    _require_equal(
        "candidate Glance result",
        expected_glance,
        candidate.get("glance_alex_synthetic", ""),
    )

    log_text = ANSI_ESCAPE.sub(
        "", (root / "verify-release.log").read_text(encoding="utf-8", errors="replace")
    )
    backend_log = _release_log_section(
        log_text, "Backend PostgreSQL contracts, coverage, and migration roundtrip"
    )
    frontend_log = _release_log_section(
        log_text, "Frontend type, lint, unit, and production build"
    )
    browser_log = _release_log_section(log_text, "Playwright Scenario A-F")
    ffmpeg_log = _release_log_section(log_text, "Container FFmpeg release evidence")
    _release_log_section(log_text, "Release verification complete")
    if not re.search(
        rf"\b{backend_passed} passed,\s+{backend_skipped} skipped\b", backend_log
    ):
        raise EvidenceError("backend pytest summary does not match candidate evidence")
    if not re.search(rf"^TOTAL\s+\d+\s+\d+\s+{coverage}%", backend_log, re.MULTILINE):
        raise EvidenceError("release log does not support candidate coverage field")
    if not re.search(rf"\bTests\s+{frontend_passed}\s+passed\b", frontend_log):
        raise EvidenceError("frontend unit summary does not match candidate evidence")
    if not re.search(rf"\b{browser_passed}\s+passed\b", browser_log):
        raise EvidenceError("Playwright summary does not match candidate evidence")
    if f"ffmpeg version {ffmpeg_version}" not in ffmpeg_log:
        raise EvidenceError("FFmpeg log section does not match candidate evidence")

    return {
        "root": root,
        "commit": commit,
        "image_id": image_id,
        "completed_at_utc": completed_at,
        "release": candidate,
        "benchmark": benchmark,
        "ffmpeg_version": ffmpeg_version,
    }


def write_pdf_binding(
    pdf_path: Path | str, evidence_root: Path | str, validated: dict[str, Any]
) -> Path:
    pdf = Path(pdf_path).resolve()
    root = Path(evidence_root).resolve()
    binding = Path(f"{pdf}.binding.json")
    payload = {
        "schema_version": 1,
        "source_commit": validated["commit"],
        "verified_backend_image_id": validated["image_id"],
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pdf_sha256": _sha256(pdf),
        "evidence_sha256": {name: _sha256(root / name) for name in EVIDENCE_FILES},
    }
    binding.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return binding


def validate_pdf_binding(
    pdf_path: Path | str, evidence_root: Path | str, validated: dict[str, Any]
) -> Path:
    pdf = Path(pdf_path).resolve()
    root = Path(evidence_root).resolve()
    binding = Path(f"{pdf}.binding.json")
    if not pdf.is_file() or pdf.stat().st_size == 0:
        raise EvidenceError(f"PDF is missing or empty: {pdf}")
    if not binding.is_file() or binding.stat().st_size == 0:
        raise EvidenceError(f"PDF evidence binding is missing or empty: {binding}")
    payload = json.loads(binding.read_text(encoding="utf-8"))
    _require_equal(
        "PDF binding commit", validated["commit"], payload.get("source_commit", "")
    )
    _require_equal(
        "PDF binding image",
        validated["image_id"],
        payload.get("verified_backend_image_id", ""),
    )
    _require_equal("PDF checksum", _sha256(pdf), payload.get("pdf_sha256", ""))
    expected_evidence = {name: _sha256(root / name) for name in EVIDENCE_FILES}
    if payload.get("evidence_sha256") != expected_evidence:
        raise EvidenceError("PDF binding does not match the current release evidence")
    return binding


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--pdf", type=Path)
    args = parser.parse_args()
    validated = validate_release_evidence(args.evidence_dir, args.expected_commit)
    result = {
        "status": "valid",
        "source_commit": validated["commit"],
        "verified_backend_image_id": validated["image_id"],
    }
    if args.pdf:
        result["pdf_binding"] = str(
            validate_pdf_binding(args.pdf, args.evidence_dir, validated)
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
