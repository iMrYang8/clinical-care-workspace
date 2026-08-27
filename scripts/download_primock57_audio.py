#!/usr/bin/env python3
"""Hydrate every pinned PriMock57 Git LFS WAV with checksum verification."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED_COMMIT = "cd2ac707ad03cb4d2531f4ec6b90c659bf4357c5"
MEDIA_ROOT = "https://media.githubusercontent.com/media/babylonhealth/primock57"
LFS_VERSION = "version https://git-lfs.github.com/spec/v1"
BUFFER_SIZE = 1024 * 1024


@dataclass(frozen=True)
class LFSObject:
    name: str
    sha256: str
    size: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def parse_pointer(path: Path) -> LFSObject:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 3 or lines[0] != LFS_VERSION:
        raise ValueError(f"Not a Git LFS pointer: {path}")
    oid = re.fullmatch(r"oid sha256:([0-9a-f]{64})", lines[1])
    size = re.fullmatch(r"size ([0-9]+)", lines[2])
    if oid is None or size is None:
        raise ValueError(f"Malformed Git LFS pointer: {path}")
    return LFSObject(path.name, oid.group(1), int(size.group(1)))


def _download_once(spec: LFSObject, destination: Path, commit: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (
        destination.is_file()
        and destination.stat().st_size == spec.size
        and sha256_file(destination) == spec.sha256
    ):
        return

    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    url = f"{MEDIA_ROOT}/{commit}/audio/{urllib.parse.quote(spec.name)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nightingale-PriMock57-Evaluation/1.0"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        with temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=BUFFER_SIZE)
    if temporary.stat().st_size != spec.size:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"Size mismatch for {spec.name}")
    actual = sha256_file(temporary)
    if actual != spec.sha256:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"Checksum mismatch for {spec.name}: {actual} != {spec.sha256}"
        )
    temporary.replace(destination)


def download_one(
    spec: LFSObject,
    destination_root: Path,
    commit: str,
    attempts: int,
) -> tuple[str, int]:
    destination = destination_root / spec.name
    for attempt in range(attempts):
        try:
            _download_once(spec, destination, commit)
            return spec.name, spec.size
        except (OSError, urllib.error.URLError, ValueError):
            if attempt + 1 == attempts:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pointer-root",
        type=Path,
        default=None,
        help="Extracted PriMock57 audio directory containing Git LFS pointers",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "datasets/raw/primock57/audio",
    )
    parser.add_argument("--commit", default=PINNED_COMMIT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")

    pointer_root = args.pointer_root
    if pointer_root is None:
        extracted = ROOT / "datasets/raw/primock57/extracted"
        repositories = sorted(extracted.glob("primock57-*"))
        if len(repositories) != 1:
            parser.error("Expected exactly one pinned PriMock57 extracted repository")
        pointer_root = repositories[0] / "audio"
    specs = sorted(
        (parse_pointer(path) for path in pointer_root.glob("*.wav")),
        key=lambda item: item.name,
    )
    if len(specs) != 114:
        parser.error(f"Expected 114 WAV pointers, found {len(specs)}")

    completed = 0
    completed_bytes = 0
    total_bytes = sum(item.size for item in specs)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                download_one,
                spec,
                args.destination,
                args.commit,
                args.attempts,
            ): spec
            for spec in specs
        }
        for future in as_completed(futures):
            name, size = future.result()
            completed += 1
            completed_bytes += size
            sys.stdout.write(
                f"[{completed:03d}/{len(specs)}] {name} "
                f"({completed_bytes / total_bytes:.1%})\n"
            )
            sys.stdout.flush()

    doctors = list(args.destination.glob("*_doctor.wav"))
    patients = list(args.destination.glob("*_patient.wav"))
    if len(doctors) != 57 or len(patients) != 57:
        raise RuntimeError(
            f"Incomplete audio set: doctor={len(doctors)}, patient={len(patients)}"
        )
    sys.stdout.write(
        f"PriMock57 audio ready: 57 consultations, {total_bytes} verified bytes\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
