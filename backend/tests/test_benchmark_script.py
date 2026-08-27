from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


def _benchmark_module() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_glance.py"
    spec = importlib.util.spec_from_file_location("benchmark_glance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark_glance = _benchmark_module()


def test_benchmark_selects_the_named_fixture_instead_of_the_first_patient() -> None:
    patients = [
        {"id": "empty-first", "display_name": "No Glance Fixture"},
        {"id": "alex", "display_name": "Alex Tan"},
    ]

    selected = benchmark_glance.select_fixture_patient(
        patients,
        display_name="Alex Tan",
        patient_id=None,
    )

    assert selected == patients[1]


def test_benchmark_requires_one_unambiguous_fixture_identity() -> None:
    duplicate = [
        {"id": "alex-1", "display_name": "Alex Tan"},
        {"id": "alex-2", "display_name": "Alex Tan"},
    ]

    with pytest.raises(RuntimeError, match="exactly one"):
        benchmark_glance.select_fixture_patient(
            duplicate,
            display_name="Alex Tan",
            patient_id=None,
        )


@pytest.mark.parametrize(
    ("payload", "expected", "message"),
    [
        (
            {"source": "precomputed", "patient_id": "alex", "cards": []},
            4,
            "empty",
        ),
        (
            {
                "source": "precomputed",
                "patient_id": "alex",
                "cards": [{"id": "one"}],
            },
            4,
            "expected 4",
        ),
        (
            {
                "source": "on-demand",
                "patient_id": "alex",
                "cards": [{"id": str(i)} for i in range(4)],
            },
            4,
            "non-precomputed",
        ),
    ],
)
def test_benchmark_rejects_wrong_or_empty_glance_payloads(
    payload: dict[str, object], expected: int, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        benchmark_glance.validate_glance(
            payload, expected_card_count=expected, expected_patient_id="alex"
        )


def test_benchmark_records_the_expected_nonempty_card_count() -> None:
    payload = {
        "source": "precomputed",
        "patient_id": "alex",
        "cards": [{"id": str(index)} for index in range(4)],
    }

    assert (
        benchmark_glance.validate_glance(
            payload, expected_card_count=4, expected_patient_id="alex"
        )
        == 4
    )


def test_benchmark_response_fingerprint_is_order_independent() -> None:
    left = {
        "source": "precomputed",
        "patient_id": "alex",
        "cards": [{"label": "A", "id": "one"}],
    }
    right = {
        "cards": [{"id": "one", "label": "A"}],
        "patient_id": "alex",
        "source": "precomputed",
    }

    assert benchmark_glance.response_fingerprint(left) == (
        benchmark_glance.response_fingerprint(right)
    )


def test_compose_identity_binds_running_backend_image_to_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outputs = iter(
        [
            '{"services": {}}',
            "backend-container-id",
            "sha256:immutable-image-id",
            "candidate-commit",
        ]
    )
    monkeypatch.setattr(
        benchmark_glance,
        "command_output",
        lambda command, *, cwd: next(outputs),
    )

    identity = benchmark_glance.compose_identity(
        tmp_path, "release-project", expected_commit="candidate-commit"
    )

    assert identity["project"] == "release-project"
    assert identity["backend_image_digest"] == "sha256:immutable-image-id"
    assert identity["backend_image_revision"] == "candidate-commit"


def test_compose_identity_rejects_stale_running_backend_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outputs = iter(
        [
            '{"services": {}}',
            "backend-container-id",
            "sha256:immutable-image-id",
            "older-commit",
        ]
    )
    monkeypatch.setattr(
        benchmark_glance,
        "command_output",
        lambda command, *, cwd: next(outputs),
    )

    with pytest.raises(RuntimeError, match="does not match checkout"):
        benchmark_glance.compose_identity(
            tmp_path, "release-project", expected_commit="candidate-commit"
        )


def test_compose_identity_requires_a_running_project(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="--compose-project"):
        benchmark_glance.compose_identity(
            tmp_path, None, expected_commit="candidate-commit"
        )


def test_release_dirty_gate_ignores_test_bytecode_but_detects_real_changes(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    (tmp_path / "tracked.txt").write_text("clean\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    cache = tmp_path / "scripts" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "benchmark.cpython-312.pyc").write_bytes(b"test bytecode")
    assert benchmark_glance.git_is_dirty(tmp_path) is False

    (tmp_path / "real-change.txt").write_text("dirty\n")
    assert benchmark_glance.git_is_dirty(tmp_path) is True
