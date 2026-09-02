from __future__ import annotations

import uuid
from pathlib import Path
from typing import ClassVar

import pytest
import sentry_sdk
from pydantic import ValidationError
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.core.config import (
    Settings,
    external_observability_retention_capability,
    settings,
)
from app.core.field_crypto import FieldEncryptionCodec
from app.main import _sanitize_sentry_event

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _production_settings(**overrides: object) -> Settings:
    values = settings.model_dump()
    values.update(
        {
            "FASTAPI_ENV": None,
            "SECRET_KEY": "production-signing-secret-with-sufficient-entropy",
            "FIELD_ENCRYPTION_MASTER_KEY": "11" * 32,
            "DATABASE_URL": (
                "postgresql://nightingale_app:runtime-password@localhost:5432/app"
            ),
            "MIGRATION_DATABASE_URL": None,
            "POSTGRES_APP_PASSWORD": None,
            "FIRST_SUPERUSER_PASSWORD": "production-admin-password",
            "SENTRY_DSN": None,
            "NOTIFICATION_EMAIL_PROVIDER": "disabled",
            "NOTIFICATION_SMS_PROVIDER": "disabled",
            "NOTIFICATION_WHATSAPP_PROVIDER": "disabled",
            "NOTIFICATION_WEBHOOK_SECRET": "independent-webhook-secret",
            "EXTERNAL_PROXY_RETENTION_DAYS": 30,
            "EXTERNAL_CONTAINER_RETENTION_DAYS": 30,
            "EXTERNAL_APM_RETENTION_DAYS": 30,
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE": "deployment_policy",
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID": (
                "policy:clinic-production-observability-v1"
            ),
        }
    )
    values.update(overrides)
    return Settings.model_validate(values)


def test_production_requires_independent_field_key_and_presidio_for_egress() -> None:
    with pytest.raises(ValidationError, match="FIELD_ENCRYPTION_MASTER_KEY"):
        _production_settings(FIELD_ENCRYPTION_MASTER_KEY=None)
    with pytest.raises(ValidationError, match="PRESIDIO_REQUIRED"):
        _production_settings(
            AI_PROVIDER="openai",
            REMOTE_TEXT_EGRESS_ENABLED=True,
            PRESIDIO_REQUIRED=False,
        )
    configured = _production_settings(
        AI_PROVIDER="openai",
        REMOTE_TEXT_EGRESS_ENABLED=True,
        PRESIDIO_REQUIRED=True,
    )
    assert configured.PRESIDIO_REQUIRED is True


def test_production_rejects_fake_delivery_and_incomplete_live_adapters() -> None:
    with pytest.raises(ValidationError, match="development-only"):
        _production_settings(NOTIFICATION_SMS_PROVIDER="deterministic")
    with pytest.raises(ValidationError, match="SMTP_HOST"):
        _production_settings(
            NOTIFICATION_EMAIL_PROVIDER="smtp",
            SMTP_HOST=None,
            EMAILS_FROM_EMAIL=None,
        )
    with pytest.raises(ValidationError, match="TWILIO_ACCOUNT_SID"):
        _production_settings(NOTIFICATION_SMS_PROVIDER="twilio")
    with pytest.raises(ValidationError, match="TWILIO_SMS_FROM"):
        _production_settings(
            NOTIFICATION_SMS_PROVIDER="twilio",
            TWILIO_ACCOUNT_SID="AC_fixture",
            TWILIO_AUTH_TOKEN="fixture-auth-token",
            NOTIFICATION_WEBHOOK_SECRET="independent-webhook-secret",
        )
    with pytest.raises(ValidationError, match="NOTIFICATION_PUBLIC_BASE_URL"):
        _production_settings(
            NOTIFICATION_SMS_PROVIDER="twilio",
            TWILIO_ACCOUNT_SID="AC_fixture",
            TWILIO_AUTH_TOKEN="fixture-auth-token",
            TWILIO_SMS_FROM="+6590000000",
        )
    with pytest.raises(ValidationError, match="independent"):
        _production_settings(
            NOTIFICATION_SMS_PROVIDER="twilio",
            TWILIO_ACCOUNT_SID="AC_fixture",
            TWILIO_AUTH_TOKEN="fixture-auth-token",
            TWILIO_SMS_FROM="+6590000000",
            NOTIFICATION_PUBLIC_BASE_URL="https://notifications.example.com",
            NOTIFICATION_WEBHOOK_SECRET="production-signing-secret-with-sufficient-entropy",
        )


