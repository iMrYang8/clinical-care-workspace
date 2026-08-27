#!/usr/bin/env python3
"""Measure the warm precomputed Glance API without invoking an AI provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def percentile(samples: list[float], value: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * value
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def git_commit(root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


def git_is_dirty(root: Path) -> bool:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
    )
    return bool(status.strip())


def command_output(command: list[str], *, cwd: Path) -> str | None:
    try:
        value = subprocess.check_output(
            command, cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return value or None


def compose_identity(
    root: Path, project: str | None, *, expected_commit: str
) -> dict[str, str]:
    if not project:
        raise RuntimeError(
            "Release Glance benchmark requires --compose-project so its running "
            "backend image can be bound to the checkout"
        )
    config = command_output(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "-f",
            "compose.yml",
            "-f",
            "compose.override.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
    )
    container_id = command_output(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "-f",
            "compose.yml",
            "-f",
            "compose.override.yml",
            "ps",
            "-q",
            "backend",
        ],
        cwd=root,
    )
    if not config or not container_id:
        raise RuntimeError(
            f"Compose project {project!r} has no inspectable running backend"
        )
    image_digest = command_output(
        ["docker", "inspect", "--format", "{{.Image}}", container_id],
        cwd=root,
    )
    if not image_digest:
        raise RuntimeError("Running backend container has no immutable image digest")
    image_revision = command_output(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            image_digest,
        ],
        cwd=root,
    )
    if image_revision != expected_commit:
        raise RuntimeError(
            "Running backend image revision "
            f"{image_revision!r} does not match checkout {expected_commit!r}"
        )
    return {
        "project": project,
        "config_sha256": hashlib.sha256(config.encode()).hexdigest(),
        "backend_image_digest": image_digest,
        "backend_image_revision": image_revision,
    }


def select_fixture_patient(
    patients: list[dict[str, Any]],
    *,
    display_name: str,
    patient_id: str | None,
) -> dict[str, Any]:
    """Resolve one named synthetic fixture; list ordering is not an identity."""

    matches = [
        patient
        for patient in patients
        if patient.get("display_name") == display_name
        and (patient_id is None or str(patient.get("id")) == patient_id)
    ]
    if len(matches) != 1:
        qualifier = f" and id {patient_id!r}" if patient_id else ""
        raise RuntimeError(
            "Glance benchmark requires exactly one patient fixture named "
            f"{display_name!r}{qualifier}; found {len(matches)}"
        )
    if not matches[0].get("id"):
        raise RuntimeError("Glance benchmark fixture has no patient id")
    return matches[0]


def validate_glance(
    payload: dict[str, Any],
    *,
    expected_card_count: int,
    expected_patient_id: str,
) -> int:
    if payload.get("source") != "precomputed":
        raise RuntimeError("Glance benchmark hit a non-precomputed read path")
    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise RuntimeError("Glance benchmark response has no cards list")
    if not cards:
        raise RuntimeError("Glance benchmark fixture returned an empty card list")
    if len(cards) != expected_card_count:
        raise RuntimeError(
            "Glance benchmark fixture returned "
            f"{len(cards)} cards; expected {expected_card_count}"
        )
    if str(payload.get("patient_id")) != expected_patient_id:
        raise RuntimeError(
            "Glance benchmark response patient mismatch: "
            f"expected {expected_patient_id}, got {payload.get('patient_id')}"
        )
    return len(cards)


def response_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    cards = payload.get("cards")
    card_keys = sorted(
        {key for card in cards if isinstance(card, dict) for key in card}
        if isinstance(cards, list)
        else set()
    )
    schema = {
        "top_level_keys": sorted(payload),
        "card_keys": card_keys,
    }
    canonical_body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    canonical_schema = json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "schema": schema,
        "schema_sha256": hashlib.sha256(canonical_schema).hexdigest(),
        "body_sha256": hashlib.sha256(canonical_body).hexdigest(),
    }


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    commit = git_commit(root)
    dirty = git_is_dirty(root)
    if dirty and not args.allow_dirty:
        raise RuntimeError(
            "Release benchmark requires a clean Git worktree; commit or stash changes"
        )
    verify = args.base_url.startswith("https://") and not args.insecure
    with httpx.Client(base_url=args.base_url, verify=verify, timeout=10.0) as client:
        login = client.post("/api/v1/auth/demo-login", json={"persona": args.persona})
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        patients = client.get("/api/v1/patients", headers=headers)
        patients.raise_for_status()
        patient = select_fixture_patient(
            patients.json()["data"],
            display_name=args.patient_display_name,
            patient_id=args.patient_id,
        )
        patient_id = str(patient["id"])
        path = f"/api/v1/patients/{patient_id}/glance"
        target = client.get(path, headers=headers)
        target.raise_for_status()
        target_payload = target.json()
        card_count = validate_glance(
            target_payload,
            expected_card_count=args.expected_card_count,
            expected_patient_id=patient_id,
        )
        for _ in range(args.warmup):
            response = client.get(path, headers=headers)
            response.raise_for_status()
            validate_glance(
                response.json(),
                expected_card_count=args.expected_card_count,
                expected_patient_id=patient_id,
            )
        durations: list[float] = []
        for _ in range(args.samples):
            started = time.perf_counter_ns()
            response = client.get(path, headers=headers)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            response.raise_for_status()
            validate_glance(
                response.json(),
                expected_card_count=args.expected_card_count,
                expected_patient_id=patient_id,
            )
            durations.append(elapsed_ms)

    p95 = percentile(durations, 0.95)
    result: dict[str, Any] = {
        "schema_version": 2,
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": commit,
        "dirty": dirty,
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "not reported",
            "logical_cpu_count": os.cpu_count(),
        },
        "config": {
            "base_url": args.base_url,
            "endpoint": "/api/v1/patients/{synthetic_patient_id}/glance",
            "auth_transport": "bearer (benchmark/API compatibility only)",
            "warmup": args.warmup,
            "samples": args.samples,
            "tls_verification": verify,
            "threshold_ms": args.threshold_ms,
        },
        "target": {
            "patient_id": patient_id,
            "patient_display_name": patient["display_name"],
            "card_count": card_count,
            "expected_card_count": args.expected_card_count,
        },
        "response": response_fingerprint(target_payload),
        "compose": compose_identity(root, args.compose_project, expected_commit=commit),
        "latency_ms": {
            "median": round(statistics.median(durations), 3),
            "p95": round(p95, 3),
            "p99": round(percentile(durations, 0.99), 3),
            "min": round(min(durations), 3),
            "max": round(max(durations), 3),
        },
        "gate": {"p95_lte_300ms": p95 <= args.threshold_ms},
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://localhost")
    parser.add_argument("--persona", default="staff", choices=("staff", "clinician"))
    parser.add_argument("--patient-display-name", default="Alex Tan")
    parser.add_argument(
        "--patient-id",
        help="Optionally pin the deterministic fixture UUID in addition to its name.",
    )
    parser.add_argument("--expected-card-count", type=int, default=4)
    parser.add_argument(
        "--compose-project",
        default=os.environ.get("NIGHTINGALE_BENCHMARK_COMPOSE_PROJECT"),
        help="Record the exact local Compose project/config/image identity.",
    )
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--threshold-ms", type=float, default=300.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/evidence/glance-benchmark.json"),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Allow the generated local Traefik certificate only.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Exploratory-only mode; release evidence must omit this flag.",
    )
    args = parser.parse_args()
    if args.warmup < 0 or args.samples < 1 or args.expected_card_count < 1:
        parser.error(
            "warmup must be >= 0, samples must be >= 1, and expected-card-count must be >= 1"
        )
    result = benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["gate"]["p95_lte_300ms"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
