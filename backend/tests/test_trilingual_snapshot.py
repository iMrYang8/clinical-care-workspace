from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "app" / "services" / "voice" / "_sandbox" / "trilingual_consult"
# The package now lives in this repository. A sibling checkout beside the repo
# is still honoured so an existing working copy keeps working, but the in-repo
# copy wins -- same precedence as scripts/sync-trilingual-sandbox.sh, or the two
# would disagree about what the snapshot is meant to match.
IN_REPO = BACKEND.parents[0] / "trilingual-consult" / "src" / "trilingual_consult"
SIBLING = BACKEND.parents[1] / "trilingual-consult" / "src" / "trilingual_consult"
SOURCE = IN_REPO if IN_REPO.is_dir() else SIBLING
_SKIP_NAMES = {
    "cli.py",
    "eval.py",
    "eval_audio.py",
    "report.py",
    "polywer.py",
    "audio_bench.py",
    "__main__.py",
}


def _py_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or path.name in _SKIP_NAMES:
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


@pytest.mark.unit
def test_vendored_trilingual_snapshot_matches_source_when_present() -> None:
    if not SOURCE.is_dir():
        pytest.skip("trilingual-consult package is not in this checkout")
    sibling = _py_files(SOURCE)
    snapshot = _py_files(SNAPSHOT)
    assert snapshot, (
        "vendored snapshot is empty; run scripts/sync-trilingual-sandbox.sh"
    )
    assert snapshot == {
        key: digest
        for key, digest in sibling.items()
        if key.split("/")[-1] not in _SKIP_NAMES
    }
