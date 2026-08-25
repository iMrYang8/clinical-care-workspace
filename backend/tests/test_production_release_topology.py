from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_deploy_ports_are_public_by_default_but_overridable_for_isolated_gate() -> None:
    deploy = (ROOT / "compose.deploy.yml").read_text()

    assert (
        '"${NIGHTINGALE_PRODUCTION_BIND_ADDRESS:-0.0.0.0}:'
        '${NIGHTINGALE_PRODUCTION_HTTP_PORT:-80}:80"'
    ) in deploy
    assert (
        '"${NIGHTINGALE_PRODUCTION_BIND_ADDRESS:-0.0.0.0}:'
        '${NIGHTINGALE_PRODUCTION_HTTPS_PORT:-443}:443"'
    ) in deploy


def test_release_gate_runs_the_same_image_in_the_production_compose_topology() -> None:
    script = (ROOT / "scripts" / "verify-release.sh").read_text()

    assert "assert_production_release_topology" in script
    assert "assert-production-project-ownership.sh" in script
    assert "unset NIGHTINGALE_BACKEND_IMAGE" in script
    assert 'NIGHTINGALE_BACKEND_IMAGE="$verified_backend_image_id"' in script
    assert "-f compose.yml -f compose.deploy.yml" in script
    assert "--no-build" in script
    assert "FASTAPI_ENV=production" in script
    assert "ENABLE_DEMO_AUTH=false" in script
    assert "Production prestart unexpectedly seeded demo data." in script
    assert "Production demo-login unexpectedly succeeded" in script
    assert '"https://${DOMAIN}:${production_https_port}' in script


def test_production_cleanup_guard_accepts_only_scoped_deploy_projects() -> None:
    guard = ROOT / "scripts" / "assert-production-project-ownership.sh"
    assert guard.is_file()
    text = guard.read_text()

    assert "nightingale-release" in text
    assert "compose.deploy.yml" in text
    assert "compose.override.yml" not in text
    assert "com.nightingale.checkout_fingerprint" in text
    assert "com.docker.compose.project.working_dir" in text


def test_deploy_resources_carry_checkout_fingerprint_for_scoped_cleanup() -> None:
    deploy = (ROOT / "compose.deploy.yml").read_text()

    assert deploy.count("com.nightingale.checkout_fingerprint") >= 3
    assert "nightingale-db-data:" in deploy
    assert "traefik-certificates:" in deploy
    assert "networks:" in deploy
