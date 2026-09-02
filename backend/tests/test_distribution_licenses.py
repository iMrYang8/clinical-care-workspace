import hashlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_required_distribution_license_texts_are_complete_and_versioned() -> None:
    directory = ROOT / "THIRD_PARTY_LICENSES"
    required = {
        "Nightingale-MIT.txt": "MIT License",
        "FastAPI-Template-MIT.txt": "Copyright (c) 2019 Sebastián Ramírez",
        "ATTRIBUTION.txt": "FastAPI Full Stack FastAPI",
        "Serene-Comment-Extension-MIT.txt": "Copyright (c) 2023 Jeet Mandaliya",
        "Tiptap-MIT.txt": "Copyright (c) 2025, Tiptap GmbH",
        "idb-ISC.txt": "Copyright (c) 2016, Jake Archibald",
        "CTranslate2-MIT.txt": "The OpenNMT Authors",
        "PyAV-BSD-3-Clause.txt": "Copyright retained by original committers",
        "Presidio-MIT.txt": "Copyright (c) Presidio Contributors",
        "Presidio-NOTICE.txt": "THIRD-PARTY SOFTWARE NOTICES AND INFORMATION",
        "Pyannote-Audio-MIT.txt": "Copyright (c) 2020 CNRS",
        "Pyannote-CITATION.bib": "Powerset multi-class cross entropy loss",
        "DISTRIBUTION_NOTICES.md": "CTranslate2",
        "THIRD_PARTY_NOTICES.md": "PyAV",
    }
    for name, marker in required.items():
        content = (directory / name).read_text()
        assert marker in content
        assert len(content) > 100

    notices = (directory / "DISTRIBUTION_NOTICES.md").read_text()
    assert "4.8.1" in notices
    assert "18.1.0" in notices
    assert "not tested" in notices.lower()
    assert "Presidio-NOTICE.txt" in notices
    assert "Pyannote-CITATION.bib" in notices

    root_license = (ROOT / "LICENSE").read_text()
    assert "Copyright (c) 2019 Sebastián Ramírez" in root_license
    assert "Copyright (c) 2026 Nightingale contributors" in root_license

    packaged_register = (directory / "THIRD_PARTY_NOTICES.md").read_text()
    assert "`](./LICENSE)" not in packaged_register
    assert "`](../LICENSE)" in packaged_register


def test_backend_image_copies_notices_and_binds_source_commit() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    assert "COPY ./THIRD_PARTY_LICENSES" in dockerfile
    assert "/usr/share/doc/nightingale/THIRD_PARTY_LICENSES" in dockerfile
    assert "org.opencontainers.image.revision=$NIGHTINGALE_SOURCE_COMMIT" in dockerfile
    assert "Serene-Comment-Extension-MIT.txt" in dockerfile
    assert "Presidio-MIT.txt" in dockerfile
    assert "Presidio-NOTICE.txt" in dockerfile
    assert "Pyannote-Audio-MIT.txt" in dockerfile
    assert "Pyannote-CITATION.bib" in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert compose.count("\n        NIGHTINGALE_SOURCE_COMMIT:") == 3
    capture = (ROOT / "scripts" / "capture_ffmpeg_inventory.sh").read_text()
    assert "git status --porcelain --untracked-files=all" in capture
    assert "org.opencontainers.image.revision" in capture
    assert (
        'docker run --rm --entrypoint ffmpeg "$immutable_image_id" -version' in capture
    )
    assert "run --rm --no-deps -T backend" not in capture
    assert "image_commit" in capture and "immutable_image_id" in capture


def test_distributed_notices_bind_the_exact_ffmpeg_evidence_digest() -> None:
    evidence = ROOT / "docs" / "evidence" / "ffmpeg-container-version.txt"
    expected = hashlib.sha256(evidence.read_bytes()).hexdigest()
    for notice in (
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "THIRD_PARTY_LICENSES" / "THIRD_PARTY_NOTICES.md",
    ):
        content = notice.read_text()
        match = re.search(
            r"ffmpeg-container-version\.txt`.*file SHA-256 `([0-9a-f]{64})`",
            content,
        )
        assert match, f"{notice} has no FFmpeg evidence digest declaration"
        assert match.group(1) == expected


def test_ffmpeg_inventory_binds_the_exact_release_candidate() -> None:
    evidence = ROOT / "docs" / "evidence" / "ffmpeg-container-version.txt"
    release = ROOT / "docs" / "evidence" / "release-candidate.txt"
    expected_digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    evidence_values = dict(
        line.split("=", 1) for line in evidence.read_text().splitlines()[:3]
    )
    release_values = dict(
        line.split("=", 1) for line in release.read_text().splitlines() if "=" in line
    )
    source_commit = evidence_values["nightingale_source_commit"]
    image_id = evidence_values["backend_image_id"]
    verification_date = release_values["verification_date_utc"]

    assert evidence_values["backend_image_revision_label"] == source_commit
    assert release_values["source_commit"] == source_commit
    assert release_values["verified_backend_image_id"] == image_id

    for manifest in (
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "THIRD_PARTY_LICENSES" / "THIRD_PARTY_NOTICES.md",
        ROOT / "MODEL_INVENTORY.md",
    ):
        content = manifest.read_text()
        assert expected_digest in content
        assert source_commit in content
        assert image_id in content
        assert verification_date in content
        assert "Debian amd64" in content