def test_callback_secret_is_independent_even_in_development() -> None:
    values = settings.model_dump()
    values.update(
        {
            "FASTAPI_ENV": "development",
            "SECRET_KEY": "shared-jwt-and-callback-secret",
            "NOTIFICATION_WEBHOOK_SECRET": "shared-jwt-and-callback-secret",
        }
    )
    with pytest.raises(ValidationError, match="must be independent"):
        Settings.model_validate(values)


def test_external_observability_retention_is_qualified_and_fail_closed() -> None:
    with pytest.raises(ValidationError, match="between 1 and 30 days"):
        _production_settings(EXTERNAL_CONTAINER_RETENTION_DAYS=31)
    with pytest.raises(ValidationError, match="qualified external observability"):
        _production_settings(
            EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE="unqualified",
            EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID="policy:missing",
        )
    with pytest.raises(ValidationError, match="qualified external observability"):
        _production_settings(
            EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE="deterministic_fixture",
            EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID=(
                "fixture:nightingale:external-observability-30d"
            ),
        )

    production = _production_settings()
    production_capability = external_observability_retention_capability(production)
    assert production_capability.qualified is True
    assert production_capability.proxy_days == 30
    assert production_capability.container_days == 30
    assert production_capability.apm_days == 30
    assert production_capability.reason_code is None

    development_values = settings.model_dump()
    development_values.update(
        {
            "FASTAPI_ENV": "development",
            "EXTERNAL_PROXY_RETENTION_DAYS": 30,
            "EXTERNAL_CONTAINER_RETENTION_DAYS": 30,
            "EXTERNAL_APM_RETENTION_DAYS": 30,
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE": "deterministic_fixture",
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID": (
                "fixture:nightingale:external-observability-30d"
            ),
        }
    )
    development = Settings.model_validate(development_values)
    development_capability = external_observability_retention_capability(development)
    assert development_capability.qualified is True
    assert development_capability.evidence == "deterministic_fixture"


def test_production_rejects_every_tracked_local_fixture_secret() -> None:
    with pytest.raises(ValidationError, match="FIELD_ENCRYPTION_MASTER_KEY"):
        _production_settings(
            FIELD_ENCRYPTION_MASTER_KEY=(
                "4e69676874696e67616c652d73796e7468657469632d6465762d6b65792d3031"
            )
        )
    with pytest.raises(ValidationError, match="DATABASE_URL password"):
        _production_settings(
            DATABASE_URL=(
                "postgresql://nightingale_app:nightingale-app-local@localhost:5432/app"
            )
        )
    with pytest.raises(ValidationError, match="POSTGRES_APP_PASSWORD"):
        _production_settings(POSTGRES_APP_PASSWORD="nightingale-app-local")


def test_production_compose_overrides_demo_and_cloud_path_is_disabled() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text()
    workflow = (REPOSITORY_ROOT / ".github/workflows/deploy.yml").read_text()
    assert compose.count('FASTAPI_ENV: "production"') == 3
    assert compose.count('ENABLE_DEMO_AUTH: "false"') == 3
    assert (
        compose.count(
            "DATABASE_URL: postgresql://nightingale_app:${POSTGRES_APP_PASSWORD:"
        )
        == 3
    )
    assert compose.count("MIGRATION_DATABASE_URL:") == 1
    assert "fastapi deploy" not in workflow
    assert "FASTAPI_CLOUD_TOKEN" not in workflow
    assert "python -m app.ai_worker" in workflow


def test_each_traefik_provider_is_scoped_to_its_compose_project() -> None:
    """Parallel checkouts must not route identical Host rules across projects."""

    constraint = (
        "--providers.docker.constraints=Label(`com.docker.compose.project`,"
        "`${COMPOSE_PROJECT_NAME}`)"
    )
    base = (REPOSITORY_ROOT / "compose.yml").read_text()
    deploy = (REPOSITORY_ROOT / "compose.deploy.yml").read_text()
    assert constraint in base
    assert constraint in deploy


