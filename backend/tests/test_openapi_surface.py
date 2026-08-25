import pytest

from app.main import app

pytestmark = pytest.mark.unit


def test_delivery_openapi_surface_is_complete() -> None:
    paths = set(app.openapi()["paths"])
    required = {
        "/api/v1/auth/demo-login",
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
        "/api/v1/patients/{patient_id}/ai/ingest",
        "/api/v1/jobs/{job_id}",
        "/api/v1/highlights/{highlight_id}/feedback",
        "/api/v1/decay/preview",
        "/api/v1/decay/entries/{version_id}/rehydrate",
        "/api/v1/voice/sessions",
        "/api/v1/admin/memberships",
        "/api/v1/admin/audit",
    }
    assert required <= paths
