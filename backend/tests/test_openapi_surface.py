import pytest

from app.main import app

pytestmark = pytest.mark.unit


def test_delivery_openapi_surface_is_complete() -> None:
    document = app.openapi()
    paths = set(document["paths"])
    required = {
        "/api/v1/auth/demo-login",
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
        "/api/v1/auth/invitations/accept",
        "/api/v1/patients/{patient_id}/ai/ingest",
        "/api/v1/jobs/{job_id}",
        "/api/v1/highlights/{highlight_id}/feedback",
        "/api/v1/decay/preview",
        "/api/v1/decay/entries/{version_id}/rehydrate",
        "/api/v1/voice/sessions",
        "/api/v1/admin/memberships",
        "/api/v1/admin/audit",
        "/api/v1/team/members",
    }
    assert required <= paths
    clinic_code = next(
        parameter
        for parameter in document["paths"]["/api/v1/auth/login"]["post"]["parameters"]
        if parameter["name"] == "X-Clinic-Code"
    )
    assert clinic_code["in"] == "header"
    assert clinic_code["required"] is True

    for path, method in (
        ("/api/v1/comments/{comment_id}/resolve", "post"),
        ("/api/v1/comments/{comment_id}/unresolve", "post"),
        ("/api/v1/comments/{comment_id}/assignment", "patch"),
    ):
        operation = document["paths"][path][method]
        assert "ETag" in operation["responses"]["200"]["headers"]
        assert operation["responses"]["409"]["description"]
        assert operation["responses"]["428"]["description"]


def test_voice_audio_quality_contract_is_typed_and_allowlisted() -> None:
    schemas = app.openapi()["components"]["schemas"]
    quality = schemas["AudioQualityPublic"]

    assert quality["additionalProperties"] is False
    assert set(quality["required"]) == {
        "measurement_stage",
        "processing_chain_version",
        "rms",
        "noise_floor_dbfs",
        "estimated_snr_db",
        "clipping_ratio",
        "silence_ratio",
        "silence_review",
        "clipping_review",
        "low_signal_review",
        "noise_review",
        "multi_device_overlap_review",
        "denoise_applied",
        "review_required",
    }
    assert quality["properties"]["measurement_stage"]["const"] == (
        "decoded-pre-normalization"
    )
    assert quality["properties"]["clipping_ratio"]["minimum"] == 0
    assert quality["properties"]["clipping_ratio"]["maximum"] == 1
    assert "device_signals" not in quality["properties"]
    assert "normalized_output_signals" not in quality["properties"]
    assert "denoise_filter" not in quality["properties"]

    for schema_name in ("VoiceSessionPublic", "TranscriptRevisionPublic"):
        schema = schemas[schema_name]
        assert {
            "audio_quality",
            "audio_quality_unavailable_reason",
        } <= set(schema["required"])
        quality_variant = schema["properties"]["audio_quality"]["anyOf"][0]
        assert quality_variant["$ref"] == "#/components/schemas/AudioQualityPublic"
        unavailable = schema["properties"]["audio_quality_unavailable_reason"]
        assert unavailable["anyOf"][0]["enum"] == [
            "AUDIO_ASSET_NOT_AVAILABLE",
            "AUDIO_QUALITY_METADATA_INVALID",
        ]