def test_proxy_and_asgi_access_logs_exclude_raw_request_targets() -> None:
    base = (REPOSITORY_ROOT / "compose.yml").read_text()
    deploy = (REPOSITORY_ROOT / "compose.deploy.yml").read_text()
    dev_tools = (REPOSITORY_ROOT / "compose.dev-tools.yml").read_text()
    dockerfile = (REPOSITORY_ROOT / "backend/Dockerfile").read_text()
    main = (REPOSITORY_ROOT / "backend/app/main.py").read_text()

    for compose in (base, deploy, dev_tools):
        assert "--accesslog.fields.defaultmode=drop" in compose
        assert "RequestPath=keep" not in compose
        assert "RequestURI=keep" not in compose
        assert "RequestHeaders=keep" not in compose
        for field in (
            "StartUTC",
            "Duration",
            "RequestMethod",
            "RouterName",
            "ServiceName",
            "DownstreamStatus",
        ):
            assert f"--accesslog.fields.names.{field}=keep" in compose
    assert "--log.level=DEBUG" not in dev_tools
    assert "--log.level=INFO" in dev_tools
    assert '"--no-access-log"' in dockerfile
    assert "class SafeAccessLogMiddleware" in main
    assert (
        'scope.get("path"'
        not in main.split("class SafeAccessLogMiddleware", 1)[1].split(
            "app.add_middleware(SafeAccessLogMiddleware)", 1
        )[0]
    )


def test_operational_observability_retention_defaults_to_30_days() -> None:
    assert Settings.model_fields["OBSERVABILITY_RETENTION_DAYS"].default == 30
    assert (
        Settings.model_fields["OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS"].default
        == 3600
    )
    assert (
        "OBSERVABILITY_RETENTION_DAYS=30"
        in (REPOSITORY_ROOT / ".env.example").read_text()
    )
    assert (
        "OBSERVABILITY_RETENTION_DAYS: ${OBSERVABILITY_RETENTION_DAYS:-30}"
        in (REPOSITORY_ROOT / "compose.yml").read_text()
    )
    assert (
        "OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS=3600"
        in (REPOSITORY_ROOT / ".env.example").read_text()
    )
    assert (
        "OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS: "
        "${OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS:-3600}"
        in (REPOSITORY_ROOT / "compose.yml").read_text()
    )
    repository = (
        REPOSITORY_ROOT / "backend/app/services/operational_events.py"
    ).read_text()
    assert "DELETE FROM operational_events WHERE expires_at <= ?" in repository
    assert "run_operational_event_purge_loop" in repository
    assert "settings.OBSERVABILITY_RETENTION_DAYS" in repository


@pytest.mark.parametrize("retention_days", [0, 31])
def test_repository_owned_operational_retention_cannot_exceed_30_days(
    retention_days: int,
) -> None:
    with pytest.raises(ValidationError, match="OBSERVABILITY_RETENTION_DAYS"):
        _production_settings(OBSERVABILITY_RETENTION_DAYS=retention_days)

    with pytest.raises(
        ValidationError,
        match="OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS",
    ):
        _production_settings(OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS=0)

    with pytest.raises(
        ValidationError,
        match="OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS",
    ):
        _production_settings(OPERATIONAL_EVENT_PURGE_INTERVAL_SECONDS=3601)


def test_compose_requires_external_retention_evidence_and_bounds_container_logs() -> (
    None
):
    env_example = (REPOSITORY_ROOT / ".env.example").read_text()
    base = (REPOSITORY_ROOT / "compose.yml").read_text()
    development = (REPOSITORY_ROOT / "compose.override.yml").read_text()
    deployment = (REPOSITORY_ROOT / "compose.deploy.yml").read_text()

    for setting in (
        "EXTERNAL_PROXY_RETENTION_DAYS",
        "EXTERNAL_CONTAINER_RETENTION_DAYS",
        "EXTERNAL_APM_RETENTION_DAYS",
    ):
        assert f"{setting}=30" in env_example
        assert base.count(f"{setting}: ${{{setting}:-30}}") == 3
        assert development.count(f'{setting}: "30"') == 3
        assert f"{setting}: ${{{setting}:?" in deployment
    assert (
        "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE=deterministic_fixture" in env_example
    )
    assert (
        base.count(
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE: "
            "${EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE:-unqualified}"
        )
        == 3
    )
    assert (
        development.count(
            'EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE: "deterministic_fixture"'
        )
        == 3
    )
    assert (
        "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE:?Set deployment_policy "
        "or provider_contract"
    ) in deployment
    assert "x-bounded-container-logging: &bounded-container-logging" in deployment
    assert deployment.count("logging: *bounded-container-logging") == 5
    assert 'max-size: "${CONTAINER_LOG_MAX_SIZE:-20m}"' in deployment
    assert 'max-file: "${CONTAINER_LOG_MAX_FILES:-5}"' in deployment
    assert "com.nightingale.observability.proxy-retention-days" in deployment
    assert "com.nightingale.observability.retention-evidence-id" in deployment

    setup_ci = (REPOSITORY_ROOT / "scripts/setup-ci-env.sh").read_text()
    verify_release = (REPOSITORY_ROOT / "scripts/verify-release.sh").read_text()
    deploy_workflow = (
        REPOSITORY_ROOT / ".github/workflows/deploy-docker-compose.yml"
    ).read_text()
    for setting in (
        "EXTERNAL_PROXY_RETENTION_DAYS",
        "EXTERNAL_CONTAINER_RETENTION_DAYS",
        "EXTERNAL_APM_RETENTION_DAYS",
        "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE",
        "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID",
    ):
        assert f"{setting}=" in setup_ci
        assert f"export {setting}=" in verify_release
        assert f"{setting}: ${{{{ vars.{setting} }}}}" in deploy_workflow
    assert (
        deployment.count(
            "NOTIFICATION_WEBHOOK_SECRET: "
            "${NOTIFICATION_WEBHOOK_SECRET:?Set an independent callback secret}"
        )
        == 3
    )
    assert 'notification_webhook_secret="$(openssl rand -hex 32)"' in setup_ci
    assert 'export NOTIFICATION_WEBHOOK_SECRET="$(openssl rand -hex 32)"' in (
        verify_release
    )
    assert (
        "NOTIFICATION_WEBHOOK_SECRET: ${{ secrets.NOTIFICATION_WEBHOOK_SECRET }}"
        in deploy_workflow
    )


