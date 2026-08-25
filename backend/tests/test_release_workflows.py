import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name",
    ["deploy.yml", "deploy-docker-compose.yml"],
)
def test_deployment_waits_for_main_sha_release_gate(name: str) -> None:
    workflow = (ROOT / ".github" / "workflows" / name).read_text()
    gate, deploy = workflow.split("\n  deploy:\n", maxsplit=1)
    assert "release-gates:" in gate
    assert "github.ref == 'refs/heads/main'" in gate
    assert "scripts/verify-release.sh" in gate
    assert "verified-sha" in gate
    assert "secrets." not in gate
    assert "needs: release-gates" in deploy
    assert "needs.release-gates.result == 'success'" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "name: production" in deploy
    assert "ref: ${{ github.sha }}" in deploy
    assert "git rev-parse HEAD" in deploy


def test_playwright_required_check_always_runs_and_cannot_allow_skip() -> None:
    workflow = (ROOT / ".github" / "workflows" / "playwright.yml").read_text()
    assert "paths-filter" not in workflow
    assert "allowed-skips" not in workflow
    assert "needs:\n      - test-playwright" in workflow
    assert "--repeat-each=3 --workers=1" in workflow
    assert "COMPOSE_PROJECT_NAME: nightingale-playwright-" in workflow


def test_compose_ci_starts_proxy_and_always_cleans_its_named_project() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test-docker-compose.yml").read_text()
    assert "COMPOSE_PROJECT_NAME: nightingale-compose-ci-" in workflow
    assert "trap cleanup EXIT INT TERM" in workflow
    assert "up -d --wait proxy backend ai-worker" in workflow
    assert "https://localhost/api/v1/utils/health-check/" in workflow


def test_openapi_schema_is_a_tracked_sync_artifact() -> None:
    schema = ROOT / "frontend" / "openapi.json"
    assert schema.is_file() and schema.stat().st_size > 1_000
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", "frontend/openapi.json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    workflow = (ROOT / ".github" / "workflows" / "openapi-sync.yml").read_text()
    assert "git ls-files --error-unmatch frontend/openapi.json" in workflow
    assert (
        "git diff --exit-code -- frontend/openapi.json frontend/src/client" in workflow
    )
