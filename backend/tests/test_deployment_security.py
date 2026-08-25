from __future__ import annotations

import uuid
from pathlib import Path
from typing import ClassVar

import pytest
import sentry_sdk
from pydantic import ValidationError
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.core.config import Settings, settings
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


def test_production_compose_and_cloud_workflow_override_demo_boundary() -> None:
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
    assert workflow.count("FASTAPI_ENV: production") == 2
    assert workflow.count('ENABLE_DEMO_AUTH: "false"') == 2


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