def test_jwt_secret_rotation_preserves_fields_with_constant_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    record_id = uuid.uuid4()
    monkeypatch.setattr(settings, "FIELD_ENCRYPTION_MASTER_KEY", "22" * 32)
    monkeypatch.setattr(settings, "SECRET_KEY", "old-jwt-secret")
    before_rotation = FieldEncryptionCodec()
    encrypted = before_rotation.encrypt_text(
        clinic_id, "synthetic.rotation", record_id, "synthetic clinical payload"
    )

    monkeypatch.setattr(settings, "SECRET_KEY", "new-independent-jwt-secret")
    after_rotation = FieldEncryptionCodec()
    assert (
        after_rotation.decrypt_text(
            clinic_id, "synthetic.rotation", record_id, encrypted
        )
        == "synthetic clinical payload"
    )


class _CaptureTransport(Transport):
    envelopes: ClassVar[list[bytes]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope.serialize())


def test_sentry_transport_never_serializes_request_body_or_sensitive_headers() -> None:
    marker = "S1234567D SYNTHETIC CLINICAL BODY"
    _CaptureTransport.envelopes = []
    client = sentry_sdk.Client(
        dsn="http://public@example.invalid/1",
        transport=_CaptureTransport,
        default_integrations=False,
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=_sanitize_sentry_event,
        before_send_transaction=_sanitize_sentry_event,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        sentry_sdk.capture_event(
            {
                "message": marker,
                "culprit": marker,  # type: ignore[typeddict-unknown-key]
                "server_name": marker,
                "transaction": marker,
                "threads": {"values": [{"name": marker}]},
                "stacktrace": {"frames": [{"vars": {"patient": marker}}]},
                "user": {"id": marker, "email": marker},
                "tags": {"patient": marker},
                "extra": {"clinical": marker},
                "contexts": {"request": {"url": marker, "body": marker}},
                "breadcrumbs": {
                    "values": [
                        {
                            "category": "http",
                            "message": marker,
                            "data": {"url": marker, "headers": marker},
                        }
                    ]
                },
                "exception": {
                    "values": [
                        {
                            "type": "SyntheticError",
                            "value": marker,
                            "mechanism": {"type": "generic", "data": marker},
                            "stacktrace": {"frames": [{"vars": {"patient": marker}}]},
                        }
                    ]
                },
                "request": {
                    "url": f"https://nightingale.test/patients/{marker}/ai/ingest",
                    "path_info": f"/patients/{marker}/ai/ingest",
                    "fragment": marker,
                    "data": marker,
                    "body": marker,
                    "headers": {
                        "authorization": marker,
                        "cookie": marker,
                    },
                    "query_string": marker,
                    "unrecognized_free_form_carrier": marker,
                },
                "spans": [
                    {
                        "span_id": "0123456789abcdef",
                        "trace_id": "0123456789abcdef0123456789abcdef",
                        "start_timestamp": 1.0,
                        "timestamp": 2.0,
                        "op": "http.server",
                        "data": {
                            "http.request.body.data": marker,
                            "http.request.header.authorization": marker,
                        },
                    }
                ],
            }
        )
        client.flush(timeout=1)
    client.close(timeout=1)

    assert _CaptureTransport.envelopes
    assert marker.encode() not in b"".join(_CaptureTransport.envelopes)
