"""The scenario-to-test map must keep pointing at tests that exist.

`docs/SCENARIO_TEST_MAP.md` is the index a reviewer reads to find the automated
coverage for each of the sixteen clinic scenarios. An index that silently rots
is worse than no index, so every reference it makes is resolved here against
the source tree.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / "docs" / "SCENARIO_TEST_MAP.md"

SCENARIO_COUNT = 16

# `file.py::test_name`, optionally prefixed with a path. Unprefixed files are
# backend tests, which is where most of the coverage lives.
REFERENCE = re.compile(r"`([\w./-]*?test_[\w./-]*\.py)::(test_[a-z0-9_]+)`")

# A browser test is cited by its own title, because Playwright has no
# importable function name.
BROWSER_REFERENCE = re.compile(r"`(frontend/tests/[\w.-]+\.spec\.ts)` — `([^`]+)`")


def _resolve(relative: str) -> Path:
    if "/" in relative:
        return ROOT / relative
    return ROOT / "backend" / "tests" / relative


def test_scenario_map_exists_and_covers_all_sixteen_scenarios() -> None:
    assert MAP.is_file(), f"missing scenario map: {MAP}"
    text = MAP.read_text(encoding="utf-8")
    for number in range(1, SCENARIO_COUNT + 1):
        heading = f"\n## {number} · "
        assert heading in text, f"scenario {number} has no section in the map"


def test_every_referenced_python_test_exists() -> None:
    text = MAP.read_text(encoding="utf-8")
    references = REFERENCE.findall(text)
    assert references, "the map cites no tests at all"

    missing: list[str] = []
    for relative, name in references:
        path = _resolve(relative)
        if not path.is_file():
            missing.append(f"{relative} (file not found)")
            continue
        source = path.read_text(encoding="utf-8")
        if not re.search(rf"^(?:async )?def {re.escape(name)}\(", source, re.M):
            missing.append(f"{relative}::{name}")

    assert not missing, "scenario map cites tests that no longer exist: " + ", ".join(
        missing
    )


def test_every_referenced_browser_test_exists() -> None:
    text = MAP.read_text(encoding="utf-8")
    missing: list[str] = []
    for relative, title in BROWSER_REFERENCE.findall(text):
        path = ROOT / relative
        if not path.is_file():
            missing.append(f"{relative} (file not found)")
            continue
        if title not in path.read_text(encoding="utf-8"):
            missing.append(f"{relative}: {title}")

    assert not missing, "scenario map cites browser tests that no longer exist: " + (
        ", ".join(missing)
    )


def test_recorded_scenarios_in_the_map_match_the_recorder() -> None:
    """Every video the map claims must be one the demo recorder can produce."""
    definitions = ROOT / "scripts" / "demo" / "scenario_definitions.mjs"
    declared = set(re.findall(r'id:\s*"([\w-]+)"', definitions.read_text("utf-8")))
    assert declared, "the demo recorder declares no scenarios"

    text = MAP.read_text(encoding="utf-8")
    claimed = set(re.findall(r"`Nightingale_Scenario_([\w-]+)\.mp4`", text))
    assert claimed <= declared, (
        "the map claims footage the recorder cannot produce: "
        f"{sorted(claimed - declared)}"
    )
