#!/usr/bin/env python3
"""Measure the warm precomputed Glance API without invoking an AI provider."""

from __future__ import annotations

import argparse
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


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    verify = args.base_url.startswith("https://") and not args.insecure
    with httpx.Client(base_url=args.base_url, verify=verify, timeout=10.0) as client:
        login = client.post("/api/v1/auth/demo-login", json={"persona": args.persona})
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        patients = client.get("/api/v1/patients", headers=headers)
        patients.raise_for_status()
        patient_id = patients.json()["data"][0]["id"]
        path = f"/api/v1/patients/{patient_id}/glance"
        for _ in range(args.warmup):
            response = client.get(path, headers=headers)
            response.raise_for_status()
        durations: list[float] = []
        for _ in range(args.samples):
            started = time.perf_counter_ns()
            response = client.get(path, headers=headers)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            response.raise_for_status()
            if response.json().get("source") != "precomputed":
                raise RuntimeError("Glance benchmark hit a non-precomputed read path")
            durations.append(elapsed_ms)

    p95 = percentile(durations, 0.95)
    result: dict[str, Any] = {
        "schema_version": 1,
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(root),
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
    args = parser.parse_args()
    if args.warmup < 0 or args.samples < 1:
        parser.error("warmup must be >= 0 and samples must be >= 1")
    result = benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["gate"]["p95_lte_300ms"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
