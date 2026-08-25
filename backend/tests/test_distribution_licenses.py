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
        "ATTRIBUTION.txt": "FastAPI Full Stack FastAPI",
        "Serene-Comment-Extension-MIT.txt": "Copyright (c) 2023 Jeet Mandaliya",
        "Tiptap-MIT.txt": "Copyright (c) 2025, Tiptap GmbH",
        "idb-ISC.txt": "Copyright (c) 2016, Jake Archibald",
        "CTranslate2-MIT.txt": "The OpenNMT Authors",
        "PyAV-BSD-3-Clause.txt": "Copyright retained by original committers",
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


def test_backend_image_copies_notices_and_binds_source_commit() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()
    assert "COPY ./THIRD_PARTY_LICENSES" in dockerfile
    assert "/usr/share/doc/nightingale/THIRD_PARTY_LICENSES" in dockerfile
    assert "org.opencontainers.image.revision=$NIGHTINGALE_SOURCE_COMMIT" in dockerfile
    assert "Serene-Comment-Extension-MIT.txt" in dockerfile
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
