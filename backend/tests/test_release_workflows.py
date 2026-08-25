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
    assert "group: production-main" in workflow
    assert "cancel-in-progress: false" in workflow


def test_compose_deploy_uses_one_inspected_content_addressed_backend_image() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "deploy-docker-compose.yml"
    ).read_text()
    compose = (ROOT / "compose.yml").read_text()
    deploy_compose = (ROOT / "compose.deploy.yml").read_text()

    assert compose.count("image: ${NIGHTINGALE_BACKEND_IMAGE:-backend:latest}") == 3
    assert (
        deploy_compose.count(
            "image: ${NIGHTINGALE_BACKEND_IMAGE:?Set to a verified release image}"
        )
        == 3
    )
    assert (
        "NIGHTINGALE_BACKEND_IMAGE: nightingale-backend:${{ github.sha }}" in workflow
    )
    assert (
        "docker compose -f compose.yml -f compose.deploy.yml build backend" in workflow
    )
    assert "docker image inspect --format '{{.Id}}'" in workflow
    assert "org.opencontainers.image.revision" in workflow
    assert (
        'printf \'NIGHTINGALE_BACKEND_IMAGE=%s\\n\' "$image_id" >> "$GITHUB_ENV"'
        in workflow
    )
    build = workflow.index("Build and bind the immutable release image")
    migrate = workflow.index("Prepare database")
    start = workflow.index("Start application")
    assert build < migrate < start


def test_playwright_required_check_always_runs_and_cannot_allow_skip() -> None:
    workflow = (ROOT / ".github" / "workflows" / "playwright.yml").read_text()
    assert "paths-filter" not in workflow
    assert "allowed-skips" not in workflow
    assert "needs:\n      - test-playwright" in workflow
    assert "--repeat-each=3 --workers=1" in workflow
    assert "run --rm --no-deps playwright" in workflow
    assert "COMPOSE_PROJECT_NAME: nightingale-playwright-" in workflow


def test_compose_ci_starts_proxy_and_always_cleans_its_named_project() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test-docker-compose.yml").read_text()
    assert "COMPOSE_PROJECT_NAME: nightingale-compose-ci-" in workflow
    assert "trap cleanup EXIT INT TERM" in workflow
    assert "up -d --wait proxy backend ai-worker" in workflow
    assert "https://localhost/api/v1/utils/health-check/" in workflow


def test_backend_ci_never_reuses_or_blindly_deletes_a_compose_project() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test-backend.yml").read_text()
    assert "temporary-project-name.sh test" in workflow
    assert "free-local-port.py" in workflow
    assert "assert-compose-project-empty.sh" in workflow
    assert "assert-demo-project-ownership.sh" in workflow
    assert "trap 'cleanup || true' EXIT INT TERM" in workflow
    assert "docker compose down -v" not in workflow
    assert "127.0.0.1:5432" not in workflow


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
