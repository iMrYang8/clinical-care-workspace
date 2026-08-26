#!/usr/bin/env python3
"""Download and verify the pinned Nightingale synthetic evaluation pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "datasets/manifests/evaluation-pack-v1.json"
DEFAULT_RAW = ROOT / "datasets/raw"
BUFFER_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(name: str) -> Path:
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe archive member: {name}")
    return Path(*candidate.parts)


def download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nightingale-Synthetic-Evaluation-Pack/1.0"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        with temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=BUFFER_SIZE)
    actual = sha256_file(temporary)
    if actual != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"Checksum mismatch for {destination.name}: {actual} != {expected_sha256}"
        )
    temporary.replace(destination)


def extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = destination / _safe_relative(member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=BUFFER_SIZE)


def extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            if not member.isfile() and not member.isdir():
                continue
            target = destination / _safe_relative(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"Missing archive payload: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=BUFFER_SIZE)


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        manifest: dict[str, Any] = json.load(handle)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported evaluation-pack manifest")
    return manifest


def materialize_dataset(raw_root: Path, dataset: dict[str, Any]) -> dict[str, Any]:
    name = str(dataset["name"])
    dataset_root = raw_root / name
    archive_spec = dict(dataset["archive"])
    archive_path = dataset_root / str(archive_spec["filename"])
    download(str(archive_spec["url"]), archive_path, str(archive_spec["sha256"]))

    extracted = dataset_root / "extracted"
    marker = extracted / ".archive-sha256"
    if not marker.is_file() or marker.read_text().strip() != archive_spec["sha256"]:
        shutil.rmtree(extracted, ignore_errors=True)
        extracted.mkdir(parents=True)
        if archive_spec["format"] == "zip":
            extract_zip(archive_path, extracted)
        elif archive_spec["format"] == "tar.gz":
            extract_tar(archive_path, extracted)
        else:
            raise ValueError(f"Unsupported archive format: {archive_spec['format']}")
        marker.write_text(str(archive_spec["sha256"]) + "\n")

    downloaded_files: list[dict[str, str]] = []
    for file_spec_raw in dataset.get("files", []):
        file_spec = dict(file_spec_raw)
        destination = dataset_root / _safe_relative(str(file_spec["filename"]))
        download(str(file_spec["url"]), destination, str(file_spec["sha256"]))
        downloaded_files.append(
            {"path": str(destination), "sha256": str(file_spec["sha256"])}
        )
    return {
        "name": name,
        "version": dataset["version"],
        "archive": str(archive_path),
        "archive_sha256": archive_spec["sha256"],
        "extracted": str(extracted),
        "files": downloaded_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    args = parser.parse_args()

    manifest = load_manifest(args.manifest.resolve())
    args.raw_root.mkdir(parents=True, exist_ok=True)
    result = {
        "pack_id": manifest["pack_id"],
        "datasets": [
            materialize_dataset(args.raw_root.resolve(), dict(dataset))
            for dataset in manifest["datasets"]
        ],
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
